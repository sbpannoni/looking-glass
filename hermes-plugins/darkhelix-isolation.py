"""Give every DARKHELIX card a real git worktree before its worker starts.

WHY THIS EXISTS
---------------
Isolation used to be created in exactly one place: the Looking Glass HUD's
SUBMIT WORK button (`POST /api/kanban/create` on CT112), which creates a git
worktree on snarf and writes a `[dispatch-target]` block into the card body.

Every other way a card reaches this board produced a card with neither. The
big one is `hermes kanban decompose`: it fans a triage card into a dependency
graph via `kb.decompose_triage_task()`, and children are created with only
title/body/assignee/parents -- no workspace, no branch. Those children then
ran with no isolated tree, so workers `git checkout`-ed inside the ONE shared
/ssdpool/DARKHELIX checkout. (On snarf /home/sam/code/projects/DARKHELIX is
the SAME checkout, which made it look like a second repo and hid the damage.)

Patching the decomposer would have fixed one of several holes. `hermes kanban
create` by hand, the dashboard, and `swarm` all have the same gap. So the
provisioning call is made HERE instead, from `kanban_task_claimed` -- the one
choke point every card passes through no matter who created it.

WHY THIS HOOK SPECIFICALLY
--------------------------
`kanban_task_claimed` fires in the DISPATCHER process, after the claim commits
and immediately before the worker subprocess spawns (hermes_cli/kanban_db.py,
`claim_task` -> `_fire_kanban_lifecycle_hook`). Because `invoke_hook` calls
callbacks synchronously, this returns before the worker exists -- so the
worker's first read of the card body already names its worktree. That
ordering is the whole design; a background thread here would race the worker
and lose.

WHAT IT CANNOT DO
-----------------
Kanban lifecycle hooks are observers: return values are ignored and any
exception is swallowed, so this cannot veto a dispatch. When provisioning
fails, CT112 writes `worktree: NONE -- ISOLATION FAILED` into the card's
dispatch-target instead, and the worker's own `execution-engine-dispatch`
skill (step 1) refuses to work a card without a usable one. The stop is
enforced on the worker side, not here.

Idempotent and safe to fire on every claim: CT112 skips cards outside
DARKHELIX's lineage, and no-ops when a card already has a worktree that
really exists on disk.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# The gateway logs to the systemd USER journal, which is not persisted on
# CT111 (`journalctl --user` reports "No journal files were found"), so a
# provisioning decision made in the dispatcher would leave no trace anywhere.
# Isolation is a safety property; "did this card get a worktree, and what was
# it cut from" has to be answerable after the fact.
_AUDIT_LOG = Path.home() / ".hermes" / "logs" / "darkhelix-isolation.log"


def _audit(line: str) -> None:
    try:
        _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        with _AUDIT_LOG.open("a") as fh:
            fh.write(f"{stamp} {line}\n")
    except Exception:
        pass


# Runtime ceiling for a card that has none.
#
# `hermes kanban create --max-runtime` sets this, and every card filed by hand
# here does. The DECOMPOSER sets nothing, so its children run uncapped: on
# 2026-08-28 t_7c57772b ran 1h01m, finished the work the card asked for at
# ~45m, then carried on regenerating testruns nobody had asked about, and only
# stopped because a human noticed.
#
# 90 minutes is chosen against the evidence rather than picked round: an engine
# round-trip is 3-5 minutes, the longest genuinely useful run observed was
# ~60 minutes, and the wedged runs that started all of this sat at 2h+
# producing nothing. It is a ceiling on pathology, not a budget for work.
#
# Cheap to be wrong about: exceeding it makes the dispatcher REQUEUE the card
# (a `timed_out` outcome), so an overrun costs a retry, not the work.
_DEFAULT_MAX_RUNTIME_SECONDS = 5400

_CONFIG_PATH = Path(__file__).with_name("config.json")
_DEFAULT_URL = "https://192.168.1.241"

# Bounded, but generous: provisioning does a `git fetch` and a worktree add
# over ssh to snarf. This call is deliberately BLOCKING -- the dispatcher
# waiting a few seconds is the point, because the worker must not start
# before its tree exists. The timeout only stops an unreachable HUD from
# stalling the dispatcher indefinitely.
_TIMEOUT_SECONDS = 60


def _config_value(key: str, default):
    try:
        return json.loads(_CONFIG_PATH.read_text()).get(key, default)
    except Exception:
        return default


def _config() -> tuple[str, str]:
    """(base_url, token). Env wins; the file is the persistent fallback."""
    url = os.environ.get("LOOKING_GLASS_URL", "")
    token = os.environ.get("LOOKING_GLASS_HUD_TOKEN", "")
    if not (url and token):
        try:
            data = json.loads(_CONFIG_PATH.read_text())
        except Exception:
            data = {}
        url = url or data.get("url") or _DEFAULT_URL
        token = token or data.get("token") or ""
    return url.rstrip("/"), token


def _apply_default_runtime_cap(task_id: str) -> None:
    """Give a card a runtime ceiling if it has none.

    Set at claim time rather than creation because the decomposer creates its
    children directly in the DB and there is no config default to hook. The
    dispatcher's timeout sweep reads the task's live ``max_runtime_seconds``
    on every tick, so a value written here covers the run that is starting.

    Never lowers an existing cap: a card that asked for longer asked on
    purpose."""
    try:
        cap = int(_config_value("max_runtime_seconds", _DEFAULT_MAX_RUNTIME_SECONDS))
    except (TypeError, ValueError):
        cap = _DEFAULT_MAX_RUNTIME_SECONDS
    if cap <= 0:
        return
    try:
        from hermes_cli import kanban_db as kb
        with kb.connect_closing() as conn:
            row = conn.execute(
                "SELECT max_runtime_seconds FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None or row[0] is not None:
                return
            conn.execute(
                "UPDATE tasks SET max_runtime_seconds = ? "
                "WHERE id = ? AND max_runtime_seconds IS NULL",
                (cap, task_id),
            )
            conn.commit()
        logger.info("darkhelix-isolation: %s had no runtime cap; set %ds",
                    task_id, cap)
        _audit(f"{task_id} CAP set max_runtime={cap}s (was unset)")
    except Exception as exc:
        logger.warning("darkhelix-isolation: could not cap %s: %s", task_id, exc)


def _on_task_claimed(**kwargs) -> None:
    task_id = kwargs.get("task_id")
    if not task_id:
        return
    # Independent of provisioning: an uncapped card is a problem whether or
    # not it is DARKHELIX work.
    _apply_default_runtime_cap(task_id)
    url, token = _config()
    if not token:
        logger.warning(
            "darkhelix-isolation: no HUD token configured (%s or "
            "$LOOKING_GLASS_HUD_TOKEN); task %s claimed WITHOUT isolation",
            _CONFIG_PATH, task_id,
        )
        _audit(f"{task_id} NO-TOKEN claimed without isolation")
        return
    try:
        import requests
        import urllib3

        # The HUD serves a self-signed cert on the LAN. Verification is off
        # for that reason and that reason only; the bearer token is what
        # authenticates the call.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.post(
            f"{url}/api/kanban/provision",
            json={"task_id": task_id},
            headers={"x-looking-glass-token": token},
            timeout=_TIMEOUT_SECONDS,
            verify=False,
        )
        body = r.json()
    except Exception as exc:
        # Swallowed by the hook runner anyway; log it so the dispatcher's
        # journal says why a card went out unisolated.
        logger.warning(
            "darkhelix-isolation: provisioning %s failed: %s: %s",
            task_id, type(exc).__name__, exc,
        )
        _audit(f"{task_id} ERROR {type(exc).__name__}: {exc}")
        return

    if body.get("skipped"):
        logger.debug("darkhelix-isolation: %s skipped (%s)", task_id, body["skipped"])
        _audit(f"{task_id} SKIP {body['skipped']}")
    elif body.get("already"):
        logger.info("darkhelix-isolation: %s already isolated at %s",
                    task_id, body.get("worktree"))
        _audit(f"{task_id} ALREADY {body.get('worktree')}")
    elif body.get("isolated"):
        logger.info("darkhelix-isolation: %s isolated at %s (branch %s, base %s)",
                    task_id, body.get("worktree"), body.get("branch"),
                    body.get("base"))
        _audit(f"{task_id} ISOLATED branch={body.get('branch')} "
               f"base={body.get('base')} merged={body.get('merged')} "
               f"unmerged={body.get('unmerged')} missing={body.get('missing')}")
        for miss in body.get("missing") or []:
            logger.info("darkhelix-isolation: %s parent branch absent: %s",
                        task_id, miss)
        for bad in body.get("unmerged") or []:
            logger.warning("darkhelix-isolation: %s parent branch CONFLICTED, "
                           "not merged: %s", task_id, bad)
    else:
        logger.warning("darkhelix-isolation: %s NOT isolated: %s",
                       task_id, body.get("error") or body)
        _audit(f"{task_id} NOT-ISOLATED {body.get('error') or body}")


def register(ctx) -> None:
    ctx.register_hook("kanban_task_claimed", _on_task_claimed)
