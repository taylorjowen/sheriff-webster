import pytest

from sheriff.parsing.text import (ShareParseError, classify_wordle_bot_text, parse_playing_text,
                                  parse_recap_text, parse_share_text)

RECAP = (
    "Your group is on a 12 day streak! \U0001F525 Here are yesterday's results:\n"
    "\U0001F451 3/6: <@111> <@!222>\n"
    "4/6: @Fire Emblem: Scuba Steve <@333>\n"
    "X/6: @Plain Pat"
)
G, Y, K, W, BLUE, ORANGE = "\U0001F7E9", "\U0001F7E8", "⬛", "⬜", "\U0001F7E6", "\U0001F7E7"


def test_recap_text_order_and_tokens():
    r = parse_recap_text(RECAP)
    entries = [(str(t), o) for t, o in r.entries]
    assert entries == [("<@111>", "3"), ("<@222>", "3"), ("@Fire Emblem: Scuba Steve", "4"),
                       ("<@333>", "4"), ("@Plain Pat", "X")]
    assert classify_wordle_bot_text(RECAP) == "recap"


def test_playing_text():
    assert parse_playing_text("Fire Emblem: Fortune's Weeb was playing") == "Fire Emblem: Fortune's Weeb"
    assert classify_wordle_bot_text("Tex was playing") == "playing"
    assert parse_playing_text("hello") is None


def test_share_dark_and_light():
    dark = f"Wordle 1,234 3/6\n\n{K}{Y}{K}{K}{K}\n{G}{G}{K}{Y}{K}\n{G * 5}"
    s = parse_share_text(dark)
    assert (s.day, s.guesses, s.hard_mode) == (1234, 3, False)
    assert s.rows == ["BYBBB", "GGBYB", "GGGGG"]
    assert parse_share_text(dark.replace(K, W)).rows == s.rows


def test_share_high_contrast_hard_mode():
    s = parse_share_text(f"Wordle 1500 2/6*\n{BLUE}{ORANGE}{K}{K}{K}\n{ORANGE * 5}")
    assert s.hard_mode and s.rows == ["YGBBB", "GGGGG"]


def test_share_failed():
    s = parse_share_text("Wordle 900 X/6\n" + "\n".join([K * 5] * 6))
    assert s.guesses is None and len(s.rows) == 6


@pytest.mark.parametrize("bad", [
    f"Wordle 900 3/6\n{K * 5}\n{G * 5}",                      # row count mismatch
    f"Wordle 900 2/6\n{K * 5}\n{G * 4}{K}",                   # doesn't end green
    f"Wordle 900 2/6\n{K * 4}\n{G * 5}",                      # short row
    "Wordle 900 X/6\n" + "\n".join([G * 5] + [K * 5] * 5),    # green row in a failure
])
def test_share_malformed(bad):
    with pytest.raises(ShareParseError):
        parse_share_text(bad)


def test_share_absent():
    assert parse_share_text("just chatting") is None
