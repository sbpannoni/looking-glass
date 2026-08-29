"""dispatch_to_engine — the whole engine round-trip, as code.

WHY THIS IS A TOOL AND NOT A SKILL
----------------------------------
`execution-engine-dispatch/SKILL.md` described a four-step procedure with
ZERO judgement calls in it: build a command, run it, parse result.json,
report to kanban. Every branch is `if result["status"] == "done"`. Handing
that to a model gave it wide latitude and a trivial mandate, and it filled
the gap — twice it decided the engine was too slow and edited files itself
(runs 4 and 6, 2026-08-28), and twice before that it exited without calling
kanban_complete at all ("protocol violation", runs 1 and 2).

Prose in a skill is a suggestion. This is code. Everything the skill used to
spell out as a warning is now unreachable rather than merely discouraged:

  * `--repo-path` is taken from the card's `worktree:` line, never `repo:`.
    Passing the repo root makes the engine cut its branch from master's HEAD
    and silently discard everything the card inherited from its parents.
  * `--branch-name` is minted fresh per attempt. `git worktree add -b` fails
    on an existing branch, and the card's own branch always exists.
  * `--description` is required; the card body minus its dispatch-target.
  * the script is mode 644, so it runs via `python3 <path>`, never directly.
  * on success the patch is attached and the card completed HERE, which is
    what the dispatcher's protocol requires and what a model kept forgetting.

WHAT IS DELIBERATELY *NOT* IN HERE
----------------------------------
Diagnosis. On failure this returns the engine's verdict verbatim and does
nothing else — it does not block the card and does not retry. Deciding
whether a red test gate means "the edit was wrong" or "the spec was
incomplete" is the one genuinely reasoned call in this flow, and it is the
model's job. That distinction is not academic: the 2026-08-28 08:20 attempt
failed its test gate because the task never mentioned the test file that
asserts the old behaviour. Re-running the same description would have failed
identically, forever.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from typing import Any, Dict

SNARF = "sam@192.168.1.239"
SNARF_KEY = "/root/.hermes/profiles/coder/snarf_key"
ENGINE = "/ssdpool/coder-engine/pipeline/dispatch_task.py"
# One root for everything a card produces on snarf:
#   /ssdpool/agent-work/<task_id>/worktree/     the card's git worktree
#   /ssdpool/agent-work/<task_id>/attempts/     one dir per engine attempt
# Attempts are counted from this card's own directory, so the number means
# "attempts on THIS card" rather than "entries matching a prefix in a shared
# pile".
AGENT_WORK = "/ssdpool/agent-work"
LOCAL_PATCHES = "/root/.hermes/kanban/engine-patches"
# Absolute, not PATH-resolved. This binary is what marks the card complete;
# a worker spawned with a trimmed PATH would fail at exactly the step whose
# omission produced the "exited without calling kanban_complete" protocol
# violations that killed runs 1 and 2.
HERMES = "/usr/local/bin/hermes"

# The container itself is capped at 1500s by dispatch_task.py; allow headroom
# for image pull, worktree setup and teardown, then give up rather than hang
# a worker session forever.
SSH_TIMEOUT = 2100

# Attempts are bounded here, not by asking the model to keep count. Past this
# the answer is not another run.
MAX_ATTEMPTS = 3

_TASK_ID_RE = re.compile(r"^t_[0-9a-f]{6,}$")
_DT_RE = re.compile(r"\[dispatch-target\](?P<inner>.*?)\[/dispatch-target\]", re.S)
_DT_NOTES_SEP = "--- notes ---"


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=str)


def _err(msg: str, **extra) -> str:
    return _json({"success": False, "error": msg, **extra})


def _ssh(cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", SNARF_KEY, "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", SNARF, cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _dispatch_target(body: str) -> Dict[str, str]:
    """Parse the block the provisioner wrote. Stops at the prose separator —
    several note lines contain a colon and would otherwise parse as fields."""
    m = _DT_RE.search(body or "")
    if not m:
        return {}
    fields: Dict[str, str] = {}
    for line in m.group("inner").splitlines():
        if line.strip() == _DT_NOTES_SEP:
            break
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip().lower()] = v.strip()
    return fields


def _card(task_id: str):
    from hermes_cli import kanban_db as kb
    with kb.connect_closing() as conn:
        return kb.get_task(conn, task_id)


def handle_dispatch_to_engine(args: Dict[str, Any], **_kw) -> str:
    task_id = (args.get("task_id") or "").strip()
    if not _TASK_ID_RE.match(task_id):
        return _err("bad task id")

    task = _card(task_id)
    if task is None:
        return _err(f"unknown task {task_id}")

    body = task.body or ""
    dt = _dispatch_target(body)
    worktree = dt.get("worktree", "")
    if not worktree or "ISOLATION FAILED" in worktree or worktree == "NONE":
        return _err(
            "this card has no usable worktree — it was never isolated. Do NOT "
            "work it in the shared checkout. Block it with this reason.",
            dispatch_target=dt or None)

    probe = _ssh(f"test -d {shlex.quote(worktree)}/.git -o -f {shlex.quote(worktree)}/.git")
    if probe.returncode != 0:
        return _err(f"the card names worktree {worktree} but it is not on disk; "
                    "re-provision it before dispatching")

    # Attempt number, counted from what is actually on disk rather than from
    # anything the model tracks.
    attempts_dir = f"{AGENT_WORK}/{task_id}/attempts"
    ls = _ssh(f"ls -1d {shlex.quote(attempts_dir)}/engine-* 2>/dev/null | wc -l")
    try:
        prior = int((ls.stdout or "0").strip() or 0)
    except ValueError:
        prior = 0
    if prior >= MAX_ATTEMPTS:
        return _err(
            f"{prior} engine attempts already made on this card (limit "
            f"{MAX_ATTEMPTS}). Another identical run will not help. Block the "
            "card and say what is actually unresolved.",
            attempts=prior)

    n = prior + 1
    branch = f"hermes/{task_id}-engine-{n}"
    out_dir = f"{attempts_dir}/engine-{n}/output"

    # The spec the engine works from: the card, minus the machinery block.
    description = (args.get("amended_description") or "").strip()
    if not description:
        stripped = _DT_RE.sub("", body).strip()
        description = f"{task.title}\n\n{stripped}".strip()
    if not description:
        return _err("card has no body and no amended_description to work from")

    cmd = (
        f"python3 {shlex.quote(ENGINE)} "
        f"--task-id {shlex.quote(task_id)} "
        f"--repo-path {shlex.quote(worktree)} "          # worktree, NEVER repo
        f"--branch-name {shlex.quote(branch)} "
        f"--description {shlex.quote(description)} "
        f"--output-dir {shlex.quote(out_dir)}"
    )
    test_command = (args.get("test_command") or "").strip()
    if test_command:
        cmd += f" --test-command {shlex.quote(test_command)}"

    if args.get("dry_run"):
        # Shows exactly what would run and what it was derived from, without
        # spending an engine attempt. The attempt counter is NOT advanced.
        return _json({
            "success": True, "dry_run": True,
            "attempt_would_be": n, "attempts_remaining": MAX_ATTEMPTS - prior,
            "worktree": worktree, "branch": branch, "output_dir": out_dir,
            "command": cmd,
            "description_chars": len(description),
            "description_head": description[:300],
        })

    try:
        proc = _ssh(cmd, timeout=SSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return _err(f"engine exceeded {SSH_TIMEOUT}s and was abandoned",
                    attempt=n, branch=branch)

    # dispatch_task.py prints result.json to stdout and exits 0 iff done.
    result: Dict[str, Any] = {}
    try:
        result = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return _err("engine produced no parseable result",
                    attempt=n, rc=proc.returncode,
                    stdout=(proc.stdout or "")[-1500:],
                    stderr=(proc.stderr or "")[-1500:])

    if result.get("status") != "done":
        # Verdict returned as-is. Not blocked, not retried — that is the
        # model's call, and it is the only one in this flow.
        return _json({
            "success": False,
            "engine_status": result.get("status"),
            "attempt": n,
            "attempts_remaining": MAX_ATTEMPTS - n,
            "branch": branch,
            "error": result.get("error"),
            "next": (
                "Decide WHY this failed before doing anything else. A red test "
                "gate usually means the SPEC was incomplete (e.g. it never "
                "mentioned a test file that asserts the old behaviour), not "
                "that the edit was wrong. If so, call dispatch_to_engine again "
                "with amended_description covering what was missing. If the "
                "spec was right and the code is genuinely hard, block the card "
                "with the specific reason. Do NOT edit files yourself."
            ),
        })

    # Success is fully deterministic from here: pull the patch back, attach it,
    # complete the card. Runs 1 and 2 both died because a model reached this
    # point and simply never called kanban_complete.
    # result.json reports patch_path as the container sees it ("/output/x.patch"),
    # because the engine writes it from inside the bind mount. That path does not
    # exist on snarf, so scp'ing it verbatim silently found nothing and the card
    # completed with no patch attached (first real run, 2026-08-28). Map it back
    # onto the host output dir this call actually asked for.
    patch_remote = result.get("patch_path")
    if patch_remote and patch_remote.startswith("/output/"):
        patch_remote = out_dir.rstrip("/") + patch_remote[len("/output"):]
    attached = None
    if patch_remote:
        os.makedirs(LOCAL_PATCHES, exist_ok=True)
        local = os.path.join(LOCAL_PATCHES, f"{task_id}-engine-{n}.patch")
        scp = subprocess.run(
            ["scp", "-i", SNARF_KEY, "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no",
             f"{SNARF}:{patch_remote}", local],
            capture_output=True, text=True, timeout=120)
        if scp.returncode == 0:
            att = subprocess.run([HERMES, "kanban", "attach", task_id, local],
                                 capture_output=True, text=True, timeout=120)
            attached = local if att.returncode == 0 else f"attach failed: {att.stderr[-300:]}"
        else:
            attached = f"scp failed: {scp.stderr[-300:]}"

    patch_ok = bool(attached) and not str(attached).startswith(("scp failed", "attach failed"))
    summary = (f"engine attempt {n} passed its test gate on {branch}; "
               f"patch {'attached' if patch_ok else 'NOT attached'}")
    comp = subprocess.run([HERMES, "kanban", "complete", task_id, "--summary", summary],
                          capture_output=True, text=True, timeout=120)
    return _json({
        "success": True,
        "engine_status": "done",
        "attempt": n,
        "branch": branch,
        "patch": attached,
        "patch_attached": patch_ok,
        "card_completed": comp.returncode == 0,
        "complete_error": None if comp.returncode == 0 else comp.stderr[-300:],
        "next": ("Nothing further. The card is complete and the branch holds a "
                 "reviewable commit. Do not merge — a human does that."),
    })


DISPATCH_TO_ENGINE_SCHEMA: Dict[str, Any] = {
    "name": "dispatch_to_engine",
    "description": (
        "Run one DARKHELIX kanban card through the LangGraph+Aider execution "
        "engine on snarf and report the outcome. This is the ONLY sanctioned "
        "way to make code changes for a card — never edit files yourself. "
        "Handles the whole round trip: reads the card's isolated worktree from "
        "its dispatch-target, mints a fresh branch, runs the engine, and on "
        "success attaches the patch and completes the card. On failure it "
        "returns the engine's verdict for you to diagnose."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The kanban task id, e.g. t_765ca5ad.",
            },
            "amended_description": {
                "type": "string",
                "description": (
                    "Optional. Replaces the card body as the engine's spec. Use "
                    "on a RETRY after diagnosing why the first attempt failed — "
                    "e.g. adding the test file the original spec omitted."
                ),
            },
            "test_command": {
                "type": "string",
                "description": (
                    "Optional. Overrides the engine's own deterministic test "
                    "discovery. Leave unset unless you know discovery picks the "
                    "wrong suite."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "Optional. Report the exact command and inputs without "
                    "running the engine or consuming an attempt."
                ),
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------- registration
def register(ctx) -> None:
    ctx.register_tool(
        name="dispatch_to_engine",
        toolset="darkhelix",
        schema=DISPATCH_TO_ENGINE_SCHEMA,
        handler=handle_dispatch_to_engine,
        description="Run a DARKHELIX card through the execution engine on snarf.",
        emoji="⚙️",
    )
