---
name: execution-engine-dispatch
description: "Run a kanban card through the LangGraph+Aider execution engine on snarf via the dispatch_to_engine tool, and diagnose the result."
version: 2.1.0
author: Hermes Agent (coder profile)
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [kanban, execution-engine, aider, langgraph, dispatch, code-editing]
    related_skills: [plan, requesting-code-review, systematic-debugging]
---

## When to use this skill

You are the dispatched worker for a claimed kanban task in the `coder`
profile. The dispatcher has already claimed the card before spawning you.

**You do not edit code. You do not run git. You do not build shell commands.**
All of that is inside the `dispatch_to_engine` tool. Your job is the one part
that needs a mind: deciding what a failed attempt actually means.

## Steps

1. **Call `dispatch_to_engine(task_id=<the card>)`.**

   That is the whole mechanical procedure. The tool reads the card's isolated
   worktree, mints a fresh branch, runs the engine, and on success attaches
   the patch and completes the card. Nothing is left for you to assemble.

2. **If it returns `success: true` — you are done.** The card is complete and
   the branch holds a reviewable commit. Do not merge; a human does that.
   Report briefly and stop.

3. **If it returns `success: false` — diagnose before doing anything else.**
   This is the actual work. Read `error` and decide which of these it is:

   - **The spec was incomplete.** By far the most common. The engine edits
     what it was told to and then runs the test suite as a gate; if the card
     never mentioned something the change necessarily breaks, the gate goes
     red through no fault of the edit. The real 2026-08-28 case: the card said
     "remove the fabricated sequences" and never mentioned
     `tests/test_synthetic_pcr_panel.py`, which asserts the OLD behaviour — so
     correcting the data made a test that was supposed to fail start passing,
     and the gate failed. Nothing about the edit was wrong.

     → Call `dispatch_to_engine` again with `amended_description` covering
     what was missing. Quote the specific failing test and say what should
     happen to it.

   - **The change is genuinely hard or ambiguous.** The spec was right and the
     engine still could not do it.

     → `hermes kanban block` with the specific reason, quoting the error. Do
     not retry.

   - **The card was never isolated** (`no usable worktree`).

     → `hermes kanban block` with that reason. Never fall back to working in
     the shared checkout: on snarf `/home/sam/code/projects/DARKHELIX` and
     `/ssdpool/DARKHELIX` are the SAME checkout, and editing there corrupts
     every other card's view of the tree.

4. **Attempts are capped at 3, enforced by the tool.** When it says no
   attempts remain, that is not a prompt to try something else — block the
   card and say precisely what is unresolved.

## Reviewing someone else's work: read the branch, not the patch

If your card reviews another card's output — a root card waking after its
children finish, or any check on work you did not do — **read the branch**:

    git show <branch>            # what the code IS
    git log --oneline master..<branch>

An attached patch is a COPY, and a copy can be stale. On 2026-08-29 card
t_5f2479af produced two patches 31 minutes apart, both named
`t_5f2479af-engine-1.patch`; the attach step disambiguated the second with a
` (1)` filename suffix that says nothing about which is current. A root card
reviewing it read the older one, correctly identified a real bug in it, and
then blocked the work and spawned a fix card — for code that had already been
replaced. Its reasoning was sound; the artifact misrepresented itself. Patches
are now named by commit sha so this cannot recur, but the branch is still the
only thing that is definitionally current.

**Before reporting a defect in code, quote it from the branch.** Give
`path:line` and the actual text as it exists there. If you cannot point at the
line, you do not have a finding yet — you have a hypothesis, and saying so is
the honest report.

**Better still, run it.** A claim like "this loads 0 rows" is checkable in one
command. A blocked card and a spawned fix task are expensive; a `python -c`
against the real input is not.

## Where the thing you produced has to live

A card's workspace is scratch, and `scratch` means deleted: Hermes removes
`~/.hermes/kanban/workspaces/<task_id>/` when the card completes. A document
written there is destroyed by the same act that marks the card done.

Not hypothetical. The 2026-09-02 swarm synthesizer (t_a2f91234) finished with
`synthesis_artifact: /root/.hermes/kanban/workspaces/t_a2f91234/synthesis.md`
in its completion metadata and signed off pointing at it. The directory was
already gone when the first person went to read it. The work was good; the
only copy was in a temp dir.

So before you complete, put the deliverable somewhere that outlives the card.

- **`/ssdpool/agent-work/<task_id>/output/` on snarf** — the durable root that
  already holds this card's worktree and its engine attempts. Right for files:
  reports, generated data, anything you would otherwise attach. You are on
  CT111, which has no `/ssdpool` at all, so write it over ssh as `sam`:

      ssh -i ~/.hermes/profiles/<profile>/snarf_key sam@192.168.1.239 \
        "mkdir -p /ssdpool/agent-work/<task_id>/output"
      scp -i ~/.hermes/profiles/<profile>/snarf_key report.md \
        sam@192.168.1.239:/ssdpool/agent-work/<task_id>/output/

  Only `coder` and `darkhelix` carry that key today. If your profile has none,
  use the next option — do not skip the step.

- **A comment on the card, or on the swarm root** — rows in `kanban.db`, so
  they survive the workspace, the run and the session, and they are what the
  board renders. Right for the findings themselves. In a swarm the root card
  IS the shared blackboard: post there and every sibling can read it too.

**Then name what you produced in the completion metadata**, e.g.
`{"artifact": "/ssdpool/agent-work/<task_id>/output/report.md"}`. The HUD's
FINDINGS pane on a done card reads those keys and checks every path it finds
against disk, on the right host — so a path that is wrong, gone, or on CT111
when the file is on snarf shows up as missing instead of sending a reader off
to look for it.

**Never name a path under the workspace.** By the time anyone reads the card,
it is not there.

## Write down what you learned

You are one of many short-lived workers. Your session ends with the card and
nothing about it survives unless you record it, so anything you had to work
out the hard way will be worked out the hard way again by the next worker —
and it has been, repeatedly.

Real examples, each rediscovered more than once before a human wrote it down:
`/ssdpool` does not exist on this host and the worktree is on snarf; the
engine is reached through `dispatch_to_engine` and not by hand; a red test
gate usually means the spec was short, not that the edit was wrong.

**Before you finish — completing OR blocking — ask whether you learned
something that would have saved you time at the start. If so, record it.**
One or two lines. A fact, not a narrative. Skip it when the answer is no;
a log of "worked on card X" is worse than nothing.

Which store depends on who else needs it:

- **`mempalace`** — anything true regardless of which profile hits it: where
  something lives, how a tool actually behaves, a trap in the environment.
  This is shared across every profile and every box in the fleet, so it is
  where a fact stops being rediscovered.
- **`memory`** — habits specific to THIS profile's work. It is scoped to the
  profile (`~/.hermes/profiles/<profile>/memories/`) and invisible to the
  others, so a fleet-wide fact put here is a fact only you will ever see.

Correct an entry you find to be wrong rather than adding a second one beside
it. `coder`'s memory spent a month asserting DARKHELIX had 270 tests when it
had 624, and every worker that read it started from a false number.

## When the card needs a fact you do not have

A card that turns on how a third-party library actually behaves is the case
where guessing costs the most: the engine implements the guess, the test gate
goes red, and the attempt is spent. Look it up instead.

- **`web_search`** — for the question. Backed by a self-hosted searxng, so it
  costs nothing and leaves the network. Titles and descriptions are often
  enough to settle an API signature or a format question.
- **`browser_navigate`** then **`browser_snapshot`** — for the page itself,
  when the snippet is not enough and you need the actual documentation text.

**`web_extract` does not work here** and is not worth an attempt: every
extract-capable backend is a paid third-party API, and the searxng backend is
search-only. The browser is the way to read a page.

This is for a genuine unknown about the outside world — a library's contract,
a file format, an error message with no obvious cause. It is not a substitute
for reading the worktree, which is faster and authoritative for anything about
*this* codebase.

## Invariants

- **Never edit files yourself**, and never reach for `execute_code`,
  `write_file` or `terminal` to change repository content — not to "save
  time", not because the engine seems slow, not because you can see the fix.
  Two runs were lost exactly that way (2026-08-28, runs 4 and 6): both
  reasoned their way to editing directly, and both produced nothing
  committable. If the engine cannot do it, that is a finding to report, not a
  task to take over.
- **Never `git push` or merge.** The patch attachment plus the card's status
  is the full extent of this skill's authority. Merges wait on a human.
- One dispatch = one attempt at one card. Cross-attempt escalation is the
  dispatcher's job, not yours.
- Reading is fine and encouraged — inspect the worktree, read the failing
  test, look at the diff. The prohibition is on *changing* things.
