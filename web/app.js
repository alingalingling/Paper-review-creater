"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
let state, activeView = "workspace", selectedId, pendingLabel = "pending", busy = false, filter = "all", toastTimer, unexported = false;
let preferredMode;
let browserStore;
let writingDraft;
let revisionDraft = "", newReviewMode = false;
let mobileEvidenceOpen = false;
let chatExpanded = false, pendingTurn, retryRequest, progressTimer;
const expandedProcesses = new Set();
const chatScrollPositions = new Map();
let chatResizeObserver;
let activeThread = localStorage.getItem("trace-active-thread") || "main", threadList = [], creatingThread = false;
const threadViews = new Map();
const snapshotView = () => ({state,activeView,selectedId,pendingLabel,busy,filter,unexported,preferredMode,writingDraft,revisionDraft,newReviewMode,mobileEvidenceOpen,chatExpanded,pendingTurn,retryRequest,expandedProcessKeys:[...expandedProcesses]});
function stashView() {
  if(!state) return;
  const context=threadViews.get(activeThread) || {};
  Object.assign(context,snapshotView());threadViews.set(activeThread,context);
  return context;
}
function activateView(id,context) {
  const switchingThread=id!==activeThread;
  activeThread=id;localStorage.setItem("trace-active-thread",id);
  ({state,activeView="workspace",selectedId,pendingLabel="pending",busy=false,filter="all",unexported=false,preferredMode,writingDraft,revisionDraft="",newReviewMode=false,mobileEvidenceOpen=false,chatExpanded=false,pendingTurn,retryRequest}=context);
  if(switchingThread){
    expandedProcesses.clear();
    for(const key of context.expandedProcessKeys || [])expandedProcesses.add(key);
  }
  render();
}
function receivedView(data) {
  const progress=data.progress;
  const pending=["running","error"].includes(progress?.status) && data.pending_request;
  const drafts=data.browser_storage?{writingDraft:data.draft?.writingDraft,revisionDraft:data.draft?.revisionDraft || "",preferredMode:data.draft?.preferredMode}:{};
  return {state:data,...drafts,busy:progress?.status==="running",chatExpanded:!!pending || !!data.run?.conversation?.length,pendingTurn:pending?{id:progress.operation_id,status:progress.status,kind:data.pending_request.kind,content:data.pending_request.content || "正在处理当前研究",progress:progress.events,error:progress.events.at(-1)?.message}:undefined};
}
function renderThreads() {
  if(!$("#threadList"))return;
  const markup=threadList.map((thread,index)=>{
    const context=threadViews.get(thread.id);
    const running=context?.busy || thread.status==="running";
    return `<button class="thread-item ${thread.id===activeThread?"active":""}" data-thread="${esc(thread.id)}" aria-current="${thread.id===activeThread?"true":"false"}" title="${esc(thread.title)}"><span class="thread-number">${String(index+1).padStart(2,"0")}</span><span class="thread-copy"><strong>${esc(thread.title)}</strong><small>${running?"正在写作…":thread.has_document?"继续研究":"新的研究"}</small></span><span class="thread-indicator ${running?"running":""}"></span></button>`;
  }).join("");
  if($("#threadList").innerHTML===markup)return;
  const focused=document.activeElement?.dataset.thread;
  $("#threadList").innerHTML=markup;
  $$("[data-thread]").forEach(button=>{
    button.onclick=()=>switchThread(button.dataset.thread);
    if(button.dataset.thread===focused)button.focus({preventScroll:true});
  });
}
let switchTicket=0;
async function switchThread(id) {
  if(id===activeThread)return;
  const ticket=++switchTicket;stashView();
  try {
    let context=threadViews.get(id);
    if(!context?.localRequest) {
      const data=await request("/api/state",undefined,false,id);
      if(ticket!==switchTicket)return;
      context={...context,...receivedView(data)};threadViews.set(id,context);
    }
    activateView(id,context);
  } catch(error){toast(error.message,true);}
}
async function createThread() {
  if(creatingThread)return;
  creatingThread=true;stashView();const ticket=++switchTicket;
  try {
    const data=await request("/api/threads/new",{});
    threadList=data.threads;const context=receivedView(data);threadViews.set(data.thread_id,context);
    if(ticket===switchTicket)activateView(data.thread_id,context);
    renderThreads();
  }catch(error){toast(error.message,true);}finally{creatingThread=false;}
}
async function refreshThreads() {
  try {
    const listing=await request("/api/threads");threadList=listing.threads;renderThreads();
    const id=activeThread, context=threadViews.get(id);
    const changedLocally=browserStore && threadList.find(t=>t.id===id)?.version!==state?.storage_version;
    if((busy || changedLocally) && !context?.localRequest) {
      const data=await request("/api/state",undefined,false,id);
      if(activeThread===id){stashView();Object.assign(context,receivedView(data));activateView(id,context);}
    }
  }catch(_){ /* Primary actions report network failures. */ }
  setTimeout(refreshThreads,2200);
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = "toast visible" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("visible"), error ? 6500 : 3500);
}

async function request(path, body, binary = false, threadId = activeThread) {
  if(browserStore)return browserStore.request(path,body,binary,threadId);
  const headers={"X-Trace-Conversation":threadId};
  const options={headers};
  if(body!==undefined) Object.assign(options,{method:"POST",headers:{...headers,"Content-Type":"application/json","X-Trace-Request":"1"},body:JSON.stringify(body)});
  const response = await fetch(path,options);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || "暂时无法连接服务，请稍后重试。");
  }
  if(binary)return response.blob();
  const data=await response.json();
  if(response.status===202)return waitForOperation(threadId,data.progress.operation_id);
  return data;
}

async function waitForOperation(threadId,operationId) {
  // Long writing jobs outlive a single proxy request. Poll without resubmitting.
  let failures=0;
  for(;;){
    await new Promise(resolve=>setTimeout(resolve,1500));
    let data;
    try {data=await request("/api/state",undefined,false,threadId);failures=0;}
    catch(error){if(++failures>=20)throw new Error("连接暂时中断，任务可能仍在处理。恢复连接后刷新页面查看进度。");continue;}
    const progress=data.progress;
    if(progress.operation_id!==operationId)throw new Error("会话已开始处理另一条请求，请刷新查看最新进度。");
    if(progress.status==="error")throw new Error(progress.events.at(-1)?.message || "本次处理未完成，原稿已保留。");
    if(progress.status!=="running")return data;
  }
}

function setBusy(value) {
  busy = value;
  $$("[data-mutation], #saveSettings, #testConnection, #restoreButton").forEach(button => button.disabled = value);
  $("#exportButton").disabled = value || !state?.run;
  const revise = $("#reviseButton");
  if(revise) {revise.disabled=value || (pendingTurn?.kind==="/api/generate" && pendingTurn.status==="error");revise.innerHTML=value?'<span class="spinner"></span><span>处理中</span>':icon("arrow");}
  if($("#revisionInput")) $("#revisionInput").disabled=value || (pendingTurn?.kind==="/api/generate" && pendingTurn.status==="error");
  const generate = $("#generateButton");
  if (generate) {
    generate.disabled = value;
    generate.innerHTML = value ? '<span class="spinner"></span>正在组织内容…' : ($("#modeSelect")?.value === "model" ? (state?.run?.mode==="model"?"重新生成全文":"生成综述") : "提取原文") + icon("arrow");
  }
}

async function mutate(path, body, message) {
  if (busy) return;
  setBusy(true);
  const conversational = ["/api/generate","/api/revise"].includes(path);
  if(conversational) {
    const expandingComposer=!$("#chatMessages");
    const operationId=crypto.randomUUID();
    pendingTurn={id:operationId,kind:path,status:"running",progress:[],content:path==="/api/generate"?"主题："+body.topic+"\n要求："+(body.requirements || "按文献生成综述"):body.instruction};
    retryRequest={path,body:{...body}};
    body={...body,operation_id:operationId};
    chatExpanded=true;newReviewMode=false;
    const change=()=>render({resetChat:path==="/api/generate"});
    if(expandingComposer && document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) document.startViewTransition(change);
    else change();
  }
  const id=activeThread,context=stashView();context.localRequest=true;
  renderThreads();
  try {
    const response=request(path,body,false,id);
    if(conversational)pollProgress(context,id);
    context.state=await response;
    if(conversational){context.pendingTurn=undefined;context.retryRequest=undefined;}
    if(["/api/annotate","/api/generate","/api/revise","/api/undo"].includes(path))context.unexported=true;
    if(["/api/revise","/api/undo"].includes(path))context.revisionDraft="";
    if(path==="/api/revise")context.selectedId=context.state.run.last_edit?.added_ids?.[0] || context.state.run.last_edit?.updated_ids?.[0] || context.selectedId;
    if(["/api/generate","/api/demo","/api/restore"].includes(path))Object.assign(context,{newReviewMode:false,revisionDraft:"",chatExpanded:path==="/api/generate",pendingTurn:undefined,writingDraft:undefined});
    threadList=context.state.threads;
    if(activeThread!==id)toast(`《${context.state.run?.topic || "研究"}》已处理完成`);
    else if(message)toast(message);
    return true;
  }catch(error){
    if(conversational && context.pendingTurn){context.pendingTurn.status="error";context.pendingTurn.error=error.message;}
    else toast(error.message,true);
    return false;
  }finally{
    context.busy=false;context.localRequest=false;
    if(activeThread===id)activateView(id,context);
    renderThreads();
  }
}

async function pollProgress(context,id) {
  if(!context.pendingTurn || context.pendingTurn.status!=="running")return;
  try {
    const progress=await request("/api/progress",undefined,false,id);
    if(context.pendingTurn && progress.operation_id===context.pendingTurn.id){
      context.pendingTurn.progress=progress.events;
      if(activeThread===id){
        const target=$("#liveProcess");
        if(target){
          const markup=processHTML(progress.events,true);
          if(target.innerHTML!==markup){
            rememberChatScroll();
            target.innerHTML=markup;
            restoreChatScroll();
          }
        }
      }
    }
  }catch(_){}
  if(context.pendingTurn?.status==="running")setTimeout(()=>pollProgress(context,id),850);
}

function processHTML(events, live=false) {
  if(!events?.length) return `<div class="process-wait"><span class="thinking-orb"></span>请求已发送，等待处理</div>`;
  return `<ol class="process-steps">${events.map((event,index)=>`<li class="${live && index===events.length-1 && !["complete","error"].includes(event.stage)?"active":"done"} ${event.stage==="error"?"error":""}"><span class="step-marker"></span><span>${esc(event.stage==="complete"?"处理完成":event.message)}</span></li>`).join("")}</ol>`;
}

function chatMessagesHTML(run) {
  let messages=pendingTurn?.kind==="/api/generate" ? [] : run?.conversation || [];
  if(!messages.length && pendingTurn?.kind!=="/api/generate" && run?.mode==="model" && run.claims?.length) messages=[
    {role:"user",content:"主题："+run.topic+"\n要求："+(run.requirements || "按文献生成综述")},
    {role:"assistant",content:`当前稿件《${run.article?.title || run.topic}》已就绪，可以继续提问或修改。`,document_ready:true}];
  return messages.map((message,index)=>`<div class="chat-message ${message.role==="user"?"user":"assistant"}">
    <span class="chat-avatar" aria-hidden="true">${message.role==="user"?"你":"循"}</span><div class="message-main">
      ${message.process?.length?`<details class="process-trace" data-process-key="${index}" ${expandedProcesses.has(String(index))?"open":""}><summary>${icon("check")}处理过程<span>${message.process.length} 步</span></summary>${processHTML(message.process)}</details>`:""}
      <div class="message-text">${esc(message.content)}</div>
      ${message.document_ready || (message.changes && Object.values(message.changes).some(ids=>ids.length))?`<a class="message-document" href="#reviewDocument">${icon("file")}${message.document_ready?"查看综述正文":`正文已更新${message.changes.added_ids.length?" · 新增 "+message.changes.added_ids.length+" 句":""}${message.changes.updated_ids.length?" · 改写 "+message.changes.updated_ids.length+" 句":""}`} ${icon("arrow")}</a>`:""}
    </div></div>`).join("")+(pendingTurn?`<div class="chat-message user"><span class="chat-avatar" aria-hidden="true">你</span><div class="message-main"><div class="message-text">${esc(pendingTurn.content)}</div></div></div><div class="chat-message assistant pending-message"><span class="chat-avatar" aria-hidden="true">循</span><div class="message-main">${pendingTurn.status==="error"?`<div class="message-text chat-error">${esc(pendingTurn.error)}</div><button class="text-button" id="retryChat">再试一次 ${icon("arrow")}</button>`:`<div class="live-process-label"><span class="thinking-orb"></span>正在处理</div><div id="liveProcess">${processHTML(pendingTurn.progress,true)}</div><span class="typing-dots" aria-label="等待回复"><i></i><i></i><i></i></span>`}</div></div>`:"");
}

const isLocated = claim => claim.evidence.length > 0 && claim.evidence.every(e => e.valid);
const selectedClaim = () => state.run?.claims.find(claim => claim.id === selectedId);
const percentage = value => value == null ? "—" : `${Math.round(value * 100)}%`;
function status(claim) {
  const label = !isLocated(claim) ? "定位未通过" : state.labels[claim.label];
  const type = !isLocated(claim) ? "invalid" : claim.label;
  return `<span class="status-pill ${esc(type)}">${icon(type === "supported" ? "check" : "clock")}${esc(label)}</span>`;
}

function rememberChatScroll(log=$("#chatMessages")) {
  if(!log?.isConnected)return;
  const position={
    top:log.scrollTop,
    follow:log.scrollHeight-log.clientHeight-log.scrollTop<=24
  };
  chatScrollPositions.set(log.dataset.threadId,position);
}

function restoreChatScroll(log=$("#chatMessages")) {
  if(!log?.isConnected)return;
  const position=chatScrollPositions.get(log.dataset.threadId) || {top:0,follow:true};
  // Restore before painting. Smooth scrolling here replays the entire chat history.
  const top=position.follow ? log.scrollHeight : position.top;
  log.scrollTo({top,behavior:"instant"});
  chatScrollPositions.set(log.dataset.threadId,{top:log.scrollTop,follow:position.follow});
}

function bindChatScroll() {
  const log=$("#chatMessages");
  if(!log)return;
  restoreChatScroll(log);
  log.addEventListener("scroll",()=>rememberChatScroll(log),{passive:true});
  chatResizeObserver=new ResizeObserver(()=>{
    if(log.isConnected)restoreChatScroll(log);
  });
  chatResizeObserver.observe(log);
  // Progress, folded details and wrapping can grow while the viewport stays fixed.
  for(const message of log.children)chatResizeObserver.observe(message);
}

function render({resetChat=false}={}) {
  rememberChatScroll();
  chatResizeObserver?.disconnect();
  if(resetChat)chatScrollPositions.delete(activeThread);
  renderThreads();
  const sources = state.corpus.documents;
  if (!state.run?.claims.some(c => c.id === selectedId)) selectedId = state.run?.claims[0]?.id;
  $("#navSourceCount").textContent = sources.length;
  $("#exportButton").disabled = !state.run || busy;
  const titles = {workspace:"写作",sources:"文献",ledger:"引用账本"};
  $("#pageTitle").textContent = titles[activeView];
  document.body.dataset.view = activeView;
  $$("[data-view]").forEach(button => {
    button.classList.toggle("active", button.dataset.view === activeView);
    if(button.dataset.view === activeView) button.setAttribute("aria-current","page");
    else button.removeAttribute("aria-current");
  });
  $("#viewContent").innerHTML = activeView === "workspace" ? workspaceHTML() : activeView === "sources" ? sourcesHTML() : ledgerHTML();
  if (activeView === "workspace") bindWorkspace();
  if (activeView === "sources") bindLibrary();
  if (activeView === "ledger") {
    $("#ledgerFilter").addEventListener("change", event => { filter = event.target.value; render(); });
    $$("[data-ledger-claim]").forEach(row => {
      const open = () => { selectedId = row.dataset.ledgerClaim; mobileEvidenceOpen = true; activeView = "workspace"; render(); $(`[data-claim="${selectedId}"]`)?.scrollIntoView({block:"center"}); };
      row.addEventListener("click", open);
      row.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") {event.preventDefault();open();} });
    });
  }
  bindSources();
  setBusy(busy);
}

function metricsHTML() {
  const m = state.metrics || {total:0, located:0, reviewed:0, supported:0};
  const items = [
    ['book','文献资料',state.corpus.documents.length,'篇',''],
    ['file','正文陈述',m.total,'句',''],
    ['check','引用定位',m.located,`/ ${m.total}`,'green'],
    ['clock','核验队列',m.total-m.reviewed,'句','amber']
  ];
  return `<div class="stats-row">${items.map(([glyph,label,value,unit,tone])=>`
    <div class="stat"><span class="stat-icon">${icon(glyph)}</span><div class="stat-copy">
      <span class="stat-label">${label}</span><div class="stat-value ${tone}">${value}<small>${unit}</small></div>
    </div></div>`).join('')}</div>`;
}

function workspaceHTML() {
  const run = state.run, claims = run?.claims || [];
  const mode = preferredMode ?? (state.config.key_set || state.config.visitor_key ? "model" : "extract");
  const canRevise = !!(run?.article && run.mode === "model" && claims.length);
  // Older versions saved empty drafts as successful chat messages. Reopen their populated composer.
  const emptySavedDraft = run && !claims.length && !pendingTurn && !busy;
  const showChat = !emptySavedDraft && !newReviewMode && (chatExpanded || canRevise || run?.conversation?.length);
  const intro = `<section class="writing-intro ${showChat?"chat-mode":`compose-mode ${claims.length?"":"is-empty"}`}" ${showChat?'':'aria-labelledby="writingIntroTitle"'}>
    ${showChat?"":`
    <div class="writing-intro-heading"><h2 id="writingIntroTitle">从文献出发，<span>让洞见成篇。</span></h2></div>`}
    ${showChat ? revisionComposerHTML(run) : generationComposerHTML(run,mode,canRevise)}</section>`;
  if(!claims.length) return intro;
  return intro + `<div class="document-bar" id="reviewDocument"><div><span class="document-dot"></span>当前稿件${state.demo?'<span class="sample-label">示例</span>':""}</div><div class="document-actions"><span class="word-count">${(run.writing?.character_count ?? claims.reduce((n,c)=>n+Array.from(c.text).filter(char=>/[\p{L}\p{N}]/u.test(char)).length,0)).toLocaleString()} 字</span><button class="text-button" id="toLedger">查看引用 ${icon("arrow")}</button></div></div>
    <div class="review-layout has-evidence"><div class="review-document"><section class="paper-panel">${reviewHeaderHTML(run)+reviewBodyHTML()}</section>
    ${run?.mode==="model" && run.gaps.length?`<details class="research-notes"><summary>研究备忘</summary><div>${run.gaps.map(g=>`<p>${esc(g)}</p>`).join("")}</div></details>`:""}
    </div>
    ${claims.length?`<aside class="inspector ${mobileEvidenceOpen?"mobile-expanded":""}" id="evidencePanel" aria-label="原文证据"><div class="evidence-heading"><h2 id="inspectorTitle">原文证据</h2><button class="text-button mobile-evidence-toggle" id="toggleEvidence" aria-expanded="${mobileEvidenceOpen}">${mobileEvidenceOpen?"收起":"展开"}</button></div><div class="inspector-body" id="inspectorBody" aria-live="polite">${inspectorHTML()}</div></aside>`:""}</div>`;
}

function generationComposerHTML(run,mode,canRevise) {
  return `<form class="composer" id="generateForm">
      ${canRevise?'<button type="button" class="text-button return-edit" id="returnToEdit">← 返回修改当前稿</button>':""}
      <div class="composer-field"><label for="topicInput" class="composer-label">研究主题</label>
      <textarea class="topic-input" id="topicInput" required maxlength="1000" rows="1" placeholder="你想研究什么？">${esc(writingDraft?.topic ?? run?.topic ?? "")}</textarea></div>
      <div class="composer-field"><label for="requirementsInput" class="composer-label requirements-label">写作要求</label><textarea class="requirements-input" id="requirementsInput" maxlength="4000" rows="2" placeholder="例如：不少于 800 字，比较研究方法与主要结论">${esc(writingDraft?.requirements ?? run?.requirements ?? "")}</textarea></div>
      <div class="composer-bottom"><div class="composer-tools"><button type="button" class="text-button" id="composerImport" data-mutation>${icon("plus")}文献 <span>${state.corpus.documents.length}</span></button><select class="mode-select" id="modeSelect" aria-label="生成模式"><option value="model" ${mode==="model"?"selected":""}>AI 综述</option><option value="extract" ${mode==="extract"?"selected":""}>原文摘录</option></select></div><button class="button primary generate-button" id="generateButton" data-mutation type="submit">${canRevise?"重新生成全文":"生成综述"} ${icon("arrow")}</button></div>
    </form>`;
}

function revisionComposerHTML(run) {
  return `<section class="chat-panel" aria-label="研究对话">
    <div class="chat-header"><div class="chat-heading"><span class="chat-emblem">${icon("spark")}</span><h2>研究对话</h2><span class="chat-source-count">${state.corpus.documents.length} 篇文献</span></div><div class="chat-header-actions">${run?.revisions?.length?'<button type="button" class="text-button" id="undoEdit" data-mutation>撤销修改</button>':""}<button type="button" class="text-button" id="newReviewButton">${icon("plus")}新对话</button></div></div>
    <div class="conversation-surface"><div class="chat-messages" id="chatMessages" data-thread-id="${esc(activeThread)}" role="log" aria-label="对话记录" aria-live="polite">${chatMessagesHTML(run)}</div>
    <form class="chat-input-form" id="revisionForm"><label for="revisionInput" class="visually-hidden">发送消息</label>
      <textarea id="revisionInput" class="chat-input" maxlength="4000" required rows="2" placeholder="继续提问，或告诉我想改哪一处…">${esc(revisionDraft)}</textarea>
      <div class="chat-input-bottom"><button type="button" class="text-button" id="composerImport" data-mutation>${icon("plus")}添加文献</button><div><span class="chat-key-hint">Enter 发送 · Shift + Enter 换行</span><button class="chat-send" id="reviseButton" type="submit" data-mutation aria-label="发送消息">${icon("arrow")}</button></div></div>
    </form></div>
  </section>`;
}

function reviewHeaderHTML(run) {
  return `<div class="paper-heading"><h3>${esc(run.article?.title || run.topic)}</h3></div>`;
}

function claimHTML(claim) {
  return `<button class="claim ${claim.id === selectedId ? "selected" : ""}" data-claim="${esc(claim.id)}" aria-pressed="${claim.id === selectedId}"><div class="claim-top"><span class="sentence-id">${esc(claim.id)}</span>${status(claim)}</div><p class="claim-text">${esc(claim.text)}</p><div class="citation-row">${claim.evidence.map(e=>`<span class="citation-chip">${icon("file")}${esc(e.filename || "未知来源")} · p.${esc(e.page)}</span>`).join("") || '<span>尚无引用证据</span>'}</div></button>`;
}

function articleReferences() {
  const cited = new Set((state.run?.claims || []).flatMap(c => c.evidence.map(e => e.source_id)));
  return state.corpus.documents.filter(doc => cited.has(doc.id));
}

function reviewBodyHTML() {
  const run = state.run;
  if (!run.article) return `<div class="claims">${run.claims.map(claimHTML).join("")}</div>`;
  const claims = new Map(run.claims.map(c => [c.id, c]));
  const references = articleReferences();
  const numbers = new Map(references.map((doc, index) => [doc.id, index + 1]));
  const sentence = id => {
    const claim = claims.get(id);
    if (!claim) return "";
    const citations = [...new Set(claim.evidence.map(e => numbers.get(e.source_id)).filter(Boolean))].sort((a,b)=>a-b);
    return `<span class="article-sentence ${(run.last_edit?.added_ids?.includes(id) || run.last_edit?.updated_ids?.includes(id))?"edited":""} ${id === selectedId ? "selected" : ""} ${isLocated(claim) ? "" : "unlocated"}" data-claim="${esc(id)}" role="button" tabindex="0" aria-pressed="${id === selectedId}" title="${esc(id)} · 点击查看原文证据">${esc(claim.text)}<sup>${citations.map(n=>`[${n}]`).join("") || "[?]"}</sup></span>`;
  };
  return `<article class="review-article">${run.article.sections.map(section => `<section class="review-section"><h4>${esc(section.title)}</h4>${section.paragraphs.map(paragraph => `<p>${paragraph.map(sentence).join("")}</p>`).join("")}</section>`).join("")}<section class="article-references"><h4>文献来源</h4>${references.map((doc,index)=>`<button data-source="${esc(doc.id)}"><span>[${index+1}]</span>${esc(doc.filename)}${icon("arrow")}</button>`).join("")}<p></p></section></article>`;
}

function evidenceHTML(evidence, index) {
  const doc = state.corpus.documents.find(d=>d.id === evidence.source_id);
  const context = evidenceContextHTML(evidence, false);
  return `<section class="source-evidence"><div class="evidence-meta">
    <span class="file-icon ${doc?.filename.toLowerCase().endsWith('.pdf')?'':'txt'}">${icon('file')}</span>
    <div class="evidence-file"><strong>${esc(evidence.filename || '未知来源')}</strong></div>
    <span class="evidence-page">P.${esc(evidence.page)}</span></div>
    <div class="evidence-context" id="evidenceContext${index}">${context}</div>
    <div class="evidence-check ${evidence.valid?'':'invalid'}">${icon(evidence.valid?'check':'close')}${evidence.valid?'':esc(evidence.reason)}
      ${doc?`<button type="button" data-read-evidence="${index}" aria-expanded="false" aria-controls="evidenceContext${index}">展开本页 ↓</button>`:''}
    </div>
    ${evidence.valid?`<details class="evidence-details"><summary>定位信息</summary><code>PAGE ${evidence.page} · CHAR ${evidence.start}–${evidence.end}</code></details>`:''}
  </section>`;
}

function evidenceContextHTML(evidence, fullPage) {
  const doc = state.corpus.documents.find(d=>d.id === evidence.source_id);
  const page = doc?.pages.find(p=>p.number === evidence.page);
  if(!evidence.valid || !page) return esc(evidence.quote || "空引文");
  const start = fullPage ? 0 : Math.max(0,evidence.start-50);
  const end = fullPage ? page.text.length : evidence.end+50;
  return esc(page.text.slice(start,evidence.start))+`<mark>${esc(page.text.slice(evidence.start,evidence.end))}</mark>`+esc(page.text.slice(evidence.end,end));
}

function bindEvidenceReaders() {
  $$("[data-read-evidence]").forEach(button=>button.onclick=()=>{
    const index = Number(button.dataset.readEvidence);
    const evidence = selectedClaim()?.evidence[index];
    if(!evidence) return;
    const expanded = button.getAttribute("aria-expanded") !== "true";
    $("#evidenceContext"+index).innerHTML = evidenceContextHTML(evidence,expanded);
    button.setAttribute("aria-expanded",expanded);
    button.textContent = expanded ? "收起本页 ↑" : "展开本页 ↓";
  });
}

function inspectorHTML() {
  const claim=selectedClaim();
  if(!claim)return `<div class="empty-state"><div class="empty-icon">${icon('shield')}</div><h3>连接正文与来源</h3><p>点击正文句子，查看它的原文出处。</p></div>`;
  pendingLabel=claim.label;
  return `<div class="selected-claim"><div class="mini-label"><span class="sentence-id">${esc(claim.id)}</span>${status(claim)}</div><p>${esc(claim.text)}</p></div>
    ${claim.evidence.map(evidenceHTML).join('') || '<div class="connection-result error">未关联原文</div>'}
    <details class="review-details"><summary>核验与批注</summary><form class="review-form" id="reviewForm"><div class="label-options">
      ${['supported','partial','contradicted','insufficient'].map(label=>`<button type="button" class="label-option ${claim.label===label?'selected':''}" data-label="${label}" aria-pressed="${claim.label===label}">${esc(state.labels[label])}</button>`).join('')}
      </div><label class="field-label" for="reviewer">核验者</label><input id="reviewer" value="${esc(claim.reviewer)}" placeholder="姓名或编号" maxlength="100">
      <label class="field-label" for="reviewNote">备注</label><textarea id="reviewNote" placeholder="补充核验依据（可选）" maxlength="5000">${esc(claim.note)}</textarea>
      <button class="button primary review-submit" type="submit" data-mutation>${icon('check')}保存核验</button>
    </form></details>
    ${claim.history.length?`<div class="history">核验记录 · ${claim.history.length}${claim.history.slice(-3).reverse().map(h=>`<div class="history-row">${esc(h.reviewer || '匿名')} → ${esc(state.labels[h.label])}${h.note?`<br>${esc(h.note)}`:''}</div>`).join('')}</div>`:''}`;
}

function bindWorkspace() {
  if ($("#toLedger")) $("#toLedger").onclick = () => {activeView="ledger";render();};
  $("#composerImport").onclick = chooseFiles;
  const toggle = $("#toggleEvidence");
  if(toggle) toggle.onclick = () => {
    mobileEvidenceOpen = !mobileEvidenceOpen;
    $("#evidencePanel").classList.toggle("mobile-expanded",mobileEvidenceOpen);
    toggle.textContent = mobileEvidenceOpen ? "收起" : "展开";
    toggle.setAttribute("aria-expanded",mobileEvidenceOpen);
  };
  if($("#revisionForm")) {
    $("#revisionInput").oninput=()=>{revisionDraft=$("#revisionInput").value;browserStore?.saveDraft(activeThread,{writingDraft,revisionDraft,preferredMode});};
    $("#revisionInput").onkeydown=event=>{
      if(event.key==="Enter" && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.isComposing) {
        event.preventDefault();if(!busy) $("#revisionForm").requestSubmit();
      }
    };
    $("#newReviewButton").onclick=createThread;
    $$("[data-process-key]").forEach(details=>details.ontoggle=()=>{
      if(details.open) expandedProcesses.add(details.dataset.processKey);else expandedProcesses.delete(details.dataset.processKey);
    });
    if($("#retryChat")) $("#retryChat").onclick=()=>retryRequest && mutate(retryRequest.path,retryRequest.body);
    bindChatScroll();
    if($("#undoEdit")) $("#undoEdit").onclick=()=>mutate("/api/undo",{base_revision:state.run.revision_number || 0},"已撤销上次修改");
    $("#revisionForm").onsubmit=async event=>{
      event.preventDefault();if(busy)return;
      const instruction=$("#revisionInput").value.trim();
      if(!instruction)return;
      await mutate("/api/revise",{instruction,selected_id:selectedId,base_revision:state.run.revision_number || 0});
    };
  }
  if($("#generateForm")) {
    if($("#returnToEdit")) $("#returnToEdit").onclick=()=>{newReviewMode=false;chatExpanded=true;render();};
    const fitField = input => {
      input.style.height="auto";
      input.style.height=Math.min(input.scrollHeight,input.id==="topicInput"?160:200)+"px";
    };
    const keepDraft = event => { fitField(event.target); writingDraft = {topic:$("#topicInput").value,requirements:$("#requirementsInput").value};browserStore?.saveDraft(activeThread,{writingDraft,revisionDraft,preferredMode}); };
    $("#topicInput").oninput = keepDraft;
    $("#requirementsInput").oninput = keepDraft;
    fitField($("#topicInput"));fitField($("#requirementsInput"));
    $("#modeSelect").onchange = () => {
      preferredMode = $("#modeSelect").value;
      $("#requirementsInput").disabled = preferredMode !== "model";
      if(!busy)$("#generateButton").innerHTML=(preferredMode === "model" ? (state.run?.mode==="model"?"重新生成全文":"生成综述") : "提取原文")+icon("arrow");
    };
    $("#modeSelect").dispatchEvent(new Event("change"));
    $("#generateForm").onsubmit = async event => {
      event.preventDefault();
      if (busy) return;
      const topic = $("#topicInput").value.trim(), mode = $("#modeSelect").value;
      if (!topic) return toast("先写下你的研究主题。",true);
      if (mode === "model" && (!state.config.key_set || !state.config.model || !state.config.base)) {
        toast("先连接模型，再开始 AI 生成。",true); openSettings(); return;
      }
      if(!allowReplace()) return;
      const requirements = $("#requirementsInput").value.trim();
      const submittedThread=activeThread;
      if(await mutate("/api/generate", {topic,mode,requirements}) && submittedThread===activeThread) {
        const issues = state.run?.writing?.issues || [];
        toast(issues.length ? "已生成稿件；"+issues.join(" ") : mode === "extract" ? "摘录已生成" : "综述已生成", issues.length>0);
      }
    };
  }
  $$("[data-claim]").forEach(button => { const select = () => {
    selectedId = button.dataset.claim;
    mobileEvidenceOpen = true;
    $("#evidencePanel").classList.add("mobile-expanded");
    $("#toggleEvidence").textContent = "收起";
    $("#toggleEvidence").setAttribute("aria-expanded","true");
    $$("[data-claim]").forEach(b=>{ b.classList.toggle("selected",b.dataset.claim===selectedId); b.setAttribute("aria-pressed",b.dataset.claim===selectedId); });
    const expanded = $(".review-details")?.open;
    $("#inspectorBody").innerHTML = inspectorHTML();
    if(expanded) $(".review-details").open = true;
    $("#inspectorBody").scrollTop = 0;
    bindReview(); bindEvidenceReaders();
  }; button.onclick = select; button.onkeydown = event => {if(event.key === "Enter" || event.key === " "){event.preventDefault();select();}}; });
  bindReview();
  bindEvidenceReaders();
}

function bindReview() {
  $$("[data-label]").forEach(button => button.onclick = () => {
    pendingLabel = button.dataset.label;
    $$("[data-label]").forEach(b=>{ b.classList.toggle("selected",b.dataset.label===pendingLabel); b.setAttribute("aria-pressed",b.dataset.label===pendingLabel); });
  });
  const form = $("#reviewForm");
  if (form) form.onsubmit = async event => {
    event.preventDefault();
    if (pendingLabel === "pending") return toast("请先选择一个核验判定。",true);
    await mutate("/api/annotate", {id:selectedId,label:pendingLabel,reviewer:$("#reviewer").value,note:$("#reviewNote").value}, "核验结果已保存");
  };
}

function sourcesHTML() {
  return `<div class="library-toolbar"><div><h2 class="section-title">${state.corpus.documents.length} 篇文献</h2></div><button class="button secondary" id="libraryImport" data-mutation>${icon("upload")}添加文献</button></div><div class="source-grid">${state.corpus.documents.map((doc,index)=>`<article class="source-card"><div class="source-card-top"><span class="file-icon ${doc.filename.toLowerCase().endsWith(".pdf")?"":"txt"}">${icon("file")}</span><span class="sentence-id">${String(index+1).padStart(2,"0")}</span></div><h3>${esc(doc.filename)}</h3><p>${esc(doc.pages[0]?.text.slice(0,180))}</p><div class="source-card-footer"><span>${doc.pages.length} 页 · ${doc.pages.reduce((n,p)=>n+p.text.length,0).toLocaleString()} 字符</span><button data-source="${esc(doc.id)}">阅读原文 ${icon("arrow")}</button></div></article>`).join("")}</div><div class="upload-zone" id="uploadZone" role="button" tabindex="0" aria-label="拖拽或选择文献上传">${icon("upload")}<strong>拖拽文献到这里，或点击选择文件</strong><small>PDF / TXT / Markdown</small></div>${state.corpus.warnings.length?`<div class="gaps-box"><h3>导入提示</h3>${state.corpus.warnings.map(w=>`<p>${esc(w)}</p>`).join("")}</div>`:""}`;
}

function bindLibrary() {
  $("#libraryImport").onclick = chooseFiles;
  const zone = $("#uploadZone");
  zone.onclick = chooseFiles;
  zone.onkeydown = event => {if(event.key === "Enter" || event.key === " "){event.preventDefault();chooseFiles();}};
  zone.ondragover = event => {event.preventDefault();zone.classList.add("dragging");};
  zone.ondragleave = () => zone.classList.remove("dragging");
  zone.ondrop = event => {event.preventDefault();zone.classList.remove("dragging");uploadFiles([...event.dataTransfer.files]);};
}

function ledgerHTML() {
  const claims = (state.run?.claims || []).filter(c=>filter==="all" || (filter==="invalid"?!isLocated(c):c.label===filter));
  const m = state.metrics;
  return `${metricsHTML()}<div class="library-toolbar"><div><h2 class="section-title">核验记录</h2></div><select class="filter-select" id="ledgerFilter" aria-label="按核验状态筛选">${[["all","全部状态"],...[...Object.entries(state.labels)], ["invalid","定位未通过"]].map(([key,value])=>`<option value="${esc(key)}" ${filter===key?"selected":""}>${esc(value)}</option>`).join("")}</select></div><div class="table-panel"><table class="ledger-table"><thead><tr><th>编号</th><th>陈述</th><th>原文定位</th><th>核验判定</th><th>核验者</th></tr></thead><tbody>${claims.map(c=>`<tr data-ledger-claim="${esc(c.id)}" tabindex="0"><td class="sentence-id">${esc(c.id)}</td><td class="ledger-text">${esc(c.text)}</td><td><span class="location-dot ${isLocated(c)?"":"invalid"}">${isLocated(c)?"● 已定位":"● 未通过"}</span></td><td>${status(c)}</td><td>${esc(c.reviewer || "—")}</td></tr>`).join("") || '<tr><td colspan="5">暂无符合筛选条件的陈述。</td></tr>'}</tbody></table></div><p class="ledger-note">${m?`${m.reviewed} / ${m.total} 条已核验 · ${m.supported} 条充分支持 · 引用定位率 ${percentage(m.location_rate)}`:"生成草稿后，这里会显示逐句核验记录。"}</p>`;
}

function bindSources() {
  $$("[data-source]").forEach(button=>button.onclick=()=>showSource(button.dataset.source));
}

function showSource(id) {
  const doc = state.corpus.documents.find(d=>d.id===id);
  if(!doc) return;
  const reader = $("#sourceReader"), dialog = $("#sourceDialog");
  const preserveLines = !/\.pdf$/i.test(doc.filename);
  $("#sourceTitle").textContent=doc.filename;
  $("#sourcePageCount").textContent=`${doc.pages.length} 页 · 提取文本`;
  $("#readerPreserveLines").checked=preserveLines;
  reader.classList.toggle("preserve-lines",preserveLines);
  // Reflow is display-only: the stored text, quotations and evidence offsets stay exact.
  reader.innerHTML=doc.pages.map(page=>{
    const text = page.text || "此页未提取到文字。";
    const paragraphs = text.replace(/\r\n?/g,"\n").split(/\n[\t ]*\n/).filter(p=>p.trim());
    return `<section class="reader-page"><h3>第 ${page.number} 页</h3><div class="reader-flow">${paragraphs.map(p=>`<p>${esc(p)}</p>`).join("")}</div><pre class="reader-raw">${esc(text)}</pre></section>`;
  }).join("")+`<div class="reader-hash">SHA-256<br>${esc(doc.file_sha256)}</div>`;
  dialog.showModal();
  dialog.scrollTop=0;
  reader.scrollTo({top:0,behavior:"instant"});
}

$("#readerPreserveLines").onchange = event => {
  const reader=$("#sourceReader"), top=reader.getBoundingClientRect().top;
  const page=[...reader.querySelectorAll(".reader-page")].find(p=>p.getBoundingClientRect().bottom>top);
  const before=page?.getBoundingClientRect();
  const fraction=before?Math.max(0,Math.min(1,(top-before.top)/before.height)):0;
  reader.classList.toggle("preserve-lines",event.target.checked);
  if(page){
    const after=page.getBoundingClientRect();
    reader.scrollTo({top:reader.scrollTop+after.top-top+fraction*after.height,behavior:"instant"});
  }
};

function chooseFiles() { if(!busy) $("#fileInput").click(); }

async function uploadFiles(files) {
  if (!files.length || busy) return;
  if (files.reduce((n,f)=>n+f.size,0)>35*1024*1024) return toast("单次文件合计请控制在 35 MB 以内。",true);
  if(files.some(f=>!/[.](pdf|txt|md)$/i.test(f.name))) return toast("请选择 PDF、TXT 或 Markdown 文献。",true);
  if(!allowReplace()) return;
  setBusy(true);
  const importId=activeThread,importContext=stashView();importContext.localRequest=true;
  toast("正在提取文献文字…");
  try {
    const encoded=[];
    for(const file of files) {
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(",")[1]);reader.onerror=()=>reject(new Error("无法读取文件："+file.name));reader.readAsDataURL(file);});
      encoded.push({name:file.name,data});
    }
    importContext.state=await request("/api/import",{files:encoded},false,importId);
    Object.assign(importContext,{unexported:false,writingDraft:undefined,chatExpanded:false,pendingTurn:undefined,retryRequest:undefined,newReviewMode:false,revisionDraft:"",activeView:"sources",selectedId:undefined});
    threadList=importContext.state.threads;
    toast(`已导入 ${importContext.state.corpus.documents.length} 篇文献。`);
  }catch(error){toast(error.message,true);}finally{
    importContext.busy=false;importContext.localRequest=false;
    if(activeThread===importId)activateView(importId,importContext);
    $("#fileInput").value="";renderThreads();
  }
}

function openSettings() {
  const visitor=!!state.config.visitor_key;
  $("#apiBase").value=state.config.base;
  $("#apiBase").readOnly=visitor;
  $("#apiModel").value=state.config.model;
  $("#apiKey").value="";
  $("#clearKey").checked=false;
  $("#persistConfig").checked=!visitor;
  $("#persistConfig").closest("label").hidden=visitor;
  $("#visitorKeyNote").hidden=!visitor;
  $("#visitorKeyNote").textContent=browserStore?"使用你自己的 DeepSeek Key，费用由你的 DeepSeek 账户承担。密钥仅保留在当前页面内存中，刷新后需重新填写；生成时临时发送到服务器使用。":"使用你自己的 DeepSeek Key，费用由你的 DeepSeek 账户承担。密钥仅保留在服务器内存中，重启后需重新填写。";
  $("#apiKey").placeholder=state.config.key_set?"留空保留现有密钥":"填写你的 DeepSeek API Key";
  $("#connectionResult").hidden=true;
  $("#keyHint").textContent=state.config.key_set?"已设置，留空则保留":"尚未设置";
  $("#settingsDialog").showModal();
}

function allowReplace() {
  return !unexported || !state.run?.claims.some(c=>c.history?.length) || window.confirm("当前核验记录尚未导出。继续替换项目？");
}

$$("[data-view]").forEach(button=>button.onclick=()=>{activeView=button.dataset.view;render();});

$("#fileInput").onchange=event=>uploadFiles([...event.target.files]);
$("#demoButton").onclick=async()=>{ $(".project-menu").open=false;if(!allowReplace())return;if(await mutate("/api/demo",{},"示例项目已打开")){unexported=false;activeView="workspace";selectedId=undefined;render();}};
$("#settingsButton").onclick=()=>state&&openSettings();

$("#settingsForm").onsubmit=async event=>{
  event.preventDefault();
  if(await mutate("/api/config",readSettings(),"模型设置已保存")){
    $("#apiKey").value="";$("#settingsDialog").close();
  }
};
function readSettings() {
  return {base:$("#apiBase").value,model:$("#apiModel").value,key:$("#apiKey").value,
          clear_key:$("#clearKey").checked,persist:$("#persistConfig").checked};
}
$("#testConnection").onclick=async()=>{
  if(busy)return;
  const result=$("#connectionResult");
  setBusy(true);result.hidden=false;result.className="connection-result";result.textContent="正在验证密钥与模型…";
  try {
    state=await request("/api/config",readSettings());
    $("#apiKey").value="";
    state=await request("/api/connection",{});
    result.className="connection-result success";
    result.textContent=`已连接 · ${state.config.connection.model}`;
    render();
  } catch(error) {result.className="connection-result error";result.textContent=error.message;}
  finally {setBusy(false);}
};
$$(".close-dialog").forEach(button=>button.onclick=()=>button.closest("dialog").close());
$("#restoreButton").onclick=()=>{ $(".project-menu").open=false; $("#ledgerInput").click(); };
$("#ledgerInput").onchange=async event=>{
  const file=event.target.files[0];if(!file)return;
  try{if(file.size>MAX_LEDGER_SIZE)throw new Error("账本文件过大，请选择小于 35 MB 的账本。");if(!allowReplace())return;const run=JSON.parse(await file.text());if(await mutate("/api/restore",{run},"账本已恢复，证据定位已重新检查。")){unexported=false;activeView="workspace";selectedId=undefined;render();}}
  catch(error){toast(error.message,true);}finally{event.target.value="";}
};
const MAX_LEDGER_SIZE=35*1024*1024;
$("#exportButton").onclick=async()=>{
  if(!state?.run||busy)return;
  $("#exportButton").disabled=true;
  try{const blob=await request("/api/export",{},true);const url=URL.createObjectURL(blob);const a=document.createElement("a");a.href=url;a.download="循证综述-证据账本.zip";document.body.append(a);a.click();a.remove();unexported=false;setTimeout(()=>URL.revokeObjectURL(url),10000);toast("综述与引用账本已导出");}
  catch(error){toast(error.message,true);}finally{$("#exportButton").disabled=false;}
};

// The presentation gate keeps the complete layout hidden until this view exists.
document.addEventListener("pointerdown",event=>{
  const menu=$(".project-menu");
  if(menu.open && !menu.contains(event.target)) menu.open=false;
});
document.addEventListener("keydown",event=>{
  if((event.ctrlKey || event.metaKey) && event.key === "Enter" && activeView === "workspace" && !busy && !$("dialog[open]")) {
    event.preventDefault();($("#revisionForm") || $("#generateForm"))?.requestSubmit();
  }
});
$("#createThread").onclick=createThread;
window.addEventListener("trace-storage-error",event=>{
  const badge=$("#localStorageStatus");badge.hidden=false;badge.textContent="尚未保存";badge.title=event.detail;
  toast(event.detail,true);
});
async function initializeWorkspace() {
  const response=await fetch("/api/bootstrap");
  if(response.ok){
    const boot=await response.json();
    if(boot.browser_storage){browserStore=await new window.TraceBrowserStorage(boot.sample).open();$("#localStorageStatus").hidden=false;}
  }else if(response.status!==404)throw new Error("暂时无法连接站点，请稍后刷新。");
  try{return await request("/api/state");}catch(error){if(activeThread==="main")throw error;activeThread="main";return request("/api/state");}
}
initializeWorkspace().then(data=>{
  threadList=data.threads;const context=receivedView(data);threadViews.set(data.thread_id,context);activateView(data.thread_id,context);refreshThreads();
}).catch(error=>{
  $("#viewContent").innerHTML=`<div class="empty-state"><div class="empty-icon">${icon("settings")}</div><h3>研究空间暂时无法打开</h3><p>${esc(error.message)}<br>请检查网络及浏览器站点存储权限，再刷新页面。</p></div>`;
}).finally(()=>{
  if(window.TracePresentation)window.TracePresentation.ready();
  else {
    document.body.classList.remove("studio-loading");
    $("#bootScreen").hidden=true;$("#studioShell").setAttribute("aria-busy","false");
    document.body.classList.remove("landing-open");$("#opening").hidden=true;$("#studioShell").inert=false;
  }
});
