import os

from app.routers import person_page


def test_get_shell_reloads_after_frontend_volume_changes(tmp_path, monkeypatch):
    shell = tmp_path / "index.html"
    shell.write_text('<script src="/assets/old.js"></script>', encoding="utf-8")
    monkeypatch.setattr(person_page, "_FRONTEND_SHELL_PATH", str(shell))
    monkeypatch.setattr(person_page, "_shell_cache", None)
    monkeypatch.setattr(person_page, "_shell_hash", None)
    monkeypatch.setattr(person_page, "_shell_signature", None)

    first, first_hash = person_page.get_shell()
    shell.write_text('<script src="/assets/new.js"></script>', encoding="utf-8")
    os.utime(shell, ns=(shell.stat().st_atime_ns, shell.stat().st_mtime_ns + 1_000_000))
    second, second_hash = person_page.get_shell()

    assert "old.js" in first
    assert "new.js" in second
    assert first_hash != second_hash
