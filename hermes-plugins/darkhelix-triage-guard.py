"""Keep the auto-decomposer's hands off a card the loop-breaker parked.

WHY THIS EXISTS
---------------
Two vendor behaviours collide, and on this install they collide backwards.

`block_task` (hermes_cli/kanban_db.py) treats the Nth re-block for the same
cause as a loop -- `BLOCK_RECURRENCE_LIMIT`, 2 -- and instead of blocking
again routes the card to `triage`. Its own comment states the intent:

    # Loop detected - stop letting the unblocker spin this task. Route
    # to triage for a human-in-the-loop decision instead of blocked.

That reasoning assumes `blocked` is where a cron spins and `triage` is where a
human looks. Here it is the other way round:

  * NOTHING auto-drains `blocked`. There is no unblock cron on CT111
    (`hermes cron list` is empty; the only crontab entry is
    reclaim_context_watcher). A blocked card waits for a person, via the
    HUD's BLOCKED panel and `POST /api/kanban/unblock`.
  * `triage` is drained BY A MACHINE, every 60s. The gateway's
    `_auto_decompose_tick` (gateway/kanban_watchers.py) lists every card in
    `triage` and hands each to `decompose_task`, which guards only on
    `status == 'triage'` -- it never checks whether the card was already
    decomposed.

So the loop-breaker parks a card for a human and the decomposer fans it out
again before any human sees it.

WHAT IT COST
------------
t_d17fef80, 2026-08-29. The engine could not run the test gate (the
.venv-dev symlink did not resolve inside the container -- since fixed in
dispatch_task.py by mounting the primary checkout read-only at the same
path). The worker blocked on it twice, the loop-breaker fired at 20:44 and
routed the card to `triage`, and the auto-decomposer picked it up at 20:45:

    [20:44] block_loop_detected {"recurrences": 2, "limit": 2}
    [20:45] decomposed {"child_ids": ["t_b53fa5cd", "t_e8465c45",
                                      "t_ca4d6f36", "t_b3e858ee"]}

Those four children re-asked the four questions the 12:47 fan-out had already
answered: audit the SELECT_REFS table, verify 263.fna, verify 234.fna, decide
patch-vs-rebuild. 2h37m of worker time for no new information -- and the two
waves returned OPPOSITE answers to the patch-vs-rebuild question (wave 1
PATCH, wave 2 REBUILD) with nothing on the board to reconcile them.

WHAT THIS DOES
--------------
On `kanban_task_blocked`, if the transition left the card in `triage` -- which
only the loop-breaker branch of `block_task` does -- put it in `blocked`
instead and say why in a comment. That is the vendor's stated intent
("a human-in-the-loop decision") expressed in the status that actually means
that on this install.

It does NOT touch `auto_decompose`. A genuinely new card still fans out on the
next tick, which is the behaviour that produced this board's useful work.

WHAT IT CANNOT DO
-----------------
Kanban lifecycle hooks are observers: return values are ignored and exceptions
are swallowed (`_fire_kanban_lifecycle_hook`). This cannot veto the transition,
only correct it afterwards. That is sufficient here because the hook fires
synchronously inside `block_task` after its write txn commits, while the
decomposer runs on a 60s tick -- the correction lands in the same second, the
fan-out is a minute away.

WHERE IT MUST BE INSTALLED
--------------------------
`_fire_kanban_lifecycle_hook` resolves plugins from the ACTIVE HERMES_HOME,
and a kanban worker runs with its assignee profile as home. `block_task` is
called by the worker, so a copy visible only from the root home would never
fire. Install for the root home AND every profile that works cards -- see the
README's "Plugins are discovered per-HERMES_HOME" trap, which cost a full
debug cycle to find the first time.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# The gateway and the workers log to the systemd USER journal, which is not
# persisted on CT111 (`journalctl --user` reports "No journal files were
# found"). A status correction made by a plugin has to be answerable after the
# fact or it is indistinguishable from the board misbehaving.
_AUDIT_LOG = Path.home() / ".hermes" / "logs" / "darkhelix-triage-guard.log"

_COMMENT = (
    "## Parked for a human by darkhelix-triage-guard\n\n"
    "The unblock-loop breaker fired (`block_kind={kind}`, "
    "recurrences={recurrences}) and routed this card to `triage` for a "
    "human-in-the-loop decision.\n\n"
    "On this install `triage` is drained by the gateway auto-decomposer "
    "every 60s, and `decompose_task` does not check whether a card was "
    "already decomposed -- so a card parked there gets fanned out again "
    "before anyone sees it. This card {children_note}\n\n"
    "Moved to `blocked` instead, which is where a card waits for a person "
    "here. Nothing auto-drains it.\n\n"
    "**Last block reason:** {reason}"
)


def _audit(line: str) -> None:
    try:
        _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOG.open("a") as fh:
            fh.write(line.rstrip() + "\n")
    except Exception:  # pragma: no cover - auditing must never break a hook
        logger.debug("darkhelix-triage-guard: could not write audit log")


def _on_task_blocked(task_id: str, board: str | None = None,
                     reason: str | None = None, **_fields) -> None:
    try:
        from hermes_cli import kanban_db as kb
    except Exception as exc:  # pragma: no cover
        logger.debug("darkhelix-triage-guard: kanban_db unavailable (%s)", exc)
        return

    try:
        with kb.connect_closing(board=board) as conn:
            task = kb.get_task(conn, task_id)
            # block_task lands a card in `triage` from exactly one branch: the
            # loop-breaker. Any other status means an ordinary block, which is
            # already where it should be.
            if task is None or task.status != "triage":
                return

            row = conn.execute(
                "SELECT block_kind, block_recurrences FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            kind = (row["block_kind"] if row else None) or "untyped"
            recurrences = (row["block_recurrences"] if row else None) or 0
            kids = kb.child_ids(conn, task_id)

            with kb.write_txn(conn):
                cur = conn.execute(
                    "UPDATE tasks SET status = 'blocked' "
                    " WHERE id = ? AND status = 'triage'",
                    (task_id,),
                )
                if cur.rowcount != 1:
                    # Something else moved it between the read and the write.
                    # Leave it alone rather than fight another writer.
                    return
                kb._append_event(
                    conn, task_id, "status",
                    {
                        "from": "triage", "to": "blocked",
                        "by": "darkhelix-triage-guard",
                        "block_kind": kind,
                        "recurrences": recurrences,
                        "children": len(kids),
                    },
                )

            children_note = (
                f"already has {len(kids)} child card(s) "
                f"({', '.join(kids)}), so a second fan-out would have "
                "duplicated work that is already on the board."
                if kids else
                "has no children yet, but a machine draining the column that "
                "means 'a human must decide' is the same defect either way."
            )
            kb.add_comment(
                conn, task_id, "darkhelix-triage-guard",
                _COMMENT.format(
                    kind=kind, recurrences=recurrences,
                    children_note=children_note,
                    reason=(reason or "(none recorded)").strip()[:600],
                ),
            )
    except Exception:
        logger.exception("darkhelix-triage-guard: %s not parked", task_id)
        _audit(f"{task_id} ERROR while parking")
        return

    logger.info("darkhelix-triage-guard: %s triage -> blocked "
                "(kind=%s recurrences=%s children=%d)",
                task_id, kind, recurrences, len(kids))
    _audit(f"{task_id} PARKED triage->blocked kind={kind} "
           f"recurrences={recurrences} children={len(kids)}")


def register(ctx) -> None:
    ctx.register_hook("kanban_task_blocked", _on_task_blocked)
