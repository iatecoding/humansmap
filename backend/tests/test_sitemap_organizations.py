import xml.etree.ElementTree as ET

from app.services import sitemap_snapshot


def test_snapshot_includes_bounded_organization_shard(monkeypatch, tmp_path):
    people = [("Q1", "2026-09-10", "Ada Lovelace", None, 301)]
    organizations = [("Q95", "2026-09-10", "Google", None, 500)]
    monkeypatch.setattr(sitemap_snapshot, "_fetch_rows", lambda: (people, 1))
    monkeypatch.setattr(
        sitemap_snapshot, "_fetch_organization_rows", lambda: (organizations, 1)
    )

    manifest = sitemap_snapshot.build_sitemap_snapshot(tmp_path)

    assert manifest["organizations"] == 1
    assert manifest["organization_pages"] == 1
    index = (tmp_path / "sitemap.xml").read_text()
    assert "sitemap-org-1.xml" in index
    shard = (tmp_path / "sitemap-org-1.xml").read_text()
    assert "organization/Q95-google" in shard
    ET.fromstring(index)
    ET.fromstring(shard)
