"use strict";

// Large manuscripts live in IndexedDB. Only an active processing job visits the server.
window.TraceBrowserStorage = class TraceBrowserStorage {
  constructor(sample) {
    this.sample=sample;this.db=null;this.pending=new Map();this.progress=new Map();
    this.unsaved=new Map();this.draftTimers=new Map();
    this.config={...sample.config,key:"",key_set:false,connection:null,visitor_key:true,browser_storage:true};
  }
  async open() {
    if(window.isSecureContext===false)throw new Error("当前连接不是安全连接，请使用 https://review.njfu.org/ 打开研究空间。");
    if(!window.indexedDB)throw new Error("浏览器未开放站点存储。请在浏览器设置中允许此站点保存数据，再重新打开。");
    if(!navigator.locks)throw new Error("当前浏览器未提供多标签页协调功能。请更新浏览器，并使用普通窗口打开 HTTPS 地址；Edge 请退出 IE 兼容模式。");
    this.db=await new Promise((resolve,reject)=>{
      const request=indexedDB.open("trace-review-workspace",1);
      request.onupgradeneeded=()=>{for(const name of ["records","summaries","meta"])request.result.createObjectStore(name,{keyPath:"id"});};
      request.onsuccess=()=>resolve(request.result);
      request.onerror=()=>reject(new Error("无法打开浏览器存储，请允许此站点保存数据。"));
      request.onblocked=()=>reject(new Error("请关闭其他循证页面，再重新打开研究空间。"));
    });
    this.db.onversionchange=()=>this.db.close();
    await this.meta("storage-check",{at:Date.now()});
    const preferences=await this.read("meta","preferences");
    if(preferences)Object.assign(this.config,preferences.value);
    await navigator.locks.request("trace-review-migration",async()=>{
      if(!(await this.read("meta","legacy-migrated"))){
        const legacy=await this.remote("/api/browser/legacy");
        for(const id of legacy.threads){
          if(await this.read("records",id))continue;
          const data=await this.remote("/api/browser/legacy/"+id);
          await this.write(this.record(id,data,data.created_at));
        }
        await this.meta("legacy-migrated",{at:Date.now()});
      }
      if(!(await this.read("records","main")))await this.write(this.record("main",this.sample));
    });
    return this;
  }
  storageError(error) {
    return new Error(error?.name==="QuotaExceededError"?"浏览器存储空间不足。新结果暂存于当前页面，请先导出备份；不要关闭页面。":"浏览器未能保存数据。请先导出备份，并检查站点存储权限。");
  }
  read(store,id) {
    return new Promise((resolve,reject)=>{
      const tx=this.db.transaction(store,"readonly"),request=tx.objectStore(store).get(id);
      request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(this.storageError(request.error));
    });
  }
  all(store) {
    return new Promise((resolve,reject)=>{
      const request=this.db.transaction(store,"readonly").objectStore(store).getAll();
      request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(this.storageError(request.error));
    });
  }
  meta(id,value) {
    return new Promise((resolve,reject)=>{
      const tx=this.db.transaction("meta","readwrite");
      if(value===undefined)tx.objectStore("meta").delete(id);else tx.objectStore("meta").put({id,value});
      tx.oncomplete=()=>resolve();tx.onerror=()=>reject(this.storageError(tx.error));tx.onabort=()=>reject(this.storageError(tx.error));
    });
  }
  snapshot(data) {
    const run=data.run || null;
    return {run,corpus:run?.corpus || data.corpus || {documents:[],warnings:[]},demo:!!data.demo,metrics:data.metrics || null};
  }
  record(id,data,createdAt) {
    return {id,created_at:createdAt || new Date().toISOString(),version:0,snapshot:this.snapshot(data),pending:null};
  }
  summary(record) {
    return {id:record.id,version:record.version,created_at:record.created_at,title:record.pending?.topic || record.snapshot.run?.topic || "新的研究",
      status:record.pending?.progress?.status || "idle",has_document:!!record.snapshot.run,source_count:record.snapshot.corpus.documents.length};
  }
  write(record,expected) {
    return new Promise((resolve,reject)=>{
      const tx=this.db.transaction(["records","summaries"],"readwrite"),records=tx.objectStore("records");
      let conflict=false;
      const lookup=records.get(record.id);
      lookup.onsuccess=()=>{
        if(expected!==undefined && lookup.result?.version!==expected){conflict=true;tx.abort();return;}
        record.version=(lookup.result?.version || 0)+1;
        records.put(record);tx.objectStore("summaries").put(this.summary(record));
      };
      tx.oncomplete=()=>resolve(record);
      tx.onerror=()=>reject(this.storageError(tx.error));
      tx.onabort=()=>reject(conflict?new Error("另一页面已更新此研究，请重新选择该会话后继续。"):this.storageError(tx.error));
    });
  }
  async list() {return (await this.all("summaries")).sort((a,b)=>(a.id!=="main")-(b.id!=="main") || a.created_at.localeCompare(b.created_at));}
  async remote(path,body,binary=false) {
    const options={headers:{}};
    if(body!==undefined)Object.assign(options,{method:"POST",headers:{"Content-Type":"application/json","X-Trace-Request":"1"},body:JSON.stringify(body)});
    const response=await fetch(path,options);
    if(!response.ok){const data=await response.json().catch(()=>({}));const error=new Error(data.error || "暂时无法连接处理服务，浏览器中的原稿仍保留。");error.status=response.status;throw error;}
    return binary?response.blob():response.json();
  }
  async state(id) {
    const record=this.unsaved.get(id) || await this.read("records",id);
    if(!record)throw new Error("这个浏览器中没有该研究。");
    if(record.pending?.job_id && record.pending.progress.status==="running" && !this.pending.has(id)){
      const recovery=navigator.locks.request("trace-review-thread:"+id,async()=>{
        const current=await this.read("records",id);
        if(current?.pending?.job_id)await this.settle(current);
      }).catch(error=>window.dispatchEvent(new CustomEvent("trace-storage-error",{detail:error.message}))).finally(()=>this.pending.delete(id));
      this.pending.set(id,recovery);
    }
    const progress=this.progress.get(record.pending?.job_id) || record.pending?.progress || {status:"idle",events:[],operation_id:null};
    const draft=await this.read("meta","draft:"+id);
    return {...record.snapshot,labels:this.sample.labels,config:{...this.config,key:undefined},thread_id:id,
      threads:await this.list(),progress,pending_request:record.pending?{kind:record.pending.kind,content:record.pending.content}:null,
      browser_storage:true,storage_version:record.version,storage_warning:record.storage_warning,draft:draft?.value};
  }
  saveDraft(id,value) {
    clearTimeout(this.draftTimers.get(id));
    this.draftTimers.set(id,setTimeout(()=>this.meta("draft:"+id,value).catch(error=>window.dispatchEvent(new CustomEvent("trace-storage-error",{detail:error.message}))),250));
  }
  async clearDraft(id) {clearTimeout(this.draftTimers.get(id));await this.meta("draft:"+id);}
  async request(path,body,binary,id) {
    if(path==="/api/threads")return {threads:await this.list()};
    if(path==="/api/state")return this.state(id);
    if(path==="/api/progress"){
      const record=await this.read("records",id);
      return this.progress.get(record?.pending?.job_id) || record?.pending?.progress || {status:"idle",events:[]};
    }
    if(path==="/api/config"){
      await this.meta("preferences",{base:"https://api.deepseek.com",model:body.model || this.sample.config.model});
      this.config={...this.config,base:"https://api.deepseek.com",model:body.model || this.sample.config.model,
        key:body.clear_key?"":body.key?.trim() || this.config.key,connection:null};
      this.config.key_set=!!this.config.key;return this.state(id);
    }
    if(path==="/api/threads/new"){
      const source=this.unsaved.get(id) || await this.read("records",id);
      const freshId=crypto.randomUUID().replaceAll("-","").slice(0,24);
      await this.write(this.record(freshId,{...source.snapshot,run:null,metrics:null}));
      return this.state(freshId);
    }
    if(path==="/api/connection" || path==="/api/export"){
      const record=this.unsaved.get(id) || await this.read("records",id);
      const output=await this.readonlyTask(path,body,path==="/api/export"?record.snapshot:{});
      if(path==="/api/export")return output;
      this.config.connection=output.connection;return this.state(id);
    }
    if(this.unsaved.has(id))throw new Error("当前结果还没有保存成功，请先导出备份，再恢复浏览器存储空间。");
    const task=navigator.locks.request("trace-review-thread:"+id,async()=>{
      let record=await this.read("records",id);
      if(record.pending?.progress.status==="running")throw new Error("此研究仍在处理，请先等待完成。");
      if(path==="/api/demo"){
        record.snapshot=this.snapshot(this.sample);record.pending=null;
        await this.write(record,record.version);await this.clearDraft(id);return this.state(id);
      }
      const operationId=body.operation_id || crypto.randomUUID();
      const content=path==="/api/generate"?"主题："+body.topic+"\n要求："+(body.requirements || ""):body.instruction || "正在处理文献";
      const jobId=crypto.randomUUID().replaceAll("-","");
      record.pending={kind:path,content,topic:body.topic,job_id:jobId,started_at:Date.now(),progress:{operation_id:operationId,status:"running",events:[]}};
      await this.write(record,record.version);
      try {
        const accepted=await this.remote("/api/browser/execute",{operation:path,job_id:jobId,body:{...body,operation_id:operationId},
          snapshot:this.payload(record.snapshot),config:this.credentials(path)});
        record.pending.job_id=accepted.job_id;
        // If this write fails, preserve the job identifier in this page and still collect its result.
        try {await this.write(record,record.version);}catch(_){}
        return await this.settle(record);
      } catch(error) {
        if(!error.status && record.pending?.job_id && !(error instanceof DOMException)){
          try{return await this.settle(record);}catch(_){}
        }
        await this.fail(record,error);
        throw error;
      }
    });
    this.pending.set(id,task);
    try{return await task;}finally{this.pending.delete(id);}
  }
  payload(snapshot) {return {run:snapshot.run,corpus:snapshot.run?undefined:snapshot.corpus,demo:snapshot.demo};}
  credentials(path) {return ["/api/generate","/api/revise","/api/connection"].includes(path)?{base:this.config.base,model:this.config.model,key:this.config.key}:{};}
  async wait(jobId) {
    let failures=0,missing=0;
    for(;;){
      let job;
      try {job=await this.remote("/api/browser/jobs/"+jobId);failures=0;}
      catch(error){if(error.status===404 && ++missing<5){}else if(error.status || ++failures>=20)throw error;await new Promise(r=>setTimeout(r,1500));continue;}
      this.progress.set(jobId,job.progress);
      if(job.ready)return job;
      await new Promise(r=>setTimeout(r,1500));
    }
  }
  async acknowledge(jobId) {await this.remote("/api/browser/ack",{job_id:jobId}).catch(()=>{});this.progress.delete(jobId);}
  async readonlyTask(path,body,snapshot) {
    const accepted=await this.remote("/api/browser/execute",{operation:path,body,snapshot:this.payload(snapshot),config:this.credentials(path)});
    const job=await this.wait(accepted.job_id);
    if(job.code>=400){await this.acknowledge(accepted.job_id);throw new Error(job.result.error);}
    const result=job.binary?await this.remote("/api/browser/result/"+accepted.job_id,undefined,true):job.result;
    await this.acknowledge(accepted.job_id);return result;
  }
  async fail(record,error) {
    if(!record.pending)return;
    record.pending.progress={...record.pending.progress,status:"error",events:[{stage:"error",message:error.message}]};
    record.pending.job_id=null;
    try{await this.write(record,record.version);}catch(_){}
  }
  async settle(record) {
    const jobId=record.pending.job_id;
    let job;
    try {job=await this.wait(jobId);}
    catch(error){await this.fail(record,error);throw error;}
    if(job.code>=400){const error=new Error(job.result.error);await this.fail(record,error);await this.acknowledge(jobId);throw error;}
    const next={...record,snapshot:this.snapshot(job.result),pending:null};
    try {
      await this.write(next,record.version);
      if(["/api/generate","/api/revise","/api/import","/api/restore"].includes(record.pending.kind))await this.clearDraft(record.id);
      await this.acknowledge(jobId);
    } catch(error) {
      next.storage_warning=error.message;this.unsaved.set(record.id,next);
      window.dispatchEvent(new CustomEvent("trace-storage-error",{detail:error.message}));
      // Never ACK a result until the browser transaction has committed.
    }
    return this.state(record.id);
  }
};
