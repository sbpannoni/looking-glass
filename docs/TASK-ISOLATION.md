# Task isolation: how a DARKHELIX card gets a git worktree

Every kanban card that touches DARKHELIX must work in its own git worktree on
snarf. This document says how that is guaranteed, why it is guaranteed at the
point it is, and what happens when it fails.

> **Isolation is only half the problem.** A worktree constrains *where* a worker
> writes; nothing here verifies *what it claims to have done*. Cards have
> reported files they never wrote, kept working after blocking themselves, and
> relaxed the test assertion that judged them — all while perfectly isolated.
> See [PIPELINE-VERIFICATION.md](PIPELINE-VERIFICATION.md) for those failures,
> the fixes, and the shared-data policy that worktrees do not cover.

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
| HUD SUBMIT WORK (single card) | yes | yes |
| HUD SUBMIT WORK (swarm) | n/a — added 2026-09-02 | yes |
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

## The download backlog

Nine TODO.md items are blocked on a database or a binary nobody has
downloaded. They are not model work — bytes, time, then an edit to match the
format the pipeline reads — but the only way onto the board was a card, and
the board runs one card at a time on one GPU seat. A 110 GB pull would hold
that seat for hours doing nothing a GPU is for.

The **DOWNLOADS** pane (`GET /api/darkhelix/downloads`) joins those items with
`server/config/downloads.yaml`, a tracked catalogue of verified URLs, and
files each one as a card that is immediately parked in `scheduled` —
`schedule_task` makes a card explicitly not dispatchable, so it is on the
board with a real id to comment on and cannot take the seat. `wget -c` then
runs detached on snarf, off the board entirely.

Three claims it refuses to make:

- **an unchecked URL.** Every entry is HEAD-checked from snarf, the host that
  will do the downloading, and a blocked item with no entry reads "needs a
  URL" rather than a guess — a plausible-looking ConoServer path was dropped
  for answering 404 before the real one (`download/conoserver_protein.fa.gz`)
  was found. Five of the nine items resolve today; the other four are not
  downloads at all — a `diamond makedb` build, two tool installs, and one item
  that says in its own text that it is blocked on curation, not compute. The
  catalogue has no way to express those, which is the next thing to fix here.
- **that a download is still needed.** On-disk size is compared against the
  server's `Content-Length` — which found the Mash RefSeq sketch already
  complete on snarf at 754,115,096 bytes, so that item is blocked on wire-in,
  not on a download. Sizes are `du -sb` (apparent), because `/ssdpool` is
  compressed ZFS and block usage reads 413M for that same file.
- **that an unpacked archive is complete.** A tree is a different size from
  its tarball, so an `archive:` entry reports presence and stops there.

Every entry carries a `wire_in` line, because "the bytes are down" is where
these items historically stop.

## Filing a swarm from the HUD

`hermes kanban swarm` is the board's other shape: a root card that completes
on arrival and holds the shared blackboard, N parallel workers, a verifier
that waits on all of them, a synthesizer that waits on the verifier. SUBMIT
WORK files one over `POST /api/kanban/swarm`, and the graph lands in scope
because `created_by` propagates to every card the swarm creates and
`--created-by looking-glass` is the first thing the lineage check looks at.

The chaining rules below then do something useful for free: workers hang off a
root with no branch, so each is cut from `origin/master`; the verifier has all
N workers as parents, so its tree is the workers' branches merged; and the
synthesizer is cut from the verifier's. The graph's shape is the branch graph.

Three things the endpoint refuses to send, each of them a failure this board
has actually had:

| refusal | what it prevents |
|---|---|
| a verifier profile without `requesting-code-review`, or a synthesizer without `humanizer` | `create_swarm` hardcodes those skills onto the role cards without checking. On 2026-09-02 `--synthesizer researcher` died at agent init with "Unknown skill(s): humanizer" *after* all three workers and the verifier had finished. `coder` cannot synthesize and `researcher` cannot verify, today |
| a colon in a worker's angle | `parse_worker_arg` splits `--worker` on `:` with maxsplit=2 and reads the third field as a comma-separated skill list, so "Audit X: the four layers" files a card whose skills are `["the four layers"]` |
| filing at all when the profile list cannot be read | an unchecked swarm risks losing every worker's run at the last card, which is worse than not filing |

Unlike the single-card path this one **dispatches**: swarm workers are created
`ready`, not `triage`. The panel says so above the button. They still
serialise on the one GPU seat — a swarm buys structure and a verifier, not
throughput.

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

## Dispatch: why it is a tool, not a skill

Isolation gets a card a worktree. Running the work is the next layer, and it
had the same shape of bug for a different reason.

`execution-engine-dispatch` used to describe a four-step procedure — build a
command, run it, parse `result.json`, report to kanban — with **zero
judgement calls in it**. Every branch is `if result["status"] == "done"`.
Handing that to a model gave it wide latitude and a trivial mandate, and it
filled the gap: runs 1 and 2 (2026-08-22) exited without ever calling
`kanban_complete` ("protocol violation"), and runs 4 and 6 (2026-08-28) both
reasoned their way to editing files directly instead of dispatching. Prose in
a skill is a suggestion.

So the procedure moved into a tool, `dispatch_to_engine`, registered by the
`darkhelix-engine` plugin on CT111. Everything that used to be a written
warning is now unreachable rather than discouraged:

| trap | how the tool removes it |
|---|---|
| passing `repo:` instead of `worktree:` — the engine cuts its branch from `--repo-path`'s HEAD, so this silently discards the parents' work | reads `worktree:` from the dispatch-target |
| reusing the card's branch — `git worktree add -b` fails on an existing branch | mints `hermes/<task_id>-engine-<n>` |
| omitting `--description` (required) or passing `--workspace-path` (never existed) | builds the argv |
| running a mode-644 file directly | invokes via `python3` |
| forgetting `kanban_complete` | attaches the patch and completes on success |

**What is deliberately NOT in the tool: diagnosis.** On failure it returns the
engine's verdict and stops — it does not block the card and does not retry.
Deciding whether a red test gate means "the edit was wrong" or "the spec was
incomplete" is the one genuinely reasoned call in the flow, and it is the
model's. That is not academic: the 2026-08-28 08:20 attempt failed its gate
because the card never mentioned `tests/test_synthetic_pcr_panel.py`, which
asserts the old behaviour — correcting the data made a strict-xfail test start
passing, which registers as a failure. The edit was fine; the spec was short.
Re-running it unchanged would fail identically forever.

Attempts are capped at 3, counted from attempt directories on disk rather than
from anything the model tracks.

`dry_run: true` reports the exact command and inputs without spending an
attempt.

## Card bodies are not authoritative about branches

The decomposer writes child bodies with an LLM, copying machinery out of the
parent it was handed. That is how `wt/the-synthetic-pcr-gene-panel-is-
fabricat-1787435482` — a branch name from the first, superseded create path,
which nothing ever created — became an instruction in four child bodies and
then the engine's real `--branch-name`. The run failed and deleted the branch,
so the work was lost.

Provisioning now defuses that one provably-dead shape (`wt/<slug>-<10-digit
epoch>`) when it rewrites the body. Deliberately narrow: a broader
"strip anything branch-shaped" rule would start editing real task text, which
is a worse failure than the one it prevents. Branches and paths are
provisioning's to decide; the card text only ever describes the work.

## Reading a decomposition on the board

The decomposer writes plain descriptive titles — "Decide amplicon sourcing
strategy", "Implement GFF-based sequence extraction", "Update PCR panel
configuration" — with no numbering and no ordering hint. The dependency graph
it builds is real but was invisible on the board, which rendered only title,
assignee and age. A card sitting in `todo` BECAUSE a parent had not finished
looked identical to one merely queued, and finding the order meant running
`hermes kanban show` per card.

Cards now carry two chips, both from data the board already sends
(`link_counts` and `progress`, each one cheap query in the plugin API):

| chip | meaning |
|---|---|
| **⛓ n** (amber) | in `todo` and waiting on n unfinished parents — cannot start |
| ↳ n | depends on n earlier cards; not the reason it is not running |
| n/m | n of m child cards done |

The amber one is the load-bearing distinction: Hermes holds a child in `todo`
while any parent is open and promotes it to `ready` once they all close, so
`todo` + parents > 0 IS "blocked on dependencies". No extra request is needed
to know it.

Note this is inferred from status + parent count, not from a per-parent status
lookup: it tells you a card is waiting and on how many, not which one is the
holdup. `hermes kanban show <id>` still gives the specific ids.

## Stopping the pipeline without losing anything

`hermes pause` is Hermes's own global stop and is exactly the right shape for
this: the dispatcher checks it every tick BEFORE spawning, so it takes effect
on the next pass with no restart; **in-flight workers are never killed**; and
cards stay `ready`, so `hermes resume` continues precisely where it stopped.

It was only reachable from a shell on CT111, which meant the way to stop
runaway work *from the board* was to reclaim cards one at a time — killing
their workers and losing whatever they had done. It is now a button on the
board (`⏸ pause dispatch`) over `GET`/`POST /api/kanban/pause`.

Two details that matter:

- The button sends an **explicit target state**, never a toggle. A toggle read
  off a stale board does the opposite of what was intended, and this is the
  control you reach for when something is already going wrong.
- A paused board otherwise looks identical to an idle one, so the state is a
  banner across the pane, not just a change of button label.

## Runtime caps

`hermes kanban create --max-runtime` sets a ceiling, and every card filed by
hand sets one. **The decomposer sets nothing**, so its children ran uncapped —
`t_7c57772b` ran 1h01m, satisfied its card at ~45m, then continued
regenerating testruns nobody asked for, and stopped only because a human
noticed.

There is no config default to hook, and the dashboard's `UpdateTaskBody`
cannot set the field, so the cap is applied by the `darkhelix-isolation` claim
hook: if a card has no `max_runtime_seconds`, it gets one. The dispatcher's
timeout sweep reads the task's live value each tick, so a value written at
claim covers the run that is starting. An existing cap is never lowered.

The default is 90 minutes, chosen against evidence rather than picked round:
an engine round-trip is 3–5 minutes, the longest genuinely useful run observed
was ~60 minutes, and the wedged runs that started this work sat at 2h+
producing nothing. Exceeding it makes the dispatcher **requeue** the card, so
an overrun costs a retry, not the work.

## Memory: siloed per profile, shared via mempalace

Workers get all the repetitions and keep none of them. Every durable lesson
from the 2026-08-28 build-out — `/ssdpool` is not on CT111, the worktree is on
snarf, `dispatch_to_engine` is the only sanctioned path, a red gate usually
means a short spec — was rediscovered by more than one worker and then written
down by a human. Nothing the workers learned survived their own sessions.

The capability was there the whole time: `memory` is a TOOL and it is in the
worker toolset. Nothing ever asked them to use it, and the `nudge_interval` /
`flush_min_turns` settings target long conversational sessions, not one-shot
`chat -q` workers that die with the card. So the skill now asks, once, before
finishing.

**The two stores are not equivalent:**

| store | scope | use for |
|---|---|---|
| `memory` | **per profile** — `get_hermes_home()/memories`, and `-p <profile>` swaps `HERMES_HOME` | habits specific to that profile's work |
| `mempalace` | **shared** — one MCP server on CT110 (`/root/.mempalace/homelab`), reached by every profile and box | facts true regardless of who hits them |

That distinction is load-bearing. All four profiles carry their own identical,
stale copy of the same two memory files from July, because a fact written to
`coder`'s memory is invisible to `bioinformatics`. A fleet-wide fact belongs in
mempalace or it will be learned again by the next profile.

Stale memory is worse than none: `coder`'s memory asserted DARKHELIX had 270
tests for a month after it had 624, so every worker that read it started from a
false number. The skill therefore says to CORRECT a wrong entry rather than add
a second one beside it.

## Artifacts must not misrepresent themselves

A review is only as good as the artifact it reads. On 2026-08-29 card
t_5f2479af attached TWO patches 31 minutes apart, both named
`t_5f2479af-engine-1.patch` — the attach step disambiguated the second with a
` (1)` filename suffix that says nothing about which is current. The root card
reviewing it read the older one, correctly identified a real positional-parsing
bug **in that patch**, and then blocked the work and spawned a fix card for
code that had already been replaced. Its reasoning was sound throughout; the
artifact was wrong about itself.

This is the same shape as the `wt/...` branch that started this work: a card
asserting a state of the world nothing had created. Neither is a context
problem — more context would not have helped, because the artifact was
internally consistent and simply stale.

Two changes:

- **Patches are named by commit sha** (`{task_id}-{sha}.patch`), not by attempt
  number. Two artifacts cannot claim one identity, and any reader can check the
  name against the branch. The completion summary names the branch and sha too.
- **The board surfaces the result, not just the transcript.** A done card has
  a `Findings` button beside `Archive` (`GET /api/kanban/<id>/output`) showing
  its completion summary, the structured facts the run recorded, the swarm
  blackboard if the card was part of a swarm, and every file the run named —
  each one checked against disk. That last part is this same lesson: the
  2026-09-02 synthesizer signed off pointing at
  `~/.hermes/kanban/workspaces/t_a2f91234/synthesis.md`, and a `scratch`
  workspace is deleted when the card completes, so the path was dead before
  anyone read it. The pane says so instead of printing it.
- **The skill says to review the BRANCH, not the patch.** `git show <branch>`
  is definitionally current; a patch is a copy. And before reporting a defect,
  quote `path:line` from the branch — if you cannot point at the line you have
  a hypothesis, not a finding. Better still, run it: "this loads 0 rows" is one
  command to check, and a blocked card plus a spawned fix task is expensive.

The test gate cannot catch this class. It verifies *code*; this was a claim
*about* code.

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

**One root for everything a card produces on snarf:**

    /ssdpool/agent-work/<task_id>/worktree/     the card's git worktree
    /ssdpool/agent-work/<task_id>/attempts/     one dir per engine attempt

It used to be spread over five places across two hosts — worktrees under
`/home/sam`, engine attempts in a flat pile under `/ssdpool/coder-engine`,
patches and attachments under `/root/.hermes` on CT111, and, because nothing
said otherwise, whatever an agent picked in `/tmp`. That last one is not
hypothetical: one run left its finished rewrite at `/tmp/synthetic_pcr_new.py`
and an earlier attempt left a whole worktree at `/tmp/darkhelix_worktree`.
`/tmp` is cleared on reboot, so "where did the work go" had five answers and a
deadline.

The root is created by hand (`sudo mkdir`, owned by `sam`, setgid) because
`/ssdpool`'s root is root-owned; everything beneath it is `sam`'s. The engine
falls back to its old flat `dispatch-attempts/` root for anything that is not
a provisioned kanban task, so the toy and eval-harness paths are unchanged.

**A document a card produces belongs under
`/ssdpool/agent-work/<task_id>/output/`, never in the card's workspace.**
`scratch` workspaces (`~/.hermes/kanban/workspaces/<task_id>/`) are deleted
when the card completes, so a file written there is destroyed by the act that
marks the card done — which is how the 2026-09-02 swarm's `synthesis.md` was
gone before anyone read the card that named it. The rule is in
`execution-engine-dispatch`, and because only `coder` carries that skill (and
only `coder` and `darkhelix` hold a snarf key), every HUD-filed swarm also
carries it in its goal, where `create_swarm` copies it onto all five cards.
For findings rather than files, a comment on the card or the swarm root is
better still: those are rows in `kanban.db`.

Patches and attachments still live under `~/.hermes` on CT111 — `hermes kanban
attach` needs a local file, so that one cannot move to snarf. The repo is 1.5 TB on disk but only ~6 MB of tracked
source; `database/`, `thirdParty/`, `testData/` and `.venv-dev/` are symlinked
back to the primary checkout so a card can actually run. Outputs
(`DARKHELIX_output/`, `testruns/`) are deliberately **not** shared, so two
cards cannot overwrite each other's results.

`.git/info/exclude` also carries `.aider*` and `stringutils.py`. Every engine
attempt otherwise committed two artefacts, reproduced byte-for-byte across
runs: an uninvited `.aider*` line appended to `.gitignore`, and an empty
`stringutils.py` (the toy repo's filename). `commit_node` stages with `git add
-A`, so both rode into the patch.

Excluding them beats narrowing what `commit_node` stages, which would also
silently drop legitimately-added files — a new test being the obvious case,
and one these specs explicitly contemplate. Verified: with both excluded,
`git add -A` still stages a new `.py` file and drops only the artefacts.

`.aider*` also stops Aider writing to `.gitignore` rather than merely hiding
the result. `aider/main.py`'s `check_gitignore()` appends the pattern only
`if not repo.ignored(".aider")`, and that check is `git check-ignore`, which
honours `info/exclude` — so it returns before opening the file.

Those symlinks are added to `.git/info/exclude` at provision time.
`.gitignore` ignores them as `database/` etc. — patterns with a trailing slash
match a *directory*, and in a worktree they are *symlinks*, so the patterns
miss and all three show as untracked, one `git add -A` away from committing
absolute snarf paths into the repo. `info/exclude` lives in the common git
dir, so writing it once covers every worktree, and it is never committed.
