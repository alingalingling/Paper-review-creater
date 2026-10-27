const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/atmosphere.js'),'utf8');

function fixture(entered=false){
  const timers=new Map(), nodes=new Map(), fontLoads=[], painted=[];
  const classList=()=>{const set=new Set();return {add:(...x)=>x.forEach(v=>set.add(v)),remove:(...x)=>x.forEach(v=>set.delete(v)),contains:v=>set.has(v),toggle:(v,on)=>on?set.add(v):set.delete(v)};};
  const node=()=>({classList:classList(),dataset:{},style:{},inert:true,hidden:false,
    attrs:{},setAttribute(k,v){this.attrs[k]=v;},addEventListener(){},focus(){}});
  const body=node();body.classList.add('studio-loading','landing-open');
  const context2d=()=>({setTransform(){},clearRect(){},fillText(text){painted.push({text,font:this.font});}});
  const document={body,hidden:false,querySelector(s){if(!nodes.has(s))nodes.set(s,node());return nodes.get(s);},
    querySelectorAll:s=>s.split(',').map(x=>document.querySelector(x)),addEventListener(){},
    createElement:()=>({getContext:()=>context2d()}),fonts:{load(spec){return new Promise((resolve,reject)=>fontLoads.push({spec,resolve,reject}));}}};
  document.querySelector('#typeField').getContext=()=>context2d();
  const context={document,window:{scrollTo(){}},location:{reload(){}},
    matchMedia:()=>({matches:false,addEventListener(){}}),
    localStorage:{getItem:()=>null,setItem(){}},sessionStorage:{getItem:()=>entered?'yes':null,setItem(){}},
    innerWidth:1280,innerHeight:800,devicePixelRatio:1,performance:{now:()=>0},
    requestAnimationFrame:()=>1,cancelAnimationFrame(){},addEventListener(){},
    setTimeout(fn,ms){const key={};timers.set(key,{fn,ms});return key;},clearTimeout:key=>timers.delete(key)};
  vm.runInNewContext(source,context);
  return {context,body,nodes,fontLoads,painted,timers};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));

test('paint real bundled glyphs after font loading; reveal only when workspace is also ready',async()=>{
  const f=fixture();
  f.context.window.TracePresentation.ready();
  assert.equal(f.painted.length,0);
  assert.ok(f.body.classList.contains('studio-loading'));
  f.fontLoads.forEach(x=>x.resolve([{}]));await flush();
  assert.equal(f.nodes.get('#typeField').dataset.typefaces,'4');
  assert.equal(new Set(f.painted.map(x=>x.font)).size,4);
  assert.ok(f.painted.every(x=>x.font.includes('Trace Ink')));
  assert.ok(!f.body.classList.contains('studio-loading'));
  assert.equal(f.nodes.get('#studioShell').attrs['aria-busy'],'false');
});

test('returning visitors never see an empty interactive workspace while data is loading',async()=>{
  const f=fixture(true);
  f.fontLoads.forEach(x=>x.resolve([{}]));await flush();
  assert.ok(f.nodes.get('#opening').hidden);
  assert.ok(f.nodes.get('#studioShell').inert);
  assert.ok(f.body.classList.contains('studio-loading'));
  f.context.window.TracePresentation.ready();
  assert.equal(f.nodes.get('#studioShell').inert,false);
  assert.ok(!f.body.classList.contains('studio-loading'));
});

test('slow fonts do not block writing and late faces rebuild the canvas atlas',async()=>{
  const f=fixture(true);f.context.window.TracePresentation.ready();
  f.fontLoads[0].resolve([{}]);await flush();
  [...f.timers.values()].find(x=>x.ms===2500).fn();
  assert.ok(!f.body.classList.contains('studio-loading'));
  assert.equal(f.nodes.get('#typeField').dataset.typefaces,'1');
  f.fontLoads.slice(1).forEach(x=>x.resolve([{}]));await flush();
  assert.equal(f.nodes.get('#typeField').dataset.typefaces,'4');
});

test('failed font file is excluded rather than painted in an installed fallback font',async()=>{
  const f=fixture();f.context.window.TracePresentation.ready();
  f.fontLoads[0].reject(new Error('offline'));
  f.fontLoads.slice(1).forEach(x=>x.resolve([{}]));await flush();
  assert.equal(f.nodes.get('#typeField').dataset.typefaces,'3');
  assert.ok(f.painted.every(x=>!x.font.includes('Running')));
  assert.ok(!f.body.classList.contains('studio-loading'));
});
