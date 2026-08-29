"use strict";
/* ============================== KANBAN =============================
   The view for managing agentic work.

   The HUD's own chat session is not where work happens — each kanban card
   runs in its own Hermes session and workspace. This shows the board, and
   lets you open any task's live run log, which is the actual "what is the
   agent doing right now" view.

   LANES, not a list. /api/kanban returns `columns` already grouped and
   ordered by the board's own status model (triage -> todo -> scheduled ->
   ready -> running -> blocked -> review -> done). A single column of
   full-width cards spent 1200px of width on three lines of text and still
   needed 1400px of scroll for 13 cards; as lanes the whole board fits on
   screen. Empty lanes collapse to a rail so the occupied ones get the width
   — with five of eight statuses empty, that is the difference between
   fitting and not.

   Cards are reconciled by id rather than re-rendered wholesale: replacing
   .innerHTML on every 15s poll destroyed scroll position, which made any
   card below the fold unreadable if you read slowly.

   Depends on $/openWorkTab from app.js.
=================================================================== */
const KANBAN_STATUS_CLASS = {
  running:"ok", done:"ok", ready:"warn", todo:"", blocked:"err",
  review:"warn", scheduled:"", triage:"warn", archived:"",
};

/* Task-log pane is blank until a card is actually dispatched — there is no
   run log file for a card still sitting in --triage. That reads as "stuck"
   with nothing else on screen, so the status header spells out what's
   actually happening at each stage (mirrors kanban_create's own module note
   in server.py: triage -> the specifier decomposes it -> dispatcher picks
   it up -> only then does a real log start). */
const KANBAN_STATUS_MSG = {
  triage: "queued for triage — waiting on Hermes's specifier to decompose it",
  todo: "specified — waiting for the dispatcher to pick it up",
  ready: "ready — waiting for the dispatcher to pick it up",
  scheduled: "scheduled — waiting for its window",
  blocked: "blocked — needs attention",
  running: "running — live log below",
  // (a running card that is actually wedged is recoverable: see Reclaim)
  review: "awaiting review",
  done: "done",
  archived: "archived",
};

/* Lane order for the ssh fallback, which returns a flat list and no columns.
   Matches the plugin API's own ordering so the board looks the same
   whichever transport served it. */
const KANBAN_LANE_ORDER = ["triage","todo","scheduled","ready","running","blocked","review","done","archived"];

/* Card buttons, as data. The click handler used to branch on a single
   "is it unblock?" boolean, which is why adding a third action needed this
   table rather than a third nested ternary. */
const KB_CARD_ACTIONS = {
  unblock: {endpoint:"/api/kanban/unblock", verb:"Unblock"},
  archive: {endpoint:"/api/kanban/archive", verb:"Archive"},
  reclaim: {endpoint:"/api/kanban/reclaim", verb:"Reclaim"},
};

const KB_COLLAPSED_KEY = "lg-kb-collapsed";
const KB_ASSIGNEE_KEY  = "lg-kb-assignee";

function kbPrefLoad(key, fallback){
  try{ const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); }
  catch{ return fallback; }
}
function kbPrefSave(key, value){
  try{ localStorage.setItem(key, JSON.stringify(value)); }catch{ /* storage disabled */ }
}

function kanbanEsc(s){
  return (s||"").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
}

function kanbanAge(task){
  const t = task.completed_at || task.started_at || task.created_at;
  if(!t) return "";
  // Hermes returns Unix SECONDS. Feeding those to new Date() treats them as
  // milliseconds and reports every card as ~56 years old.
  const epochMs = typeof t === "number" ? t*1000 : new Date(t).getTime();
  const ms = Date.now() - epochMs;
  if(isNaN(ms)) return "";
  const m = Math.floor(ms/60000);
  if(m < 1) return "just now";
  if(m < 60) return m+"m";
  const h = Math.floor(m/60);
  return h < 24 ? h+"h" : Math.floor(h/24)+"d";
}

/* How long a card has actually been running, in minutes. Distinct from
   kanbanAge, which reports whichever timestamp is newest — for a running
   card that is started_at, but the two diverge for every other status and
   only the running clock says anything about being stuck. */
function kbRunningMinutes(task){
  if(task.status !== "running" || !task.started_at) return 0;
  const t = task.started_at;
  const epochMs = typeof t === "number" ? t*1000 : new Date(t).getTime();
  if(isNaN(epochMs)) return 0;
  return Math.floor((Date.now() - epochMs)/60000);
}

/* Past this, a running card is presumed wedged rather than working. Cards
   carry a max-runtime the dispatcher enforces on its own tick; a card that
   is well past any plausible cap is exactly the one nothing else will move.
   It changes presentation only — Reclaim is offered on every running card. */
const KB_STUCK_MINUTES = 30;

async function kanbanCardAction(panel, endpoint, verb, id, btn){
  btn.disabled = true;
  btn.textContent = verb + "ing…";
  try{
    const r = await fetch(endpoint, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({task_id: id}),
    });
    const j = await r.json();
    if(!j.ok){ btn.disabled = false; btn.textContent = verb + " failed — retry"; return; }
    refreshKanbanPanel(panel);
  }catch(err){ btn.disabled = false; btn.textContent = verb + " failed — retry"; }
}

/* ------------------------------ lanes -------------------------------- */

/* Group a flat task list into the same shape /api/kanban's `columns` has, so
   the renderer never has to care which transport answered. */
function kanbanColumnsFromTasks(tasks){
  const by = new Map();
  tasks.forEach(t => {
    const k = t.status || "todo";
    if(!by.has(k)) by.set(k, []);
    by.get(k).push(t);
  });
  const known = KANBAN_LANE_ORDER.filter(n => by.has(n)).map(n => ({name:n, tasks:by.get(n)}));
  // A status this list doesn't know about still gets a lane rather than
  // vanishing off the board.
  const extra = [...by.keys()].filter(n => !KANBAN_LANE_ORDER.includes(n))
                              .map(n => ({name:n, tasks:by.get(n)}));
  return known.concat(extra);
}

/* Everything a card displays. If this string is unchanged the card's DOM is
   left completely alone — that is what keeps scroll and hover stable across
   a poll. */
function kbCardSignature(t){
  const lc = t.link_counts || {};
  const pr = t.progress || {};
  return [t.status, t.title, t.assignee, t.comment_count, t.completed_at,
          t.started_at, t.block_kind, t.last_failure_error,
          lc.parents, lc.children, pr.done, pr.total].join("|");
}

/* ---- dependency state -------------------------------------------------
   A decomposed card's position in the graph was invisible here. The
   decomposer writes plain descriptive titles ("Decide amplicon sourcing
   strategy", "Implement GFF-based sequence extraction") with no ordering
   hint, and the board rendered only title/assignee/age — so a card sitting
   in `todo` BECAUSE it is waiting on an unfinished parent looked exactly
   like one that is merely queued. You had to run `hermes kanban show` per
   card to find the order.

   Hermes holds a child in `todo` while any parent is open and promotes it to
   `ready` once they all close (the same machinery `block --kind dependency`
   relies on). So `todo` + parents > 0 IS "blocked on dependencies" — no
   extra request needed, the board already sends link_counts and progress. */
function kbDepChips(t){
  const lc = t.link_counts || {};
  const parents = lc.parents || 0;
  const pr = t.progress || null;
  const chips = [];
  if(parents){
    // In `todo` the parents are the reason it is not running. Anywhere else
    // they are just provenance, so say it quietly.
    const waiting = t.status === "todo";
    chips.push(`<span class="kb-chip kb-dep${waiting ? " waiting" : ""}"
      title="${waiting
        ? `Waiting on ${parents} unfinished card${parents === 1 ? "" : "s"} — it cannot start until they finish`
        : `Depends on ${parents} earlier card${parents === 1 ? "" : "s"}`}"
      >${waiting ? "⛓" : "↳"} ${parents}</span>`);
  }
  if(pr && pr.total){
    const done = pr.done === pr.total;
    chips.push(`<span class="kb-chip kb-kids${done ? " ok" : ""}"
      title="${pr.done} of ${pr.total} child card${pr.total === 1 ? "" : "s"} done"
      >${pr.done}/${pr.total}</span>`);
  }
  return chips.join("");
}

function kbCardInner(t){
  const comments = t.comment_count ? `<span class="kb-chip">${t.comment_count}c</span>` : "";
  // The lane header already says what the status is, so the card doesn't
  // repeat it — that word was most of the old card's height.
  const note = (t.status === "blocked" && t.last_failure_error)
    ? `<div class="kb-note">${kanbanEsc(String(t.last_failure_error)).slice(0,140)}</div>`
    : "";
  // Every non-terminal lane needs SOME way out of it by hand, or a card that
  // wedges there is unrecoverable from the HUD. `running` had none: when a
  // worker dies mid-run the claim is never released and the card sits in the
  // running lane indefinitely (the board still shows one from five days ago).
  //
  // Reclaim is not a park button. Per hermes_cli/kanban_db.py:reclaim_task it
  // SIGTERM/SIGKILLs the worker, clears the claim, and sets the card to
  // `ready` -- which is DISPATCHABLE, so the gateway starts a fresh run on its
  // next pass and spends another model run. The tooltip says so, because
  // "reclaim" on its own sounds free.
  const action = t.status === "blocked"
    ? `<button class="btn kb-card-btn" data-action="unblock" data-id="${kanbanEsc(t.id)}">Unblock</button>`
    : t.status === "done"
      ? `<button class="btn kb-card-btn" data-action="archive" data-id="${kanbanEsc(t.id)}">Archive</button>`
      : t.status === "running"
        ? `<button class="btn kb-card-btn" data-action="reclaim" data-id="${kanbanEsc(t.id)}"
             title="Kill this card's worker and reset it to ready — the dispatcher then starts a FRESH run on its next pass, which costs another model run. Use when a run is wedged: the worker died, the runtime cap fired, or the model endpoint went away, and the card is still marked running. Does not count as a failure, so the retry limit is unaffected.">Reclaim</button>`
        : "";
  return `<div class="kb-title">${kanbanEsc(t.title)}</div>
    ${note}
    <div class="kb-meta"><span class="kb-who">${kanbanEsc(t.assignee) || "—"}</span>
      <span class="kb-meta-r">${kbDepChips(t)}${comments}<span class="kb-age">${kanbanAge(t)}</span></span></div>
    ${action}`;
}

/* Reconcile one lane's cards in place: update what changed, move what moved,
   remove what's gone. Never rebuilds the list wholesale. */
function kbSyncLane(listEl, tasks){
  const existing = new Map();
  listEl.querySelectorAll(".kb-card").forEach(el => existing.set(el.dataset.id, el));
  let prev = null;
  tasks.forEach(t => {
    let el = existing.get(t.id);
    const sig = kbCardSignature(t);
    if(!el){
      el = document.createElement("div");
      el.className = "kb-card";
      el.dataset.id = t.id;
      el.dataset.sig = sig;
      el.innerHTML = kbCardInner(t);
    }else{
      existing.delete(t.id);
      if(el.dataset.sig !== sig){
        el.dataset.sig = sig;
        el.innerHTML = kbCardInner(t);
      }
    }
    el.classList.toggle("live", t.status === "running");
    el.classList.toggle("archived", t.status === "archived");
    // Toggled outside the signature gate on purpose: elapsed time crosses the
    // threshold with no field on the card changing, so a signature-gated
    // rebuild would never notice it.
    el.classList.toggle("stuck", kbRunningMinutes(t) >= KB_STUCK_MINUTES);
    // insertBefore on a node already in position is a no-op, so a steady
    // board does no DOM work at all — and no DOM work means no scroll jump.
    const want = prev ? prev.nextSibling : listEl.firstChild;
    if(el !== want) listEl.insertBefore(el, want);
    prev = el;
  });
  existing.forEach(el => el.remove());
}

function kbLaneEl(lanesEl, name){
  const found = lanesEl.querySelector(`.kb-lane[data-status="${name}"]`);
  if(found) return found;
  const lane = document.createElement("div");
  lane.className = "kb-lane";
  lane.dataset.status = name;
  lane.innerHTML = `<div class="kb-lane-head" title="Collapse or expand this lane">
      <span class="kb-lane-name">${kanbanEsc(name)}</span>
      <span class="kb-lane-count">0</span>
    </div>
    <div class="kb-lane-list"></div>`;
  lanesEl.appendChild(lane);
  return lane;
}

function renderKanban(panel, board, err){
  const lanesEl = panel.querySelector(".kb-lanes");
  const sourceEl = panel.querySelector(".kb-source");
  if(err){
    // A failed poll must not destroy a board that is already on screen.
    //
    // This used to replace every lane with the error text, so one transient
    // blip wiped the board -- and it stayed wiped until a later poll
    // succeeded, up to 15s of showing nothing. The common cause is not the
    // board being down at all: restarting looking-glass.service kills every
    // in-flight fetch, and "Failed to fetch" is what the browser calls that.
    // The board is what you watch WHILE work runs, so the last known good
    // state is far more useful than an error where the cards were.
    //
    // First load is the exception: there is nothing to preserve, so the error
    // is the only thing worth showing.
    const hasCards = lanesEl.querySelector(".kb-card");
    if(hasCards){
      panel.classList.add("kb-stale");
      if(sourceEl){
        sourceEl.innerHTML = `<span class="warn" title="${kanbanEsc(err)}">` +
          `last poll failed — showing the last known board, retrying</span>`;
      }
    }else{
      lanesEl.innerHTML = `<div class="kb-board-err"><span class="err">board unavailable: ${kanbanEsc(err)}</span></div>`;
      if(sourceEl) sourceEl.textContent = "";
    }
    return;
  }
  // Any successful render clears the stale marker.
  panel.classList.remove("kb-stale");
  const tasks = board.tasks || [];
  const columns = (board.columns && board.columns.length)
    ? board.columns : kanbanColumnsFromTasks(tasks);

  // Assignee options come from the board itself, so they can't go stale.
  const sel = panel.querySelector(".kb-assignee");
  const assignees = (board.assignees && board.assignees.length)
    ? board.assignees
    : [...new Set(tasks.map(t => t.assignee).filter(Boolean))].sort();
  const wantOpts = assignees.join("|");
  if(sel.dataset.opts !== wantOpts){
    sel.dataset.opts = wantOpts;
    const keep = sel.value;
    sel.innerHTML = `<option value="">everyone</option>` +
      assignees.map(a => `<option value="${kanbanEsc(a)}">${kanbanEsc(a)}</option>`).join("");
    sel.value = assignees.includes(keep) ? keep : "";
  }
  const filter = sel.value;

  const collapsed = kbPrefLoad(KB_COLLAPSED_KEY, {});
  let shown = 0;
  columns.forEach(col => {
    const lane = kbLaneEl(lanesEl, col.name);
    const list = filter
      ? (col.tasks || []).filter(t => t.assignee === filter)
      : (col.tasks || []);
    lane.querySelector(".kb-lane-count").textContent = list.length;
    // Auto: an empty lane collapses to a rail so the occupied lanes get the
    // width. An explicit click overrides that, in either direction.
    const override = collapsed[col.name];
    const rail = override === undefined ? list.length === 0 : override;
    lane.classList.toggle("rail", rail);
    // Sync even a railed lane. Its list is display:none, so this costs
    // nothing visually, but skipping it left the cards of a lane that had
    // just been filtered or collapsed sitting stale in the DOM — hidden, yet
    // still matching every query over .kb-card.
    kbSyncLane(lane.querySelector(".kb-lane-list"), list);
    shown += list.length;
  });
  // A lane the board has stopped reporting.
  const names = new Set(columns.map(c => c.name));
  lanesEl.querySelectorAll(".kb-lane").forEach(l => {
    if(!names.has(l.dataset.status)) l.remove();
  });

  if(sourceEl){
    // Say so when the board came from the ssh fallback: the cards are real
    // either way, but the columns and the richer fields are not there.
    const degraded = board.source === "ssh"
      ? ` <span class="warn" title="${kanbanEsc(board.api_error || "")}">· ssh fallback</span>`
      : "";
    sourceEl.innerHTML = `${shown} card${shown === 1 ? "" : "s"}${degraded}`;
  }
}

/* ---- global dispatch stop -------------------------------------------
   `hermes pause` halts NEW dispatch only: the dispatcher checks it every tick
   BEFORE spawning, in-flight workers are never killed, and cards stay `ready`
   so resuming continues exactly where it stopped. That is the right tool for
   "stop the pipeline" and it was previously only reachable from a shell on
   CT111 -- so the way to stop runaway work FROM THE BOARD was to reclaim
   cards one at a time, killing their workers and losing what they had done.

   A paused board otherwise looks identical to an idle one, which is its own
   trap, so the state is shown as a banner rather than only on the button. */
async function refreshKanbanPause(panel){
  const btn = panel.querySelector(".kb-pause");
  const banner = panel.querySelector(".kb-paused-banner");
  if(!btn || !banner) return;
  try{
    const r = await fetch("/api/kanban/pause");
    const j = await r.json();
    const paused = !!j.paused;
    btn.dataset.paused = paused ? "1" : "";
    btn.textContent = paused ? "▶ resume dispatch" : "⏸ pause dispatch";
    btn.classList.toggle("on", paused);
    banner.hidden = !paused;
    if(paused){
      banner.innerHTML = `<b>DISPATCH PAUSED</b> — no new workers will start. ` +
        `In-flight work continues and cards stay ready.` +
        (j.reason ? ` <span class="kb-paused-why">${kanbanEsc(j.reason)}</span>` : "");
    }
  }catch{ /* a failed state read must not disturb the board */ }
}

async function refreshKanbanPanel(panel){
  refreshKanbanPause(panel);
  try{
    const r = await fetch("/api/kanban");
    const j = await r.json();
    renderKanban(panel, j, j.error);
  }catch(err){ renderKanban(panel, {}, err.message); }
}

function openKanbanBoard(){
  openWorkTabTurning("kanban","board","KANBAN",(panel,tab)=>{
    panel.innerHTML = `<div class="kb-head">
        <span class="kb-head-title">BOARD</span>
        <span class="kb-head-sub">click a card for its run log · ⛓ = waiting on unfinished parents · n/m = children done</span>
        <span class="kb-head-spacer"></span>
        <label class="kb-filter">assignee <select class="kb-assignee"></select></label>
        <button class="kb-pause" type="button" title="Halt NEW dispatch. In-flight workers are never killed and cards stay ready, so resuming picks up exactly where it left off.">⏸ pause dispatch</button>
        <span class="kb-source">loading…</span>
      </div>
      <div class="kb-paused-banner" hidden></div>
      <div class="kb-lanes"></div>`;
    panel.classList.add("kanban-pane");

    // One delegated listener for the whole board — card clicks, action
    // buttons and lane collapse. Re-binding per card on every poll was both
    // wasteful and a way to leak handlers onto reused nodes.
    const lanesEl = panel.querySelector(".kb-lanes");
    lanesEl.addEventListener("click", (e) => {
      const btn = e.target.closest(".kb-card-btn");
      if(btn){
        e.stopPropagation();
        const act = KB_CARD_ACTIONS[btn.dataset.action];
        if(act) kanbanCardAction(panel, act.endpoint, act.verb, btn.dataset.id, btn);
        return;
      }
      const head = e.target.closest(".kb-lane-head");
      if(head){
        const lane = head.closest(".kb-lane");
        const collapsed = kbPrefLoad(KB_COLLAPSED_KEY, {});
        collapsed[lane.dataset.status] = !lane.classList.contains("rail");
        kbPrefSave(KB_COLLAPSED_KEY, collapsed);
        refreshKanbanPanel(panel);
        return;
      }
      const card = e.target.closest(".kb-card");
      if(card) openTaskLog(card.dataset.id);
    });

    const pauseBtn = panel.querySelector(".kb-pause");
    pauseBtn.onclick = async () => {
      // Send the explicit target state, never a toggle: a toggle read off a
      // stale board does the opposite of what was intended, and this is the
      // control you reach for when something is already going wrong.
      const want = pauseBtn.dataset.paused !== "1";
      pauseBtn.disabled = true;
      const prev = pauseBtn.textContent;
      pauseBtn.textContent = want ? "pausing…" : "resuming…";
      try{
        const r = await fetch("/api/kanban/pause", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({paused: want,
                                reason: "paused from the Looking Glass board"}),
        });
        const j = await r.json();
        if(!j.ok) pauseBtn.textContent = prev + " — failed";
      }catch{ pauseBtn.textContent = prev + " — failed"; }
      pauseBtn.disabled = false;
      refreshKanbanPause(panel);
      refreshKanbanPanel(panel);
    };

    const sel = panel.querySelector(".kb-assignee");
    sel.value = kbPrefLoad(KB_ASSIGNEE_KEY, "") || "";
    sel.onchange = () => { kbPrefSave(KB_ASSIGNEE_KEY, sel.value); refreshKanbanPanel(panel); };

    refreshKanbanPanel(panel);
    // Cards change state on the dispatcher's tick; keep it current but light.
    const iv = setInterval(()=>refreshKanbanPanel(panel), 15000);
    tab.onBeforeClose = () => clearInterval(iv);
  });
}

/* ---------------------------- VERIFY ---------------------------------
   Running a card's work is one thing; checking it held is another, and that
   check needs a live session on snarf -- DARKHELIX lives there and no other
   box can see it. Doing it with an agent costs a full model run; the repo's
   own suite is ~600 tests in ~45s with no model involved. This runs one of
   the server's named checks (server.py's DARKHELIX_CHECKS -- the card never
   supplies a command) and can file the verdict back as a comment, so the
   result outlives this pane and a retrying worker can read it.
===================================================================== */
async function kbWireVerify(panel, taskId){
  const sel = panel.querySelector(".kb-verify-check");
  const btn = panel.querySelector(".kb-verify-btn");
  const status = panel.querySelector(".kb-verify-status");
  const out = panel.querySelector(".kb-verify-out");
  try{
    const r = await fetch("/api/darkhelix/checks");
    const j = await r.json();
    const checks = j.checks || [];
    if(!checks.length){ sel.innerHTML = `<option value="">none available</option>`; return; }
    sel.innerHTML = checks.map(c =>
      `<option value="${kanbanEsc(c.id)}">${kanbanEsc(c.label)}</option>`).join("");
    btn.disabled = false;
  }catch(err){
    sel.innerHTML = `<option value="">unavailable</option>`;
    status.innerHTML = `<span class="err">${kanbanEsc(err.message)}</span>`;
    return;
  }
  btn.onclick = async () => {
    const check = sel.value;
    if(!check) return;
    btn.disabled = true;
    const label = sel.options[sel.selectedIndex].textContent;
    status.innerHTML = `<span class="warn">running ${kanbanEsc(label)} on snarf…</span>`;
    out.hidden = true;
    try{
      const r = await fetch("/api/darkhelix/verify", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          check, task_id: taskId,
          comment: panel.querySelector(".kb-verify-file input").checked,
        }),
      });
      const j = await r.json();
      if(j.error){
        status.innerHTML = `<span class="err">${kanbanEsc(j.error)}</span>`;
      }else{
        const verdict = j.ok
          ? `<span class="ok">passed in ${j.elapsed}s</span>`
          : `<span class="err">failed (exit ${j.rc}) in ${j.elapsed}s</span>`;
        const filed = j.commented ? ` · filed on the card` :
                      j.comment_error ? ` · <span class="warn">not filed</span>` : "";
        status.innerHTML = verdict + filed;
        out.textContent = j.output || "(no output)";
        out.hidden = false;
        out.scrollTop = out.scrollHeight;
      }
    }catch(err){
      status.innerHTML = `<span class="err">${kanbanEsc(err.message)}</span>`;
    }
    btn.disabled = false;
  };

  /* Land: verify -> commit -> push -> PR, on the card's own worktree.
     A failed check stops before the push (DARKHELIX's pre-push hook would
     refuse it anyway) and reroutes the card back onto the board with the
     reason attached, rather than failing silently off-screen. */
  const landBtn = panel.querySelector(".kb-land-btn");
  landBtn.onclick = async () => {
    landBtn.disabled = true; btn.disabled = true;
    status.innerHTML = `<span class="warn">landing — verify, commit, push…</span>`;
    out.hidden = true;
    try{
      const r = await fetch("/api/darkhelix/land", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({task_id: taskId, check: sel.value || "tests"}),
      });
      const j = await r.json();
      const lines = (j.steps || []).map(st =>
        `${st.ok ? "ok  " : "FAIL"}  ${st.stage}${st.rc === null || st.rc === undefined ? "" : "  (rc " + st.rc + ")"}`);
      if(j.pr_url) lines.push("", "pull request: " + j.pr_url);
      if(j.branch) lines.push("branch: " + j.branch);
      const last = (j.steps || [])[j.steps.length - 1];
      if(last && last.detail) lines.push("", last.detail);
      out.textContent = lines.join("\n");
      out.hidden = false;

      if(j.ok && j.pr_url){
        status.innerHTML = `<span class="ok">landed</span> · <a href="${kanbanEsc(j.pr_url)}" target="_blank" rel="noreferrer">PR</a>`;
      }else if(j.ok){
        status.innerHTML = `<span class="ok">pushed ${kanbanEsc(j.branch || "")}</span>` +
          (j.pr_skipped ? ` · no PR (${kanbanEsc(j.pr_skipped)})` : "");
      }else{
        const routed = j.rerouted_to_kanban === "blocked" ? "card blocked on the board"
                     : j.rerouted_to_kanban === "commented" ? "reason filed on the card"
                     : "NOT recorded on the card";
        status.innerHTML = `<span class="err">stopped at ${kanbanEsc(j.stage || "?")}</span> · ${routed}`;
        refreshOpenKanbanBoards();
      }
    }catch(err){
      status.innerHTML = `<span class="err">${kanbanEsc(err.message)}</span>`;
    }
    landBtn.disabled = false; btn.disabled = false;
  };
}

/* A land that reroutes changes the board, so any open board pane should show
   it without waiting out its 15s poll. */
function refreshOpenKanbanBoards(){
  document.querySelectorAll(".work-panel.kanban-pane").forEach(p => refreshKanbanPanel(p));
}

/* A task's run log is the live transcript of Hermes working. Polled rather
   than streamed: the log is a file on another box, and a 3s poll is far
   simpler than plumbing a second websocket for something read-only.

   The status header comes from /api/kanban/<id> — one card's worth of
   traffic. It used to refetch the ENTIRE board every 3s because the ssh CLI
   had no per-task read; the plugin API does. */
function openTaskLog(taskId){
  openWorkTabTurning("tasklog",taskId,taskId,(panel,tab)=>{
    panel.innerHTML = `<div class="kb-log-status">checking status…</div>
      <div class="kb-verify">
        <span class="kb-verify-label">VERIFY ON SNARF</span>
        <select class="kb-verify-check"><option value="">loading…</option></select>
        <button class="btn kb-verify-btn" disabled>Run</button>
        <label class="kb-verify-file"><input type="checkbox" checked> file result on the card</label>
        <button class="btn kb-land-btn" title="Verify, commit, push the card's branch, and open a PR if the check passes">Land ▸</button>
        <span class="kb-verify-status"></span>
      </div>
      <pre class="kb-verify-out" hidden></pre>
      <pre class="kb-log">loading…</pre>`;
    panel.classList.add("tasklog-pane");
    const statusEl = panel.querySelector(".kb-log-status");
    const pre = panel.querySelector(".kb-log");
    kbWireVerify(panel, taskId);
    // Delegated: the status line is rebuilt on every 3s poll, so a handler
    // bound to the button itself would be thrown away a moment later.
    statusEl.addEventListener("click", (e) => {
      const btn = e.target.closest(".kb-log-reclaim");
      if(!btn) return;
      btn.disabled = true;
      btn.textContent = "Reclaiming…";
      fetch("/api/kanban/reclaim", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({task_id: btn.dataset.id}),
      }).then(r => r.json()).then(j => {
        if(!j.ok){ btn.disabled = false; btn.textContent = "Reclaim failed — retry"; return; }
        // The board is the other view of this same fact.
        refreshOpenKanbanBoards();
      }).catch(() => { btn.disabled = false; btn.textContent = "Reclaim failed — retry"; });
    });
    let stopped = false;
    const pullStatus = async () => {
      try{
        const r = await fetch(`/api/kanban/${encodeURIComponent(taskId)}`);
        const j = await r.json();
        const task = j.task;
        if(!task || !task.status){
          statusEl.innerHTML = `<span class="err">card not found on board</span>`;
          return;
        }
        const cls = KANBAN_STATUS_CLASS[task.status] ?? "";
        const msg = KANBAN_STATUS_MSG[task.status] ?? task.status;
        const runs = (j.runs || []).length;
        const attempts = runs > 1 ? ` · ${runs} runs` : "";
        // This pane is where you sit watching a run, so it is where you find
        // out it is wedged — and therefore where the way out of it belongs.
        // Having to close the log, go back to the board and find the card
        // again is the reason a stuck card just gets left alone.
        const mins = kbRunningMinutes(task);
        const elapsed = mins ? ` · ${mins < 60 ? mins+"m" : Math.floor(mins/60)+"h"+(mins%60)+"m"} elapsed` : "";
        const stuck = mins >= KB_STUCK_MINUTES
          ? ` <span class="warn">— no longer looks live</span>` : "";
        const reclaim = task.status === "running"
          ? ` <button class="btn kb-log-reclaim" data-id="${kanbanEsc(task.id)}"
               title="Kill this worker and reset the card to ready — the dispatcher starts a fresh run, costing another model run">Reclaim</button>` : "";
        statusEl.innerHTML = `<b class="${cls}">${task.status}${task.status==="running"?" ●":""}</b> — ${msg}${attempts}${elapsed}${stuck}${reclaim}`;
      }catch(err){ statusEl.innerHTML = `<span class="err">status unavailable: ${err.message}</span>`; }
    };
    const pullLog = async () => {
      if(stopped) return;
      try{
        const r = await fetch(`/api/kanban/${encodeURIComponent(taskId)}/log?lines=400`);
        const j = await r.json();
        const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 60;
        pre.textContent = j.log || j.error || "(no run log yet — still in triage/queue; see status above)";
        if(atBottom) pre.scrollTop = pre.scrollHeight;
      }catch(err){ pre.textContent = "log unavailable: "+err.message; }
    };
    const pull = () => { pullStatus(); pullLog(); };
    pull();
    const iv = setInterval(pull, 3000);
    tab.onBeforeClose = () => { stopped = true; clearInterval(iv); };
  });
}

document.querySelectorAll('[data-action="kanban"]').forEach(btn=>{
  btn.addEventListener("click", openKanbanBoard);
});
