"use strict";
/* ============================== SUBMIT WORK ==========================
   One-click work submission: pick a real DARKHELIX TODO.md item, add
   optional instructions, submit -- files a real kanban card via
   /api/kanban/create, always to --triage (Hermes's specifier still fleshes
   it out and decomposes it before anything executes; see server.py's
   module note on /api/kanban/create for why this is a deliberate,
   reviewed choice, not a default left unconsidered).

   Deliberately simple: a filterable list (single-select, radio behavior),
   one optional textarea, one button. No second screen, no confirmation
   dialog -- the safety gate is server-side (--triage), not UI friction.

   On success this jumps straight into the new card's task-log pane
   (openTaskLog, from kanban.js) instead of leaving you stranded here with
   just a text confirmation -- filing a card used to be a dead end with no
   way to see it move.
================================================================= */

let submitWorkItems = [];
let submitWorkSelected = null;

function submitWorkEsc(s){
  return (s||"").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
}

function submitWorkRow(item){
  const disabled = item.blocked;
  const cls = ["sw-row"];
  if (disabled) cls.push("sw-row-blocked");
  if (submitWorkSelected === item.id) cls.push("sw-row-selected");
  const badge = item.blocked
    ? `<span class="badge flaky">blocked</span>`
    : item.wip
      ? `<span class="badge">WIP</span>`
      : "";
  return `<div class="${cls.join(" ")}" data-id="${item.id}">
    <div class="sw-row-section">${submitWorkEsc(item.section || "")}</div>
    <div class="sw-row-title">${submitWorkEsc(item.title)}${badge}</div>
  </div>`;
}

function submitWorkRenderList(panel, filterText){
  const list = panel.querySelector(".sw-list");
  const q = (filterText || "").toLowerCase();
  const filtered = submitWorkItems.filter(it =>
    !q || it.title.toLowerCase().includes(q) || (it.section||"").toLowerCase().includes(q)
  );
  list.innerHTML = filtered.length
    ? filtered.map(submitWorkRow).join("")
    : `<div class="kv"><span>no matching items</span></div>`;
  list.querySelectorAll(".sw-row:not(.sw-row-blocked)").forEach(row => {
    row.onclick = () => {
      submitWorkSelected = row.dataset.id;
      submitWorkRenderList(panel, panel.querySelector(".sw-filter").value);
      submitWorkRenderSelected(panel);
    };
  });
}

function submitWorkRenderSelected(panel){
  const box = panel.querySelector(".sw-selected");
  const submitBtn = panel.querySelector(".sw-submit");
  const item = submitWorkItems.find(it => it.id === submitWorkSelected);
  if (!item){
    box.innerHTML = `<div class="kv"><span>select an item above</span></div>`;
    submitBtn.disabled = true;
    return;
  }
  box.innerHTML = `<div class="sw-selected-text">${submitWorkEsc(item.text).replace(/\n/g,"<br>")}</div>`;
  submitBtn.disabled = false;
}

async function submitWorkLoad(panel){
  panel.querySelector(".sw-list").innerHTML = `<div class="kv"><span>loading…</span></div>`;
  try{
    const r = await fetch("/api/darkhelix-todo");
    const j = await r.json();
    if (j.error){
      panel.querySelector(".sw-list").innerHTML = `<div class="kv"><span class="err">${submitWorkEsc(j.error)}</span></div>`;
      return;
    }
    submitWorkItems = j.items || [];
    submitWorkRenderList(panel, "");
  }catch(err){
    panel.querySelector(".sw-list").innerHTML = `<div class="kv"><span class="err">todo list unavailable: ${submitWorkEsc(err.message)}</span></div>`;
  }
}

async function submitWorkSubmit(panel){
  const item = submitWorkItems.find(it => it.id === submitWorkSelected);
  if (!item) return;
  const notes = panel.querySelector(".sw-notes").value.trim();
  const status = panel.querySelector(".sw-status");
  const submitBtn = panel.querySelector(".sw-submit");
  submitBtn.disabled = true;
  submitBtn.textContent = "Submitting…";
  status.innerHTML = "";

  const body = notes ? `${item.text}\n\n--- additional instructions ---\n${notes}` : item.text;
  try{
    const r = await fetch("/api/kanban/create", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({title: item.title, body}),
    });
    const j = await r.json();
    if (j.ok){
      const taskId = (j.task && (j.task.id || j.task.task_id)) || "?";
      status.innerHTML = `<div class="kv"><span class="ok">Filed to triage — task ${submitWorkEsc(String(taskId))}. Opening its log…</span></div>`;
      panel.querySelector(".sw-notes").value = "";
      submitWorkSelected = null;
      submitWorkRenderList(panel, panel.querySelector(".sw-filter").value);
      submitWorkRenderSelected(panel);
      if (taskId !== "?" && typeof openTaskLog === "function") openTaskLog(taskId);
    }else{
      status.innerHTML = `<div class="kv"><span class="err">${submitWorkEsc(j.error || "unknown error")}</span></div>`;
    }
  }catch(err){
    status.innerHTML = `<div class="kv"><span class="err">${submitWorkEsc(err.message)}</span></div>`;
  }
  submitBtn.textContent = "Submit to Hermes";
  submitBtn.disabled = !submitWorkSelected;
}

function openSubmitWork(){
  openWorkTabTurning("submit-work","main","SUBMIT WORK",(panel,tab)=>{
    panel.classList.add("flow-pane");
    panel.innerHTML = `
      <div class="flow-head-bar">SUBMIT WORK — DARKHELIX TODO.md, files to triage</div>
      <div class="sw-body">
        <input class="sw-filter" type="text" placeholder="filter…">
        <div class="sw-list"></div>
        <div class="flow-head-bar">SELECTED ITEM</div>
        <div class="sw-selected"><div class="kv"><span>select an item above</span></div></div>
        <div class="flow-head-bar">ADDITIONAL INSTRUCTIONS (OPTIONAL)</div>
        <textarea class="sw-notes" placeholder="anything to add or override…"></textarea>
        <button class="btn sw-submit" disabled>Submit to Hermes</button>
        <div class="sw-status"></div>
      </div>
    `;
    panel.querySelector(".sw-filter").oninput = (e) => submitWorkRenderList(panel, e.target.value);
    panel.querySelector(".sw-submit").onclick = () => submitWorkSubmit(panel);
    submitWorkLoad(panel);
  });
}

document.querySelectorAll('[data-action="submit-work"]').forEach(b=>b.addEventListener("click", openSubmitWork));
