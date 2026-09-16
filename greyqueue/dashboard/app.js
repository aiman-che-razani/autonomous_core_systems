"use strict";
let token = "", timer = null, busy = false, history = [];
const el = id => document.getElementById(id);
function notice(text) { el("notice").textContent = text; }
async function api(path, options = {}) {
  if (!token) throw new Error("Connect with a client token first.");
  const response = await fetch(path, {...options, headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"}, signal: AbortSignal.timeout(10000)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data;
}
function cell(row, value) { const td = document.createElement("td"); td.textContent = value; row.append(td); return td; }
function badge(row, state) { const td = cell(row, ""), span = document.createElement("span"); span.className = "badge" + (["FAILED","DEAD_LETTER","DEAD"].includes(state)?" bad":["QUEUED","RETRY_WAIT","SUSPECT"].includes(state)?" wait":""); span.textContent = state; td.append(span); }
function button(td, label, action) { const b = document.createElement("button"); b.type = "button"; b.className = "secondary"; b.textContent = label; b.onclick = async () => { try { await action(); } catch(error) { notice(error.message); } }; td.append(b); }
function chart() {
 const c = el("chart"), ctx = c.getContext("2d"), w=c.width,h=c.height; ctx.clearRect(0,0,w,h); const max=Math.max(1,...history);
 ctx.strokeStyle="#2b3539"; for(let y=20;y<h;y+=40){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();}
 ctx.strokeStyle="#a5efb7";ctx.lineWidth=2;ctx.beginPath();history.forEach((v,i)=>{const x=i*w/59,y=h-15-v*(h-35)/max;i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke();ctx.fillStyle="#97aaa5";ctx.font="12px monospace";ctx.fillText(`Peak ${max} waiting jobs`,12,16);
}
async function refresh() {
 if(busy || !token) return; busy=true;
 try {
  const [data,jobs]=await Promise.all([api("/operations"),api("/jobs?limit=30&state="+encodeURIComponent(el("filter").value))]);
  el("connection").textContent="Connected · "+new Date().toLocaleTimeString();
  el("depth").textContent=data.queue_depth;el("saturation").textContent=`${data.admitted_active} / ${data.queue_limit} admitted jobs`;
  el("healthy").textContent=data.workers.filter(w=>w.state==="HEALTHY").length;
  el("slots").textContent=`${data.workers.reduce((n,w)=>n+w.running,0)} active / ${data.workers.filter(w=>w.state==="HEALTHY").reduce((n,w)=>n+w.capacity,0)} healthy slots`;
  el("throughput").textContent=data.throughput_60s.toFixed(2);el("p95").textContent=data.duration.p95.toFixed(3);
  history.push(data.queue_depth);if(history.length>60)history.shift();chart();
  el("workers").replaceChildren(); for(const w of data.workers){const row=document.createElement("tr");cell(row,w.id);badge(row,w.state);cell(row,`${w.running}/${w.capacity}`);cell(row,w.heartbeat_age_seconds.toFixed(1)+"s");const td=cell(row,"");if(w.state==="HEALTHY")button(td,"Drain",async()=>{await api(`/workers/${encodeURIComponent(w.id)}/drain`,{method:"POST"});notice("Worker will finish active jobs and stop claiming.");await refresh()});el("workers").append(row)}
  el("jobs").replaceChildren();for(const job of jobs){const row=document.createElement("tr");cell(row,job.id.slice(0,8));cell(row,job.task);badge(row,job.status);cell(row,job.attempt_count);cell(row,job.priority);const td=cell(row,"");button(td,"Inspect",async()=>{const [current,attempts]=await Promise.all([api(`/jobs/${job.id}`),api(`/jobs/${job.id}/attempts`)]);el("inspect").textContent=JSON.stringify({job:current,attempts},null,2)});if(["QUEUED","RETRY_WAIT"].includes(job.status))button(td,"Cancel",async()=>{await api(`/jobs/${job.id}`,{method:"DELETE"});notice("Job cancelled.");await refresh()});el("jobs").append(row)}
  el("events").replaceChildren();const events=[...data.system_events.map(e=>({at:e.at,text:e.kind+" · "+(e.worker_id||"")})),...data.job_events.map(e=>({at:e.at,text:e.state+" · "+e.job_id.slice(0,8)}))].sort((a,b)=>b.at.localeCompare(a.at)).slice(0,30);for(const e of events){const li=document.createElement("li");li.textContent=new Date(e.at).toLocaleTimeString()+"  "+e.text;el("events").append(li)}
 }catch(error){el("connection").textContent="Connection unavailable";notice(error.message)}finally{busy=false}
}
el("connect").onsubmit=async event=>{event.preventDefault();token=el("token").value;el("token").value="";clearInterval(timer);notice("");await refresh();timer=setInterval(refresh,2000)};
el("disconnect").onclick=()=>{token="";clearInterval(timer);el("connection").textContent="Disconnected";notice("Disconnected. Reload to clear displayed results.")};
el("task").onchange=()=>{el("args").value=JSON.stringify({calculate_pi:{iterations:100000},sleep:{seconds:2},hash_text:{text:"Hello GreyQueue"},flaky:{failures:2}}[el("task").value])};
el("filter").onchange=refresh;
el("submit").onsubmit=async event=>{event.preventDefault();try{const job=await api("/jobs",{method:"POST",body:JSON.stringify({task:el("task").value,args:JSON.parse(el("args").value),priority:Number(el("priority").value),max_retries:Number(el("retries").value),idempotency_key:crypto.randomUUID()})});notice("Submitted "+job.id);await refresh()}catch(error){notice(error.message)}};
