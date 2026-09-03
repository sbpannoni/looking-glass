"use strict";
/* ============================== SUBMIT WORK ==========================
   One-click work submission: pick a real DARKHELIX TODO.md item, add
   optional instructions, submit -- files a real kanban card via
   /api/kanban/create, always to --triage (Hermes's specifier still fleshes
   it out and decomposes it before anything executes; see server.py's
   module note on /api/kanban/create for why this is a deliberate,
   reviewed choice, not a default left unconsidered).

   TWO PANES, one scroller each. The old single column stacked a picker, the
   selected item, a notes box, a button and three submission groups down a
   1200px-wide pane, and every one of those was a flex child with
   flex-shrink:1 inside a scrolling flex column -- so `max-height:34vh` on
   the picker rendered as ~142px. You saw 3 of 51 items, and the first 9 of
   those were blocked and unclickable. Nothing here relies on a viewport
   height: the picker owns the left pane's space (flex:1 + min-height:0) and
   the form owns the right.

   The picker borrows the board's lane vocabulary -- sections with counts in
   the header, click a header to collapse, and the section that is never
   actionable (blocked) is hidden by default the same way an empty lane
   collapses to a rail. The section name is a header now, not a line on
   every row: nine consecutive rows used to print "1. BLOCKED -- MISSING
   DATABASE OR BINARY" above their own titles, in a louder font than the
   title.

   On success this jumps straight into the new card's task-log pane
   (openTaskLog, from kanban.js) instead of leaving you stranded here with
   just a text confirmation -- filing a card used to be a dead end with no
   way to see it move.

   There is deliberately no submissions tracker here any more. It duplicated
   the board badly: the same statuses, the same Archive/Unblock buttons, and
   a second poller against /api/kanban, all backed by a localStorage list
   that only this browser knew about. It existed because filing WAS a dead
   end; now the card lands in the board's triage lane and this view opens
   its log. Work in flight lives on the board, in one place.

   Submissions are deduped server-side by a content hash (see
   /api/kanban/create's --idempotency-key): filing the same item twice
   returns the card already on the board instead of a second copy.

   TWO SHAPES, one picker. `single card` files to triage and lets Hermes's
   auto-decomposer build the chain -- one card in, an ordered graph out.
   `swarm` names the workers itself: N parallel angles on the same goal, a
   verifier that waits on all of them, a synthesizer that waits on the
   verifier. The swarm shape was proven by hand over ssh on 2026-09-02 and
   then had no button, so recreating it meant composing the CLI invocation
   from memory; this is that run, as a form.

   The difference that matters at the button: a triage card QUEUES (the
   specifier still has to flesh it out), a swarm DISPATCHES -- its workers
   are created `ready`. Same dedup key for both, so an item filed either way
   shows as filed and cannot quietly be opened twice.
================================================================= */

const SW_COLLAPSED_KEY = "lg-sw-collapsed";
const SW_SHOW_BLOCKED_KEY = "lg-sw-show-blocked";
const SW_SHOW_UI_KEY = "lg-sw-show-ui";
/* How often the picker re-reads the board while it is open. The "filed"
   badges ARE the in-flight state of everything on this list, and they used
   to be a snapshot taken when the pane opened: a card could go triage ->
   running -> done with this view still showing the moment it was filed. */
const SW_POLL_MS = 20000;

/* The trio the hand-run swarm used on 2026-09-02 (root t_c4c82bf1): the
   domain expert, the wet-lab/tooling angle, and the literature angle. A
   starting point, not a rule -- every row is a dropdown. Profiles that do
   not exist on hermes are dropped rather than offered. */
const SW_SWARM_DEFAULT_WORKERS = ["darkhelix", "bioinformatics", "researcher"];
const SW_SWARM_DEFAULT_VERIFIER = "darkhelix";
const SW_SWARM_DEFAULT_SYNTH = "researcher";
const SW_SWARM_MAX_WORKERS = 6;

function submitWorkEsc(s){
  return (s||"").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
}

function swPrefLoad(key, fallback){
  try{ const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); }
  catch{ return fallback; }
}
function swPrefSave(key, value){
  try{ localStorage.setItem(key, JSON.stringify(value)); }catch{ /* storage disabled */ }
}

/* ------------------------------ picker ------------------------------- */

function swState(panel){
  if(!panel._sw){
    // Per-panel, not module-level. A module global meant closing and
    // reopening the tab restored the previous selection against a freshly
    // loaded (and possibly different) list.
    // `mode` is deliberately NOT persisted across opens. Single-card is
    // the safe default (it queues; a swarm dispatches), and a sticky mode
    // means the dangerous one can be the one you get by not looking.
    panel._sw = {items: [], selected: null, filter: "", mode: "single",
                 profiles: [], profilesError: null, workers: null};
  }
  return panel._sw;
}

/* A section that contains nothing you can act on is noise. The blocked
   section is 9 of the 51 items and every one of them is unclickable, so it
   is hidden until asked for -- the same judgement the board makes when it
   collapses an empty lane to a rail. */
function swVisibleItems(panel){
  const state = swState(panel);
  const showBlocked = swPrefLoad(SW_SHOW_BLOCKED_KEY, false);
  const showUi = swPrefLoad(SW_SHOW_UI_KEY, false);
  const q = state.filter.trim().toLowerCase();
  return state.items.filter(it => {
    if(it.blocked && !showBlocked) return false;
    // Same judgement as blocked, for the same reason: an item that can only
    // be done in a live Electron session against real outputs is not work a
    // dispatched worker can do at all. Filing one buys a full model run that
    // can only come back asking for a UI. Hidden until asked for, not
    // removed -- they are real work, just not work for this button.
    if(it.needs_ui && !showUi) return false;
    if(!q) return true;
    return it.title.toLowerCase().includes(q) || (it.section||"").toLowerCase().includes(q);
  });
}

/* TODO.md and the board are separate trackers. Nothing in Hermes ties them
   together: filing a card does not tick the box and landing the work does not
   tick it either, so an item whose fix is already in a card -- or in a merged
   PR -- would go on looking like open work forever.

   Two things now close that gap, and they are different in kind. This badge
   reflects the board live (the picker re-reads it every SW_POLL_MS while it
   is open), but only in this view. `⇄ sync TODO.md` writes the state into the
   file itself, which is what anything that is not this HUD reads.

   The badge is evidence of a card, never proof there is none: only
   submissions made after the idempotency key was introduced carry one. */
function swFiledBadge(item){
  const filed = item.filed_as;
  if(!filed) return "";
  // A decomposed card parks in `todo` while its leaves do the work, so its own
  // status is the least useful thing about it. The server rolls the leaves up
  // into effective_status; show that, and keep the card's real status in the
  // tooltip rather than dropping it -- "todo" is still the answer to "what
  // will the board do with this card", it is just not the answer to "is
  // anything happening".
  const shown = filed.effective_status || filed.status;
  const cls = (typeof KANBAN_STATUS_CLASS !== "undefined"
               ? KANBAN_STATUS_CLASS[shown] : "") || "";
  const why = filed.effective_status
    ? ` — card itself is ${filed.status}, waiting on subtask ${filed.wip_via || "?"}`
      + ` (${filed.wip_via_status || "active"})`
    : "";
  return `<span class="sw-filed" data-card="${submitWorkEsc(filed.id)}"
    title="Already filed as ${submitWorkEsc(filed.id)}${submitWorkEsc(why)} — click to open its run log"
    >filed · <b class="${cls}">${submitWorkEsc(shown || "?")}</b></span>`;
}

function swRow(item, state){
  const cls = ["sw-row"];
  if(item.blocked) cls.push("sw-row-blocked");
  if(item.needs_ui) cls.push("sw-row-ui");
  if(item.filed_as) cls.push("sw-row-filed");
  if(state.selected === item.id) cls.push("sw-row-selected");
  const badge = item.blocked
    ? `<span class="badge flaky">blocked</span>`
    : item.needs_ui ? `<span class="badge flaky" title="Needs a live Electron/UI session against real outputs — a dispatched worker cannot do this">UI run</span>`
    : item.wip ? `<span class="badge">WIP</span>` : "";
  // No section line: the section header above says it once.
  return `<div class="${cls.join(" ")}" data-id="${submitWorkEsc(item.id)}">
    <span class="sw-row-title">${submitWorkEsc(item.title)}</span>${badge}${swFiledBadge(item)}</div>`;
}

function submitWorkRenderList(panel){
  const state = swState(panel);
  const list = panel.querySelector(".sw-list");
  const items = swVisibleItems(panel);
  const blockedCount = state.items.filter(it => it.blocked).length;

  const uiCount = state.items.filter(it => it.needs_ui).length;

  const toggle = panel.querySelector(".sw-blocked-toggle");
  if(toggle){
    const showing = swPrefLoad(SW_SHOW_BLOCKED_KEY, false);
    toggle.textContent = `${showing ? "hide" : "show"} blocked (${blockedCount})`;
    toggle.classList.toggle("on", showing);
    toggle.style.display = blockedCount ? "" : "none";
  }
  const uiToggle = panel.querySelector(".sw-ui-toggle");
  if(uiToggle){
    const showing = swPrefLoad(SW_SHOW_UI_KEY, false);
    uiToggle.textContent = `${showing ? "hide" : "show"} UI-run (${uiCount})`;
    uiToggle.classList.toggle("on", showing);
    uiToggle.style.display = uiCount ? "" : "none";
  }

  const note = panel.querySelector(".sw-filed-note");
  if(note){
    const filedCount = state.items.filter(it => it.filed_as).length;
    note.innerHTML = state.boardError
      ? `<span class="warn" title="${submitWorkEsc(state.boardError)}">board unreadable — "filed" badges unavailable</span>`
      : filedCount
        ? `${filedCount} already filed`
        : "";
  }

  if(!items.length){
    list.innerHTML = `<div class="sw-empty">${state.items.length ? "no matching items" : "no items"}</div>`;
    return;
  }
  // Group by section, preserving TODO.md's own order.
  const order = [];
  const bySection = new Map();
  items.forEach(it => {
    const name = it.section || "—";
    if(!bySection.has(name)){ bySection.set(name, []); order.push(name); }
    bySection.get(name).push(it);
  });
  const collapsed = swPrefLoad(SW_COLLAPSED_KEY, {});
  list.innerHTML = order.map(name => {
    const rows = bySection.get(name);
    const isCollapsed = collapsed[name] === true;
    return `<div class="sw-section${isCollapsed ? " collapsed" : ""}" data-section="${submitWorkEsc(name)}">
      <div class="sw-section-head" title="Collapse or expand this section">
        <span class="sw-section-name">${submitWorkEsc(name)}</span>
        <span class="sw-section-count">${rows.length}</span>
      </div>
      <div class="sw-section-rows">${rows.map(r => swRow(r, state)).join("")}</div>
    </div>`;
  }).join("");
}

function submitWorkRenderSelected(panel){
  const state = swState(panel);
  const box = panel.querySelector(".sw-selected");
  const submitBtn = panel.querySelector(".sw-submit");
  const item = state.items.find(it => it.id === state.selected);
  // The clear control lives in the bar above the box, so it has to be
  // updated in step with it. Selecting used to be a one-way door: there was
  // no way to put an item back, only to pick a different one, so a pane
  // opened to browse stayed stuck showing whatever was touched first.
  const clearBtn = panel.querySelector(".sw-clear");
  if(clearBtn) clearBtn.hidden = !item;
  if(!item){
    box.innerHTML = `<div class="sw-placeholder">Pick an item on the left to see it in full and file it to the board.</div>`;
    submitBtn.disabled = true;
    swRenderParentOptions(panel);
    return;
  }
  // Selectable once revealed, but not silently: this is the one class of
  // item where "submit" cannot lead anywhere good on its own.
  const uiWarn = item.needs_ui
    ? `<div class="sw-selected-ui">Needs a live Electron/UI session against real
         outputs. A dispatched worker has no display — file this only if the
         instructions below turn it into something headless (a check, a
         refactor, a fixture) that a worker can actually finish.</div>`
    : "";
  // Loud, because filing it again is usually not what you want -- and it is
  // still allowed, because adding instructions makes a genuinely different
  // request and the server keys on the text you send.
  const filed = item.filed_as
    ? `<div class="sw-selected-filed">Already filed as
         <a href="#" class="sw-open-card" data-card="${submitWorkEsc(item.filed_as.id)}"
            >${submitWorkEsc(item.filed_as.id)}</a>
         (${submitWorkEsc(item.filed_as.status || "?")}${item.filed_as.effective_status
            ? ", subtask " + submitWorkEsc(item.filed_as.wip_via || "?")
              + " " + submitWorkEsc(item.filed_as.wip_via_status || "active")
            : ""}). Submitting with no extra
         instructions returns that same card rather than making a second one.</div>`
    : "";
  box.innerHTML = `<div class="sw-selected-section">${submitWorkEsc(item.section || "")}</div>
    ${uiWarn}
    ${filed}
    <div class="sw-selected-text">${submitWorkEsc(item.text).replace(/\n/g,"<br>")}</div>`;
  submitBtn.disabled = false;
  swRenderParentOptions(panel);
  swRenderFollowUp(panel);
}

/* ---------------------------- builds on ------------------------------
   Cards are isolated from each other by design (each gets its own worktree
   off origin/master), and for unrelated work that is correct. For a chain
   it is the bug: the second card opens a tree with no sign of the first, so
   it re-derives the same groundwork, or reimplements it differently, and
   the two branches then conflict. Naming a parent here makes the board hold
   this card until that one closes AND cuts its branch from the parent's, so
   it starts where the parent finished. */
function swRenderParentOptions(panel){
  const sel = panel.querySelector(".sw-parent");
  if(!sel) return;
  const state = swState(panel);
  const cards = state.parentCards || [];
  const keep = sel.value;
  // Only cards that can still produce a branch to build on. A done card's
  // branch is exactly what a follow-up wants; an archived one is not.
  const opts = cards.map(c =>
    `<option value="${submitWorkEsc(c.id)}">${submitWorkEsc(c.status)} · ${submitWorkEsc((c.title||"").slice(0,64))}</option>`).join("");
  const want = cards.map(c => c.id).join("|");
  if(sel.dataset.opts !== want){
    sel.dataset.opts = want;
    sel.innerHTML = `<option value="">nothing — branch from origin/master</option>` + opts;
    sel.value = cards.some(c => c.id === keep) ? keep : "";
    // The 20s poll rebuilds these options. If the parent we were showing
    // findings for is gone, the findings below it are about nothing.
    if(sel.value !== keep) swRenderFollowUp(panel);
  }
}

/* --------------------------- follow-up ------------------------------
   Picking a parent used to be a blind act: the dropdown gave you an id and a
   title, and whether that card had actually concluded anything -- and which
   TODO item its conclusion feeds -- lived in the card detail pane, or in a
   terminal. So the control that exists to chain work told you nothing about
   the work you were chaining to.

   This shows the parent's real handoff (the run summary its worker wrote for
   downstream tasks) and the open TODO items that handoff is evidence for,
   ranked server-side by shared filenames and vocabulary. Clicking one selects
   it, so "review the research, file the coding card against it" is two
   clicks with the parent already set. Items that ALREADY have a card say so
   loudly: suggesting related work is only half the job if it invites you to
   file the same thing twice. */
function swRenderFollowUp(panel){
  const box = panel.querySelector(".sw-followup");
  if(!box) return;
  const state = swState(panel);
  const fu = state.followUp;
  const sel = panel.querySelector(".sw-parent");
  if(!sel || !sel.value){ box.innerHTML = ""; box.hidden = true; return; }
  box.hidden = false;
  if(state.followUpLoading){ box.innerHTML = `<div class="sw-fu-note">reading parent handoff…</div>`; return; }
  if(!fu || fu.parentId !== sel.value){ box.innerHTML = ""; return; }
  if(fu.error){
    box.innerHTML = `<div class="sw-fu-note err">${submitWorkEsc(fu.error)}</div>`;
    return;
  }
  const h = fu.handoff;
  // A parent with no handoff is worth saying out loud rather than rendering
  // as an empty box: it means the worker closed without writing a summary,
  // so a child linked to it inherits its BRANCH but not its findings.
  const handoffHtml = h && (h.summary || "").trim()
    ? `<div class="sw-fu-summary">${submitWorkEsc(h.summary)}</div>`
    : `<div class="sw-fu-note">This card wrote no handoff summary. A child still
         branches from its work, but inherits no findings — ask for
         <code>--summary</code> in the card body next time.</div>`;
  const items = fu.suggested || [];
  const list = items.length
    ? items.map(it => {
        const filed = it.filed_as
          ? `<span class="sw-fu-dup" title="A card for this item already exists — check it before filing another">already filed · ${submitWorkEsc(it.filed_as.id)}</span>`
          : "";
        const why = (it.why || []).slice(0,4)
          .map(w => `<span class="sw-fu-why">${submitWorkEsc(w)}</span>`).join("");
        return `<div class="sw-fu-item${it.filed_as ? " sw-fu-item-filed" : ""}" data-item="${submitWorkEsc(it.id)}">
            <div class="sw-fu-item-title">${submitWorkEsc(it.title)}</div>
            <div class="sw-fu-item-meta">${why}${filed}</div>
          </div>`;
      }).join("")
    : `<div class="sw-fu-note">No open TODO item shares evidence with this
         card. That is a real answer, not an empty list — file freeform from
         the picker on the left.</div>`;
  box.innerHTML = `<div class="sw-fu-head">PARENT FINDINGS</div>
    ${handoffHtml}
    <div class="sw-fu-head">RELATED TODO ITEMS <span class="sw-fu-sub">matched on shared files and terms</span></div>
    ${list}`;
}

async function swLoadFollowUp(panel){
  const state = panel && swState(panel);
  const sel = panel.querySelector(".sw-parent");
  if(!sel || !sel.value){ state.followUp = null; swRenderFollowUp(panel); return; }
  const want = sel.value;
  state.followUpLoading = true;
  swRenderFollowUp(panel);
  try{
    const r = await fetch(`/api/kanban/${encodeURIComponent(want)}/follow-up`);
    const j = await r.json();
    // The dropdown can move while this is in flight; a late answer for a
    // parent nobody is looking at any more must not overwrite the current one.
    if(sel.value !== want) return;
    state.followUp = j.ok
      ? {parentId: want, handoff: j.handoff, suggested: j.suggested || []}
      : {parentId: want, error: j.error || "could not read that card"};
  }catch(err){
    state.followUp = {parentId: want, error: err.message};
  }finally{
    state.followUpLoading = false;
    swRenderFollowUp(panel);
  }
}

async function swLoadParentCards(panel){
  const state = swState(panel);
  try{
    const r = await fetch("/api/kanban");
    const j = await r.json();
    state.parentCards = (j.tasks || [])
      .filter(t => t.status && t.status !== "archived")
      .slice(0, 40)
      .map(t => ({id: t.id, title: t.title, status: t.status}));
  }catch{ state.parentCards = state.parentCards || []; }
  swRenderParentOptions(panel);
}

/* ------------------------------ swarm --------------------------------
   `hermes kanban swarm` builds a fixed graph: a root card that is completed
   on arrival and serves as the shared blackboard, N parallel workers, a
   verifier that waits on every worker, and a synthesizer that waits on the
   verifier. Everything the workers share -- the TODO item's text and any
   instructions -- travels as the GOAL, which the swarm appends to every card
   it creates. A worker's own line is its angle on that goal and nothing
   else: the CLI uses the worker title as the worker's whole body.

   Three server-side guards back this form, and each is a failure we have had
   rather than a precaution: the role dropdowns only offer profiles that
   carry the skill the swarm hardcodes onto that role card (a synthesizer
   without `humanizer` dies at agent init AFTER every worker has finished --
   2026-09-02, t_a2f91234), colons are rejected in an angle (the CLI reads
   everything after one as a skill list), and the whole graph is filed
   --created-by looking-glass so claim-time provisioning gives every card its
   own worktree. See server.py's note on POST /api/kanban/swarm. */

function swSwarmReady(state){
  return !!(state.profiles && state.profiles.length && !state.profilesError);
}

/* DOM -> state, before anything that re-renders the rows. The inputs are the
   truth while you are typing in them; state is the truth across a redraw. */
function swCollectSwarm(panel){
  const state = swState(panel);
  const rows = [...panel.querySelectorAll(".sw-worker")];
  if(rows.length){
    state.workers = rows.map(row => ({
      profile: row.querySelector(".sw-worker-profile").value,
      title: row.querySelector(".sw-worker-angle").value,
    }));
  }
  const ver = panel.querySelector(".sw-verifier");
  const syn = panel.querySelector(".sw-synth");
  if(ver && ver.value) state.verifier = ver.value;
  if(syn && syn.value) state.synthesizer = syn.value;
}

function swSeedSwarm(panel){
  const state = swState(panel);
  const names = state.profiles.map(p => p.name);
  const pick = (want, cap) => {
    const row = state.profiles.find(p => p.name === want && (!cap || p[cap]));
    if(row) return row.name;
    const any = state.profiles.find(p => !cap || p[cap]);
    return any ? any.name : "";
  };
  if(!state.workers){
    const seeds = SW_SWARM_DEFAULT_WORKERS.filter(n => names.includes(n));
    state.workers = (seeds.length ? seeds : names.slice(0, 3))
      .map(profile => ({profile, title: ""}));
  }
  if(!state.verifier) state.verifier = pick(SW_SWARM_DEFAULT_VERIFIER, "can_verify");
  if(!state.synthesizer) state.synthesizer = pick(SW_SWARM_DEFAULT_SYNTH, "can_synthesize");
}

/* `cap` names the capability this role needs. A profile that cannot hold the
   role is not rendered disabled-but-visible: the server refuses it anyway, and
   a greyed option invites the question "why not" at exactly the moment you are
   trying to file work. The count under the dropdown answers it once. */
function swProfileOptions(profiles, selected, cap){
  return profiles.filter(p => !cap || p[cap])
    .map(p => `<option value="${submitWorkEsc(p.name)}"${p.name === selected ? " selected" : ""}
        >${submitWorkEsc(p.name)}</option>`).join("");
}

function swRenderSwarm(panel){
  const box = panel.querySelector(".sw-swarm");
  if(!box) return;
  const state = swState(panel);
  if(state.profilesError){
    box.innerHTML = `<div class="sw-swarm-note err">Cannot read the profiles on
      hermes (${submitWorkEsc(state.profilesError)}). A swarm hardcodes a skill
      onto its verifier and synthesizer cards, so filing one without checking
      the profiles risks losing every worker's run at the last card. Fix the
      connection, or file this as a single card.</div>`;
    return;
  }
  if(!swSwarmReady(state)){
    box.innerHTML = `<div class="sw-swarm-note">reading profiles…</div>`;
    return;
  }
  swSeedSwarm(panel);
  const rows = state.workers.map((w, i) => `
    <div class="sw-worker" data-i="${i}">
      <select class="sw-worker-profile">${swProfileOptions(state.profiles, w.profile, null)}</select>
      <input class="sw-worker-angle" type="text" value="${submitWorkEsc(w.title)}"
             placeholder="this worker's angle on the goal — one line, no colons">
      <button class="sw-worker-del" type="button" title="Remove this worker"
        ${state.workers.length < 2 ? "disabled" : ""}>✕</button>
    </div>`).join("");
  const verCount = state.profiles.filter(p => p.can_verify).length;
  const synCount = state.profiles.filter(p => p.can_synthesize).length;
  box.innerHTML = `
    <div class="sw-swarm-note">Every card gets the item text and your
      instructions as the shared goal; each line below is one worker's angle on
      it. The verifier waits for all of them, the synthesizer waits for the
      verifier. <b>Workers are filed <code>ready</code>, not to triage — this
      button dispatches.</b> They still serialise on the one GPU seat.</div>
    <div class="sw-workers">${rows}</div>
    <div class="sw-swarm-actions">
      <button class="sw-worker-add" type="button"
        ${state.workers.length >= SW_SWARM_MAX_WORKERS ? "disabled" : ""}
        >+ worker</button>
      <span class="sw-swarm-count">${state.workers.length} of ${SW_SWARM_MAX_WORKERS}</span>
    </div>
    <div class="sw-roles">
      <label>VERIFIER
        <select class="sw-verifier" title="Gets the requesting-code-review skill from the swarm — only profiles that have it are listed">${swProfileOptions(state.profiles, state.verifier, "can_verify")}</select>
        <span class="sw-role-why">${verCount} of ${state.profiles.length} profiles have requesting-code-review</span>
      </label>
      <label>SYNTHESIZER
        <select class="sw-synth" title="Gets the humanizer skill from the swarm — only profiles that have it are listed">${swProfileOptions(state.profiles, state.synthesizer, "can_synthesize")}</select>
        <span class="sw-role-why">${synCount} of ${state.profiles.length} profiles have humanizer</span>
      </label>
    </div>`;
}

/* The mode switch. BUILDS ON is hidden rather than disabled in swarm mode:
   `hermes kanban swarm` has no --parent, so a parent picked here would be
   silently dropped, and a control that is ignored is worse than one that is
   not there. */
function swRenderMode(panel, {redraw = false} = {}){
  const state = swState(panel);
  const swarm = state.mode === "swarm";
  panel.querySelectorAll(".sw-mode-btn").forEach(b =>
    b.classList.toggle("on", b.dataset.mode === state.mode));
  const single = panel.querySelector(".sw-single");
  if(single) single.hidden = swarm;
  const box = panel.querySelector(".sw-swarm");
  if(box) box.hidden = !swarm;
  // The form column has to give the swarm room: the item text and the swarm
  // box both want to grow, and with the selected item unbounded the role
  // dropdowns -- the part that carries the "this profile can hold this role"
  // guarantee -- were shrunk off the bottom of the pane.
  panel.querySelector(".sw-col-form")?.classList.toggle("sw-mode-swarm", swarm);
  const btn = panel.querySelector(".sw-submit");
  if(btn) btn.textContent = swarm ? "Fan out to Hermes" : "Submit to Hermes";
  // Only draw the form when there is nothing to lose. Flipping modes keeps
  // the (hidden) rows exactly as typed; a redraw on every flip would empty
  // three angles because you wanted to re-read the item text.
  if(swarm && (redraw || !panel.querySelector(".sw-worker"))) swRenderSwarm(panel);
}

async function swLoadProfiles(panel){
  const state = swState(panel);
  try{
    const r = await fetch("/api/kanban/profiles");
    const j = await r.json();
    state.profiles = j.profiles || [];
    state.profilesError = j.error || (state.profiles.length ? null : "no profiles returned");
  }catch(err){
    state.profiles = [];
    state.profilesError = err.message;
  }
  if(state.mode === "swarm" && !panel.querySelector(".sw-worker")) swRenderSwarm(panel);
}

async function submitWorkSwarm(panel){
  const state = swState(panel);
  const item = state.items.find(it => it.id === state.selected);
  if(!item) return;
  swCollectSwarm(panel);
  const status = panel.querySelector(".sw-status");
  const submitBtn = panel.querySelector(".sw-submit");
  // A row left blank is not an error, it is an unused row -- but a colon is,
  // and saying so here beats a 400 after a round trip.
  const workers = (state.workers || [])
    .map(w => ({profile: w.profile, title: (w.title || "").trim()}))
    .filter(w => w.title);
  if(!workers.length){
    status.innerHTML = `<span class="err">Give at least one worker an angle.</span>`;
    return;
  }
  const bad = workers.find(w => w.title.includes(":"));
  if(bad){
    status.innerHTML = `<span class="err">No colons in an angle — the CLI reads
      everything after one as a skill list. Fix: ${submitWorkEsc(bad.title.slice(0, 60))}</span>`;
    return;
  }
  const notes = panel.querySelector(".sw-notes").value.trim();
  const body = notes ? `${item.text}\n\n--- additional instructions ---\n${notes}` : item.text;
  submitBtn.disabled = true;
  submitBtn.textContent = "Fanning out…";
  status.innerHTML = "";
  try{
    const r = await fetch("/api/kanban/swarm", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({title: item.title, body, workers,
                            verifier: state.verifier, synthesizer: state.synthesizer}),
    });
    const j = await r.json();
    if(j.ok){
      const first = (j.workers || [])[0];
      status.innerHTML = j.duplicate
        ? `<span class="warn">This item is already a swarm — root ${submitWorkEsc(j.root)}.
            Nothing new was filed; opening it…</span>`
        : `<span class="ok">Swarm filed — root ${submitWorkEsc(j.root)},
            ${(j.workers || []).length} workers running,
            verifier ${submitWorkEsc((j.verifier || {}).id || "?")},
            synthesizer ${submitWorkEsc((j.synthesizer || {}).id || "?")}.</span>`;
      panel.querySelector(".sw-notes").value = "";
      state.selected = null;
      submitWorkLoad(panel);
      swLoadParentCards(panel);
      // The root is `done` the moment it exists and has no log of its own --
      // it is the blackboard, not a run. The first worker is where something
      // is actually happening.
      const open = (first && first.id) || j.root;
      if(open && typeof openTaskLog === "function") openTaskLog(open);
    }else{
      status.innerHTML = `<span class="err">${submitWorkEsc(j.error || "unknown error")}</span>`;
    }
  }catch(err){
    status.innerHTML = `<span class="err">${submitWorkEsc(err.message)}</span>`;
  }
  submitBtn.textContent = "Fan out to Hermes";
  submitBtn.disabled = !state.selected;
}

/* Everything the list actually draws. A background poll that redraws an
   unchanged list would throw away scroll position every 20 seconds, which is
   worse than the staleness it fixes -- so a quiet poll only touches the DOM
   when this string moves. */
function swItemsSignature(items){
  return items.map(it => [it.id, it.blocked, it.needs_ui, it.wip,
                          it.filed_as ? it.filed_as.id + ":" + it.filed_as.status
                                        + ":" + (it.filed_as.effective_status || "") : ""
                         ].join("~")).join("|");
}

function swClearSelection(panel){
  const state = swState(panel);
  if(!state.selected) return;
  state.selected = null;
  submitWorkRenderList(panel);
  submitWorkRenderSelected(panel);
}

async function submitWorkLoad(panel, {quiet = false} = {}){
  const state = swState(panel);
  const list = panel.querySelector(".sw-list");
  // A poll must not flash "loading…" over a list you are reading.
  if(!quiet) list.innerHTML = `<div class="sw-empty">loading…</div>`;
  try{
    const r = await fetch("/api/darkhelix-todo");
    const j = await r.json();
    if(j.error){
      if(!quiet){
        list.innerHTML = `<div class="sw-empty"><span class="err">${submitWorkEsc(j.error)}</span></div>`;
      }
      return;
    }
    const items = j.items || [];
    const changed = swItemsSignature(items) !== swItemsSignature(state.items || []);
    state.items = items;
    // No badges is ambiguous on its own -- it looks the same whether nothing
    // is filed or the board could not be read. Say which.
    state.boardError = j.board_error || null;
    // A reload can drop the selected item out from under us.
    if(!state.items.some(it => it.id === state.selected)) state.selected = null;
    if(!quiet || changed){
      const scroll = list.scrollTop;
      submitWorkRenderList(panel);
      list.scrollTop = scroll;
      submitWorkRenderSelected(panel);
    }
  }catch(err){
    if(!quiet){
      list.innerHTML =
        `<div class="sw-empty"><span class="err">todo list unavailable: ${submitWorkEsc(err.message)}</span></div>`;
    }
  }
}

/* ---------------------------- sync back ------------------------------
   The badges keep this VIEW current, but TODO.md is the tracker ("if it
   isn't here, it isn't tracked") and nothing was writing to it -- so an item
   whose card finished stayed an unticked box, and the next read of the file
   by anything that is not this HUD still called it open work. This pushes
   the board's state into the file, in the file's own notation. */
async function submitWorkSync(panel){
  const btn = panel.querySelector(".sw-sync");
  const status = panel.querySelector(".sw-status");
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "syncing…";
  try{
    const r = await fetch("/api/darkhelix-todo/sync", {method: "POST"});
    const j = await r.json();
    if(!j.ok){
      status.innerHTML = `<span class="err">sync failed: ${submitWorkEsc(j.error || "unknown")}</span>`;
    }else if(!j.changes.length){
      status.innerHTML = `<span class="ok">TODO.md already matches the board</span>`;
    }else{
      const ticked = j.changes.filter(c => c.change === "completed").length;
      const wip = j.changes.length - ticked;
      status.innerHTML = `<span class="ok">TODO.md updated — ${ticked} ticked, ${wip} marked WIP</span>`;
      submitWorkLoad(panel);
    }
  }catch(err){
    status.innerHTML = `<span class="err">sync failed: ${submitWorkEsc(err.message)}</span>`;
  }
  btn.textContent = label;
  btn.disabled = false;
}

async function submitWorkSubmit(panel){
  const state = swState(panel);
  const item = state.items.find(it => it.id === state.selected);
  if(!item) return;
  const notes = panel.querySelector(".sw-notes").value.trim();
  const status = panel.querySelector(".sw-status");
  const submitBtn = panel.querySelector(".sw-submit");
  submitBtn.disabled = true;
  submitBtn.textContent = "Submitting…";
  status.innerHTML = "";

  const body = notes ? `${item.text}\n\n--- additional instructions ---\n${notes}` : item.text;
  const parentSel = panel.querySelector(".sw-parent");
  const parentTaskId = parentSel ? parentSel.value : "";
  try{
    const r = await fetch("/api/kanban/create", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({title: item.title, body,
                            parent_task_id: parentTaskId || undefined}),
    });
    const j = await r.json();
    if(j.ok){
      const taskId = (j.task && (j.task.id || j.task.task_id)) || "?";
      // A dedup hit is not a failure and not a new card: the server asked
      // for this exact submission by content hash and Hermes handed back the
      // one already on the board.
      // Say what it was actually cut from. Asking to build on a card whose
      // branch does not exist yet silently falls back to origin/master, and
      // silently is how you end up debugging why the changes were not there.
      const chained = j.parent
        ? (j.base && j.base !== "origin/master"
            ? ` <span class="ok">· branched from ${submitWorkEsc(j.parent)}</span>`
            : ` <span class="warn">· ${submitWorkEsc(j.parent)} has no branch yet — cut from origin/master</span>`)
        : "";
      status.innerHTML = (j.duplicate
        ? `<span class="warn">Already on the board as ${submitWorkEsc(String(taskId))} — opening it…</span>`
        : `<span class="ok">Filed to triage — task ${submitWorkEsc(String(taskId))}. Opening its log…</span>`) + chained;
      panel.querySelector(".sw-notes").value = "";
      state.selected = null;
      // Re-read so the item it was just filed from picks up its "filed"
      // badge immediately, rather than looking untouched until a reload.
      submitWorkLoad(panel);
      swLoadParentCards(panel);
      if(taskId !== "?" && typeof openTaskLog === "function") openTaskLog(taskId);
    }else{
      status.innerHTML = `<span class="err">${submitWorkEsc(j.error || "unknown error")}</span>`;
    }
  }catch(err){
    status.innerHTML = `<span class="err">${submitWorkEsc(err.message)}</span>`;
  }
  submitBtn.textContent = "Submit to Hermes";
  submitBtn.disabled = !state.selected;
}

function openSubmitWork(){
  openWorkTabTurning("submit-work","main","SUBMIT WORK",(panel,tab)=>{
    panel.classList.add("sw-pane");
    panel.innerHTML = `
      <div class="sw-head">
        <span class="sw-head-title">SUBMIT WORK</span>
        <span class="sw-head-sub">DARKHELIX TODO.md · files to triage</span>
        <span class="sw-filed-note"></span>
        <span class="sw-head-spacer"></span>
        <button class="sw-blocked-toggle" type="button">show blocked</button>
        <button class="sw-ui-toggle" type="button" title="Items that need a live Electron/UI session — hidden by default because a dispatched worker has no display">show UI-run</button>
        <button class="sw-sync" type="button" title="Write the board's state back into TODO.md on snarf: tick items whose card is done, tag items whose card is in flight **WIP**. Never unticks anything.">⇄ sync TODO.md</button>
        <button class="sw-reload" type="button" title="Re-read TODO.md">⟲ reload</button>
      </div>
      <div class="sw-body">
        <div class="sw-col sw-col-list">
          <input class="sw-filter" type="text" placeholder="filter items…">
          <div class="sw-list"></div>
        </div>
        <div class="sw-col sw-col-form">
          <div class="sw-bar">SELECTED ITEM
            <button class="sw-clear" type="button" hidden
              title="Clear the selection (Esc)">✕ clear</button></div>
          <div class="sw-selected"></div>
          <div class="sw-bar">FILE AS
            <span class="sw-mode">
              <button class="sw-mode-btn on" type="button" data-mode="single"
                title="One card to triage. Hermes's specifier fleshes it out and the auto-decomposer builds an ordered chain from it.">single card → triage</button>
              <button class="sw-mode-btn" type="button" data-mode="swarm"
                title="Parallel workers on one goal, then a verifier, then a synthesizer. Skips triage — the workers are filed ready and dispatch on the next tick.">swarm → fan out</button>
            </span>
          </div>
          <div class="sw-single">
            <div class="sw-bar">BUILDS ON (OPTIONAL)</div>
            <select class="sw-parent" title="File this as a child of an existing card: the board holds it until that card closes, and its worktree is branched from that card's branch instead of origin/master — so it starts with that work already in the tree."></select>
            <div class="sw-followup"></div>
          </div>
          <div class="sw-swarm" hidden></div>
          <div class="sw-bar">ADDITIONAL INSTRUCTIONS (OPTIONAL)</div>
          <textarea class="sw-notes" placeholder="anything to add or override…"></textarea>
          <div class="sw-actions">
            <button class="btn sw-submit" disabled>Submit to Hermes</button>
            <span class="sw-status"></span>
          </div>
        </div>
      </div>`;

    const state = swState(panel);

    panel.querySelector(".sw-filter").oninput = (e) => {
      state.filter = e.target.value;
      submitWorkRenderList(panel);
    };
    panel.querySelector(".sw-reload").onclick = () => {
      submitWorkLoad(panel); swLoadParentCards(panel); swLoadProfiles(panel);
    };
    panel.querySelector(".sw-blocked-toggle").onclick = () => {
      swPrefSave(SW_SHOW_BLOCKED_KEY, !swPrefLoad(SW_SHOW_BLOCKED_KEY, false));
      submitWorkRenderList(panel);
    };
    panel.querySelector(".sw-ui-toggle").onclick = () => {
      swPrefSave(SW_SHOW_UI_KEY, !swPrefLoad(SW_SHOW_UI_KEY, false));
      submitWorkRenderList(panel);
    };
    panel.querySelector(".sw-sync").onclick = () => submitWorkSync(panel);
    panel.querySelector(".sw-submit").onclick = () =>
      (state.mode === "swarm" ? submitWorkSwarm(panel) : submitWorkSubmit(panel));

    panel.querySelector(".sw-mode").addEventListener("click", (e) => {
      const btn = e.target.closest(".sw-mode-btn");
      if(!btn || btn.dataset.mode === state.mode) return;
      state.mode = btn.dataset.mode;
      swRenderMode(panel);
    });

    // Add/remove rebuild the rows, so what is typed has to be read out of the
    // DOM first -- swCollectSwarm does that -- or removing worker 3 would
    // silently blank workers 1 and 2.
    panel.querySelector(".sw-swarm").addEventListener("click", (e) => {
      if(e.target.closest(".sw-worker-add")){
        swCollectSwarm(panel);
        if(state.workers.length < SW_SWARM_MAX_WORKERS){
          const used = state.workers.map(w => w.profile);
          const fresh = state.profiles.find(p => !used.includes(p.name));
          state.workers.push({profile: fresh ? fresh.name : (state.profiles[0] || {}).name,
                              title: ""});
        }
        swRenderSwarm(panel);
        return;
      }
      const del = e.target.closest(".sw-worker-del");
      if(del){
        swCollectSwarm(panel);
        const i = Number(del.closest(".sw-worker").dataset.i);
        if(state.workers.length > 1) state.workers.splice(i, 1);
        swRenderSwarm(panel);
      }
    });
    // Dropdowns move without a redraw; keep state in step so a later redraw
    // does not put the old profile back.
    panel.querySelector(".sw-swarm").addEventListener("change", () => swCollectSwarm(panel));
    panel.querySelector(".sw-clear").onclick = () => swClearSelection(panel);
    // Esc clears the selection, but only while the focus is actually in this
    // pane -- the HUD binds Esc globally to stop a voice run.
    panel.addEventListener("keydown", (e) => {
      if(e.key !== "Escape" || !state.selected) return;
      if(e.target.closest(".sw-notes")) return;   // let a textarea keep its own Esc
      e.stopPropagation();
      swClearSelection(panel);
    });

    // One delegated listener for the picker: section collapse and row select.
    panel.querySelector(".sw-list").addEventListener("click", (e) => {
      const head = e.target.closest(".sw-section-head");
      if(head){
        const section = head.closest(".sw-section").dataset.section;
        const collapsed = swPrefLoad(SW_COLLAPSED_KEY, {});
        collapsed[section] = !collapsed[section];
        swPrefSave(SW_COLLAPSED_KEY, collapsed);
        submitWorkRenderList(panel);
        return;
      }
      // The badge opens the existing card instead of selecting the row --
      // "this is already filed" is most useful when you can go look at it.
      const filed = e.target.closest(".sw-filed");
      if(filed){
        e.stopPropagation();
        if(typeof openTaskLog === "function") openTaskLog(filed.dataset.card);
        return;
      }
      const row = e.target.closest(".sw-row");
      if(!row || row.classList.contains("sw-row-blocked")) return;
      // Clicking the selected row again puts it back. Together with ✕ clear
      // and Esc that is three ways out of a selection, where there were none.
      state.selected = state.selected === row.dataset.id ? null : row.dataset.id;
      submitWorkRenderList(panel);
      submitWorkRenderSelected(panel);
    });

    panel.querySelector(".sw-parent").addEventListener("change", () => swLoadFollowUp(panel));

    // Clicking a suggestion selects that TODO item, leaving the parent set --
    // which is the whole point: the coding card is filed as a child of the
    // research, so its worker gets the findings without anyone pasting them.
    panel.querySelector(".sw-followup").addEventListener("click", (e) => {
      const row = e.target.closest(".sw-fu-item");
      if(!row) return;
      const state = swState(panel);
      state.selected = row.dataset.item;
      submitWorkRenderList(panel);
      submitWorkRenderSelected(panel);
      panel.querySelector(".sw-selected")?.scrollIntoView({block: "nearest"});
    });

    panel.querySelector(".sw-selected").addEventListener("click", (e) => {
      const open = e.target.closest(".sw-open-card");
      if(!open) return;
      e.preventDefault();
      if(typeof openTaskLog === "function") openTaskLog(open.dataset.card);
    });

    submitWorkRenderSelected(panel);
    swRenderMode(panel);
    submitWorkLoad(panel);
    swLoadParentCards(panel);
    // Once per open, not on the poll: profile directories on CT111 do not
    // move while you are looking at this form, and the server caches them.
    swLoadProfiles(panel);
    // The picker is a live view of two things that both move underneath it:
    // TODO.md on snarf and the board on hermes. Left as a one-shot read it
    // showed the state at the moment the tab was opened, forever.
    const iv = setInterval(() => {
      submitWorkLoad(panel, {quiet: true});
      swLoadParentCards(panel);
    }, SW_POLL_MS);
    tab.onBeforeClose = () => clearInterval(iv);
  });
}

document.querySelectorAll('[data-action="submit-work"]').forEach(b=>b.addEventListener("click", openSubmitWork));
