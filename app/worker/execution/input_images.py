"""Materialize validated task images outside the checkout for one agent invocation."""

from pathlib import Path

from app.contracts.input_images import decode_image, image_ref


def write_images(images: tuple[str, ...], directory: Path) -> list[Path]:
    paths = []
    for url in images:
        kind, data = decode_image(url)
        path = directory / f"{image_ref(url).removeprefix('image:')}.{kind}"
        path.write_bytes(data)
        paths.append(path)
    return paths
