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
================================================================= */

const SW_COLLAPSED_KEY = "lg-sw-collapsed";
const SW_SHOW_BLOCKED_KEY = "lg-sw-show-blocked";
const SW_SHOW_UI_KEY = "lg-sw-show-ui";
/* How often the picker re-reads the board while it is open. The "filed"
   badges ARE the in-flight state of everything on this list, and they used
   to be a snapshot taken when the pane opened: a card could go triage ->
   running -> done with this view still showing the moment it was filed. */
const SW_POLL_MS = 20000;

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
    panel._sw = {items: [], selected: null, filter: ""};
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
  const cls = (typeof KANBAN_STATUS_CLASS !== "undefined"
               ? KANBAN_STATUS_CLASS[filed.status] : "") || "";
  return `<span class="sw-filed" data-card="${submitWorkEsc(filed.id)}"
    title="Already filed as ${submitWorkEsc(filed.id)} — click to open its run log"
    >filed · <b class="${cls}">${submitWorkEsc(filed.status || "?")}</b></span>`;
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
         (${submitWorkEsc(item.filed_as.status || "?")}). Submitting with no extra
         instructions returns that same card rather than making a second one.</div>`
    : "";
  box.innerHTML = `<div class="sw-selected-section">${submitWorkEsc(item.section || "")}</div>
    ${uiWarn}
    ${filed}
    <div class="sw-selected-text">${submitWorkEsc(item.text).replace(/\n/g,"<br>")}</div>`;
  submitBtn.disabled = false;
  swRenderParentOptions(panel);
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

/* Everything the list actually draws. A background poll that redraws an
   unchanged list would throw away scroll position every 20 seconds, which is
   worse than the staleness it fixes -- so a quiet poll only touches the DOM
   when this string moves. */
function swItemsSignature(items){
  return items.map(it => [it.id, it.blocked, it.needs_ui, it.wip,
                          it.filed_as ? it.filed_as.id + ":" + it.filed_as.status : ""
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
          <div class="sw-bar">BUILDS ON (OPTIONAL)</div>
          <select class="sw-parent" title="File this as a child of an existing card: the board holds it until that card closes, and its worktree is branched from that card's branch instead of origin/master — so it starts with that work already in the tree."></select>
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
      submitWorkLoad(panel); swLoadParentCards(panel);
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
    panel.querySelector(".sw-submit").onclick = () => submitWorkSubmit(panel);
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

    panel.querySelector(".sw-selected").addEventListener("click", (e) => {
      const open = e.target.closest(".sw-open-card");
      if(!open) return;
      e.preventDefault();
      if(typeof openTaskLog === "function") openTaskLog(open.dataset.card);
    });

    submitWorkRenderSelected(panel);
    submitWorkLoad(panel);
    swLoadParentCards(panel);
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
