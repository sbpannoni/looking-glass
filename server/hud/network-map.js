"use strict";
/* ============================ NETWORK MAP ============================
   Ambient background fleet map — NOT a boxed work-tab. Renders full-
   viewport behind #grid (see #netmapLayer in index.html, same z-index
   tier as #bgDepth) so it reads as part of the HUD's depth, not a
   separate window. Toggled on/off via the NETWORK MAP button.

   Nodes render as custom THREE objects (glow halo + core + a canvas-drawn
   nameplate showing id/kind/status/ports) via window.THREE3D, loaded by a
   small ES module shim in index.html — see the comment there for why this
   is a second, independent THREE instance from the one 3d-force-graph
   bundles internally, and why the load-order split is safe. If THREE3D
   isn't available for some reason, falls back to the library's built-in
   plain-sphere rendering rather than breaking.

   Layers: physical (beelink branch point — straight, dim, always the
   most "real" layer), general/hermes/claude ("virtual" — logical, not
   physical, layers) render as arced glowing lines with a particle flow.
   Nodes are always visible when the map is on regardless of which layers
   are toggled (up/down should always read at a glance); layer toggles
   only affect which *links* draw.

   Live activity: backend broadcasts {"type":"network_activity","node",
   "source":"hermes"|"claude","state":"start"|"end"|"pulse"} over the main
   WS (see server.py _broadcast_network_activity). Picked up here via the
   generic onWsEvent_* hook in app.js — nothing there needs to change to
   extend this further.
======================================================================= */

// Hosts the HUD can open a real terminal on — mirrors TERMINAL_HOSTS /
// the QUICK ACCESS tiles in index.html. Keep in sync if you add a host.
const SSH_HOSTS = new Set(["snarf","r720","octominer","beelink","claude-control","hermes","looking-glass"]);

function cssVar(name, fallback){
  const v=getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v||fallback;
}
const PALETTE={
  ok: cssVar("--teal","#4ade80"), err: cssVar("--red","#f87171"),
  amber: cssVar("--amber","#fbbf24"), cyan: cssVar("--cyan","#22e5ff"),
  magenta: cssVar("--magenta","#ff2fd0"), orange: cssVar("--orange","#ff8a3d"),
  dim: "#5b6b82",
};
const LAYER_COLOR={physical:"#93a1bd", general:PALETTE.cyan, hermes:PALETTE.amber, claude:PALETTE.magenta};
const LAYER_CURVE={physical:0, general:0.22, hermes:0.34, claude:0.46}; // "virtual" layers arc; physical stays straight

let latestTopology=null;
let graph=null;            // ForceGraph3D instance, created lazily on first toggle-on
let netmapActive=false;
let focusedId=null;
let hoveredId=null;
const layers={physical:true, general:true, hermes:false, claude:false};

// Persistent node objects, keyed by id, reused across every data refresh.
// 3d-force-graph stores simulation position/velocity directly ON each node
// object — handing it a *new* object every poll (as an earlier version of
// this file did) throws that away and re-randomizes the layout, which
// reads as nodes "jumping around". Mutating the same object in place keeps
// them settled. The custom viz object (see buildNodeViz) is cached on the
// node the same way, for the same reason.
const nodeObjects=new Map();
function mergeNode(raw){
  let obj=nodeObjects.get(raw.id);
  if(!obj){ obj={}; nodeObjects.set(raw.id,obj); }
  Object.assign(obj, raw);
  return obj;
}
function syntheticNode(id,kind){
  let obj=nodeObjects.get(id);
  if(!obj){ obj={id,kind,up:true,synthetic:true}; nodeObjects.set(id,obj); }
  return obj;
}

async function pollTopology(){
  try{
    const r=await fetch("/api/network_topology"); const j=await r.json();
    if(j && j.nodes){ latestTopology=j; if(graph) applyData(); }
  }catch{}
}

function buildGraphData(){
  if(!latestTopology) return {nodes:[], links:[]};
  const nodes=(latestTopology.nodes||[]).map(mergeNode);
  const links=[];
  if(layers.physical){
    (latestTopology.edges.physical||[]).forEach(e=>links.push({source:e.from,target:e.to,layer:"physical"}));
  }
  if(layers.general){
    nodes.push(syntheticNode("lan","hub"));
    (latestTopology.edges.general||[]).forEach(e=>links.push({source:e.from,target:e.to,layer:"general"}));
  }
  if(layers.hermes){
    (latestTopology.edges.hermes||[]).forEach(e=>links.push({source:e.from,target:e.to,layer:"hermes",label:e.label,port:e.port}));
  }
  if(layers.claude){
    nodes.push(syntheticNode("claude","hub"));
    (latestTopology.edges.claude||[]).forEach(e=>links.push({source:e.from,target:e.to,layer:"claude",via:e.via}));
  }
  return {nodes, links};
}

/* ---- pulses (live activity) ------------------------------------------ */
const pulsingNodes=new Map(); // id -> {source, until}
let pulseTicker=null;
function pulseNode(id, source){
  pulsingNodes.set(id, {source, until: Date.now()+2600});
  refreshAccessors();
  if(!pulseTicker){
    pulseTicker=setInterval(()=>{
      const now=Date.now();
      let changed=false;
      for(const [k,v] of pulsingNodes){ if(v.until<=now){ pulsingNodes.delete(k); changed=true; } }
      if(changed || pulsingNodes.size) refreshAccessors();
      if(!pulsingNodes.size){ clearInterval(pulseTicker); pulseTicker=null; }
    },400);
  }
}
window.onWsEvent_network_activity=function(e){
  if(!e || !e.node) return;
  pulseNode(e.node, e.source);
};

/* ---- connectivity (blast-radius focus) -------------------------------- */
function connectedIds(id){
  const out=new Set([id]);
  if(!latestTopology) return out;
  const all=[...(latestTopology.edges.physical||[]),...(latestTopology.edges.general||[]),
             ...(latestTopology.edges.hermes||[]),...(latestTopology.edges.claude||[])];
  all.forEach(e=>{
    if(e.from===id) out.add(e.to);
    if(e.to===id) out.add(e.from);
  });
  return out;
}

function nodeStatusColor(n){
  if(pulsingNodes.has(n.id)){
    const src=pulsingNodes.get(n.id).source;
    return src==="claude"?PALETTE.magenta:PALETTE.amber;
  }
  if(n.id==="claude") return PALETTE.magenta;
  if(n.id==="lan") return PALETTE.dim;
  if(n.up===false) return PALETTE.err;
  return PALETTE.ok;
}
function nodeDimmed(n){ return !!(focusedId && !connectedIds(focusedId).has(n.id)); }

/* ---- custom node visuals (glow halo + core + nameplate) --------------- */
function roundRect(ctx,x,y,w,h,r){
  ctx.beginPath();
  ctx.moveTo(x+r,y);
  ctx.arcTo(x+w,y,x+w,y+h,r); ctx.arcTo(x+w,y+h,x,y+h,r);
  ctx.arcTo(x,y+h,x,y,r); ctx.arcTo(x,y,x+w,y,r);
  ctx.closePath();
}
function portSummary(n){
  if(!n.ports || !n.ports.length) return "";
  const open=n.ports.filter(p=>p.open).length;
  return `${open}/${n.ports.length} ports open`;
}
// Small abstract glyph per node kind so the nameplate reads at a glance
// instead of everyone getting the same dot. Drawn in white on the colored
// status badge behind it.
function drawKindIcon(ctx, kind, cx, cy, r){
  ctx.save();
  ctx.translate(cx,cy);
  ctx.strokeStyle="#0b0f16"; ctx.fillStyle="#0b0f16"; ctx.lineWidth=Math.max(1.4,r*0.16);
  ctx.lineCap="round"; ctx.lineJoin="round";
  const k=kind||"";
  if(k.includes("gpu")){ // chip: square + corner pins
    const s=r*1.05;
    ctx.strokeRect(-s*0.55,-s*0.55,s*1.1,s*1.1);
    for(const [dx,dy] of [[-1,0],[1,0],[0,-1],[0,1]]){
      ctx.beginPath(); ctx.moveTo(dx*s*0.55,dy*s*0.55); ctx.lineTo(dx*s*0.85,dy*s*0.85); ctx.stroke();
    }
  }else if(k==="proxmox"){ // stacked layers (virtualization host)
    for(let i=-1;i<=1;i++){ ctx.beginPath(); ctx.ellipse(0,i*r*0.42,r*0.62,r*0.24,0,0,Math.PI*2); ctx.stroke(); }
  }else if(k==="lxc"){ // two nested boxes (container)
    ctx.strokeRect(-r*0.6,-r*0.6,r*1.2,r*1.2);
    ctx.strokeRect(-r*0.3,-r*0.3,r*0.6,r*0.6);
  }else if(k==="oob"){ // bolt (out-of-band power)
    ctx.beginPath();
    ctx.moveTo(r*0.15,-r*0.7); ctx.lineTo(-r*0.35,r*0.05); ctx.lineTo(r*0.05,r*0.05);
    ctx.lineTo(-r*0.15,r*0.7); ctx.lineTo(r*0.35,-r*0.05); ctx.lineTo(r*0.0,-r*0.05);
    ctx.closePath(); ctx.fill();
  }else if(k==="hub"){ // asterisk (virtual/logical node)
    for(let a=0;a<Math.PI*2;a+=Math.PI/3){
      ctx.beginPath(); ctx.moveTo(0,0); ctx.lineTo(Math.cos(a)*r*0.75,Math.sin(a)*r*0.75); ctx.stroke();
    }
  }else if(k==="service"){ // small orbiting satellite dot
    ctx.beginPath(); ctx.arc(0,0,r*0.32,0,Math.PI*2); ctx.stroke();
    ctx.beginPath(); ctx.arc(r*0.55,0,r*0.14,0,Math.PI*2); ctx.fill();
  }else{ // server (default): rack bars
    for(let i=-1;i<=1;i++){ ctx.beginPath(); ctx.moveTo(-r*0.6,i*r*0.35); ctx.lineTo(r*0.6,i*r*0.35); ctx.stroke(); }
  }
  ctx.restore();
}
const LABEL_DPR=Math.min(2, window.devicePixelRatio||1); // crisper text on hi-DPI screens
const LABEL_W=300, LABEL_H=112; // logical size — canvas backing store is LABEL_DPR× this
function drawLabelCanvas(canvas, n, colorHex){
  const W=LABEL_W, H=LABEL_H;
  const ctx=canvas.getContext("2d");
  ctx.setTransform(LABEL_DPR,0,0,LABEL_DPR,0,0);
  ctx.clearRect(0,0,W,H);
  // Near-opaque dark plate: this is what makes the text legible over a
  // bright lunar surface. Text colors below are all kept under the bloom
  // threshold (see the UnrealBloomPass note) so they stay crisp.
  ctx.fillStyle="rgba(4,7,12,0.93)";
  roundRect(ctx,3,3,W-6,H-6,18); ctx.fill();
  ctx.lineWidth=2; ctx.strokeStyle=colorHex; ctx.globalAlpha=0.55;
  roundRect(ctx,3,3,W-6,H-6,18); ctx.stroke(); ctx.globalAlpha=1;
  // status/kind badge — the one element allowed to be vivid
  ctx.beginPath(); ctx.arc(30,H/2,13,0,Math.PI*2);
  ctx.fillStyle=colorHex; ctx.fill();
  drawKindIcon(ctx, n.kind, 30, H/2, 10);
  // Text is shrunk to fit rather than clipped — "home-assistant" and
  // "claude-control" ran off the plate at a fixed size.
  const textX=54, maxW=W-textX-14;
  const fit=(text,weight,size,family)=>{
    let s=size;
    for(;s>11;s--){
      ctx.font=`${weight} ${s}px ${family}`;
      if(ctx.measureText(text).width<=maxW) break;
    }
    return s;
  };
  // id
  ctx.fillStyle="#c6ccd8";
  ctx.textBaseline="alphabetic";
  fit(n.id,"700",32,"Orbitron, sans-serif");
  ctx.fillText(n.id, textX, H*0.42);
  // subtitle (kind + status / address)
  ctx.fillStyle="#98a0b2";
  const sub=[n.kind, n.up===false?"DOWN":"up"].filter(Boolean).join(" · ");
  fit(sub,"",20,"Rajdhani, sans-serif");
  ctx.fillText(sub, textX, H*0.68);
  const extra=n.address||portSummary(n);
  if(extra){
    ctx.fillStyle="#737b8e";
    fit(extra,"",16,"Rajdhani, sans-serif");
    ctx.fillText(extra, textX, H*0.90);
  }
}
function makeHalo(THREE, colorHex, size){
  const c=document.createElement("canvas"); c.width=c.height=128;
  const ctx=c.getContext("2d");
  const g=ctx.createRadialGradient(64,64,0,64,64,64);
  // Tight, restrained falloff — the old version was a wide near-white blob
  // that washed out the nameplate sitting right next to it.
  g.addColorStop(0,"rgba(255,255,255,0.55)");
  g.addColorStop(0.18,colorHex+"88");
  g.addColorStop(1,colorHex+"00");
  ctx.fillStyle=g; ctx.fillRect(0,0,128,128);
  const tex=new THREE.CanvasTexture(c);
  const mat=new THREE.SpriteMaterial({map:tex, transparent:true, depthWrite:false, blending:THREE.AdditiveBlending});
  const sprite=new THREE.Sprite(mat);
  sprite.scale.set(size,size,1);
  return sprite;
}
let sharedCoreTexture=null;
function coreTexture(THREE){
  // A faint etched circuit/hex pattern, shared by every node (tinted per-
  // node via material.color) so the cores read as "tech artifact" rather
  // than flat-shaded balls.
  if(sharedCoreTexture) return sharedCoreTexture;
  const c=document.createElement("canvas"); c.width=c.height=256;
  const ctx=c.getContext("2d");
  ctx.fillStyle="#ffffff"; ctx.fillRect(0,0,256,256);
  ctx.strokeStyle="rgba(0,0,0,0.55)"; ctx.lineWidth=2;
  const step=32;
  for(let y=0;y<=256;y+=step){
    ctx.beginPath();
    for(let x=0;x<=256;x+=step/2){
      const jag=(x/step)%2===0? y : y+step*0.5;
      x===0? ctx.moveTo(x,jag) : ctx.lineTo(x,jag);
    }
    ctx.stroke();
  }
  for(let i=0;i<26;i++){
    ctx.beginPath();
    ctx.arc(Math.random()*256, Math.random()*256, 2+Math.random()*3, 0, Math.PI*2);
    ctx.fillStyle="rgba(0,0,0,0.35)"; ctx.fill();
  }
  const tex=new THREE.CanvasTexture(c);
  tex.wrapS=tex.wrapT=THREE.RepeatWrapping;
  tex.repeat.set(2,2);
  sharedCoreTexture=tex;
  return tex;
}
function buildNodeViz(n){
  const THREE=window.THREE3D;
  const baseR = n.synthetic ? 4.5 : 7;
  const group=new THREE.Group();

  // Deliberately NO wide additive halo any more. An additive white sprite
  // several times the node radius, with bloom stacked on top, turned every
  // node into a featureless white blob and drowned the nameplate sitting
  // above it. Bloom already supplies the glow from the emissive core — the
  // halo was redundant and destructive. A tight, dim one remains purely to
  // seat the node against the ground.
  const halo=makeHalo(THREE, "#ffffff", baseR*1.9);
  group.add(halo);

  // Unlit on purpose. The moonscape needs a strong directional sun (intensity
  // ~2) to throw long crater shadows, and any lit material under that light
  // clips to white — which is exactly why the nodes lost their status colour.
  // MeshBasicMaterial ignores scene lighting, so a node reads as its true
  // green/red/magenta regardless of where the sun is; bloom supplies the glow.
  const core=new THREE.Mesh(
    new THREE.IcosahedronGeometry(baseR, 1),
    new THREE.MeshBasicMaterial({color:0xffffff, transparent:true, map:coreTexture(THREE)})
  );
  group.add(core);

  const ringGeo=new THREE.TorusGeometry(baseR*1.55, 0.3, 8, 40);
  const ring=new THREE.Mesh(ringGeo, new THREE.MeshBasicMaterial({color:0xffffff, transparent:true, opacity:0.45}));
  ring.rotation.x=Math.PI/2.4;
  group.add(ring);

  const labelCanvas=document.createElement("canvas");
  labelCanvas.width=LABEL_W*LABEL_DPR; labelCanvas.height=LABEL_H*LABEL_DPR;
  const labelTex=new THREE.CanvasTexture(labelCanvas);
  labelTex.anisotropy=8;
  const labelSprite=new THREE.Sprite(new THREE.SpriteMaterial({
    map:labelTex, transparent:true, depthWrite:false,
    depthTest:false,          // never let geometry or glow occlude the text
  }));
  labelSprite.scale.set(38,14.2,1);
  // Clear of the node body and its glow, not tucked inside it.
  labelSprite.position.set(0, baseR+22, 0);
  labelSprite.renderOrder=999;
  group.add(labelSprite);

  group.userData={halo, core, ring, labelSprite, labelCanvas, labelTex, baseR};
  return group;
}
function updateNodeViz(n){
  const group=n.__viz;
  if(!group) return;
  const {halo, core, ring, labelSprite, labelCanvas, labelTex, baseR}=group.userData;
  const color=nodeStatusColor(n);
  const dimmed=nodeDimmed(n);
  const pulsing=pulsingNodes.has(n.id);
  const hovered=hoveredId===n.id && !pulsing;
  const scale=pulsing?1.4:(hovered?1.15:1);
  group.scale.setScalar(scale);
  // Unlit material (see buildNodeViz): colour IS the final look, so push it
  // above the bloom threshold when pulsing to make the flash register.
  core.material.color.set(color);
  if(pulsing) core.material.color.multiplyScalar(1.7);
  core.material.opacity=dimmed?0.22:1;
  ring.material.color.set(color);
  ring.material.opacity=dimmed?0.08:(hovered?0.8:0.45);
  halo.material.color.set(color);
  halo.material.opacity=dimmed?0.03:(pulsing?0.3:hovered?0.22:0.14);
  labelSprite.material.opacity=dimmed?0.25:1;
  drawLabelCanvas(labelCanvas, n, color);
  labelTex.needsUpdate=true;
}
function refreshAccessors(){
  if(!graph) return;
  if(window.THREE3D){
    nodeObjects.forEach(n=>{ if(n.__viz) updateNodeViz(n); });
  }else{
    // fallback: built-in sphere rendering
    graph.nodeColor(n=>nodeDimmed(n)?"#242938":nodeStatusColor(n))
         .nodeVal(n=>pulsingNodes.has(n.id)?9:(n.synthetic?3:5));
  }
  graph.linkColor(linkColorFn);
}
function linkColorFn(l){
  const from=typeof l.source==="object"?l.source.id:l.source;
  const to=typeof l.target==="object"?l.target.id:l.target;
  if(focusedId && from!==focusedId && to!==focusedId) return "#1a1e28";
  return LAYER_COLOR[l.layer]||"#888";
}

function applyData(){
  if(!graph) return;
  graph.graphData(buildGraphData());
  refreshAccessors();
}

function showNodeInfo(n){
  const info=$("netmapInfo");
  if(!n){ info.style.display="none"; return; }
  const portRows=(n.ports||[]).map(p=>
    `<div class="kv"><span>port ${p.port}</span><b class="${p.open?"ok":"err"}">${p.open?"open":"closed"}</b></div>`
  ).join("");
  const canSsh=SSH_HOSTS.has(n.id);
  info.innerHTML=`
    <div class="kv"><span><b>${n.id}</b></span><span class="dot ${n.up===false?"off":"on"}"></span></div>
    <div class="kv"><span>address</span><b>${n.address||"—"}</b></div>
    <div class="kv"><span>kind</span><b>${n.kind||"—"}</b></div>
    ${portRows}
    ${canSsh?'<button class="btn" id="netmapTermBtn">▸ OPEN TERMINAL</button>':""}`;
  info.style.display="block";
  if(canSsh) info.querySelector("#netmapTermBtn").onclick=()=>openTerminal(n.id);
}

function onNodeClick(n){
  focusedId = (focusedId===n.id) ? null : n.id;
  showNodeInfo(focusedId?n:null);
  refreshAccessors();
}

/* ---- moonscape environment ---------------------------------------------
   The map used to sit on a flat black void — this gives it a horizon and
   ground so it reads as a *place* the fleet lives in, not a plot on a
   graph. Built once and added straight into 3d-force-graph's own scene
   (graph.scene()). */
// Real Apollo 15 lunar-surface photograph (NASA as15-97-13168) — see
// vendor/textures/CREDITS.md. Mapped ONCE across the terrain, never tiled:
// mirrored repeat made the repetition obvious (the same crater field stamped
// and flipped over and over). One pass costs texel density up close, which
// the displaced geometry below more than repays.
function loadMoonTexture(THREE, onReady){
  const loader=new THREE.TextureLoader();
  return loader.load("vendor/textures/moon-surface.jpg", tex=>{
    tex.wrapS=tex.wrapT=THREE.ClampToEdgeWrapping;
    tex.repeat.set(1,1);
    tex.anisotropy=16;
    if(onReady) onReady(tex, blurredCopy(THREE, tex.image));
  });
}

// A heavily blurred copy of the photo, for DISPLACEMENT only.
// Displacing straight from the sharp photo turns every speck of grain into a
// spike — the terrain came out looking like crumpled foil rather than a
// moonscape. Downsampling to 72px and scaling back up with smoothing keeps
// only the large landforms (basins, ridges, big crater bowls), while the
// full-resolution photo still supplies colour and fine bump detail.
function blurredCopy(THREE, img){
  if(!img) return null;
  const small=document.createElement("canvas"); small.width=small.height=104;
  small.getContext("2d").drawImage(img,0,0,104,104);
  const big=document.createElement("canvas"); big.width=big.height=1024;
  const ctx=big.getContext("2d");
  ctx.imageSmoothingEnabled=true; ctx.imageSmoothingQuality="high";
  ctx.drawImage(small,0,0,1024,1024);
  const tex=new THREE.CanvasTexture(big);
  tex.wrapS=tex.wrapT=THREE.ClampToEdgeWrapping;
  return tex;
}
let moonscapeBuilt=false;
function buildMoonscape(THREE, scene, renderer){
  if(moonscapeBuilt) return;
  moonscapeBuilt=true;

  // Space, not sky. A blue-grey gradient made the horizon read as haze on a
  // planet with an atmosphere; the Moon has none, so terrain should fall off
  // into pure black with stars behind it.
  scene.background=new THREE.Color(0x000000);
  scene.fog=new THREE.FogExp2(0x000000, 0.00085);

  // Real relief. A flat plane with only a bumpMap fakes shading but has no
  // silhouette, no occlusion and cannot self-shadow — which is why the old
  // ground looked flat and "boring". This is a heavily subdivided plane whose
  // vertices are actually displaced by the photo's luminance, so craters are
  // genuine depressions that catch the sun and throw shadows into themselves.
  const groundMat=new THREE.MeshStandardMaterial({
    // Regolith is mid-grey, not white. A brighter albedo under a 3-intensity
    // sun clipped the whole surface to a featureless white sheet.
    color:0x8d929c, roughness:1.0, metalness:0.0,
    displacementScale:225, bumpScale:3.0,
  });
  const moonTex=loadMoonTexture(THREE, (tex, displaceTex)=>{
    groundMat.map=tex;
    groundMat.bumpMap=tex;                       // sharp photo → fine surface grain
    groundMat.displacementMap=displaceTex||tex;  // blurred copy → landforms
    groundMat.needsUpdate=true;
  });
  groundMat.map=moonTex;
  const ground=new THREE.Mesh(new THREE.PlaneGeometry(5200,5200,300,300), groundMat);
  ground.rotation.x=-Math.PI/2;
  ground.position.y=-400;
  ground.receiveShadow=true;
  ground.castShadow=true;
  scene.add(ground);

  // starfield — fog:false, or the black distance fog would swallow the stars
  const starCount=2600;
  const positions=new Float32Array(starCount*3);
  for(let i=0;i<starCount;i++){
    const r=2600+Math.random()*2600;
    const theta=Math.random()*Math.PI*2, phi=Math.acos(Math.random()*0.9);
    positions[i*3]=r*Math.sin(phi)*Math.cos(theta);
    positions[i*3+1]=Math.abs(r*Math.cos(phi))+120;
    positions[i*3+2]=r*Math.sin(phi)*Math.sin(theta);
  }
  const starGeo=new THREE.BufferGeometry();
  starGeo.setAttribute("position", new THREE.BufferAttribute(positions,3));
  scene.add(new THREE.Points(starGeo, new THREE.PointsMaterial({
    color:0xcfe0ff, size:2.4, sizeAttenuation:false, transparent:true,
    opacity:0.9, fog:false,
  })));

  // Very low, raking sun. Lunar photographs read as lunar because of long
  // hard shadows; a high light flattens the craters straight back out.
  const sun=new THREE.DirectionalLight(0xfff2e2, 2.2);
  sun.position.set(-2400,560,700);
  sun.castShadow=true;
  sun.shadow.mapSize.set(2048,2048);
  sun.shadow.camera.near=10; sun.shadow.camera.far=9000;
  const span=2800;
  sun.shadow.camera.left=-span; sun.shadow.camera.right=span;
  sun.shadow.camera.top=span;   sun.shadow.camera.bottom=-span;
  sun.shadow.bias=-0.0016;
  scene.add(sun);
  // Barely-there fill. On an airless body the shadows really are almost black;
  // this only stops them clipping to pure void.
  scene.add(new THREE.AmbientLight(0x1b2436, 0.28));

  if(renderer){
    renderer.shadowMap.enabled=true;
    renderer.shadowMap.type=THREE.PCFShadowMap;  // PCFSoft is deprecated in this three build
  }
}

/* ---- lazy graph creation ------------------------------------------------*/
function ensureGraph(){
  if(graph) return;
  graph=ForceGraph3D()($("netmapLayer"))
    .backgroundColor(cssVar("--bg","#03060c"))
    .showNavInfo(false)
    .nodeId("id")
    .linkColor(linkColorFn)
    .linkWidth(l=>l.layer==="physical"?2.4:2.0)
    .linkOpacity(0.8)
    .linkCurvature(l=>LAYER_CURVE[l.layer]??0.2)          // "virtual" layers arc
    .linkDirectionalParticles(l=>l.layer==="physical"?0:3) // glowing flow on virtual links
    .linkDirectionalParticleWidth(2.2)
    .linkDirectionalParticleSpeed(0.006)
    .linkDirectionalParticleColor(l=>LAYER_COLOR[l.layer]||"#888")
    .d3VelocityDecay(0.5)     // more friction, less jitter
    .cooldownTicks(260)       // settle and stop, don't simmer forever
    .onNodeClick(onNodeClick)
    .onNodeHover(n=>{
      if(hoveredId===(n?n.id:null)) return;
      hoveredId=n?n.id:null;
      $("netmapLayer").style.cursor=n?"pointer":"grab";
      refreshAccessors();
    })
    .onBackgroundClick(()=>{ focusedId=null; showNodeInfo(null); refreshAccessors(); })
    .onEngineStop(()=>{
      // The layout keeps drifting apart for a while after graphData() is
      // set (charge/link-distance are large now, so it takes real time to
      // reach rest) — framing the camera too early left nodes drifting
      // outside the shot as they kept spreading. onEngineStop fires exactly
      // when the simulation has actually settled, so frame then instead of
      // guessing a timeout. Only auto-frame once per open session so it
      // doesn't yank the camera away from a manual pan/zoom on later polls.
      if(!autoFramed){
        autoFramed=true;
        try{
          graph.zoomToFit(700,230);
          // Then drop the camera to a low, oblique angle. zoomToFit alone
          // frames the graph dead-on, which shows almost no ground and reads
          // as a flat diagram; sitting lower puts the terrain under the
          // network and gives the shot real depth.
          setTimeout(()=>{
            try{
              const cam=graph.camera();
              const d=Math.max(260, cam.position.length());
              graph.cameraPosition(
                {x:d*0.34, y:d*0.20, z:d*0.90},
                {x:0, y:-60, z:0},
                1500,
              );
            }catch{}
          },420);
        }catch{}
        setTimeout(()=>{ zoomBaseDist=null; const z=$("netmapZoom"); if(z)z.value=1; },2100);
      }
    });

  if(window.THREE3D){
    // Paint immediately on creation. 3d-force-graph builds node objects
    // lazily during its own render pass — i.e. AFTER applyData() has already
    // called refreshAccessors() — so a viz created here would otherwise keep
    // the blank canvas it was born with and never show its nameplate.
    graph.nodeThreeObject(n=>{
      if(!n.__viz){ n.__viz=buildNodeViz(n); updateNodeViz(n); }
      return n.__viz;
    });
    try{
      buildMoonscape(window.THREE3D, graph.scene(), graph.renderer());
      // Wider lens = stronger perspective convergence. The default felt flat
      // and "shallow"; this makes the terrain rush away toward the horizon.
      const cam=graph.camera();
      if(cam && cam.isPerspectiveCamera){ cam.fov=68; cam.updateProjectionMatrix(); }
    }catch(err){ console.warn("[network-map] moonscape build failed",err); }
  }else{
    graph.nodeVal(n=>pulsingNodes.has(n.id)?9:(n.synthetic?3:5));
  }

  if(window.THREE3D && window.THREE_POSTFX){
    try{
      const THREE=window.THREE3D;
      const {UnrealBloomPass}=window.THREE_POSTFX;
      // (strength, radius, threshold). Threshold is the important one: bloom
      // only picks up pixels brighter than it, so a high threshold means the
      // emissive node cores and particles glow while the label plates — drawn
      // deliberately below this luminance in drawLabelCanvas — stay sharp and
      // readable instead of smearing into the glow.
      bloomPass=new UnrealBloomPass(new THREE.Vector2(innerWidth,innerHeight), 0.42, 0.35, 0.78);
      graph.postProcessingComposer().addPass(bloomPass);
    }catch(err){ console.warn("[network-map] bloom setup failed, continuing without it",err); bloomPass=null; }
  }

  // Bigger nodes need more room: pull nodes apart harder and lengthen the
  // resting link distance well past three-forcegraph's tiny-sphere default.
  graph.d3Force("charge").strength(-520);
  const linkForce=graph.d3Force("link");
  if(linkForce) linkForce.distance(210);

  resizeGraph();
}

let autoFramed=false;
let idleSpin=null;
let bloomPass=null;
let zoomBaseDist=null;
function tickIdle(){
  // Cheap continuous motion (ring spin only, no canvas/texture work) so the
  // scene reads as "live" even once the force simulation has cooled down
  // and stopped — a fully static scene reads as "a math plot".
  if(netmapActive){
    nodeObjects.forEach(n=>{
      const g=n.__viz;
      if(g) g.userData.ring.rotation.z+=0.006;
    });
  }
  idleSpin=requestAnimationFrame(tickIdle);
}

function resizeGraph(){
  if(!graph) return;
  graph.width(innerWidth).height(innerHeight);
  try{ bloomPass?.setSize(innerWidth,innerHeight); }catch{}
}

/* ---- zoom slider — dollies the camera in/out along its current view
   direction from whatever it's currently looking at, rather than jumping
   to an absolute position (keeps whatever angle you've orbited to). ------*/
function applyZoomSlider(val){
  if(!graph || !window.THREE3D) return;
  try{
    const cam=graph.camera();
    const controls=graph.controls();
    const target=controls?.target || new window.THREE3D.Vector3(0,0,0);
    if(zoomBaseDist==null) zoomBaseDist=Math.max(40, cam.position.distanceTo(target));
    const dir=cam.position.clone().sub(target);
    if(dir.lengthSq()<1e-6) dir.set(0,0,1);
    dir.normalize().multiplyScalar(zoomBaseDist/val);
    cam.position.copy(target).add(dir);
    controls?.update?.();
  }catch{}
}

function toggleNetworkMap(){
  // Same camera-turn transition as opening/closing a view (hudTurn lives in
  // app.js); reversed on hide so showing and hiding swing opposite ways.
  hudTurn(()=>applyNetworkMapToggle(),{reverse:netmapActive});
}

function applyNetworkMapToggle(){
  netmapActive=!netmapActive;
  const btn=$("netmapToggleBtn");
  if(netmapActive){
    $("netmapLayer").classList.add("active");
    $("netmapControls").style.display="flex";
    document.body.classList.add("netmap-on");
    if(btn)btn.textContent="▸ HIDE NETWORK MAP";
    autoFramed=false;
    zoomBaseDist=null;
    ensureGraph();
    graph.resumeAnimation();
    resizeGraph();
    applyData();
    if(!idleSpin) tickIdle();
  }else{
    $("netmapLayer").classList.remove("active");
    $("netmapControls").style.display="none";
    showNodeInfo(null);
    document.body.classList.remove("netmap-on");
    if(btn)btn.textContent="▸ NETWORK MAP";
    graph?.pauseAnimation();
    if(idleSpin){ cancelAnimationFrame(idleSpin); idleSpin=null; }
  }
}

document.querySelectorAll("#netmapControls input[type=checkbox]").forEach(cb=>{
  cb.addEventListener("change",()=>{ layers[cb.dataset.layer]=cb.checked; applyData(); });
});
$("netmapZoom")?.addEventListener("input",e=>applyZoomSlider(parseFloat(e.target.value)));
$("netmapRecenterBtn")?.addEventListener("click",()=>{
  try{ graph?.zoomToFit(600,230); }catch{}
  zoomBaseDist=null;
  const z=$("netmapZoom"); if(z)z.value=1;
});
addEventListener("resize",()=>{ if(netmapActive) resizeGraph(); });

pollTopology();
setInterval(pollTopology,15000);

// The map IS the HUD's backdrop, not an optional extra — it comes up with the
// page rather than waiting for a click. Deliberately calls the plain toggle
// (not hudTurn) so it doesn't fight the boot sequence's own animation.
addEventListener("DOMContentLoaded",()=>{
  setTimeout(()=>{ if(!netmapActive) applyNetworkMapToggle(); },400);
});
if(document.readyState!=="loading"){
  setTimeout(()=>{ if(!netmapActive) applyNetworkMapToggle(); },400);
}
