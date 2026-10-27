const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const {webcrypto} = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '../web/browser-storage.js'), 'utf8');

function fixture(environment={}) {
  const events = [], calls = [], records = new Map(), metadata = new Map();
  const context = {window:{dispatchEvent:e=>events.push(e)},
    navigator:{locks:{request:(_,fn)=>Promise.resolve().then(fn)}},
    crypto:webcrypto, DOMException, setTimeout, clearTimeout,
    CustomEvent:class {constructor(type, options){this.type=type;Object.assign(this,options);}}};
  Object.assign(context.window,environment.window);
  Object.assign(context.navigator,environment.navigator);
  vm.runInNewContext(source, context);
  const store = new context.window.TraceBrowserStorage({config:{base:'https://api.deepseek.com',model:'test'}, labels:{}});
  const record = store.record('main', {corpus:{documents:[],warnings:[]},run:null});
  record.version=1;records.set('main',structuredClone(record));
  store.read=async(name,id)=>structuredClone((name==='records'?records:metadata).get(id));
  store.all=async()=>[...records.values()].map(r=>store.summary(r));
  store.meta=async(id,value)=>{calls.push('meta:'+id);value===undefined?metadata.delete(id):metadata.set(id,{id,value:structuredClone(value)});};
  store.write=async(next,expected)=>{
    assert.equal(expected,records.get(next.id)?.version);
    calls.push('commit');next.version++;records.set(next.id,structuredClone(next));return next;
  };
  store.acknowledge=async()=>calls.push('ack');
  return {store,record,records,metadata,events,calls};
}

test('HTTP explains the secure entry before attempting to access or migrate local data',async()=>{
  const {store,records,calls}=fixture({window:{isSecureContext:false}});
  await assert.rejects(store.open(),/https:\/\/review\.njfu\.org\//);
  assert.equal(store.db,null);
  assert.equal(records.size,1);
  assert.deepEqual(calls,[]);
});

test('unavailable storage and missing Web Locks have distinct actionable messages',async()=>{
  const blocked=fixture({window:{isSecureContext:true}});
  await assert.rejects(blocked.store.open(),/允许此站点保存数据/);
  const older=fixture({window:{isSecureContext:true,indexedDB:{}},navigator:{locks:undefined}});
  await assert.rejects(older.store.open(),/多标签页协调功能/);
  assert.deepEqual(blocked.calls,[]);
  assert.deepEqual(older.calls,[]);
});

test('result is acknowledged only after the browser commit; old draft is replaced',async()=>{
  const {store,record,records,calls}=fixture();
  record.pending={job_id:'job',kind:'/api/generate',progress:{status:'running'}};
  store.wait=async()=>({code:200,result:{run:{topic:'new',corpus:{documents:[],warnings:[]}}}});
  await store.settle(record);
  assert.deepEqual(calls,['commit','meta:draft:main','ack']);
  assert.equal(records.get('main').snapshot.run.topic,'new');
  assert.equal(records.get('main').pending,null);
});

test('quota failure keeps the old draft, retains the new result, and allows export without a write',async()=>{
  const {store,record,records,events,calls}=fixture();
  record.pending={job_id:'job',progress:{status:'running'}};
  store.wait=async()=>({code:200,result:{run:{topic:'unsaved',corpus:{documents:[],warnings:[]}}}});
  store.write=async()=>{throw new Error('quota');};
  const state=await store.settle(record);
  assert.equal(records.get('main').snapshot.run,null);
  assert.equal(state.run.topic,'unsaved');
  assert.equal(state.storage_warning,'quota');
  assert.equal(events.length,1);
  assert.ok(!calls.includes('ack'));
  store.readonlyTask=async(operation,body,snapshot)=>{
    assert.equal(operation,'/api/export');assert.equal(snapshot.run.topic,'unsaved');return 'archive';
  };
  assert.equal(await store.request('/api/export',{},true,'main'),'archive');
  await assert.rejects(store.request('/api/annotate',{},false,'main'),/导出备份/);
});

test('key is held in page memory, excluded from preferences, state, document and non-AI requests',async()=>{
  const {store,metadata}=fixture();
  const state=await store.request('/api/config',{key:'test-only-secret',model:'chosen'},false,'main');
  assert.equal(store.config.key,'test-only-secret');
  assert.equal(state.config.key_set,true);
  assert.ok(!JSON.stringify(state).includes('test-only-secret'));
  assert.ok(!JSON.stringify([...metadata]).includes('test-only-secret'));
  assert.equal(store.credentials('/api/generate').key,'test-only-secret');
  assert.ok(!JSON.stringify(store.credentials('/api/import')).includes('test-only-secret'));
  assert.ok(!JSON.stringify(store.record('copy',{...state,config:store.config})).includes('test-only-secret'));
});

test('lost submission reply recovers the known job instead of generating twice',async()=>{
  const {store,records}=fixture();let submissions=0;
  store.remote=async(path,body)=>{
    assert.equal(path,'/api/browser/execute');submissions++;
    assert.equal(records.get('main').pending.job_id,body.job_id);
    throw new TypeError('connection interrupted after server accepted');
  };
  store.wait=async(jobId)=>{
    assert.equal(jobId,records.get('main').pending.job_id);
    return {code:200,result:{run:{topic:'recovered',corpus:{documents:[],warnings:[]}}}};
  };
  const state=await store.request('/api/generate',{topic:'recovered'},false,'main');
  assert.equal(submissions,1);assert.equal(state.run.topic,'recovered');
});

test('a failed processing job preserves the prior manuscript',async()=>{
  const {store,record,records,calls}=fixture();
  record.snapshot.run={topic:'original',corpus:{documents:[],warnings:[]}};
  record.pending={job_id:'job',progress:{status:'running'}};
  records.set('main',structuredClone(record));
  store.wait=async()=>({code:400,result:{error:'model unavailable'}});
  await assert.rejects(store.settle(record),/model unavailable/);
  assert.equal(records.get('main').snapshot.run.topic,'original');
  assert.equal(records.get('main').pending.progress.status,'error');
  assert.equal(records.get('main').pending.job_id,null);
  assert.deepEqual(calls,['commit','ack']);
});

test('saving an annotation preserves the unsent chat draft',async()=>{
  const {store,record,metadata}=fixture();
  metadata.set('draft:main',{id:'draft:main',value:{revisionDraft:'continue later'}});
  record.pending={job_id:'job',kind:'/api/annotate',progress:{status:'running'}};
  store.wait=async()=>({code:200,result:record.snapshot});
  const state=await store.settle(record);
  assert.equal(state.draft.revisionDraft,'continue later');
});
