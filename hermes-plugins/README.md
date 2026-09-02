# Hermes-side components (deployed to CT111)

> **The backup of record moved, 2026-09-01.** These files are now captured in
> `claude-config/hosts/hermes/` — `plugins/<name>/`, the profile configs, and
> `config.yaml` — by `hosts/hermes/sync.sh`, run from this box (CT112 is the
> only node with both a checkout of that repo and a route to CT111). Back up
> with `./sync.sh backup`, and check drift with `./sync.sh diff`.
>
> This directory stays as the **explanation**: the write-ups below are why each
> plugin exists and what it cost to learn, which is not something a redacted
> config dump carries. The code here is a reading copy. When the two disagree,
> `sync.sh diff` against the live box settles it — not this directory.
>
> Keeping one box's config in two repos is what made the Hermes plugins the
> only part of CT111 with no real backup for three weeks.

These are **not loaded from this repo**. They live on CT111 under
`~/.hermes/`. Editing a file here changes nothing until it is deployed.

| file | deployed to | what it does |
|---|---|---|
| `darkhelix-isolation.py` | `~/.hermes/plugins/darkhelix-isolation/__init__.py` | `kanban_task_claimed` hook → calls this HUD's `/api/kanban/provision`, so every card gets an isolated worktree regardless of who created it |
| `darkhelix-engine.py` | `~/.hermes/plugins/darkhelix-engine/__init__.py` | registers the `dispatch_to_engine` tool — the whole engine round trip as code |
| `darkhelix-triage-guard.py` | `~/.hermes/plugins/darkhelix-triage-guard/__init__.py` | `kanban_task_blocked` hook → parks a loop-broken card in `blocked` instead of `triage`, so the auto-decomposer cannot fan it out again before a human sees it |
| `darkhelix-triage-guard.plugin.yaml` | `~/.hermes/plugins/darkhelix-triage-guard/plugin.yaml` | its manifest — **without a `plugin.yaml` the loader silently ignores the directory**, and `hermes plugins list` simply does not mention it |
| `coder-profile-config.yaml` | `~/.hermes/profiles/coder/config.yaml` | the assignee profile: enables the `darkhelix` plugin and toolset for workers |
| `execution-engine-dispatch.SKILL.md` | `~/.hermes/profiles/coder/skills/software-development/execution-engine-dispatch/SKILL.md` | what is left for the worker once the mechanics are a tool: diagnosis |

All three plugins are enabled in `~/.hermes/config.yaml` under `plugins.enabled`.
`darkhelix-isolation` needs a `config.json` beside it holding the HUD URL and
token (mode 600, not mirrored here — it is a secret).
`darkhelix-triage-guard` is additionally enabled in **every** profile that works
cards (coder, darkhelix, bioinformatics, researcher, sysadmin, ai-tune), with a
`plugins/` symlink in each — see the per-HERMES_HOME trap below; the block it
has to catch is issued by the worker, not the gateway.

## Why the triage guard exists (2026-09-01)

`block_task` treats the second re-block for the same cause as an unblock loop
and routes the card to `triage` — its comment says "for a human-in-the-loop
decision", on the assumption that `blocked` is where a cron spins and `triage`
is where a human looks. Here that is inverted: nothing auto-drains `blocked`
(no unblock cron on CT111), while `triage` is drained by the gateway's
`_auto_decompose_tick` every 60s, and `decompose_task` guards only on
`status == 'triage'` — it never checks whether the card was already decomposed.

t_d17fef80 paid for it: `block_loop_detected` at 20:44, `decomposed` at 20:45
into four children that re-asked the four questions the 12:47 fan-out had
already answered — 2h37m of worker time, and the two waves returned opposite
answers to the same patch-vs-rebuild question. Reproduced again while testing
this guard: a card left in `triage` was picked up by the decomposer, had its
title and body rewritten by `specify`, was promoted, and had a worker spawned
on it, all inside one 60s tick.

The guard does not touch `auto_decompose`. A genuinely new card still fans out
on the next tick, which is the behaviour that produced this board's useful work.

**Testing it:** a plain `hermes kanban block` from the shell will NOT fire the
hook — `discover_plugins()` runs on agent/gateway startup paths (`cli.py`,
`gateway/run.py`), not in a short-lived CLI invocation, so the plugin manager
in that process is empty. Drive `kb.block_task` from one Python process that
called `discover_plugins()` first; that is what a worker session
(`hermes -p coder --cli … chat`) actually is.

## Two traps that cost a full debug cycle each (2026-08-28)

**Plugins are discovered per-HERMES_HOME.** `get_hermes_home()/plugins` — and a
kanban worker runs with the *assignee profile* as its home, not the root one.
A plugin installed only at `~/.hermes/plugins/` is therefore invisible to any
worker. The failure is silent and looks like nothing is wrong: the toolset name
still resolves, `validate_toolset()` still returns True, and the worker is
handed a toolset whose tool does not exist in its registry. It reported
"dispatch_to_engine is not available in the current toolset" and there was no
error anywhere to explain why.

So a plugin a WORKER needs must be visible from the profile:

    ~/.hermes/profiles/coder/plugins/darkhelix-engine -> ~/.hermes/plugins/darkhelix-engine

plus `plugins.enabled` in that profile's `config.yaml`. The symlink keeps one
copy to maintain. `darkhelix-isolation` needs none of this only because it runs
in the *gateway*, which uses the root home.

**Plugin toolsets are not auto-enabled.** The dispatcher pins the worker's tool
surface at spawn time from the assignee profile
(`kanban_db._resolve_worker_cli_toolsets`), so a toolset not named there is
simply absent. The coder profile therefore sets:

    platform_toolsets:
      cli: [hermes-cli, darkhelix]

And the card has to be ASSIGNED to that profile in the first place: cards filed
with no `--assignee` land on `default`, which has neither the skill nor the
toolset. `/api/kanban/create` now passes one (`darkhelix.assignee`, default
`coder`).

Verify all three at once by spawning a real card and reading the worker's
command line — it must show `-p coder` and `darkhelix` in `--toolsets` — then
its log, which must show `dispatch_to_engine` actually called.

**Deploying a change**: copy the file to its path on CT111, then restart the
gateway so it reloads —
`XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart hermes-gateway`.
Plugins are discovered once at gateway startup. Workers are spawned fresh per
card, so a tool change reaches them on the next card without a restart; the
claim hook runs *in* the gateway and does need one.

See [../docs/TASK-ISOLATION.md](../docs/TASK-ISOLATION.md) for why these exist.

## Two more things that are per-profile and fail silently

Same trap as the plugin scoping above, found 2026-08-29 while making web access
and sequential execution actually work. Both looked configured and were not.

**Delegation caps are read from the PROFILE config, not `~/.hermes/config.yaml`.**
`delegate_tool._get_max_concurrent_children()` calls `_load_config()`, which
follows the active `HERMES_HOME`. A worker runs `-p coder`, so it reads
`profiles/coder/config.yaml`. Setting `delegation.max_concurrent_children: 1` in
the root config left every worker on the built-in default of **3** — the root
home resolved to 1 and the coder profile to 3 at the same moment. There is no
warning; the key is simply absent and the default applies. The caps now live in
each profile's own `delegation:` block:

    delegation:
      max_concurrent_children: 1
      max_spawn_depth: 1
      max_async_children: 1

`darkhelix` was the sharper version of this: it already *had* a `delegation:`
block, so "is the key there" was the wrong check — it carried
`max_concurrent_children: 3` explicitly and had to be edited, not appended to.
Verify per profile, never once:

    from hermes_constants import set_hermes_home_override
    set_hermes_home_override("/root/.hermes/profiles/<p>")
    from tools.delegate_tool import _get_max_concurrent_children as g; g()

Note `kanban.max_in_progress` is the opposite case — it is read by the
*dispatcher*, once, at gateway startup (outside the tick loop in
`gateway/kanban_watchers.py:_kanban_dispatcher_watcher`), so it needs a gateway
restart and lives in the root config. Two different caps, two different homes,
two different reload rules.

**The browser needs Chrome under the profile's HOME.** Kanban workers get the
`browser` toolset pinned into their surface, but `browser_navigate` returned
"Chrome not found", listing `<profile>/home/.agent-browser/browsers` and the
Playwright cache among the paths checked. The profile HOME is remapped to
`~/.hermes/profiles/<p>/home`, so the Playwright install under real root's
`~/.cache/ms-playwright` was invisible — advertised tool, broken at call time,
exactly the failure mode this file already warns about. Shared rather than
re-downloaded per profile (379M each):

    ln -s /root/.cache/ms-playwright <profile>/home/.cache/ms-playwright

Done for all six profiles. This matters because `web_extract` cannot cover it:
the only extract-capable providers (firecrawl, tavily, exa, parallel) are paid
third-party APIs, while the self-hosted searxng backend is search-only
(`supports_extract()` returns False). So `web_search` answers questions and the
browser is the only way to pull a page's actual text. Verified end-to-end:
`browser_navigate` + `browser_snapshot` returned 15.8K chars of real page
content under both the coder and darkhelix profiles.
