"""The sanctioned write path into the shared pool.

This is the only mechanism that can destroy reference data, so most of these
are about what it refuses: clobbering without being told to, promoting a name
that is not a plain reference file, and trusting a filename from the request.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as srv  # noqa: E402

client = TestClient(srv.app)


@pytest.fixture(autouse=True)
def quiet_comments(monkeypatch):
    monkeypatch.setattr(srv, "_kanban_api_call", lambda *a, **k: None)
    async def forced(reason, candidates):
        return {"counts": {"added": 1, "removed": 0, "changed": 0}}
    monkeypatch.setattr(srv, "_dh_pool_force_snapshot", forced)
    yield


def _fleet(monkeypatch, handler):
    calls = []
    async def fake(host, cmd):
        calls.append(cmd)
        return handler(cmd)
    monkeypatch.setattr(srv, "_fleet_ssh", fake)
    return calls


def test_name_rules_reject_paths_dotfiles_and_odd_extensions():
    ok = srv._dh_promote_name_ok
    assert ok("263.fna") and ok("GCF_000978785.2.gff") and ok("notes.md")
    assert not ok("../../etc/passwd")
    assert not ok("sub/dir/x.fna")
    assert not ok(".hidden.fna")
    assert not ok("evil.sh")
    assert not ok("payload.pyc")
    assert not ok("")


def test_listing_reports_eligibility(monkeypatch):
    _fleet(monkeypatch, lambda cmd: (0, "263.fna\t4096\nrun.sh\t12\n"))
    r = client.get("/api/darkhelix/staged/t_c0ffee")
    body = r.json()
    assert body["ok"] is True
    by = {f["name"]: f["eligible"] for f in body["files"]}
    assert by == {"263.fna": True, "run.sh": False}


def test_bad_task_id_is_rejected():
    assert client.get("/api/darkhelix/staged/not-a-task").status_code == 400
    r = client.post("/api/darkhelix/promote-refs", json={"task_id": "../x"})
    assert r.status_code == 400


def test_refuses_to_overwrite_without_explicit_consent(monkeypatch):
    """Replacing a reference in place is exactly what happened to 263.fna."""
    def handler(cmd):
        if "-printf" in cmd:
            return 0, "263.fna\t4096\n"
        if "if [ -e" in cmd:
            return 0, "263.fna\n"
        raise AssertionError(f"should not have copied: {cmd}")
    _fleet(monkeypatch, handler)

    r = client.post("/api/darkhelix/promote-refs", json={"task_id": "t_c0ffee"})
    assert r.status_code == 409
    assert r.json()["existing"] == ["263.fna"]


def test_overwrite_true_proceeds(monkeypatch):
    def handler(cmd):
        if "-printf" in cmd:
            return 0, "263.fna\t4096\n"
        if "if [ -e" in cmd:
            return 0, "263.fna\n"
        if "cp -f" in cmd:
            return 0, "DONE"
        if "md5sum" in cmd:
            return 0, "aaa  263.fna\naaa  263.fna\n"
        return 0, ""
    _fleet(monkeypatch, handler)

    r = client.post("/api/darkhelix/promote-refs",
                    json={"task_id": "t_c0ffee", "overwrite": True})
    body = r.json()
    assert body["ok"] is True
    assert body["promoted"] == ["263.fna"]
    assert body["overwrote"] == ["263.fna"]


def test_a_requested_name_that_is_not_staged_never_reaches_a_command(monkeypatch):
    calls = _fleet(monkeypatch, lambda cmd: (0, "263.fna\t4096\n"))
    r = client.post("/api/darkhelix/promote-refs",
                    json={"task_id": "t_c0ffee", "files": ["/etc/shadow"]})
    assert r.status_code == 400
    assert "not staged" in r.json()["error"]
    assert not any("shadow" in c for c in calls[1:])


def test_ineligible_files_are_refused_not_promoted(monkeypatch):
    def handler(cmd):
        if "-printf" in cmd:
            return 0, "run.sh\t12\n"
        raise AssertionError(f"should not have proceeded: {cmd}")
    _fleet(monkeypatch, handler)
    r = client.post("/api/darkhelix/promote-refs", json={"task_id": "t_c0ffee"})
    assert r.status_code == 400
    assert r.json()["refused"] == ["run.sh"]


def test_md5_mismatch_is_reported_as_failure(monkeypatch):
    def handler(cmd):
        if "-printf" in cmd:
            return 0, "263.fna\t4096\n"
        if "if [ -e" in cmd:
            return 0, ""
        if "cp -f" in cmd:
            return 0, "DONE"
        if "md5sum" in cmd:
            return 0, "aaa  263.fna\nbbb  263.fna\n"     # differ
        return 0, ""
    _fleet(monkeypatch, handler)
    r = client.post("/api/darkhelix/promote-refs", json={"task_id": "t_c0ffee"})
    body = r.json()
    assert body["ok"] is False
    assert body["failed"] == ["263.fna"]
    assert body["promoted"] == []


def test_dry_run_copies_nothing(monkeypatch):
    def handler(cmd):
        if "-printf" in cmd:
            return 0, "263.fna\t4096\n"
        if "if [ -e" in cmd:
            return 0, ""
        raise AssertionError(f"dry run must not run: {cmd}")
    _fleet(monkeypatch, handler)
    r = client.post("/api/darkhelix/promote-refs",
                    json={"task_id": "t_c0ffee", "dry_run": True})
    body = r.json()
    assert body["dry_run"] is True and body["would_promote"] == ["263.fna"]


def test_nothing_staged_is_a_404(monkeypatch):
    _fleet(monkeypatch, lambda cmd: (0, "NODIR"))
    r = client.post("/api/darkhelix/promote-refs", json={"task_id": "t_c0ffee"})
    assert r.status_code == 404


def test_promotion_writes_the_pool_ledger(monkeypatch):
    """A promotion happens outside any run boundary, so the poller would not
    otherwise re-hash. The writer records its own change."""
    def handler(cmd):
        if "-printf" in cmd:
            return 0, "999.fna\t4096\n"
        if "if [ -e" in cmd:
            return 0, ""
        if "cp -f" in cmd:
            return 0, "DONE"
        if "md5sum" in cmd:
            return 0, "aaa  999.fna\naaa  999.fna\n"
        return 0, ""
    _fleet(monkeypatch, handler)
    seen = {}
    async def forced(reason, candidates):
        seen["reason"], seen["candidates"] = reason, candidates
        return {"counts": {"added": 1, "removed": 0, "changed": 0}}
    monkeypatch.setattr(srv, "_dh_pool_force_snapshot", forced)

    r = client.post("/api/darkhelix/promote-refs", json={"task_id": "t_c0ffee"})
    assert r.json()["ledger"]["counts"]["added"] == 1
    assert seen["candidates"] == ["t_c0ffee"]
    assert "promotion" in seen["reason"]


def test_staging_root_is_outside_the_repo():
    """Inside collab_refs it would register in the manifest as a pool change;
    inside the checkout it would show in git status. A proposal is neither."""
    d = srv._dh_staging_dir("t_c0ffee")
    assert not d.startswith(srv.DARKHELIX_REPO_PATH)
    assert d.endswith("/t_c0ffee")
