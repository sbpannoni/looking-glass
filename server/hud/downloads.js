"use strict";
/* ============================== DOWNLOADS ============================
   The backlog nobody could start: nine TODO.md items blocked on a database
   or a binary that has never been downloaded.

   They were stuck for a structural reason, not a lack of will. A download is
   bytes and patience — no model, no GPU — but the only way to get work onto
   the board was a card, and the board runs ONE card at a time on ONE seat. A
   110 GB pull would hold that seat for hours doing nothing a GPU is for, so
   filing one meant stopping everything else.

   So a download files as a card and is parked in `scheduled` immediately:
   visible on the board, real id, comments and attachments — and explicitly
   NOT dispatchable, so it cannot take the seat. The bytes come down through
   wget on snarf, off the board entirely.

   Two honesty rules this pane exists to enforce:

   * A URL is checked before it is offered, from snarf, which is the host that
     will do the downloading. An item with no verified URL says "needs a URL"
     rather than showing a guess — the seed catalogue lost a plausible
     ConoServer path exactly that way (404).
   * What is already on disk is compared against the server's Content-Length.
     The Mash sketch turned out to be complete on snarf already — that item is
     blocked on wire-in, not on a download, and no card should be filed for it.

   And the download is never the whole job: every entry carries its wire-in
   step, because "the bytes are down" is where these items historically stop. */

const DL_SIZES = [[1e12, "TB"], [1e9, "GB"], [1e6, "MB"], [1e3, "kB"]];

function dlEsc(s){
  return (s || "").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
}

function dlBytes(n){
  if(n === null || n === undefined) return "—";
  for(const [scale, unit] of DL_SIZES){
    if(n >= scale) return (n / scale).toFixed(n / scale < 10 ? 1 : 0) + " " + unit;
  }
  return n + " B";
}

/* The verdict, spelled out. `complete` is the one that changes what you do:
   it means the blocker is the wire-in step and a download card would be
   re-pulling something that is already there. */
const DL_STATE_TEXT = {
  missing:  {cls: "dl-missing",  label: "not on disk"},
  partial:  {cls: "dl-partial",  label: "partial — resume"},
  present:  {cls: "dl-present",  label: "on disk"},
  complete: {cls: "dl-complete", label: "already complete"},
};

function dlRow(row){
  const e = row.entry;
  if(!e){
    return `<div class="dl-row dl-row-none">
      <div class="dl-head"><span class="dl-name">${dlEsc(row.title)}</span>
        <span class="dl-chip dl-none">needs a URL</span></div>
      ${(row.text || "").trim() === (row.title || "").trim()
        ? "" : `<div class="dl-text">${dlEsc(row.text)}</div>`}
      <div class="dl-note">No catalogue entry. Add one to
        <code>server/config/downloads.yaml</code> — a verified URL, the
        destination on snarf, and what still has to be wired in after it lands.
        Some of these are curation rather than a download, and those belong on
        the board as ordinary work.</div>
    </div>`;
  }
  const st = DL_STATE_TEXT[e.state] || DL_STATE_TEXT.present;
  const url = e.url_state || {};
  const live = url.ok
    ? `<span class="dl-chip dl-ok">URL 200 · ${dlBytes(url.bytes)}</span>`
    : `<span class="dl-chip dl-bad">URL ${dlEsc(url.status || url.error || "?")}</span>`;
  const card = row.card
    ? `<a href="#" class="dl-card" data-card="${dlEsc(row.card.id)}"
         >filed · ${dlEsc(row.card.status || "?")} ${dlEsc(row.card.id)}</a>`
    : "";
  return `<div class="dl-row" data-item="${dlEsc(row.id)}" data-entry="${dlEsc(e.id)}"
       data-task="${dlEsc(row.card ? row.card.id : "")}">
    <div class="dl-head">
      <span class="dl-name">${dlEsc(e.name)}</span>
      <span class="dl-chip ${st.cls}">${st.label}</span>
      ${live}${card}
    </div>
    <div class="dl-grid">
      <span class="dl-k">url</span><span class="dl-v dl-url">${dlEsc(e.url)}</span>
      <span class="dl-k">dest</span><span class="dl-v">${dlEsc(e.dest)}
        ${e.dest_state && e.dest_state.present ? `· ${dlBytes(e.dest_state.bytes)} there` : ""}</span>
      <span class="dl-k">wire-in</span><span class="dl-v dl-wire">${dlEsc(e.wire_in || "—")}</span>
    </div>
    <div class="dl-actions">
      <button class="btn dl-schedule" ${row.card ? "disabled" : ""}
        title="File this as a card and park it in the scheduled lane — on the board, but not dispatchable, so it never takes the single model seat">${row.card ? "card filed" : "Schedule card"}</button>
      <button class="btn dl-run" ${e.state === "complete" ? "disabled" : ""}
        title="Start wget -c on snarf, detached. Resumes a partial file rather than restarting it.">${e.state === "partial" ? "Resume" : "Download now"}</button>
      <button class="btn dl-progress">Progress</button>
      <span class="dl-status"></span>
    </div>
    <pre class="dl-tail" hidden></pre>
  </div>`;
}

async function dlLoad(panel, {quiet = false} = {}){
  const list = panel.querySelector(".dl-list");
  if(!quiet) list.innerHTML = `<div class="dl-note">reading TODO.md and checking every URL…</div>`;
  try{
    const r = await fetch("/api/darkhelix/downloads");
    const j = await r.json();
    if(j.error){
      list.innerHTML = `<div class="dl-note err">${dlEsc(j.error)}</div>`;
      return;
    }
    const rows = j.items || [];
    const withUrl = rows.filter(x => x.entry).length;
    panel.querySelector(".dl-count").textContent =
      `${rows.length} blocked · ${withUrl} with a verified URL`;
    list.innerHTML = rows.length
      ? rows.map(dlRow).join("")
      : `<div class="dl-note">Nothing in TODO.md is blocked on a database or a binary.</div>`;
  }catch(err){
    list.innerHTML = `<div class="dl-note err">${dlEsc(err.message)}</div>`;
  }
}

async function dlAct(row, panel, kind){
  const status = row.querySelector(".dl-status");
  const body = kind === "schedule"
    ? {item_id: row.dataset.item}
    : {entry_id: row.dataset.entry, task_id: row.dataset.task || undefined};
  status.innerHTML = kind === "schedule" ? "filing…" : "starting…";
  try{
    const r = await fetch(`/api/darkhelix/downloads/${kind}`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if(!j.ok){ status.innerHTML = `<span class="err">${dlEsc(j.error || "failed")}</span>`; return; }
    if(kind === "schedule"){
      status.innerHTML = `<span class="ok">filed ${dlEsc(j.task_id)}${
        j.scheduled ? " and parked in scheduled" : " — but parking failed, check the board"}</span>`;
      dlLoad(panel, {quiet: true});
    }else{
      status.innerHTML = j.state === "running"
        ? `<span class="warn">already running on snarf</span>`
        : `<span class="ok">started on snarf — log ${dlEsc(j.log)}</span>`;
    }
  }catch(err){
    status.innerHTML = `<span class="err">${dlEsc(err.message)}</span>`;
  }
}

async function dlProgress(row){
  const pre = row.querySelector(".dl-tail");
  const status = row.querySelector(".dl-status");
  pre.hidden = false;
  pre.textContent = "reading…";
  try{
    const r = await fetch(`/api/darkhelix/downloads/progress?entry_id=${encodeURIComponent(row.dataset.entry)}`);
    const j = await r.json();
    if(!j.ok){ pre.textContent = j.error || "unavailable"; return; }
    status.innerHTML = j.state === "running"
      ? `<span class="ok">running</span>`
      : `<span class="warn">not running</span>`;
    const there = j.dest && j.dest.present ? `on disk: ${dlBytes(j.dest.bytes)}\n` : "";
    pre.textContent = there + (j.tail || "(no log yet)");
  }catch(err){ pre.textContent = err.message; }
}

function openDownloads(){
  openWorkTabTurning("downloads", "main", "DOWNLOADS", (panel) => {
    panel.classList.add("dl-pane");
    panel.innerHTML = `
      <div class="dl-bar">
        <span class="dl-bar-title">DOWNLOADS</span>
        <span class="dl-bar-sub">TODO.md items blocked on a database or binary — filed as scheduled cards, pulled off the board</span>
        <span class="dl-count"></span>
        <span class="dl-spacer"></span>
        <button class="btn dl-reload">⟲ recheck</button>
      </div>
      <div class="dl-list"></div>`;
    panel.querySelector(".dl-reload").onclick = () => dlLoad(panel);
    panel.querySelector(".dl-list").addEventListener("click", (e) => {
      const row = e.target.closest(".dl-row");
      if(!row) return;
      const card = e.target.closest(".dl-card");
      if(card){
        e.preventDefault();
        if(typeof openTaskLog === "function") openTaskLog(card.dataset.card);
        return;
      }
      if(e.target.closest(".dl-schedule")) return dlAct(row, panel, "schedule");
      if(e.target.closest(".dl-run")) return dlAct(row, panel, "run");
      if(e.target.closest(".dl-progress")) return dlProgress(row);
    });
    dlLoad(panel);
  });
}

document.querySelectorAll('[data-action="downloads"]').forEach(b =>
  b.addEventListener("click", openDownloads));
