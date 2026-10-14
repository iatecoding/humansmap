"""
Pytest tests for the image download/optimization service.

Requires: network access to Wikimedia Commons + Pillow.

Run inside the Docker container:
    docker compose exec backend pytest tests/test_image_cache.py
"""

import os
import tempfile

import pytest
import app.config as cfg

# Use a temp dir so tests don't pollute /app/images
_TEST_CACHE_DIR = tempfile.mkdtemp(prefix="imgcache_test_")
cfg.settings.image_cache_dir = _TEST_CACHE_DIR

from app.services.image_cache import (
    _encode_jpeg_with_limit,
    _is_license_allowed,
    _plain_text,
    commons_filename,
    download_and_optimize_image,
)

# Known stable Wikimedia image for Ettore Scola (Italian director)
TEST_IMAGE_URL = "http://commons.wikimedia.org/wiki/Special:FilePath/Ettore%20Scola.jpg"
TEST_QID = "Q53037_pytest"


# ── License filter ────────────────────────────────────────────────────────────

def test_license_cc_by_sa_allowed():
    assert _is_license_allowed("CC BY-SA 4.0")


def test_license_cc0_allowed():
    assert _is_license_allowed("CC0")


def test_license_unknown_blocked():
    assert not _is_license_allowed(None)


def test_license_all_rights_reserved_blocked():
    assert not _is_license_allowed("All rights reserved")


def test_license_getty_blocked():
    assert not _is_license_allowed("Getty Images")


# ── Download + optimization ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
async def downloaded_image():
    result = await download_and_optimize_image(TEST_QID, TEST_IMAGE_URL, "CC BY-SA 3.0")
    assert result is not None, "Download returned None — URL may be stale"
    yield result
    if os.path.exists(result):
        os.remove(result)
    if os.path.isdir(_TEST_CACHE_DIR):
        try:
            os.rmdir(_TEST_CACHE_DIR)
        except OSError:
            pass  # not empty — that's fine


def test_download_file_exists(downloaded_image):
    assert os.path.exists(downloaded_image)


def test_download_file_not_empty(downloaded_image):
    assert os.path.getsize(downloaded_image) > 0


def test_download_is_valid_jpeg(downloaded_image):
    from PIL import Image
    with Image.open(downloaded_image) as img:
        assert img.format == "JPEG"
        assert img.mode == "RGB"


def test_download_respects_max_dimension(downloaded_image):
    from PIL import Image
    with Image.open(downloaded_image) as img:
        assert max(img.size) <= cfg.settings.image_max_dimension_px + 1  # +1 for rounding


async def test_cache_hit_returns_same_path(downloaded_image):
    result2 = await download_and_optimize_image(TEST_QID, TEST_IMAGE_URL, "CC BY-SA 3.0")
    assert result2 == downloaded_image


def test_commons_filename_is_decoded():
    assert commons_filename(TEST_IMAGE_URL) == "Ettore Scola.jpg"


def test_commons_html_credit_is_plain_text():
    assert _plain_text('<a href="/wiki/User:Jane">Jane Doe</a> &amp; Archive') == "Jane Doe & Archive"


def test_jpeg_encoder_respects_byte_ceiling():
    from PIL import Image

    image = Image.effect_noise((300, 300), 100).convert("RGB")
    encoded = _encode_jpeg_with_limit(image, 50_000)
    assert len(encoded) <= 50_000
