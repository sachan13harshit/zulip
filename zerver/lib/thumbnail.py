import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import urljoin

import pyvips
from bs4 import BeautifulSoup
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from typing_extensions import override

from zerver.lib.camo import get_camo_url
from zerver.lib.exceptions import ErrorCode, JsonableError
from zerver.lib.queue import queue_event_on_commit
from zerver.models import AbstractAttachment, ImageAttachment

DEFAULT_AVATAR_SIZE = 100
MEDIUM_AVATAR_SIZE = 500
DEFAULT_EMOJI_SIZE = 64

# We refuse to deal with any image whose total pixelcount exceeds this.
IMAGE_BOMB_TOTAL_PIXELS = 90000000

# Reject emoji which, after resizing, have stills larger than this
MAX_EMOJI_GIF_FILE_SIZE_BYTES = 128 * 1024  # 128 kb

T = TypeVar("T", bound="BaseThumbnailFormat")


@dataclass(frozen=True)
class BaseThumbnailFormat:
    extension: str
    max_width: int
    max_height: int
    animated: bool

    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BaseThumbnailFormat):
            return False
        return str(self) == str(other)

    @override
    def __str__(self) -> str:
        animated = "-anim" if self.animated else ""
        return f"{self.max_width}x{self.max_height}{animated}.{self.extension}"

    @classmethod
    def from_string(cls: type[T], format_string: str) -> T | None:
        format_parts = re.match(r"(\d+)x(\d+)(-anim)?\.(\w+)$", format_string)
        if format_parts is None:
            return None

        return cls(
            max_width=int(format_parts[1]),
            max_height=int(format_parts[2]),
            animated=format_parts[3] is not None,
            extension=format_parts[4],
        )


@dataclass(frozen=True, eq=False)
class ThumbnailFormat(BaseThumbnailFormat):
    opts: str | None = ""


# Note that this is serialized into a JSONB column in the database,
# and as such fields cannot be removed without a migration.
@dataclass(frozen=True, eq=False)
class StoredThumbnailFormat(BaseThumbnailFormat):
    content_type: str
    width: int
    height: int
    byte_size: int


# Formats that we generate; the first animated and non-animated
# options on this list are the ones which are written into
# rendered_content.
THUMBNAIL_OUTPUT_FORMATS = [
    # For now, we generate relatively large default "thumbnails", so
    # that clients that do not understand the thumbnailing protocol
    # (e.g. mobile) get something which does not look pixelated.
    ThumbnailFormat("webp", 840, 560, animated=True),
    ThumbnailFormat("webp", 840, 560, animated=False),
    # 300x200 is the size preferred by the web client.
    ThumbnailFormat("webp", 300, 200, animated=True),
    ThumbnailFormat("webp", 300, 200, animated=False),
    ThumbnailFormat("jpg", 300, 200, animated=False),
]


# These are the image content-types which the server supports parsing
# and thumbnailing; these do not need to supported on all browsers,
# since we will the serving thumbnailed versions of them.  Note that
# this does not provide any *security*, since the content-type is
# provided by the browser, and may not match the bytes they uploaded.
#
# This should be kept synced with the client-side image-picker in
# web/upload_widget.ts.  Any additions below must be accompanied by
# changes to the pyvips block below as well.
THUMBNAIL_ACCEPT_IMAGE_TYPES = frozenset(
    [
        "image/avif",
        "image/gif",
        "image/heic",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
    ]
)

# This is what enforces security limitations on which formats are
# parsed; we disable all loaders, then re-enable the ones we support
# -- then explicitly disable any "untrusted" ones, in case libvips for
# some reason marks one of the above formats as such (because they are
# no longer fuzzed, for instance).
#
# Note that only libvips >= 8.13 (Uubntu 24.04 or later, Debian 12 or
# later) supports this!  These are no-ops on earlier versions of libvips.
pyvips.operation_block_set("VipsForeignLoad", True)
pyvips.operation_block_set("VipsForeignLoadHeif", False)  # image/avif, image/heic
pyvips.operation_block_set("VipsForeignLoadNsgif", False)  # image/gif
pyvips.operation_block_set("VipsForeignLoadJpeg", False)  # image/jpeg
pyvips.operation_block_set("VipsForeignLoadPng", False)  # image/png
pyvips.operation_block_set("VipsForeignLoadTiff", False)  # image/tiff
pyvips.operation_block_set("VipsForeignLoadWebp", False)  # image/webp
pyvips.block_untrusted_set(True)


class BadImageError(JsonableError):
    code = ErrorCode.BAD_IMAGE


def generate_thumbnail_url(path: str, size: str = "0x0") -> str:
    path = urljoin("/", path)

    if url_has_allowed_host_and_scheme(path, allowed_hosts=None):
        return path
    return get_camo_url(path)


@contextmanager
def libvips_check_image(image_data: bytes) -> Iterator[pyvips.Image]:
    # The primary goal of this is to verify that the image is valid,
    # and raise BadImageError otherwise.  The yielded `source_image`
    # may be ignored, since calling `thumbnail_buffer` is faster than
    # calling `thumbnail_image` on a pyvips.Image, since the latter
    # cannot make use of shrink-on-load optimizations:
    # https://www.libvips.org/API/current/libvips-resample.html#vips-thumbnail-image
    try:
        source_image = pyvips.Image.new_from_buffer(image_data, "")
    except pyvips.Error:
        raise BadImageError(_("Could not decode image; did you upload an image file?"))

    if (
        source_image.width * source_image.height * source_image.get_n_pages()
        > IMAGE_BOMB_TOTAL_PIXELS
    ):
        raise BadImageError(_("Image size exceeds limit."))

    try:
        yield source_image
    except pyvips.Error as e:  # nocoverage
        logging.exception(e)
        raise BadImageError(_("Image is corrupted or truncated"))


def resize_avatar(image_data: bytes, size: int = DEFAULT_AVATAR_SIZE) -> bytes:
    # This will scale up, if necessary, and will scale the smallest
    # dimension to fit.  That is, a 1x1000 image will end up with the
    # one middle pixel enlarged to fill the full square.
    with libvips_check_image(image_data):
        return pyvips.Image.thumbnail_buffer(
            image_data,
            size,
            height=size,
            crop=pyvips.Interesting.CENTRE,
        ).write_to_buffer(".png")


def resize_logo(image_data: bytes) -> bytes:
    # This will only scale the image down, and will resize it to
    # preserve aspect ratio and be contained within 8*AVATAR by AVATAR
    # pixels; it does not add any padding to make it exactly that
    # size.  A 1000x10 pixel image will end up as 800x8; a 10x10 will
    # end up 10x10.
    with libvips_check_image(image_data):
        return pyvips.Image.thumbnail_buffer(
            image_data,
            8 * DEFAULT_AVATAR_SIZE,
            height=DEFAULT_AVATAR_SIZE,
            size=pyvips.Size.DOWN,
        ).write_to_buffer(".png")


def resize_emoji(
    image_data: bytes, emoji_file_name: str, size: int = DEFAULT_EMOJI_SIZE
) -> tuple[bytes, bytes | None]:
    # Square brackets are used for providing options to libvips' save
    # operation; the extension on the filename comes from reversing
    # the content-type, which removes most of the attacker control of
    # this string, but assert it has no bracketed pieces for safety.
    write_file_ext = os.path.splitext(emoji_file_name)[1]
    assert "[" not in write_file_ext

    # This function returns two values:
    # 1) Emoji image data.
    # 2) If it is animated, the still image data i.e. first frame of gif.
    with libvips_check_image(image_data) as source_image:
        if source_image.get_n_pages() == 1:
            return (
                pyvips.Image.thumbnail_buffer(
                    image_data,
                    size,
                    height=size,
                    crop=pyvips.Interesting.CENTRE,
                ).write_to_buffer(write_file_ext),
                None,
            )
        first_still = pyvips.Image.thumbnail_buffer(
            image_data,
            size,
            height=size,
            crop=pyvips.Interesting.CENTRE,
        ).write_to_buffer(".png")

        animated = pyvips.Image.thumbnail_buffer(
            image_data,
            size,
            height=size,
            # This is passed to the loader, and means "load all
            # frames", instead of the default of just the first
            option_string="n=-1",
        )
        if animated.width != animated.get("page-height"):
            # If the image is non-square, we have to iterate the
            # frames to add padding to make it so
            if not animated.hasalpha():
                animated = animated.addalpha()
            frames = [
                frame.gravity(
                    pyvips.CompassDirection.CENTRE,
                    size,
                    size,
                    extend=pyvips.Extend.BACKGROUND,
                    background=[0, 0, 0, 0],
                )
                for frame in animated.pagesplit()
            ]
            animated = frames[0].pagejoin(frames[1:])
        return (animated.write_to_buffer(write_file_ext), first_still)


def missing_thumbnails(image_attachment: ImageAttachment) -> list[ThumbnailFormat]:
    seen_thumbnails: set[StoredThumbnailFormat] = set()
    for existing_thumbnail in image_attachment.thumbnail_metadata:
        seen_thumbnails.add(StoredThumbnailFormat(**existing_thumbnail))

    # We use the shared `__eq__` method from BaseThumbnailFormat to
    # compare between the StoredThumbnailFormat values pulled from the
    # database, and the ThumbnailFormat values in
    # THUMBNAIL_OUTPUT_FORMATS.
    needed_thumbnails = [
        thumbnail_format
        for thumbnail_format in THUMBNAIL_OUTPUT_FORMATS
        if thumbnail_format not in seen_thumbnails
    ]

    if image_attachment.frames == 1:
        # We do not generate -anim versions if the source is still
        needed_thumbnails = [
            thumbnail_format
            for thumbnail_format in needed_thumbnails
            if not thumbnail_format.animated
        ]

    return needed_thumbnails


def maybe_thumbnail(attachment: AbstractAttachment, content: bytes) -> ImageAttachment | None:
    if attachment.content_type not in THUMBNAIL_ACCEPT_IMAGE_TYPES:
        # If it doesn't self-report as an image file that we might want
        # to thumbnail, don't parse the bytes at all.
        return None
    try:
        # This only attempts to read the header, not the full image content
        with libvips_check_image(content) as image:
            image_row = ImageAttachment.objects.create(
                realm_id=attachment.realm_id,
                path_id=attachment.path_id,
                original_width_px=image.width,
                original_height_px=image.height,
                frames=image.get_n_pages(),
                thumbnail_metadata=[],
            )
            queue_event_on_commit("thumbnail", {"id": image_row.id})
            return image_row
    except BadImageError:
        return None


def get_image_thumbnail_path(
    image_attachment: ImageAttachment,
    thumbnail_format: BaseThumbnailFormat,
) -> str:
    return f"thumbnail/{image_attachment.path_id}/{thumbnail_format!s}"


def split_thumbnail_path(file_path: str) -> tuple[str, BaseThumbnailFormat]:
    assert file_path.startswith("thumbnail/")
    path_parts = file_path.split("/")
    thumbnail_format = BaseThumbnailFormat.from_string(path_parts.pop())
    assert thumbnail_format is not None
    path_id = "/".join(path_parts[1:])
    return path_id, thumbnail_format


def get_user_upload_previews(realm_id: int, content: str) -> dict[str, tuple[str, bool] | None]:
    matches = re.findall(r"/user_uploads/(\d+/[/\w.-]+)", content)
    upload_preview_data: dict[str, tuple[str, bool] | None] = {}
    for image_attachment in ImageAttachment.objects.filter(realm_id=realm_id, path_id__in=matches):
        if image_attachment.thumbnail_metadata == []:
            # Image exists, and header of it parsed as a valid image,
            # but has not been thumbnailed yet; we will render a
            # spinner.
            upload_preview_data[image_attachment.path_id] = None
        else:
            upload_preview_data[image_attachment.path_id] = get_default_thumbnail_url(
                image_attachment
            )
    return upload_preview_data


def get_default_thumbnail_url(image_attachment: ImageAttachment) -> tuple[str, bool]:
    # For "dumb" clients which cannot rewrite it into their
    # preferred format and size, we choose the first one in
    # THUMBNAIL_OUTPUT_FORMATS which matches the animated/not
    # nature of the source image.
    found_format: ThumbnailFormat | None = None
    for thumbnail_format in THUMBNAIL_OUTPUT_FORMATS:
        if thumbnail_format.animated == (image_attachment.frames > 1):
            found_format = thumbnail_format
            break
    if found_format is None:
        # No animated thumbnail formats exist somehow, and the
        # image is animated?  Just take the first thumbnail
        # format.
        found_format = THUMBNAIL_OUTPUT_FORMATS[0]
    return (
        "/user_uploads/" + get_image_thumbnail_path(image_attachment, found_format),
        found_format.animated,
    )


def rewrite_thumbnailed_images(
    rendered_content: str,
    images: dict[str, tuple[str, bool] | None],
    to_delete: set[str] | None = None,
) -> str | None:
    if not images and not to_delete:
        return None

    parsed_message = BeautifulSoup(rendered_content, "html.parser")

    changed = False
    for inline_image_div in parsed_message.find_all("div", class_="message_inline_image"):
        image_link = inline_image_div.find("a")
        if (
            image_link is None
            or image_link["href"] is None
            or not image_link["href"].startswith("/user_uploads/")
        ):
            # This is not an inline image generated by the markdown
            # processor for a locally-uploaded image.
            continue
        image_tag = image_link.find("img", class_="image-loading-placeholder")
        if image_tag is None:
            # The placeholder was already replaced -- for instance,
            # this is expected if multiple images are included in the
            # same message.  The second time this is run, for the
            # second image, the first image will have no placeholder.
            continue

        path_id = image_link["href"][len("/user_uploads/") :]
        if to_delete and path_id in to_delete:
            # This was not a valid thumbnail target, for some reason.
            # Trim out the whole "message_inline_image" element, since
            # it's not going be renderable by clients either.
            inline_image_div.decompose()
            changed = True
            continue

        image_data = images.get(path_id)
        if image_data is None:
            # Has not been thumbnailed yet; leave it as a spinner.
            # This happens routinely when a message contained multiple
            # unthumbnailed images, and only one of those images just
            # completed thumbnailing.
            pass
        else:
            changed = True
            del image_tag["class"]
            image_tag["src"], is_animated = image_data
            if is_animated:
                image_tag["data-animated"] = "true"

    if changed:
        # The formatter="html5" means we do not produce self-closing tags
        return parsed_message.encode(formatter="html5").decode().strip()
    else:
        return None
