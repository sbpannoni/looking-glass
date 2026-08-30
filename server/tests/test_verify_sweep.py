"""The automatic side of completion verification (PIPELINE-VERIFICATION item A).

`_darkhelix_verify_completion` itself is exercised against the live board; what
is testable here is the part that decides WHICH cards it runs on -- the seed,
the seen-set, the per-tick bound, and the outage/verdict distinction. Each of
those was a deliberate choice with a failure mode behind it, so each gets a test.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as srv  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Fresh, unseeded sweep state writing to a throwaway file."""
    monkeypatch.setattr(srv, "VERIFY_STATE_PATH", tmp_path / "verify.json")
    monkeypatch.setattr(srv, "_DH_VERIFY_STATE", {"seeded": False, "seen": []})
    monkeypatch.setattr(srv, "_DH_VERIFY_STATUS", {
        "enabled": False, "seeded": False, "last_tick": None,
        "last_error": None, "note": None, "checked": 0, "recent": [],
    })
    yield


def _board(*rows):
    return {"columns": [{"name": "board", "tasks": list(rows)}]}


def _done(task_id, completed_at=0):
    return {"id": task_id, "status": "done", "completed_at": completed_at}


def _stub_board(monkeypatch, board):
    async def fake_get(path):
        return board
    monkeypatch.setattr(srv, "_kanban_api_get", fake_get)


def _stub_verify(monkeypatch, results, calls):
    async def fake_verify(task_id, dry_run=False):
        calls.append(task_id)
        return results.get(task_id, {"ok": True, "verdict": "verified"})
    monkeypatch.setattr(srv, "_darkhelix_verify_completion", fake_verify)


async def test_first_tick_seeds_the_backlog_without_judging_it(monkeypatch):
    """The done lane at first boot was adjudicated by hand; re-blocking it on
    every restart is the whole reason seeding exists."""
    _stub_board(monkeypatch, _board(_done("t_43886eea"), _done("t_97cff6a5")))
    calls: list[str] = []
    _stub_verify(monkeypatch, {}, calls)

    await srv._verify_completions_tick()

    assert calls == []
    assert srv._DH_VERIFY_STATE["seeded"] is True
    assert set(srv._DH_VERIFY_STATE["seen"]) == {"t_43886eea", "t_97cff6a5"}
    assert "without judging" in srv._DH_VERIFY_STATUS["note"]


async def test_seed_persists_so_a_restart_does_not_re_judge(monkeypatch):
    _stub_board(monkeypatch, _board(_done("t_43886eea")))
    _stub_verify(monkeypatch, {}, [])
    await srv._verify_completions_tick()

    on_disk = json.loads(srv.VERIFY_STATE_PATH.read_text())
    assert on_disk["seeded"] is True
    assert on_disk["seen"] == ["t_43886eea"]

    srv._DH_VERIFY_STATE = {"seeded": False, "seen": []}
    srv._dh_verify_load_state()
    assert srv._DH_VERIFY_STATE == {"seeded": True, "seen": ["t_43886eea"]}


async def test_only_newly_done_cards_are_checked(monkeypatch):
    """A card present at seed time is never judged; one that appears after is."""
    _stub_board(monkeypatch, _board(_done("t_old")))
    calls: list[str] = []
    _stub_verify(monkeypatch, {}, calls)
    await srv._verify_completions_tick()          # seeds t_old

    _stub_board(monkeypatch, _board(_done("t_old"), _done("t_new", 5)))
    await srv._verify_completions_tick()

    assert calls == ["t_new"]
    assert "t_new" in srv._DH_VERIFY_STATE["seen"]


async def test_a_card_is_judged_once_not_every_tick(monkeypatch):
    _stub_board(monkeypatch, _board())
    calls: list[str] = []
    _stub_verify(monkeypatch, {}, calls)
    await srv._verify_completions_tick()          # seed (empty board)

    _stub_board(monkeypatch, _board(_done("t_new")))
    await srv._verify_completions_tick()
    await srv._verify_completions_tick()

    assert calls == ["t_new"]


async def test_tick_is_bounded_and_the_remainder_carries_over(monkeypatch):
    """A decomposer fan-out can complete a dozen cards at once; each costs up
    to four ssh round trips to snarf."""
    monkeypatch.setattr(srv, "CFG", {**srv.CFG, "darkhelix": {
        **(srv.CFG.get("darkhelix") or {}), "verify_max_per_tick": 2}})
    _stub_board(monkeypatch, _board())
    calls: list[str] = []
    _stub_verify(monkeypatch, {}, calls)
    await srv._verify_completions_tick()          # seed (empty board)

    _stub_board(monkeypatch, _board(*[_done(f"t_{i}", i) for i in range(5)]))
    await srv._verify_completions_tick()
    assert len(calls) == 2
    await srv._verify_completions_tick()
    assert len(calls) == 4
    await srv._verify_completions_tick()
    assert len(calls) == 5
    assert sorted(calls) == [f"t_{i}" for i in range(5)]


async def test_newest_completions_are_judged_first(monkeypatch):
    monkeypatch.setattr(srv, "CFG", {**srv.CFG, "darkhelix": {
        **(srv.CFG.get("darkhelix") or {}), "verify_max_per_tick": 1}})
    _stub_board(monkeypatch, _board())
    calls: list[str] = []
    _stub_verify(monkeypatch, {}, calls)
    await srv._verify_completions_tick()

    _stub_board(monkeypatch, _board(_done("t_older", 10), _done("t_newer", 99)))
    await srv._verify_completions_tick()
    assert calls == ["t_newer"]


async def test_an_outage_is_retried_not_recorded_as_a_pass(monkeypatch):
    """`ok: False` means snarf or the board was unreachable. Marking the card
    seen there would let an outage launder a card past the check forever."""
    _stub_board(monkeypatch, _board())
    calls: list[str] = []
    results = {"t_flaky": {"ok": False, "error": "task lookup failed: boom"}}
    _stub_verify(monkeypatch, results, calls)
    await srv._verify_completions_tick()          # seed (empty board)

    _stub_board(monkeypatch, _board(_done("t_flaky")))
    await srv._verify_completions_tick()
    assert "t_flaky" not in srv._DH_VERIFY_STATE["seen"]
    assert srv._DH_VERIFY_STATUS["recent"][0]["verdict"] == "error"

    results["t_flaky"] = {"ok": True, "verdict": "verified"}
    await srv._verify_completions_tick()
    assert calls == ["t_flaky", "t_flaky"]
    assert "t_flaky" in srv._DH_VERIFY_STATE["seen"]


async def test_a_raising_verify_does_not_stop_the_tick(monkeypatch):
    _stub_board(monkeypatch, _board())
    await srv._verify_completions_tick()          # seed (empty board)

    async def fake_verify(task_id, dry_run=False):
        if task_id == "t_boom":
            raise RuntimeError("ssh exploded")
        return {"ok": True, "verdict": "verified"}
    monkeypatch.setattr(srv, "_darkhelix_verify_completion", fake_verify)

    _stub_board(monkeypatch, _board(_done("t_boom", 9), _done("t_fine", 1)))
    await srv._verify_completions_tick()
    assert "t_fine" in srv._DH_VERIFY_STATE["seen"]
    assert "t_boom" not in srv._DH_VERIFY_STATE["seen"]


async def test_unverified_records_what_reached_the_card(monkeypatch):
    """`action` is _kanban_block's report -- blocked/commented/failed -- not an
    assumption that the move landed."""
    _stub_board(monkeypatch, _board())
    await srv._verify_completions_tick()

    _stub_verify(monkeypatch, {"t_bad": {
        "ok": True, "verdict": "unverified", "action": "blocked",
        "checked": ["branch hermes/t_bad: absent"]}}, [])
    _stub_board(monkeypatch, _board(_done("t_bad")))
    await srv._verify_completions_tick()

    entry = srv._DH_VERIFY_STATUS["recent"][0]
    assert entry["verdict"] == "unverified"
    assert entry["action"] == "blocked"
    assert entry["checked_for"] == ["branch hermes/t_bad: absent"]


async def test_non_done_cards_are_ignored(monkeypatch):
    _stub_board(monkeypatch, _board(
        {"id": "t_running", "status": "running"},
        {"id": "t_blocked", "status": "blocked"}))
    calls: list[str] = []
    _stub_verify(monkeypatch, {}, calls)
    await srv._verify_completions_tick()
    assert srv._DH_VERIFY_STATE["seen"] == []
    _stub_board(monkeypatch, _board({"id": "t_running", "status": "running"}))
    await srv._verify_completions_tick()
    assert calls == []


async def test_seen_set_is_bounded(monkeypatch):
    for i in range(srv._DH_VERIFY_SEEN_MAX + 25):
        srv._dh_verify_mark_seen(f"t_{i:04x}")
    seen = srv._DH_VERIFY_STATE["seen"]
    assert len(seen) == srv._DH_VERIFY_SEEN_MAX
    assert seen[-1] == f"t_{srv._DH_VERIFY_SEEN_MAX + 24:04x}"   # newest kept
    assert "t_0000" not in seen                                  # oldest dropped


async def test_loop_is_off_unless_configured(monkeypatch):
    """It moves cards on a shared board, so it must be opt-in."""
    monkeypatch.setattr(srv, "CFG", {**srv.CFG, "darkhelix": {"assignee": "coder"}})
    called = False

    async def fake_tick():
        nonlocal called
        called = True
    monkeypatch.setattr(srv, "_verify_completions_tick", fake_tick)

    await srv._verify_completions_forever()   # returns immediately, no loop
    assert called is False
    assert srv._DH_VERIFY_STATUS["enabled"] is False
