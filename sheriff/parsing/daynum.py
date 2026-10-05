"""Read the day number from the "Wordle No. N" title by digit template matching (§5.2 step 6).

Templates live in <glyph_dir or user_glyph_dir>/<set>/<digit>_<k>.png and are harvested from real
fixtures with `python -m sheriff.tools.harvest_glyphs <image> <day>`. Tesseract is
used as a fallback only if `pytesseract` is installed.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PIL import Image

from ..config import Config

log = logging.getLogger(__name__)

GLYPH_W, GLYPH_H = 14, 20
_GLYPH_CACHE: dict[str, dict[str, list[np.ndarray]]] = {}


def text_mask(rgb: np.ndarray, cfg: Config) -> np.ndarray:
    luma = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    chroma = rgb.max(-1) - rgb.min(-1)
    return (luma >= cfg.get("image.text_min_luma")) & (chroma <= cfg.get("image.text_max_chroma"))


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    out, start = [], None
    for i, f in enumerate(flags.tolist() + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((start, i))
            start = None
    return out


def title_glyphs(rgb: np.ndarray, title_bottom: int, cfg: Config) -> tuple[list[np.ndarray], str | None]:
    """Segment the last word of the title (the digits) into normalized glyph images."""
    region = rgb[:max(0, title_bottom)]
    if region.shape[0] < 4:
        return [], "no title region"
    mask = text_mask(region, cfg)
    bands = [b for b in _runs(mask.any(axis=1)) if b[1] - b[0] >= 4]
    if not bands:
        return [], "no title text"
    y0, y1 = bands[0]
    band = mask[y0:y1]
    luma = (0.299 * region[y0:y1, :, 0] + 0.587 * region[y0:y1, :, 1] + 0.114 * region[y0:y1, :, 2])
    segs = _runs(band.any(axis=0))
    if not segs:
        return [], "no glyphs"
    boxes = []
    for x0, x1 in segs:
        rows = np.flatnonzero(band[:, x0:x1].any(axis=1))
        boxes.append((x0, x1, int(rows.min()), int(rows.max()) + 1))
    max_h = max(r1 - r0 for _, _, r0, r1 in boxes)
    small = [(r1 - r0) < 0.4 * max_h for _, _, r0, r1 in boxes]
    # The digits follow the period of "No." -- the first small glyph after a few letters.
    start = None
    for i, is_small in enumerate(small):
        if is_small and i >= 3:
            start = i + 1
            break
    if start is None:
        # fallback: last word, split on the widest gap
        gaps = [b[0] - a[1] for a, b in zip(segs, segs[1:])]
        if not gaps:
            return [], "title has a single glyph"
        start = int(np.argmax(gaps)) + 1
    glyphs = []
    for (x0, x1, r0, r1), is_small in zip(boxes[start:], small[start:]):
        if is_small or (r1 - r0) < 0.55 * max_h:   # comma / stray mark
            continue
        crop = np.clip((luma[r0:r1, x0:x1] - 40) / 215.0, 0, 1)
        glyphs.append(normalize_glyph(crop))
    if not glyphs:
        return [], "no digit glyphs"
    return glyphs, None


def normalize_glyph(crop: np.ndarray) -> np.ndarray:
    img = Image.fromarray((crop * 255).astype(np.uint8))
    h, w = crop.shape
    # keep aspect: pad to the template aspect before resizing (so "1" stays thin)
    target_ratio = GLYPH_W / GLYPH_H
    if w / h < target_ratio:
        new_w = int(round(h * target_ratio))
        canvas = Image.new("L", (new_w, h), 0)
        canvas.paste(img, ((new_w - w) // 2, 0))
        img = canvas
    img = img.resize((GLYPH_W, GLYPH_H), Image.BILINEAR)
    return np.asarray(img, dtype=np.float64) / 255.0


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    den = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / den) if den > 0 else 0.0


def load_glyph_sets(glyph_dir: str | Path) -> dict[str, dict[str, list[np.ndarray]]]:
    key = str(glyph_dir)
    if key in _GLYPH_CACHE:
        return _GLYPH_CACHE[key]
    sets: dict[str, dict[str, list[np.ndarray]]] = {}
    root = Path(glyph_dir)
    if root.exists():
        for set_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            digits: dict[str, list[np.ndarray]] = {}
            for f in sorted(set_dir.glob("*.png")):
                d = f.stem.split("_")[0]
                arr = np.asarray(Image.open(f).convert("L").resize((GLYPH_W, GLYPH_H)), dtype=np.float64) / 255.0
                digits.setdefault(d, []).append(arr)
            if digits:
                sets[set_dir.name] = digits
    _GLYPH_CACHE[key] = sets
    return sets


def clear_glyph_cache() -> None:
    _GLYPH_CACHE.clear()


def match_glyphs(glyphs: list[np.ndarray], sets: dict, threshold: float) -> tuple[str | None, float]:
    """Best reading across glyph sets; returns (digits, min score)."""
    best: tuple[str | None, float] = (None, -1.0)
    for digits in sets.values():
        out, worst = [], 1.0
        for g in glyphs:
            score, label = max(((max(_ncc(g, t) for t in ts), d) for d, ts in digits.items()), default=(-1.0, "?"))
            out.append(label)
            worst = min(worst, score)
        if worst > best[1]:
            best = ("".join(out), worst)
    if best[0] is None or best[1] < threshold:
        return None, best[1]
    return best


def _tesseract(rgb: np.ndarray, title_bottom: int) -> str | None:
    try:
        import pytesseract  # type: ignore
    except ImportError:
        return None
    try:
        img = Image.fromarray(rgb[:title_bottom].astype(np.uint8))
        txt = pytesseract.image_to_string(img, config="--psm 7")
        import re
        m = re.search(r"No\.?\s*([\d,]+)", txt)
        return m.group(1).replace(",", "") if m else None
    except Exception as e:
        log.warning("tesseract failed: %s", e)
        return None


def read_day_number(rgb: np.ndarray, title_bottom: int, tile_size: float, cfg: Config,
                    expected_day: int | None = None, glyph_sets: dict | None = None) -> tuple[int | None, str | None]:
    glyphs, err = title_glyphs(rgb, title_bottom, cfg)
    reading = None
    if not err and glyphs:
        if glyph_sets is not None:
            sets = glyph_sets
        else:   # built-in templates plus ones harvested from real images (kept under data/)
            sets = {**load_glyph_sets(cfg.path("glyph_dir")), **load_glyph_sets(cfg.path("user_glyph_dir"))}
        if sets:
            reading, score = match_glyphs(glyphs, sets, float(cfg.get("image.digit_match_threshold")))
            if reading is None:
                err = f"digit match too weak ({score:.2f})"
        else:
            err = "no digit templates (run sheriff.tools.harvest_glyphs)"
    if reading is None:
        t = _tesseract(rgb, title_bottom)
        if t and t.isdigit():
            reading, err = t, None
    if reading is None:
        return None, err or "unreadable"
    day = int(reading)
    if expected_day is not None and abs(day - expected_day) > 1:
        return None, f"read {day}, but message date implies {expected_day}"
    return day, None
