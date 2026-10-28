"""Generate the application .ico (no binary assets are kept in source control)."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def draw(size: int) -> Image.Image:
    s = size / 64
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([10 * s, 4 * s, 50 * s, 58 * s], radius=4 * s, fill="white", outline="#37474f",
                        width=max(1, int(3 * s)))
    d.rounded_rectangle([4 * s, 30 * s, 38 * s, 48 * s], radius=3 * s, fill="#c62828")
    try:
        f = ImageFont.truetype("segoeuib.ttf", max(6, int(10 * s)))
    except OSError:
        f = ImageFont.load_default()
    d.text((21 * s, 39 * s), "PDF", fill="white", font=f, anchor="mm")
    d.arc([34 * s, 30 * s, 60 * s, 56 * s], start=-30, end=250, fill="#1565c0", width=max(1, int(5 * s)))
    return im


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/icon.ico")
    out.parent.mkdir(parents=True, exist_ok=True)
    draw(256).save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(out)
