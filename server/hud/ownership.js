"use strict";
/* ========================== PROJECT FLOW ===========================
   Which node owns which domain, and which agent has native context.

   This view exists because the routing was tribal knowledge: Hermes is
   configured on CT111 but has no agent installed there, the rack's power
   and monitoring history lives on CT110, and the HUD lives on CT112 — so
   work got started on the wrong box and re-derived findings another node
   had already written down. The diagram is the answer to "where do I go
   to change this?", with live status so it doubles as a health view.

   Depends on openWorkTabTurning from app.js.
================================================================= */

const AGENT_LABEL = {
  native: "claude · native context",
  none:   "no agent installed",
};

function flowCard(d){
  const virt   = d.virt ? `<span class="flow-virt">${d.virt.id}${d.virt.name?" · "+d.virt.name:""}</span>` : "";
  const dot    = d.online===false ? "off" : (d.online ? "on" : "na");
  const agent  = AGENT_LABEL[d.agent] || d.agent || "";
  const stew   = d.steward ? `<div class="flow-steward">↳ driven from <b>${d.steward}</b> over ssh</div>` : "";
  // A lease is a boundary that is currently DOWN. Always render it with its
  // exit condition: the failure mode of a temporary grant is nobody noticing
  // it stopped being temporary, so a lease with no `until` says so out loud.
  const lease  = d.lease
    ? `<div class="flow-lease">⇄ open to <b>${d.lease.to}</b>${d.lease.since?" since "+d.lease.since:""}
         <span class="flow-lease-until">until ${d.lease.until || "— NO EXIT CONDITION SET"}</span></div>`
    : "";
  const owns   = (d.owns||[]).map(o=>`<li>${o}</li>`).join("");
  const proj   = (d.projects||[]).map(p=>`<span class="flow-proj">${p}</span>`).join("");
  const repo   = d.repo ? `<div class="flow-repo">${d.repo}</div>` : "";
  const note   = d.note ? `<div class="flow-note">${d.note}</div>` : "";
  return `<div class="flow-card ${d.agent==="none"?"noagent":""} ${d.lease?"leased":""}">
    <div class="flow-head">
      <span class="dot ${dot}"></span><b>${d.label}</b>
    </div>
    <div class="flow-node">${d.node}${virt}</div>
    <div class="flow-path">${d.path||""}</div>
    ${repo}
    <div class="flow-agent ${d.agent==="none"?"err":"ok"}">${agent}</div>
    ${stew}
    ${lease}
    ${owns?`<ul class="flow-owns">${owns}</ul>`:""}
    ${proj?`<div class="flow-projects">${proj}</div>`:""}
    ${note}
  </div>`;
}

async function renderFlow(panel){
  try{
    const r = await fetch("/api/ownership");
    const j = await r.json();
    const cards = (j.domains||[]).map(flowCard).join("");
    const unowned = (j.unowned||[]).length
      ? `<div class="flow-unowned"><b>Tracked but unowned</b> — no checkout anywhere in the fleet,
           so no agent has native context: ${j.unowned.map(p=>`<span class="flow-proj">${p}</span>`).join("")}</div>`
      : "";
    panel.innerHTML = `<div class="flow-head-bar">WHERE WORK LIVES — open a terminal on the node that owns it</div>
                       <div class="flow-grid">${cards}</div>${unowned}`;
    // Clicking a card opens a terminal on the owning node: the whole point is to
    // get you working on the right box, not just to tell you which one it is.
    panel.querySelectorAll(".flow-card").forEach((el,i)=>{
      const node = (j.domains[i]||{}).node;
      if(node && typeof openTerminal === "function"){
        el.style.cursor = "pointer";
        el.title = "Open a terminal on "+node;
        el.onclick = () => openTerminal(node);
      }
    });
  }catch(err){
    panel.innerHTML = `<div class="kv"><span class="err">flow unavailable: ${err.message}</span></div>`;
  }
}

function openProjectFlow(){
  openWorkTabTurning("flow","main","PROJECT FLOW",(panel,tab)=>{
    panel.innerHTML = `<div class="kv"><span>loading…</span></div>`;
    panel.classList.add("flow-pane");
    renderFlow(panel);
    const iv = setInterval(()=>renderFlow(panel), 30000);
    tab.onBeforeClose = () => clearInterval(iv);
  });
}

document.querySelectorAll('[data-action="flow"]').forEach(b=>b.addEventListener("click", openProjectFlow));
