"""The download backlog: verified URLs, and cards that cannot take the seat.

Nine TODO.md items are blocked on a database or a binary. The value here is
entirely in what the endpoint refuses to claim -- a URL it has not checked, a
download that is already on disk, a card filed for either -- so those are the
tests.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as srv  # noqa: E402

client = TestClient(srv.app)

TODO = """# TODO

## 1. Blocked — missing database or binary
- [ ] **WAITING** GTDB-Tk reference package (~110 GB) — needs `gd_gtdbtk_db` pointed at a release
- [ ] **WAITING** `pathofact` binary + DB — no idea where from
"""

ENTRY = {"id": "gtdbtk", "match": "GTDB-Tk reference package",
         "name": "GTDB-Tk reference package",
         "url": "https://example.invalid/gtdbtk_data.tar.gz",
         "dest": "/ssdpool/DARKHELIX/database/gtdbtk",
         "wire_in": "point gd_gtdbtk_db at it"}


@pytest.fixture(autouse=True)
def catalogue(monkeypatch):
    monkeypatch.setattr(srv, "_download_catalogue", lambda: [dict(ENTRY)])
    srv._URL_CHECKS.clear()
    yield
    srv._URL_CHECKS.clear()


def _stub_fleet(monkeypatch, *, head, dest):
    """One stub for the three shapes of ssh call this endpoint makes."""
    async def fake(host, cmd):
        if "cat " in cmd and "TODO" in cmd:
            return 0, TODO
        if "curl" in cmd:
            return 0, head
        if "du -sb" in cmd:
            return 0, dest
        return 0, ""
    monkeypatch.setattr(srv, "_fleet_ssh", fake)


def _no_board(monkeypatch):
    async def fake():
        raise RuntimeError("board down")
    monkeypatch.setattr(srv, "_submitted_keys", fake)


HEAD_OK = "HTTP/1.1 200 OK\nContent-Length: 60806405195\nSTATUS 200 https://example.invalid/gtdbtk_data.tar.gz\n"
HEAD_404 = "HTTP/1.1 404 Not Found\nSTATUS 404 https://example.invalid/gtdbtk_data.tar.gz\n"


# --- matching ---------------------------------------------------------------

def test_matches_a_todo_item_case_insensitively(monkeypatch):
    assert srv._download_entry_for("gtdb-tk REFERENCE package (~110 GB)")["id"] == "gtdbtk"
    assert srv._download_entry_for("`hostile` binary — Step 6.5") is None


# --- the listing ------------------------------------------------------------

def test_a_blocked_item_with_no_entry_says_so_instead_of_guessing(monkeypatch):
    _stub_fleet(monkeypatch, head=HEAD_OK, dest="ABSENT")
    _no_board(monkeypatch)
    rows = client.get("/api/darkhelix/downloads").json()["items"]
    assert len(rows) == 2
    pathofact = next(r for r in rows if "pathofact" in r["text"])
    assert pathofact["entry"] is None


def test_reads_the_remote_size_out_of_the_headers(monkeypatch):
    """snarf's curl has no %{content_length_download} write-out variable, and
    asking for one fails the whole call rather than that one field."""
    _stub_fleet(monkeypatch, head=HEAD_OK, dest="ABSENT")
    _no_board(monkeypatch)
    entry = client.get("/api/darkhelix/downloads").json()["items"][0]["entry"]
    assert entry["url_state"] == {"ok": True, "status": "200", "bytes": 60806405195,
                                  "final_url": "https://example.invalid/gtdbtk_data.tar.gz"}
    assert entry["state"] == "missing"


def test_a_file_already_the_full_size_reads_as_complete(monkeypatch):
    """The Mash sketch was exactly this: 754,115,096 bytes on disk and the same
    on the server, so the item is blocked on wire-in and a download card would
    re-pull a finished file."""
    _stub_fleet(monkeypatch, head=HEAD_OK, dest="60806405195")
    _no_board(monkeypatch)
    assert client.get("/api/darkhelix/downloads").json()["items"][0]["entry"]["state"] == "complete"


def test_a_short_file_reads_as_partial(monkeypatch):
    _stub_fleet(monkeypatch, head=HEAD_OK, dest="12345")
    _no_board(monkeypatch)
    assert client.get("/api/darkhelix/downloads").json()["items"][0]["entry"]["state"] == "partial"


def test_an_unpacked_archive_is_never_called_complete(monkeypatch):
    """A tree is a different size from its tarball, so the byte comparison says
    nothing and claiming otherwise would be a guess."""
    monkeypatch.setattr(srv, "_download_catalogue",
                        lambda: [dict(ENTRY, archive="tar.gz")])
    _stub_fleet(monkeypatch, head=HEAD_OK, dest="60806405195")
    _no_board(monkeypatch)
    assert client.get("/api/darkhelix/downloads").json()["items"][0]["entry"]["state"] == "present"


# --- filing -----------------------------------------------------------------

def test_refuses_to_file_a_card_for_an_item_with_no_url(monkeypatch):
    _stub_fleet(monkeypatch, head=HEAD_OK, dest="ABSENT")
    r = client.post("/api/darkhelix/downloads/schedule", json={"item_id": "todo-1"})
    assert r.status_code == 400 and "downloads.yaml" in r.json()["error"]


def test_refuses_to_file_a_card_carrying_a_dead_url(monkeypatch):
    """A card with a 404 in its body costs somebody an afternoon."""
    _stub_fleet(monkeypatch, head=HEAD_404, dest="ABSENT")
    seen = []
    async def kanban(cmd):
        seen.append(cmd)
        return 0, "{}"
    monkeypatch.setattr(srv, "_kanban_ssh", kanban)
    r = client.post("/api/darkhelix/downloads/schedule", json={"item_id": "todo-0"})
    assert r.status_code == 400 and "404" in r.json()["error"]
    assert not seen


def test_files_then_parks_the_card_so_it_cannot_take_the_seat(monkeypatch):
    _stub_fleet(monkeypatch, head=HEAD_OK, dest="ABSENT")
    seen = []
    async def kanban(cmd):
        seen.append(cmd)
        return 0, '{"id": "t_74981c9f"}' if "kanban create" in cmd else "scheduled"
    monkeypatch.setattr(srv, "_kanban_ssh", kanban)
    j = client.post("/api/darkhelix/downloads/schedule", json={"item_id": "todo-0"}).json()
    assert j["ok"] and j["task_id"] == "t_74981c9f" and j["scheduled"] is True
    assert "--assignee bioinformatics" in seen[0]
    # The park is a second call because `create` cannot open a card in
    # `scheduled`, and the card must never be left dispatchable in between.
    assert "kanban schedule t_74981c9f" in seen[1]
