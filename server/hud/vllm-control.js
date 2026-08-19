"use strict";
/* ====================== backend service control =====================
   Start/stop/restart the services the HUD depends on: the vLLM GPU brain
   on snarf, and the Hermes gateway (API) + dashboard on the hermes LXC.
   Driven entirely by service_control: in server.yaml — add an entry there
   and a row appears here, no JS change needed.

   Services flagged `critical` confirm before stop/restart, because they
   take down the very brain this HUD talks to. Stopping the gateway in
   particular makes the HUD's own chat and voice go dead until it's
   started again — the row says so rather than letting it surprise you.

   Depends on $/addActivity from app.js and registerPanel from panels.js.
==================================================================== */
const SERVICE_STATE_CLASS = {active:"ok", failed:"err", unreachable:"err"};

function serviceRowHTML(s){
  const cls = SERVICE_STATE_CLASS[s.active] || "warn";
  const dot = s.active === "active" ? "on" : "off";
  return `
    <div class="svc-row" data-id="${s.id}">
      <div class="kv">
        <span><span class="dot ${dot}"></span>${s.label}</span>
        <b class="${cls}">${s.active}</b>
      </div>
      <div class="svc-btns">
        <button class="btn" data-act="start">START</button>
        <button class="btn amber" data-act="restart">RESTART</button>
        <button class="btn danger" data-act="stop">STOP</button>
      </div>
    </div>`;
}

async function refreshServices(){
  const box = $("servicesList");
  if(!box) return;
  try{
    const r = await fetch("/api/services");
    const j = await r.json();
    const list = j.services || [];
    if(!list.length){ box.innerHTML = "<div class='kv'><span>none configured</span></div>"; return; }
    box.innerHTML = list.map(serviceRowHTML).join("");
    list.forEach(s=>{
      const row = box.querySelector(`.svc-row[data-id="${s.id}"]`);
      row.querySelectorAll("button[data-act]").forEach(btn=>{
        btn.onclick = () => serviceAction(s, btn.dataset.act);
      });
    });
  }catch{
    box.innerHTML = "<div class='kv'><span>unavailable</span></div>";
  }
}

async function serviceAction(svc, action){
  if(svc.critical && action !== "start"){
    const extra = svc.id === "hermes-gateway"
      ? " This HUD's own chat and voice will stop working until it is started again."
      : " This takes Hermes' brain offline.";
    if(!confirm(`${action.toUpperCase()} ${svc.label}?${extra}`)) return;
  }
  addActivity(`${svc.id}: ${action}…`);
  try{
    const r = await fetch(`/api/services/${encodeURIComponent(svc.id)}/action`,{
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action}),
    });
    const j = await r.json();
    addActivity(`${svc.id} ${action}: ${j.ok ? "ok" : (j.error || "failed")}`);
  }catch(err){
    addActivity(`${svc.id} ${action} error: ${err.message}`);
  }
  // systemd returns as soon as it has spawned the unit; the process needs
  // longer before it is actually serving (vLLM in particular takes ~30-60s
  // to load the model), so poll rather than trusting the response.
  setTimeout(refreshServices, 1500);
  setTimeout(refreshServices, 6000);
}

registerPanel({id:"services", refresh:refreshServices, intervalMs:20000});
