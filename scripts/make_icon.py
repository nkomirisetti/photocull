"""Draw PhotoCull's app icon and write a macOS .icns.

    python scripts/make_icon.py OUT.icns
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw


def draw(size: int) -> Image.Image:
    s = size / 1024
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # rounded-square tile, the macOS icon grid's 824px body
    d.rounded_rectangle([100 * s, 100 * s, 924 * s, 924 * s], radius=185 * s, fill=(24, 24, 24, 255))
    # two photos fanned out: archived (red) behind, kept (green) in front
    back = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(back).rounded_rectangle([250 * s, 300 * s, 690 * s, 640 * s], radius=28 * s,
                                           fill=(60, 60, 60, 255), outline=(231, 76, 60, 255), width=round(22 * s))
    im.alpha_composite(back.rotate(10, center=(470 * s, 470 * s), resample=Image.BICUBIC))
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = 330 * s, 400 * s, 790 * s, 750 * s
    d.rounded_rectangle([x0, y0, x1, y1], radius=28 * s, fill=(242, 242, 242, 255),
                        outline=(46, 204, 113, 255), width=round(24 * s))
    # a little landscape: sky, sun, mountains
    d.rectangle([x0 + 30 * s, y0 + 30 * s, x1 - 30 * s, y1 - 30 * s], fill=(120, 180, 230, 255))
    d.ellipse([650 * s, 455 * s, 710 * s, 515 * s], fill=(255, 215, 94, 255))
    d.polygon([(x0 + 30 * s, y1 - 30 * s), (470 * s, 530 * s), (590 * s, y1 - 30 * s)], fill=(70, 110, 80, 255))
    d.polygon([(500 * s, y1 - 30 * s), (640 * s, 580 * s), (x1 - 30 * s, y1 - 30 * s)], fill=(50, 90, 65, 255))
    # green check badge
    cx, cy, r = 760 * s, 740 * s, 95 * s
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(46, 204, 113, 255), outline=(24, 24, 24, 255), width=round(14 * s))
    d.line([(cx - 45 * s, cy), (cx - 12 * s, cy + 35 * s), (cx + 48 * s, cy - 35 * s)],
           fill=(255, 255, 255, 255), width=round(26 * s), joint="curve")
    return im


def main(out: str):
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "PhotoCull.iconset"
        iconset.mkdir()
        for px in (16, 32, 128, 256, 512):
            draw(px).save(iconset / f"icon_{px}x{px}.png")
            draw(px * 2).save(iconset / f"icon_{px}x{px}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", out], check=True)


if __name__ == "__main__":
    main(sys.argv[1])
