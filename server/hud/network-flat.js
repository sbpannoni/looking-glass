"use strict";
/* ========================= FLAT LINKAGE MAP =========================
   Replaces the 3D force-directed network. The landscape/gridscape in
   network-map.js is untouched — that is permanent scenery; this is the
   instrument drawn over it.

   Why flat, fixed SVG instead of a force graph:
     - Force layout makes positions EMERGENT. The old file has a comment
       about fighting "nodes jumping around". Nothing was ever in the same
       place twice, so nobody could build muscle memory. Here every node
       has a hand-chosen home and stays in it.
     - 3D means occlusion; you could not tell what connected to what,
       which is the only thing a linkage map is for.
     - 11 nodes / ~25 edges is tiny. SVG buys crisp text at any zoom,
       free hit-testing, and CSS-var theming, with no THREE dependency.

   THREE REGIONS, not six (Sam's call 2026-08-28). A true Venn collapses
   past 3 sets, and virtualisation/home-automation cut across the others.
   Memory is a CONNECTOR (see memory_edges in server.yaml) rather than a
   fourth region, so the only overlaps drawn are the two that matter:
     hermes        in {inference, agent work}
     claude-control in {agent work, out-of-band}

   POSITIONS ARE CONTENT. They are chosen so every region's members are
   spatially contiguous — that is what keeps the hulls from swallowing
   non-members. Moving a node without re-checking the hulls will break it.
==================================================================== */

const FN_POS = {
  // normalized 0..1, y down. OOB cluster sits next to claude-control and
  // snarf sits next to hermes; that adjacency is what makes the hulls work.
  "looking-glass":  [0.20, 0.35],
  "hermes":         [0.50, 0.35],
  "snarf":          [0.80, 0.35],
  "claude-control": [0.22, 0.60],
  "snarf-bmc":      [0.09, 0.82],
  "r720-idrac":     [0.22, 0.86],
  "pdu":            [0.35, 0.82],
  "home-assistant": [0.50, 0.13],
  // Was [0.66, 0.62] -- same y as octominer, which put it 4.4px off the
  // claude-control->octominer ssh line, so that edge read as terminating at
  // beelink. Dropped and pulled left: clear of that path, clear of
  // claude-control->r720, and outside the OUT-OF-BAND hull.
  "beelink":        [0.55, 0.78],
  "octominer":      [0.86, 0.62],
  "r720":           [0.90, 0.84],
};

const FN_USES = [
  { id:"agentwork", label:"AGENT WORK", color:"#22e5ff",
    members:["looking-glass","hermes","claude-control"] },
  { id:"inference", label:"INFERENCE",  color:"#ff2fd0",
    members:["hermes","snarf"] },
  { id:"oob",       label:"OUT-OF-BAND",color:"#ff8a3d",
    members:["claude-control","snarf-bmc","r720-idrac","pdu"] },
];

// Stroke pattern differs per kind as well as colour, so the map survives
// greyscale and colour-blindness — colour alone is not a channel.
const FN_KINDS = {
  ssh:       { label:"ssh",       color:"#22e5ff", dash:"",       width:1.6 },
  inference: { label:"inference", color:"#ff2fd0", dash:"",       width:3.0 },
  mempalace: { label:"memory",    color:"#4ade80", dash:"7 4",    width:1.8 },
  ipmi:      { label:"ipmi/oob",  color:"#ff8a3d", dash:"2 5",    width:1.6 },
  api:       { label:"api",       color:"#fbbf24", dash:"10 3 2 3", width:1.6 },
  host:      { label:"hosts",     color:"#5b6b82", dash:"1 5",    width:1.4 },
};

let fnRoot=null, fnSvg=null, fnTopo=null;
let fnActiveKind=null, fnFocus=null;
const fnOff = new Set();          // kinds toggled off in the legend

const NS="http://www.w3.org/2000/svg";
function el(tag, attrs){
  const n=document.createElementNS(NS, tag);
  for(const k in (attrs||{})) n.setAttribute(k, attrs[k]);
  return n;
}

/* ---- edges: API groups (by source) -> connector kinds (by purpose) ---- */
function fnEdges(topo){
  const e=(topo && topo.edges) || {};
  const out=[];
  const known = id => !!FN_POS[id];
  (e.physical||[]).forEach(x=>{ if(known(x.from)&&known(x.to)) out.push({from:x.from,to:x.to,kind:"host"}); });
  (e.memory||[]).forEach(x=>{ if(known(x.from)&&known(x.to)) out.push({from:x.from,to:x.to,kind:"mempalace"}); });
  (e.hermes||[]).forEach(x=>{
    if(!known(x.to)) return;
    out.push({from:"hermes", to:x.to, kind: x.port===8000 ? "inference" : "api", label:x.label});
  });
  (e.claude||[]).forEach(x=>{
    if(!known(x.to)) return;                       // "claude" is a pseudo-source;
    const from="claude-control";                   // the agent actually runs on CT110
    if(x.to===from) return;
    out.push({from, to:x.to, kind: x.via==="ipmitool" ? "ipmi" : "ssh"});
  });
  // edges.general is a lan->every-node star: pure clutter on a flat map, dropped.
  return out;
}

function fnReach(id, edges){
  const s=new Set([id]);
  edges.forEach(x=>{ if(x.from===id) s.add(x.to); if(x.to===id) s.add(x.from); });
  return s;
}

/* ---- convex hull (monotone chain) over padded member discs ---- */
function fnHull(pts){
  if(pts.length<3) return pts;
  const p=pts.slice().sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
  const cr=(o,a,b)=>(a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0]);
  const lo=[]; for(const q of p){ while(lo.length>=2&&cr(lo[lo.length-2],lo[lo.length-1],q)<=0)lo.pop(); lo.push(q); }
  const up=[]; for(let i=p.length-1;i>=0;i--){ const q=p[i]; while(up.length>=2&&cr(up[up.length-2],up[up.length-1],q)<=0)up.pop(); up.push(q); }
  lo.pop(); up.pop(); return lo.concat(up);
}
function fnSmooth(h){
  if(h.length<3) return "";
  let d="";
  for(let i=0;i<h.length;i++){
    const cur=h[i], nxt=h[(i+1)%h.length];
    const mx=(cur[0]+nxt[0])/2, my=(cur[1]+nxt[1])/2;
    d += i===0 ? `M ${mx} ${my}` : ` Q ${cur[0]} ${cur[1]} ${mx} ${my}`;
  }
  const f=h[0], l=h[h.length-1];
  d += ` Q ${l[0]} ${l[1]} ${(l[0]+f[0])/2} ${(l[1]+f[1])/2} Z`;
  return d;
}

/* The drawable region is NOT the viewport: the HUD floats fixed side columns,
   a legend bar and a composer over this layer. The first version hardcoded
   `max(360, W*0.19)` per side, which was wrong three ways -- #grid is a FIXED
   `320px 1fr 320px`, so the guess over-inset at every width; below ~1400px the
   two 360px margins ate most of the canvas and the nodes crowded into a strip;
   and it ignored `body.sidebars-hidden`, which slides the columns off-screen
   and should hand the map the whole width.

   So measure the real elements instead of guessing. Off-screen columns yield a
   rect outside the viewport and fall through to the small default, which is
   exactly what the hidden-sidebar case wants. */
function fnMeasure(id){
  const e=document.getElementById(id);
  if(!e) return null;
  const r=e.getBoundingClientRect();
  return (r.width>0 && r.height>0) ? r : null;
}
function fnInsets(W,H){
  const PAD=26;
  let left=PAD, right=PAD, top=PAD, bottom=PAD;
  const l=fnMeasure("colLeft");
  if(l && l.right>0 && l.right<W*0.6) left=l.right+PAD;
  const r=fnMeasure("colRight");
  if(r && r.left<W && r.left>W*0.4) right=(W-r.left)+PAD;
  const c=fnMeasure("netmapControls");
  if(c && c.bottom>0 && c.bottom<H*0.4) top=c.bottom+PAD;
  // #hermesDock is the composer BAR. Measuring #center instead was wrong:
  // that is the whole centre column, so its top sits near the header, the
  // >H*0.5 guard rejected it, and bottom silently fell back to the minimum --
  // which put the OOB node row straight through the composer at reduced
  // heights. Measure the bar itself.
  const dock=fnMeasure("hermesDock");
  if(dock && dock.top>H*0.4 && dock.top<H) bottom=(H-dock.top)+PAD;
  // Never let the insets collapse the canvas to nothing.
  if(W-left-right < W*0.25){ left=right=PAD; }
  if(H-top-bottom < H*0.3){ top=bottom=PAD; }
  return {left,right,top,bottom};
}

function fnRender(){
  if(!fnSvg || !fnTopo) return;
  const W=fnRoot.clientWidth||1200, H=fnRoot.clientHeight||800;
  const ins = fnInsets(W, H);
  const X=u=>ins.left+u*(W-ins.left-ins.right);
  const Y=v=>ins.top+v*(H-ins.top-ins.bottom);
  const R=13;

  while(fnSvg.firstChild) fnSvg.removeChild(fnSvg.firstChild);
  fnSvg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const nodes={}; (fnTopo.nodes||[]).forEach(n=>{ nodes[n.id]=n; });
  const edges=fnEdges(fnTopo).filter(x=>!fnOff.has(x.kind));
  const reach = fnFocus ? fnReach(fnFocus, edges) : null;

  const gHull=el("g"), gEdge=el("g"), gNode=el("g");
  fnSvg.appendChild(gHull); fnSvg.appendChild(gEdge); fnSvg.appendChild(gNode);

  // regions
  FN_USES.forEach(u=>{
    const pts=[];
    u.members.forEach(m=>{
      const p=FN_POS[m]; if(!p) return;
      const cx=X(p[0]), cy=Y(p[1]), pad=R+26;
      for(let a=0;a<12;a++) pts.push([cx+pad*Math.cos(a*Math.PI/6), cy+pad*Math.sin(a*Math.PI/6)]);
    });
    if(!pts.length) return;
    const d=fnSmooth(fnHull(pts));
    const dim = fnFocus ? 0.35 : 1;
    gHull.appendChild(el("path",{d, fill:u.color, "fill-opacity":0.07*dim,
      stroke:u.color, "stroke-opacity":0.34*dim, "stroke-width":1.2}));
    const first=FN_POS[u.members[0]];
    const t=el("text",{x:X(first[0])-34, y:Y(first[1])-R-36, fill:u.color,
      "fill-opacity":0.9*dim, "font-size":11.5, "letter-spacing":2,
      "font-weight":"600", "paint-order":"stroke", stroke:"#020509",
      "stroke-width":3.5, "stroke-linejoin":"round"});
    t.textContent=u.label; gHull.appendChild(t);
  });

  // connectors
  edges.forEach(x=>{
    const a=FN_POS[x.from], b=FN_POS[x.to]; if(!a||!b) return;
    const k=FN_KINDS[x.kind]||FN_KINDS.ssh;
    let op=0.85;
    if(fnActiveKind && x.kind!==fnActiveKind) op=0.10;
    if(reach && !(reach.has(x.from)&&reach.has(x.to))) op=0.07;
    gEdge.appendChild(el("line",{x1:X(a[0]),y1:Y(a[1]),x2:X(b[0]),y2:Y(b[1]),
      stroke:k.color, "stroke-width":k.width, "stroke-dasharray":k.dash,
      "stroke-opacity":op, "stroke-linecap":"round"}));
  });

  // nodes
  Object.keys(FN_POS).forEach(id=>{
    const p=FN_POS[id], n=nodes[id]||{id, up:null, monitored:true};
    const cx=X(p[0]), cy=Y(p[1]);
    let op=1;
    if(reach && !reach.has(id)) op=0.18;
    else if(fnActiveKind) {
      const touches=edges.some(x=>x.kind===fnActiveKind&&(x.from===id||x.to===id));
      if(!touches) op=0.2;
    }
    const g=el("g",{opacity:op, style:"cursor:pointer; pointer-events:auto", id:"fnode-"+id, "data-node":id});
    const ring = n.monitored===false ? "#5b6b82" : (n.up ? "#4ade80" : "#f87171");
    g.appendChild(el("circle",{cx,cy,r:R+5,fill:"none",stroke:ring,
      "stroke-width":1.6,"stroke-opacity":0.85}));
    g.appendChild(el("circle",{cx,cy,r:R,fill:"#0a1220","fill-opacity":0.92,
      stroke:ring,"stroke-opacity":0.35,"stroke-width":1}));
    const halo={"paint-order":"stroke","stroke":"#020509","stroke-width":3.5,
                "stroke-linejoin":"round"};
    const lab=el("text",{x:cx, y:cy+R+17, fill:"#e6f0ff","font-size":12,
      "text-anchor":"middle","letter-spacing":0.6,"font-weight":"600", ...halo});
    lab.textContent=id; g.appendChild(lab);
    if(n.address){
      const ad=el("text",{x:cx,y:cy+R+30,fill:"#8fa5c0","font-size":9.5,
        "text-anchor":"middle", ...halo});
      ad.textContent=n.address; g.appendChild(ad);
    }
    g.addEventListener("click",ev=>{ ev.stopPropagation();
      window.flatNetworkFocus(fnFocus===id ? null : id); });
    gNode.appendChild(g);
  });
}

function fnLegend(){
  const box=document.getElementById("netmapControls");
  if(!box) return;
  box.innerHTML="";
  const t=document.createElement("span");
  t.className="netmap-title"; t.textContent="◈ LINKS"; box.appendChild(t);
  Object.keys(FN_KINDS).forEach(k=>{
    const spec=FN_KINDS[k];
    const l=document.createElement("label");
    l.style.cssText="gap:5px";
    const cb=document.createElement("input");
    cb.type="checkbox"; cb.checked=!fnOff.has(k);
    cb.addEventListener("change",()=>{ cb.checked?fnOff.delete(k):fnOff.add(k); fnRender(); });
    const sw=document.createElement("span");
    sw.style.cssText=`display:inline-block;width:18px;height:0;border-top:${spec.width}px ${spec.dash?"dashed":"solid"} ${spec.color}`;
    const nm=document.createElement("span"); nm.textContent=spec.label;
    l.appendChild(cb); l.appendChild(sw); l.appendChild(nm);
    l.addEventListener("mouseenter",()=>{ fnActiveKind=k; fnRender(); });
    l.addEventListener("mouseleave",()=>{ fnActiveKind=null; fnRender(); });
    box.appendChild(l);
  });
  const hint=document.createElement("span");
  hint.style.cssText="color:#5b6b82;font-size:9px;margin-left:6px";
  hint.textContent="click a node → blast radius";
  box.appendChild(hint);
}

window.flatNetworkInit=function(){
  if(fnRoot) return;
  fnRoot=document.createElement("div");
  fnRoot.id="netmapFlat";
  // inset:0 means this covers the ENTIRE viewport, including every HUD panel
  // and button. It must stay pointer-events:none forever; only the node groups
  // opt back in. Setting it to auto while visible (and the map is the DEFAULT
  // view at boot) made the whole HUD unclickable -- and programmatic
  // e.click() in the screenshot harness bypasses pointer-events, so no
  // screenshot could catch it. Only a real cursor could.
  fnRoot.style.cssText="position:fixed; inset:0; z-index:0; display:none; pointer-events:none";
  fnSvg=el("svg",{width:"100%",height:"100%"});
  fnSvg.style.pointerEvents="none";
  fnRoot.appendChild(fnSvg);
  document.body.appendChild(fnRoot);
  fnLegend();
  addEventListener("resize",()=>{ if(fnRoot.style.display!=="none") fnRender(); });
};

window.flatNetworkSetData=function(topo){ fnTopo=topo; if(fnRoot&&fnRoot.style.display!=="none") fnRender(); };

// Deep link: /hud/#focus=hermes opens straight into that node's blast radius,
// so a finding can be handed to someone as a URL instead of "click the thing".
function fnHashFocus(){
  const m=/(?:^|[#&])focus=([A-Za-z0-9_.-]+)/.exec(location.hash||"");
  return m && FN_POS[m[1]] ? m[1] : null;
}
window.flatNetworkFocus=function(id){
  fnFocus = (id && FN_POS[id]) ? id : null;
  try{ history.replaceState(null,"", fnFocus ? "#focus="+fnFocus : location.pathname); }catch{}
  fnRender();
};
addEventListener("hashchange",()=>{ fnFocus=fnHashFocus(); fnRender(); });
// Clearing focus used to be a click on the SVG background. That background is
// gone (it was the thing eating the HUD's clicks), so Esc does it -- clicking
// the focused node again also toggles it off.
addEventListener("keydown",e=>{ if(e.key==="Escape" && fnFocus) window.flatNetworkFocus(null); });

window.flatNetworkSetVisible=function(on){
  window.flatNetworkInit();
  fnRoot.style.display = on ? "block" : "none";
  if(on){ fnLegend(); fnFocus=fnHashFocus(); fnRender(); }
  else { fnFocus=null; fnActiveKind=null; }
};
