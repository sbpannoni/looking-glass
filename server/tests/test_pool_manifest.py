"""Shared-pool manifest logging (PIPELINE-VERIFICATION, database policy 3).

`database/` is gitignored and shared by every worktree, so a mutation leaves no
diff and nothing to revert. These cover the part that decides WHEN to hash and
WHO a change is attributed to -- including the t_d17fef80 signature, a pool
that moves while no card holds the pipeline.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as srv  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "POOL_MANIFEST_PATH", tmp_path / "manifest.json")
    monkeypatch.setattr(srv, "POOL_DELTA_LOG", tmp_path / "deltas.jsonl")
    monkeypatch.setattr(srv, "_DH_POOL_MANIFEST", None)
    monkeypatch.setattr(srv, "_DH_POOL_STATUS", {
        "enabled": False, "baseline": False, "last_tick": None,
        "last_error": None, "note": None, "snapshots": 0,
        "windows_clean": 0, "deltas": 0, "recent": [],
    })
    monkeypatch.setattr(srv, "CFG", {**srv.CFG, "darkhelix": {
        **(srv.CFG.get("darkhelix") or {}),
        "pool_manifest": True, "pool_manifest_paths": ["database/collab_refs"]}})
    yield


def _board(tasks, event_id=1):
    return {"columns": [{"name": "b", "tasks": list(tasks)}],
            "latest_event_id": event_id}


def _stub(monkeypatch, board, files):
    """Board and pool are mutable dicts the test rebinds between ticks."""
    async def fake_get(path):
        return board["v"]
    async def fake_snapshot():
        return dict(files["v"])
    monkeypatch.setattr(srv, "_kanban_api_get", fake_get)
    monkeypatch.setattr(srv, "_dh_pool_snapshot", fake_snapshot)


def _stub_comments(monkeypatch):
    posted = []
    async def fake_report(record):
        posted.append(record)
        record["commented"] = list(record["candidates"])
    monkeypatch.setattr(srv, "_dh_pool_report", fake_report)
    return posted


async def test_first_tick_takes_a_baseline_and_attributes_nothing(monkeypatch):
    board = {"v": _board([{"id": "t_a", "status": "running"}])}
    files = {"v": {"database/collab_refs/234.fna": "aaa"}}
    _stub(monkeypatch, board, files)
    posted = _stub_comments(monkeypatch)

    await srv._pool_manifest_tick()

    assert posted == []
    assert srv._DH_POOL_STATUS["baseline"] is True
    assert srv._DH_POOL_MANIFEST["files"] == files["v"]
    assert srv._DH_POOL_MANIFEST["in_flight"] == ["t_a"]


async def test_no_boundary_means_no_rehash(monkeypatch):
    """Hashing is the expensive part; it happens at run boundaries only."""
    board = {"v": _board([{"id": "t_a", "status": "running"}], event_id=7)}
    files = {"v": {"f": "aaa"}}
    _stub(monkeypatch, board, files)
    await srv._pool_manifest_tick()
    assert srv._DH_POOL_STATUS["snapshots"] == 1

    await srv._pool_manifest_tick()
    await srv._pool_manifest_tick()
    assert srv._DH_POOL_STATUS["snapshots"] == 1


async def test_delta_is_attributed_to_the_card_that_was_running(monkeypatch):
    board = {"v": _board([{"id": "t_a", "status": "running"}], event_id=1)}
    files = {"v": {"database/collab_refs/234.fna": "aaa"}}
    _stub(monkeypatch, board, files)
    posted = _stub_comments(monkeypatch)
    await srv._pool_manifest_tick()                      # baseline

    # t_a finishes, and the pool moved exactly the way t_d17fef80 moved it.
    board["v"] = _board([{"id": "t_a", "status": "done", "completed_at": 1}],
                        event_id=2)
    files["v"] = {"database/collab_refs/263.fna": "bbb"}
    await srv._pool_manifest_tick()

    assert len(posted) == 1
    rec = posted[0]
    assert rec["candidates"] == ["t_a"]
    assert rec["delta"]["removed"] == ["database/collab_refs/234.fna"]
    assert rec["delta"]["added"] == ["database/collab_refs/263.fna"]
    assert srv._DH_POOL_STATUS["deltas"] == 1


async def test_a_clean_window_files_nothing_but_is_counted(monkeypatch):
    board = {"v": _board([{"id": "t_a", "status": "running"}], event_id=1)}
    files = {"v": {"f": "aaa"}}
    _stub(monkeypatch, board, files)
    posted = _stub_comments(monkeypatch)
    await srv._pool_manifest_tick()

    board["v"] = _board([{"id": "t_a", "status": "done", "completed_at": 1}],
                        event_id=2)
    await srv._pool_manifest_tick()

    assert posted == []
    assert srv._DH_POOL_STATUS["windows_clean"] == 1
    assert srv._DH_POOL_STATUS["deltas"] == 0


async def test_pool_moving_with_nobody_running_is_flagged_unattributed(monkeypatch):
    """The t_d17fef80 signature: a blocked worker doing the work by hand,
    outside the container and outside the board. Nobody to comment on, so it
    must not be dropped for lack of a recipient."""
    board = {"v": _board([], event_id=1)}
    files = {"v": {"database/collab_refs/234.fna": "aaa"}}
    _stub(monkeypatch, board, files)
    posted = _stub_comments(monkeypatch)
    await srv._pool_manifest_tick()                      # baseline, nobody running

    board["v"] = _board([], event_id=2)
    files["v"] = {}                                      # file deleted by hand
    await srv._pool_manifest_tick()

    assert posted == []                                  # no card to blame
    written = [json.loads(l) for l in
               srv.POOL_DELTA_LOG.read_text().splitlines()]
    assert len(written) == 1
    assert written[0]["unattributed"] is True
    assert written[0]["delta"]["removed"] == ["database/collab_refs/234.fna"]
    assert srv._DH_POOL_STATUS["recent"][0]["unattributed"] is True


async def test_concurrent_cards_make_attribution_ambiguous(monkeypatch):
    """The pool is shared. With two cards in flight the honest answer is that
    it could be either, not a confident finger at one."""
    board = {"v": _board([{"id": "t_a", "status": "running"},
                          {"id": "t_b", "status": "running"}], event_id=1)}
    files = {"v": {"f": "aaa"}}
    _stub(monkeypatch, board, files)
    posted = _stub_comments(monkeypatch)
    await srv._pool_manifest_tick()

    board["v"] = _board([{"id": "t_a", "status": "done", "completed_at": 1},
                         {"id": "t_b", "status": "running"}], event_id=2)
    files["v"] = {"f": "bbb"}
    await srv._pool_manifest_tick()

    assert posted[0]["candidates"] == ["t_a", "t_b"]
    body = srv._dh_pool_comment_body(posted[0])
    assert "Attribution is ambiguous" in body
    assert "t_a" in body and "t_b" in body


async def test_card_that_starts_and_ends_inside_one_interval_is_caught(monkeypatch):
    """The running-set alone cannot see this -- it reads empty both ticks.
    latest_event_id moving is what makes the boundary visible."""
    board = {"v": _board([], event_id=1)}
    files = {"v": {"f": "aaa"}}
    _stub(monkeypatch, board, files)
    posted = _stub_comments(monkeypatch)
    await srv._pool_manifest_tick()
    baseline_at = srv._DH_POOL_MANIFEST["taken_at"]

    board["v"] = _board([{"id": "t_quick", "status": "done",
                          "completed_at": baseline_at + 1}], event_id=9)
    files["v"] = {"f": "bbb"}
    await srv._pool_manifest_tick()

    assert posted[0]["candidates"] == ["t_quick"]


async def test_a_card_done_before_the_window_is_not_blamed(monkeypatch):
    board = {"v": _board([], event_id=1)}
    files = {"v": {"f": "aaa"}}
    _stub(monkeypatch, board, files)
    posted = _stub_comments(monkeypatch)
    await srv._pool_manifest_tick()
    baseline_at = srv._DH_POOL_MANIFEST["taken_at"]

    board["v"] = _board([{"id": "t_ancient", "status": "done",
                          "completed_at": baseline_at - 5000}], event_id=9)
    files["v"] = {"f": "bbb"}
    await srv._pool_manifest_tick()

    assert posted == []                                  # nobody in the window
    written = [json.loads(l) for l in
               srv.POOL_DELTA_LOG.read_text().splitlines()]
    assert written[0]["unattributed"] is True


async def test_ledger_is_written_before_commenting(monkeypatch):
    """CT111 being unreachable must not lose the record -- the local log is
    the source of truth, the comment is the convenience."""
    board = {"v": _board([{"id": "t_a", "status": "running"}], event_id=1)}
    files = {"v": {"f": "aaa"}}
    _stub(monkeypatch, board, files)

    async def exploding_report(record):
        raise RuntimeError("CT111 down")
    monkeypatch.setattr(srv, "_dh_pool_report", exploding_report)
    await srv._pool_manifest_tick()

    board["v"] = _board([{"id": "t_a", "status": "done", "completed_at": 1}],
                        event_id=2)
    files["v"] = {"f": "bbb"}
    with pytest.raises(RuntimeError):
        await srv._pool_manifest_tick()

    written = [json.loads(l) for l in
               srv.POOL_DELTA_LOG.read_text().splitlines()]
    assert written[0]["delta"]["changed"] == ["f"]


async def test_manifest_survives_restart(monkeypatch):
    board = {"v": _board([{"id": "t_a", "status": "running"}], event_id=1)}
    files = {"v": {"f": "aaa"}}
    _stub(monkeypatch, board, files)
    await srv._pool_manifest_tick()

    srv._DH_POOL_MANIFEST = None
    srv._dh_pool_manifest_load()
    assert srv._DH_POOL_MANIFEST["files"] == {"f": "aaa"}
    assert srv._DH_POOL_MANIFEST["in_flight"] == ["t_a"]


async def test_watched_paths_cannot_escape_the_repo(monkeypatch):
    monkeypatch.setattr(srv, "CFG", {**srv.CFG, "darkhelix": {
        "pool_manifest": True,
        "pool_manifest_paths": ["database/collab_refs", "/etc",
                                "../../root", "ok/path"]}})
    assert srv._dh_pool_paths() == ["database/collab_refs", "etc", "ok/path"]


async def test_snapshot_refuses_an_enormous_path(monkeypatch):
    """A config typo pointing at all 582G of database/ must fail fast, not
    spend an hour hashing and log nothing."""
    async def huge(host, cmd):
        return 0, "\n".join(f"{'0'*32}  f{i}" for i in range(srv._DH_POOL_MAX_FILES + 1))
    monkeypatch.setattr(srv, "_fleet_ssh", huge)
    with pytest.raises(RuntimeError, match="refuses"):
        await srv._dh_pool_snapshot()


async def test_snapshot_parses_md5sum_output(monkeypatch):
    async def out(host, cmd):
        return 0, ("d41d8cd98f00b204e9800998ecf8427e  database/collab_refs/a.fna\n"
                   "fc253a779d2d84d45b59b3eccd84d705  database/collab_refs/b.fna\n"
                   "\\bad  database/collab_refs/weird\n"
                   "garbage line\n")
    monkeypatch.setattr(srv, "_fleet_ssh", out)
    files = await srv._dh_pool_snapshot()
    assert files == {"database/collab_refs/a.fna": "d41d8cd98f00b204e9800998ecf8427e",
                     "database/collab_refs/b.fna": "fc253a779d2d84d45b59b3eccd84d705"}


async def test_loop_is_off_unless_configured(monkeypatch):
    monkeypatch.setattr(srv, "CFG", {**srv.CFG, "darkhelix": {"assignee": "coder"}})
    called = False
    async def fake_tick():
        nonlocal called
        called = True
    monkeypatch.setattr(srv, "_pool_manifest_tick", fake_tick)
    await srv._poll_pool_manifest_forever()
    assert called is False
    assert srv._DH_POOL_STATUS["enabled"] is False
