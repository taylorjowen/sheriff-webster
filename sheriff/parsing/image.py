"""Layout-agnostic grid parser for Wordle-app images (recap and "was playing" layouts), §5.2.

Pipeline: classify pixels by nearest reference color -> connected components ->
keep roughly-square components of the modal tile size -> cluster into card bands,
columns and rows -> read each 6x5 grid. All thresholds scale with the detected tile size.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from ..config import Config
from .daynum import read_day_number

CLASS_NAMES = ["green", "yellow", "gray", "empty"]
CLASS_LETTER = {"green": "G", "yellow": "Y", "gray": "B", "empty": None}


@dataclass
class Tile:
    x0: int
    y0: int
    x1: int
    y1: int
    cls: str
    dist: float
    margin: float
    confident: bool

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def size(self) -> float:
        return (self.x1 - self.x0 + self.y1 - self.y0) / 2


@dataclass
class ParsedGrid:
    rows: list[str] = field(default_factory=list)   # filled rows only, top to bottom
    error: str | None = None
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def grid(self) -> str:
        return "|".join(self.rows)


@dataclass
class ParsedImage:
    grids: list[ParsedGrid]
    day: int | None
    day_error: str | None
    tile_size: float
    error: str | None = None
    tiles: list[Tile] = field(default_factory=list)


def load_rgb(data: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return np.asarray(img, dtype=np.int16)


def _components(label: np.ndarray, rgb: np.ndarray) -> list[dict]:
    """Connected components (4-connectivity on runs) of equal non-negative labels."""
    H, W = label.shape
    parent: list[int] = []

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    runs: list[tuple[int, int, int, int]] = []   # (y, x0, x1_exclusive, label)
    prev: list[int] = []                          # run indices in previous row
    csum = np.cumsum(rgb, axis=1, dtype=np.int64)
    run_color: list[np.ndarray] = []
    for y in range(H):
        row = label[y]
        change = np.flatnonzero(np.diff(row)) + 1
        starts = np.concatenate(([0], change))
        ends = np.concatenate((change, [W]))
        cur: list[int] = []
        j = 0
        for s, e in zip(starts.tolist(), ends.tolist()):
            lab = int(row[s])
            if lab < 0:
                continue
            idx = len(runs)
            runs.append((y, s, e, lab))
            parent.append(idx)
            c = csum[y, e - 1] - (csum[y, s - 1] if s > 0 else 0)
            run_color.append(c)
            # union with overlapping same-label runs in previous row
            while j < len(prev) and runs[prev[j]][2] <= s:
                j += 1
            k = j
            while k < len(prev) and runs[prev[k]][1] < e:
                if runs[prev[k]][3] == lab:
                    a, b = find(idx), find(prev[k])
                    if a != b:
                        parent[a] = b
                k += 1
            cur.append(idx)
        prev = cur
    comps: dict[int, dict] = {}
    for i, (y, s, e, lab) in enumerate(runs):
        r = find(i)
        c = comps.get(r)
        if c is None:
            comps[r] = {"x0": s, "x1": e, "y0": y, "y1": y + 1, "n": e - s, "lab": lab, "rgb": run_color[i].copy()}
        else:
            c["x0"] = min(c["x0"], s)
            c["x1"] = max(c["x1"], e)
            c["y0"] = min(c["y0"], y)
            c["y1"] = max(c["y1"], y + 1)
            c["n"] += e - s
            c["rgb"] += run_color[i]
    return list(comps.values())


def find_tiles(rgb: np.ndarray, cfg: Config) -> tuple[list[Tile], float]:
    refs = np.array([cfg.get(f"image.colors.{n}") for n in CLASS_NAMES], dtype=np.float64)
    tol = float(cfg.get("image.tolerance"))
    min_margin = float(cfg.get("image.min_margin"))
    min_px = int(cfg.get("image.min_tile_px"))

    d = np.sqrt(((rgb[:, :, None, :].astype(np.float64) - refs[None, None]) ** 2).sum(-1))
    label = d.argmin(-1).astype(np.int16)
    label[d.min(-1) > tol] = -1
    comps = _components(label, rgb)

    cands = []
    for c in comps:
        w, h = c["x1"] - c["x0"], c["y1"] - c["y0"]
        if w < min_px or h < min_px:
            continue
        if abs(w - h) > max(2, 0.2 * max(w, h)):
            continue
        if c["n"] / (w * h) < 0.75:
            continue
        cands.append(c)
    if not cands:
        return [], 0.0
    sizes = np.array([(c["x1"] - c["x0"] + c["y1"] - c["y0"]) / 2 for c in cands])
    # modal size: the size with the most components within +-12%
    best, best_n = sizes[0], 0
    for s in np.unique(np.round(sizes)):
        n = int(((sizes >= s * 0.88) & (sizes <= s * 1.12)).sum())
        if n > best_n:
            best, best_n = s, n
    tiles: list[Tile] = []
    for c, s in zip(cands, sizes):
        if not (best * 0.8 <= s <= best * 1.2):
            continue
        mean = c["rgb"] / c["n"]
        dist = np.sqrt(((refs - mean) ** 2).sum(-1))
        order = np.argsort(dist)
        cls = CLASS_NAMES[int(order[0])]
        margin = float(dist[order[1]] - dist[order[0]])
        confident = bool(dist[order[0]] <= tol and margin >= min_margin)
        tiles.append(Tile(c["x0"], c["y0"], c["x1"], c["y1"], cls, float(dist[order[0]]), margin, confident))
    size = float(np.median([t.size for t in tiles])) if tiles else 0.0
    return tiles, size


def _cluster_1d(values: list[float], gap: float) -> list[list[int]]:
    """Group indices of `values` whose sorted neighbours are within `gap`."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    groups: list[list[int]] = []
    last = None
    for i in order:
        if last is None or values[i] - last > gap:
            groups.append([i])
        else:
            groups[-1].append(i)
        last = values[i]
    return groups


def _pitch(centers: list[float], size: float) -> float:
    gaps = [b - a for a, b in zip(centers, centers[1:]) if size * 0.9 <= b - a <= size * 1.7]
    return float(np.median(gaps)) if gaps else size * 1.15


def group_grids(tiles: list[Tile], size: float) -> list[ParsedGrid]:
    if not tiles:
        return []
    # 1) rows of tiles (by y), then vertical card bands separated by large gaps
    row_groups = _cluster_1d([t.cy for t in tiles], size * 0.5)
    row_ys = [float(np.mean([tiles[i].cy for i in g])) for g in row_groups]
    order = sorted(range(len(row_groups)), key=lambda k: row_ys[k])
    pitch_y = _pitch([row_ys[k] for k in order], size)
    bands: list[list[int]] = []
    last_y = None
    for k in order:
        if last_y is None or row_ys[k] - last_y > pitch_y * 1.6:
            bands.append([])
        bands[-1].extend(row_groups[k])
        last_y = row_ys[k]

    grids: list[ParsedGrid] = []
    for band in bands:
        btiles = [tiles[i] for i in band]
        col_groups = _cluster_1d([t.cx for t in btiles], size * 0.5)
        col_xs = [float(np.mean([btiles[i].cx for i in g])) for g in col_groups]
        corder = sorted(range(len(col_groups)), key=lambda k: col_xs[k])
        pitch_x = _pitch([col_xs[k] for k in corder], size)
        cards: list[list[int]] = []
        last_x = None
        for k in corder:
            if last_x is None or col_xs[k] - last_x > pitch_x * 1.6:
                cards.append([])
            cards[-1].append(k)
            last_x = col_xs[k]
        for card_cols in cards:
            grids.append(_read_grid([[btiles[i] for i in col_groups[k]] for k in card_cols], pitch_y))
    return grids


def _read_grid(columns: list[list[Tile]], pitch_y: float) -> ParsedGrid:
    all_tiles = [t for col in columns for t in col]
    bbox = (min(t.x0 for t in all_tiles), min(t.y0 for t in all_tiles),
            max(t.x1 for t in all_tiles), max(t.y1 for t in all_tiles))
    g = ParsedGrid(bbox=bbox)
    if len(columns) != 5:
        g.error = f"grid has {len(columns)} columns"
        return g
    top = min(t.cy for t in all_tiles)
    cells: list[list[str | None]] = [[None] * 5 for _ in range(6)]
    seen = [[False] * 5 for _ in range(6)]
    for c, col in enumerate(columns):
        for t in col:
            r = round((t.cy - top) / pitch_y)
            if not 0 <= r < 6:
                g.error = f"tile outside 6 rows (row {r})"
                return g
            if seen[r][c]:
                g.error = "two tiles in one cell"
                return g
            if not t.confident:
                g.error = f"unconfident tile at row {r + 1} col {c + 1} ({t.cls}, dist {t.dist:.0f})"
                return g
            seen[r][c] = True
            cells[r][c] = CLASS_LETTER[t.cls] or "."
    rows: list[str] = []
    finished = False
    for r in range(6):
        filled = [x for x in cells[r] if x in ("G", "Y", "B")]
        if len(filled) == 5 and not finished:
            rows.append("".join(cells[r]))  # type: ignore[arg-type]
        elif len(filled) == 0:
            finished = True
        else:
            g.error = f"row {r + 1} is partially filled or follows an empty row"
            return g
    if not rows:
        g.error = "no filled rows"
        return g
    g.rows = rows
    return g


def parse_image(data: bytes, cfg: Config, expected_day: int | None = None,
                glyph_sets: dict | None = None) -> ParsedImage:
    rgb = load_rgb(data)
    tiles, size = find_tiles(rgb, cfg)
    if not tiles:
        return ParsedImage([], None, "no tiles", 0.0, error="no tiles found")
    grids = group_grids(tiles, size)
    title_bottom = min(t.y0 for t in tiles)
    day, day_err = read_day_number(rgb, title_bottom, size, cfg, expected_day, glyph_sets)
    return ParsedImage(grids, day, day_err, size, tiles=tiles)
