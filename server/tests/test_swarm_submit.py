"""SUBMIT WORK's second shape: filing a swarm instead of a triage card.

The value of this endpoint is almost entirely in what it refuses to send. A
swarm hardcodes `humanizer` onto its synthesizer card and
`requesting-code-review` onto its verifier without checking the profile has
them, and it parses `--worker profile:title:skills` by splitting on colons --
so a wrong dropdown or a colon in a sentence produces a card that dies at
agent init AFTER every worker has finished. Those refusals are the tests.
"""
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as srv  # noqa: E402

client = TestClient(srv.app)

PROBE_OUT = (
    "ai-tune: humanizer requesting-code-review\n"
    "coder: requesting-code-review\n"          # no humanizer -> cannot synthesize
    "darkhelix: humanizer requesting-code-review\n"
    "researcher: humanizer\n"                  # no requesting-code-review -> cannot verify
)


@pytest.fixture(autouse=True)
def fresh_profile_cache():
    srv._PROFILES_CACHE.update({"at": 0.0, "rows": []})
    yield
    srv._PROFILES_CACHE.update({"at": 0.0, "rows": []})


def _stub_ssh(monkeypatch, swarm_out='{"root_id": "t_aaaa1111", '
                                     '"worker_ids": ["t_bbbb2222", "t_cccc3333"], '
                                     '"verifier_id": "t_dddd4444", '
                                     '"synthesizer_id": "t_eeee5555"}', rc=0):
    """One stub for both ssh users: the profile probe and the swarm command."""
    seen = []

    async def fake(cmd):
        seen.append(cmd)
        if "profiles" in cmd:
            return 0, PROBE_OUT
        return rc, swarm_out

    monkeypatch.setattr(srv, "_kanban_ssh", fake)
    return seen


def _stub_detail(monkeypatch, created_at=None):
    async def fake(task_id):
        return {"task": {"id": task_id,
                         "created_at": time.time() if created_at is None else created_at}}
    monkeypatch.setattr(srv, "_kanban_task_detail", fake)


def _post(**over):
    payload = {"title": "An item", "body": "The full item text",
               "workers": [{"profile": "darkhelix", "title": "First angle"},
                           {"profile": "researcher", "title": "Second angle"}],
               "verifier": "darkhelix", "synthesizer": "researcher"}
    payload.update(over)
    return client.post("/api/kanban/swarm", json=payload)


# --- the probe -------------------------------------------------------------

async def test_profiles_report_which_roles_each_can_hold(monkeypatch):
    _stub_ssh(monkeypatch)
    rows = {p["name"]: p for p in await srv._hermes_profiles()}
    assert rows["darkhelix"]["can_verify"] and rows["darkhelix"]["can_synthesize"]
    # The two live gaps this endpoint exists to catch.
    assert rows["coder"]["can_verify"] and not rows["coder"]["can_synthesize"]
    assert rows["researcher"]["can_synthesize"] and not rows["researcher"]["can_verify"]


async def test_an_empty_probe_is_an_error_not_an_empty_roster(monkeypatch):
    """"No profiles" and "could not read the profiles" must not look alike --
    the second one silently disables every role check."""
    async def fake(cmd):
        return 0, ""
    monkeypatch.setattr(srv, "_kanban_ssh", fake)
    with pytest.raises(RuntimeError):
        await srv._hermes_profiles()
    assert srv._PROFILES_CACHE["rows"] == []


# --- what it refuses to file ----------------------------------------------

def test_rejects_a_synthesizer_that_cannot_load_the_humanizer_skill(monkeypatch):
    seen = _stub_ssh(monkeypatch)
    r = _post(synthesizer="coder")
    assert r.status_code == 400
    assert "humanizer" in r.json()["error"]
    assert not any("kanban swarm" in c for c in seen)


def test_rejects_a_verifier_that_cannot_load_the_review_skill(monkeypatch):
    seen = _stub_ssh(monkeypatch)
    r = _post(verifier="researcher")
    assert r.status_code == 400
    assert "requesting-code-review" in r.json()["error"]
    assert not any("kanban swarm" in c for c in seen)


def test_rejects_a_colon_in_a_worker_angle(monkeypatch):
    """parse_worker_arg would read "the four layers" as a SKILL LIST."""
    seen = _stub_ssh(monkeypatch)
    r = _post(workers=[{"profile": "darkhelix", "title": "Audit X: the four layers"}])
    assert r.status_code == 400
    assert "colon" in r.json()["error"]
    assert not any("kanban swarm" in c for c in seen)


def test_rejects_an_unknown_profile(monkeypatch):
    _stub_ssh(monkeypatch)
    r = _post(workers=[{"profile": "nobody", "title": "An angle"}])
    assert r.status_code == 400 and "nobody" in r.json()["error"]


def test_refuses_to_file_when_the_profiles_cannot_be_read(monkeypatch):
    """Fail closed: an unchecked swarm risks losing every worker's run at the
    last card, which is worse than not filing."""
    async def fake(cmd):
        raise RuntimeError("ssh down")
    monkeypatch.setattr(srv, "_kanban_ssh", fake)
    r = _post()
    assert r.status_code == 502
    assert "refusing" in r.json()["error"]


def test_rejects_no_workers_and_too_many_workers(monkeypatch):
    _stub_ssh(monkeypatch)
    assert _post(workers=[]).status_code == 400
    many = [{"profile": "darkhelix", "title": f"angle {i}"}
            for i in range(srv._SWARM_MAX_WORKERS + 1)]
    assert _post(workers=many).status_code == 400


# --- what it sends ---------------------------------------------------------

def test_files_the_graph_as_the_hud_so_every_card_gets_a_worktree(monkeypatch):
    """created_by propagates to all five cards, and it is the first thing
    _darkhelix_lineage checks -- this is what puts the whole swarm in scope
    for claim-time provisioning."""
    seen = _stub_ssh(monkeypatch)
    _stub_detail(monkeypatch)
    r = _post()
    assert r.status_code == 200
    cmd = next(c for c in seen if "kanban swarm" in c)
    assert f"--created-by {srv._HUD_CREATOR}" in cmd
    assert "'darkhelix:First angle'" in cmd and "'researcher:Second angle'" in cmd
    assert "--verifier darkhelix" in cmd and "--synthesizer researcher" in cmd


def test_dedups_on_the_same_key_as_a_single_card_submission(monkeypatch):
    """One item, one identity, whichever shape it was filed in: filing it as a
    swarm marks it filed in the picker, and a later single-card submission
    returns the swarm's root instead of opening a second front."""
    seen = _stub_ssh(monkeypatch)
    _stub_detail(monkeypatch)
    r = _post()
    key = srv._submission_key("An item", "The full item text")
    assert r.json()["idempotency_key"] == key
    assert key in next(c for c in seen if "kanban swarm" in c)


def test_every_card_is_told_where_durable_output_goes(monkeypatch):
    """The rule lives in `execution-engine-dispatch`, which only `coder`
    carries -- and the card that lost its synthesis.md to a deleted workspace
    was a `researcher` synthesizer. create_swarm copies the goal onto every
    card it makes, so the goal is the one place that reaches all of them."""
    seen = _stub_ssh(monkeypatch)
    _stub_detail(monkeypatch)
    _post()
    cmd = next(c for c in seen if "kanban swarm" in c)
    assert "where your output has to live" in cmd
    assert "/ssdpool/agent-work/" in cmd


def test_the_output_rule_does_not_change_the_dedup_identity(monkeypatch):
    """Boilerplate appended to the goal must not reach the key, or every
    submission becomes unique and the dedup above is silently dead."""
    seen = _stub_ssh(monkeypatch)
    _stub_detail(monkeypatch)
    r = _post()
    assert r.json()["idempotency_key"] == srv._submission_key("An item", "The full item text")


def test_reports_the_whole_graph_back(monkeypatch):
    _stub_ssh(monkeypatch)
    _stub_detail(monkeypatch)
    j = _post().json()
    assert j["root"] == "t_aaaa1111"
    assert [w["id"] for w in j["workers"]] == ["t_bbbb2222", "t_cccc3333"]
    assert [w["profile"] for w in j["workers"]] == ["darkhelix", "researcher"]
    assert j["verifier"]["id"] == "t_dddd4444" and j["synthesizer"]["id"] == "t_eeee5555"
    assert j["duplicate"] is False


def test_an_old_root_means_the_swarm_already_existed(monkeypatch):
    """create_swarm recovers the existing topology from the root's blackboard
    and returns the same ids, saying nothing about which happened. Age is the
    only tell, same as /api/kanban/create."""
    _stub_ssh(monkeypatch)
    _stub_detail(monkeypatch, created_at=time.time() - 3600)
    assert _post().json()["duplicate"] is True


def test_a_failing_cli_is_reported_not_swallowed(monkeypatch):
    _stub_ssh(monkeypatch, swarm_out="kanban swarm: at least one --worker", rc=2)
    r = _post()
    assert r.status_code == 502 and "--worker" in r.json()["error"]


# --- BUILDS ON lineage -----------------------------------------------------
# `captures_all` is a claim about the graph -- "build on this one and you
# inherit the whole run" -- and the dropdown puts a star on it. A star on the
# wrong card sends follow-up work off a branch missing half the run, so the
# tests are mostly about when it must NOT be set.

def _annotate(edges, ids):
    """edges: {child: [parents]}"""
    cards = [{"id": i, "created_at": n} for n, i in enumerate(ids)]
    parents = {i: set(edges.get(i, ())) for i in ids}
    children = {i: set() for i in ids}
    for child, ps in edges.items():
        for pid in ps:
            # Mirrors the endpoint: a link to a card outside the window is
            # recorded on the child and nowhere else.
            if pid in children:
                children[pid].add(child)
    srv._lineage_annotate(cards, parents, children)
    return {c["id"]: c for c in cards}


def test_the_swarm_synthesizer_is_the_card_that_captures_everything():
    """The real shape: root -> 3 workers -> verifier -> synthesizer."""
    rows = _annotate(
        {"w1": ["root"], "w2": ["root"], "w3": ["root"],
         "ver": ["w1", "w2", "w3"], "syn": ["ver"]},
        ["root", "w1", "w2", "w3", "ver", "syn"])
    assert rows["syn"]["captures_all"] is True
    assert [rows[i]["captures_all"] for i in ("root", "w1", "ver")] == [False, False, False]
    assert rows["root"]["depth"] == 0 and rows["w1"]["depth"] == 1
    assert rows["ver"]["depth"] == 2 and rows["syn"]["depth"] == 3
    assert {rows[i]["tree"] for i in rows} == {0}


def test_nothing_is_starred_when_no_single_card_covers_the_tree():
    """Two independent leaves off one root: neither inherits the other."""
    rows = _annotate({"a": ["root"], "b": ["root"]}, ["root", "a", "b"])
    assert not any(r["captures_all"] for r in rows.values())


def test_a_lone_card_captures_nothing():
    rows = _annotate({}, ["solo"])
    assert rows["solo"]["captures_all"] is False and rows["solo"]["tree_size"] == 1


def test_separate_trees_get_separate_ids_and_their_own_star():
    rows = _annotate({"a2": ["a1"], "b2": ["b1"]}, ["a1", "a2", "b1", "b2"])
    assert rows["a1"]["tree"] != rows["b1"]["tree"]
    assert rows["a2"]["captures_all"] and rows["b2"]["captures_all"]
    assert rows["a2"]["tree_size"] == 2


def test_a_cycle_terminates_instead_of_recursing_forever():
    """The board should not contain one; a claim about the graph must not
    depend on that."""
    rows = _annotate({"a": ["b"], "b": ["a"], "c": ["a"]}, ["a", "b", "c"])
    assert all("depth" in r for r in rows.values())


def test_links_to_cards_outside_the_window_are_ignored():
    """Only the newest _LINEAGE_MAX_CARDS are fetched, so a parent may be off
    the end of the list -- it must not become a phantom ancestor."""
    rows = _annotate({"a": ["off_the_list"]}, ["a", "b"])
    assert rows["a"]["depth"] == 0 and rows["a"]["captures_all"] is False
