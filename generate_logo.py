"""
Generates a placeholder logo from the company details in config/company.yaml.

Run with:  py generate_logo.py

It reads the configured company rather than hard-coding one, so preparing the bot for a
new client gives a logo that matches their details instead of the previous client's.
Replace config/logo.png with the real artwork whenever there is one.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.config_loader import get_config

OUTPUT = Path("config/logo.png")
W, H = 400, 120
BG = (26, 58, 92)       # same dark blue as invoice brand colour
FG = (255, 255, 255)
ACCENT = (100, 160, 220)
MUTED = (150, 150, 150)


def _font(size: int, bold: bool = False):
    for name in (("arialbd.ttf",) if bold else ("arial.ttf",)):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _fit(draw, text: str, size: int, bold: bool, max_width: int):
    """Shrink the name until it fits: company names vary wildly in length."""
    while size > 12:
        font = _font(size, bold)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 2
    return _font(12, bold)


def main() -> None:
    config = get_config()

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Decorative left bar
    draw.rectangle([0, 0, 10, H], fill=ACCENT)

    name = config.name or "Sin configurar"
    draw.text((24, 22), name, font=_fit(draw, name, 32, True, W - 48), fill=FG)

    small = _font(14)
    if config.address:
        draw.text((26, 70), config.address[:60], font=small, fill=ACCENT)
    details = " · ".join(p for p in (config.cif and f"CIF: {config.cif}",
                                     config.phone) if p)
    if details:
        draw.text((26, 90), details, font=small, fill=ACCENT)

    if config.is_placeholder:
        draw.text((W - 76, 6), "PRUEBA", font=_font(11, True), fill=MUTED)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUTPUT, "PNG")
    print(f"Logo saved to {OUTPUT}  ({name})")


if __name__ == "__main__":
    main()
