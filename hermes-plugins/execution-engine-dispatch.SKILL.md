---
name: execution-engine-dispatch
description: "Run a kanban card through the LangGraph+Aider execution engine on snarf via the dispatch_to_engine tool, and diagnose the result."
version: 2.0.0
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
