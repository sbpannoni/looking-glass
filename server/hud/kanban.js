"use strict";
/* ============================== KANBAN =============================
   The view for managing agentic work.

   The HUD's own chat session is not where work happens — each kanban card
   runs in its own Hermes session and workspace. This shows the board, and
   lets you open any task's live run log, which is the actual "what is the
   agent doing right now" view.

   Depends on $/openWorkTab from app.js.
=================================================================== */
const KANBAN_STATUS_CLASS = {
  running:"ok", done:"ok", ready:"warn", todo:"", blocked:"err",
  review:"warn", scheduled:"", triage:"warn", archived:"",
};

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

function renderKanban(panel, tasks, err){
  const list = panel.querySelector(".kb-list");
  if(err){ list.innerHTML = `<div class="kv"><span class="err">board unavailable: ${err}</span></div>`; return; }
  if(!tasks.length){ list.innerHTML = `<div class="kv"><span>no cards on the board</span></div>`; return; }
  list.innerHTML = tasks.map(t=>{
    const cls = KANBAN_STATUS_CLASS[t.status] ?? "";
    const live = t.status === "running";
    return `<div class="kb-card${live?" live":""}" data-id="${t.id}">
      <div class="kb-row">
        <b class="${cls}">${t.status}${live?" ●":""}</b>
        <span class="kb-age">${kanbanAge(t)}</span>
      </div>
      <div class="kb-title">${(t.title||"").replace(/[<>&]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]))}</div>
      <div class="kb-meta">${t.assignee||"—"} · ${t.id}</div>
    </div>`;
  }).join("");
  list.querySelectorAll(".kb-card").forEach(card=>{
    card.onclick = () => openTaskLog(card.dataset.id);
  });
}

async function refreshKanbanPanel(panel){
  try{
    const r = await fetch("/api/kanban");
    const j = await r.json();
    renderKanban(panel, j.tasks||[], j.error);
  }catch(err){ renderKanban(panel, [], err.message); }
}

function openKanbanBoard(){
  openWorkTabTurning("kanban","board","KANBAN",(panel,tab)=>{
    panel.innerHTML = `<div class="kb-head">BOARD — click a card to follow its run log</div>
                       <div class="kb-list"><div class="kv"><span>loading…</span></div></div>`;
    panel.classList.add("kanban-pane");
    refreshKanbanPanel(panel);
    // Cards change state on the dispatcher's tick; keep it current but light.
    const iv = setInterval(()=>refreshKanbanPanel(panel), 15000);
    tab.onBeforeClose = () => clearInterval(iv);
  });
}

/* A task's run log is the live transcript of Hermes working. Polled rather
   than streamed: the log is a file on another box, and a 3s poll is far
   simpler than plumbing a second websocket for something read-only. */
function openTaskLog(taskId){
  openWorkTabTurning("tasklog",taskId,taskId,(panel,tab)=>{
    panel.innerHTML = `<pre class="kb-log">loading…</pre>`;
    panel.classList.add("tasklog-pane");
    const pre = panel.querySelector(".kb-log");
    let stopped = false;
    const pull = async () => {
      if(stopped) return;
      try{
        const r = await fetch(`/api/kanban/${encodeURIComponent(taskId)}/log?lines=400`);
        const j = await r.json();
        const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 60;
        pre.textContent = j.log || j.error || "(no log yet — the task may not have started)";
        if(atBottom) pre.scrollTop = pre.scrollHeight;
      }catch(err){ pre.textContent = "log unavailable: "+err.message; }
    };
    pull();
    const iv = setInterval(pull, 3000);
    tab.onBeforeClose = () => { stopped = true; clearInterval(iv); };
  });
}

document.querySelectorAll('[data-action="kanban"]').forEach(btn=>{
  btn.addEventListener("click", openKanbanBoard);
});
