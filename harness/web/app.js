const $ = s => document.querySelector(s);
const feed = $("#feed"), artList = $("#artifact-list"), jobList = $("#job-list"),
      pill = $("#status-pill"), promptEl = $("#prompt"), picker = $("#pilot-picker"),
      driverMeta = $("#driver-meta");
const esc = s => (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let es = null, attachments = [];

function setStatus(s){ pill.className = "pill " + s; pill.textContent = s; }

function addMsg(role, text){
  const el = document.createElement("div");
  el.className = "msg " + role;
  el.innerHTML = `<div class="who">${role==="user"?"you":"pilot"}</div>`+
                 `<div class="bubble">${esc(text)}</div>`;
  feed.appendChild(el); feed.scrollTop = feed.scrollHeight;
  return el.querySelector(".bubble");
}

function addActionCard(d){
  const el = document.createElement("div");
  el.className = "action-card open";
  el.id = "card-" + d.id;
  const goal = d.goal || "(investigation)";
  el.innerHTML =
    `<div class="action-head">
       <span class="spin" data-spin></span>
       <span class="label">Ran <b>swarm</b> &middot; ${esc(goal.slice(0,80))}</span>
       <span class="chev">&#9654;</span>
     </div>
     <div class="action-body">
       <div class="action-kv"><span class="k">kind</span><span class="v">${esc(d.kind)}</span></div>
       <div class="action-kv"><span class="k">goal</span><span class="v">${esc(goal)}</span></div>
       ${d.cwd?`<div class="action-kv"><span class="k">cwd</span><span class="v">${esc(d.cwd)}</span></div>`:""}
       <div data-result class="muted" style="font-size:12px;margin-top:6px">running...</div>
     </div>`;
  feed.appendChild(el); feed.scrollTop = feed.scrollHeight;
  el.querySelector(".action-head").onclick = () => el.classList.toggle("open");
  return el;
}

function fillActionResult(d){
  const el = $("#card-" + d.id); if(!el) return;
  const spin = el.querySelector("[data-spin]"); if(spin) spin.remove();
  const body = el.querySelector("[data-result]");
  if(d.error){ body.innerHTML = `<span style="color:var(--risk)">error: ${esc(d.error)}</span>`; el.classList.add("open"); return; }
  const rows = (d.artifacts||[]).map(a=>{
    const t = (a.type||"").toLowerCase();
    const cls = t.includes("risk")?"risk":(t.includes("decision")?"decision":"");
    return `<div class="art-row"><span class="t ${cls}">${esc(a.type)}</span><span>${esc(a.headline||"")}</span></div>`;
  }).join("");
  const sub = d.adapter==="demo" ? `<div class="substrate-note">demo substrate -- not real codebase analysis</div>` : "";
  body.innerHTML =
    `<div class="action-kv"><span class="k">job</span><span class="v">${esc(d.job_id)}</span></div>
     <div class="action-kv"><span class="k">found</span><span class="v">${d.num} artifacts &middot; ${esc((d.types||[]).join(", "))}</span></div>
     ${rows}${sub}`;
  // collapse the card now that it's done (Cursor-style: collapsed when complete)
  el.classList.remove("open");
  // populate right-pane durable state
  (d.artifacts||[]).forEach(a=>{
    const c = document.createElement("div"); c.className="acard";
    c.innerHTML = `<div class="atype">${esc(a.type)}</div><div class="ahead">${esc(a.headline||"")}</div>`+
                  (a.confidence!=null?`<div class="aconf">confidence ${a.confidence}</div>`:"");
    if(artList.querySelector(".empty")) artList.innerHTML="";
    artList.prepend(c);
  });
  refreshJobs();
}

function send(){
  const msg = promptEl.value.trim(); if(!msg) return;
  addMsg("user", msg); promptEl.value=""; promptEl.style.height="auto";
  setStatus("thinking");
  $("#send").hidden = true; $("#stop").hidden = false;
  let url = "/api/chat?message=" + encodeURIComponent(msg);
  es = new EventSource(url);
  es.onmessage = e => {
    let ev; try { ev = JSON.parse(e.data); } catch { return; }
    if(ev.kind==="done"){ es.close(); es=null; setStatus("done");
      $("#send").hidden=false; $("#stop").hidden=true; return; }
    const d = ev.data||{};
    if(ev.kind==="message"){ setStatus("thinking"); addMsg("assistant", d.text||""); }
    else if(ev.kind==="action_start"){ setStatus("executing"); addActionCard(d); }
    else if(ev.kind==="action_result"){ setStatus("thinking"); fillActionResult(d); }
    else if(ev.kind==="assistant_done"){ setStatus("done"); }
    else if(ev.kind==="error"){ setStatus("error"); addMsg("assistant", "[error] "+(d.error||"")); }
  };
  es.onerror = () => { if(es){es.close();es=null;} setStatus("error");
    $("#send").hidden=false; $("#stop").hidden=true; };
}

function refreshJobs(){
  fetch("/api/jobs").then(r=>r.json()).then(jobs=>{
    jobList.innerHTML = jobs.slice().reverse().map(j=>
      `<div class="job-item"><span class="g">${esc((j.goal||"").slice(0,40))}</span>`+
      `<span class="s">${esc((j.status||"").split(".").pop())}</span></div>`).join("")
      || `<div class="empty">No jobs yet.</div>`;
  }).catch(()=>{});
}

function loadConfig(){
  fetch("/api/config").then(r=>r.json()).then(c=>{
    driverMeta.textContent = `reach=${c.reach} · budget=${c.budget}`;
    // populate picker (current driver + any alternatives the server advertises)
    const models = c.models || [c.driver];
    picker.innerHTML = models.map(m=>`<option ${m===c.driver?"selected":""}>${esc(m)}</option>`).join("");
    if(c.preflight){ addMsg("assistant", "Setup needed: "+c.preflight); setStatus("error"); }
  }).catch(()=>{});
}

// composer
$("#composer").addEventListener("submit", e=>{ e.preventDefault(); send(); });
promptEl.addEventListener("keydown", e=>{
  if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); send(); }
});
promptEl.addEventListener("input", ()=>{ promptEl.style.height="auto";
  promptEl.style.height=Math.min(promptEl.scrollHeight,140)+"px"; });
$("#stop").onclick = ()=>{ if(es){es.close();es=null;} setStatus("idle");
  $("#send").hidden=false; $("#stop").hidden=true; };
$("#new-chat").onclick = ()=>{ feed.innerHTML=""; artList.innerHTML=`<div class="empty">Findings appear here as the pilot investigates.</div>`; setStatus("idle"); };
picker.onchange = ()=>{ fetch("/api/pilot?model="+encodeURIComponent(picker.value)).catch(()=>{}); };

loadConfig(); refreshJobs();
