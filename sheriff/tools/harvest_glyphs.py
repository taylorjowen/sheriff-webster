"""Harvest digit templates from an image whose day number you know.

    python -m sheriff.tools.harvest_glyphs data/archive/<msg>.png 1924 [--set real]

Run this on one or two real recap images (and a "was playing" image, if its title
font size differs) after `/sheriff backfill`; then `/sheriff reprocess`.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image

from ..config import Config
from ..parsing.daynum import clear_glyph_cache, title_glyphs
from ..parsing.image import find_tiles, load_rgb


def harvest(image_bytes: bytes, day: int, cfg: Config, set_name: str) -> list[Path]:
    rgb = load_rgb(image_bytes)
    tiles, _ = find_tiles(rgb, cfg)
    if not tiles:
        raise SystemExit("no tiles found; calibrate colors first (sheriff.tools.calibrate)")
    glyphs, err = title_glyphs(rgb, min(t.y0 for t in tiles), cfg)
    digits = str(day)
    if err or len(glyphs) != len(digits):
        raise SystemExit(f"found {len(glyphs)} digit glyphs ({err or 'ok'}), expected {len(digits)}")
    out_dir = Path(cfg.get("paths.glyph_dir")) / set_name
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for d, g in zip(digits, glyphs):
        k = len(list(out_dir.glob(f"{d}_*.png")))
        p = out_dir / f"{d}_{k}.png"
        Image.fromarray((g * 255).astype(np.uint8)).save(p)
        written.append(p)
    clear_glyph_cache()
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image")
    ap.add_argument("day", type=int)
    ap.add_argument("--set", default="real")
    ap.add_argument("--config")
    a = ap.parse_args()
    cfg = Config.load(a.config)
    for p in harvest(Path(a.image).read_bytes(), a.day, cfg, a.set):
        print("wrote", p)


if __name__ == "__main__":
    main()
