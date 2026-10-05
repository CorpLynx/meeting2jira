"""Regenerate asgard/asgard.ico and asgard/asgard.png (maintainers only; needs Pillow).

    py -3 -m pip install pillow
    py -3 tools/make_icon.py [path-to-a-bold-ttf]
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "asgard"
BG, FG = (46, 58, 135, 255), (255, 255, 255, 255)
SIZE = 1024


def find_font() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    for candidate in (r"C:\Windows\Fonts\segoeuib.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if Path(candidate).exists():
            return candidate
    raise SystemExit("Pass the path to a bold .ttf font")


def master() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    inset = SIZE // 16
    d.rounded_rectangle((inset, inset, SIZE - inset, SIZE - inset), radius=SIZE // 5, fill=BG)
    font = ImageFont.truetype(find_font(), int(SIZE * 0.62))
    left, top, right, bottom = d.textbbox((0, 0), "A", font=font)
    d.text(((SIZE - (right - left)) / 2 - left, (SIZE - (bottom - top)) / 2 - top), "A", font=font, fill=FG)
    return img


def main() -> None:
    img = master()
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    img.resize((256, 256), Image.LANCZOS).save(OUT / "asgard.ico", sizes=[(s, s) for s in sizes])
    img.resize((64, 64), Image.LANCZOS).save(OUT / "asgard.png")
    print("wrote", OUT / "asgard.ico", "and", OUT / "asgard.png")


if __name__ == "__main__":
    main()
