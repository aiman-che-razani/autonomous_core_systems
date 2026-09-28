"use strict";
let token = "", timer = null, busy = false, generation = 0, depths = [];
const el = id => document.getElementById(id);
const TONE = {FAILED:"bad",DEAD_LETTER:"bad",DEAD:"bad",QUEUED:"wait",RETRY_WAIT:"wait",SUSPECT:"wait",CANCELLED:"muted",DRAINING:"muted"};
const INSPECT_HINT = "Select a job to inspect its result and attempt history.";
function notice(text) { el("notice").textContent = text; }
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
async function api(path, options = {}) {
  if (!token) throw new Error("Connect with a client token first.");
  const response = await fetch(path, {...options, headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"}, signal: AbortSignal.timeout(10000)});
  const body = await response.text(); let data = null;
  try { data = body ? JSON.parse(body) : null; } catch { data = null; }
  if (!response.ok) throw new Error(!data || !data.detail ? `HTTP ${response.status}` : Array.isArray(data.detail) ? data.detail.map(d => [d.loc && d.loc.slice(1).join("."), d.msg].filter(Boolean).join(": ")).join("; ") : String(data.detail));
  return data;
}
function cell(row, value) { const td = document.createElement("td"); td.textContent = value; row.append(td); return td; }
function empty(body, columns, text) { const row = document.createElement("tr"), td = cell(row, text); td.colSpan = columns; td.className = "empty"; body.append(row); }
function badge(row, state) { const td = cell(row, ""), span = document.createElement("span"); span.className = "badge " + (TONE[state] || ""); span.textContent = state; td.append(span); }
function button(td, label, subject, action) { const b = document.createElement("button"); b.type = "button"; b.className = "secondary"; b.textContent = label; b.setAttribute("aria-label", `${label} ${subject}`); b.onclick = async () => { try { await action(); } catch(error) { notice(error.message); } }; td.append(b); }
function chart() {
 const c = el("chart"), r = devicePixelRatio || 1, w = c.clientWidth || 1100, h = 180; c.width = Math.round(w * r); c.height = h * r;
 const ctx = c.getContext("2d"); ctx.setTransform(r, 0, 0, r, 0, 0); ctx.clearRect(0, 0, w, h);
 const peak = Math.max(0, ...depths), scale = Math.max(1, peak), latest = depths.length ? depths[depths.length - 1] : 0;
 ctx.strokeStyle = css("--line"); ctx.lineWidth = 1; for (let y = 20; y < h; y += 40) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
 ctx.strokeStyle = css("--accent"); ctx.lineWidth = 2; ctx.beginPath(); depths.forEach((v, i) => { const x = i * w / 59, y = h - 15 - v * (h - 35) / scale; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke();
 ctx.fillStyle = css("--muted"); ctx.font = "12px monospace"; ctx.fillText(depths.length ? `Peak ${peak} waiting jobs` : "No data yet", 12, 16);
 c.setAttribute("aria-label", depths.length ? `Queue depth over time: latest ${latest}, peak ${peak}` : "Queue depth over time: no data yet");
}
function resetPanels() {
 depths = []; chart();
 for (const id of ["workers", "jobs", "events"]) el(id).replaceChildren();
 for (const id of ["depth", "healthy", "throughput", "p95"]) el(id).textContent = "—";
 el("saturation").textContent = "Awaiting connection"; el("slots").textContent = "Capacity / active slots";
 el("inspect").textContent = INSPECT_HINT; el("updated").textContent = ""; document.body.classList.remove("stale");
}
function setStatus(text) { if (el("connection").textContent !== text) el("connection").textContent = text; }
async function refresh() {
 if (busy || !token) return; busy = true; const current = generation;
 try {
  const [data, jobs] = await Promise.all([api("/operations"), api("/jobs?limit=30&state=" + encodeURIComponent(el("filter").value))]);
  if (current !== generation) return; // disconnected or reconnected while in flight
  setStatus("Connected"); el("updated").textContent = new Date().toLocaleTimeString(); document.body.classList.remove("stale");
  el("depth").textContent = data.queue_depth; el("saturation").textContent = `${data.admitted_active} / ${data.queue_limit} admitted jobs`;
  const healthy = data.workers.filter(w => w.state === "HEALTHY");
  el("healthy").textContent = healthy.length; el("slots").textContent = `${data.workers.reduce((n, w) => n + w.running, 0)} active / ${healthy.reduce((n, w) => n + w.capacity, 0)} healthy slots`;
  el("throughput").textContent = data.throughput_60s.toFixed(2); el("p95").textContent = data.duration.p95.toFixed(3);
  depths.push(data.queue_depth); if (depths.length > 60) depths.shift(); chart();
  el("workers").replaceChildren(); if (!data.workers.length) empty(el("workers"), 5, "No workers registered yet");
  for (const w of data.workers) { const row = document.createElement("tr"); cell(row, w.id); badge(row, w.state); cell(row, `${w.running}/${w.capacity}`); cell(row, w.heartbeat_age_seconds.toFixed(1) + "s"); const td = cell(row, ""); if (["HEALTHY", "SUSPECT"].includes(w.state)) button(td, "Drain", `worker ${w.id}`, async () => { await api(`/workers/${encodeURIComponent(w.id)}/drain`, {method: "POST"}); notice("Worker will finish active jobs and stop claiming."); await refresh(); }); el("workers").append(row); }
  el("jobs").replaceChildren(); if (!jobs.length) empty(el("jobs"), 6, el("filter").value ? `No ${el("filter").value} jobs` : "No jobs yet");
  for (const job of jobs) { const row = document.createElement("tr"), short = job.id.slice(0, 8); cell(row, short); cell(row, job.task); badge(row, job.status); cell(row, job.attempt_count); cell(row, job.priority); const td = cell(row, ""); button(td, "Inspect", `job ${short}`, async () => { const [now, attempts] = await Promise.all([api(`/jobs/${job.id}`), api(`/jobs/${job.id}/attempts`)]); el("inspect").textContent = JSON.stringify({job: now, attempts}, null, 2); }); if (["QUEUED", "RETRY_WAIT"].includes(job.status)) button(td, "Cancel", `job ${short}`, async () => { await api(`/jobs/${job.id}`, {method: "DELETE"}); notice("Job cancelled."); await refresh(); }); el("jobs").append(row); }
  el("events").replaceChildren();
  const events = [...data.system_events.map(e => ({at: e.at, text: e.kind + " · " + (e.worker_id || "")})), ...data.job_events.map(e => ({at: e.at, text: e.state + " · " + e.job_id.slice(0, 8)}))].sort((a, b) => b.at.localeCompare(a.at)).slice(0, 30);
  if (!events.length) { const li = document.createElement("li"); li.className = "empty"; li.textContent = "No events yet"; el("events").append(li); }
  for (const e of events) { const li = document.createElement("li"); li.textContent = new Date(e.at).toLocaleTimeString() + "  " + e.text; el("events").append(li); }
 } catch (error) {
  if (current !== generation) return;
  setStatus("Connection unavailable"); document.body.classList.add("stale"); notice(error.message + " · Showing the last successful refresh.");
 } finally { busy = false; }
}
el("connect").onsubmit = async event => { event.preventDefault(); token = el("token").value; el("token").value = ""; generation++; clearInterval(timer); resetPanels(); notice(""); setStatus("Connecting…"); el("disconnect").disabled = false; await refresh(); timer = setInterval(refresh, 2000); };
el("disconnect").onclick = () => { token = ""; generation++; clearInterval(timer); resetPanels(); setStatus("Disconnected"); el("disconnect").disabled = true; notice("Disconnected. Displayed results were cleared."); };
el("task").onchange = () => { el("args").value = JSON.stringify({calculate_pi: {iterations: 100000}, sleep: {seconds: 2}, hash_text: {text: "Hello GreyQueue"}, flaky: {failures: 2}}[el("task").value]); };
el("filter").onchange = refresh;
el("submit").onsubmit = async event => { event.preventDefault(); try { const job = await api("/jobs", {method: "POST", body: JSON.stringify({task: el("task").value, args: JSON.parse(el("args").value), priority: Number(el("priority").value), max_retries: Number(el("retries").value), idempotency_key: crypto.randomUUID()})}); notice("Submitted " + job.id); await refresh(); } catch (error) { notice(error.message); } };
addEventListener("resize", chart); chart();
