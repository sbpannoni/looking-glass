# Task isolation: how a DARKHELIX card gets a git worktree

Every kanban card that touches DARKHELIX must work in its own git worktree on
snarf. This document says how that is guaranteed, why it is guaranteed at the
point it is, and what happens when it fails.

## The failure this prevents

DARKHELIX lives at `/ssdpool/DARKHELIX` on snarf, and there is exactly one
checkout. `/home/sam/code/projects/DARKHELIX` is the **same** checkout reached
by a second path — which is what made this hard to see, because a worker
"working in `/home/sam/code/projects/DARKHELIX`" looked like it had its own
copy.

Without isolation, workers `git checkout` branches in that shared tree. Two
cards running at once fight over `HEAD`; uncommitted work from four cards
becomes indistinguishable; and in August 2026 five days of agent work (552
lines across four cards) had to be salvaged by hand.

## Where isolation is created, and why there

**At claim time, for every card, regardless of who created it.**

It used to be created in one place: `POST /api/kanban/create`, behind the
HUD's SUBMIT WORK button. That covered cards Sam filed by hand and nothing
else. Every other route onto the board produced a card with no worktree:

| route onto the board | isolated before | isolated now |
|---|---|---|
| HUD SUBMIT WORK | yes | yes |
| `hermes kanban decompose` (auto-decomposer) | **no** | yes |
| `hermes kanban create` by hand / by an agent | **no** | yes |
| the Hermes dashboard | **no** | yes |
| `hermes kanban swarm` | **no** | yes |

The decomposer was the visible offender — it fans a triage card into a
dependency graph via `kb.decompose_triage_task()`, and children are created
with only title/body/assignee/parents — but fixing the decomposer alone would
have left the other three. So provisioning moved to the one point every card
passes through: **`kanban_task_claimed`**.

That hook fires in the *dispatcher* process, after the claim commits and
immediately before the worker subprocess spawns
(`hermes_cli/kanban_db.py: claim_task -> _fire_kanban_lifecycle_hook`).
`invoke_hook` calls callbacks synchronously, so provisioning finishes before
the worker exists, and the worker's first read of the card body already names
its worktree. A background thread here would race the worker and lose.

## The parts

```
CT111 hermes                              CT112 looking-glass          snarf
─────────────────────────────             ──────────────────────       ─────────────
gateway (hosts the dispatcher)
  └─ claim_task() commits
       └─ kanban_task_claimed ──POST──▶ /api/kanban/provision
            (plugin:                       └─ _darkhelix_provision()
             ~/.hermes/plugins/                  ├─ lineage check
             darkhelix-isolation)                ├─ worktree exists?
                                                 ├─ _darkhelix_worktree_create ──ssh──▶ git worktree add
                                                 └─ PATCH card body  ◀── dispatch-target
```

- **`~/.hermes/plugins/darkhelix-isolation/`** (CT111) — a ~40-line client.
  Holds no policy; it calls the HUD and logs the outcome. Enabled via
  `plugins.enabled` in `~/.hermes/config.yaml`.
- **`_darkhelix_provision()`** (`server/server.py`, CT112) — the single
  implementation. `POST /api/kanban/create` calls it too, so a card filed by
  hand and a card decomposed out of it cannot be provisioned differently.

## Scope: which cards get a worktree

Not every card on the board is DARKHELIX work, and provisioning a DARKHELIX
worktree for something else is worse than not provisioning at all. A card is
in scope when either:

1. **Lineage** — it is connected, through the dependency graph, to a card the
   HUD filed (`created_by == "looking-glass"`). SUBMIT WORK files DARKHELIX
   TODO.md items and nothing else, so anything linked to one is DARKHELIX
   work.

   The walk goes in **both** directions. The decomposer inverts the direction
   you would expect: it keeps the original card alive and makes it *depend on*
   every leaf it produced, so from a decomposed child the HUD's own card is
   reached through `children`, not `parents`.

2. **Declaration** — its assignee is listed in `darkhelix.provision_assignees`
   in `server.yaml`. This is the escape hatch for a card filed straight onto
   the board with no link to anything, which lineage cannot see.

Anything else is left alone — see "when it fails" below for why that is safe.

## Chaining: what a card inherits from its parents

Isolation is not independence. A card whose parents produced work needs that
work in its tree, or it re-derives it — which is exactly what happened to
`t_765ca5ad`: it burned three runs rediscovering the `markers.fasta` its
parent had already committed.

So the branch is cut from a **parent's** branch, not from `origin/master`:

- one parent with a branch → cut from it;
- several → cut from the first, `git merge` the rest;
- a merge that conflicts → **aborted**, not forced. The tree stays on the
  clean base and the unmerged parent is named on the card;
- a parent with no branch → skipped, and named on the card;
- no parents with branches → `origin/master`.

The `--- notes ---` half of the dispatch-target then states, per parent,
whether its work is actually in the tree. That claim is built from what git
did, not from what was asked for. Naming a parent whose branch never existed
as "already in your tree" would be the same class of lie as the `wt/...`
branch that started all of this.

## The dispatch-target block

The contract between the server (which provisions) and the worker (which must
use it). Written into the card body:

```
[dispatch-target]
repo: /ssdpool/DARKHELIX
worktree: /home/sam/darkhelix-wt/<task_id>
branch: hermes/<task_id>
base: <ref it was cut from>
builds-on: <parent task ids>
--- notes ---
<prose for the worker: what is in the tree, what is not>
[/dispatch-target]
```

Details that matter:

- **The prose lives inside the block.** Everything the server writes is
  replaceable in one substitution. When only the bracketed header was
  replaceable, each re-provision stripped the header and left the previous
  prose, stacking another copy of the notes on every pass.
- **Field parsing stops at `--- notes ---`**, because several prose lines
  contain a colon and would otherwise be parsed as fields.
- **A block is only "current" if it names this worktree and branch.** The
  superseded first version wrote `branch: wt/<slug>-<epoch>` with no worktree
  line and created nothing; the decomposer's LLM then copied that dead branch
  name into every child body. Cards carrying that shape are re-provisioned,
  not skipped.
- **"Already provisioned" is verified, not believed.** The body is a claim
  about the world; the check also confirms the worktree is on disk. A body
  naming a worktree someone has since removed would otherwise hand a worker a
  path that is not there — and back into the shared checkout it goes.

## When provisioning fails

Kanban lifecycle hooks are **observers**: return values are ignored and
exceptions are swallowed, so the hook *cannot* veto a dispatch. The stop is
enforced on the worker side instead:

1. provisioning fails → the server writes
   `worktree: NONE — ISOLATION FAILED` into the dispatch-target;
2. the worker's `execution-engine-dispatch` skill (step 1) refuses to work a
   card without a usable dispatch-target and blocks it.

A card outside the scope rules gets no block at all, which the same skill step
also treats as "stop and block". So both failure modes stop the card rather
than letting it edit the shared checkout. That is why a conservative scope
rule is safe: out-of-scope fails loudly, not silently.

## Operating it

- **Audit the whole board without side effects:**
  `POST /api/kanban/provision {"task_id": "...", "dry_run": true}` reports
  what it would do — in scope or not, which parents, whether the worktree
  really exists.
- **Provisioning history:** `~/.hermes/logs/darkhelix-isolation.log` on CT111.
  The gateway logs to the systemd *user* journal, which is not persisted on
  that box, so this file is the only durable record of which cards got a
  worktree and what it was cut from.
- **After changing the plugin**, restart the gateway so it reloads:
  `XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart hermes-gateway`.
  Plugins are discovered once at gateway startup (`gateway/run.py`).

## Worktree layout on snarf

Worktrees live at `/home/sam/darkhelix-wt/<task_id>` (`sam` cannot write at
the `/ssdpool` root). The repo is 1.5 TB on disk but only ~6 MB of tracked
source; `database/`, `thirdParty/`, `testData/` and `.venv-dev/` are symlinked
back to the primary checkout so a card can actually run. Outputs
(`DARKHELIX_output/`, `testruns/`) are deliberately **not** shared, so two
cards cannot overwrite each other's results.

Those symlinks are added to `.git/info/exclude` at provision time.
`.gitignore` ignores them as `database/` etc. — patterns with a trailing slash
match a *directory*, and in a worktree they are *symlinks*, so the patterns
miss and all three show as untracked, one `git add -A` away from committing
absolute snarf paths into the repo. `info/exclude` lives in the common git
dir, so writing it once covers every worktree, and it is never committed.
