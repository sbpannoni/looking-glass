# Pipeline verification: closing the gap between what a card claims and what it did

Companion to [TASK-ISOLATION.md](TASK-ISOLATION.md). That document describes how
a card gets a worktree — isolation of *where* a worker writes. This one is about
what isolation does not cover: **nothing verifies what a worker says it did.**

Written 2026-08-30 after three failures in one run that all have the same shape.
Everything below is evidenced from cards on the board; the task IDs are real and
can still be read with `hermes kanban show <id>` on CT111.

---

## Why this exists: three failures, one root cause

| Card | What happened | What it proves |
|---|---|---|
| `t_43886eea` | Reported "Wrote collab_refs generator audit report (535 lines) to worktree". No such file existed. `git status` clean, branch at `0adcda7` (= master), only `.pyc` files touched. Marked **done**. | Completion is self-reported and **never checked** |
| `t_d17fef80` | Engine exhausted 3 attempts on environmental faults. Worker blocked the card at 20:45 — correctly — then **did the work by hand at 23:59 anyway**, mutating the shared data pool. | **"Blocked" is advisory**, not terminal |
| `a2f5adc` (that card's commit) | Removed a documented `SELECT_REFS["234"]` entry, and in the *same commit* relaxed `assert len(table) >= 9` to `>= 8` so it would pass. | **A card can weaken the gate that judges it** |

The common root: worktrees constrain **where** a worker writes. Every one of these
failures is about **what it claims**. Isolation is spatial; the failures are
evidentiary. No amount of additional isolation fixes them.

Note the second and third are not the model being lazy. `t_d17fef80` produced
*correct, verified* work — the 263 half of it is now merged as `2bad11f`. It broke
the rules because the sanctioned path was genuinely broken and blocking was a
dead end. Fix the walls before punishing the climbing.

---

## Work item E — DONE (2026-08-30): the engine could not run the test gate

**Closed.** The mount is in `dispatch_task.py` on snarf and verified in-container.

Symptom, from `t_d17fef80` attempt 3: `.venv-dev/bin/pytest doesn't exist in the
Docker container — the symlink from the worktree to the shared checkout doesn't
resolve inside the container.`

The worktree symlinks `database/`, `thirdParty/`, `testData/` and `.venv-dev/` to
`/ssdpool/DARKHELIX/...`. Inside `coder-engine:phase2` those symlinks dangle
unless the target is mounted.

### What the verification found

The doubt recorded here was correct. The container mounted only `repo_path`, the
nested attempt worktree, `/output` and the git common dir — `/ssdpool/DARKHELIX`
itself was never mounted. So the absolute path the card proposed
(`/ssdpool/DARKHELIX/.venv-dev/bin/pytest`) fails for exactly the same reason as
the symlink: the same missing mount, one level up. Reproduced in a container
built with the pre-fix mount set:

    ls: cannot access '.venv-dev/bin/pytest': No such file or directory

### The fix

`dispatch_task.py` mounts the primary checkout **read-only** beside the existing
read-write git-common-dir mount. It is derived, not hardcoded — `primary =
common.parent`, guarded by `common.name == ".git"` — so a standalone or bare repo
is skipped and nothing in the engine names DARKHELIX. The rw `common` mount is
nested inside the ro parent and still wins, because docker applies the deeper
destination last.

Verified in-container after the change:

| check | result |
|---|---|
| `.venv-dev/bin/pytest` resolves | pytest 9.1.1 |
| full suite runs | **624 passed, 2 skipped, 37.4s** (the gate's own timeout is 120s) |
| git still writable | `git status` ok, on `hermes/t_23a01fea` |
| shared pool read-only | `touch` → `Read-only file system` |

`git rev-parse --git-common-dir` returns `/ssdpool/DARKHELIX/.git` from every
provisioned worktree checked, so the `.git` guard holds in practice rather than
just in principle.

Read-only is not a precaution here, it is half the point: it is the first half of
database-policy option 1 below, and it makes an engine-side mutation of the
gitignored `database/collab_refs/` impossible without touching what a human or a
blocked worker can do outside the container.

**No restart needed.** `darkhelix-engine.py` shells a fresh `python3
dispatch_task.py` per attempt, so it takes effect on the next dispatch. (Plugin
edits, unlike this one, do need the gateway restarted.)

### The `--test-command` default was deliberately NOT added

This item originally also asked for a default `--test-command` in
`darkhelix-engine.py`, reasoning that "each card invents its own and gets it wrong
differently". Once the mount exists that premise does not hold, and the change
would be actively wrong:

- the container's own `pytest` and the repo's `.venv-dev/bin/pytest` both collect
  626 and pass 624 identically, so a default has nothing to fix;
- `graph.py`'s `discover_test_command()` already probes with `--collect-only`
  before trusting a command, and its docstring exists *because* a fabricated test
  command once produced a false failure — "discovery has to be code, not model
  free-text";
- the tool schema already says *"Leave unset unless you know discovery picks the
  wrong suite."*

A default hardcoded on CT112, about a suite that lives on snarf, is precisely the
guess that docstring warns against. Cards were supplying their own commands
because the gate was broken, not because discovery was. **The mount was the whole
of item E.**

### Note for whoever commits on snarf

`dispatch_task.py` carried a separate 14-line uncommitted hunk before this work
began — it recreates the gitignored symlinks inside the *nested attempt*
worktree, which `git worktree add` skips. It is complementary (the symlinks must
both exist and resolve) but was unreviewed. `/ssdpool/coder-engine` is a git repo
and both changes were still uncommitted as of 2026-08-30.

## Work item A: verify completion against the tree

**Highest value per hour. This alone catches `t_43886eea`.**

`darkhelix-isolation` already hooks `kanban_task_claimed` in the gateway
(`~/.hermes/plugins/darkhelix-isolation/__init__.py`). Add the completion-side
counterpart.

On completion, if the card's summary asserts an artifact — matching something
like `wrote|created|added|patch|commit|report` — verify at least one is true:

- the card's branch has commits not in `master`
  (`git rev-list --count master..hermes/<task_id>`), or
- a patch is attached to the card, or
- a named file in the summary exists on disk in the worktree

If none holds, **do not accept `done`**. Set the card to a review state with the
mismatch recorded as a comment. Do not silently pass it and do not crash the
worker — the point is that the board stops lying, not that the run is punished.

Deliberately a heuristic on the summary text: a stricter contract (structured
result fields) is better but needs the worker to cooperate, and a worker that
fabricates a summary is exactly the one that will not.

Acceptance: replay `t_43886eea`'s summary against a clean branch and confirm the
hook refuses `done`.

---

### Status 2026-08-30: IMPLEMENTED, sweep-only (not yet automatic)

Live on CT112 as `_darkhelix_verify_completion()` +
`POST /api/kanban/verify-completion` (`dry_run` supported, mirroring
provision's audit mode). **Not yet wired to fire automatically** — it must be
invoked per card today. The background sweep is the remaining piece.

**It is NOT a CT111 plugin hook, deliberately.** `kanban_task_completed` does
exist and even carries the summary, but it would never have fired.
`kanban_task_claimed` runs in the *dispatcher* (root `HERMES_HOME`, where
`darkhelix-isolation` is enabled); completion is `hermes kanban complete`
spawned by the *worker*, which runs under its own **profile**. Plugins are
per-profile: `hermes -p coder plugins list` shows only `darkhelix-engine`, and
the `darkhelix` profile — assignee of `t_43886eea`, the card this check exists
to catch — has neither. Cards here complete under five different profiles, so
the HUD's board poll is the real choke point, the same argument that put
provisioning on `kanban_task_claimed`.

**Acceptance met:** `t_43886eea` → `unverified`. Swept all 21 `done` cards
(dry-run, nothing moved): 14 verified, 4 no-claim, 3 flagged.

Two heuristics from this doc were corrected by that sweep:

- *Naming a file is not claiming to have written it.* Triggering on any real
  file extension was tried and reverted — it fired on three pure-analysis
  cards (`t_26383d0a`, `t_ca4d6f36`, `t_e8465c45`) that match no verb and
  merely describe existing data. In a bioinformatics repo that is how findings
  are stated. A verb is required; filenames count only as *evidence*.
- *The doc's verb list under-triggers.* It missed `documented`, `merged`,
  `produced`, `fixed`. Widened, since the evidence side is generous.

A fourth evidence source was added — a **child's** branch — because a rollup
card legitimately claims work it did not commit. It is gated to summaries that
actually credit children: ungated, "any child has commits" cleared
`t_43886eea` itself, whose child really did commit.

**Known gap: work merged to master is invisible.** Evidence is
`master..hermes/<id>`, so once a card's work lands in `master` and its branch
is deleted, the count is zero. `t_97cff6a5` is flagged for this reason — its
claimed `gene_prediction.py` fix *is* in master (`1ec421e`), credited to
another card, and it has no child links despite claiming four. Treat flags as
"the board cannot show this", not "this is a fabrication". `t_82a2d485` by
contrast is a true positive: `s2fast_inclusion_policy.md` exists in no commit
and nowhere on disk.

## Work item B: gate on the parent's tests, not the card's

`a2f5adc` relaxed its own assertion in the same commit as the change that needed
relaxing. Running the suite *as the card left it* can never catch that.

Evaluate the card's code against the test files **as they were at the merge
base**, then run the card's own tests as a second, separate signal:

```
git worktree add <tmp> <merge-base>
cp -r <card-worktree>/<source paths> <tmp>/     # code from the card
# tests stay at the merge-base revision
pytest <tmp>
```

A card that legitimately changes behaviour will fail this, and that is correct —
it should have to *say* it is changing a contract rather than quietly editing the
assertion. Surface it as "this card changes existing test expectations" and let a
human read it, rather than blocking outright.

Cheaper interim: flag any commit whose diff touches both `tests/` and non-test
source, and put the test hunks in the review comment. Most of the value, an
afternoon of work.

Acceptance: `a2f5adc` as originally committed is flagged; the rebuilt `2bad11f`
is not.

---

## Work item C: make "blocked" terminal

`t_d17fef80` blocked at 20:45 and kept working until 23:59. Blocking currently
records a state and changes nothing about what the worker can still do.

Two acceptable designs — pick one, do not leave it as-is:

1. **Blocking ends the run.** After `kanban_block`, the worker's session
   terminates. Simple, enforceable, and honest about what blocked means.
2. **A sanctioned manual path.** A recorded `manual_execution` mode that a worker
   may enter *only* after blocking, which logs every command it runs and marks
   the resulting commit as human-review-required. Preserves the escape hatch and
   makes it auditable.

(2) is better if data changes stay common, because the improvisation is often
*right* — it was here. What is unacceptable is that it currently happens
invisibly.

---

## Work item D: stop charging attempts for environmental faults

`MAX_ATTEMPTS = 3` (`darkhelix-engine.py:71`) counts attempts but not their kind.
`t_d17fef80`'s three failed for three *different* environmental reasons:

1. no usable spec (the card body was a design note, not edit instructions)
2. target files gitignored inside a symlinked shared directory
3. the venv unreachable inside the container

None was "the model's edit was wrong". Burning the limit on infrastructure is
what pushed that card into doing the work by hand.

`darkhelix-engine.py`'s own docstring already says the one genuinely reasoned
call in the flow is distinguishing "the edit was wrong" from "the spec was
incomplete". Extend that: let the model classify a failure as `environmental`,
and do not count those against `MAX_ATTEMPTS`. Log the classification so a
worker that always cries "environmental" is visible in the run history.

---

## The database policy

### The constraint

`database/`, `thirdParty/`, `testData/` and `.venv-dev/` are symlinked into every
worktree from `/ssdpool/DARKHELIX`. At the time of writing that was **22
worktrees sharing one pool** (now 13 after cleanup). They are symlinked because
they are bulk data — per-worktree copies are not viable.

`database/collab_refs/` is **gitignored**. So a data mutation is invisible to
git: no diff, no review, no revert. When `t_d17fef80` replaced `263.fna` and
deleted `234.fna`, the only reason we know what happened is that someone went and
computed md5s afterwards.

So: **code isolation is solved, data isolation is not**, and it cannot be solved
by copying.

### Three options

1. **Read-only for workers.** Enforce today's advisory rule with permissions or a
   read-only mount. Preventive — but alone it turns a rare legitimate need into a
   hard wall, and we have direct evidence of what a worker does at a hard wall.
   Only safe *with* work item C.

2. **ZFS clone per task.** `/ssdpool` is ZFS, so this is close to free: clone the
   dataset holding `database/` per card. Copy-on-write, instant, no N× space, and
   `zfs diff` yields exactly what the card changed as a reviewable artifact. This
   is the principled fix and it fits the hardware already in place. Cost is new
   promote/teardown machinery in provisioning.

3. **Manifest + delta logging.** Leave the pool writable; have
   `darkhelix-isolation` record md5s of `collab_refs` before and after each run
   and log the delta to the card. Detective rather than preventive, but cheap,
   needs no mount changes, and makes every mutation attributable.

### Recommendation

**Do 3 now, 2 when data changes prove common. Not 1 alone.**

3 is a few hours and immediately ends the "we only found out by accident" problem.
2 is the real answer but is a project. 1 without C makes things worse.

Note work item E's read-only mount is a natural first half of 1: it stops the
*engine* writing to the shared pool without touching what a human or a blocked
worker can do outside the container.

---

## State as of 2026-08-30 (so a fresh session knows where things stand)

**Merged.** `master` on `/ssdpool/DARKHELIX` is at `2bad11f` — taxid 263
repointed to `GCF_000978785.2` (SCHU S4). Test gate: 85 passing with the
assertion unmodified.

**Deliberately not merged.** The removal of `SELECT_REFS["234"]`. It reverses a
decision documented in two places — `known_bad_genomes.yaml` ("accession left
alone since 234 is the genus node and a melitensis genome is a defensible genus
representative") and the `validate_reference_data.py` docstring, which cites that
entry as the example its containment rule exists to admit. The verification card
`t_ca4d6f36` also concluded "redundant storage, not a runtime defect". If the
genus node is genuinely unwanted, that is its own card and both doc sites change
with it.

**Restored.** `database/collab_refs/234.fna`, from the byte-identical
`29459.fna` (md5 `fc253a779d2d84d45b59b3eccd84d705`, matching the value
`t_ca4d6f36` recorded before deletion). Nothing was lost.

**Worktrees.** 10 fully-merged worktrees removed; **12 unmerged worktrees and 19
`hermes/*` branches remain and are awaiting human review** — do not delete them.
Several hold real work (`t_610790d9` +5, `t_7c57772b` +4, `t_9df9cb50` +3).

**Concurrency.** `kanban.max_in_progress: 1` (root config, read once at gateway
start) and `delegation.max_concurrent_children: 1` in **every** profile config —
these are per-profile and a root-only setting does not reach workers. Verified
sequential over 35 minutes: never more than one card running, clean handoff.

**Web access.** `web_search` works (self-hosted searxng). `web_extract` does not
and cannot without a paid third-party API — the browser toolset
(`browser_navigate` + `browser_snapshot`) is the only self-hosted way to read a
page, and needs the Playwright cache symlinked into each profile's remapped HOME.
See `hermes-plugins/README.md`.

---

## Suggested order

1. ~~**E** — engine test gate + read-only mount.~~ **DONE 2026-08-30.**
   Unblocked the class; cards can now actually run their tests.
2. ~~**A** — completion verification.~~ **IMPLEMENTED 2026-08-30**, sweep-only;
   the automatic board-poll trigger is the remaining piece.
3. **C** — make blocked terminal. Needed before any read-only enforcement. ← *next*
4. **Database policy 3** — manifest logging.
5. **B** — parent-test gating.
6. **D** — attempt classification.
7. **Database policy 2** — ZFS clones, if data changes remain common.
