# Open items — Looking Glass / coder-engine model roles

Compiled on snarf 2026-08-26. Status is what was **actually verified**, not assumed.
Items are grouped by **where the fix has to happen**, because that's the blocker.

---

## 0. RESOLVED 2026-08-26 — Claude Code on CT112

Was: `/usr/bin/claude` resolved but ran a stub that errored
`claude native binary not installed` — `@anthropic-ai/claude-code` 2.1.237 was
installed globally without its platform-native optional dep.

**Fixed** with `npm install -g @anthropic-ai/claude-code --include=optional`.
`claude --version` and `claude --help` both work (exit 0).

Two things worth knowing:

- npm resolved "latest" to **2.1.197** — a *downgrade* from the broken 2.1.237.
  That build works; 2.1.237 appears to have been the bad one. If a future
  `claude update` pulls 2.1.237 again and the stub error returns, this is why.
- npm config on this box is clean (`omit` empty, `ignore-scripts` false, no
  npmrc anywhere), so the original bad install was a one-off, not a persistent
  setting that will re-break it.

**`claude-sync` cannot self-heal this.** Its CLI-update step runs
`claude update` and routes on the output; a broken CLI emits the stub error,
which matches neither the brew nor npm branch, so it falls through to an echo
and the install stays broken while the run still reports success. Worth a guard
there — see the fleet-fragility note at the end of this file.

**Editing Looking Glass:** `/opt/looking-glass` on CT112 is the only checkout —
no copy exists on the Mac, and native deps (PyAudio/torch) must build on the
deploy target. With the CLI fixed, a Claude session on CT112 is now the right
place to do it.

## 1. Fix on CT112 — `/opt/looking-glass/server/hud/coder-transit-map.js`

### 1a. Roster offers two models that cannot tool-call
`MODEL_SWITCH_TARGETS` and the Transit Map dropdown both include:

- `codestral-22b-awq` — ships no `chat_template.jinja`; its `tokenizer_config`
  template ignores `tools` entirely, so the model never sees tool definitions.
- `deepseek-r1-distill-qwen-32b-awq` — emits ```` ```json ```` blocks, which no
  registered vLLM parser matches (`deepseek_v3` expects `<|tool_call_begin|>`).

Assigning either to **editor** silently reproduces the "model rambles instead of
acting" failure. Neither is fixable at the vLLM layer — the guard belongs in the
roster. Filter them out, or tag them non-agent in the dropdown.

### 1b. STALE COMMENTS claim reviewer/orchestrator are inert — they are NOT

Corrected 2026-08-26 after reading the code. **All three roles are live:**

- `server.py:1717` — `MODEL_ROLE_LIVE = {"editor", "orchestrator", "reviewer"}`
- `dispatch_review_task.py:87` — `default=_role_default_model("reviewer")`,
  and it calls `ensure_model_loaded()` at line 50. The reviewer assignment is
  consumed by a real dispatch.
- `server.py:1852-1856` — orchestrator is overlaid from Hermes's own config on
  CT111 via `_get_orchestrator_model()`, its real live mechanism.

Two comments still say otherwise and will mislead the next person:

- `coder-transit-map.js:239-242` — "editor is the only role a real dispatch
  reads today ... vs. are just recorded for when reviewer/orchestrator get a
  real dispatch path."
- `dispatch_task.py:55-58` (on snarf) — "reviewer/orchestrator entries are
  recorded here for the HUD's benefit but aren't consumed by a live dispatch
  path yet."

The `dispatch_task.py` one was **fixed on snarf 2026-08-26** — it now spells out
all three roles, including that `orchestrator`'s entry in the file is a stale
mirror and Hermes's config on CT111 is that role's real source of truth.

**The JS one at `coder-transit-map.js:239-242` is still wrong and is CT112-scoped
— that is this session's second card.**

**Why this matters operationally:** reviewer and orchestrator are both assigned
`qwen3.6-27b-awq`, whose unit is in `failed` state (see §4). If those comments
led anyone to believe changing them is inert, a reviewer dispatch will surprise
them.

## 2. Fix on snarf — verified DONE this session

- **9 vLLM units were missing `--enable-auto-tool-choice --tool-call-parser`**
  (11 of 13 total lacked it). Tool calls returned as raw text in
  `message.content` instead of a `tool_calls` array. Parsers chosen from each
  model's `chat_template.jinja`. **Verified live on devstral-24b**:
  `finish_reason: tool_calls` with clean arguments.
- Docs corrected: `~/vllm/CLAUDE.md`, `~/CLAUDE.md`, both `hermes-handoff`
  SKILL.md copies (its triage curl hardcoded `qwen3.6-27b-awq`, which now 404s).
- `hosts/snarf/vllm/sync.sh` had `UNITS` hardcoded to 3 entries — 10 units were
  never backed up while `diff` reported "in sync". Now globs.
- Pushed as `6275a44` and `bc01592`.

---

## 3. VERIFIED 2026-08-26

### 3a. Editor role model — CONFIRMED WORKING
`qwen3.8-27b-abliterated-awq` (the `editor` assignment) tested live with a real
tool call: `finish_reason: tool_calls`, `run_shell {"command": "ls -la /tmp"}`,
empty content. The parser fix holds on the model that actually matters.

### 3b. Reviewer/orchestrator model — CONFIRMED WORKING
`qwen3.6-27b-awq` likewise returns structured `tool_calls`.

### 3c. gemma4 parser — still untested
`vllm-gemma4-31b` has `--tool-call-parser gemma4`, chosen by family match; the
emission format was never confirmed live. Not assigned to any role, so low
priority — verify before ever assigning it.

## 4. RESOLVED — the failed units were stale residue, not broken units

`vllm-qwen36-27b` was in `failed` state (`Result=timeout`, `ExecMainStatus=9`).
**Tested: it starts fine — 65 s to a serving endpoint, then `active`.** The
failed state was leftover from `vllm-switch` SIGKILLing it mid-load on Aug 22,
not a defect. Reviewer/orchestrator dispatch is not at risk from this.

Note the general behaviour: `vllm-switch` stopping a still-loading unit leaves
it in `failed`. That state is cosmetic — `systemctl start` works on a failed
unit and `ensure_model_loaded()` is unaffected. Do not read `failed` in
`systemctl list-units 'vllm*'` as "this model is broken".

`vllm-kimi-linear-48b` is still `failed` from the same cause and has **not**
been start-tested. It is assigned to no role, so it is not blocking anything.

## Correction to carry forward

An earlier note (since fixed in both CLAUDE.md files) framed per-role selection
as an unresolved design tension — "cannot mean concurrent models". That was
wrong about the code. `dispatch_task.py`'s `ensure_model_loaded()` already
serialises correctly: it reads `/v1/models`, runs `vllm-switch` on mismatch, and
blocks until the new model is served, with stall-kicks. The design is sound; the
only real constraint is that `MODEL_SWITCH_TARGETS` must stay in sync with
`vllm-switch` and each unit's `--served-model-name` (verified complete for all
13 models on 2026-08-26).

---

## 5. Orchestrator 404 — FIXED 2026-08-26, and what it cost out to

**Was:** `kanban_decomposer` in `/root/.hermes/config.yaml` (CT111/hermes) set
`base_url: http://192.168.1.239:8000/v1` — snarf direct — with a hardcoded
`model: qwen3.6-27b-awq`. snarf serves one model at a time, so any other model
being loaded produced `404 The model 'qwen3.6-27b-awq' does not exist`. Nothing
on the orchestrator path calls `ensure_model_loaded()`; only `dispatch_task.py`
and `dispatch_review_task.py` do.

**Fixed** by pointing it at `http://127.0.0.1:8010/v1` — the local
`model_proxy.py` (`hermes-model-proxy.service`), which was built for exactly
this and rewrites `model` to whatever the backend actually serves. Verified: a
request naming `qwen3.6-27b-awq` returned 200 from
`qwen3.8-27b-abliterated-awq`. Config backed up as `config.yaml.bak-proxy-*`.

**Consequence for the HUD:** the orchestrator's model name is now **advisory**.
`_get_orchestrator_model()` reports `qwen3.6-27b-awq`, but the proxy rewrites it
at call time. The orchestrator dropdown should be labelled advisory — pair this
with the roster-guard card.

### Costing: making the proxy honour the requested model ("option 1")

Prerequisite already in place — the `hermes` account on snarf has
`NOPASSWD: /usr/local/bin/vllm-switch` and `systemctl start|stop|restart|status
vllm-*`, and root@hermes can SSH to snarf as both `hermes` and `sam`.

| Phase | Work | Est. |
|---|---|---|
| A | `vllm-ensure <model>` broker on snarf: `flock`, check `/v1/models`, switch, block until ready. Single switch path for all callers. ~80 lines + sudoers entry. | 3–4h |
| B | Proxy honours requested model instead of rewriting; `threading.Lock` against stampede; keep the Mistral `reasoning_effort` strip. ~40 lines of 128. | 2–3h |
| C | `kanban_decomposer` `timeout: 180` vs a 55–90 s cold swap + generation. Raise it, or fast-503 "swapping, retry". | 1h |
| D | Concurrency proof: decompose during an editor dispatch, no 404, no thrash. | 1–2h |

**≈1–1.5 days.** **Do not start this without reading §6 first** — it exists only
to prop up a design that may not be worth keeping.

---

## 6. Step back: is per-role model selection the right design?

**The premise is inverted.** The design promises a model per role; the hardware
provides one model at a time (48 GB VRAM, one port). Everything in §1–§5 is
scaffolding to simulate a capability that does not exist:

- `vllm-switch` + `ensure_model_loaded()` — emulating concurrency by time-slicing
- `MODEL_SWITCH_TARGETS` — a third copy of a mapping that also lives in
  `vllm-switch` and in each unit's `--served-model-name`
- `model_proxy.py` — papering over the mismatch by rewriting the model name
- no mutex anywhere — the emulation's unhandled race, now with three callers
- today's 404 — the emulation leaking

**What the scaffolding buys, measured from your own eval** (`eval_results.json`,
n=207, swept 2026-08-21/22):

| model | editor pass rate |
|---|---|
| qwen3.6-27b-awq | 24/36 — 66.7% |
| qwen3.8-27b-abliterated-awq | 10/16 — 62.5% |
| devstral-small-2-24b-awq | 20/33 — 60.6% |
| deepseek-r1-distill-qwen-32b-awq | 22/40 — 55.0% |
| qwen3.8-27b-int8-w8a16 | 13/24 — 54.2% |
| qwen2.5-coder-32b-instruct-awq | 4/8 — 50.0% |
| qwen3-coder-30b-a3b-awq | 16/32 — 50.0% |
| codestral-22b-awq | 4/16 — 25.0% |

Top five sit inside ~12 pp on samples of 8–40 attempts — differences well inside
noise. **And the model currently assigned to editor (62.5%) is not the top
scorer (66.7%).** The machinery selects between options its own data cannot
separate, and does not select the best one.

Caveat: `graph.py`/`harness.py` use no native `tool_calls`, so these scores are
NOT invalidated by the pre-2026-08-26 missing parsers. But it does mean the eval
measures a text-based editing path while live dispatch uses tool calls — the
scores are not a direct proxy for live behaviour either way.

### Two coherent designs; the current one is neither

**A. Make "one model" true.** Pick one, delete switching from the dispatch path.
Removes the mutex problem, the cold swaps, the 404 class, the proxy's rewriting,
and `MODEL_SWITCH_TARGETS`. **Makes §5's 1–1.5 days unnecessary.**

**B. Make "concurrent" true.** Run 2–3 small models on fixed ports at once —
48 GB holds e.g. `qwen3-coder-30b-a3b` (17 G) + `qwen3-14b` (9.4 G) with
headroom. Per-role becomes real and instant, no switching, no broker. Cost:
smaller models per role.

**Recommendation: A now.** Revisit B only if a re-run eval with adequate n shows
a real per-role separation that beats the best single model. The §5 broker is
worth building under neither design — it exists only to make the emulation
correct rather than to remove the need for it.
