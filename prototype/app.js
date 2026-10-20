// fugazy-delta clickable prototype. Plain JS, no build step.
// Data comes from prototype/data/<event>-<setup>.json, written by
// `python -m fugazy_delta.demo prototype/data`.
"use strict";

const EVENTS = {
  autocross: { name: "CVSCC Points Event 1", venue: "Chippewa Valley (demo course)" },
  track: { name: "Track day, intermediate group", venue: "Demo road course" },
};
const UNLOCK = {
  gopro: "Add a RaceBox to unlock full accuracy and line comparison",
  racebox: "Add your GoPro video to unlock the overlay",
};
const SOURCE = { gopro: "GoPro GPS, 18 Hz, about 2 m accuracy", racebox: "RaceBox, 25 Hz, sub-metre accuracy" };
const SUPPORT_ICON = { full: "✓", approximate: "≈", unavailable: "✕" };
const SVG = "http://www.w3.org/2000/svg";

const state = { event: "autocross", data: null, selected: null, droppedFiles: null };
const $ = (sel) => document.querySelector(sel);

// ---------- theme ----------
function storage(fn) { try { return fn(window.localStorage); } catch { return null; } }
function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}
applyTheme(storage((s) => s.getItem("theme")));
$("#theme-toggle").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  applyTheme(next);
  storage((s) => s.setItem("theme", next));
});

// ---------- screens ----------
function show(id) {
  for (const s of document.querySelectorAll(".screen")) s.hidden = s.id !== id;
  window.scrollTo(0, 0);
}

function setEvent(event) {
  state.event = event;
  $(".event-name").textContent = EVENTS[event].name;
  $(".event-venue").textContent = EVENTS[event].venue;
  for (const b of document.querySelectorAll("[data-event]")) b.setAttribute("aria-pressed", String(b.dataset.event === event));
  const url = new URL(location.href);
  url.searchParams.set("event", event);
  history.replaceState(null, "", url);
}

function restart(message) {
  $("#footer-source").textContent = "";
  state.droppedFiles = null;
  $("#drop-error").hidden = !message;
  if (message) $("#drop-error").textContent = message;
  show("upload");
}

// ---------- upload ----------
function classify(file) {
  const ext = file.name.split(".").pop().toLowerCase();
  if (["mp4", "lrv", "360"].includes(ext)) return "gopro";
  if (["vbo", "csv"].includes(ext)) return "racebox";
  return null;
}

function handleFiles(fileList) {
  const files = [...fileList];
  const kinds = files.map(classify);
  const bad = files.filter((_, i) => !kinds[i]).map((f) => f.name);
  const err = $("#drop-error");
  if (bad.length) {
    err.textContent = `${bad.join(", ")}: not a GoPro video or a RaceBox export. From RaceBox, export the session as VBO on racebox.pro.`;
    err.hidden = false;
    if (bad.length === files.length) return;
  } else {
    err.hidden = true;
  }
  const hasGoPro = kinds.includes("gopro");
  const hasRaceBox = kinds.includes("racebox");
  const scenario = hasGoPro && hasRaceBox ? "gopro-racebox" : hasGoPro ? "gopro" : "racebox";
  state.droppedFiles = files
    .filter((_, i) => kinds[i])
    .map((f) => ({ name: f.name, kind: classify(f) === "gopro" ? "GoPro video" : "RaceBox export", size: f.size }));
  start(scenario);
}

const dropzone = $("#dropzone");
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("over"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("over"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("over");
  if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
});
$("#file-input").addEventListener("change", (e) => { if (e.target.files.length) handleFiles(e.target.files); e.target.value = ""; });
for (const b of document.querySelectorAll("[data-scenario]")) b.addEventListener("click", () => { state.droppedFiles = null; start(b.dataset.scenario); });
for (const b of document.querySelectorAll("[data-event]")) b.addEventListener("click", () => setEvent(b.dataset.event));
for (const b of document.querySelectorAll("[data-action=restart]")) b.addEventListener("click", (e) => { e.preventDefault(); restart(); });
$("#delete-session").addEventListener("click", () => {
  if (confirm("Delete this session and its GPS data? This can't be undone.")) restart();
});

function formatSize(bytes) {
  if (bytes > 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  if (bytes > 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1e3))} kB`;
}

async function start(scenario) {
  const url = `data/${state.event}-${scenario}.json`;
  const loading = fetch(url).then((r) => {
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    return r.json();
  });

  const files = state.droppedFiles || null;
  const hasGoPro = scenario !== "racebox";
  const hasRaceBox = scenario !== "gopro";
  const list = $("#processing-files");
  list.replaceChildren();
  const steps = [
    "Identifying each file",
    hasGoPro && "Copying the GPS data out of the video (the video stays on this device)",
    hasGoPro && hasRaceBox && "Lining up GoPro and RaceBox on GPS time",
    state.event === "track" ? "Matching the track and splitting laps" : "Finding the start and finish, splitting runs",
    "Working out times, delta and optimal",
  ].filter(Boolean);
  const ol = $("#processing-steps");
  ol.replaceChildren(...steps.map((s) => Object.assign(document.createElement("li"), { textContent: s })));
  show("processing");

  let data;
  try {
    data = await loading;
  } catch (e) {
    restart(`Couldn't load the demo data (${e.message}). Serve the prototype over HTTP: flox services start prototype`);
    return;
  }
  const shown = files || data.files.map((f) => ({ ...f, size: f.kind === "GoPro video" ? 3.9e9 : 2.4e6 }));
  for (const f of shown) {
    const li = document.createElement("li");
    li.innerHTML = `<span></span><span class="kind"></span>`;
    li.firstChild.textContent = f.name;
    li.lastChild.textContent = `${f.kind} · ${formatSize(f.size)}`;
    list.append(li);
  }
  const items = [...ol.children];
  for (let i = 0; i < items.length; i++) {
    items[i].className = "active";
    await new Promise((r) => setTimeout(r, 450));
    items[i].className = "done";
  }
  await new Promise((r) => setTimeout(r, 250));
  renderResults(data);
}

// ---------- results ----------
function feature(data, key) { return data.features.find((f) => f.key === key); }
function approx(data) { return feature(data, "LAP_TIMES").support === "approximate"; }
// A time the lap finder could not measure (a missed sector line) is null.
function fmt(seconds, data) { return seconds == null ? "—" : seconds.toFixed(approx(data) ? 2 : 3); }
function signed(seconds, digits) { return (seconds > 0 ? "+" : seconds < 0 ? "−" : "±") + Math.abs(seconds).toFixed(digits); }
// Error sizes: two decimals, three below 0.1 s (matches fugazy_delta/accuracy.py).
function sec(x) { return Math.abs(x).toFixed(Math.abs(x) < 0.1 ? 3 : 2); }
function errorOf(data, key) { return data.accuracy.rows.find((r) => r.key === key); }
function within(data, key) { return `±${sec(errorOf(data, key).p95)}`; }
function cap(s) { return s[0].toUpperCase() + s.slice(1); }

function renderResults(data) {
  state.data = data;
  const unit = data.event.unit;
  const best = data.laps[data.best - 1];
  const counted = data.laps.filter((l) => l.counted);
  const others = counted.filter((l) => !l.best).sort((a, b) => a.time - b.time);
  state.selected = (others[0] || best).number;

  const form = new FormData($("#driver-form"));
  const name = (form.get("name") || "").trim() || "Isaac";
  const car = (form.get("car") || "").trim() || "42";

  $("#r-venue").textContent = data.event.venue;
  $("#r-event").textContent = data.event.name;
  $("#r-driver").textContent = `${name} · car #${car}`;
  $("#r-setup").textContent = `Setup: ${data.setup.label}`;
  $("#r-files").textContent = (state.droppedFiles || data.files).map((f) => f.name).join(", ");
  $("#footer-source").textContent = ` · positions from ${SOURCE[data.position_source]}`;

  const warn = $("#r-warnings");
  warn.replaceChildren(...data.warnings.map((w) => Object.assign(document.createElement("p"), { className: "warning", textContent: w })));

  // tiles
  const digits = approx(data) ? 2 : 3;
  $("#t-best-label").textContent = `Best ${unit}`;
  $("#t-best").textContent = fmt(best.time, data);
  const tag = approx(data) ? "approximate · " : "";
  const lean = (key) => {
    const e = errorOf(data, key);
    return e.biased ? `typically ${sec(e.bias)} s too ${e.bias < 0 ? "fast" : "slow"}, ` : "";
  };
  $("#t-best-sub").textContent = `${cap(unit)} ${best.number} · ${tag}${lean("best")}95% within ${within(data, "best")} s`;
  $("#t-optimal").textContent = fmt(data.optimal.time, data);
  $("#t-optimal-sub").textContent = data.optimal.time == null
    ? `No ${unit} timed every sector`
    : `${signed(data.optimal.time - best.time, digits)} s · ${tag}${lean("optimal")}95% within ${within(data, "optimal")} s`;
  $("#t-count-label").textContent = `${cap(unit)}s`;
  $("#t-count").textContent = String(counted.length);
  $("#t-count-sub").textContent = data.laps.length > counted.length ? `plus ${data.laps.length - counted.length} out lap` : `${(data.event.course_length_m / 1000).toFixed(2)} km course`;

  $("#table-title").textContent = `${cap(unit)}s`;
  const timesNote = $("#times-note");
  timesNote.hidden = !approx(data);
  timesNote.textContent = approx(data) ? `Times are approximate. ${feature(data, "LAP_TIMES").reason}` : "";
  renderTable(data);
  renderFeatures(data);
  renderAccuracy(data);
  renderVideoCard(data);
  renderComparison();
  show("results");
}

function renderTable(data) {
  const nSectors = data.laps[0].sectors.length;
  const best = data.laps[data.best - 1];
  const thead = $("#laps-table thead");
  // Each time column carries its error: 95% of such times are within ±x s.
  const pm = (key) => `<span class="pm">${within(data, key)}</span>`;
  const head = ["#", `Time${pm("lap")}`, ...Array.from({ length: nSectors }, (_, i) => `S${i + 1}${pm(`sector_${i + 1}`)}`), "Gap"];
  thead.innerHTML = `<tr>${head.map((h) => `<th scope="col">${h}</th>`).join("")}</tr>`;
  const tbody = $("#laps-table tbody");
  tbody.replaceChildren();
  const digits = approx(data) ? 2 : 3;
  for (const lap of data.laps) {
    const tr = document.createElement("tr");
    tr.dataset.lap = lap.number;
    tr.tabIndex = 0;
    tr.className = [lap.best && "best", lap.out_lap && "out"].filter(Boolean).join(" ");
    const tag = lap.best ? `<span class="tag">best</span>` : lap.out_lap ? `<span class="tag">out lap</span>` : "";
    const cells = [`<td>${lap.number}${tag}</td>`, `<td>${fmt(lap.time, data)}</td>`];
    lap.sectors.forEach((s, i) => {
      const isBest = data.optimal.sector_laps[i] === lap.number;
      const title = s == null ? ` title="Sector line missed; not timed"` : isBest ? ` title="Best sector ${i + 1}"` : "";
      cells.push(`<td class="${isBest ? "best-sector" : ""}"${title}>${s == null ? "—" : s.toFixed(digits)}</td>`);
    });
    cells.push(`<td>${lap.best ? "" : signed(lap.time - best.time, digits)}</td>`);
    tr.innerHTML = cells.join("");
    const pick = () => { state.selected = lap.number; renderComparison(); };
    tr.addEventListener("click", pick);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
    tbody.append(tr);
  }
}

function renderFeatures(data) {
  const ul = $("#features");
  ul.replaceChildren();
  for (const f of data.features) {
    const li = document.createElement("li");
    li.className = f.support;
    li.innerHTML = `<span class="icon" aria-hidden="true"></span><span><span class="name"></span><span class="status"></span></span><span class="reason"></span>`;
    li.querySelector(".icon").textContent = SUPPORT_ICON[f.support];
    li.querySelector(".name").textContent = f.label;
    li.querySelector(".status").textContent = cap(f.support);
    li.querySelector(".reason").textContent = f.reason || "Included with this setup.";
    ul.append(li);
  }
  const unlock = $("#unlock");
  const missing = data.features.filter((f) => f.support !== "full");
  unlock.hidden = !missing.length || !UNLOCK[data.setup.key];
  if (!unlock.hidden) {
    unlock.innerHTML = `<strong></strong><ul></ul>`;
    unlock.querySelector("strong").textContent = UNLOCK[data.setup.key];
    for (const f of missing) {
      const li = document.createElement("li");
      li.textContent = `${f.label}: ${f.support === "unavailable" ? "not available" : "approximate"} now, full with ${data.setup.key === "gopro" ? "a RaceBox" : "your GoPro video"}`;
      unlock.querySelector("ul").append(li);
    }
  }
}

function renderAccuracy(data) {
  const acc = data.accuracy;
  const unit = data.event.unit;
  $("#acc-intro").textContent = `Your times come from ${acc.detail}${acc.approximate ? ", so they are approximate" : ""}. ` +
    `Each row is how far that kind of time lands from the true time, measured over ${acc.seeds.toLocaleString("en")} simulated sessions.`;
  const tbody = $("#accuracy-table tbody");
  tbody.replaceChildren();
  for (const r of acc.rows) {
    const tr = document.createElement("tr");
    const bias = r.biased
      ? `${sec(r.bias)} ${r.bias < 0 ? "fast" : "slow"}`
      : "none";
    tr.innerHTML = `<th scope="row"></th><td></td><td></td><td></td><td></td>`;
    const [th, ...tds] = tr.children;
    th.textContent = r.label;
    tds[0].textContent = bias;
    tds[0].title = `Mean error ${signed(r.bias, 3)} s, 95% interval ${signed(r.bias_ci[0], 3)} to ${signed(r.bias_ci[1], 3)} s`;
    tds[1].textContent = `±${sec(r.p95)}`;
    tds[2].textContent = sec(r.rms);
    tds[3].textContent = r.n.toLocaleString("en");
    if (r.biased) tr.className = "biased";
    tbody.append(tr);
  }
  const notes = [];
  const opt = errorOf(data, "optimal");
  if (opt.biased) {
    notes.push(`The optimal ${unit} reads too fast: it takes the best time in each sector, and that also picks the sectors whose GPS error happened to flatter them. ` +
      `It is shown as measured, not corrected, because how much it flatters depends on how close your ${unit}s are.`);
  }
  if (acc.flagged_rate > 0) {
    notes.push(`About 1 ${unit} in ${Math.round(1 / acc.flagged_rate)} is flagged rather than timed: GPS error put the staged car past the start line.`);
  }
  notes.push("Measured on a simulated car and simulated receivers, not yet checked against real RaceBox or GoPro recordings.");
  $("#acc-notes").replaceChildren(...notes.map((n) => Object.assign(document.createElement("p"), { textContent: n })));
}

function renderVideoCard(data) {
  const card = $("#video-card");
  const overlay = feature(data, "VIDEO_OVERLAY");
  if (overlay.support === "unavailable") {
    card.innerHTML = `<h2>Overlay video</h2><p></p>`;
    card.querySelector("p").textContent = `No video in this upload. ${overlay.reason}`;
  } else {
    card.innerHTML = `<h2>Overlay video</h2>
      <p>Your times are ready without the video. When you're on wifi, send the video and get it back with speed, g-force, the map and your delta drawn on it.</p>
      <button type="button" class="button secondary" disabled>Send video later (not in this demo)</button>`;
  }
}

// ---------- comparison: table selection, delta chart, map ----------
function renderComparison() {
  const data = state.data;
  for (const tr of document.querySelectorAll("#laps-table tbody tr")) {
    tr.classList.toggle("selected", Number(tr.dataset.lap) === state.selected);
  }
  const unit = data.event.unit;
  const lap = data.laps[state.selected - 1];
  const best = data.laps[data.best - 1];
  $("#delta-caption").textContent = lap.best
    ? `Tap another ${unit} to compare`
    : `${cap(unit)} ${lap.number} against ${unit} ${best.number}`;
  const delta = feature(data, "DELTA");
  $("#delta-note").hidden = delta.support === "full";
  $("#delta-note").textContent = delta.support === "full" ? "" : `Approximate. ${delta.reason}`;
  drawDelta(data, lap);
  drawMap(data, lap);
}

function el(name, attrs = {}, parent) {
  const node = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.append(node);
  return node;
}

function niceStep(range, target) {
  const raw = range / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
}

function drawDelta(data, lap) {
  const box = $("#delta-chart");
  box.replaceChildren();
  // Drawn at the container's pixel width so text stays at its real size on a phone.
  const W = Math.max(280, Math.round(box.clientWidth || 600)), H = W < 420 ? 220 : 260;
  const m = { l: 44, r: 10, t: 22, b: 28 };
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Delta of ${data.event.unit} ${lap.number} to the best, by distance` }, box);
  const dist = data.distance;
  const ys = lap.delta_to_best;
  const L = dist[dist.length - 1];
  let lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  if (hi - lo < 0.1) { hi = Math.max(hi, 0.05); lo = Math.min(lo, -0.05); }
  const step = niceStep(hi - lo, 4);
  lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
  const x = (d) => m.l + (d / L) * (W - m.l - m.r);
  const y = (v) => m.t + ((hi - v) / (hi - lo)) * (H - m.t - m.b);

  for (let v = lo; v <= hi + 1e-9; v += step) {
    el("line", { class: Math.abs(v) < 1e-9 ? "zero" : "gridline", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }, svg);
    const t = el("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end" }, svg);
    t.textContent = `${v > 1e-9 ? "+" : ""}${v.toFixed(step < 0.1 ? 2 : 1)}`;
  }
  const marks = [0, ...data.course.sector_marks.map((s) => s.distance)];
  marks.forEach((d, i) => {
    if (i) el("line", { class: "sector-line", x1: x(d), x2: x(d), y1: m.t - 6, y2: H - m.b }, svg);
    const t = el("text", { x: x(d) + 4, y: m.t - 8 }, svg);
    t.textContent = `S${i + 1}`;
  });
  const kmStep = niceStep(L, 5);
  for (let d = 0; d <= L + 1e-9; d += kmStep) {
    const t = el("text", { x: x(d), y: H - 10, "text-anchor": d === 0 ? "start" : "middle" }, svg);
    t.textContent = d === 0 ? "0 m" : `${Math.round(d)}`;
  }
  const path = ys.map((v, i) => `${i ? "L" : "M"}${x(dist[i]).toFixed(1)},${y(v).toFixed(1)}`).join("");
  el("path", { class: "delta-line", d: path }, svg);
  const end = el("text", { x: W - m.r, y: y(ys[ys.length - 1]) - 6, "text-anchor": "end" }, svg);
  end.textContent = lap.best ? "your best" : `${signed(ys[ys.length - 1], 2)} s at the finish`;

  // hover layer: crosshair + tooltip, and the car's position on the map
  const cross = el("line", { class: "crosshair", y1: m.t, y2: H - m.b, visibility: "hidden" }, svg);
  const dot = el("circle", { class: "dot", r: 5, visibility: "hidden" }, svg);
  const hit = el("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" }, svg);
  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.hidden = true;
  box.append(tip);
  const move = (evt) => {
    const r = svg.getBoundingClientRect();
    const px = ((evt.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(dist.length - 1, Math.round(((px - m.l) / (W - m.l - m.r)) * (dist.length - 1))));
    cross.setAttribute("x1", x(dist[i])); cross.setAttribute("x2", x(dist[i]));
    dot.setAttribute("cx", x(dist[i])); dot.setAttribute("cy", y(ys[i]));
    cross.setAttribute("visibility", "visible"); dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.style.left = `${(x(dist[i]) / W) * r.width}px`;
    tip.style.top = `${(y(ys[i]) / H) * r.height}px`;
    const v = ys[i];
    tip.innerHTML = `${Math.round(dist[i])} m · <span class="val"></span>`;
    tip.querySelector(".val").textContent = lap.best ? "best" : v === 0 ? "level" : `${Math.abs(v).toFixed(2)} s ${v > 0 ? "slower" : "faster"}`;
    showCar(i);
  };
  const leave = () => {
    cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden");
    tip.hidden = true; showCar(null);
  };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerdown", move);
  hit.addEventListener("pointerleave", leave);
}

let showCar = () => {};

function drawMap(data, lap) {
  const box = $("#map");
  box.replaceChildren();
  const best = data.laps[data.best - 1];
  const line = feature(data, "LINE_COMPARISON");
  const compare = line.support !== "unavailable" && !lap.best;
  const traces = compare ? [best, lap] : [best];
  const note = $("#map-note");
  note.hidden = line.support !== "unavailable";
  note.textContent = line.support === "unavailable" ? `Racing line comparison unavailable. ${line.reason}` : "";

  const legend = $("#map-legend");
  legend.innerHTML = "";
  const item = (cls, text) => {
    const s = document.createElement("span");
    s.innerHTML = `<i style="background: var(--series-${cls})"></i>`;
    s.append(text);
    legend.append(s);
  };
  item("best", `${cap(data.event.unit)} ${best.number} (best)`);
  if (compare) item("other", `${cap(data.event.unit)} ${lap.number}`);

  const xs = traces.flatMap((t) => t.trace.x), ys = traces.flatMap((t) => t.trace.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const span = Math.max(maxX - minX, maxY - minY);
  const pad = span * 0.06;
  const labelRoom = span * 0.2; // marker labels sit to the right of their dot
  const vw = maxX - minX + 2 * pad + labelRoom, vh = maxY - minY + 2 * pad;
  // Fit inside the card's width and at most 380 px tall, keeping north up and metres square.
  const width = Math.min(box.clientWidth || 400, (380 * vw) / vh);
  const svg = el("svg", { viewBox: `${minX - pad} ${-maxY - pad} ${vw} ${vh}`, role: "img",
    "aria-label": "Course map", width: Math.round(width), height: Math.round((width * vh) / vw) }, box);
  const pts = (t) => t.trace.x.map((xv, i) => `${xv},${-t.trace.y[i]}`).join(" ");
  traces.forEach((t, i) => el("polyline", { class: `trace ${i ? "other" : "best"}`, points: pts(t) }, svg));
  const r = span / 70;
  for (const s of data.course.sector_marks) el("circle", { class: "sector-dot", cx: s.x, cy: -s.y, r }, svg);
  const label = (p, text, dy) => {
    const t = el("text", { x: p.x + r * 1.8, y: -p.y + dy, "font-size": r * 3.2 }, svg);
    t.textContent = text;
  };
  const st = data.course.start, fi = data.course.finish;
  el("rect", { class: "marker", x: st.x - r, y: -st.y - r, width: 2 * r, height: 2 * r, style: "fill: var(--good)" }, svg);
  if (data.event.closed) {
    label(st, "Start / finish", r);
  } else {
    el("rect", { class: "marker", x: fi.x - r, y: -fi.y - r, width: 2 * r, height: 2 * r, style: "fill: var(--ink)" }, svg);
    label(st, "Start", r);
    label(fi, "Finish", r);
  }
  data.course.sector_marks.forEach((s, i) => label(s, `S${i + 2}`, r));
  const car = el("circle", { class: "car", r: r * 1.3, visibility: "hidden" }, svg);
  const on = traces[traces.length - 1];
  showCar = (i) => {
    if (i === null) { car.setAttribute("visibility", "hidden"); return; }
    car.setAttribute("cx", on.trace.x[i]);
    car.setAttribute("cy", -on.trace.y[i]);
    car.setAttribute("visibility", "visible");
  };
}

// ---------- boot ----------
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (!$("#results").hidden) renderComparison(); }, 150);
});
setEvent(new URLSearchParams(location.search).get("event") === "track" ? "track" : "autocross");
