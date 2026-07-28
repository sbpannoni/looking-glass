"use strict";
/* ============================== theme ============================== */
const THEME_KEY = "jarvis-theme";

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  applyTheme(next);
  localStorage.setItem(THEME_KEY, next);
}

(function initTheme() {
  const stored = localStorage.getItem(THEME_KEY);
  if (stored === "dark" || stored === "light") {
    applyTheme(stored);
  }
})();

/* ============================== state ============================== */
const CONV = "jarvis-main";
let ws=null, wsReady=false, capturing=false, state="standby";
let audioCtx=null, workletNode=null, mediaStream=null, srcNode=null;
let playhead=0, activeSources=[], leftoverByte=null, audioArrived=false;
let level=0, turns=0;

const $=id=>document.getElementById(id);
const feed=$("feed");

/* ============================== clock ============================== */
setInterval(()=>{const d=new Date();$("clock").textContent=d.toTimeString().slice(0,8)},500);

/* ============================== status indicator ==================== */
const STATUS_DOT_CLASSES=["standby","listening","thinking","tool","speaking","error"];
function setState(st,label,hint){
  state=st;
  $("stateLabel").textContent=label;
  if(hint!==undefined)$("stateHint").textContent=hint;
  const dot=$("status-indicator");
  STATUS_DOT_CLASSES.forEach(c=>dot.classList.remove(c));
  dot.classList.add(st);
}

/* ============================== feed =============================== */
function addMsg(cls,text){
  const d=document.createElement("div"); d.className="msg "+cls; d.textContent=text;
  feed.appendChild(d); feed.scrollTop=feed.scrollHeight;
  while(feed.children.length>80)feed.removeChild(feed.firstChild);
  return d;
}
function addActivity(text,toolName,preview){
  const a=$("activity");
  if(a.firstChild&&a.firstChild.textContent.startsWith("—"))a.innerHTML="";
  const d=document.createElement("div");
  const esc=s=>String(s||"").replace(/[<>&]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
  d.innerHTML=toolName?`▸ tool <b>${esc(toolName)}</b> <span style="opacity:.7">${esc((preview||"").slice(0,60))}</span>`:`▸ ${esc(text)}`;
  a.prepend(d); while(a.children.length>40)a.removeChild(a.lastChild);
}

/* live partial transcript bubble */
let liveEl=null, currentRun=null;
function showLive(text){
  if(!text)return;
  if(!liveEl){liveEl=addMsg("you live","");}
  liveEl.textContent=text; feed.scrollTop=feed.scrollHeight;
}
function clearLive(){ if(liveEl){liveEl.remove(); liveEl=null;} }
function showStop(on){ $("stopBtn").style.display=on?"block":"none"; }
function stopRun(){
  stopPlayback();
  if(wsReady)ws.send(JSON.stringify({type:"stop_run"}));
  showStop(false);
}

/* approval cards */
function showApproval(e){
  const data=e.data||{};
  const esc=s=>String(s||"").replace(/[<>&]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
  const id=data.approval_id||data.id||"";
  const desc=data.preview||data.command||data.description||JSON.stringify(data).slice(0,300);
  const card=document.createElement("div"); card.className="appr";
  card.innerHTML=`<h3>⚠ APPROVAL REQUIRED</h3><pre>${esc(desc)}</pre>
    <div class="row"><button class="btn">ALLOW</button><button class="btn danger">DENY</button></div>`;
  const [allowB,denyB]=card.querySelectorAll("button");
  const send=dec=>{ if(wsReady)ws.send(JSON.stringify({type:"approval_decision",run_id:e.run_id||currentRun,approval_id:id,decision:dec})); card.remove(); };
  allowB.onclick=()=>send("allow"); denyB.onclick=()=>send("deny");
  $("approvals").appendChild(card);
  addActivity("approval requested");
}

/* ============================== websocket ========================== */
function wsUrl(){return (location.protocol==="https:"?"wss://":"ws://")+location.host+"/ws"}
function connect(){
  ws=new WebSocket(wsUrl()); ws.binaryType="arraybuffer";
  ws.onopen=()=>{wsReady=true;$("wsDot").className="dot on";$("wsState").textContent="online";$("connState").textContent="LINK ACTIVE";$("footMsg").textContent="VOICE SERVER CONNECTED"};
  ws.onclose=()=>{wsReady=false;$("wsDot").className="dot off";$("wsState").textContent="offline";$("connState").textContent="LINK DOWN";setState("standby","STANDBY");setTimeout(connect,3000)};
  ws.onerror=()=>{};
  ws.onmessage=ev=>{
    if(ev.data instanceof ArrayBuffer){playChunk(ev.data);return}
    let e; try{e=JSON.parse(ev.data)}catch{return}
    if(e.type==="summon_panel"){ summonPanel(e); }
    else if(e.type==="dismiss_panels"){ dismissAllPanels(); }
    else if(e.type==="partial_transcript"){ showLive(e.text); }
    else if(e.type==="transcript"){ clearLive(); if(e.text)addMsg("you",e.text); }
    else if(e.type==="run_started"){ currentRun=e.run_id; showStop(true); }
    else if(e.type==="approval_request"){ showApproval(e); }
    else if(e.type==="agent_status"){
      if(e.state==="thinking"){setState("thinking","PROCESSING");showStop(true)}
      else if(e.state==="tool_use"){setState("tool","WORKING");addActivity("",e.tool||"tool",e.preview)}
      else if(e.state==="speaking")setState("speaking","SPEAKING");
      else if(e.state==="stopped"){setState("standby","STANDBY");showStop(false);stopPlayback();addMsg("sys","run stopped")}
    }
    else if(e.type==="error"){ clearLive(); addMsg("sys","error: "+e.message); setState("standby","STANDBY"); showStop(false); }
    else if(e.type==="done"){
      currentRun=null; showStop(false);
      const tm=e.timing||{};
      turns++; $("mTurns").textContent=turns;
      if(tm.end_of_speech_to_first_audio_seconds!=null)$("mFirst").textContent=tm.end_of_speech_to_first_audio_seconds+"s";
      if(tm.total_turn_seconds!=null)$("mTotal").textContent=tm.total_turn_seconds+"s";
      $("mBrain").textContent=tm.llm_provider||"—";
      if(tm.response_text)addMsg("jarvis",tm.response_text);
      setTimeout(()=>{if(state!=="listening")setState("standby","STANDBY")},400);
    }
  };
}
connect();

/* ============================== audio out ========================== */
function ensureCtx(){
  if(!audioCtx){
    try{ audioCtx=new (window.AudioContext||window.webkitAudioContext)({sampleRate:16000}); }
    catch{ audioCtx=new (window.AudioContext||window.webkitAudioContext)(); }
  }
  if(audioCtx.state==="suspended")audioCtx.resume();
}
function playChunk(buf){
  ensureCtx();
  let bytes=new Uint8Array(buf);
  if(leftoverByte!==null){const m=new Uint8Array(bytes.length+1);m[0]=leftoverByte;m.set(bytes,1);bytes=m;leftoverByte=null}
  if(bytes.length%2===1){leftoverByte=bytes[bytes.length-1];bytes=bytes.subarray(0,bytes.length-1)}
  if(!bytes.length)return;
  const i16=new Int16Array(bytes.buffer,bytes.byteOffset,bytes.length/2);
  const f32=new Float32Array(i16.length);
  for(let i=0;i<i16.length;i++)f32[i]=i16[i]/32768;
  const ab=audioCtx.createBuffer(1,f32.length,16000); ab.copyToChannel(f32,0);
  const src=audioCtx.createBufferSource(); src.buffer=ab; src.connect(audioCtx.destination);
  const t=Math.max(audioCtx.currentTime+0.06,playhead);
  src.start(t); playhead=t+ab.duration;
  activeSources.push(src); src.onended=()=>{activeSources=activeSources.filter(x=>x!==src)};
  audioArrived=true;
}
function stopPlayback(){ activeSources.forEach(s=>{try{s.stop()}catch{}}); activeSources=[]; playhead=0; }

/* ============================== audio in =========================== */
const workletCode=`
class PCM16K extends AudioWorkletProcessor{
  constructor(){super();this.frac=0;this.acc=0;this.n=0;this.out=[];}
  process(inputs){
    const ch=inputs[0][0]; if(!ch)return true;
    if(sampleRate===16000){            // context already at 16k: pass through
      for(let i=0;i<ch.length;i++){
        const v=Math.max(-1,Math.min(1,ch[i])); this.out.push(v*32767|0);
      }
    }else{                              // averaging (box-filter) downsample
      for(let i=0;i<ch.length;i++){
        this.acc+=ch[i]; this.n++; this.frac+=16000;
        if(this.frac>=sampleRate){ this.frac-=sampleRate;
          const v=Math.max(-1,Math.min(1,this.acc/this.n));
          this.out.push(v*32767|0); this.acc=0; this.n=0; }
      }
    }
    if(this.out.length>=1280){ // 80ms
      const a=new Int16Array(this.out.splice(0,1280));
      this.port.postMessage(a.buffer,[a.buffer]);
    }
    return true;
  }
}
registerProcessor("pcm16k",PCM16K);`;
let workletReady=false;
async function initMic(){
  ensureCtx();
  if(!workletReady){
    const url=URL.createObjectURL(new Blob([workletCode],{type:"application/javascript"}));
    await audioCtx.audioWorklet.addModule(url); workletReady=true;
  }
  if(!mediaStream){
    mediaStream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,sampleRate:16000,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
    srcNode=audioCtx.createMediaStreamSource(mediaStream);
    workletNode=new AudioWorkletNode(audioCtx,"pcm16k");
    workletNode.port.onmessage=e=>{
      if(capturing&&wsReady)ws.send(e.data);
      // level meter
      const a=new Int16Array(e.data); let s=0;
      for(let i=0;i<a.length;i+=8)s+=Math.abs(a[i]);
      level=Math.min(1,(s/(a.length/8))/9000);
      if(capturing)$("levelBar").style.width=(level*100).toFixed(0)+"%";
    };
    srcNode.connect(workletNode);
  }
  $("micDot").className="dot on"; $("micState").textContent="ready";
}

/* ============================== talk flow ========================== */
async function toggleTalk(){
  if(!wsReady){addMsg("sys","voice server offline");return}
  if(capturing){ // stop talking
    capturing=false;
    ws.send(JSON.stringify({type:"stop"}));
    $("talkBtn").textContent="ENGAGE VOICE"; $("micState").textContent="ready"; $("levelBar").style.width="0%";
    setState("thinking","PROCESSING");
    return;
  }
  try{await initMic()}catch(err){addMsg("sys","mic blocked: "+err.message);return}
  stopPlayback();                       // barge-in
  audioArrived=false;
  ws.send(JSON.stringify({type:"start",sample_rate:16000,format:"pcm_s16le",channels:1,conversation:CONV}));
  capturing=true;
  $("talkBtn").textContent="■ STOP & SEND"; $("micState").textContent="LIVE";
  setState("listening","LISTENING","CLICK AGAIN TO SEND");
}
$("talkBtn").onclick=toggleTalk;
$("reactorWrap").onclick=toggleTalk;
$("stopBtn").onclick=stopRun;

/* ---- unified work-area tabs (terminals + views share one system) ---- */
const DASH_PROXY=`https://${location.hostname}:9443`;
const workTabs=new Map(); // id -> {type, title, term?, socket?, iframe?}

function tabId(type,key){return `${type}:${key}`}

function updateWorkAreaVisibility(){
  $("work-area").classList.toggle("has-tabs",workTabs.size>0);
}

function activateWorkTab(id){
  document.querySelectorAll(".work-tab").forEach(b=>b.classList.toggle("active",b.dataset.tabId===id));
  document.querySelectorAll(".work-panel").forEach(p=>p.classList.toggle("active",p.dataset.tabId===id));
}

function closeWorkTab(id){
  const tab=workTabs.get(id);
  if(!tab)return;
  if(tab.socket)tab.socket.close();
  if(tab.term)tab.term.dispose();
  document.querySelector(`.work-tab[data-tab-id="${id}"]`)?.remove();
  document.querySelector(`.work-panel[data-tab-id="${id}"]`)?.remove();
  workTabs.delete(id);
  const remaining=[...workTabs.keys()];
  if(remaining.length)activateWorkTab(remaining[remaining.length-1]);
  updateWorkAreaVisibility();
}

function openWorkTab(type,key,title,buildContent){
  const id=tabId(type,key);
  if(workTabs.has(id)){activateWorkTab(id);return workTabs.get(id)}

  const tabBtn=document.createElement("div");
  tabBtn.className="work-tab"; tabBtn.dataset.tabId=id;
  const label=document.createElement("span"); label.textContent=title;
  const close=document.createElement("span"); close.className="work-tab-close"; close.textContent="✕";
  label.onclick=()=>activateWorkTab(id);
  close.onclick=e=>{e.stopPropagation();closeWorkTab(id)};
  tabBtn.append(label,close);
  $("work-tabs").appendChild(tabBtn);

  const panel=document.createElement("div");
  panel.className="work-panel"; panel.dataset.tabId=id;
  $("work-content").appendChild(panel);

  const tab={type,title};
  workTabs.set(id,tab);

  // Activate + reveal the work-area BEFORE building content: xterm.js measures
  // its container's size at Terminal.open() time, so the panel must already
  // be display:block, or it sizes to zero. No CSS transition on #work-area's
  // reveal (deliberately - see styles.css), so this is a plain synchronous
  // show/hide with no animation to race against.
  activateWorkTab(id);
  updateWorkAreaVisibility();
  buildContent(panel,tab);
  return tab;
}

function openView(name,path){
  openWorkTab("view",path,name,(panel)=>{
    const iframe=document.createElement("iframe");
    iframe.className="work-view-iframe";
    iframe.src=DASH_PROXY+path;
    iframe.title=name;
    panel.appendChild(iframe);
  });
}

function openTerminal(host){
  openWorkTab("terminal",host,host,(panel,tab)=>{
    const term=new Terminal({convertEol:true, fontSize:13});
    term.open(panel);
    const proto=location.protocol==="https:"?"wss:":"ws:";
    const socket=new WebSocket(`${proto}//${location.host}/ws/terminal/${host}`);
    socket.onmessage=ev=>term.write(ev.data);
    socket.onclose=()=>term.write("\r\n[connection closed]\r\n");
    term.onData(data=>{if(socket.readyState===WebSocket.OPEN)socket.send(data)});
    tab.term=term; tab.socket=socket;
  });
}

document.querySelectorAll(".terminal-tile").forEach(btn=>{
  btn.addEventListener("click",()=>openTerminal(btn.dataset.host));
});

addEventListener("keydown",e=>{
  if(e.key!=="Escape")return;
  if(document.querySelector(".holo:not(.dismiss)"))dismissAllPanels();
  else stopRun();
});
addEventListener("keydown",e=>{
  if(e.code==="Space"&&document.activeElement!==$("chatInput")){e.preventDefault();toggleTalk()}
});

/* ============================== typed chat ========================= */
async function sendChat(){
  const inp=$("chatInput"); const text=inp.value.trim(); if(!text)return;
  inp.value=""; addMsg("you",text); setState("thinking","PROCESSING");
  try{
    const r=await fetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({input:text,conversation:CONV})});
    const j=await r.json();
    if(!r.ok)throw new Error(j.error||r.status);
    (j.tools||[]).forEach(t=>addActivity("",t.name,t.preview));
    addMsg("jarvis",j.text||"(no reply)");
    if(/^\/(new|reset|clear)\b/i.test(text)){     // slash reset → also clear the visible feed
      setTimeout(()=>{feed.innerHTML="";addMsg("sys","conversation reset");},800);
    }
  }catch(err){ addMsg("sys","chat error: "+err.message); }
  if(state!=="listening")setState("standby","STANDBY");
}
$("sendBtn").onclick=sendChat;
$("clearBtn").onclick=()=>{feed.innerHTML="";addMsg("sys","chat cleared (history stays in Hermes — type /new to reset the conversation)");};
$("chatInput").addEventListener("keydown",e=>{if(e.key==="Enter")sendChat()});

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
    $("machinesList").innerHTML=(j.machines||[]).map(m=>{
      const bits=[];
      if(m.cpu!=null)bits.push(`CPU ${Math.round(m.cpu)}%`);
      if(m.mem!=null)bits.push(`MEM ${Math.round(m.mem)}%`);
      if(m.gpu_util!=null)bits.push(`GPU ${Math.round(m.gpu_util)}%`);
      if(m.vram_used!=null&&m.vram_total!=null)bits.push(`VRAM ${m.vram_used}/${m.vram_total}G`);
      if(m.gpu_temp!=null)bits.push(`${m.gpu_temp}°`);
      if(!bits.length&&m.note)bits.push(m.note);
      return `<div class="kv"><span><span class="dot ${m.online?"on":"off"}"></span>${m.name}</span><b>${bits.join(" · ")||(m.online?"online":"offline")}</b></div>`;
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
refreshHealth();refreshSkills();refreshJobs();refreshMachines();refreshUsage();
setInterval(refreshUsage,60000);
setInterval(refreshHealth,15000); setInterval(refreshJobs,60000); setInterval(refreshMachines,10000);
setInterval(refreshSkills,120000);

/* ====================== holographic media panels ==================== */
function holoWhoosh(up=true){
  try{
    ensureCtx();
    if(!audioCtx || audioCtx.state!=="running") return;   // needs prior user gesture
    const t=audioCtx.currentTime;
    const o1=audioCtx.createOscillator(), o2=audioCtx.createOscillator(), g=audioCtx.createGain();
    const f0=up?150:850, f1=up?850:120;
    o1.type="sawtooth"; o2.type="sine";
    o1.frequency.setValueAtTime(f0,t); o1.frequency.exponentialRampToValueAtTime(f1,t+.42);
    o2.frequency.setValueAtTime(f0*2.02,t); o2.frequency.exponentialRampToValueAtTime(f1*2.02,t+.42);
    g.gain.setValueAtTime(.0001,t);
    g.gain.exponentialRampToValueAtTime(.11,t+.07);
    g.gain.exponentialRampToValueAtTime(.0001,t+.55);
    o1.connect(g); o2.connect(g); g.connect(audioCtx.destination);
    o1.start(t); o2.start(t); o1.stop(t+.6); o2.stop(t+.6);
  }catch{}
}
function holoBootTicker(el){
  const lines=()=>Array.from({length:3},()=>
    "0x"+Math.random().toString(16).slice(2,8).toUpperCase()+"  "+
    (Math.random()*90).toFixed(4)+"N "+(Math.random()*180).toFixed(4)+"W");
  el.innerHTML=lines().join("<br>");
  const iv=setInterval(()=>{el.innerHTML=lines().join("<br>")},65);
  setTimeout(()=>{clearInterval(iv); el.style.opacity="0";
    setTimeout(()=>el.remove(),700)},1000);
}
function embedURL(src){
  const yt=src.match(/(?:youtube\.com\/(?:watch\?v=|shorts\/)|youtu\.be\/)([\w-]{6,})/);
  if(yt) return `https://www.youtube.com/embed/${yt[1]}?autoplay=1`;
  return src;
}
let lastSummon={key:"",t:0};
function summonPanel(opts){
  const o=opts||{};
  const media=o.media||o.type||"iframe", src=o.src||"", title=(o.title||"INCOMING FEED").toUpperCase();
  // dedupe: stale WS reconnects can deliver the same broadcast multiple times
  const key=media+"|"+src+"|"+title;
  if(key===lastSummon.key && Date.now()-lastSummon.t<8000) return null;
  lastSummon={key, t:Date.now()};
  const position=["left","right","center"].includes(o.position)?o.position:"center";
  const p=document.createElement("div");
  p.className="holo pos-"+position; p.dataset.fx="hologram";
  let inner="";
  const esrc=encodeURI(src);
  if(media==="image") inner=`<img class="holoContent" src="${esrc}" alt="">`;
  else if(media==="video" && !/youtube|youtu\.be/.test(src))
    inner=`<video class="holoContent" src="${esrc}" controls autoplay playsinline></video>`;
  else inner=`<iframe class="holoContent" src="${embedURL(src)}" allow="autoplay; fullscreen; encrypted-media"></iframe>`;
  p.innerHTML=`
    <svg class="holoFrame" preserveAspectRatio="none" viewBox="0 0 100 100">
      <rect x="0.5" y="0.5" width="99" height="99" pathLength="100" vector-effect="non-scaling-stroke"/>
      <path d="M0.5 8 V0.5 H8" vector-effect="non-scaling-stroke"/>
      <path d="M92 0.5 H99.5 V8" vector-effect="non-scaling-stroke"/>
      <path d="M99.5 92 V99.5 H92" vector-effect="non-scaling-stroke"/>
      <path d="M8 99.5 H0.5 V92" vector-effect="non-scaling-stroke"/>
    </svg>
    <div class="holoBar"><span>◈ ${title}</span><span class="hx" title="dismiss">✕</span></div>
    <div class="holoContentWrap">${inner}</div>
    <div class="holoHex"></div>
    <div class="holoScan"></div>
    <div class="holoChroma c1"></div>
    <div class="holoChroma c2"></div>
    <div class="holoBoot"></div>`;
  $("holoStage").appendChild(p);
  holoWhoosh(true);
  // camera dolly: the whole HUD recedes briefly while the panel arrives
  document.getElementById("grid").classList.add("dolly");
  setTimeout(()=>document.getElementById("grid").classList.remove("dolly"),900);
  holoBootTicker(p.querySelector(".holoBoot"));
  p.addEventListener("animationend",e=>{
    if(e.animationName==="holoApproach") p.classList.add("idle");
    if(e.animationName==="holoDismiss") p.remove();
  });
  p.querySelector(".hx").onclick=()=>dismissPanel(p);
  addActivity("hologram: "+title.toLowerCase());
  return p;
}
function dismissPanel(p){
  if(!p||p.classList.contains("dismiss"))return;
  p.classList.remove("idle");
  holoWhoosh(false);
  p.classList.add("dismiss");
  setTimeout(()=>p.remove(),700);  // safety net if animationend is missed
}
function dismissAllPanels(){document.querySelectorAll(".holo").forEach(dismissPanel)}
window.summonPanel=summonPanel; window.dismissAllPanels=dismissAllPanels;

/* ============================== auth gate ========================== */
async function checkAuth(){
  try{
    const r=await fetch("/api/usage");
    if(r.status===401){showPinGate();return false}
  }catch{}
  return true;
}
function showPinGate(){
  $("pinGate").style.display="flex";
  $("pinInput").focus();
}
$("pinBtn").onclick=async()=>{
  const v=$("pinInput").value.trim(); if(!v)return;
  document.cookie=`jarvis_token=${encodeURIComponent(v)}; path=/; max-age=31536000; secure; samesite=lax`;
  const r=await fetch("/api/usage");
  if(r.status===401){$("pinMsg").textContent="ACCESS DENIED";$("pinInput").value="";return}
  location.reload();
};
$("pinInput")&&$("pinInput").addEventListener("keydown",e=>{if(e.key==="Enter")$("pinBtn").click()});
checkAuth();

/* ============================== boot sequence ====================== */
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const BOOT_LINES=[
  ["INITIALIZING NEURAL INTERFACE",380],["LOADING HUD MODULES",300],
  ["CONNECTING HERMES CORE",650],["VOICE LINK ESTABLISHED",420],
  ["MEMORY SYSTEMS SYNCHRONIZED",420],["WEAPONS OFFLINE. KETTLE ONLINE.",300],
  ["ALL SYSTEMS NOMINAL",550],
];
let bootRunning=false;
async function bootSequence(full){
  if(bootRunning)return; bootRunning=true;
  const boot=$("boot");
  boot.classList.remove("done"); boot.style.display="flex";
  document.body.classList.remove("booted"); document.body.classList.add("booting");
  const bl=$("bootLines"); bl.innerHTML="";
  const scale=full?1:0.32;
  for(const [t,d] of BOOT_LINES){
    if(!full&&t.startsWith("WEAPONS"))continue;   // easter egg only in full boot
    const div=document.createElement("div"); div.innerHTML="▸ "+t+" <b>OK</b>";
    bl.appendChild(div); await sleep(d*scale);
  }
  await sleep(full?350:120);
  // staggered panel power-on
  document.querySelectorAll(".panel").forEach((p,i)=>p.style.animationDelay=(120+i*95)+"ms");
  boot.classList.add("done");
  document.body.classList.remove("booting"); document.body.classList.add("booted");
  if(full){
    try{
      const h=new Date().getHours();
      const f=h<12?"morning":h<18?"afternoon":"evening";
      await new Audio("audio/boot_"+f+".mp3").play();
    }catch{/* audio needs a user gesture; B-key boots always have one */}
  }
  setTimeout(()=>{boot.style.display="none"},900);
  bootRunning=false;
}
addEventListener("keydown",e=>{
  if((e.key==="b"||e.key==="B")&&document.activeElement!==$("chatInput"))bootSequence(true);
});
bootSequence(new URLSearchParams(location.search).get("boot")==="full");

addMsg("sys","Jarvis HUD online. Click the ring or press Space to talk.");
