"""RecapRenderer: fake Wordle-app messages that mimic the real layouts (§9.2).

Colors come from config so the parser must round-trip renders perfectly.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from ..config import Config

BG = (18, 18, 19)
TITLE = (235, 235, 235)
AVATAR_COLORS = [(120, 80, 200), (60, 130, 200), (220, 120, 60), (200, 70, 110),
                 (40, 160, 160), (150, 150, 40), (100, 100, 220), (180, 90, 180)]
LETTER_COLOR = {"G": "green", "Y": "yellow", "B": "gray"}


@dataclass
class CardSpec:
    name: str              # display name (plain-text "@Name" form)
    mention: str | None    # placeholder mention id ("<@id>" form) or None for plain text
    outcome: str           # "1".."6" | "X"
    rows: list[str]
    avatar: int = 0


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _colors(cfg: Config) -> dict[str, tuple[int, int, int]]:
    return {k: tuple(cfg.get(f"image.colors.{k}")) for k in ("green", "yellow", "gray", "empty")}


def _draw_grid(d: ImageDraw.ImageDraw, x: int, y: int, rows: list[str], tile: int, gap: int, colors: dict) -> None:
    for r in range(6):
        for c in range(5):
            if r < len(rows):
                col = colors[LETTER_COLOR[rows[r][c]]]
            else:
                col = colors["empty"]
            x0 = x + c * (tile + gap)
            y0 = y + r * (tile + gap)
            d.rectangle([x0, y0, x0 + tile - 1, y0 + tile - 1], fill=col)


def _draw_avatar(d: ImageDraw.ImageDraw, x: int, y: int, size: int, idx: int, name: str) -> None:
    d.ellipse([x, y, x + size - 1, y + size - 1], fill=AVATAR_COLORS[idx % len(AVATAR_COLORS)])
    initial = (name.strip() or "?")[0].upper()
    f = _font(int(size * 0.5))
    d.text((x + size / 2, y + size / 2), initial, fill=(250, 250, 250), font=f, anchor="mm")


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def outcome_sort_key(outcome: str) -> int:
    return 7 if outcome == "X" else int(outcome)


def order_cards(cards: list[CardSpec]) -> list[CardSpec]:
    """Text/card order: grouped by outcome (best first, X last), stable within ties."""
    return sorted(cards, key=lambda c: outcome_sort_key(c.outcome))


def recap_text(cards: list[CardSpec], streak: int = 1) -> str:
    cards = order_cards(cards)
    lines = [f"Your group is on a {streak} day streak! \U0001F525 Here are yesterday's results:"]
    best = outcome_sort_key(cards[0].outcome) if cards else None
    groups: dict[str, list[CardSpec]] = {}
    for c in cards:
        groups.setdefault(c.outcome, []).append(c)
    for outcome, group in groups.items():
        names = " ".join(f"<@{c.mention}>" if c.mention else f"@{c.name}" for c in group)
        crown = "\U0001F451 " if outcome_sort_key(outcome) == best and outcome != "X" else ""
        lines.append(f"{crown}{outcome}/6: {names}")
    return "\n".join(lines)


def render_recap_image(day: int, cards: list[CardSpec], cfg: Config, tile: int = 20, gap: int = 4,
                       per_row: int = 8) -> bytes:
    cards = order_cards(cards)
    colors = _colors(cfg)
    grid_w = 5 * tile + 4 * gap
    grid_h = 6 * tile + 5 * gap
    avatar = int(tile * 1.8)
    card_w = grid_w + 2 * int(tile * 0.6)
    card_h = avatar + int(tile * 0.5) + grid_h
    spacing = int(tile * 0.8)
    title_h = int(tile * 2.6)
    n_rows = max(1, (len(cards) + per_row - 1) // per_row)
    n_cols = min(per_row, max(1, len(cards)))
    W = spacing + n_cols * (card_w + spacing)
    H = title_h + n_rows * (card_h + spacing * 2) + spacing
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((W / 2, title_h / 2), f"Wordle No. {day}", fill=TITLE, font=_font(int(tile * 1.1)), anchor="mm")
    for i, c in enumerate(cards):
        r, k = divmod(i, per_row)
        cx = spacing + k * (card_w + spacing)
        cy = title_h + r * (card_h + spacing * 2)
        _draw_avatar(d, cx + (card_w - avatar) // 2, cy, avatar, c.avatar, c.name)
        _draw_grid(d, cx + (card_w - grid_w) // 2, cy + avatar + int(tile * 0.5), c.rows, tile, gap, colors)
    return _png(img)


def render_playing_image(day: int, card: CardSpec, cfg: Config, tile: int = 40, gap: int = 6) -> bytes:
    colors = _colors(cfg)
    grid_w = 5 * tile + 4 * gap
    grid_h = 6 * tile + 5 * gap
    avatar = int(tile * 2.2)
    pad = tile // 2
    title_h = int(tile * 1.6)
    W = pad * 3 + avatar + grid_w
    H = title_h + grid_h + pad * 2
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((W / 2, title_h / 2 + pad / 2), f"Wordle No. {day}", fill=TITLE, font=_font(int(tile * 0.6)), anchor="mm")
    _draw_avatar(d, pad, title_h + pad + (grid_h - avatar) // 2, avatar, card.avatar, card.name)
    _draw_grid(d, pad * 2 + avatar, title_h + pad, card.rows, tile, gap, colors)
    return _png(img)


def playing_text(name: str) -> str:
    return f"{name} was playing"
