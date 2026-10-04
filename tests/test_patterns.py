import random

from sheriff.patterns import ALL_GREEN, code_to_row, encode_words, pattern, pattern_matrix, row_to_code


def p(g, a):
    return code_to_row(pattern(g, a))


def test_duplicate_letters():
    assert p("speed", "abide") == "BBYBY"
    assert p("llama", "hello") == "YYBBB"
    assert p("hello", "llama") == "BBYYB"
    assert p("eerie", "there") == "YBYBG"
    assert p("geese", "those") == "BBBGG"
    assert p("abbey", "babes") == "YYGGB"
    assert p("sassy", "assay") == "YYGBG"
    assert p("lolly", "allow") == "YYGBB"
    assert p("crane", "crane") == "GGGGG"
    assert pattern("crane", "crane") == ALL_GREEN


def test_row_code_roundtrip():
    for c in range(243):
        assert row_to_code(code_to_row(c)) == c


def test_matrix_matches_scalar(wd):
    rng = random.Random(1)
    gs = [rng.choice(wd.guesses) for _ in range(300)]
    as_ = [rng.choice(wd.candidates) for _ in range(300)]
    m = pattern_matrix(encode_words(gs), encode_words(as_))
    for i in range(0, 300, 7):
        for j in range(0, 300, 11):
            assert m[i, j] == pattern(gs[i], as_[j])


def test_table_spot_check(wd):
    rng = random.Random(2)
    for _ in range(2000):
        g, a = rng.choice(wd.guesses), rng.choice(wd.candidates)
        assert wd.table[wd.guess_idx[g], wd.cand_idx[a]] == pattern(g, a)


def test_ensure_answer_adds_column(wd):
    idx = wd.ensure_answer("peeve")
    assert wd.candidates[idx] == "peeve"
    assert wd.table[wd.guess_idx["eerie"], idx] == pattern("eerie", "peeve")
    assert wd.table.shape == (len(wd.guesses), len(wd.candidates))
