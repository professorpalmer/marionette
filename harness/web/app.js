const $ = s => document.querySelector(s);
const stream = $("#stream"), artList = $("#artifact-list"),
      jobList = $("#job-list"), pill = $("#status-pill"),
      attachments = $("#attachments");
let running = false;
let pending = [];  // {path, name} uploaded, awaiting run
let activeES = null;

async function loadConfig(){
  const c = await (await fetch("/api/config")).json();
  $("#driver-name").textContent = c.driver;
  $("#driver-meta").textContent = `reach=${c.reach} · budget=${c.budget}`;
  if(c.preflight){
    const b = el("turn err");
    b.innerHTML = `<div class="turn-head"><span class="badge error">setup</span>`+
      `<span>driver not ready</span></div><div class="turn-body">${esc(c.preflight)}</div>`;
    stream.appendChild(b);
  }
}
function setStatus(s){ pill.className = "pill " + s; pill.textContent = s; }
function el(cls, html){ const d=document.createElement("div"); d.className=cls;
  if(html!=null) d.innerHTML=html; return d; }
function esc(s){ return (s||"").replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function addTurn(action, headHtml, bodyHtml, isErr){
  const t = el("turn" + (isErr?" err":""));
  const h = el("turn-head");
  h.appendChild(el("badge "+action, action.replace("_"," ")));
  const lbl = document.createElement("span"); lbl.innerHTML = headHtml; h.appendChild(lbl);
  t.appendChild(h);
  if(bodyHtml!=null) t.appendChild(el("turn-body", bodyHtml));
  stream.appendChild(t); stream.scrollTop = stream.scrollHeight;
  return t;
}

function addThinking(label){
  const t = el("turn thinking");
  t.innerHTML = `<div class="turn-head"><span class="dots"><i></i><i></i><i></i></span>`+
    `<span class="muted">${esc(label||"driver thinking...")}</span></div>`;
  stream.appendChild(t); stream.scrollTop = stream.scrollHeight;
  return t;
}

function addUser(text, bodyHtml){
  const t = el("turn user");
  const h = el("turn-head");
  h.appendChild(el("who", "you"));
  const lbl = document.createElement("span"); lbl.textContent = text; h.appendChild(lbl);
  t.appendChild(h);
  if(bodyHtml!=null) t.appendChild(el("turn-body", bodyHtml));
  stream.appendChild(t); stream.scrollTop = stream.scrollHeight;
}

function pushArtifacts(arts){
  if(!arts || !arts.length) return;
  if(artList.querySelector(".empty")) artList.innerHTML="";
  for(const a of arts){
    const c = el("acard");
    c.innerHTML = `<div class="atype">${esc(a.type)}</div>`+
                  `<div class="ahead">${esc(a.headline)}</div>`+
                  (a.confidence!=null?`<div class="aconf">confidence ${a.confidence}</div>`:"");
    artList.appendChild(c);
  }
}

async function refreshJobs(){
  const jobs = await (await fetch("/api/jobs")).json();
  jobList.innerHTML="";
  for(const j of jobs.slice().reverse()){
    const it = el("job-item");
    it.innerHTML = `<div class="job-goal">${esc(j.goal||"(task)")}</div>`+
                   `<div class="job-meta">${esc(j.status.split('.').pop())} · ${j.artifacts} artifacts</div>`;
    it.onclick = async ()=>{
      const arts = await (await fetch("/api/artifacts?job_id="+encodeURIComponent(j.id))).json();
      artList.innerHTML=""; pushArtifacts(arts);
    };
    jobList.appendChild(it);
  }
}

function renderChips(){
  attachments.innerHTML="";
  pending.forEach((f,idx)=>{
    const c = el("chip");
    c.innerHTML = `<span>${esc(f.name)}</span><span class="x" data-i="${idx}">remove</span>`;
    c.querySelector(".x").onclick = ()=>{ pending.splice(idx,1); renderChips(); };
    attachments.appendChild(c);
  });
}

async function uploadFiles(files){
  if(!files || !files.length) return;
  const fd = new FormData();
  for(const f of files) fd.append("file", f, f.name);
  try{
    const r = await (await fetch("/api/upload", {method:"POST", body:fd})).json();
    (r.saved||[]).forEach(s=>pending.push(s));
    renderChips();
  }catch(e){ addTurn("error", "upload failed", `<div>${esc(""+e)}</div>`, true); }
}

function run(prompt){
  if(running) return; running = true; setStatus("running");
  $("#send").disabled = true;
  const imgs = pending.map(p=>p.path);
  const userBody = imgs.length ? `<div class="muted">${imgs.length} image(s) attached</div>` : null;
  addUser(prompt, userBody);
  pending=[]; renderChips();
  let url = "/api/run?prompt="+encodeURIComponent(prompt);
  if(imgs.length) url += "&images="+encodeURIComponent(imgs.join("|"));
  let thinking = addThinking();
  const es = new EventSource(url); activeES = es;
  $("#send").hidden = true; $("#stop").hidden = false;
  es.onmessage = (m)=>{
    const ev = JSON.parse(m.data);
    if(thinking){ thinking.remove(); thinking = null; }
    // re-show thinking after a swarm's artifacts while the driver decides next
    const reThink = (ev.kind==="artifacts");
    if(ev.kind==="done"){ es.close(); activeES=null; running=false;
      $("#send").disabled=false; $("#send").hidden=false; $("#stop").hidden=true;
      if(pill.textContent==="running") setStatus("done"); refreshJobs(); return; }
    const d = ev.data||{};
    if(ev.kind==="vision"){
      if(d.error) addTurn("error", `vision error`, `<div>${esc(d.error)}</div>`, true);
      else if(d.chars!=null) addTurn("executing", `vision · ${d.chars} chars · ${esc(d.model||"")}`,
                                     `<div class="muted">${esc(d.preview||"")}</div>`);
      else addTurn("executing", `vision · transcribing ${d.count||""} image(s)`, null);
    } else if(ev.kind==="intent"){
      const rep = d.repairs_used ? ` <span class="muted">(repaired x${d.repairs_used})</span>`:"";
      setStatus("running");
      if(d.action==="run_swarm")
        addTurn("run_swarm", `turn ${ev.turn} · <span class="muted">${d.tokens_out} tok</span>${rep}`,
          `<div class="goal">${esc(d.goal)}</div><div class="rationale">${esc(d.rationale)}</div>`);
      else
        addTurn(d.action, `turn ${ev.turn}${rep}`, `<div class="rationale">${esc(d.rationale)}</div>`);
    } else if(ev.kind==="executing"){
      setStatus("executing");
      addTurn("executing", `Puppetmaster running`, `<div class="goal">${esc(d.goal)}</div>`);
    } else if(ev.kind==="artifacts"){
      const body = (d.artifacts||[]).map(a=>
        `<div class="art"><span class="t">${esc(a.type)}</span>${esc(a.headline)}</div>`).join("");
      const subNote = d.adapter==="demo" ? ` <span class="muted">· demo substrate</span>` : "";
      addTurn("run_swarm", `job ${esc(d.job_id)} · ${d.num} artifacts · ${esc((d.types||[]).join(", "))}${subNote}`, body);
      pushArtifacts(d.artifacts);
      thinking = addThinking("driver reviewing findings...");
    } else if(ev.kind==="final"){
      addTurn(d.action, d.forced?"final (forced)":"final", `<div class="rationale">${esc(d.rationale)}</div>`);
      setStatus(d.forced?"error":"done");
    } else if(ev.kind==="error"){
      addTurn("error", `error`, `<div>${esc(d.error)}</div>`+
        (d.raw?`<div class="muted">raw: ${esc(d.raw)}</div>`:""), true); setStatus("error");
    }
  };
  es.onerror = ()=>{ es.close(); activeES=null; running=false; $("#send").disabled=false;
    $("#send").hidden=false; $("#stop").hidden=true;
    if(pill.textContent==="running") setStatus("error"); };
}

$("#composer").addEventListener("submit", e=>{
  e.preventDefault(); const p = $("#prompt").value.trim();
  if(!p) return; $("#prompt").value=""; run(p);
});
$("#stop").onclick = ()=>{
  if(activeES){ activeES.close(); activeES=null; }
  running=false; $("#send").disabled=false; $("#send").hidden=false; $("#stop").hidden=true;
  setStatus("idle");
  addTurn("error", "stopped by user", null, true);
};
$("#attach").onclick = ()=> $("#file").click();
$("#file").onchange = e=> uploadFiles(e.target.files);

// drag & drop onto center pane
const center = $("#center");
["dragover","dragenter"].forEach(ev=>center.addEventListener(ev,e=>{
  e.preventDefault(); center.classList.add("drag"); }));
["dragleave","drop"].forEach(ev=>center.addEventListener(ev,e=>{
  e.preventDefault(); center.classList.remove("drag"); }));
center.addEventListener("drop", e=>{
  const files=[...(e.dataTransfer?.files||[])].filter(f=>f.type.startsWith("image/"));
  if(files.length) uploadFiles(files);
});

loadConfig(); refreshJobs();
