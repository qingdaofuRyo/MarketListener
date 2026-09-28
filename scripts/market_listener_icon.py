"""One MarketListener chart-line mark for Windows and the system tray.

The geometry and colors mirror desktop/web/public/favicon.svg, adapted from
Tabler Icons chart-line.svg (MIT; see desktop/web/public/THIRD_PARTY_LICENSES.md).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


BACKGROUND = "#111827"
STROKE = "#48e0b1"
VIEWBOX = 24
ICON_SIZES = ((16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256))


def render_icon(size: int = 64) -> Image.Image:
    """Rasterize the favicon's rounded square and two paths with antialiasing."""

    if size < 16:
        raise ValueError("icon size must be at least 16 pixels")
    scale = 4
    canvas_size = size * scale
    unit = canvas_size / VIEWBOX
    image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, canvas_size - 1, canvas_size - 1),
        radius=round(5 * unit),
        fill=BACKGROUND,
    )
    width = max(1, round(2 * unit))
    paths = (
        ((4, 19), (20, 19)),
        ((4, 15), (8, 9), (12, 11), (16, 6), (20, 10)),
    )
    for path in paths:
        points = [(round(x * unit), round(y * unit)) for x, y in path]
        draw.line(points, fill=STROKE, width=width, joint="curve")
        radius = width / 2
        for x, y in (points[0], points[-1]):
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=STROKE)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def write_ico(path: Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    render_icon(256).save(destination, format="ICO", sizes=ICON_SIZES)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the MarketListener Windows icon")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_ico(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
