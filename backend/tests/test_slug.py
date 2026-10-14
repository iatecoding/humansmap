"""Unit tests for backend/app/utils/slug.py.

These are pure-function tests — no database or network required.
"""

import pytest

from app.utils.slug import slugify


# ── Basic cases ───────────────────────────────────────────────────────────────

def test_empty_string():
    assert slugify("") == ""


def test_simple_ascii():
    assert slugify("mario draghi") == "mario-draghi"


# ── Accent folding ────────────────────────────────────────────────────────────

def test_accents_canon():
    assert slugify("Cañón") == "canon"


def test_accents_various():
    assert slugify("André Citroën") == "andre-citroen"


def test_accents_italian():
    assert slugify("Università") == "universita"


# ── Punctuation ───────────────────────────────────────────────────────────────

def test_punctuation_dots():
    assert slugify("J.S. Bach") == "j-s-bach"


def test_punctuation_apostrophe():
    assert slugify("D'Annunzio") == "d-annunzio"


def test_punctuation_multiple_separators():
    # Consecutive non-alnum chars collapse to one hyphen
    assert slugify("foo  --  bar") == "foo-bar"


# ── Emoji / non-ASCII ─────────────────────────────────────────────────────────

def test_emoji():
    assert slugify("hello 😀 world") == "hello-world"


def test_all_non_ascii():
    assert slugify("日本語") == ""


# ── Length cap ────────────────────────────────────────────────────────────────

def test_long_name_truncated():
    long_name = "a" * 70
    result = slugify(long_name)
    assert len(result) <= 60


def test_long_name_hyphen_boundary():
    # Build a name whose 60-char prefix ends mid-word — should break at last hyphen
    name = "word-" * 20   # "word-word-word-..." = 100 chars
    result = slugify(name)
    assert len(result) <= 60
    assert not result.endswith("-")


# ── Casing ────────────────────────────────────────────────────────────────────

def test_uppercase_lowercased():
    assert slugify("MARIO DRAGHI") == "mario-draghi"


# ── Real names from the DB (spot-check) ──────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Johann Sebastian Bach", "johann-sebastian-bach"),
    ("Mario Draghi", "mario-draghi"),
    ("Silvio Berlusconi", "silvio-berlusconi"),
    ("Angela Merkel", "angela-merkel"),
    ("François Mitterrand", "francois-mitterrand"),
    ("Héctor", "hector"),
    ("Óscar Wilde", "oscar-wilde"),
    ("São Paulo", "sao-paulo"),
    ("José Mourinho", "jose-mourinho"),
    ("Björk", "bjork"),
])
def test_real_names(name, expected):
    assert slugify(name) == expected
