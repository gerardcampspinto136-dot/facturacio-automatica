"""
Generates a simple placeholder logo for testing.
Run with:  py generate_logo.py
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUTPUT = Path("config/logo.png")
W, H = 400, 120
BG = (26, 58, 92)       # same dark blue as invoice brand colour
FG = (255, 255, 255)
ACCENT = (100, 160, 220)


def main() -> None:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Decorative left bar
    draw.rectangle([0, 0, 10, H], fill=ACCENT)

    # Try to load a nice system font; fall back to default
    font_large: ImageFont.ImageFont | ImageFont.FreeTypeFont
    font_small: ImageFont.ImageFont | ImageFont.FreeTypeFont
    try:
        font_large = ImageFont.truetype("arial.ttf", 32)
        font_small = ImageFont.truetype("arial.ttf", 14)
    except OSError:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()

    draw.text((24, 22), "TechSolucions Test S.L.", font=font_large, fill=FG)
    draw.text((26, 70), "Carrer de Provença 42 · 08029 Barcelona", font=font_small, fill=ACCENT)
    draw.text((26, 90), "CIF: B87654321  ·  +34 932 000 001", font=font_small, fill=ACCENT)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUTPUT, "PNG")
    print(f"Logo saved to {OUTPUT}")


if __name__ == "__main__":
    main()
