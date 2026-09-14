// Fairbill page. Talks to src/fairbill/app.py. No framework, no CDN.
const $ = (id) => document.getElementById(id);
const money = (n) => (n === null || n === undefined) ? "—" : "$" + Number(n).toFixed(2);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const SESSION = (() => {
  let s = null;
  try { s = localStorage.getItem("fairbill_session"); } catch (e) { /* private mode */ }
  if (!s) {
    s = (crypto.randomUUID ? crypto.randomUUID() : "s" + Date.now() + Math.random().toString(16).slice(2));
    try { localStorage.setItem("fairbill_session", s); } catch (e) { /* ignore */ }
  }
  return s;
})();
const H = { "X-Fairbill-Session": SESSION, "Content-Type": "application/json" };

const S = { billId: null, bill: null, result: null, card: null, crops: null };
const SCENES = ["Gallery", "Read", "Fetch", "Audit", "Decision", "Output", "Ledger", "Guard", "Measured"];

// ------------------------------------------------------------- scene rail --
const io = new IntersectionObserver((entries) => {
  entries.forEach((e) => {
    if (!e.isIntersecting) return;
    const n = Number(e.target.id.slice(1));
    $("scenepos").innerHTML = n + " / 9 &nbsp;" + SCENES[n - 1];
  });
}, { rootMargin: "-45% 0px -45% 0px" });
document.querySelectorAll("section.scene").forEach((s) => io.observe(s));

function show(id) { const el = $(id); if (el.hidden) { el.hidden = false; el.classList.add("reveal"); } }

// --------------------------------------------------------- stage tracker ---
// Every stage says three things from its first millisecond: what it is doing in plain
// words, how long it has been doing it, and how long that usually takes. Nothing is silent.
// expected seconds: payload.expected_s wins when the core sends it; this table is the fallback.
// exp = median seconds from the live bench of 2026-09-14 (_runs/2026-09-14_speed/TIMINGS.md);
// short = the header line, which has room for about three words next to the counter.
const STAGE_META = {
  read:   { name: "Reading your statement", short: "Reading", exp: 16, scene: "s2", el: "stRead" },
  fetch:  { name: "Downloading the hospital's price file", short: "Price file", exp: 8, scene: "s3", el: "stFetch",
            busy: "still downloading, the hospital's server is slow today" },
  audit:  { name: "Checking each line against the file", short: "Checking lines", exp: 14, scene: "s4", el: "stAudit" },
  card:   { name: "Writing the Decision Card", short: "Decision Card", exp: 1, scene: "s5", el: "stCard" },
  letter: { name: "Drafting the dispute letter", short: "Letter", exp: 10, scene: "s6", el: "stLetter" },
  chat:   { name: "Asking Fairbill", short: "Answering", exp: 5, scene: "s8", el: "stChat" },
};
// A stage is "slow" past twice its expected time, never sooner than 5 s (the card lands in
// milliseconds, and a hospital download has no useful median).
const SLOW_FLOOR_S = 5;
const isSlow = (s, secs) => !s.done && !s.cached && secs > Math.max(2 * s.exp, SLOW_FLOOR_S);
const ST = { live: {}, order: [], timer: null };
const now = () => (window.performance && performance.now) ? performance.now() : Date.now();

function startStage(step, opts) {
  const m = STAGE_META[step];
  if (!m) return;
  opts = opts || {};
  const prev = ST.live[step];
  const s = (prev && !prev.done) ? prev : { t0: now() };
  if (opts.restart) s.t0 = now();
  s.done = false;
  s.final = null;
  s.label = null;
  s.exp = Number(opts.expected_s) > 0 ? Number(opts.expected_s) : m.exp;
  s.name = opts.name || s.name || m.name;
  s.short = opts.short || m.short;
  s.usually = opts.usually || null;
  s.cached = !!opts.cached;
  ST.live[step] = s;
  if (ST.order[ST.order.length - 1] !== step) ST.order.push(step);
  show(m.scene);
  paintStage(step);
  if (!ST.timer) ST.timer = setInterval(tickStages, 250);
}

function renameStage(step, name) {
  const s = ST.live[step];
  if (!s) return;
  s.name = name;
  paintStage(step);
}

// The landed number is what this page counted, so the counter never runs backwards; the
// core's own per-stage seconds stay in the payload. opts.final overrides for a stage that
// did its work before its counter started (the overlapped price-file download).
function landStage(step, seconds, label, opts) {
  const s = ST.live[step];
  if (!s) return;
  opts = opts || {};
  s.done = true;
  s.final = (typeof opts.final === "number") ? opts.final : (now() - s.t0) / 1000;
  s.label = label || "done in";
  s.tail = opts.tail || "";
  paintStage(step);
  if (!Object.keys(ST.live).some((k) => !ST.live[k].done)) {
    clearInterval(ST.timer); ST.timer = null;
  }
  paintHeaderStage();
}

function stageElapsed(step) {
  const s = ST.live[step];
  if (!s) return 0;
  return s.done ? s.final : (now() - s.t0) / 1000;
}

function paintStage(step) {
  const m = STAGE_META[step], s = ST.live[step];
  if (!m || !s) return;
  const el = $(m.el);
  if (!el) return;
  const secs = stageElapsed(step);
  const slow = isSlow(s, secs);
  let tail = s.done ? (s.tail || "") : "";
  if (!s.done) {
    if (s.cached) tail = "replaying a saved run";
    else if (slow) tail = m.busy || "still working, Bedrock is busy";
    else tail = s.usually || m.usually || ("usually about " + Math.round(s.exp) + " s");
  }
  // a stage that landed inside a tenth of a second (the card is plain code) says so
  const clock = s.done
    ? (s.label === "skipped" ? "skipped" : secs < 0.1 ? "ready" : s.label + " " + secs.toFixed(1) + " s")
    : secs.toFixed(1) + " s";
  el.hidden = false;
  el.className = "stage" + (s.done ? " done" : " live") + (slow ? " slow" : "");
  el.innerHTML = '<span class="sname">' + esc(s.name) + "</span>" +
    '<span class="sclock">' + esc(clock) + "</span>" +
    (tail ? '<span class="sexp">' + esc(tail) + "</span>" : "");
  paintHeaderStage();
}

function tickStages() {
  Object.keys(ST.live).forEach((k) => { if (!ST.live[k].done) paintStage(k); });
}

// One line in the header, so the counter is readable even when the scene is scrolled away.
function paintHeaderStage() {
  const h = $("hstage");
  if (!h) return;
  let active = null;
  for (let i = ST.order.length - 1; i >= 0; i--) {
    const s = ST.live[ST.order[i]];
    if (s && !s.done) { active = s; break; }
  }
  if (!active) { h.hidden = true; $("scenepos").hidden = false; return; }
  $("scenepos").hidden = true;
  h.hidden = false;
  h.className = "hstage" + (isSlow(active, (now() - active.t0) / 1000) ? " slow" : "");
  h.innerHTML = '<span class="hname">' + esc(active.short || active.name) + "</span>" +
    '<span class="hclock">' + ((now() - active.t0) / 1000).toFixed(1) + " s</span>";
}

function resetStages() {
  Object.keys(ST.live).forEach((k) => { delete ST.live[k]; });
  ST.order = [];
  if (ST.timer) { clearInterval(ST.timer); ST.timer = null; }
  Object.keys(STAGE_META).forEach((k) => {
    const el = $(STAGE_META[k].el);
    if (el) { el.hidden = true; el.innerHTML = ""; el.className = "stage"; }
  });
  document.querySelectorAll(".dropped").forEach((n) => n.remove());
  paintHeaderStage();
}

// -------------------------------------------------------------- skeletons --
// Grey placeholders in the shape of the answer, so no stage ever shows a blank screen.
const skRows = (n) => Array.from({ length: n }, () =>
  '<div class="row skel"><span class="sk w3"></span><span class="sk"></span><span class="sk w4"></span></div>').join("");
const skLines = (n) => Array.from({ length: n }, (_, i) =>
  '<span class="sk line' + (i % 3 === 2 ? " w7" : "") + '"></span>').join("");

function skeletonRead() {
  $("fields").innerHTML = Array.from({ length: 8 }, () =>
    '<div><dt><span class="sk w5"></span></dt><dd><span class="sk"></span></dd></div>').join("");
  $("readRows").innerHTML = skRows(5);
}
function skeletonAudit() {
  $("auditBanner").innerHTML = '<p class="banner skel"><span class="sk"></span><span class="sk w7"></span></p>';
  $("auditRows").innerHTML = skRows(5);
}
function skeletonCard() {
  $("stakeWrap").innerHTML = '<span class="sk stake"></span>';
  $("situation").innerHTML = skLines(3);
  $("options").innerHTML = ('<div class="opt skel"><span class="sk w5"></span><span class="sk line w7"></span></div>').repeat(3);
  $("deadline").innerHTML = '<span class="sk w5"></span><span class="sk line w7"></span>';
}
function skeletonLetter() {
  $("letter").innerHTML = skLines(9);
}

// ---------------------------------------------------------------- gallery --
async function loadGallery() {
  const bills = await (await fetch("/api/bills")).json();
  $("gallery").innerHTML = bills.map((b) => `
    <button class="tile" data-id="${b.id}">
      <img src="${b.photo_url}" alt="Statement from ${esc(b.hospital_name)}" loading="lazy">
      <span class="meta">
        <b>${esc(b.hospital_name)}</b>
        <span>${esc(b.visit_type)}</span>
        <span>${esc(b.service_date || "")}</span>
      </span>
    </button>`).join("");
  document.querySelectorAll(".tile").forEach((t) =>
    t.addEventListener("click", () => run(t.dataset.id)));
}

// ------------------------------------------------------------- SSE runner --
let RUN_ABORT = null;  // the in-flight stream; a new tap ends it before starting its own
async function run(billId) {
  if (RUN_ABORT) { try { RUN_ABORT.abort(); } catch (e) { /* already closed */ } }
  RUN_ABORT = new AbortController();
  const signal = RUN_ABORT.signal;
  S.billId = billId;
  S.cachedShown = false;
  S.readPartial = [];
  S.specialists = [];
  ["s2", "s3", "s4", "s5", "s6", "s7", "s8"].forEach((i) => { $(i).hidden = true; });
  resetStages();
  $("readNote").textContent = "";
  $("auditChips").hidden = true;
  $("auditChips").innerHTML = "";
  $("fetchStats").textContent = "";
  $("fetchNote").textContent = "";
  skeletonRead();
  skeletonAudit();
  $("fetchBar").style.width = "0";
  show("s2");
  // the counter starts at the tap: the first seconds are the Runtime starting, named as such
  startStage("read", { name: "Connecting to Fairbill", short: "Connecting", expected_s: 5 });
  $("pageimg").src = `/gallery/${billId}_photo.jpg`;
  $("s2").scrollIntoView();
  fetch(`/api/bills/${billId}/truth-crops`).then((r) => r.json()).then((c) => { S.crops = c; });

  try {
    const q = new URLSearchParams(location.search).get("cached") === "1" ? "?cached=1" : "";
    const res = await fetch(`/api/run/${billId}${q}`, { method: "POST", headers: H, signal });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        const line = part.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        try { onEvent(JSON.parse(line.slice(5).trim())); } catch (e) { console.warn("bad event", e); }
      }
    }
  } catch (e) {
    if (signal.aborted) return;  // replaced by a newer run; that run owns the page now
    console.warn("stream ended", e);
  }
  // a dropped connection on cellular ends the stream with no terminal event: stop the
  // counters rather than climb forever claiming Bedrock is busy
  endOrphanStages();
}

function endOrphanStages() {
  const stuck = Object.keys(ST.live).filter((k) => !ST.live[k].done);
  if (!stuck.length) return;
  stuck.forEach((k) => {
    landStage(k, null, "stopped after");
    const el = $(STAGE_META[k].el);
    if (el) el.classList.add("slow");
  });
  const el = $(STAGE_META[stuck[0]].el);
  if (el) el.insertAdjacentHTML("afterend",
    '<p class="note dropped">The connection to Fairbill ended before this step finished. ' +
    'Pick the statement again to rerun it.</p>');
}

// The core (A8) yields {step, status, payload, seconds}. Payloads may arrive bare or wrapped.
const pick = (p, ...keys) => {
  if (!p || typeof p !== "object") return p;
  for (const k of keys) if (p[k] && typeof p[k] === "object") return p[k];
  return p;
};

// what the card and the audit banner may claim when nothing was checked
const NOT_CHECKED = {
  unsupported_hospital: "Fairbill could not check this bill: this hospital's published price " +
    "file is not in the gallery yet. You can still ask for an itemized bill.",
  incomplete: "Fairbill could not finish checking this bill against the hospital's price file, " +
    "so no verdict is given. Run it again, or ask for an itemized bill.",
};

// the hospital's file size, in the words the owner asked for, from what is known so far
function fetchStageName(totalBytes) {
  const base = "Downloading the hospital's price file";
  if (totalBytes) return base + ", " + (totalBytes / 1e6).toFixed(0) + " MB";
  const h = (S.bill?.hospital_name || "").toLowerCase();
  if (h.includes("norman")) return base + ", 39 MB";
  if (h.includes("ou health") || h.includes("oklahoma university") || h.includes("oumc")) {
    return base + ", the first 8 MB of 1.5 GB";
  }
  return base;
}

function onEvent(ev) {
  const cached = !!ev.cached;
  if (cached && !S.cachedShown) {
    S.cachedShown = true;
    $("readNote").textContent = "replaying a precomputed run (?cached=1)";
  }

  if (ev.status === "skipped" && ev.step === "fetch") {
    show("s3");
    landStage("fetch", ev.seconds, "skipped");
    $("fetchBadge").textContent = "skipped";
    $("fetchBadge").className = "badge fixture";
    $("fetchStats").textContent = "no price file to fetch";
    $("fetchNote").textContent = (ev.payload || {}).reason || "";
    return;
  }

  if (ev.status === "start") {
    const exp = (ev.payload || {}).expected_s;
    if (ev.step === "read") {
      skeletonRead();
      startStage("read", { cached, expected_s: exp, name: STAGE_META.read.name, short: STAGE_META.read.short, restart: true });
    }
    if (ev.step === "fetch") {
      show("s3");
      const p = ev.payload || {};
      const age = Number(p.running_for_s || 0);
      // exp is what is left of the download: 0 when it already landed under the read
      const usually = p.already_done ? "finished while the statement was being read"
        : (age > 0 ? "started " + Math.round(age) + " s ago under the read, usually about " + Math.round(exp) + " s more"
                   : undefined);
      startStage("fetch", { cached, expected_s: exp, name: fetchStageName(p.total_bytes), usually });
      const total = Number(p.total_bytes || 0), got = Number(p.bytes || 0);
      $("fetchBar").style.width = total > 0 ? Math.min(100, got / total * 100).toFixed(1) + "%" : "18%";
      $("fetchUrl").innerHTML = `<b>${esc(S.bill?.hospital_name || "")}</b><span class="sk w7"></span>`;
      $("fetchStats").innerHTML = '<span class="lbl">' + (got > 0 ? "downloading the hospital's file" : "contacting the hospital's server") +
        '</span><span class="sk w5"></span>';
      $("fetchBadge").textContent = "downloading";
      $("fetchBadge").className = "badge fixture";
      $("fetchNote").textContent = "";
    }
    if (ev.step === "audit") { skeletonAudit(); startStage("audit", { cached, expected_s: exp }); paintSpecialists(true); }
    if (ev.step === "card") { skeletonCard(); startStage("card", { cached, expected_s: exp }); }
    return;
  }

  // Optional streaming. The core does not send these yet; when it does they render at once,
  // and when it never does nothing is drawn and nothing breaks.
  if (ev.status === "progress") { onProgress(ev); return; }

  if (ev.status === "error") {
    const where = { read: "s2", fetch: "s3", audit: "s4", card: "s5" }[ev.step] || "s4";
    show(where);
    landStage(ev.step, ev.seconds, "stopped after");
    $(where).insertAdjacentHTML("beforeend",
      `<p class="banner" style="color:var(--flag)">${esc(ev.step)} failed: ${esc((ev.payload || {}).error || "unknown error")}</p>`);
    return;
  }
  if (ev.status === "ok" && ev.step === "fetch") {
    const f = pick(ev.payload, "report", "fetch") || {};
    const secs = Number(f.fetch_seconds || f.seconds || 0);
    if (f.overlapped) {
      landStage("fetch", null, "downloaded in", { final: secs,
        tail: "ran while the statement was being read, so there was nothing to wait for" });
    } else if (f.fetch_seconds) {
      landStage("fetch", null, "waited", {
        tail: "the download started under the read and took " + secs.toFixed(1) + " s in all; the hospital's server sets the pace" });
    } else landStage("fetch", null);
  } else if (ev.status === "ok" && STAGE_META[ev.step]) landStage(ev.step, ev.seconds);
  if (ev.step === "read") { S.bill = pick(ev.payload, "bill"); renderRead(ev); }
  if (ev.step === "fetch") renderFetch(pick(ev.payload, "report", "fetch"));
  if (ev.step === "audit") { S.result = pick(ev.payload, "result", "audit"); renderAudit(); }
  if (ev.step === "card") { S.card = pick(ev.payload, "card"); renderCard(); }
  if (ev.step === "done") { show("s7"); show("s8"); loadLedger(); }
}

// -------------------------------------------------------- live detail ------
function onProgress(ev) {
  const p = ev.payload || {};
  if (ev.step === "fetch") {
    const done = Number(p.bytes);
    const total = Number(p.total_bytes);
    if (!(done >= 0)) return;
    show("s3");
    if (total > 0) {
      $("fetchBar").style.width = Math.min(100, done / total * 100).toFixed(1) + "%";
      renameStage("fetch", fetchStageName(total));
    }
    const secs = Math.max(0.001, stageElapsed("fetch"));
    const mb = (n) => (n / 1e6).toFixed(1) + " MB";
    $("fetchStats").innerHTML = "<b>" + mb(done) + "</b>" + (total > 0 ? " of <b>" + mb(total) + "</b>" : "") +
      ", " + (done / 1e6 / secs).toFixed(1) + " MB per second";
    return;
  }
  if (ev.step === "read" && Array.isArray(p.lines) && p.lines.length) {
    S.readPartial = (S.readPartial || []).concat(p.lines);
    show("s2");
    $("readRows").innerHTML = S.readPartial.map((l) => `
      <div class="row">
        <span class="code">${esc(l.code || "—")}</span>
        <span class="desc">${esc(l.description || "")}</span>
        <span class="amt">${money(l.charge)}</span>
      </div>`).join("") + skRows(Math.max(1, 5 - S.readPartial.length));
    return;
  }
  if (ev.step === "audit" && p.specialist) {
    const sp = p.specialist;
    const name = typeof sp === "string" ? sp : sp.name;
    const state = (typeof sp === "string" ? p.status : sp.status) || "working";
    if (!name) return;
    S.specialists = S.specialists || [];
    const at = S.specialists.find((x) => x.name === name);
    if (at) at.state = state; else S.specialists.push({ name: name, state: state });
    paintSpecialists(false);
  }
}

// The three Graph specialists, in the patient's words. They start together, so all three
// chips show "working" the moment the audit starts and flip to "done" one by one.
const SPECIALISTS = [
  ["line_matcher", "Price match"], ["coding_specialist", "Coding check"], ["rights_specialist", "Your rights"],
];
function paintSpecialists(fresh) {
  if (fresh) S.specialists = SPECIALISTS.map(([id]) => ({ name: id, state: "working" }));
  const label = (id) => (SPECIALISTS.find(([k]) => k === id) || [id, id.replace(/_/g, " ")])[1];
  show("s4");
  const chips = $("auditChips");
  chips.hidden = false;
  chips.innerHTML = (S.specialists || []).map((x) => {
    const done = x.state === "done" || x.state === "ok";
    return `<span class="chip ${done ? "done" : "working"}">${esc(label(x.name))}<b>${done ? "done" : "working"}</b></span>`;
  }).join("");
}

// ------------------------------------------------------------------ read ---
const FIELDS = [
  ["hospital_name", "Hospital"], ["patient_name", "Patient"], ["account_number", "Account"],
  ["statement_date", "Statement date"], ["service_date_start", "Service date"], ["payer", "Payer"],
];

function renderRead(ev) {
  const b = S.bill || {};
  $("readTitle").textContent = b.hospital_name || "Reading the statement";
  $("fields").innerHTML = FIELDS.map(([k, label]) =>
    `<div><dt>${label}</dt><dd>${esc(b[k] ?? "—")}</dd></div>`).join("") +
    `<div><dt>Total charges</dt><dd>${money(b.total_charges)}</dd></div>` +
    `<div><dt>Balance due</dt><dd>${money(b.patient_balance)}</dd></div>`;
  $("readRows").innerHTML = (b.lines || []).map((l) => `
    <div class="row" data-line="${l.line_no}">
      <span class="code">${esc(l.code || "—")}</span>
      <span class="desc">${esc(l.description)}</span>
      <span class="amt">${money(l.charge)}</span>
    </div>`).join("");
  $("readRows").querySelectorAll(".row").forEach((r) =>
    r.addEventListener("click", () => highlight(Number(r.dataset.line), r)));
  // the seconds now live in the stage line above; this note only says what kind of run this is
  if (ev.cached) $("readNote").textContent = "replaying a precomputed run (?cached=1)";
  else if (ev.mock) $("readNote").textContent = "mock data, no model call";
  else $("readNote").textContent = "";
}

function highlight(lineNo, rowEl) {
  document.querySelectorAll("#readRows .row").forEach((r) => r.classList.remove("sel"));
  rowEl.classList.add("sel");
  const img = $("pageimg");
  const crop = (S.crops?.lines || []).find((l) => l.line_no === lineNo)?.crop;
  if (!crop) return;
  const draw = () => {
    $("pageinner").querySelectorAll(".box").forEach((b) => b.remove());
    const w = img.naturalWidth, h = img.naturalHeight;
    if (!w) return;
    const box = document.createElement("div");
    box.className = "box";
    box.style.left = (crop.x0 / w * 100) + "%";
    box.style.top = (crop.y0 / h * 100) + "%";
    box.style.width = ((crop.x1 - crop.x0) / w * 100) + "%";
    box.style.height = ((crop.y1 - crop.y0) / h * 100) + "%";
    $("pageinner").appendChild(box);
    $("pagewrap").scrollTop = Math.max(0, crop.y0 / h * img.clientHeight - 90);
  };
  if (img.dataset.mode !== "page") {
    img.dataset.mode = "page";
    img.addEventListener("load", draw, { once: true });
    img.src = `/gallery/${S.billId}_page.png`;
  } else { draw(); }
}

// ----------------------------------------------------------------- fetch ---
function renderFetch(f) {
  show("s3");
  f = f || {};
  $("fetchTitle").textContent = "Downloading the published price file";
  $("fetchUrl").innerHTML = `<b>${esc(S.bill?.hospital_name || "")}</b><br>` +
    `<a href="${esc(f.url || "#")}" target="_blank" rel="noopener">${esc(f.url || "")}</a>`;
  const pct = f.total_bytes ? Math.min(100, f.bytes / f.total_bytes * 100) : 100;
  requestAnimationFrame(() => { $("fetchBar").style.width = pct + "%"; });
  const kb = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.round(n / 1024) + " KB";
  $("fetchStats").innerHTML =
    `<b>${kb(f.bytes || 0)}</b>${f.total_bytes ? " of <b>" + kb(f.total_bytes) + "</b>" : ""}` +
    ` in <b>${Number(f.fetch_seconds || f.seconds || 0).toFixed(1)} s</b>` +
    (f.overlapped ? ", while the statement was being read" : (f.fetch_seconds ? ", started under the read" : "")) +
    `<br>sha256 <b>${esc((f.sha256 || "").slice(0, 16))}</b>`;
  const live = f.status === "live" || f.status === "live_partial";
  $("fetchBadge").textContent = f.status || "fixture";
  $("fetchBadge").className = "badge" + (live ? "" : " fixture");
  $("fetchNote").textContent = f.note || "";
}

// ----------------------------------------------------------------- audit ---
function renderAudit() {
  show("s4");
  const r = S.result || {}, lines = S.bill?.lines || [];
  const byLine = new Map();
  (r.findings || []).forEach((f) => (f.line_nos || []).forEach((n) => byLine.set(n, f)));
  const n = (r.findings || []).length;
  const st = r.status || "incomplete";
  $("auditBanner").innerHTML = st !== "audited"
    ? `<p class="banner">${esc(NOT_CHECKED[st])}</p>`
    : (r.clean && !n)
      ? `<p class="banner" style="border-color:var(--ok);background:var(--ok-soft);color:var(--ok)">
         Matches the hospital's file. Nothing to do.</p>`
      : `<p class="banner">${n} charge${n === 1 ? "" : "s"} on this statement ${n === 1 ? "does" : "do"}
         not match what ${esc(S.bill?.hospital_name || "the hospital")} publishes.</p>`;
  $("auditRows").innerHTML = auditRowsHtml(lines, byLine);
}

// One row per printed line number, in printed order. A payload that repeats a line
// (a re-delivered event, a reader that numbered two rows the same) draws it once.
function auditRowsHtml(lines, byLine) {
  const seen = new Set();
  return lines.filter((l) => !seen.has(l.line_no) && seen.add(l.line_no)).map((l) => {
    const f = byLine.get(l.line_no);
    return `<div class="row ${f ? "bad" : "ok"}">
      <span class="verdict">${f ? "FLAG" : "OK"}</span>
      <span class="desc"><span class="code">${esc(l.code || "—")}</span> ${esc(l.description)}</span>
      <span class="amt">${money(l.charge)}</span>
      ${f ? compare(l, f) : ""}
    </div>`;
  }).join("");
}

function compare(line, f) {
  const ev = (f.evidence || [])[0];
  const fileVal = ev ? (f.basis === "cash" ? ev.discounted_cash : ev.gross) : null;
  const billed = line.charge;
  const ratio = (fileVal && billed) ? Math.max(0, Math.min(1, fileVal / billed)) : 1;
  const diff = (fileVal !== null && fileVal !== undefined) ? billed - fileVal : null;
  return `<div class="compare">
    <div class="cmp">
      <div class="billed"><b>Bill says</b><span class="n">${money(billed)}</span></div>
      <div class="file"><b>Their file says</b><span class="n">${money(fileVal)}</span></div>
    </div>
    <div class="bar"><i style="width:${(100 - ratio * 100).toFixed(1)}%"></i></div>
    ${diff && diff > 0 ? `<div class="delta">+${money(diff).slice(1)} over the hospital's own
       ${f.basis === "cash" ? "cash" : "gross"} price</div>` : ""}
    <div class="srcrow">${esc(f.summary)}</div>
    ${ev ? `<div class="srcrow">File row: <code>${esc(ev.code)} — ${esc(ev.description)}</code>,
       line ${ev.source_line}, fetched ${esc(ev.fetched_on)} ·
       <a href="${esc(ev.source_url)}" target="_blank" rel="noopener">open the file</a></div>` : ""}
    ${f.regulation ? `<div class="srcrow">${esc(f.regulation)}</div>` : ""}
  </div>`;
}

// ------------------------------------------------------------------ card ---
function renderCard() {
  show("s5");
  const c = S.card || {};
  $("cardTitle").textContent = (c.status === "audited" && c.clean)
    ? "Nothing to dispute" : "What do you want to do?";
  $("stakeWrap").innerHTML = c.amount_at_stake
    ? `<div class="stake">${money(c.amount_at_stake)}</div>
       <p class="lede" style="margin-top:-4px">at stake on this statement</p>` : "";
  $("situation").textContent = c.situation || "";
  $("options").innerHTML = (c.options || []).map((o) => `
    <button class="opt ${o.id === c.default_option ? "def" : ""}" data-opt="${esc(o.id)}" type="button">
      ${o.id === c.default_option ? '<span class="tag">Recommended</span>' : ""}
      <b>${esc(o.label)}</b><span>${esc(o.consequence)}</span>
    </button>`).join("");
  $("options").querySelectorAll(".opt").forEach((b) =>
    b.addEventListener("click", () => decide(b.dataset.opt)));
  $("deadline").innerHTML = `<div class="d">${esc(c.deadline || "")}</div>
    <div class="note" style="margin-top:4px">${esc(c.deadline_reason || "")}</div>
    ${c.evidence_url ? `<div class="note"><a href="${esc(c.evidence_url)}" target="_blank"
       rel="noopener">The hospital's published price file</a>${
       (c.evidence_lines || []).length ? " · line " + c.evidence_lines.join(", ") : ""}</div>` : ""}`;
}

// ---------------------------------------------------------------- decide ---
async function decide(optionId) {
  show("s6");
  skeletonLetter();
  startStage("letter", {});
  $("s6").scrollIntoView();
  let out;
  try {
    out = await (await fetch(`/api/decide/${S.billId}`, {
      method: "POST", headers: H, body: JSON.stringify({ option_id: optionId }),
    })).json();
  } catch (e) {
    landStage("letter", null, "stopped after");
    $("letter").textContent = "The letter did not come back. Try the option again.";
    return;
  }
  landStage("letter", null);
  const L = out.letter;
  if (!L) {
    $("outTitle").textContent = "Logged, nothing sent";
    $("letter").textContent = "No letter for this option. The check is recorded in the ledger.";
  } else {
    $("outTitle").textContent = { itemized_request: "Your itemized-bill request",
      nsa_complaint: "Your No Surprises Act notice" }[L.kind] || "Your dispute letter";
    $("letter").textContent = [L.to_name, L.to_address, "", L.re_line, "", L.body, "",
      "Citations: " + (L.citations || []).join(" | "), "", L.footer].join("\n");
  }
  $("icsLink").href = out.ics_url || `/api/ics/${S.billId}`;
  $("outNote").textContent = out.ledger_entry
    ? `Ledger row ${out.ledger_entry.id} — ${out.ledger_entry.action}. Undo it in the ledger.` : "";
  show("s7"); show("s8");
  loadLedger();
}

$("copyBtn").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("letter").textContent); $("copyBtn").textContent = "Copied"; }
  catch (e) { $("copyBtn").textContent = "Select and copy"; }
  setTimeout(() => { $("copyBtn").textContent = "Copy the letter"; }, 1800);
});

// ---------------------------------------------------------------- ledger ---
async function loadLedger() {
  const out = await (await fetch("/api/ledger", { headers: H })).json();
  const rows = out.entries || [];
  $("ledgerCount").textContent = rows.length + " rows recorded this session.";
  $("ledgerRows").innerHTML = rows.length ? rows.map((r) => `
    <div class="lrow ${r.undone ? "undone" : ""}">
      <span>
        <span class="act">${esc(r.action)}</span>
        <span class="why">${esc(r.why || "")}</span>
        <span class="ts">${esc(String(r.ts).replace("T", " ").slice(0, 16))} UTC · ${esc(r.actor || "agent")}</span>
      </span>
      <button class="undo" data-entry="${esc(r.id)}" ${r.undo && !r.undone ? "" : "disabled"}>
        ${r.undone ? "undone" : (r.undo ? "Undo" : "final")}</button>
    </div>`).join("") : '<p class="spin">Nothing yet.</p>';
  $("ledgerRows").querySelectorAll(".undo:not([disabled])").forEach((b) =>
    b.addEventListener("click", async () => {
      await fetch(`/api/undo/${b.dataset.entry}`, { method: "POST", headers: H });
      loadLedger();
    }));
}

const sheet = $("sheet");
const openSheet = () => { sheet.classList.add("open"); sheet.setAttribute("aria-hidden", "false"); loadLedger(); };
const closeSheet = () => { sheet.classList.remove("open"); sheet.setAttribute("aria-hidden", "true"); };
$("openLedger").addEventListener("click", openSheet);
$("footLedger").addEventListener("click", openSheet);
$("closeLedger").addEventListener("click", closeSheet);

// ----------------------------------------------------------------- guard ---
$("chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("chatSend").disabled = true;
  $("chatOut").innerHTML = '<div class="reply skel">' + skLines(3) + "</div>";
  startStage("chat", {});
  let out;
  try {
    out = await (await fetch(`/api/chat/${S.billId || "bill_02"}`, {
      method: "POST", headers: H, body: JSON.stringify({ text: $("chatText").value }),
    })).json();
  } catch (err) {
    landStage("chat", null, "stopped after");
    $("chatOut").innerHTML = '<p class="note">No answer came back. Send it again.</p>';
    $("chatSend").disabled = false;
    return;
  }
  landStage("chat", null);
  // the model answers in markdown; only **bold** is honoured, everything else stays literal
  const md = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  const red = typeof out.redactions === "number"
    ? `<p class="note">${out.redactions} value${out.redactions === 1 ? "" : "s"} redacted from the
       tool trace (patient name, account number, card-length digits).</p>` : "";
  $("chatOut").innerHTML = `<div class="reply">${md(out.reply || "")}</div>` + red +
    (out.tool_calls || []).map((t) => `
      <div class="trace">
        <div class="head"><span class="tool">${esc(t.name)}()</span>
          <span class="status">${esc((t.status || "").toUpperCase())}</span></div>
        <div class="reason">${esc(t.reason || "")}</div>
      </div>`).join("");
  $("chatSend").disabled = false;
  loadLedger();
});

// ----------------------------------------------------------------- bench ---
async function loadBench() {
  const b = await (await fetch("/api/bench")).json();
  $("bench").innerHTML = `
    <div><div class="n">${b.reader.exact}/${b.reader.total}</div><div class="l">bills read exact</div></div>
    <div><div class="n">${b.audit.matched}/${b.audit.planted}</div><div class="l">planted errors found</div></div>
    <div><div class="n">${b.audit.false_flags}</div><div class="l">false flags</div></div>`;
  $("sources").innerHTML = "Prices come from these two published files: " +
    '<a href="https://www.normanregional.com/documents/PFS/736048282_norman-regional-health-system_standardcharges.csv" target="_blank" rel="noopener">Norman Regional</a> · ' +
    '<a href="https://www.ouhealth.com/patients-visitors/billing-insurance/price-transparency/" target="_blank" rel="noopener">OU Health</a> (both fetched 2026-09-13).';
  $("benchNote").textContent =
    `Reader ${b.reader.model}, ${b.reader.seconds} s per bill on average (${b.reader.generated}). ` +
    `Audit bench ${b.audit.generated}. Mode: ${b.mode}.`;
}

loadGallery();
loadBench();
