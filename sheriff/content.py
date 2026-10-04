"""Flavor content (§10)."""
from __future__ import annotations

import random

FIRST_NAMES = ["Jebediah", "Cornelius", "Ezekiel", "Clementine", "Hezekiah", "Prudence", "Thaddeus", "Mabel",
               "Obadiah", "Winifred", "Silas", "Delphine", "Augustus", "Temperance", "Bartholomew", "Henrietta",
               "Amos", "Lavinia", "Ephraim", "Rosalind"]
NICKNAMES = ["Two-Guess", "Greenhorn", "Lucky Streak", "Yellowbelly", "Six-Shooter", "Dead-Eye", "Copperhead",
             "Snake Oil", "Quickdraw", "Five-Letter", "Hornswoggle", "No-Gray", "Sidewinder", "Tin Star",
             "Long Odds", "Card Sharp", "Trap Dodger", "Dusty"]
LAST_NAMES = ["McGraw", "Buckley", "Whitlock", "Pettibone", "Crenshaw", "Hollister", "Blackwood", "Tumbleweed",
              "Fairweather", "Haverford", "Calloway", "Thistlewood", "Redfern", "Cartwright", "Ashby", "Munroe"]

PUNISHMENTS = [
    "Sentenced to muck the stables for a fortnight",
    "Run out of town on a rail",
    "Must play hard mode until the next full moon",
    'Tarred, feathered, and forced to open with "QAJAQ"',
    "Banished to the Dodge City saloon to sweep floors",
    "Sheriff confiscates their horse and their vowels",
    "Must buy a round for the whole saloon",
    "Shall ride backwards on a mule through Main Street",
    "Chained to the hitching post with only consonants for company",
    'Ordered to guess "XYLYL" first thing every morning for a week',
]

CLEAN = "You're clean, partner. For now."
OPT_IN = "Takes a brave soul to look at the Wanted list."
CONFESSION = "Honesty's a rare thing in these parts."


def make_alias(rng: random.Random, taken: set[str] | None = None, avoid: str | None = None) -> str:
    taken = set(taken or ())
    if avoid:
        taken.add(avoid)
    for _ in range(200):
        nick = rng.choice(NICKNAMES)
        last = rng.choice(LAST_NAMES)
        alias = f'{rng.choice(FIRST_NAMES)} "{nick}" {last}' if rng.random() < 0.7 else f"{nick} {last}"
        if alias not in taken:
            return alias
    return f'{rng.choice(FIRST_NAMES)} "{rng.choice(NICKNAMES)}" {rng.choice(LAST_NAMES)} {rng.randint(2, 99)}'


def pick_punishment(rng: random.Random) -> str:
    return rng.choice(PUNISHMENTS)
