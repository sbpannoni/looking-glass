"use strict";
/* ========================== CODER MODELS ============================
   One card per candidate model for the coder-engine (Hermes+LangGraph+
   Aider) pipeline's Phase 3 eval harness: what it's for, and how it's
   actually performing in eval_results.json so far. Metrics are pushed here
   by a CT110 script (homelab/coder_models_refresh.py) that pulls the raw
   run log from snarf — see server.py's /api/coder-models docstring.

   Mirrors ownership.js's PROJECT FLOW panel shape (fetch -> cards -> poll).
================================================================= */

const CODER_STATUS_DOT = {
  "resident default": "on",
  "resident (rotation)": "on",
};

function coderModelDot(status){
  if (CODER_STATUS_DOT[status]) return CODER_STATUS_DOT[status];
  if ((status||"").includes("higher risk")) return "off";
  return "na";
}

// Sort into sections so the panel reads best-info-first instead of
// insertion order (which was just "whatever order candidates got added"):
// resident/rotation models, then tested candidates ranked by pass rate
// (the actual decision-relevant number), then untested/pending downloads,
// then hardware-incompatible ones ruled out entirely.
function coderModelBucket(d){
  const status = d.status || "";
  if (status === "resident default") return 0;
  if (status.startsWith("resident")) return 1;
  if (status.startsWith("incompatible")) return 4;
  if (!d.metrics || d.metrics.total_runs == null) return 3;
  return 2;
}

const CODER_BUCKET_LABEL = {
  0: "RESIDENT / ROTATION",
  1: "RESIDENT / ROTATION",
  2: "TESTED — RANKED BY PASS RATE",
  3: "AWAITING EVAL",
  4: "RULED OUT — HARDWARE INCOMPATIBLE",
};

function sortCoderModels(models){
  return models.slice().sort((a,b)=>{
    const ba = coderModelBucket(a), bb = coderModelBucket(b);
    if (ba !== bb) return ba - bb;
    if (ba === 2){
      const pa = a.metrics && a.metrics.pass_rate != null ? a.metrics.pass_rate : -1;
      const pb = b.metrics && b.metrics.pass_rate != null ? b.metrics.pass_rate : -1;
      if (pb !== pa) return pb - pa;
    }
    return 0;
  });
}

function coderMetricsBlock(m){
  if (!m){
    return `<div class="mcard-metrics mcard-none">no runs yet</div>`;
  }
  const pct = m.pass_rate == null ? "—" : Math.round(m.pass_rate*100)+"%";
  const avg = m.avg_elapsed_s == null ? "—" : m.avg_elapsed_s+"s";
  const last = m.last_run_at ? new Date(m.last_run_at).toLocaleString() : "—";
  return `<div class="mcard-metrics">
    <div class="mcard-metric-row"><span>pass rate</span><b>${pct}</b></div>
    <div class="mcard-metric-row"><span>runs</span><b>${m.total_runs} (${m.pass}✓ ${m.fail}✗ ${m.error}⚠)</b></div>
    <div class="mcard-metric-row"><span>avg time</span><b>${avg}</b></div>
    <div class="mcard-metric-row"><span>last run</span><b>${last}</b></div>
  </div>`;
}

function coderModelCard(d){
  const dot = coderModelDot(d.status);
  const risky = (d.status||"").includes("higher risk");
  return `<div class="mcard ${risky?"risky":""}">
    <div class="mcard-head">
      <span class="dot ${dot}"></span><b>${d.label}</b>
      <span class="mcard-license">${d.license}</span>
    </div>
    <div class="mcard-params">${d.params}</div>
    <div class="mcard-status">${d.status}</div>
    <div class="mcard-good">${d.good_for}</div>
    ${coderMetricsBlock(d.metrics)}
  </div>`;
}

function coderModelSections(models){
  const sorted = sortCoderModels(models);
  let html = "";
  let lastBucket = null;
  let cards = [];
  const flush = () => {
    if (!cards.length) return;
    html += `<div class="flow-head-bar mcard-section-bar">${CODER_BUCKET_LABEL[lastBucket]}</div>
             <div class="flow-grid mcard-grid">${cards.join("")}</div>`;
    cards = [];
  };
  for (const d of sorted){
    const bucket = coderModelBucket(d);
    if (lastBucket !== null && bucket !== lastBucket && !(lastBucket === 0 && bucket === 1)) flush();
    lastBucket = bucket;
    cards.push(coderModelCard(d));
  }
  flush();
  return html;
}

async function renderCoderModels(panel){
  try{
    const r = await fetch("/api/coder-models");
    const j = await r.json();
    const gen = j.metrics_generated_at
      ? `metrics refreshed ${new Date(j.metrics_generated_at).toLocaleString()} from ${j.metrics_source||"?"}`
      : "metrics not yet refreshed — run homelab/coder_models_refresh.py on claude-control";
    panel.innerHTML = `<div class="flow-head-bar">CODER-ENGINE MODEL ROSTER — ${gen}</div>
                       ${coderModelSections(j.models||[])}`;
  }catch(err){
    panel.innerHTML = `<div class="kv"><span class="err">coder models unavailable: ${err.message}</span></div>`;
  }
}

function openCoderModels(){
  openWorkTabTurning("coder-models","main","CODER MODELS",(panel,tab)=>{
    panel.innerHTML = `<div class="kv"><span>loading…</span></div>`;
    panel.classList.add("flow-pane");
    renderCoderModels(panel);
    const iv = setInterval(()=>renderCoderModels(panel), 30000);
    tab.onBeforeClose = () => clearInterval(iv);
  });
}

document.querySelectorAll('[data-action="coder-models"]').forEach(b=>b.addEventListener("click", openCoderModels));
