"use strict";
/* ============================== panel registry ======================
   HOW TO ADD A NEW SIDE PANEL:
   1. Add a `<div class="panel">...</div>` block to index.html with
      whatever static markup/ids it needs (see MACHINES or PROJECTS for a
      template).
   2. Write an async function that fetches data and writes it into those
      ids (see refreshMachines/refreshProjects below).
   3. Call registerPanel({id: "myPanel", refresh: myRefreshFn, intervalMs: 30000}).
   registerPanel runs the refresh once immediately, then on the interval.
   Depends on `$` and `jget` from app.js (loaded first) — nothing here is
   voice/WS-pipeline specific, so this file is safe to extend freely.
======================================================================= */
const PANEL_REGISTRY = new Map();
function registerPanel({id, refresh, intervalMs}) {
  if (PANEL_REGISTRY.has(id)) unregisterPanel(id);
  refresh();
  const timer = intervalMs ? setInterval(refresh, intervalMs) : null;
  PANEL_REGISTRY.set(id, {refresh, timer});
}
function unregisterPanel(id) {
  const p = PANEL_REGISTRY.get(id);
  if (p?.timer) clearInterval(p.timer);
  PANEL_REGISTRY.delete(id);
}

/* ============================== widgets ============================ */
async function jget(path){const r=await fetch("/api/hermes"+path);if(!r.ok)throw new Error(r.status);return r.json()}
async function refreshHealth(){
  try{
    const h=await jget("/health/detailed");
    $("apiDot").className="dot on"; $("apiState").textContent="online";
    const st=h.sessions||h.session_stats||{}; const rs=h.resources||{};
    $("hSessions").textContent=st.active??st.total??"ok";
    $("hAgents").textContent=h.running_agents??h.agents??"0";
    $("hCpu").textContent=rs.cpu_percent!=null?rs.cpu_percent+"%":"—";
    $("hMem").textContent=rs.memory_percent!=null?rs.memory_percent+"%":(rs.rss||"—");
    $("hWhen").textContent=new Date().toTimeString().slice(0,8);
  }catch{ $("apiDot").className="dot off"; $("apiState").textContent="offline"; }
}
async function refreshSkills(){
  try{
    const s=await jget("/v1/skills"); const list=Array.isArray(s)?s:(s.data||s.skills||[]);
    $("skillCount").textContent=list.length;
    $("skillsList").innerHTML=list.slice(0,14).map(x=>`<div>▸ ${x.name||x}</div>`).join("");
  }catch{ $("skillCount").textContent="?"; }
}
async function refreshJobs(){
  try{
    const j=await jget("/api/jobs"); const list=Array.isArray(j)?j:(j.jobs||j.data||[]);
    $("jobsList").innerHTML=list.length?list.slice(0,8).map(x=>
      `<div>▸ ${x.name||x.prompt?.slice(0,38)||x.id} <span class="${x.paused?"warn":"ok"}">${x.paused?"paused":"on"}</span></div>`).join("")
      :"<div>— none —</div>";
  }catch{ $("jobsList").innerHTML="<div>—</div>"; }
}
async function refreshMachines(){
  try{
    const r=await fetch("/api/machines"); const j=await r.json();
    const rows=(j.machines||[]);
    if(!rows.length){ $("machinesList").innerHTML="<div class='kv'><span>probing…</span></div>"; return; }
    $("machinesList").innerHTML=rows.map(m=>{
      const bits=[];
      if(m.cpu!=null)bits.push(`CPU ${Math.round(m.cpu)}%`);
      if(m.load1!=null)bits.push(`load ${m.load1}`);
      if(m.mem!=null)bits.push(`MEM ${Math.round(m.mem)}%`);
      if(m.gpu_util!=null)bits.push(`GPU ${Math.round(m.gpu_util)}%`);
      if(m.vram_used!=null&&m.vram_total!=null)bits.push(`VRAM ${m.vram_used}/${m.vram_total}G`);
      if(m.gpu_temp!=null)bits.push(`${m.gpu_temp}°`);
      if(!bits.length&&m.note)bits.push(m.note);
      // kind is the useful disambiguator here — three of these rows are LXCs on
      // one mini-PC, and two are BMCs that stay up when their host is dark.
      const kind=m.kind?`<span class="mach-kind">${m.kind}</span>`:"";
      // depth 1 = a guest on the row above it (the LXCs sharing beelink's
      // hardware). Indenting makes the blast radius obvious: beelink going down
      // takes the three rows beneath it with it.
      const child=m.depth?" mach-child":"";
      // A node we deliberately don't probe is neither green nor red — a red dot
      // there is a false alarm that trains you to ignore red dots.
      const dot=m.monitored===false?"na":(m.online?"on":"off");
      return `<div class="kv mach-row${child}${m.monitored===false?" mach-na":""}" title="${m.address||""}">`+
             `<span><span class="dot ${dot}"></span>${m.name}${kind}</span>`+
             `<b>${bits.join(" · ")||(m.online?"online":"offline")}</b></div>`;
    }).join("");
  }catch{ $("machinesList").innerHTML="<div class='kv'><span>unavailable</span></div>"; }
}
const fmtK=n=>n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?(n/1e3).toFixed(1)+"k":String(n||0);
async function refreshUsage(){
  try{
    const r=await fetch("/api/usage"); const j=await r.json();
    const t=j.llm?.today||{};
    $("uTok").textContent=`${fmtK(t.llm_in||0)} in / ${fmtK(t.llm_out||0)} out`;
    $("uTurns").textContent=t.turns||0;
    if(j.llm?.today_cost!=null){$("uCostRow").style.display="flex";$("uCost").textContent="$"+j.llm.today_cost.toFixed(2)}
    const e=j.elevenlabs;
    if(e&&e.limit){
      const pct=Math.round(e.used/e.limit*100);
      $("uTts").textContent=`${fmtK(e.used)} / ${fmtK(e.limit)} (${100-pct}% left)`;
      $("ttsBar").style.width=pct+"%";
      $("uTts").className=pct>90?"err":pct>70?"warn":"";
    }else{
      // quota API unavailable (key needs user_read permission) → local tally
      $("uTts").textContent=fmtK(t.tts_chars||0)+" chars today";
      $("ttsBar").style.width="0%";
    }
  }catch{}
}
async function refreshProjects(){
  try{
    const r=await fetch("/api/projects"); const j=await r.json();
    $("projectsList").innerHTML=(j.projects||[]).map(p=>{
      if(p.error)return `<div class="kv"><span><span class="dot off"></span>${p.name}</span><b>${p.error}</b></div>`;
      const pct=p.total?Math.round(p.done/p.total*100):0;
      return `<div class="kv"><span>${p.name}</span><b>${p.done}/${p.total}</b></div>
        <div class="bar"><i style="width:${pct}%"></i></div>`;
    }).join("")||"<div class='kv'><span>no projects configured</span></div>";
  }catch{ $("projectsList").innerHTML="<div class='kv'><span>unavailable</span></div>"; }
}

registerPanel({id:"health",    refresh:refreshHealth,   intervalMs:15000});
registerPanel({id:"jobs",      refresh:refreshJobs,     intervalMs:60000});
async function refreshBrain(){
  try{
    const r=await fetch("/api/brain"); const j=await r.json();
    const el=document.getElementById("mBrainModel"); if(!el) return;
    if(j.model){
      const ctx=j.max_model_len?` · ${Math.round(j.max_model_len/1024)}k ctx`:"";
      el.textContent=j.model+ctx; el.className="";
    }else{ el.textContent="offline"; el.className="err"; }
  }catch{}
}
registerPanel({id:"brain", refresh:refreshBrain, intervalMs:60000});
registerPanel({id:"machines",  refresh:refreshMachines, intervalMs:10000});
registerPanel({id:"skills",    refresh:refreshSkills,   intervalMs:120000});
registerPanel({id:"projects",  refresh:refreshProjects, intervalMs:120000});
registerPanel({id:"usage",     refresh:refreshUsage,    intervalMs:60000});
