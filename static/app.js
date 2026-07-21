/**
 * app.js — AutoFlow frontend.
 * REST + Socket.IO backend. Full screenshots, PDF/script export, element badges,
 * start-from-any-step playback, and snipping-tool overlay.
 */

const socket = io();

// Prevent accidental tab close / refresh while recording or playing back.
// Shows the browser's built-in "Leave site?" dialog.
window.addEventListener('beforeunload', e => {
  if (S.recording || S.playing) {
    e.preventDefault();
    e.returnValue = '';  // required by Chrome to trigger the dialog
  }
});
socket.on("step",          s   => onStep(s));
socket.on("app_state",     s   => onAppState(s));
socket.on("play_paused",   ()  => onPlayPaused());
socket.on("step_update",   msg => onStepUpdate(msg.index, msg.step));
socket.on("play_progress", msg => onPlayProgress(msg.index));
socket.on("play_done",     ()  => onPlayDone());
socket.on("play_error",    msg => onPlayError(msg.error));
socket.on("browser_hint",  msg => onBrowserHint(msg.msg));
socket.on("clear_steps",   ()  => { S.steps=[]; S.variables={}; S.activeWF=null; S.playStartIdx=0; render(); });
socket.on("record_stopped",msg => { S.recording=false; S.recPaused=false; S.steps=msg.steps||[]; S.playStartIdx=0; S.dirty=true; render(); updateButtons(); setStatus(`Recorded ${S.steps.length} steps`); });

// ── State ─────────────────────────────────────────────────────────────
const S = {
  steps: [], variables: {},
  recording: false, recPaused: false,
  playing: false, playPaused: false, playIdx: -1,
  playStartIdx: 0,    // which step Play / Step starts from (set by clicking a card)
  activeWF: null, view: "cards", dragSrc: null,
  dirty: false,       // unsaved changes indicator
  _trash: [],         // undo stack for deleted steps (last 10)
};

// ── Metadata ──────────────────────────────────────────────────────────
const META = {
  click:       {icon:"🖱️", label:"Mouse Click",   cmd:"click"},
  type:        {icon:"⌨️", label:"Type Text",     cmd:"type"},
  hotkey:      {icon:"⚡", label:"Hotkey",        cmd:"hotkey"},
  scroll:      {icon:"🔄", label:"Scroll",        cmd:"scroll"},
  wait:        {icon:"⏱️", label:"Wait",          cmd:"wait"},
  navigate:    {icon:"🌐", label:"Navigate URL",  cmd:"navigate"},
  loop:        {icon:"🔁", label:"Loop",          cmd:"loop"},
  loop_end:    {icon:"↩️", label:"End Loop",      cmd:"loop_end"},
  if:          {icon:"❓", label:"If Condition",  cmd:"if"},
  else:        {icon:"↕️", label:"Else",          cmd:"else"},
  end_if:      {icon:"✓",  label:"End If",        cmd:"end_if"},
  set_variable:{icon:"📦", label:"Set Variable",  cmd:"set_variable"},
  run_script:  {icon:"⚙️", label:"Run Script",    cmd:"run_script"},
  screenshot:  {icon:"📸", label:"Screenshot",    cmd:"screenshot"},
  comment:     {icon:"💬", label:"Comment",       cmd:"comment"},
  // ── Tier 1 new step types ────────────────────────────────────────────────
  error_handler:   {icon:"🛡️",  label:"Error Handler",    cmd:"error_handler"},
  launch_browser:  {icon:"🚀",  label:"Launch Browser",   cmd:"launch_browser"},
  show_message:    {icon:"📢",  label:"Show Message",     cmd:"show_message"},
  wait_for_element:{icon:"⏳",  label:"Wait for Element", cmd:"wait_for_element"},
  get_clipboard:   {icon:"📋",  label:"Get Clipboard",    cmd:"get_clipboard"},
  set_clipboard:   {icon:"📌",  label:"Set Clipboard",    cmd:"set_clipboard"},
  image_click:     {icon:"🖼️",  label:"Image Click",      cmd:"image_click"},
  // ── Tier 2: Power-Automate-style file / web / process actions ──────────
  wait_for_window: {icon:"🪟",  label:"Wait for Window",  cmd:"wait_for_window"},
  read_file:       {icon:"📄",  label:"Read File",        cmd:"read_file"},
  write_file:      {icon:"📝",  label:"Write File",       cmd:"write_file"},
  copy_file:       {icon:"📑",  label:"Copy File",        cmd:"copy_file"},
  move_file:       {icon:"📦",  label:"Move File",        cmd:"move_file"},
  delete_file:     {icon:"🗑️",  label:"Delete File",      cmd:"delete_file"},
  http_request:    {icon:"🌍",  label:"HTTP Request",     cmd:"http_request"},
  kill_process:    {icon:"⛔",  label:"Kill Process",     cmd:"kill_process"},
  close_window:    {icon:"❌",  label:"Close Window",     cmd:"close_window"},
  open_file:       {icon:"📂",  label:"Open File/App",    cmd:"open_file"},
  play_sound:      {icon:"🔊",  label:"Play Sound",       cmd:"play_sound"},
};
const meta = t => META[t] || {icon:"❓", label:t, cmd:t};

// ── Helpers ───────────────────────────────────────────────────────────
const esc = s => String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");

function toast(msg, ms=2500){
  const el=document.getElementById("toast");
  el.textContent=msg; el.classList.add("show");
  clearTimeout(el._t); el._t=setTimeout(()=>el.classList.remove("show"),ms);
}
function setStatus(msg,cls=""){
  const el=document.getElementById("status-msg");
  el.textContent=msg; el.className=cls;
}
function stepCountEl(){ document.getElementById("step-count").textContent=`${S.steps.length} step${S.steps.length!==1?"s":""}`; }
function updateStartHint(){
  const hint = document.getElementById("play-start-hint");
  if(hint){
    if(!S.playing && !S.recording && S.steps.length > 0 && S.playStartIdx > 0){
      hint.textContent = `▶ from step ${S.playStartIdx + 1}`;
      hint.style.display = "";
    } else {
      hint.style.display = "none";
    }
  }
}

// ── Descriptions ──────────────────────────────────────────────────────
function stepMain(step){
  const d=step.data||{}, el=d.element;
  switch(step.type){
    case"click":
      { const n=el&&el.name?el.name:""; return n?(n.length>90?n.slice(0,90)+"…":n):`(${d.x}, ${d.y}) — ${d.button||"left"}`; }
    case"type":
      { const t=(d.text||"").replace(/\n/g,"↵"); return `"${t.length>80?t.slice(0,80)+"…":t}"`; }
    case"hotkey":   return d.combo||"";
    case"scroll":   return `scroll dy:${d.dy||0} at (${d.x},${d.y})`;
    case"wait":     return `${d.ms||1000} ms`;
    case"navigate": return d.url||"(no URL)";
    case"loop":     return `Repeat ${d.count||1} times`;
    case"loop_end": return "End of Loop";
    case"if":       return `If {{${d.var||"…"}}} = "${d.value||""}"`;
    case"else":     return "Else";
    case"end_if":   return "End If";
    case"set_variable": return `{{${d.name||"?"}}} = "${d.value||""}"`;
    case"run_script":{ const c=d.command||""; return c.length>70?c.slice(0,70)+"…":c; }
    case"comment":  { const c=d.text||"";    return c.length>80?c.slice(0,80)+"…":c; }
    case"screenshot": return "Capture screenshot";
    case"error_handler":    return `On error: ${d.action||"stop"} (max retries: ${d.max_retries||0})`;
    case"launch_browser":   return `${d.browser||"chrome"} → ${d.url||""}${d.cdp?" (CDP)":""}`;
    case"show_message":     return `[${d.type||"info"}] ${d.title||"AutoFlow"}: ${(d.message||"").slice(0,60)}`;
    case"wait_for_element": return `Wait for "${d.name||d.type||"?"}" (${d.timeout_ms||5000} ms)`;
    case"get_clipboard":    return `clipboard → {{${d.variable||"?"}}}`;
    case"set_clipboard":    { const t=(d.text||""); return `clipboard ← "${t.length>60?t.slice(0,60)+"…":t}"`; }
    case"image_click":      return `Image click (confidence: ${d.confidence||0.85})`;
    case"wait_for_window":  return `Wait for window "${d.title||"?"}" (${d.timeout_ms||8000} ms)`;
    case"read_file":        return `Read ${d.path||"?"} → {{${d.variable||"?"}}}`;
    case"write_file":       return `${d.append?"Append to":"Write"} ${d.path||"?"}`;
    case"copy_file":        return `Copy ${d.src||"?"} → ${d.dst||"?"}`;
    case"move_file":        return `Move ${d.src||"?"} → ${d.dst||"?"}`;
    case"delete_file":      return `Delete ${d.path||"?"}`;
    case"http_request":     return `${d.method||"GET"} ${d.url||"?"} → {{${d.variable||"?"}}}`;
    case"kill_process":     return `Kill process "${d.name||"?"}"`;
    case"close_window":     return `Close window "${d.title||"?"}"`;
    case"open_file":        return `Open ${d.path||"?"}`;
    case"play_sound":       return `Play sound: ${d.sound||"default"}`;
    default: return JSON.stringify(d);
  }
}

function stepSub(step){
  const d=step.data||{}, el=d.element;
  if(step.type==="click" && el&&el.name)
    return `(${d.x}, ${d.y}) · ${d.button||"left"} click${el.window?" · "+el.window:""}`;
  return "";
}

function stepTarget(step){
  const d=step.data||{}, el=d.element;
  switch(step.type){
    case"click": return el&&el.name?`${el.type?el.type+": ":""}${el.name}${el.window?" @ "+el.window:""}`:(`(${d.x},${d.y})`);
    case"navigate": return d.url||"";
    case"loop":     return `×${d.count||1}`;
    case"if":       return `{{${d.var||""}}} = ${d.value||""}`;
    case"set_variable": return `{{${d.name||""}}}`;
    default: return "";
  }
}

function stepValue(step){
  const d=step.data||{};
  switch(step.type){
    case"click":    return `${d.button||"left"} click`;
    case"type":     { const t=(d.text||"").replace(/\n/g,"↵"); return `"${t.length>60?t.slice(0,60)+"…":t}"`; }
    case"hotkey":   return d.combo||"";
    case"scroll":   return `dy=${d.dy||0}`;
    case"wait":     return `${d.ms||1000} ms`;
    case"set_variable": return d.value||"";
    case"run_script":{ const c=d.command||""; return c.length>60?c.slice(0,60)+"…":c; }
    case"comment":  { const c=d.text||"";    return c.length>70?c.slice(0,70)+"…":c; }
    default: return "";
  }
}

// ── Indent calculator ─────────────────────────────────────────────────
function calcIndents(){
  let depth=0;
  return S.steps.map(s=>{
    const t=s.type;
    if(t==="loop_end"||t==="end_if"||t==="else") depth=Math.max(0,depth-1);
    const d=depth;
    if(t==="loop"||t==="if") depth++;
    if(t==="else") depth++;
    return d;
  });
}

// ── Render ────────────────────────────────────────────────────────────
function render(){
  if(S.view==="cards") renderCards();
  else renderTable();
  stepCountEl();
  updateStartHint();
  updateButtons();
  updateNativeHint();
}

// Show a persistent banner when any step hit a NativeViewHost (Chrome without accessibility).
// Fires on every render so it catches newly loaded workflows and live recording steps.
function updateNativeHint(){
  const banner=document.getElementById("native-hint");
  if(!banner) return;
  const hasNative=S.steps.some(s=>(s.data&&s.data.element&&s.data.element.class)==="NativeViewHost");
  // Only show (never auto-hide once dismissed): if banner was manually dismissed, leave it hidden.
  // Re-show whenever NativeViewHost steps are present and banner is not already visible.
  if(hasNative && banner.style.display==="none" && !banner.dataset.dismissed){
    banner.style.display="flex";
  } else if(!hasNative){
    banner.style.display="none";
    delete banner.dataset.dismissed;
  }
}

function renderCards(){
  const wrap=document.getElementById("canvas-wrap");
  const empty=document.getElementById("canvas-empty");
  wrap.querySelectorAll(".step-card").forEach(e=>e.remove());
  if(!S.steps.length){ empty.style.display="flex"; return; }
  empty.style.display="none";
  const indents=calcIndents();

  S.steps.forEach((step,i)=>{
    const m=meta(step.type), d=step.data||{}, el=d.element;
    const isActive  = S.playing && S.playIdx===i;
    const isStart   = !S.playing && !S.recording && i===S.playStartIdx;
    const card=document.createElement("div");
    const isDisabled = !!step.disabled;
    card.className="step-card"
      +(isActive?" playing":"")
      +(isStart?" play-start":"")
      +(isDisabled?" step-disabled":"")
      +(indents[i]>0?` indent-${Math.min(indents[i],2)}`:"");
    card.dataset.idx=i; card.draggable=true;
    card.title = isStart && S.playStartIdx===0 ? "" :
                 isStart ? `Play / Step will start from here (step ${i+1})` :
                 "Click to start playback from this step";

    // Element badge
    let elBadge="";
    if(step.type==="click"){
      if(!d.screenshot&&!d.element){
        elBadge=`<div class="el-badge pending">⏳ detecting…</div>`;
      } else if(el&&el.name){
        // Fully identified: has a human-readable name
        const src_icon = el.source==="cdp"?"🌐":el.source==="win32"?"🪟":"🎯";
        elBadge=`<div class="el-badge">${src_icon} ${esc(el.type?el.type+": ":"")}${esc(el.name)}${el.window?" · "+esc(el.window):""}</div>`;
      } else if(el&&el.class){
        // Element found in accessibility tree but has no accessible name.
        // Show the ClassName so the user knows what was detected.
        const cls_note = el.class==="NativeViewHost"
          ? "⚠ Chrome content area — enable CDP"
          : el.class==="scroll-canvas"||el.class==="canvas"
            ? "⚠ canvas element — use Image Click"
            : `⚠ class: ${esc(el.class)}`;
        elBadge=`<div class="el-badge nameless" title="Element found but has no accessible name. Playback will use coordinates.">${cls_note}</div>`;
      } else {
        elBadge=`<div class="el-badge miss">⚠ element not detected</div>`;
      }
    }

    // Region crop as hero image; click opens full screenshot
    let thumbHtml="";
    if(step.type==="click"){
      const full   = d.screenshot_full || d.screenshot;
      const region = d.screenshot_region || d.screenshot;
      if(region){
        thumbHtml=`<img class="step-thumb step-thumb--region" src="data:image/jpeg;base64,${region}" alt="" `
          +`onclick="showLightbox('${full||region}')" title="Click to view full screenshot">`;
      } else {
        thumbHtml=`<div class="thumb-spinner">⏳</div>`;
      }
    }

    const ts="";  // timestamps removed — cluttered cards; wait steps show duration in description
    const sub=stepSub(step);
    card.innerHTML=`
      <div class="step-drag">⠿</div>
      <div class="step-num" title="Click to set start point">${i+1}${isStart&&i>0?'<span class="start-arrow">▶</span>':""}</div>
      <div class="step-icon">${m.icon}</div>
      <div class="step-body">
        ${elBadge}
        <div class="step-type">${m.label}</div>
        <div class="step-main">${esc(stepMain(step))}</div>
        ${sub?`<div class="step-sub">${esc(sub)}</div>`:""}
        ${ts?`<div class="step-ts">${ts}</div>`:""}
        <div class="step-note-wrap" onclick="event.stopPropagation()"><div class="step-note-edit" contenteditable="true" data-placeholder="💬 Add note…" onblur="saveNoteInline(${i},this)" onkeydown="handleNoteKey(event,${i},this)">${step.note?esc(step.note):""}</div></div>
      </div>
      ${thumbHtml}
      <div class="step-acts">
        <button class="s-btn" title="Edit"      onclick="editStep(${i})">✏️</button>
        <button class="s-btn" title="Duplicate" onclick="dupStep(${i})">⎘</button>
        <button class="s-btn${isDisabled?' active':''}" title="${isDisabled?'Enable step':'Disable step'}" onclick="toggleDisable(${i})">${isDisabled?'▶':'🚫'}</button>
        <button class="s-btn del" title="Delete" onclick="delStep(${i})">🗑</button>
      </div>`;

    // Clicking the card body (not action buttons / thumbnail) sets the start index
    card.addEventListener("click", e => {
      if(S.recording || S.playing) return;
      if(e.target.closest("button, .step-thumb, .step-acts")) return;
      S.playStartIdx = i;
      render();
    });

    card.addEventListener("dragstart",e=>{S.dragSrc=i;card.classList.add("dragging");e.dataTransfer.effectAllowed="move";});
    card.addEventListener("dragend",  ()=>card.classList.remove("dragging"));
    card.addEventListener("dragover", e=>{e.preventDefault();card.classList.add("drag-over");});
    card.addEventListener("dragleave",()=>card.classList.remove("drag-over"));
    card.addEventListener("drop",     e=>{
      e.preventDefault(); card.classList.remove("drag-over");
      if(S.dragSrc!==null&&S.dragSrc!==i){
        const[m]=S.steps.splice(S.dragSrc,1);
        S.steps.splice(i,0,m); S.dragSrc=null; S.dirty=true; render();
      }
    });
    wrap.appendChild(card);
  });
}

function renderTable(){
  const tbody=document.getElementById("cmd-tbody");
  tbody.innerHTML="";
  const indents=calcIndents();
  S.steps.forEach((step,i)=>{
    const m=meta(step.type), playing=S.playing&&S.playIdx===i;
    const isStart = !S.playing && !S.recording && i===S.playStartIdx;
    const tr=document.createElement("tr");
    if(playing)  tr.classList.add("playing");
    if(isStart)  tr.classList.add("play-start");
    tr.title = isStart ? `Start point (step ${i+1})` : "Click to set start point";
    const ind=indents[i];
    const pad=ind?`style="padding-left:${ind*18+10}px"`:""
    tr.innerHTML=`
      <td class="td-num">${i+1}${isStart&&i>0?'<span class="start-arrow">▶</span>':""}</td>
      <td class="td-icon">${m.icon}</td>
      <td class="td-cmd" ${pad}><span class="cmd-badge">${m.cmd}</span></td>
      <td class="td-target">${esc(stepTarget(step))}</td>
      <td class="td-value">${esc(stepValue(step))}</td>
      <td class="td-acts">
        <button class="s-btn" onclick="editStep(${i})" title="Edit">✏️</button>
        <button class="s-btn del" onclick="delStep(${i})" title="Delete">🗑</button>
      </td>`;
    // Row click (not on action buttons) sets start index
    tr.addEventListener("click", e => {
      if(S.recording || S.playing) return;
      if(e.target.closest("button")) return;
      S.playStartIdx = i;
      render();
    });
    tbody.appendChild(tr);
  });
}

// ── View toggle ───────────────────────────────────────────────────────
function setView(v){
  S.view=v;
  document.getElementById("canvas-wrap").classList.toggle("hidden",v!=="cards");
  document.getElementById("cmd-table-wrap").classList.toggle("active",v==="table");
  document.getElementById("vt-cards").classList.toggle("active",v==="cards");
  document.getElementById("vt-table").classList.toggle("active",v==="table");
  render();
}

// ── Buttons ───────────────────────────────────────────────────────────
function updateButtons(){
  const r=S.recording, rp=S.recPaused, p=S.playing, pp=S.playPaused, n=S.steps.length;

  const btnRec=document.getElementById("btn-record");
  btnRec.textContent = r ? "⏹ Stop Rec" : "● Record";
  btnRec.classList.toggle("active", r && !rp);

  const btnPauseRec=document.getElementById("btn-pause-rec");
  btnPauseRec.style.display = r ? "" : "none";
  btnPauseRec.textContent   = rp ? "▶ Resume" : "⏸ Pause";
  btnPauseRec.classList.toggle("active", rp);

  document.getElementById("btn-play").disabled = r || p || n===0;

  const btnPausePlay=document.getElementById("btn-pause-play");
  btnPausePlay.style.display = p ? "" : "none";
  btnPausePlay.textContent   = pp ? "▶ Resume" : "⏸ Pause";

  // Step button: show when playing/paused OR when idle with steps (step-from-start)
  const btnStep=document.getElementById("btn-step");
  btnStep.style.display = (p || pp || (!r && n > 0)) ? "" : "none";
  btnStep.disabled = r;

  document.getElementById("btn-stop").disabled = !p && !pp;

  const hasSteps = n > 0;
  const btnSave = document.getElementById("btn-save");
  btnSave.disabled = !hasSteps;
  btnSave.classList.toggle("unsaved", S.dirty && hasSteps);
  document.title = (S.dirty && hasSteps ? "● " : "") + "AutoFlow";
  document.getElementById("btn-export").disabled = !hasSteps;
  document.getElementById("btn-script").disabled = !hasSteps;
  document.getElementById("btn-zip").disabled    = !hasSteps;
}

// ── Record ────────────────────────────────────────────────────────────
async function toggleRecord(){
  if(!S.recording){
    const r=await fetch("/api/record/start",{method:"POST"}).then(x=>x.json());
    if(!r.ok){toast("Error: "+r.error);return;}
    S.recording=true; S.recPaused=false; S.steps=[];
    render(); setStatus("● Recording… click Stop when done","recording"); toast("Recording started");
  } else {
    const r=await fetch("/api/record/stop",{method:"POST"}).then(x=>x.json());
    S.recording=false; S.recPaused=false;
    if(r.ok){
      S.steps=r.steps||[];
      S.playStartIdx=0;
      render(); setStatus(`Recorded ${S.steps.length} steps`); toast(`Recorded ${S.steps.length} steps`);
    }
  }
  updateButtons();
}

async function togglePauseRecord(){
  if(!S.recPaused){
    await fetch("/api/record/pause",{method:"POST"});
    S.recPaused=true; setStatus("⏸ Recording paused"); toast("Recording paused");
  } else {
    await fetch("/api/record/resume",{method:"POST"});
    S.recPaused=false; setStatus("● Recording…","recording"); toast("Recording resumed");
  }
  updateButtons();
}

// ── Playback ──────────────────────────────────────────────────────────
async function playWorkflow(){
  if(!S.steps.length) return;
  const speed=parseFloat(document.getElementById("speed-r").value);
  const useEl=document.getElementById("el-target").checked;
  const r=await fetch("/api/play",{
    method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
      steps:S.steps, speed, variables:S.variables,
      useElementTargeting:useEl, startIndex:S.playStartIdx,
    }),
  }).then(x=>x.json());
  if(!r.ok){toast("Error: "+r.error);return;}
  S.playing=true; S.playPaused=false; S.playIdx=-1;
  const fromMsg = S.playStartIdx > 0 ? ` from step ${S.playStartIdx+1}` : "";
  setStatus("▶ Playing"+fromMsg+"…","playing"); updateButtons();
}

async function stopPlayback(){
  await fetch("/api/play/stop",{method:"POST"});
  S.playing=false; S.playPaused=false; S.playIdx=-1;
  setStatus("Stopped"); render(); updateButtons();
}

async function togglePausePlay(){
  if(!S.playPaused){
    await fetch("/api/play/pause",{method:"POST"});
    S.playPaused=true; setStatus("⏸ Playback paused"); toast("Paused"); updateButtons();
  } else {
    await fetch("/api/play/resume",{method:"POST"});
    S.playPaused=false; setStatus("▶ Playing…","playing"); toast("Resumed"); updateButtons();
  }
}

async function stepPlayback(){
  if(S.recording) return;

  if(S.playing || S.playPaused){
    // Player already active — just advance one step
    await fetch("/api/play/step",{method:"POST"});
  } else {
    // Not playing at all — start a fresh player at playStartIdx, in paused mode
    if(!S.steps.length) return;
    const speed=parseFloat(document.getElementById("speed-r").value);
    const useEl=document.getElementById("el-target").checked;
    const r=await fetch("/api/play/step",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        steps:S.steps, speed, variables:S.variables,
        useElementTargeting:useEl, startIndex:S.playStartIdx,
      }),
    }).then(x=>x.json());
    if(!r.ok){toast("Error: "+(r.error||"unknown"));return;}
    S.playing=true;
  }
  S.playPaused=true;
  setStatus("⏭ Stepping…");
  updateButtons();
}


// ── Socket handlers ───────────────────────────────────────────────────
function onStep(step){
  S.steps.push(step);
  render();
  const w=document.getElementById("canvas-wrap");
  w.scrollTop=w.scrollHeight;
}
function onStepUpdate(idx,step){
  if(idx<S.steps.length){ S.steps[idx]=step; render(); }
}
function onPlayProgress(idx){
  if(idx===0){ S.playError=null; const bar=document.getElementById("error-bar"); if(bar)bar.remove(); }
  S.playIdx=idx;
  render();
  const el=S.view==="cards"
    ?document.querySelectorAll(".step-card")[idx]
    :document.querySelectorAll("#cmd-tbody tr")[idx];
  if(el){
    el.scrollIntoView({behavior:"smooth",block:"nearest"});
    // If this is the error step, mark it red
    if(S.playError && idx===S.playIdx)
      el.style.outline="2px solid #ff4444";
  }
}
function onPlayDone(){
  S.playing=false; S.playPaused=false; S.playIdx=-1;
  setStatus("Playback complete ✓"); render(); updateButtons(); toast("Playback complete");
}
function onPlayError(msg){
  S.playing=false; S.playPaused=false;
  // Parse "Step N (type): ..." to highlight the failed card
  const m = msg.match(/^Step (\d+)/);
  S.playIdx = m ? parseInt(m[1]) - 1 : S.playIdx;
  S.playError = msg;   // persistent — cleared only by a new play/record
  setStatus("\u274c Error: "+msg,"error"); render(); updateButtons();
  showErrorBanner(msg);
}
function showErrorBanner(msg){
  let bar = document.getElementById("error-bar");
  if(!bar){
    bar = document.createElement("div");
    bar.id = "error-bar";
    bar.style.cssText = "position:fixed;bottom:0;left:0;right:0;background:#7a1818;color:#fff;"+
      "padding:10px 16px;font-size:13px;z-index:9999;display:flex;align-items:center;gap:12px;"+
      "border-top:2px solid #ff4444;";
    document.body.appendChild(bar);
  }
  bar.innerHTML = "<span style=\"font-size:16px\">\u274c</span>" +
    "<span style=\"flex:1\">" + esc(msg) + "</span>" +
    "<button onclick=\"dismissError()\" style=\"background:#b03030;border:none;color:white;"+
    "padding:4px 10px;border-radius:4px;cursor:pointer\">Dismiss</button>";
}
function dismissError(){
  const bar = document.getElementById("error-bar");
  if(bar) bar.remove();
  S.playError = null;
  S.playIdx = -1;
  render();
}
function onPlayPaused(){
  S.playPaused=true; updateButtons();
}
function onAppState(s){
  if(s.record==="recording"){ S.recording=true;  S.recPaused=false; updateButtons(); }
  if(s.record==="paused")   { S.recPaused=true;  updateButtons(); }
  if(s.record==="idle" && S.recording){ S.recording=false; S.recPaused=false; updateButtons(); }
  if(s.play==="paused")  { S.playing=true;  S.playPaused=true;  updateButtons(); }
  if(s.play==="playing") { S.playing=true;  S.playPaused=false; updateButtons(); }
  if(s.play==="idle")    { S.playing=false; S.playPaused=false; updateButtons(); }
}
function onBrowserHint(msg){
  toast("💡 " + msg, 6000);
}

// ── Save / Load ───────────────────────────────────────────────────────
async function saveWorkflow(){
  const name=document.getElementById("wf-name").value.trim()||"Untitled";
  const r=await fetch("/api/workflows",{
    method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name,steps:S.steps,variables:S.variables}),
  }).then(x=>x.json());
  if(r.ok){S.dirty=false;updateButtons();toast(`Saved "${name}"`);loadWFList();}
  else toast("Save failed: "+r.error);
}

async function loadWFList(){
  const r=await fetch("/api/workflows").then(x=>x.json());
  const list=document.getElementById("wf-list");
  list.innerHTML="";
  if(!r.ok||!r.workflows.length){list.innerHTML='<div class="empty-hint">No saved workflows yet.</div>';return;}
  r.workflows.forEach(wf=>{
    const el=document.createElement("div");
    el.className="wf-item"+(S.activeWF===wf.file?" active":"");
    el.innerHTML=`<div class="wf-n" title="${esc(wf.name)}">${esc(wf.name)}</div>
      <div class="wf-c">${wf.steps}</div>
      <button class="wf-del" onclick="delWF('${esc(wf.file)}',event)">✕</button>`;
    el.addEventListener("click",()=>openWF(wf.file,wf.name));
    list.appendChild(el);
  });
}

async function openWF(file,name){
  const r=await fetch(`/api/workflows/${encodeURIComponent(file)}`).then(x=>x.json());
  if(!r.ok){toast("Load failed");return;}
  S.steps=r.workflow.steps||[]; S.variables=r.workflow.variables||{};
  S.activeWF=file; S.playStartIdx=0;
  document.getElementById("wf-name").value=name;
  S.dirty=false; render(); renderVariables(); setStatus(`Opened "${name}"`); toast(`Loaded "${name}"`); loadWFList();
}

async function delWF(file,e){
  e.stopPropagation();
  await fetch(`/api/workflows/${encodeURIComponent(file)}`,{method:"DELETE"});
  if(S.activeWF===file){S.activeWF=null;S.steps=[];S.variables={};S.playStartIdx=0;render();}
  loadWFList(); toast("Deleted");
}

function newWorkflow(){
  if(S.steps.length&&!confirm("Start new workflow? Unsaved steps will be lost."))return;
  S.steps=[];S.variables={};S.activeWF=null;S.playStartIdx=0;
  document.getElementById("wf-name").value="";
  S.dirty=false; render();renderVariables();setStatus("New workflow");loadWFList();
}

// ── PDF Export ────────────────────────────────────────────────────────
async function exportPDF(){
  if(!S.steps.length)return;
  const name=document.getElementById("wf-name").value.trim()||"Workflow";
  toast("Generating report…");
  const r=await fetch("/api/export/report",{
    method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name,steps:S.steps,variables:S.variables,created:Date.now()/1000}),
  });
  if(!r.ok){toast("Export failed");return;}
  const html=await r.text();
  const blob=new Blob([html],{type:"text/html"});
  const url=URL.createObjectURL(blob);
  const win=window.open(url,"_blank");
  if(win) win.addEventListener("load",()=>setTimeout(()=>win.print(),800));
  toast("Report opened — use Ctrl+P to save as PDF");
}

// ── Python Script Export ──────────────────────────────────────────────
async function exportScript(){
  if(!S.steps.length)return;
  const name=document.getElementById("wf-name").value.trim()||"workflow";
  toast("Generating script…");
  const r=await fetch("/api/export/script",{
    method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name,steps:S.steps,variables:S.variables,created:Date.now()/1000}),
  });
  if(!r.ok){toast("Script export failed");return;}
  const blob=await r.blob();
  const url=URL.createObjectURL(blob);
  const a=document.createElement("a");
  // Derive filename from Content-Disposition header if present
  const disp=r.headers.get("Content-Disposition")||"";
  const match=disp.match(/filename="([^"]+)"/);
  a.download=match?match[1]:(name.replace(/[^a-zA-Z0-9_]/g,"_")+".py");
  a.href=url;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
  toast(`Script saved: ${a.download}`);
}


// ── ZIP Package Export ─────────────────────────────────────────────────────
async function exportZip(){
  if(!S.steps.length)return;
  const name=document.getElementById("wf-name").value.trim()||"workflow";
  toast("Building package…");
  const r=await fetch("/api/export/zip",{
    method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name,steps:S.steps,variables:S.variables,created:Date.now()/1000}),
  });
  if(!r.ok){toast("ZIP export failed");return;}
  const blob=await r.blob();
  const url=URL.createObjectURL(blob);
  const a=document.createElement("a");
  const disp=r.headers.get("Content-Disposition")||"";
  const match=disp.match(/filename="([^"]+)"/);
  a.download=match?match[1]:(name.replace(/[^a-zA-Z0-9_]/g,"_")+".zip");
  a.href=url;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
  toast(`Package saved: ${a.download} — share it with anyone (run.bat handles the rest)`);
}

// ── Import Workflow or Script ──────────────────────────────────────────────
function importWorkflow(){
  document.getElementById("import-input").value="";
  document.getElementById("import-input").click();
}

async function onImportFile(event){
  const file=event.target.files[0];
  if(!file)return;
  const text=await file.text();
  const ext=file.name.split(".").pop().toLowerCase();

  if(ext==="json"){
    // Direct workflow JSON — load as-is
    try{
      const wf=JSON.parse(text);
      const steps=wf.steps||[];
      const variables=wf.variables||{};
      const wfName=wf.name||(file.name.replace(/\.json$/i,""))||"Imported";
      S.steps=steps; S.variables=variables; S.playStartIdx=0;
      document.getElementById("wf-name").value=wfName;
      render(); renderVariables(); updateButtons();
      setStatus(`Imported ${steps.length} steps from ${file.name}`);
      toast(`Loaded: ${wfName} (${steps.length} steps)`);
    } catch(e){
      toast("Invalid JSON: "+e.message);
    }
    return;
  }

  if(ext==="py"){
    // Parse exported Python script back into steps
    toast("Parsing script…");
    const name=file.name.replace(/\.py$/i,"")||"Imported";
    const r=await fetch("/api/import/script",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({text,name}),
    });
    if(!r.ok){toast("Import failed");return;}
    const wf=await r.json();
    if(wf.error){toast("Parse error: "+wf.error);return;}
    S.steps=wf.steps||[]; S.variables=wf.variables||{}; S.playStartIdx=0;
    document.getElementById("wf-name").value=wf.name||name;
    render(); renderVariables(); updateButtons();
    setStatus(`Imported ${S.steps.length} steps from ${file.name}`);
    toast(`Parsed: ${S.steps.length} steps imported`);
    return;
  }

  toast("Unsupported file type — use .json or .py");
}

// ── Quit ──────────────────────────────────────────────────────────────
async function quitApp(){
  if(!confirm("Quit AutoFlow? The server will stop."))return;
  await fetch("/api/quit",{method:"POST"}).catch(()=>{});
  document.body.innerHTML="<div style='display:flex;align-items:center;justify-content:center;height:100vh;color:#8892a4;font-size:18px'>AutoFlow stopped.</div>";
}

// ── Variables ─────────────────────────────────────────────────────────
function renderVariables(){
  const list=document.getElementById("var-list"); list.innerHTML="";
  Object.entries(S.variables).forEach(([k,v])=>{
    const row=document.createElement("div"); row.className="var-row";
    row.innerHTML=`
      <input class="var-name" value="${esc(k)}" placeholder="name" onblur="renameVar('${esc(k)}',this.value)">
      <span class="var-eq">=</span>
      <input class="var-val" value="${esc(v)}" placeholder="value" onchange="S.variables['${esc(k)}']=this.value">
      <button class="var-del" onclick="delVar('${esc(k)}')">✕</button>`;
    list.appendChild(row);
  });
}

function addVariable(){ let n="var1",i=1; while(S.variables.hasOwnProperty(n)) n=`var${++i}`; S.variables[n]=""; renderVariables(); }
function delVar(name){ delete S.variables[name]; renderVariables(); }
function renameVar(o,n){ n=n.trim(); if(!n||n===o)return; S.variables[n]=S.variables[o]; delete S.variables[o]; renderVariables(); }

// ── Step add / edit / delete ──────────────────────────────────────────
const DEFAULTS={
  click:{x:0,y:0,button:"left",element:null,screenshot:null,screenshot_full:null},
  type:{text:""},hotkey:{combo:"ctrl+c"},
  scroll:{x:0,y:0,dx:0,dy:-3},wait:{ms:1000},
  navigate:{url:"https://"},loop:{count:3},
  loop_end:{},if:{var:"",value:""},else:{},end_if:{},
  set_variable:{name:"",value:""},run_script:{command:""},
  screenshot:{},comment:{text:""},
  error_handler:   {action:"continue", max_retries:3},
  launch_browser:  {browser:"chrome", url:"https://", cdp:true},
  show_message:    {title:"AutoFlow", message:"", type:"info"},
  wait_for_element:{name:"", type:"", timeout_ms:5000},
  get_clipboard:   {variable:"clipboard_text"},
  set_clipboard:   {text:""},
  image_click:     {image_b64:"", confidence:0.85},
  wait_for_window: {title:"", timeout_ms:8000},
  read_file:       {path:"", variable:"file_text", encoding:"utf-8"},
  write_file:      {path:"", text:"", append:false, encoding:"utf-8"},
  copy_file:       {src:"", dst:""},
  move_file:       {src:"", dst:""},
  delete_file:     {path:"", is_folder:false, ignore_errors:true},
  http_request:    {url:"https://", method:"GET", headers:{}, body:"", variable:"http_response", timeout_sec:15},
  kill_process:    {name:"notepad.exe"},
  close_window:    {title:"", ignore_missing:true},
  open_file:       {path:""},
  play_sound:      {sound:"default"},
};

function addStep(type){
  const last=S.steps[S.steps.length-1];
  const step={id:S.steps.length,type,timestamp:last?(last.timestamp||0)+0.5:0,data:{...(DEFAULTS[type]||{})}};
  S.steps.push(step);
  if(type==="loop") S.steps.push({id:S.steps.length,type:"loop_end",timestamp:step.timestamp+0.1,data:{}});
  if(type==="if")   S.steps.push({id:S.steps.length,type:"end_if",  timestamp:step.timestamp+0.1,data:{}});
  S.dirty = true;
  render();
  if(!["loop_end","else","end_if","screenshot"].includes(type)) editStep(S.steps.indexOf(step));
}

function dupStep(i){ const c=JSON.parse(JSON.stringify(S.steps[i])); c.timestamp=(c.timestamp||0)+0.1; S.steps.splice(i+1,0,c); S.dirty=true; render(); }
function toggleDisable(i){ S.steps[i].disabled = !S.steps[i].disabled; S.dirty=true; render(); }
function undoDelete(){
  if(!S._trash.length){ toast("Nothing to undo"); return; }
  const {step, idx} = S._trash.pop();
  S.steps.splice(Math.min(idx, S.steps.length), 0, step);
  S.dirty = true;
  render();
  toast("Step restored");
}
function delStep(i){
  // Save to trash for undo (keep last 10)
  S._trash.push({step: JSON.parse(JSON.stringify(S.steps[i])), idx: i});
  if(S._trash.length > 10) S._trash.shift();
  S.steps.splice(i,1);
  // Clamp start index so it doesn't point past the end
  if(S.playStartIdx >= S.steps.length) S.playStartIdx = Math.max(0, S.steps.length-1);
  S.dirty = true;
  render();
  toast("Step deleted — Ctrl+Z to undo");
}

// ── Edit modal ────────────────────────────────────────────────────────
let _ei=-1;
const _d=(k,def="")=>{ if(_ei<0)return def; const d=S.steps[_ei]?.data||{}; return d[k]!==undefined?d[k]:def; };

const FIELDS={
  click:       ()=>`<div class="field"><label>X</label><input id="ef-x" type="number" value="${_d("x",0)}"></div>
                    <div class="field"><label>Y</label><input id="ef-y" type="number" value="${_d("y",0)}"></div>
                    <div class="field"><label>Button</label><select id="ef-btn">
                      <option ${_d("button")==="left"?"selected":""}>left</option>
                      <option ${_d("button")==="right"?"selected":""}>right</option>
                      <option ${_d("button")==="middle"?"selected":""}>middle</option></select></div>`,
  type:        ()=>`<div class="field"><label>Text</label><textarea id="ef-text">${esc(_d("text",""))}</textarea>
                    <div class="field-hint">Use {{varName}} for variables</div></div>`,
  hotkey:      ()=>`<div class="field"><label>Key combo (e.g. ctrl+s)</label><input id="ef-combo" value="${esc(_d("combo",""))}"></div>`,
  wait:        ()=>`<div class="field"><label>Delay (ms)</label><input id="ef-ms" type="number" min="0" value="${_d("ms",1000)}"></div>`,
  scroll:      ()=>`<div class="field"><label>X</label><input id="ef-x" type="number" value="${_d("x",0)}"></div>
                    <div class="field"><label>Y</label><input id="ef-y" type="number" value="${_d("y",0)}"></div>
                    <div class="field"><label>Horizontal (dx)</label><input id="ef-dx" type="number" value="${_d("dx",0)}"></div>
                    <div class="field"><label>Vertical (dy, negative=up)</label><input id="ef-dy" type="number" value="${_d("dy",0)}"></div>`,
  navigate:    ()=>`<div class="field"><label>URL</label><input id="ef-url" value="${esc(_d("url",""))}">
                    <div class="field-hint">Use {{varName}} for dynamic URLs</div></div>`,
  loop:        ()=>`<div class="field"><label>Repeat count</label><input id="ef-count" type="number" min="1" value="${_d("count",3)}"></div>`,
  if:          ()=>`<div class="field"><label>Variable name (without {{ }})</label><input id="ef-var" value="${esc(_d("var",""))}"></div>
                    <div class="field"><label>Equals value</label><input id="ef-value" value="${esc(_d("value",""))}"></div>`,
  set_variable:()=>`<div class="field"><label>Variable name</label><input id="ef-name" value="${esc(_d("name",""))}">
                    <div class="field-hint">Reference as {{name}} in other steps</div></div>
                    <div class="field"><label>Value</label><input id="ef-value" value="${esc(_d("value",""))}"></div>`,
  run_script:  ()=>`<div class="field"><label>Shell command</label><textarea id="ef-command">${esc(_d("command",""))}</textarea>
                    <div class="field-hint">Runs via shell. Use {{varName}} for variables.</div></div>`,
  comment:       ()=>`<div class="field"><label>Comment text</label><textarea id="ef-text">${esc(_d("text",""))}</textarea></div>`,
  error_handler: ()=>`
    <div class="field"><label>On error</label><select id="ef-action">
      <option ${_d("action")==="stop"    ?"selected":""}>stop</option>
      <option ${_d("action")==="continue"?"selected":""}>continue</option>
      <option ${_d("action")==="retry"   ?"selected":""}>retry</option></select></div>
    <div class="field"><label>Max retries (retry mode only)</label>
      <input id="ef-retries" type="number" min="0" max="20" value="${_d("max_retries",3)}"></div>
    <div class="field-hint">Applies to all subsequent steps until the next Error Handler.</div>`,
  launch_browser: ()=>`
    <div class="field"><label>Browser</label><select id="ef-browser">
      <option ${_d("browser")==="chrome"?"selected":""}>chrome</option>
      <option ${_d("browser")==="edge"  ?"selected":""}>edge</option></select></div>
    <div class="field"><label>URL</label><input id="ef-url" value="${esc(_d("url","https://"))}"></div>
    <div class="field"><label><input type="checkbox" id="ef-cdp" ${_d("cdp",true)?"checked":""}> Enable CDP (--remote-debugging-port=9222)</label></div>`,
  show_message: ()=>`
    <div class="field"><label>Title</label><input id="ef-title" value="${esc(_d("title","AutoFlow"))}"></div>
    <div class="field"><label>Message</label><textarea id="ef-message">${esc(_d("message",""))}</textarea></div>
    <div class="field"><label>Type</label><select id="ef-type">
      <option ${_d("type")==="info"   ?"selected":""}>info</option>
      <option ${_d("type")==="warning"?"selected":""}>warning</option>
      <option ${_d("type")==="error"  ?"selected":""}>error</option></select></div>`,
  wait_for_element: ()=>`
    <div class="field"><label>Element name (UIA Name)</label>
      <input id="ef-name" value="${esc(_d("name",""))}">
      <div class="field-hint">Must match exactly what UIA reports as the control's Name.</div></div>
    <div class="field"><label>Control type (optional)</label>
      <input id="ef-type" value="${esc(_d("type",""))}" placeholder="e.g. ButtonControl"></div>
    <div class="field"><label>Timeout (ms)</label>
      <input id="ef-ms" type="number" min="100" value="${_d("timeout_ms",5000)}"></div>`,
  get_clipboard: ()=>`
    <div class="field"><label>Store result in variable</label>
      <input id="ef-variable" value="${esc(_d("variable","clipboard_text"))}">
      <div class="field-hint">Reference as {{variable}} in subsequent steps.</div></div>`,
  set_clipboard: ()=>`
    <div class="field"><label>Text to copy</label>
      <textarea id="ef-text">${esc(_d("text",""))}</textarea>
      <div class="field-hint">Use {{varName}} for variable substitution.</div></div>`,
  image_click: ()=>`
    <div class="field"><label>Confidence (0–1)</label>
      <input id="ef-confidence" type="number" step="0.05" min="0.1" max="1.0" value="${_d("confidence",0.85)}">
      <div class="field-hint">Image template stored in step data. Record a click first; AutoFlow uses the screenshot_region as the image template.</div></div>`,
  wait_for_window: ()=>`
    <div class="field"><label>Window title</label>
      <input id="ef-title" value="${esc(_d("title",""))}">
      <div class="field-hint">Exact top-level window title to wait for (fast win32 check, no UIA).</div></div>
    <div class="field"><label>Timeout (ms)</label>
      <input id="ef-ms" type="number" min="100" value="${_d("timeout_ms",8000)}"></div>`,
  read_file: ()=>`
    <div class="field"><label>File path</label><input id="ef-path" value="${esc(_d("path",""))}"></div>
    <div class="field"><label>Store result in variable</label><input id="ef-variable" value="${esc(_d("variable","file_text"))}"></div>
    <div class="field"><label>Encoding</label><input id="ef-encoding" value="${esc(_d("encoding","utf-8"))}"></div>`,
  write_file: ()=>`
    <div class="field"><label>File path</label><input id="ef-path" value="${esc(_d("path",""))}"></div>
    <div class="field"><label>Text</label><textarea id="ef-text">${esc(_d("text",""))}</textarea>
      <div class="field-hint">Use {{varName}} for variable substitution.</div></div>
    <div class="field"><label><input type="checkbox" id="ef-append" ${_d("append")?"checked":""}> Append instead of overwrite</label></div>`,
  copy_file: ()=>`
    <div class="field"><label>Source path</label><input id="ef-src" value="${esc(_d("src",""))}"></div>
    <div class="field"><label>Destination path</label><input id="ef-dst" value="${esc(_d("dst",""))}"></div>`,
  move_file: ()=>`
    <div class="field"><label>Source path</label><input id="ef-src" value="${esc(_d("src",""))}"></div>
    <div class="field"><label>Destination path</label><input id="ef-dst" value="${esc(_d("dst",""))}"></div>`,
  delete_file: ()=>`
    <div class="field"><label>Path</label><input id="ef-path" value="${esc(_d("path",""))}"></div>
    <div class="field"><label><input type="checkbox" id="ef-isfolder" ${_d("is_folder")?"checked":""}> This is a folder (delete recursively)</label></div>
    <div class="field"><label><input type="checkbox" id="ef-ignore" ${_d("ignore_errors",true)?"checked":""}> Ignore errors if missing</label></div>`,
  http_request: ()=>`
    <div class="field"><label>Method</label><select id="ef-method">
      <option ${_d("method")==="GET"?"selected":""}>GET</option>
      <option ${_d("method")==="POST"?"selected":""}>POST</option>
      <option ${_d("method")==="PUT"?"selected":""}>PUT</option>
      <option ${_d("method")==="DELETE"?"selected":""}>DELETE</option></select></div>
    <div class="field"><label>URL</label><input id="ef-url" value="${esc(_d("url","https://"))}"></div>
    <div class="field"><label>Body (optional)</label><textarea id="ef-body">${esc(_d("body",""))}</textarea></div>
    <div class="field"><label>Store response in variable</label><input id="ef-variable" value="${esc(_d("variable","http_response"))}"></div>`,
  kill_process: ()=>`
    <div class="field"><label>Process name (e.g. notepad.exe)</label>
      <input id="ef-name" value="${esc(_d("name",""))}"></div>`,
  close_window: ()=>`
    <div class="field"><label>Window title</label><input id="ef-title" value="${esc(_d("title",""))}"></div>
    <div class="field"><label><input type="checkbox" id="ef-ignoremissing" ${_d("ignore_missing",true)?"checked":""}> Ignore if window not found</label></div>`,
  open_file: ()=>`
    <div class="field"><label>File / application path</label>
      <input id="ef-path" value="${esc(_d("path",""))}">
      <div class="field-hint">Opens with the OS default handler (like double-clicking it).</div></div>`,
  play_sound: ()=>`
    <div class="field"><label>Sound</label><select id="ef-sound">
      <option ${_d("sound")==="default"?"selected":""}>default</option>
      <option ${_d("sound")==="info"?"selected":""}>info</option>
      <option ${_d("sound")==="warning"?"selected":""}>warning</option>
      <option ${_d("sound")==="error"?"selected":""}>error</option></select></div>`,
};

function editStep(idx){
  _ei=idx;
  const step=S.steps[idx], m=meta(step.type);
  document.getElementById("modal-title").textContent=`Edit — ${m.label}`;
  const fn=FIELDS[step.type];
  const typeFields=fn?fn()
    :`<div class="field"><label>Data (JSON)</label><textarea id="ef-json">${esc(JSON.stringify(step.data,null,2))}</textarea></div>`;
  const noteField=`<div class="field note-field">
    <label>📝 Note<span class="label-hint">(optional — shown on the card and in the PDF)</span></label>
    <textarea id="ef-note" rows="3" placeholder="Add a note for this step…">${esc(step.note||"")}</textarea>
  </div>`;
  document.getElementById("modal-fields").innerHTML=typeFields+noteField;
  document.getElementById("modal-overlay").classList.add("open");
}

function saveEdit(){
  if(_ei<0)return;
  const step=S.steps[_ei], g=id=>document.getElementById(id)?.value??"";
  switch(step.type){
    case"click":       step.data.x=+g("ef-x");step.data.y=+g("ef-y");step.data.button=g("ef-btn");break;
    case"type":        step.data.text=g("ef-text");break;
    case"hotkey":      step.data.combo=g("ef-combo");break;
    case"wait":        step.data.ms=+g("ef-ms")||1000;break;
    case"scroll":      step.data.x=+g("ef-x");step.data.y=+g("ef-y");step.data.dx=+g("ef-dx");step.data.dy=+g("ef-dy");break;
    case"navigate":    step.data.url=g("ef-url");break;
    case"loop":        step.data.count=+g("ef-count")||1;break;
    case"if":          step.data.var=g("ef-var");step.data.value=g("ef-value");break;
    case"set_variable":step.data.name=g("ef-name");step.data.value=g("ef-value");break;
    case"run_script":  step.data.command=g("ef-command");break;
    case"comment":       step.data.text=g("ef-text");break;
    case"error_handler":
      step.data.action=g("ef-action");
      step.data.max_retries=+g("ef-retries")||0;
      break;
    case"launch_browser":
      step.data.browser=g("ef-browser");
      step.data.url=g("ef-url");
      step.data.cdp=document.getElementById("ef-cdp")?.checked??true;
      break;
    case"show_message":
      step.data.title=g("ef-title");
      step.data.message=g("ef-message");
      step.data.type=g("ef-type");
      break;
    case"wait_for_element":
      step.data.name=g("ef-name");
      step.data.type=g("ef-type");
      step.data.timeout_ms=+g("ef-ms")||5000;
      break;
    case"get_clipboard":  step.data.variable=g("ef-variable");break;
    case"set_clipboard":  step.data.text=g("ef-text");break;
    case"image_click":    step.data.confidence=+g("ef-confidence")||0.85;break;
    case"wait_for_window":
      step.data.title=g("ef-title");
      step.data.timeout_ms=+g("ef-ms")||8000;
      break;
    case"read_file":
      step.data.path=g("ef-path");
      step.data.variable=g("ef-variable");
      step.data.encoding=g("ef-encoding")||"utf-8";
      break;
    case"write_file":
      step.data.path=g("ef-path");
      step.data.text=g("ef-text");
      step.data.append=document.getElementById("ef-append")?.checked??false;
      break;
    case"copy_file":
    case"move_file":
      step.data.src=g("ef-src");
      step.data.dst=g("ef-dst");
      break;
    case"delete_file":
      step.data.path=g("ef-path");
      step.data.is_folder=document.getElementById("ef-isfolder")?.checked??false;
      step.data.ignore_errors=document.getElementById("ef-ignore")?.checked??true;
      break;
    case"http_request":
      step.data.method=g("ef-method");
      step.data.url=g("ef-url");
      step.data.body=g("ef-body");
      step.data.variable=g("ef-variable");
      break;
    case"kill_process":
      step.data.name=g("ef-name");
      break;
    case"close_window":
      step.data.title=g("ef-title");
      step.data.ignore_missing=document.getElementById("ef-ignoremissing")?.checked??true;
      break;
    case"open_file":
      step.data.path=g("ef-path");
      break;
    case"play_sound":
      step.data.sound=g("ef-sound");
      break;
    default: try{step.data=JSON.parse(g("ef-json"));}catch{}break;
  }
  // Save SOP note — universal for all step types
  const _note=(document.getElementById("ef-note")?.value||"").trim();
  if(_note) step.note=_note; else delete step.note;
  closeModal(); S.dirty = true;
  render();
}

function closeModal(){ document.getElementById("modal-overlay").classList.remove("open"); _ei=-1; }

// ── Lightbox ──────────────────────────────────────────────────────────
function showLightbox(b64){
  document.getElementById("lightbox-img").src="data:image/jpeg;base64,"+b64;
  document.getElementById("lightbox").classList.add("open");
}

// ── Speed ─────────────────────────────────────────────────────────────
function updateSpeed(){ const v=parseFloat(document.getElementById("speed-r").value); document.getElementById("speed-lbl").textContent=v.toFixed(2).replace(/\.?0+$/,"")+"×"; }

// ── Init ──────────────────────────────────────────────────────────────

// ── Inline SOP note editing ──────────────────────────────────────────────
function saveNoteInline(idx, el){
  const note=(el.textContent||"").trim();
  const step=S.steps[idx];
  if(!step) return;
  if((step.note||"")!==note){
    if(note) step.note=note; else delete step.note;
    S.dirty=true;
    updateButtons();
  }
}
function handleNoteKey(event, idx, el){
  event.stopPropagation();  // prevent card click / start-point handler
  if(event.key==="Enter"&&!event.shiftKey){ event.preventDefault(); el.blur(); }
  if(event.key==="Escape"){ el.textContent=S.steps[idx]?.note||""; el.blur(); }
}

// ── Collapsible sidebar sections ────────────────────────────────────────────
// Bump PAL_VER when the default collapsed-state changes so stale localStorage
// is ignored and users get the new defaults on the next launch.
const PAL_VER = 2;

function toggleSection(id) {
  const sec = document.getElementById(id);
  if (!sec) return;
  sec.classList.toggle("collapsed");
  // Persist open/closed state so it survives page refreshes
  const state = {};
  document.querySelectorAll(".pal-section").forEach(s => {
    if (s.id) state[s.id] = s.classList.contains("collapsed");
  });
  try {
    localStorage.setItem("palSections", JSON.stringify(state));
    localStorage.setItem("palSectionsV", String(PAL_VER));
  } catch(e) {}
}

function restorePalState() {
  try {
    // Defaults: collapse everything except the Workflows list.
    const defaults = { "sec-addstep": true, "sec-automation": true, "sec-filesweb": true, "sec-settings": true };
    const savedVer = parseInt(localStorage.getItem("palSectionsV") || "0");
    let state = defaults;
    if (savedVer >= PAL_VER) {
      // Only trust saved state if it was written by this version of the app.
      const saved = localStorage.getItem("palSections");
      state = Object.assign({}, defaults, saved ? JSON.parse(saved) : {});
    }
    document.querySelectorAll(".pal-section").forEach(s => {
      if (s.id && state[s.id]) s.classList.add("collapsed");
    });
  } catch(e) {}
}

function loadSettings() {
  // Read from localStorage — no external file needed
  const mode = localStorage.getItem("screenshotMode") || "all";
  const el = document.getElementById(mode === "active" ? "ss-active" : "ss-all");
  if (el) el.checked = true;
  // Push to server memory so the recorder picks it up immediately
  fetch("/api/settings", {method:"PATCH", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({screenshot_mode: mode})}).catch(()=>{});
}
function saveScreenshotMode(val) {
  localStorage.setItem("screenshotMode", val);
  fetch("/api/settings", {method:"PATCH", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({screenshot_mode: val})}).catch(()=>{});
}

document.addEventListener("keydown", e => {
  if (e.target.matches("input,textarea,select,[contenteditable]")) return;
  if (e.ctrlKey && e.key === "z") { e.preventDefault(); undoDelete(); }
  if (e.ctrlKey && e.key === "r") { e.preventDefault(); if (!S.recording && !S.playing) toggleRecord(); }
  if (e.ctrlKey && e.key === "p") { e.preventDefault(); if (!S.recording && !S.playing && S.steps.length) playWorkflow(); }
  if (e.ctrlKey && e.key === "s") { e.preventDefault(); if (S.steps.length) saveWorkflow(); }
  if (e.key === "Escape") { if (S.playing || S.recording) stopPlayback(); }
});

window.addEventListener("DOMContentLoaded",()=>{ restorePalState(); render(); renderVariables(); updateSpeed(); loadWFList(); loadSettings(); setStatus("Ready"); });
