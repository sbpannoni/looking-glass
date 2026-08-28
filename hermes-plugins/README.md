# Hermes-side components (deployed to CT111, mirrored here)

These are **not loaded from this repo**. They live on CT111 under
`~/.hermes/`, which has no version control, so copies are kept here so the
mechanism is reviewable and recoverable. Editing a file here changes nothing
until it is deployed.

| file | deployed to | what it does |
|---|---|---|
| `darkhelix-isolation.py` | `~/.hermes/plugins/darkhelix-isolation/__init__.py` | `kanban_task_claimed` hook → calls this HUD's `/api/kanban/provision`, so every card gets an isolated worktree regardless of who created it |
| `darkhelix-engine.py` | `~/.hermes/plugins/darkhelix-engine/__init__.py` | registers the `dispatch_to_engine` tool — the whole engine round trip as code |
| `execution-engine-dispatch.SKILL.md` | `~/.hermes/profiles/coder/skills/software-development/execution-engine-dispatch/SKILL.md` | what is left for the worker once the mechanics are a tool: diagnosis |

Both plugins are enabled in `~/.hermes/config.yaml` under `plugins.enabled`.
`darkhelix-isolation` needs a `config.json` beside it holding the HUD URL and
token (mode 600, not mirrored here — it is a secret).

**Deploying a change**: copy the file to its path on CT111, then restart the
gateway so it reloads —
`XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart hermes-gateway`.
Plugins are discovered once at gateway startup. Workers are spawned fresh per
card, so a tool change reaches them on the next card without a restart; the
claim hook runs *in* the gateway and does need one.

See [../docs/TASK-ISOLATION.md](../docs/TASK-ISOLATION.md) for why these exist.
