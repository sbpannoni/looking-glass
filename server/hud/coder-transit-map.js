"use strict";
/* ======================== CODER-ENGINE TRANSIT MAP ======================
   Subway-style diagram of every role the coder-engine pipeline needs a
   model for: Editor (live), Reviewer (live, new), Orchestrator (native
   Hermes mechanism, unevaluated). Data comes from /api/coder-transit-map,
   which combines the same editor metrics /api/coder-models reads with a
   new reviewer-metrics file -- both refreshed by coder_models_refresh.py
   on claude-control (CT110). See that endpoint's docstring in server.py.

   Line colors deliberately reuse existing HUD semantics rather than
   inventing a new palette: --cyan is already this dashboard's coder-engine
   accent (CODER MODELS panel), --magenta already means "agent work +
   Claude" (the human/Claude review pairing this Reviewer line is meant to
   offload), --amber already means kanban/task-work (fits Orchestrator's
   task-decomposition role). Station status dots stay teal=good/
   orange=native-but-unevaluated/dim=not-built, matching .dot.on elsewhere.

   Station labels are angled (real transit-map convention -- London/NYC
   maps do exactly this) rather than centered under each stop: at 160px
   station spacing, horizontal centered labels for names like "DOCKER: EDIT
   (AIDER)" overlapped their neighbors. Editor/reviewer labels angle up-
   right into open space above their row; orchestrator angles down-right
   into the bottom margin -- never toward another row's content.

   Mirrors coder-models.js's fetch -> render -> poll shape and
   openWorkTabTurning shell.
================================================================= */

function transitFmtPct(x){ return x == null ? "—" : Math.round(x * 100) + "%"; }
function transitFmtTime(x){ return x == null ? "—" : x + "s"; }

// Performance heatmap for card borders: red (0) -> amber -> teal (1). Null
// (no data yet) stays the neutral line color -- absence isn't "bad", it's
// unmeasured, and shouldn't read as a red flag next to real low scores.
function transitHeatColor(value){
  if (value == null) return "var(--line)";
  const v = Math.max(0, Math.min(1, value));
  const hue = v * 150; // 0=red, ~40=amber, 150=teal-green
  return `hsl(${hue}, 78%, 52%)`;
}

function transitStationDot(status){
  if (status === "good") return `<circle class="tm-station-dot" r="4" fill="var(--teal)"/>`;
  if (status === "warn") return `<circle class="tm-station-dot" r="4" fill="var(--orange)"/>`;
  return "";
}

function transitStationCircle(cx, cy, lineColorVar, status){
  const dashed = status === "none" ? ' stroke-dasharray="2 4"' : "";
  const fill = status === "none" ? "var(--bg)" : "var(--panel)";
  return `<circle cx="${cx}" cy="${cy}" r="8" fill="${fill}" stroke="${lineColorVar}" stroke-width="3"${dashed}/>
          <g transform="translate(${cx},${cy})">${transitStationDot(status)}</g>`;
}

// Angled label, real-subway-map style. dir "up": text starts just above-
// right of the station and reads diagonally up-right (rotate -40). dir
// "down": starts below-right and reads diagonally down-right (rotate 40).
// Always text-anchor="start" so the label extends AWAY from the station,
// never back over it or its neighbors.
function transitAngledLabel(cx, cy, text, dir){
  const dy = dir === "up" ? -10 : 10;
  const x = cx + 9, y = cy + dy;
  const angle = dir === "up" ? -40 : 40;
  return `<text x="${x}" y="${y}" transform="rotate(${angle} ${x} ${y})" text-anchor="start" class="tm-station-label">${text}</text>`;
}

function transitMetricChip(x, y, text, color){
  return `<text x="${x}" y="${y}" text-anchor="middle" class="tm-metric-chip" fill="${color||'var(--txt-dim)'}">${text}</text>`;
}

function buildTransitSvg(data){
  const editorTop = (data.editor.models || [])[0];
  const editorChip = editorTop
    ? transitMetricChip(780, 205, `${editorTop.label.toUpperCase()} ${transitFmtPct(editorTop.pass_rate)} · ${transitFmtTime(editorTop.avg_elapsed_s)}`, "var(--teal)")
    : transitMetricChip(780, 205, "no runs yet", "var(--txt-dim)");
  const editorSub = transitMetricChip(780, 219, `${(data.editor.models||[]).length} candidates measured`, "var(--txt-dim)");

  const reviewGraded = (data.reviewer.models || []).filter(m => m.graded_count > 0);
  const reviewTop = reviewGraded[0];
  const reviewChip = reviewTop
    ? transitMetricChip(780, 245, `${reviewTop.label.toUpperCase()} ${transitFmtPct(reviewTop.catch_rate)} catch · ${transitFmtPct(reviewTop.false_positive_rate)} FP`, "var(--teal)")
    : transitMetricChip(780, 245, "no graded reviews yet", "var(--txt-dim)");
  const reviewSub = transitMetricChip(780, 259, `${reviewGraded.length} candidates graded`, "var(--txt-dim)");

  const orchGraded = (data.orchestrator.models || []).filter(m => m.graded_count > 0);
  const orchTop = orchGraded[0];
  const orchChip = orchTop
    ? transitMetricChip(700, 495, `${orchTop.label.toUpperCase()} ${transitFmtPct(orchTop.coverage_rate)} coverage`, "var(--teal)")
    : transitMetricChip(700, 495, "0 candidates measured", "var(--orange)");
  const orchSub = transitMetricChip(700, 509, `${orchGraded.length} candidates graded`, "var(--txt-dim)");

  const reviewerHasData = reviewGraded.length > 0;
  const reviewerStationStatus = reviewerHasData ? "good" : "none";
  const reviewerLineDash = reviewerHasData ? "" : ' stroke-dasharray="2 14"';
  const orchStationStatus = orchGraded.length > 0 ? "good" : "warn";

  return `
  <svg viewBox="0 0 1300 600" role="img" class="tm-svg" aria-label="Transit-style diagram of the coder-engine pipeline: a shared Kanban-and-Dispatch trunk splits into an Editor line, a Reviewer line, and an Orchestrator line, reconverging at this HUD.">

    <g stroke="var(--line)" stroke-width="1" opacity="0.5">
      <line x1="40" y1="150" x2="1260" y2="150"/>
      <line x1="40" y1="300" x2="1260" y2="300"/>
      <line x1="40" y1="450" x2="1260" y2="450"/>
    </g>

    <g fill="none" stroke-width="6" stroke-linecap="round">
      <path d="M 80 292 L 280 292" stroke="var(--cyan)"/>
      <path d="M 80 300 L 280 300" stroke="var(--magenta)"/>
      <path d="M 80 308 L 280 308" stroke="var(--amber)"/>
    </g>

    <g fill="none" stroke="var(--cyan)" stroke-width="6" stroke-linecap="round" stroke-linejoin="round">
      <path d="M 280 292 L 310 292 L 420 150 L 1060 150 L 1170 270 L 1200 292"/>
    </g>

    <g fill="none" stroke="var(--amber)" stroke-width="6" stroke-linecap="round" stroke-linejoin="round">
      <path d="M 280 308 L 310 308 L 420 450 L 1060 450 L 1170 330 L 1200 308"/>
    </g>

    <g fill="none" stroke="var(--magenta)" stroke-width="6" stroke-linecap="round"${reviewerLineDash}>
      <path d="M 280 300 L 1200 300"/>
    </g>

    <g fill="none" stroke-width="6" stroke-linecap="round">
      <path d="M 1200 292 L 1250 292" stroke="var(--cyan)"/>
      <path d="M 1200 300 L 1250 300" stroke="var(--magenta)"/>
      <path d="M 1200 308 L 1250 308" stroke="var(--amber)"/>
    </g>

    <text x="440" y="118" class="tm-line-tag" fill="var(--cyan)">EDITOR — LIVE</text>
    <text x="330" y="272" class="tm-line-tag" fill="var(--magenta)">REVIEWER — ${reviewerHasData ? "LIVE" : "NOT BUILT"}</text>
    <text x="400" y="500" class="tm-line-tag" fill="var(--amber)">ORCHESTRATOR — NATIVE, ${orchGraded.length > 0 ? "BENCHMARKED" : "UNTESTED"}</text>

    <circle cx="80" cy="300" r="12" fill="var(--panel)" stroke="var(--txt)" stroke-width="3"/>
    <circle cx="80" cy="300" r="4" fill="var(--teal)"/>
    ${transitAngledLabel(80, 300, "KANBAN BOARD", "down")}

    <circle cx="280" cy="300" r="12" fill="var(--panel)" stroke="var(--txt)" stroke-width="3"/>
    <circle cx="280" cy="300" r="4" fill="var(--teal)"/>
    ${transitAngledLabel(280, 300, "CLAIM + DISPATCH", "down")}

    ${transitStationCircle(560, 150, "var(--cyan)", "good")}
    ${transitAngledLabel(560, 150, "WORKTREE + BRANCH", "up")}

    ${transitStationCircle(700, 150, "var(--cyan)", "good")}
    ${transitAngledLabel(700, 150, "DOCKER: EDIT (AIDER)", "up")}
    ${editorChip}${editorSub}

    ${transitStationCircle(840, 150, "var(--cyan)", "good")}
    ${transitAngledLabel(840, 150, "TEST GATE", "up")}

    ${transitStationCircle(980, 150, "var(--cyan)", "good")}
    ${transitAngledLabel(980, 150, "COMMIT + PATCH", "up")}

    ${transitStationCircle(560, 450, "var(--amber)", orchStationStatus)}
    ${transitAngledLabel(560, 450, "TASK INTAKE", "down")}

    ${transitStationCircle(700, 450, "var(--amber)", orchStationStatus)}
    ${transitAngledLabel(700, 450, "MODEL: DECOMPOSE", "down")}
    ${orchChip}${orchSub}

    ${transitStationCircle(840, 450, "var(--amber)", orchStationStatus)}
    ${transitAngledLabel(840, 450, "DEPENDENCY GRAPH", "down")}

    ${transitStationCircle(980, 450, "var(--amber)", orchStationStatus)}
    ${transitAngledLabel(980, 450, "KANBAN: CHILD TASKS", "down")}

    ${transitStationCircle(560, 300, "var(--magenta)", reviewerStationStatus)}
    ${transitAngledLabel(560, 300, "WORKTREE INJECT", "up")}

    ${transitStationCircle(700, 300, "var(--magenta)", reviewerStationStatus)}
    ${transitAngledLabel(700, 300, "MODEL: ANALYZE", "up")}
    ${reviewChip}${reviewSub}

    ${transitStationCircle(840, 300, "var(--magenta)", reviewerStationStatus)}
    ${transitAngledLabel(840, 300, "FINDINGS REPORT", "up")}

    <circle cx="980" cy="300" r="8" fill="var(--bg)" stroke="var(--magenta)" stroke-width="3" stroke-dasharray="2 4"/>
    ${transitAngledLabel(980, 300, "KANBAN: TRIAGE CARD", "down")}

    <circle cx="1200" cy="300" r="12" fill="var(--panel)" stroke="var(--txt)" stroke-width="3"/>
    <circle cx="1200" cy="300" r="4" fill="var(--teal)"/>
    ${transitAngledLabel(1200, 300, "RESULT", "down")}
  </svg>`;
}

function transitModelCard(m, kind){
  const heat = kind === "editor" ? transitHeatColor(m.pass_rate) : transitHeatColor(m.catch_rate);
  const style = `border-top-color:${heat}`;
  if (kind === "editor"){
    return `<div class="mcard" style="${style}">
      <div class="mcard-head"><b>${m.label}</b></div>
      <div class="mcard-metrics">
        <div class="mcard-metric-row"><span>pass rate</span><b>${transitFmtPct(m.pass_rate)}</b></div>
        <div class="mcard-metric-row"><span>avg time</span><b>${transitFmtTime(m.avg_elapsed_s)}</b></div>
        <div class="mcard-metric-row"><span>runs</span><b>${m.total_runs}</b></div>
      </div>
    </div>`;
  }
  const graded = m.graded_count > 0;
  return `<div class="mcard" style="${style}">
    <div class="mcard-head"><b>${m.label}</b></div>
    <div class="mcard-metrics">
      ${graded ? `
        <div class="mcard-metric-row"><span>catch rate</span><b>${transitFmtPct(m.catch_rate)}</b></div>
        <div class="mcard-metric-row"><span>false-positive rate</span><b>${transitFmtPct(m.false_positive_rate)}</b></div>
        <div class="mcard-metric-row"><span>graded</span><b>${m.graded_count}</b></div>
      ` : `<div class="mcard-metrics mcard-none">${m.raw_reviews_captured} captured, not yet graded</div>`}
    </div>
  </div>`;
}

function transitOrchestratorCard(m){
  const heat = transitHeatColor(m.coverage_rate);
  const style = `border-top-color:${heat}`;
  const graded = m.graded_count > 0;
  return `<div class="mcard" style="${style}">
    <div class="mcard-head"><b>${m.label}</b></div>
    <div class="mcard-metrics">
      ${graded ? `
        <div class="mcard-metric-row"><span>coverage</span><b>${transitFmtPct(m.coverage_rate)} (${m.items_covered}/${m.items_total})</b></div>
        <div class="mcard-metric-row"><span>ordering sound</span><b>${m.ordering_sound_count}/${m.graded_count}</b></div>
        <div class="mcard-metric-row"><span>invented extra work</span><b>${m.invented_unnecessary_work_count}/${m.graded_count}</b></div>
      ` : `<div class="mcard-metrics mcard-none">${m.raw_plans_captured} captured, not yet graded</div>`}
    </div>
  </div>`;
}

function transitRoleOptions(roster, selected){
  // A model with tool_calling === false (from CODER_MODELS_ROSTER via
  // /api/model-role-assignments) can't emit structured tool calls, so it can't
  // fill any role -- render it disabled with a visible reason. The server
  // rejects it too (POST guard), since this endpoint is callable directly.
  return (roster || []).map(m => {
    const noTools = m.tool_calling === false;
    const label = noTools ? `${m.label} — no tool calls (can't be assigned)` : m.label;
    return `<option value="${m.id}"${m.id === selected ? " selected" : ""}`
      + `${noTools ? " disabled" : ""}>${label}</option>`;
  }).join("");
}

// Dropdown bar surfacing the model actually configured per role and
// letting it be changed live -- separate from the leaderboard cards below,
// which rank candidates by eval score but don't say what's actually set.
// Backed by model_role_assignments.json on snarf (see server.py's
// /api/model-role-assignments docstring): "editor" is the only role a real
// dispatch reads today, so its LIVE/NOT WIRED tag tells you which changes
// actually take effect on the next dispatch vs. are just recorded for when
// reviewer/orchestrator get a real dispatch path.
function transitRoleBar(roleData){
  const roster = roleData.roster || [];
  const assignments = roleData.assignments || {};
  const live = new Set(roleData.live_roles || []);
  const roles = [["editor", "EDITOR"], ["reviewer", "REVIEWER"], ["orchestrator", "ORCHESTRATOR"]];
  return `<div class="tm-role-bar">${roles.map(([key, label]) => `
    <div class="tm-role-item">
      <span class="tm-role-label">${label}</span>
      <select class="tm-role-select" data-role="${key}">${transitRoleOptions(roster, assignments[key])}</select>
      <span class="${live.has(key) ? "tm-role-live" : "tm-role-notlive"}">${live.has(key) ? "LIVE" : "NOT WIRED"}</span>
      <span class="tm-role-status" data-role-status="${key}"></span>
    </div>`).join("")}
  </div>`;
}

async function saveRoleAssignment(role, model, statusEl){
  statusEl.textContent = "saving…";
  statusEl.className = "tm-role-status";
  try{
    const r = await fetch("/api/model-role-assignments", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({role, model}),
    });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    statusEl.textContent = "saved";
    statusEl.className = "tm-role-status saved";
    setTimeout(() => { statusEl.textContent = ""; }, 2500);
  }catch(err){
    statusEl.textContent = `error: ${err.message}`;
    statusEl.className = "tm-role-status err";
  }
}

function wireRoleBar(panel){
  panel.querySelectorAll(".tm-role-select").forEach(sel => {
    sel.addEventListener("change", () => {
      const role = sel.dataset.role;
      const statusEl = panel.querySelector(`[data-role-status="${role}"]`);
      saveRoleAssignment(role, sel.value, statusEl);
    });
  });
}

// One-click trigger for the reviewer role's real live path (POST
// /api/review-file -> dispatch_review_task.py on snarf -> a --triage
// kanban card, never a higher status). One call = one review; no polling
// loop needed, the request itself blocks until the review (and card
// filing) completes or fails.
function wireReviewTrigger(panel){
  const btn = panel.querySelector(".tm-review-btn");
  const input = panel.querySelector(".tm-review-input");
  const statusEl = panel.querySelector(".tm-review-status");
  if (!btn || !input || !statusEl) return;
  btn.addEventListener("click", async () => {
    const target_file = input.value.trim();
    if (!target_file){ statusEl.textContent = "enter a file path first"; statusEl.className = "tm-review-status err"; return; }
    btn.disabled = true;
    statusEl.textContent = "reviewing… (can take a minute or two)";
    statusEl.className = "tm-review-status";
    try{
      const r = await fetch("/api/review-file", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({target_file}),
      });
      const j = await r.json();
      if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
      statusEl.textContent = `filed to triage: ${j.task.id}`;
      statusEl.className = "tm-review-status saved";
    }catch(err){
      statusEl.textContent = `error: ${err.message}`;
      statusEl.className = "tm-review-status err";
    }finally{
      btn.disabled = false;
    }
  });
}

async function renderTransitMap(panel){
  try{
    const [r, roleR] = await Promise.all([
      fetch("/api/coder-transit-map"),
      fetch("/api/model-role-assignments"),
    ]);
    const j = await r.json();
    const roleData = roleR.ok ? await roleR.json() : {roster: [], assignments: {}, live_roles: []};
    const editorGen = j.editor.generated_at ? new Date(j.editor.generated_at).toLocaleString() : "never";
    const reviewGen = j.reviewer.generated_at ? new Date(j.reviewer.generated_at).toLocaleString() : "never";
    const orchGen = j.orchestrator.generated_at ? new Date(j.orchestrator.generated_at).toLocaleString() : "never";

    const editorCards = (j.editor.models || []).map(m => transitModelCard(m, "editor")).join("");
    const reviewerCards = (j.reviewer.models || []).map(m => transitModelCard(m, "reviewer")).join("");
    const orchCards = (j.orchestrator.models || []).map(transitOrchestratorCard).join("");

    panel.innerHTML = `
      <div class="flow-head-bar">CODER-ENGINE TRANSIT MAP — editor ${editorGen}, reviewer ${reviewGen}, orchestrator ${orchGen}</div>
      ${transitRoleBar(roleData)}
      <div class="tm-svg-wrap">${buildTransitSvg(j)}</div>
      <details class="tm-line-section">
        <summary class="flow-head-bar">EDITOR LINE — ranked by pass rate</summary>
        <div class="flow-grid mcard-grid">${editorCards || '<div class="kv"><span>no runs yet</span></div>'}</div>
      </details>
      <details class="tm-line-section">
        <summary class="flow-head-bar">REVIEWER LINE — ranked by catch rate</summary>
        <div class="tm-review-trigger">
          <input type="text" class="tm-review-input" placeholder="path inside DARKHELIX, e.g. darkhelix/ui_registry.py" />
          <button type="button" class="btn tm-review-btn">RUN REVIEW</button>
          <span class="tm-review-status"></span>
        </div>
        <div class="flow-grid mcard-grid">${reviewerCards || '<div class="kv"><span>no runs yet</span></div>'}</div>
      </details>
      <details class="tm-line-section">
        <summary class="flow-head-bar">ORCHESTRATOR LINE — ranked by coverage rate</summary>
        <div class="flow-grid mcard-grid">${orchCards || '<div class="kv"><span>no runs yet</span></div>'}</div>
        <div class="kv" style="padding:6px 10px"><span style="color:var(--txt-dim); font-size:11px">${j.orchestrator.note}</span></div>
      </details>
    `;
    wireRoleBar(panel);
    wireReviewTrigger(panel);
  }catch(err){
    panel.innerHTML = `<div class="kv"><span class="err">transit map unavailable: ${err.message}</span></div>`;
  }
}

function openCoderTransitMap(){
  openWorkTabTurning("coder-transit-map","main","TRANSIT MAP",(panel,tab)=>{
    panel.innerHTML = `<div class="kv"><span>loading…</span></div>`;
    panel.classList.add("flow-pane");
    renderTransitMap(panel);
    const iv = setInterval(()=>renderTransitMap(panel), 30000);
    tab.onBeforeClose = () => clearInterval(iv);
  });
}

document.querySelectorAll('[data-action="coder-transit-map"]').forEach(b=>b.addEventListener("click", openCoderTransitMap));
