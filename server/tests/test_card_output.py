"""FINDINGS: what a finished card produced, as opposed to what it did.

The case this was built from (t_a2f91234, the 2026-09-02 swarm synthesizer)
has all three traps in one card: its newest run is a reclaim with no result,
its output lives on a DIFFERENT card (the swarm root's blackboard), and the
file its metadata names was deleted by the completion that made it done. Each
is a test.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as srv  # noqa: E402

client = TestClient(srv.app)


# --- which strings in metadata are artifacts -------------------------------

def test_picks_named_file_paths_and_nothing_else():
    paths = srv._artifact_paths({
        "synthesis_artifact": "/root/.hermes/kanban/workspaces/t_x/synthesis.md",
        "report_path": "/ssdpool/agent-work/t_x/report.txt",
        # A repo root is a real path and not an artifact: catting a directory
        # and reporting "missing" would be a lie about a healthy card.
        "workspace": "/ssdpool/DARKHELIX",
        "verifier_gate": "t_0408a935 PASSED",
        "worker_session_id": "abc123",
        "runtime_hrs": "1.5-2.5",
    })
    assert paths == ["/root/.hermes/kanban/workspaces/t_x/synthesis.md",
                     "/ssdpool/agent-work/t_x/report.txt"]


def test_a_directory_key_without_an_extension_is_not_an_artifact():
    assert srv._artifact_paths({"output_path": "/ssdpool/agent-work/t_x/"}) == []


def test_ssdpool_paths_are_read_on_snarf():
    """CT111 has no /ssdpool at all, so reading there returns 'no such file'
    and a wrong host would be reported as a deleted artifact."""
    assert srv._artifact_host("/ssdpool/agent-work/t_x/report.md") == "snarf"
    assert srv._artifact_host("/root/.hermes/x.md") != "snarf"


# --- reading one, or saying why not ---------------------------------------

async def test_a_gone_workspace_file_is_reported_with_the_reason(monkeypatch):
    async def fake(host, cmd):
        return 0, "MISSING\n"
    monkeypatch.setattr(srv, "_ssh_run", fake)
    got = await srv._read_artifact("/root/.hermes/kanban/workspaces/t_x/synthesis.md")
    assert got["exists"] is False
    assert "workspace" in got["why"] and "removed" in got["why"]


async def test_a_present_file_comes_back_with_its_content(monkeypatch):
    async def fake(host, cmd):
        return 0, "12\n---8<---\nhello world\n"
    monkeypatch.setattr(srv, "_ssh_run", fake)
    got = await srv._read_artifact("/root/x.md")
    assert got["exists"] is True and got["bytes"] == 12
    assert got["content"].strip() == "hello world"


# --- the swarm blackboard --------------------------------------------------

ROOT_COMMENTS = [
    {"author": "claude", "body": '[swarm:blackboard] {"key": "topology", "value": {"n": 3}}'},
    {"author": "darkhelix", "body": '[swarm:audit] {"task": "t_w1"}'},
    # A worker that posted prose instead of a tagged payload. Real: this is
    # how bioinformatics filed its assessment on 2026-09-02.
    {"author": "bioinformatics", "body": "**t_w2 findings** — no pathway profiling today"},
    {"author": "reclaim-watcher", "body": "[auto-context] Attempt reclaimed (reclaimed): lock=hermes:1"},
]


def _stub_graph(monkeypatch):
    detail = {
        "t_syn": {"task": {"id": "t_syn"}, "comments": [],
                  "links": {"parents": ["t_ver"], "children": []}},
        "t_ver": {"task": {"id": "t_ver"}, "comments": [],
                  "links": {"parents": ["t_root"], "children": ["t_syn"]}},
        "t_root": {"task": {"id": "t_root", "title": "Swarm: something"},
                   "comments": ROOT_COMMENTS, "links": {"parents": [], "children": ["t_ver"]}},
    }
    async def fake(task_id):
        return detail[task_id]
    monkeypatch.setattr(srv, "_kanban_task_detail", fake)
    return detail


async def test_walks_up_to_the_root_and_reads_its_blackboard(monkeypatch):
    detail = _stub_graph(monkeypatch)
    bb = await srv._swarm_blackboard("t_syn", detail["t_syn"])
    assert bb["root_id"] == "t_root"
    kinds = [e["kind"] for e in bb["entries"]]
    # The prose entry is kept as a note; the reclaim notice is not output.
    assert kinds == ["blackboard", "audit", "note"]
    assert bb["entries"][0]["data"] == {"key": "topology", "value": {"n": 3}}
    assert bb["entries"][2]["text"].startswith("**t_w2 findings**")


async def test_a_card_with_no_swarm_above_it_has_no_blackboard(monkeypatch):
    async def fake(task_id):
        return {"task": {"id": task_id}, "comments": [], "links": {}}
    monkeypatch.setattr(srv, "_kanban_task_detail", fake)
    assert await srv._swarm_blackboard("t_solo", {"task": {}, "comments": [],
                                                  "links": {}}) is None


# --- the endpoint ----------------------------------------------------------

def test_reports_the_last_run_that_actually_produced_something(monkeypatch):
    """t_a2f91234's newest run is a reclaim with no summary; the result is in
    the run before it. Reading "the last run" would show an empty pane for a
    card that finished perfectly well."""
    # A real-shaped id: _TASK_ID_RE is ^t_[0-9a-f]{6,}$ and the endpoint
    # rejects anything else before it does any work.
    detail = {
        "task": {"id": "t_a2f91234", "title": "Synthesize", "status": "done",
                 "assignee": "researcher"},
        "comments": [],
        "links": {},
        "runs": [
            {"outcome": "completed", "summary": "the real result",
             "metadata": {"synthesis_artifact": "/root/gone.md"}},
            {"outcome": "reclaimed", "summary": "", "metadata": {}},
        ],
    }
    async def fake_detail(task_id):
        return detail
    async def fake_ssh(host, cmd):
        return 0, "MISSING\n"
    monkeypatch.setattr(srv, "_kanban_task_detail", fake_detail)
    monkeypatch.setattr(srv, "_ssh_run", fake_ssh)

    j = client.get("/api/kanban/t_a2f91234/output").json()
    assert j["ok"] and j["run"]["summary"] == "the real result"
    assert j["artifacts"][0]["exists"] is False
    assert j["blackboard"] is None


def test_rejects_a_bad_task_id():
    assert client.get("/api/kanban/not-a-task/output").status_code == 400
