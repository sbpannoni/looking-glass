"use strict";
/* ======================= vLLM / GPU brain control ===================
   Start/stop/restart the vLLM systemd service on snarf (see vllm_control:
   in server.yaml). Depends on $/registerPanel from app.js/panels.js.
======================================================================= */
async function refreshVllmStatus(){
  try{
    const r=await fetch("/api/vllm/status"); const j=await r.json();
    const dot=$("vllmDot"), label=$("vllmState");
    if(j.error){ dot.className="dot off"; label.textContent="not configured"; return; }
    const active=j.active==="active";
    dot.className="dot "+(active?"on":"off");
    label.textContent=j.active||"unknown";
    label.className=active?"ok":j.active==="failed"?"err":"warn";
  }catch{ $("vllmDot").className="dot off"; $("vllmState").textContent="unreachable"; }
}

async function vllmAction(action){
  if((action==="stop"||action==="restart") &&
     !confirm(`${action.toUpperCase()} vLLM on snarf — this interrupts Hermes' agent brain. Continue?`)){
    return;
  }
  $("vllmState").textContent=action+"ing…";
  try{
    const r=await fetch("/api/vllm/action",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({action})});
    const j=await r.json();
    if(!j.ok)throw new Error(j.error||"failed");
    addActivity(`vllm ${action}: ok`);
  }catch(err){
    addActivity(`vllm ${action} error: ${err.message}`);
  }
  setTimeout(refreshVllmStatus,1500);
}

$("vllmStartBtn")?.addEventListener("click",()=>vllmAction("start"));
$("vllmStopBtn")?.addEventListener("click",()=>vllmAction("stop"));
$("vllmRestartBtn")?.addEventListener("click",()=>vllmAction("restart"));

registerPanel({id:"vllm", refresh:refreshVllmStatus, intervalMs:15000});
