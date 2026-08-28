"use strict";
/* ====================== GPU PANEL (snarf) ===========================
   The two Quadro RTX 6000s are the scarcest thing in the fleet: one model
   sits in the seat at a time, and when they are busy nothing else in the
   rack can think. That makes them the sidebar's most important readout,
   so they are rendered as vertical bars near the top rather than as another
   key/value row buried in MACHINES.

   Data: /api/rack_health, which queries the nvidia_gpu_exporter on
   snarf:9835 through Prometheus on CT110. Every GPU query returns TWO
   series, one per card.

   ORDERING TRAP: the exporter returns series in arbitrary order, and uuid
   04185dbf is physically GPU *1* while 23afce94 is GPU *0*. Rendering by
   array position silently mislabels the cards. Always join on
   snarf_gpu_index.
==================================================================== */

function gpuByUuid(series) {
  const out = {};
  (series || []).forEach(r => {
    const u = r && r.labels && r.labels.uuid;
    if (u) out[u] = r.value;
  });
  return out;
}

function gpuTempClass(c) {
  if (c == null) return "";
  if (c >= 80) return "gpu-t-hot";
  if (c >= 65) return "gpu-t-warm";
  return "gpu-t-ok";
}

function gpuFmtBytes(b) {
  if (!b && b !== 0) return "—";
  return (b / 1073741824).toFixed(1) + "G";
}

function renderGpuPanel(rh) {
  const host = document.getElementById("gpuBars");
  if (!host) return;

  const idx = rh && rh.snarf_gpu_index;
  if (!Array.isArray(idx) || !idx.length) {
    host.innerHTML = '<div class="gpu-offline">— GPU telemetry unavailable —</div>';
    return;
  }

  const temp  = gpuByUuid(rh.snarf_gpu_temp_c);
  const util  = gpuByUuid(rh.snarf_gpu_util_ratio);
  const used  = gpuByUuid(rh.snarf_gpu_mem_used);
  const total = gpuByUuid(rh.snarf_gpu_mem_total);
  const power = gpuByUuid(rh.snarf_gpu_power_w);

  // Sort by PHYSICAL index, not by the order Prometheus happened to return.
  const cards = idx
    .map(r => ({ uuid: r.labels.uuid, i: Number(r.value) }))
    .sort((a, b) => a.i - b.i);

  host.innerHTML = cards.map(({ uuid, i }) => {
    const u = util[uuid];
    const uPct = u == null ? 0 : Math.max(0, Math.min(100, u * 100));
    const mu = used[uuid], mt = total[uuid];
    const mPct = (mu != null && mt) ? Math.max(0, Math.min(100, (mu / mt) * 100)) : 0;
    const t = temp[uuid], w = power[uuid];
    return `
      <div class="gpu-card">
        <div class="gpu-bars">
          <div class="gpu-bar" title="utilization ${uPct.toFixed(0)}%"><i style="height:${uPct}%"></i></div>
          <div class="gpu-bar vram" title="VRAM ${gpuFmtBytes(mu)} / ${gpuFmtBytes(mt)}"><i style="height:${mPct}%"></i></div>
        </div>
        <div class="gpu-caps"><span>UTL</span><span>VRM</span></div>
        <div class="gpu-name">GPU${i}</div>
        <div class="gpu-stats">
          <b>${uPct.toFixed(0)}%</b> · <b>${mPct.toFixed(0)}%</b><br>
          <span class="${gpuTempClass(t)}">${t == null ? "—" : t + "°C"}</span>
          · ${w == null ? "—" : w.toFixed(0) + "W"}
        </div>
      </div>`;
  }).join("");
}

async function pollGpuPanel() {
  try {
    const r = await fetch("/api/rack_health", { credentials: "same-origin" });
    if (r.ok) renderGpuPanel(await r.json());
  } catch { /* transient; keep the last good render rather than blanking */ }
}

addEventListener("DOMContentLoaded", () => {
  pollGpuPanel();
  setInterval(pollGpuPanel, 10000);   // matches the endpoint's own 10s cache
});
