"""The download backlog: verified URLs, and cards that cannot take the seat.

Nine TODO.md items are blocked on a database or a binary. The value here is
entirely in what the endpoint refuses to claim -- a URL it has not checked, a
download that is already on disk, a card filed for either -- so those are the
tests.
"""
import os
import shlex
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


# --- the "already running" guard --------------------------------------------
#
# The guard used to be `pgrep -af <url>`, and it matched the remote `bash -c`
# that ssh was running it in -- the URL is in that shell's own argv, and pgrep
# excludes its own pid but not its parent's. So every click reported "already
# running on snarf" and no wget was ever reached. These tests run the probe
# through a real shell against a process table that contains the calling shell,
# because that is the only shape in which the bug is visible.

FAKE_PGREP = r'''#!/usr/bin/env python3
"""Enough of pgrep to tell the two probes apart: -f matches the pattern as a
regex against the whole command line, plain matches the process name."""
import os, re, sys
args = sys.argv[1:]
flags = "".join(a[1:] for a in args if a.startswith("-"))
pattern = next((a for a in args if not a.startswith("-")), "")
hits = []
for line in os.environ.get("FAKE_PROCS", "").splitlines():
    if not line.strip():
        continue
    pid, _, cmdline = line.partition(" ")
    if "f" in flags:
        ok = re.search(pattern, cmdline) is not None
    else:
        ok = os.path.basename(cmdline.split()[0]) == pattern
    if ok:
        hits.append(f"{pid} {cmdline}" if "a" in flags else pid)
print("\n".join(hits))
sys.exit(0 if hits else 1)
'''

ZENODO = "https://zenodo.org/records/14192463/files/DATABASES.tar.gz?download=1"


@pytest.fixture
def shell(tmp_path):
    """Run a probe the way ssh does: in a `bash -c` whose argv holds the URL."""
    import subprocess
    fake = tmp_path / "pgrep"
    fake.write_text(FAKE_PGREP)
    fake.chmod(0o755)

    def run(probe, *, writing=None, extra=None):
        procs = [f"31337 bash -c {probe}"]          # the shell asking the question
        if writing:
            procs.append(f"4242 wget -c -O {writing} https://host.invalid/f")
        if extra:
            procs.append(extra)
        env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}",
                   FAKE_PROCS="\n".join(procs))
        return subprocess.run(["bash", "-c", probe], env=env,
                              capture_output=True).returncode == 0

    return run


def test_the_probe_ignores_the_shell_that_is_running_it(shell):
    assert shell(srv._wget_writing_probe(ENTRY)) is False
    # The old form, for contrast: nothing is downloading and it still says yes.
    assert shell(f"pgrep -af {shlex.quote(ENTRY['url'])} >/dev/null") is True


def test_the_probe_still_sees_a_wget_writing_the_file(shell):
    assert shell(srv._wget_writing_probe(ENTRY),
                 writing=srv._entry_target(ENTRY)) is True


def test_the_probe_sees_a_legacy_dash_p_download_of_the_same_file(shell):
    """`-P <dest> <url>` writes the same target by another spelling. A pull
    started before this file used -O must still block a second one, or the
    click lands a competing wget on a half-finished archive."""
    probe = srv._wget_writing_probe(ENTRY)
    assert shell(probe) is False          # sanity: no wget in the table at all
    assert shell(probe,
                 extra=f"4242 wget -c -P {ENTRY['dest']} {ENTRY['url']}") is True


def test_two_entries_sharing_a_url_do_not_block_each_other(shell):
    """The Zenodo archive unblocks two TODO items and is listed twice, with
    different destinations. Keyed on the url, starting one refused the other
    as "already running" while its destination sat empty."""
    a = dict(ENTRY, id="pathofact-db", url=ZENODO,
             dest="/ssdpool/DARKHELIX/database/pathofact")
    b = dict(ENTRY, id="pathofact2-db", url=ZENODO,
             dest="/ssdpool/DARKHELIX/database/toxin_hmm/pathofact2")
    assert shell(srv._wget_writing_probe(a), writing=srv._entry_target(a)) is True
    assert shell(srv._wget_writing_probe(b), writing=srv._entry_target(a)) is False


def test_both_endpoints_ask_the_question_the_same_way(monkeypatch):
    """run and progress drifting apart is how one of them regresses alone."""
    seen = []

    async def fake(host, cmd):
        seen.append(cmd)
        return 0, "STARTED 4242" if "wget" in cmd else "IDLE"

    monkeypatch.setattr(srv, "_fleet_ssh", fake)
    client.post("/api/darkhelix/downloads/run", json={"entry_id": "gtdbtk"})
    client.get("/api/darkhelix/downloads/progress", params={"entry_id": "gtdbtk"})
    probe = srv._wget_writing_probe(ENTRY)
    assert [c for c in seen if probe in c] and all("pgrep -af" not in c for c in seen)


# --- where the bytes actually land ------------------------------------------

CONO = {"id": "conoserver-protein", "match": "ConoServer",
        "name": "ConoServer conopeptide protein sequences",
        "url": "https://www.conoserver.org/download/conoserver_protein.fa.gz",
        "dest": "/ssdpool/DARKHELIX/database/conoserver"}


def test_the_filename_comes_from_the_url_path(monkeypatch):
    assert (srv._entry_target(CONO)
            == "/ssdpool/DARKHELIX/database/conoserver/conoserver_protein.fa.gz")


def test_a_query_string_never_reaches_the_filename():
    """wget names a file from the whole URL. Verified on snarf: given
    `...DATABASES.tar.gz?download=1` it writes that literally, query and all,
    and the wire-in step then cannot find what it is looking for."""
    e = {"url": ZENODO, "dest": "/ssdpool/DARKHELIX/database/pathofact"}
    assert srv._entry_target(e) == "/ssdpool/DARKHELIX/database/pathofact/DATABASES.tar.gz"


def test_an_explicit_filename_wins_over_the_url():
    e = dict(CONO, filename="conopeptides.fa.gz")
    assert srv._entry_target(e).endswith("/conopeptides.fa.gz")


def test_a_plain_download_is_measured_as_a_file_not_a_directory():
    """du -sb on the directory adds the 4096-byte inode, so the total never
    equals Content-Length and the entry reads `partial` for ever."""
    assert srv._entry_probe_path(CONO) == srv._entry_target(CONO)
    archive = dict(CONO, archive="tar.gz")
    assert srv._entry_probe_path(archive) == archive["dest"]


# --- the endpoint must not own the transfer ---------------------------------

def _shape(background_list: bool) -> str:
    """The run command's shape, with a sleep standing in for wget."""
    tail = "nohup sleep 5 >> /dev/null 2>&1 < /dev/null & echo STARTED $!"
    return (f"mkdir -p /tmp && {tail}" if background_list
            else f"mkdir -p /tmp || {{ echo MKDIR_FAILED; exit 1; }}; {tail}")


def test_starting_a_download_does_not_hold_the_pipes_open():
    """capture_output gives the child a pipe, exactly as ssh gives it a channel.
    Backgrounding the whole `mkdir && wget` list forks a subshell that keeps
    that pipe open for the life of the transfer -- which is how one click sat
    in asyncssh for the duration of a 110 GB download."""
    import subprocess
    r = subprocess.run(["bash", "-c", _shape(background_list=False)],
                       capture_output=True, timeout=3, text=True)
    assert r.stdout.startswith("STARTED")

    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(["bash", "-c", _shape(background_list=True)],
                       capture_output=True, timeout=3)


def test_run_names_its_output_and_backgrounds_only_the_wget(monkeypatch):
    seen = []

    async def fake(host, cmd):
        seen.append(cmd)
        return 0, "STARTED 4242"

    monkeypatch.setattr(srv, "_fleet_ssh", fake)
    client.post("/api/darkhelix/downloads/run", json={"entry_id": "gtdbtk"})
    cmd = seen[0]
    assert f"-O {shlex.quote(srv._entry_target(ENTRY))}" in cmd
    # -P appears only inside the legacy grep pattern, never as wget's own flag.
    assert "wget -c -P" not in cmd
    # The mkdir must not be welded to the wget by `&&`, or the whole list ends
    # up in the background together.
    assert "MKDIR_FAILED" in cmd and "&& nohup" not in cmd
