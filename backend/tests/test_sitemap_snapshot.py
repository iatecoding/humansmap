import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services import sitemap_snapshot


def test_build_snapshot_writes_index_last_and_shards(monkeypatch, tmp_path: Path):
    rows = [
        ("Q1", "2026-09-10", "Ada Lovelace", None, 301),
        ("Q2", None, None, "Galileo Galilei", 50),
    ]
    monkeypatch.setattr(sitemap_snapshot, "_fetch_rows", lambda: (rows, 3))
    monkeypatch.setattr(sitemap_snapshot, "_fetch_organization_rows", lambda: ([], 0))
    monkeypatch.setattr(sitemap_snapshot, "MAX_URLS_PER_SITEMAP", 1)

    manifest = sitemap_snapshot.build_sitemap_snapshot(tmp_path)

    assert manifest["included"] == 2
    assert manifest["excluded"] == 1
    assert manifest["pages"] == 2
    assert manifest["organization_pages"] == 0
    assert "sitemap-1.xml" in (tmp_path / "sitemap.xml").read_text()
    assert "Q1-ada-lovelace" in (tmp_path / "sitemap-1.xml").read_text()
    assert "Q2-galileo-galilei" in (tmp_path / "sitemap-2.xml").read_text()
    assert json.loads((tmp_path / "manifest.json").read_text())["pages"] == 2


def test_snapshot_response_returns_503_when_not_materialized(monkeypatch, tmp_path):
    monkeypatch.setattr(sitemap_snapshot, "SNAPSHOT_DIR", tmp_path)
    with pytest.raises(HTTPException) as exc:
        sitemap_snapshot.snapshot_response("sitemap.xml")
    assert exc.value.status_code == 503
    assert exc.value.headers == {"Retry-After": "300"}


def test_snapshot_response_rejects_page_beyond_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(sitemap_snapshot, "SNAPSHOT_DIR", tmp_path)
    (tmp_path / "manifest.json").write_text('{"pages": 2}')
    with pytest.raises(HTTPException) as exc:
        sitemap_snapshot.snapshot_response("sitemap-3.xml", page=3)
    assert exc.value.status_code == 404
