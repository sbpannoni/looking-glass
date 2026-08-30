"""Item C's lever: end the run behind a card that blocked itself.

The value of this mechanism is entirely in what it refuses to do -- signal a
stale or recycled pid, signal across hosts, or touch the card's status. Those
are the tests.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as srv  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "BLOCK_STATE_PATH", tmp_path / "eb.json")
    monkeypatch.setattr(srv, "_DH_BLOCK_STATE", {"seeded": False, "handled": []})
    monkeypatch.setattr(srv, "_DH_BLOCK_STATUS", {
        "enabled": False, "seeded": False, "last_tick": None,
        "last_error": None, "note": None, "killed": 0, "recent": []})
    async def host():
        return "hermes"
    monkeypatch.setattr(srv, "_dh_kanban_hostname", host)
    async def no_comment(*a, **k):
        return None
    monkeypatch.setattr(srv, "asyncio_to_thread_stub", None, raising=False)
    yield


def _detail(status="blocked", lock="hermes:4242", pid=4242):
    return {"task": {"id": "t_c0ffee", "status": status,
                     "claim_lock": lock, "worker_pid": pid}}


def _stub_detail(monkeypatch, detail):
    async def fake(task_id):
        return detail
    monkeypatch.setattr(srv, "_kanban_task_detail", fake)


def _stub_ssh(monkeypatch, token="TERMED"):
    seen = []
    async def fake(cmd):
        seen.append(cmd)
        return 0, token
    monkeypatch.setattr(srv, "_kanban_ssh", fake)
    return seen


def _no_comments(monkeypatch):
    def fake_call(method, path, **kw):
        return None
    monkeypatch.setattr(srv, "_kanban_api_call", fake_call)


async def test_terminates_a_live_host_local_worker(monkeypatch):
    _stub_detail(monkeypatch, _detail())
    seen = _stub_ssh(monkeypatch, "TERMED")
    _no_comments(monkeypatch)

    r = await srv._dh_enforce_block("t_c0ffee")

    assert r["ok"] and r["outcome"] == "TERMED"
    cmd = " ".join(seen)
    assert "kill -TERM" in cmd and "kill -KILL" in cmd


async def test_never_touches_the_cards_status(monkeypatch):
    """The whole reason reclaim is not the lever: `ready` is dispatchable."""
    _stub_detail(monkeypatch, _detail())
    seen = _stub_ssh(monkeypatch, "TERMED")
    _no_comments(monkeypatch)

    await srv._dh_enforce_block("t_c0ffee")

    joined = " ".join(seen)
    for forbidden in ("reclaim", "unblock", "status", "complete"):
        assert forbidden not in joined


async def test_pid_must_belong_to_this_cards_hermes_worker(monkeypatch):
    """A stale pid can be recycled by the OS. Identity check and signal must
    be one command -- checking then signalling is a race with that exact
    failure mode."""
    _stub_detail(monkeypatch, _detail())
    seen = _stub_ssh(monkeypatch, "TERMED")
    _no_comments(monkeypatch)

    await srv._dh_enforce_block("t_c0ffee")

    assert len(seen) == 1, "identity check and kill must not be separate calls"
    cmd = seen[0]
    assert "/proc/$p/cmdline" in cmd
    assert "hermes" in cmd and "t_c0ffee" in cmd
    assert cmd.index("cmdline") < cmd.index("kill -TERM")


async def test_a_recycled_pid_is_not_killed(monkeypatch):
    _stub_detail(monkeypatch, _detail())
    _stub_ssh(monkeypatch, "NOTHERMES")
    r = await srv._dh_enforce_block("t_c0ffee")
    assert r["ok"] is False
    assert r["outcome"] == "NOTHERMES"


async def test_a_pid_running_a_different_task_is_not_killed(monkeypatch):
    _stub_detail(monkeypatch, _detail())
    _stub_ssh(monkeypatch, "WRONGTASK")
    r = await srv._dh_enforce_block("t_c0ffee")
    assert r["ok"] is False and r["outcome"] == "WRONGTASK"


async def test_a_remote_claim_is_declined(monkeypatch):
    """Same guard Hermes applies -- we cannot signal another host from here."""
    _stub_detail(monkeypatch, _detail(lock="othermachine:99"))
    seen = _stub_ssh(monkeypatch)
    r = await srv._dh_enforce_block("t_c0ffee")
    assert r["outcome"] == "remote-claim"
    assert not any("kill" in c for c in seen)


async def test_a_card_with_no_live_claim_is_left_alone(monkeypatch):
    _stub_detail(monkeypatch, _detail(lock=None, pid=None))
    seen = _stub_ssh(monkeypatch)
    r = await srv._dh_enforce_block("t_c0ffee")
    assert r["outcome"] == "no-live-claim"
    assert not any("kill" in c for c in seen)


async def test_a_card_that_is_not_blocked_is_skipped(monkeypatch):
    _stub_detail(monkeypatch, _detail(status="running"))
    seen = _stub_ssh(monkeypatch)
    r = await srv._dh_enforce_block("t_c0ffee")
    assert r["outcome"] == "skipped"
    assert not any("kill" in c for c in seen)


async def test_dry_run_signals_nothing(monkeypatch):
    _stub_detail(monkeypatch, _detail())
    seen = _stub_ssh(monkeypatch)
    r = await srv._dh_enforce_block("t_c0ffee", dry_run=True)
    assert r["outcome"] == "would-terminate" and r["dry_run"] is True
    assert not any("kill" in c for c in seen)


async def test_seed_never_signals_the_existing_blocked_lane(monkeypatch):
    """Those runs ended long ago; their worker_pid is stale, and a stale pid
    is the one thing that must never be signalled."""
    async def board(path):
        return {"columns": [{"tasks": [{"id": "t_old1", "status": "blocked"},
                                       {"id": "t_old2", "status": "blocked"}]}]}
    monkeypatch.setattr(srv, "_kanban_api_get", board)
    called = []
    async def enforce(task_id, dry_run=False):
        called.append(task_id)
        return {"ok": True, "outcome": "TERMED"}
    monkeypatch.setattr(srv, "_dh_enforce_block", enforce)

    await srv._enforce_blocks_tick()

    assert called == []
    assert set(srv._DH_BLOCK_STATE["handled"]) == {"t_old1", "t_old2"}


async def test_only_newly_blocked_cards_are_enforced(monkeypatch):
    rows = {"v": [{"id": "t_old", "status": "blocked"}]}
    async def board(path):
        return {"columns": [{"tasks": rows["v"]}]}
    monkeypatch.setattr(srv, "_kanban_api_get", board)
    called = []
    async def enforce(task_id, dry_run=False):
        called.append(task_id)
        return {"ok": True, "outcome": "TERMED"}
    monkeypatch.setattr(srv, "_dh_enforce_block", enforce)

    await srv._enforce_blocks_tick()                  # seed
    rows["v"] = rows["v"] + [{"id": "t_new", "status": "blocked"}]
    await srv._enforce_blocks_tick()
    await srv._enforce_blocks_tick()

    assert called == ["t_new"]


async def test_enforcement_is_off_by_default(monkeypatch):
    """It must not precede the sanctioned staging path."""
    monkeypatch.setattr(srv, "CFG", {**srv.CFG, "darkhelix": {"assignee": "coder"}})
    called = False
    async def tick():
        nonlocal called
        called = True
    monkeypatch.setattr(srv, "_enforce_blocks_tick", tick)
    await srv._poll_enforce_blocks_forever()
    assert called is False


async def test_shipped_config_never_enforces_without_a_staging_path():
    """The ordering constraint, as an invariant that outlives the rollout.

    This started life as "enforcement must be off", which was right only until
    the staging mount landed on snarf (2026-08-30). The durable rule is the
    reason behind it: ending a run the moment a card blocks itself is only
    legitimate while a card that needs to ADD a reference genome has somewhere
    to put it. Enforcement with no staging root configured rebuilds the hard
    wall this whole item exists to dismantle.
    """
    dh = srv.CFG.get("darkhelix") or {}
    if dh.get("enforce_block") is True:
        assert (dh.get("pool_staging_root") or "").startswith("/"), (
            "enforce_block is on with no pool_staging_root — a card needing to "
            "add a reference genome would have no sanctioned path")
