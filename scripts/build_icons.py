#!/usr/bin/env python3
"""Render mhm's vector mark and deterministic toolbar state indicators."""

from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1] / "extension/icons"


def render(size, color=None, opacity=255):
    scale = size * 4 / 64
    image = Image.new("RGBA", (size * 4, size * 4))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, size * 4 - 1, size * 4 - 1), radius=18 * scale, fill="#153D3A"
    )
    points = [
        (int(x * scale), int(y * scale))
        for x, y in [
            (13, 44),
            (13, 24),
            (23, 34),
            (32, 19),
            (41, 34),
            (51, 24),
            (51, 44),
        ]
    ]
    draw.line(points, fill="#93F0D0", width=round(6 * scale), joint="curve")
    for x, y in points:
        draw.ellipse(
            (x - 3 * scale, y - 3 * scale, x + 3 * scale, y + 3 * scale), fill="#93F0D0"
        )
    if color:
        draw.ellipse((38 * scale, 38 * scale, 64 * scale, 64 * scale), fill="#F4F6F7")
        draw.ellipse((42 * scale, 42 * scale, 60 * scale, 60 * scale), fill=color)
        if opacity < 255:
            draw.ellipse(
                (46 * scale, 46 * scale, 56 * scale, 56 * scale), fill="#F4F6F7"
            )
    return image.resize((size, size), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        render(size).save(ROOT / f"icon-{size}.png")
    for size in (16, 32):
        for state, color in [
            ("connected", "#16A36B"),
            ("blocked", "#D54256"),
            ("disconnected", None),
        ]:
            render(size, color).save(ROOT / f"status-{state}-{size}.png")
        for frame, color in enumerate(["#E5A526", "#F2C45A", "#BC7D0D"]):
            render(size, color).save(ROOT / f"status-connecting-{frame}-{size}.png")
