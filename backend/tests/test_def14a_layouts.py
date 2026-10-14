"""
Unit tests for backend/scripts/sec/def14a_layouts.py

Run inside the backend container:
    docker compose exec backend pytest backend/tests/test_def14a_layouts.py -v
"""

import pathlib
import sys

# Allow importing the scripts package from tests/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.sec.def14a_layouts import classify_layout

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "def14a"


# ── fixture-based tests ───────────────────────────────────────────────────────

def test_classify_apple_is_table():
    html = (FIXTURES / "apple_def14a.htm").read_text(encoding="utf-8", errors="replace")
    assert classify_layout(html) == "table"


def test_classify_microsoft_is_table():
    html = (FIXTURES / "microsoft_def14a.htm").read_text(encoding="utf-8", errors="replace")
    assert classify_layout(html) == "table"


def test_classify_alphabet_is_mini():
    html = (FIXTURES / "alphabet_def14a.htm").read_text(encoding="utf-8", errors="replace")
    assert classify_layout(html) == "mini"


def test_classify_berkshire_is_prose():
    html = (FIXTURES / "berkshire_def14a.htm").read_text(encoding="utf-8", errors="replace")
    assert classify_layout(html) == "prose"


def test_classify_tesla_is_span():
    html = (FIXTURES / "tesla_def14a.htm").read_text(encoding="utf-8", errors="replace")
    assert classify_layout(html) == "span"


# ── fallback / edge-case tests ────────────────────────────────────────────────

def test_classify_empty_is_unknown():
    assert classify_layout("") == "unknown"


def test_classify_whitespace_is_unknown():
    assert classify_layout("   \n\t  ") == "unknown"


def test_classify_garbage_is_unknown():
    assert classify_layout("<html><body>no rosters here</body></html>") == "unknown"
