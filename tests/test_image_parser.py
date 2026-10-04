import io
import random

import numpy as np
import pytest
from PIL import Image, ImageFilter

from sheriff.days import day_to_datetime
from sheriff.parsing import parse_playing, parse_recap
from sheriff.parsing.image import parse_image
from sheriff.seams import RecapEnvelope
from sheriff.sim.renderer import (CardSpec, order_cards, playing_text, recap_text, render_playing_image,
                                  render_recap_image)


def random_cards(rng, n):
    cards = []
    for i in range(n):
        k = rng.randint(1, 6)
        rows = ["".join(rng.choice("GYB") for _ in range(5)) for _ in range(k)]
        rows = [r if r != "GGGGG" else "GGGGB" for r in rows]
        if rng.random() < 0.85:
            rows[-1] = "GGGGG"
            outcome = str(k)
        else:
            rows = (rows + ["BBBBB"] * 6)[:6]
            outcome = "X"
        cards.append(CardSpec(f"P{i}", str(i + 1) if rng.random() < 0.6 else None, outcome, rows, avatar=i))
    return cards


def recap_env(text, day, images):
    return RecapEnvelope("recap", "m", "c", "a", text, day_to_datetime(day + 1), images=images)


@pytest.mark.parametrize("n", [1, 3, 8, 11])
def test_recap_roundtrip(cfg, n):
    rng = random.Random(n)
    for day in (1924, 2001):
        cards = random_cards(rng, n)
        parsed = parse_recap(recap_env(recap_text(cards), day, [render_recap_image(day, cards, cfg)]), cfg)
        assert parsed.error is None, parsed.error
        assert parsed.day == day
        assert [c.rows for c in parsed.cards] == [c.rows for c in order_cards(cards)]
        assert all(c.error is None for c in parsed.cards)


def test_playing_roundtrip_and_partial(cfg):
    rows = ["BYBBG", "GGBYG", "YGYBB", "GGGGG"]
    for k in range(1, 5):
        partial = CardSpec("Tex", None, "4", rows[:k])
        env = RecapEnvelope("playing", "m", "c", "a", playing_text("Tex"), day_to_datetime(1925),
                            images=[render_playing_image(1925, partial, cfg)])
        p = parse_playing(env, cfg)
        assert p.error is None and p.day == 1925 and p.rows == rows[:k]
        assert p.finished == (k == 4)


def _perturb(png: bytes, kind: str) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if kind == "up":
        img = img.resize((int(img.width * 1.5), int(img.height * 1.5)), Image.NEAREST)
    elif kind == "down":
        img = img.resize((int(img.width * 0.8), int(img.height * 0.8)), Image.BILINEAR)
    elif kind == "jpeg":
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        img = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    elif kind == "noise":
        arr = np.asarray(img).astype(np.int16)
        arr = np.clip(arr + np.random.default_rng(0).integers(-6, 7, arr.shape), 0, 255).astype(np.uint8)
        img = Image.fromarray(arr)
    elif kind == "blur":
        img = img.filter(ImageFilter.GaussianBlur(1.2))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize("kind", ["up", "down", "jpeg", "noise", "blur"])
def test_perturbations_never_silently_wrong(cfg, kind):
    """Perturbed images must parse correctly or fail validation -- never produce wrong data."""
    rng = random.Random(kind)
    for _ in range(4):
        cards = random_cards(rng, 4)
        png = _perturb(render_recap_image(1950, cards, cfg), kind)
        parsed = parse_recap(recap_env(recap_text(cards), 1950, [png]), cfg)
        if parsed.error:
            continue
        assert parsed.day == 1950
        for got, want in zip(parsed.cards, order_cards(cards)):
            assert got.rows is None or got.rows == want.rows, (kind, got, want)


def test_extra_and_missing_cards_fail_validation(cfg):
    cards = random_cards(random.Random(9), 4)
    env = recap_env(recap_text(cards), 1950, [render_recap_image(1950, cards[:3], cfg)])
    assert "grids" in parse_recap(env, cfg).error
    env = recap_env(recap_text(cards[:3]), 1950, [render_recap_image(1950, cards, cfg)])
    assert parse_recap(env, cfg).error


def test_outcome_mismatch_fails_only_that_card(cfg):
    cards = [CardSpec("A", None, "3", ["BYBBG", "GGBYG", "GGGGG"])]
    text = recap_text([CardSpec("A", None, "4", [])])
    parsed = parse_recap(recap_env(text, 1950, [render_recap_image(1950, cards, cfg)]), cfg)
    assert parsed.error is None and parsed.cards[0].rows is None and parsed.cards[0].error


def test_day_number_out_of_range_rejected(cfg):
    png = render_recap_image(1950, [CardSpec("A", None, "2", ["BYBBG", "GGGGG"])], cfg)
    assert parse_image(png, cfg, expected_day=1951).day == 1950
    p = parse_image(png, cfg, expected_day=1960)
    assert p.day is None and "1960" in p.day_error


def test_synthetic_grids_refused_in_live(cfg):
    env = RecapEnvelope("recap", "m", "c", "a", "3/6: @A", day_to_datetime(1951),
                        synthetic_grids=[["BBBBB", "BBBBB", "GGGGG"]], synthetic_day=1950)
    assert parse_recap(env, cfg, allow_synthetic=False).error
    assert parse_recap(env, cfg, allow_synthetic=True).cards[0].rows
