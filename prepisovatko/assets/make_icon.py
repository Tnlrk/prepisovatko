"""Generátor ikony aplikace (řečová bublina + zvuková vlna).

Vytvoří assets/icon.ico s více velikostmi. Spustit: python make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icon.ico"
BLUE = (47, 125, 224, 255)  # ladí s modrým tématem GUI


def render(size: int) -> Image.Image:
    s = 1024
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # bublina: zaoblený obdélník + ocásek dole vlevo
    d.rounded_rectangle([110, 150, 914, 730], radius=170, fill=BLUE)
    d.polygon([(300, 700), (300, 880), (470, 700)], fill=BLUE)
    # zvuková vlna: 7 zaoblených sloupců (audio waveform), bílé
    heights = [210, 400, 250, 540, 440, 300, 180]
    bw, gap = 70, 42
    total = len(heights) * bw + (len(heights) - 1) * gap
    x, cy = 512 - total // 2, 430
    for h in heights:
        d.rounded_rectangle([x, cy - h // 2, x + bw, cy + h // 2],
                            radius=bw // 2, fill="white")
        x += bw + gap
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    render(256).save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                 (64, 64), (128, 128), (256, 256)])
    # PNG 1024 px — výchozí bod pro macOS .icns (iconutil) i jiné použití
    png = OUT.with_name("icon.png")
    render(1024).save(png)
    print(f"OK → {OUT}\nOK → {png}")
