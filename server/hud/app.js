"use strict";
/* ============================== theme ============================== */
const THEME_KEY = "looking-glass-theme";

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
const CONV = "looking-glass-main";
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
      if(tm.response_text)addMsg("looking-glass",tm.response_text);
      setTimeout(()=>{if(state!=="listening")setState("standby","STANDBY")},400);
    }
    // Extension point: any WS event type not handled above is dispatched to
    // a global `onWsEvent_<type>` function if one exists, so new features
    // (e.g. network-map.js's "network_activity" pulses) can hook into the
    // socket without editing this router. See network-map.js for an example.
    else if(typeof window["onWsEvent_"+e.type]==="function"){ window["onWsEvent_"+e.type](e); }
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
$("feedToggleBtn").onclick=()=>feed.classList.toggle("open");

/* ---- unified work-area tabs (terminals + views share one system) ---- */
const DASH_PROXY=`https://${location.hostname}:9443`;
const workTabs=new Map(); // id -> {type, title, term?, socket?, iframe?}

function tabId(type,key){return `${type}:${key}`}

/* Global shortcuts must never steal keys from something the user is typing
   into. Exempting only #chatInput meant Space (preventDefault-ed for
   push-to-talk), S, B and Escape were all swallowed inside xterm terminals —
   you could not type a space in the Claude pane. */
function isTypingContext(){
  const el=document.activeElement;
  if(!el) return false;
  if(el.isContentEditable) return true;
  const tag=(el.tagName||"").toLowerCase();
  if(tag==="input"||tag==="textarea"||tag==="select") return true;
  // xterm puts focus on a hidden textarea inside .xterm; also treat any
  // focus inside the work area as "the user is working in a pane".
  return !!el.closest?.(".xterm, #work-area");
}

/* ---- view model -------------------------------------------------------
   Two distinct operations, which used to be conflated:

     VIEW CHANGE   switching what the centre of the HUD is showing
                   (the network map vs the work area). Rotates the HUD and
                   swings the 3D camera with it.

     TAB ADDITION  adding a terminal or page INSIDE the work view. Tabs are
                   how one workspace organises itself, so they must not
                   rotate anything — opening a second terminal is not a
                   change of view.

   Previously every openWorkTab() rotated, so adding a tab performed a full
   view change, which made no sense. Now a rotation happens only when the
   view actually changes.
------------------------------------------------------------------------ */
// Starts as null, NOT "map". switchView() early-returns when the target
// equals the current view, so seeding this with "map" made the startup
// switchView("map") a no-op — setNetworkVisible(true) never ran and the
// network could never appear, on any number of clicks.
let currentView=null;            // null | "map" | "work"

function switchView(next,{animate=true}={}){
  if(next===currentView){ return; }
  const reverse = next==="map";       // work->map swings back the other way
  const apply=()=>{
    currentView=next;
    document.body.classList.toggle("view-work", next==="work");
    document.body.classList.toggle("view-map",  next==="map");
    if(typeof setNetworkVisible==="function") setNetworkVisible(next==="map");
  };
  if(!animate){ apply(); return; }
  hudTurn(apply,{reverse});
}

/* ---- 3D view transition ----
   Swings the work area (and banks the side columns) as if the camera turned
   to a different display, running `swap` at the midpoint so the content
   changes while it's edge-on. Used for opening/closing a view and toggling
   the network map — NOT for switching between already-open tabs.
   `swap` always runs exactly once even if a turn is already in flight, so a
   fast double-click can never drop the action it was asked to perform. */
let hudTurning=false;
const TURN_OUT_MS=250, TURN_IN_MS=340;

/* xterm and the graph canvas can't measure themselves mid-rotation, so
   re-fit once the panel is square to the viewport again. */
function refitActiveWorkTab(){
  const active=document.querySelector(".work-panel.active");
  const tab=active&&workTabs.get(active.dataset.tabId);
  if(tab?.fitAddon)tab.fitAddon.fit();
  if(tab?.onActivate)tab.onActivate();
}
function hudTurn(swap,{reverse=false}={}){
  const wa=$("work-area");
  const reduced=matchMedia("(prefers-reduced-motion: reduce)").matches;
  if(hudTurning || !wa || reduced){ swap(); return; }
  hudTurning=true;
  document.body.classList.add("hud-banking");
  // The background is part of the move, not a static backdrop behind it.
  if(typeof nudgeCameraForViewChange==="function") nudgeCameraForViewChange(reverse);
  wa.classList.toggle("rev",reverse);
  document.body.classList.toggle("turn-rev",reverse);

  // A display:none element cannot animate, so when the work area is still
  // hidden (opening the first view) there is nothing to turn OUT — the
  // out-phase was silently doing nothing and the whole move looked broken.
  // In that case go straight to the swap and play only the turn-in.
  if(!wa.classList.contains("has-tabs")){
    swap();
    wa.classList.add("turn-in");
    setTimeout(()=>{
      wa.classList.remove("turn-in","rev");
      document.body.classList.remove("hud-banking","turn-rev");
      hudTurning=false;
      refitActiveWorkTab();
    },TURN_IN_MS);
    return;
  }

  wa.classList.add("turn-out");
  setTimeout(()=>{
    try{ swap(); }
    finally{
      wa.classList.remove("turn-out");
      wa.classList.add("turn-in");
      setTimeout(()=>{
        wa.classList.remove("turn-in","rev");
        document.body.classList.remove("hud-banking","turn-rev");
        hudTurning=false;
        refitActiveWorkTab();
      },TURN_IN_MS);
    }
  },TURN_OUT_MS);
}

function updateWorkAreaVisibility(){
  const hasTabs=workTabs.size>0;
  $("work-area").classList.toggle("has-tabs",hasTabs);
  // Suspend the 3D scenery whenever real work is on screen.
  if(typeof setSceneryPaused==="function") setSceneryPaused(hasTabs);
}

/* ---- split view -------------------------------------------------------
   splitRightId is the tab pinned to the right pane. The left pane continues
   to follow normal tab selection, so you can drive one side while the other
   stays fixed. Pinning is a layout operation, not a view change — it must
   not rotate the HUD. */
let splitRightId=null;
let sidebarsVisible=true;

/* Sidebars are overlays, not fixed furniture: hidden they slide off-canvas
   and the centre reclaims the full width; shown they float above it. */
function setSidebars(visible){
  sidebarsVisible=visible;
  document.body.classList.toggle("sidebars-hidden", !visible);
  const btn=$("sidebarToggleBtn");
  if(btn){ btn.textContent = visible ? "▤" : "▥"; btn.title = visible ? "Hide side panels (S)" : "Show side panels (S)"; }
  refitAllVisibleWorkTabs();
}
function toggleSidebars(){ setSidebars(!sidebarsVisible); }

function applySplitLayout(){
  const content=$("work-content");
  const nowSplit=!!splitRightId;
  // Split view needs the width, so the side columns get out of the way. They
  // become overlays you can summon with the ▤ button (or the S key) rather
  // than being gone entirely.
  if(nowSplit && !content.classList.contains("split")) setSidebars(false);
  if(!nowSplit && content.classList.contains("split")) setSidebars(true);
  content.classList.toggle("split", nowSplit);
  document.querySelectorAll(".work-panel").forEach(p=>{
    p.classList.toggle("split-right", p.dataset.tabId===splitRightId);
  });
  document.querySelectorAll(".work-tab").forEach(b=>{
    b.classList.toggle("is-split", b.dataset.tabId===splitRightId);
  });
  refitAllVisibleWorkTabs();
}

function toggleSplit(id){
  // Pinning the tab that is already pinned un-pins it. Pinning the tab that
  // is currently active would show the same content twice, so move selection
  // to any other tab first.
  splitRightId = (splitRightId===id) ? null : id;
  if(splitRightId){
    const active=document.querySelector(".work-panel.active");
    if(active && active.dataset.tabId===splitRightId){
      const other=[...workTabs.keys()].find(k=>k!==splitRightId);
      if(other) activateWorkTab(other); else splitRightId=null;
    }
  }
  applySplitLayout();
}

function refitAllVisibleWorkTabs(){
  // Both panes changed size, so every visible tab needs re-measuring.
  requestAnimationFrame(()=>{
    document.querySelectorAll(".work-panel.active, .work-panel.split-right").forEach(p=>{
      const tab=workTabs.get(p.dataset.tabId);
      if(tab?.fitAddon)tab.fitAddon.fit();
      if(tab?.onActivate)tab.onActivate();
    });
  });
}

function activateWorkTab(id){
  document.querySelectorAll(".work-tab").forEach(b=>b.classList.toggle("active",b.dataset.tabId===id));
  document.querySelectorAll(".work-panel").forEach(p=>p.classList.toggle("active",p.dataset.tabId===id));
  // re-fit in case the window resized while this tab was hidden (a hidden
  // xterm container can't measure itself, so this only takes effect now
  // that .active makes it visible again)
  // The pinned pane must survive a selection change in the left pane.
  if(splitRightId) applySplitLayout();
  const tab=workTabs.get(id);
  if(tab?.fitAddon)tab.fitAddon.fit();
  // Extension point: any work-tab type can set tab.onActivate (e.g. to
  // resize a canvas that couldn't measure itself while hidden — same
  // reason fitAddon.fit() above exists for terminals). See network-map.js.
  if(tab?.onActivate)tab.onActivate();
}

function closeWorkTab(id){
  if(!workTabs.has(id))return;
  // Closing one of several tabs is a tab operation; closing the last one
  // leaves the work view empty, so that IS a view change back to the map.
  if(workTabs.size>1){ closeWorkTabNow(id); return; }
  switchView("map");
  setTimeout(()=>closeWorkTabNow(id), TURN_OUT_MS);
}

function closeWorkTabNow(id){
  const tab=workTabs.get(id);
  if(!tab)return;
  // Some tabs borrow live DOM (the Hermes pane moves the real chat feed into
  // itself). Give them a chance to put it back before the panel is removed.
  if(tab.onBeforeClose){ try{ tab.onBeforeClose(); }catch{} }
  if(splitRightId===id) splitRightId=null;
  if(tab.socket)tab.socket.close();
  if(tab.term)tab.term.dispose();
  document.querySelector(`.work-tab[data-tab-id="${id}"]`)?.remove();
  document.querySelector(`.work-panel[data-tab-id="${id}"]`)?.remove();
  workTabs.delete(id);
  applySplitLayout();
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
  const split=document.createElement("span");
  // ◧ (half-filled square) reads as "split panes". The previous ⫿ rendered as
  // a hairline that looked like a separator, so the control was invisible and
  // nobody could find split view.
  split.className="work-tab-split"; split.textContent="◧";
  split.title="Split: pin this tab to the right pane";
  const close=document.createElement("span"); close.className="work-tab-close"; close.textContent="✕";
  label.onclick=()=>activateWorkTab(id);
  split.onclick=e=>{e.stopPropagation();toggleSplit(id)};
  close.onclick=e=>{e.stopPropagation();closeWorkTab(id)};
  tabBtn.append(label,split,close);
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

/* Opening something NEW turns the view; re-selecting a tab that's already
   open is a plain instant switch. */
/* Adding a tab is NOT a view change. It only rotates when it forces one —
   i.e. when we are still on the map view and have to move to the work view
   to show it. Once in the work view, further tabs just appear. */
function openWorkTabTurning(type,key,title,buildContent){
  const id=tabId(type,key);
  if(workTabs.has(id)){ activateWorkTab(id); switchView("work"); return; }
  if(currentView!=="work"){
    hudTurn(()=>{
      openWorkTab(type,key,title,buildContent);
      currentView="work";
      document.body.classList.add("view-work");
      document.body.classList.remove("view-map");
      if(typeof setNetworkVisible==="function") setNetworkVisible(false);
    });
  }else{
    openWorkTab(type,key,title,buildContent);   // plain tab addition
  }
}

function openView(name,path){
  openWorkTabTurning("view",path,name,(panel)=>{
    const iframe=document.createElement("iframe");
    iframe.className="work-view-iframe";
    iframe.src=DASH_PROXY+path;
    iframe.title=name;
    panel.appendChild(iframe);
  });
}

/* Shows the live looking-glass-main conversation in a pane — the same session
   the voice pipeline and the dock input use. It does this by MOVING the real
   #feed element into the panel rather than rendering a copy, so there is one
   source of truth and no second session. The dashboard's own /chat page
   cannot do this: it starts a fresh session unrelated to this one. */
/* The feed only ever held messages from THIS browser session, so the live
   pane looked empty even though the Hermes session had full history. Pulls
   the real transcript and prepends it above whatever is already on screen. */
let historyLoaded=false;
async function loadConversationHistory(){
  const feed=$("feed");
  if(historyLoaded){ feed.scrollTop=feed.scrollHeight; return; }
  try{
    const r=await fetch(`/api/conversation?conversation=${encodeURIComponent(CONV)}&limit=60`);
    const j=await r.json();
    const msgs=j.messages||[];
    if(!msgs.length){
      addMsg("sys", j.error ? `history unavailable: ${j.error}` : "no earlier messages in this session");
    }else{
      const frag=document.createDocumentFragment();
      msgs.forEach(m=>{
        const d=document.createElement("div");
        d.className="msg "+(m.role==="user"?"you":"looking-glass");
        d.textContent=m.content;
        frag.appendChild(d);
      });
      const marker=document.createElement("div");
      marker.className="msg sys"; marker.textContent=`— ${msgs.length} earlier messages · session ${j.session_id||""} —`;
      feed.insertBefore(frag, feed.firstChild);
      feed.insertBefore(marker, feed.firstChild);
    }
    historyLoaded=true;
  }catch(err){ addMsg("sys","history load failed: "+err.message); }
  feed.scrollTop=feed.scrollHeight;
}

function openHermesChatPane(){
  openWorkTabTurning("hermes","live","HERMES (live)",(panel,tab)=>{
    const feed=$("feed");
    const anchor=document.createComment("feed-home");
    feed.parentNode.insertBefore(anchor,feed);
    panel.classList.add("hermes-pane");
    panel.appendChild(feed);
    feed.classList.add("open","in-pane");
    loadConversationHistory();
    tab.onBeforeClose=()=>{
      feed.classList.remove("in-pane");
      anchor.parentNode.insertBefore(feed,anchor);
      anchor.remove();
    };
  });
}

function openTerminal(host,{run=null,title=null}={}){
  openWorkTabTurning("terminal",host,title||host,(panel,tab)=>{
    const term=new Terminal({convertEol:true, fontSize:13});
    const fitAddon=new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(panel);
    fitAddon.fit();  // size to the actual panel, not xterm's 80x24 default
    const proto=location.protocol==="https:"?"wss:":"ws:";
    const socket=new WebSocket(`${proto}//${location.host}/ws/terminal/${host}`);
    socket.onmessage=ev=>term.write(ev.data);
    // Optional boot command (e.g. launching `claude`). Sent once the shell is
    // actually up rather than on socket open, or it lands before the prompt.
    if(run){
      let sent=false;
      const armed=setTimeout(()=>{ if(!sent&&socket.readyState===WebSocket.OPEN){sent=true;socket.send(run+"\n");} },1200);
      tab.cancelBoot=()=>clearTimeout(armed);
    }
    socket.onclose=()=>term.write("\r\n[connection closed]\r\n");
    term.onData(data=>{if(socket.readyState===WebSocket.OPEN)socket.send(data)});
    tab.term=term; tab.socket=socket; tab.fitAddon=fitAddon;
  });
}

let resizeDebounce=null;
addEventListener("resize",()=>{
  clearTimeout(resizeDebounce);
  resizeDebounce=setTimeout(()=>{
    const activePanel=document.querySelector(".work-panel.active");
    const id=activePanel?.dataset.tabId;
    const tab=id&&workTabs.get(id);
    if(tab?.fitAddon)tab.fitAddon.fit();
  },150);
});

document.querySelectorAll(".terminal-tile").forEach(btn=>{
  btn.addEventListener("click",()=>openTerminal(btn.dataset.host));
});

/* Buttons are wired here rather than with inline onclick= attributes: a
   Content-Security-Policy without 'unsafe-inline' blocks inline handlers, so
   inline wiring would leave the theme toggle, the VIEWS buttons and the
   network-map toggle silently dead. */
document.querySelectorAll("[data-view]").forEach(btn=>{
  btn.addEventListener("click",()=>openView(btn.dataset.view,btn.dataset.path));
});
document.querySelectorAll('[data-action="sidebars"]').forEach(btn=>{
  btn.addEventListener("click",toggleSidebars);
});
addEventListener("keydown",e=>{
  if((e.key==="s"||e.key==="S") && !isTypingContext()) toggleSidebars();
});
document.querySelectorAll('[data-action="theme"]').forEach(btn=>{
  btn.addEventListener("click",toggleTheme);
});
document.querySelectorAll('[data-action="pair"]').forEach(btn=>{
  // The review setup in one click: live Hermes on the left, Claude in the
  // repo on the right, already split. Hunting for two buttons and then a
  // pin control was too much to discover.
  btn.addEventListener("click",()=>{
    // Pairs the KANBAN BOARD with Claude, not the HUD chat thread. The chat
    // thread is a different conversation from where cards actually execute —
    // pairing it with the reviewer was showing the wrong half of the system.
    openKanbanBoard();
    setTimeout(()=>{
      openTerminal("snarf",{run:"cd /ssdpool/DARKHELIX && bash -lc claude",title:"claude · DARKHELIX"});
      setTimeout(()=>{ if(!splitRightId) toggleSplit(tabId("terminal","snarf")); },900);
    },700);
  });
});
document.querySelectorAll('[data-action="hermes-live"]').forEach(btn=>{
  btn.addEventListener("click",openHermesChatPane);
});
document.querySelectorAll('[data-action="claude"]').forEach(btn=>{
  // Opens Claude Code where the code and its context actually live — on
  // snarf, in the repo (which carries its own CLAUDE.md and .claude/), next
  // to the already-authenticated gh. Host, directory and command come from
  // the button's data-* attributes so this is not hardcoded here.
  // `bash -lc` matters: claude sits in ~/.local/bin, which a non-login shell
  // does not have on PATH.
  btn.addEventListener("click",()=>openTerminal(btn.dataset.host||"snarf",{
    run: btn.dataset.run || "claude",
    title: btn.dataset.title || "claude",
  }));
});
document.querySelectorAll('[data-action="netmap"]').forEach(btn=>{
  // network-map.js loads after this file, so resolve the handler at click time.
  btn.addEventListener("click",()=>{
    if(typeof toggleNetworkMap==="function")toggleNetworkMap();
    else addMsg("sys","network map unavailable (3D libraries failed to load)");
  });
});

addEventListener("keydown",e=>{
  if(e.key!=="Escape")return;
  if(isTypingContext())return;   // Esc belongs to the terminal / editor
  if(document.querySelector(".holo:not(.dismiss)"))dismissAllPanels();
  else stopRun();
});
addEventListener("keydown",e=>{
  if(e.code==="Space" && !isTypingContext()){e.preventDefault();toggleTalk()}
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
    addMsg("looking-glass",j.text||"(no reply)");
    if(/^\/(new|reset|clear)\b/i.test(text)){     // slash reset → also clear the visible feed
      setTimeout(()=>{feed.innerHTML="";addMsg("sys","conversation reset");},800);
    }
  }catch(err){ addMsg("sys","chat error: "+err.message); }
  if(state!=="listening")setState("standby","STANDBY");
}
$("sendBtn").onclick=sendChat;
$("clearBtn").onclick=()=>{feed.innerHTML="";addMsg("sys","chat cleared (history stays in Hermes — type /new to reset the conversation)");};
$("chatInput").addEventListener("keydown",e=>{if(e.key==="Enter")sendChat()});

/* Panel widgets (health/skills/jobs/machines/usage/projects) live in
   panels.js now — see its top-of-file comment for the pattern to follow
   when adding a new one. vLLM control lives in vllm-control.js. Network
   topology map lives in network-map.js. */

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
  document.cookie=`looking_glass_token=${encodeURIComponent(v)}; path=/; max-age=31536000; secure; samesite=lax`;
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
  if((e.key==="b"||e.key==="B") && !isTypingContext())bootSequence(true);
});
bootSequence(new URLSearchParams(location.search).get("boot")==="full");

addMsg("sys","Looking Glass HUD online. Click the ring or press Space to talk.");
