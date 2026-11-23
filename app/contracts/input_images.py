"""Bounded raster image inputs; only short content references appear in task text."""

import base64
import hashlib
from typing import Annotated

from pydantic import AfterValidator, Field

MAX_IMAGE_BYTES = 1024 * 1024
_SIGNATURES = {"png": b"\x89PNG\r\n\x1a\n", "jpeg": b"\xff\xd8\xff", "webp": b"RIFF"}


def decode_image(url: str) -> tuple[str, bytes]:
    """Decode only the raster data URL types accepted by the composer."""
    header, _, payload = url.partition(",")
    kind = header.removeprefix("data:image/").removesuffix(";base64")
    if kind not in _SIGNATURES or header != f"data:image/{kind};base64":
        raise ValueError("expected PNG, JPEG or WebP")
    data = base64.b64decode(payload, validate=True)
    if (len(data) > MAX_IMAGE_BYTES or not data.startswith(_SIGNATURES[kind])
            or (kind == "webp" and data[8:12] != b"WEBP")):
        raise ValueError("invalid image content or size")
    return kind, data


def _validate(url: str) -> str:
    decode_image(url)
    return url


InputImage = Annotated[str, Field(max_length=4 * ((MAX_IMAGE_BYTES + 2) // 3) + 32), AfterValidator(_validate)]


def image_ref(url: str) -> str:
    return "image:" + hashlib.sha256(url.encode("ascii")).hexdigest()


def attach_refs(text: str, images: list[str]) -> str:
    return "\n\n".join(filter(None, [text, *(image_ref(url) for url in images)]))
