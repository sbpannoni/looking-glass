# Hermes-side components (deployed to CT111, mirrored here)

These are **not loaded from this repo**. They live on CT111 under
`~/.hermes/`, which has no version control, so copies are kept here so the
mechanism is reviewable and recoverable. Editing a file here changes nothing
until it is deployed.

| file | deployed to | what it does |
|---|---|---|
| `darkhelix-isolation.py` | `~/.hermes/plugins/darkhelix-isolation/__init__.py` | `kanban_task_claimed` hook → calls this HUD's `/api/kanban/provision`, so every card gets an isolated worktree regardless of who created it |
| `darkhelix-engine.py` | `~/.hermes/plugins/darkhelix-engine/__init__.py` | registers the `dispatch_to_engine` tool — the whole engine round trip as code |
| `coder-profile-config.yaml` | `~/.hermes/profiles/coder/config.yaml` | the assignee profile: enables the `darkhelix` plugin and toolset for workers |
| `execution-engine-dispatch.SKILL.md` | `~/.hermes/profiles/coder/skills/software-development/execution-engine-dispatch/SKILL.md` | what is left for the worker once the mechanics are a tool: diagnosis |

Both plugins are enabled in `~/.hermes/config.yaml` under `plugins.enabled`.
`darkhelix-isolation` needs a `config.json` beside it holding the HUD URL and
token (mode 600, not mirrored here — it is a secret).

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
