"""Calibrate tile colors against a real Wordle-app image and debug the parser.

    python -m sheriff.tools.calibrate data/archive/<msg>_0.png [--overlay out.png]

Prints the dominant colors (so you can set image.colors in config.yaml), what the
parser currently finds (tiles, grids, day number), and optionally writes an overlay
image with every detected tile outlined.
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from ..config import Config
from ..parsing.image import find_tiles, group_grids, load_rgb
from ..parsing.daynum import read_day_number

OUTLINE = {"green": (0, 255, 0), "yellow": (255, 255, 0), "gray": (255, 0, 255), "empty": (0, 255, 255)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image")
    ap.add_argument("--overlay")
    ap.add_argument("--config")
    a = ap.parse_args()
    cfg = Config.load(a.config)
    data = Path(a.image).read_bytes()
    rgb = load_rgb(data)
    print(f"image {rgb.shape[1]}x{rgb.shape[0]}")

    q = (rgb // 4 * 4).reshape(-1, 3)
    counts = Counter(map(tuple, q.tolist()))
    print("\nmost common colors (RGB, pixel count) -- tile colors are flat, so they show up here:")
    for color, n in counts.most_common(14):
        print(f"  {list(color)!s:18s} {n}")

    tiles, size = find_tiles(rgb, cfg)
    print(f"\nconfigured colors: {cfg.get('image.colors')}  tolerance={cfg.get('image.tolerance')}")
    print(f"tiles found: {len(tiles)} (modal size {size:.1f}px); "
          f"unconfident: {sum(1 for t in tiles if not t.confident)}")
    by_cls = Counter(t.cls for t in tiles)
    print("  by class:", dict(by_cls))
    for cls in OUTLINE:
        ts = [t for t in tiles if t.cls == cls]
        if ts:
            print(f"  {cls}: mean distance to reference {np.mean([t.dist for t in ts]):.1f}, "
                  f"min margin {min(t.margin for t in ts):.1f}")
    grids = group_grids(tiles, size)
    print(f"\ngrids found: {len(grids)}")
    for i, g in enumerate(grids):
        print(f"  card {i + 1}: {'|'.join(g.rows) if g.ok else 'ERROR ' + str(g.error)}")
    if tiles:
        day, err = read_day_number(rgb, min(t.y0 for t in tiles), size, cfg)
        print(f"\nday number: {day} {err or ''}")
        if err:
            print("  -> harvest digit templates: python -m sheriff.tools.harvest_glyphs <image> <day>")
    if a.overlay:
        img = Image.fromarray(rgb.astype(np.uint8))
        d = ImageDraw.Draw(img)
        for t in tiles:
            d.rectangle([t.x0 - 1, t.y0 - 1, t.x1, t.y1], outline=OUTLINE[t.cls] if t.confident else (255, 0, 0))
        img.save(a.overlay)
        print(f"overlay written to {a.overlay}")


if __name__ == "__main__":
    main()
