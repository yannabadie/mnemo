import assert from "node:assert/strict";
import {test} from "node:test";
import {readFileSync,mkdtempSync,rmSync,existsSync} from "node:fs";
import {tmpdir} from "node:os";
import {join,dirname,resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {spawn} from "node:child_process";
import vm from "node:vm";

const root=dirname(dirname(fileURLToPath(import.meta.url)));
const html=readFileSync(join(root,"ui/index.html"),"utf8");
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\nstart\(\);\s*$/,"");
const repo=resolve(process.env.MNEMO_TEST_REPO || root);
const venvPython=join(repo,".venv","bin","python");
const python=process.env.MNEMO_TEST_PYTHON || (existsSync(venvPython)?venvPython:"python3");
const backendScript=[
 "import json,sys","sys.path.insert(0,sys.argv[1])","from store import MemoryStore",
 "from planner import prepare_workshop,export_workshop","from plan_receipts import PlanReceiptRegistry",
 "store=MemoryStore(sys.argv[2])","receipts=PlanReceiptRegistry()","try:"," for line in sys.stdin:",
 "  data=json.loads(line)","  try:","   name,args=data['name'],data['args']",
 "   if name=='prepare_workshop': result=prepare_workshop(store,**args,receipts=receipts)",
 "   elif name=='export_workshop': result=export_workshop(store,**args,receipts=receipts)",
 "   else: result=getattr(store,name)(**args)",
 "   output={'id':data['id'],'result':result}","  except Exception as error:",
 "   output={'id':data['id'],'error':type(error).__name__+': '+str(error)}",
 "  print(json.dumps(output),flush=True)","finally: store.close()"
].join("\n");

class Element{
 constructor(){this.children=[];this._html="";this._text="";this.dataset={};this.attributes={};this.disabled=false;this.hidden=false;this.value="";this.className="";}
 set innerHTML(value){this.children=[];this._text="";this._html=String(value);}
 get innerHTML(){return this._html+this.children.map(c=>c.innerHTML ?? c.textContent).join("");}
 set textContent(value){this.children=[];this._html="";this._text=String(value);}
 get textContent(){return this._text+this._html.replace(/<[^>]+>/g,"")+this.children.map(c=>c.textContent).join("");}
 appendChild(node){this.children.push(node);node.parent=this;return node;}
 get firstChild(){return this.children[0];}
 remove(){if(this.parent)this.parent.children.splice(this.parent.children.indexOf(this),1);}
 addEventListener(){}
 setAttribute(name,value){this.attributes[name]=String(value);}
 removeAttribute(name){delete this.attributes[name];}
 hasAttribute(name){return name in this.attributes;}
 focus(){this.focused=true;}
 reset(){}
 click(){}
}
function client(backend=true){
 const directory=mkdtempSync(join(tmpdir(),"mnemo-workshop-ui-")),elements=new Map(),calls=[],results=[],pending=new Map();
 const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 get("next-page").dataset.offset="";get("brief-editor").hidden=true;get("fact-editor").hidden=true;
 const window={addEventListener(){},confirm:()=>true};
 const context=vm.createContext({
  document:{getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>[],querySelector:()=>null,documentElement:new Element(),body:new Element()},
  window,fetch:async()=>{throw new Error("unexpected network request");},
  setTimeout,clearTimeout,AbortController,Response,Blob,URL,Intl,Date,Map,JSON,console
 });
 vm.runInContext(script,context);
 const worker=backend?spawn(python,["-u","-B","-c",backendScript,repo,join(directory,"isolated.sqlite")],{env:{...process.env,PYTHONDONTWRITEBYTECODE:"1"},stdio:["pipe","pipe","pipe"]}):null;
 let sequence=0,buffer="",stderr="";
 const fail=error=>{for(const task of pending.values()){clearTimeout(task.timer);task.reject(error);}pending.clear();};
 if(worker){
  worker.stdout.setEncoding("utf8");worker.stderr.setEncoding("utf8");
  worker.stderr.on("data",chunk=>{stderr=(stderr+chunk).slice(-4000);});
  worker.on("error",fail);
  worker.on("exit",code=>{if(pending.size)fail(new Error("Isolated Python worker ended: "+code+" "+stderr));});
  worker.stdout.on("data",chunk=>{
   buffer+=chunk;let end;
   while((end=buffer.indexOf("\n"))>=0){
    const line=buffer.slice(0,end);buffer=buffer.slice(end+1);if(!line)continue;
    const data=JSON.parse(line),task=pending.get(data.id);if(!task)continue;
    pending.delete(data.id);clearTimeout(task.timer);
    if(data.error){const error=new Error(data.error);if(/VersionConflict/.test(data.error))error.code="stale";task.reject(error);}
    else{results.push({name:task.name,args:task.args,result:data.result});task.resolve(data.result);}
   }
  });
 }
 function tool(name,args){
  if(!worker)return Promise.reject(new Error("No isolated Python worker"));
  const id=++sequence,copy=JSON.parse(JSON.stringify(args));calls.push({name,args:copy});
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(id);reject(new Error("Isolated Python tool timed out"));},10000);
   pending.set(id,{resolve,reject,timer,name,args:copy});
   worker.stdin.write(JSON.stringify({id,name,args:copy})+"\n");
  });
 }
 if(backend)context.callTool=tool;
 return {context,get,window,calls,results,tool,run:expression=>vm.runInContext(expression,context),
  close:async()=>{
   if(worker && worker.exitCode===null){worker.stdin.end();await new Promise(resolve=>{const timer=setTimeout(()=>worker.kill(),2000);worker.once("exit",()=>{clearTimeout(timer);resolve();});});}
   rmSync(directory,{recursive:true,force:true});
  }};
}
const brief=(overrides={})=>({schema:"workshop-brief-v1",title:"Fictional workshop",participants:12,capacity:12,event_date:"2026-11-06",timezone:"Europe/Paris",start:"10:00",end:"12:00",session_minutes:45,transition_minutes:15,...overrides});
async function selectWorkshop(c,data=brief()){
 const row=await c.context.rememberFact({title:data.title,content:JSON.stringify(data),source_refs:["synthetic:initial brief"]});
 await c.context.selectMemory(row);await c.context.useSelectedBrief();return row;
}
function response(body,result,status=200,extra={}){
 return new Response(JSON.stringify({jsonrpc:"2.0",id:body.id,result}),{status,headers:{"Content-Type":"application/json",...extra}});
}

test("workshop change requires a real decision; export is the server's current version",async()=>{
 const c=client();
 try{
  const row=await selectWorkshop(c);
  assert.equal(c.run("activePlan.status"),"ready");
  assert.equal(c.run("activePlan.brief.version"),1);
  assert.match(c.get("plan-result").innerHTML,/1 séance\./);assert.doesNotMatch(c.get("plan-result").innerHTML,/séance\(s\)/);
  assert.match(c.get("plan-result").innerHTML,/Confirmer la capacité déclarée de 12 places/);
  assert.deepEqual(JSON.parse(c.run("JSON.stringify(activePlan.sessions)")),[{group:1,participants:12,start:"10:00",end:"10:45"}]);
  await c.context.changeRoom();
  assert.equal(c.run("activePlan.status"),"needs_decision");
  assert.equal(c.run("activePlan.export_text"),undefined);
  assert.equal(c.run("activePlan.plan_id"),null);
  assert.equal(c.run("previousPlan.brief.version"),1);
  assert.deepEqual(JSON.parse(c.run("JSON.stringify(activePlan.brief.source_refs)")),["synthetic:initial brief",c.context.t("roomSource")]);
  await assert.rejects(c.context.getDownloadData(),/recalculez/);
  const planningCalls=c.calls.filter(v=>v.name==="prepare_workshop");
  assert.equal(planningCalls.length,2);
  assert.equal(planningCalls[1].args.expected_version,2);
  assert.equal("strategy" in planningCalls[1].args,false);
  await c.context.prepareWorkshop("split");
  assert.deepEqual(JSON.parse(c.run("JSON.stringify(activePlan.sessions)")),[{group:1,participants:6,start:"10:00",end:"10:45"},{group:2,participants:6,start:"11:00",end:"11:45"}]);
  assert.match(c.get("plan-result").innerHTML,/2 séances\./);
  const download=await c.context.getDownloadData();
  assert.equal(download.version,2);
  assert.equal(download.plan_id,c.run("activePlan.plan_id"));
  assert.equal(download.text,c.results.filter(v=>v.name==="export_workshop").at(-1).result.export_text);
  assert.match(download.text,/Current brief checked at:/);
  assert.match(download.text,/10:00–10:45/);assert.match(download.text,/11:00–11:45/);
  assert.match(download.text,/new|nouvelle salle/);
  assert.equal((await c.tool("history",{memory_id:row.memory_id})).chain.length,2);
  (await c.tool("correct",{memory_id:row.memory_id,expected_version:2,content:JSON.stringify(brief({capacity:3})),source_refs:["synthetic:external change"]}));
  assert.equal(await c.context.getDownloadData(),null);
  assert.equal(c.run("planObsolete"),true);
  assert.match(c.get("feedback").textContent,/brief a changé/);
 }finally{await c.close();}
});


test("structured brief edits preserve old declared sources without duplicates",async()=>{
 const c=client();
 try{
  await selectWorkshop(c);c.context.openBriefEditor();
  await c.context.saveBrief(brief({capacity:6}),["synthetic:initial brief","synthetic:new room","synthetic:new room"]);
  assert.deepEqual(JSON.parse(c.run("JSON.stringify(activePlan.brief.source_refs)")),["synthetic:initial brief","synthetic:new room"]);
  assert.equal(c.run("activePlan.status"),"needs_decision");
  assert.match(c.get("plan-result").innerHTML,/105 min nécessaires pour 120 min disponibles/);
 }finally{await c.close();}
});

test("limit is an explicit choice and keeps the waiting list visible",async()=>{
 const c=client();
 try{
  await selectWorkshop(c,brief({capacity:6}));
  assert.equal(c.run("activePlan.status"),"needs_decision");
  await c.context.prepareWorkshop("limit");
  assert.equal(c.run("activePlan.scheduled_participants"),6);
  assert.equal(c.run("activePlan.waitlisted_participants"),6);
  assert.match(c.get("plan-result").innerHTML,/Sur liste d’attente/);
  assert.match((await c.context.getDownloadData()).text,/Waiting list: 6/);
 }finally{await c.close();}
});

test("a changed brief drives different groups and times, with no fixed demo-plan fallback",async()=>{
 const c=client();
 try{
  await selectWorkshop(c,brief({participants:17,capacity:7,start:"13:00",end:"16:00",session_minutes:30,transition_minutes:10}));
  await c.context.prepareWorkshop("split");
  assert.deepEqual(JSON.parse(c.run("JSON.stringify(activePlan.sessions)")),[{group:1,participants:7,start:"13:00",end:"13:30"},{group:2,participants:7,start:"13:40",end:"14:10"},{group:3,participants:3,start:"14:20",end:"14:50"}]);
  assert.match(c.get("plan-result").innerHTML,/13:40/);
  assert.equal(c.run("activePlan.scheduled_participants"),17);
 }finally{await c.close();}
});

test("blocked plans have no sessions or downloadable result",async()=>{
 const c=client();
 try{
  await selectWorkshop(c,brief({capacity:6,end:"11:00",session_minutes:90}));
  assert.equal(c.run("activePlan.status"),"blocked");
  assert.equal(c.run("activePlan.sessions.length"),0);
  assert.equal(c.run("activePlan.export_text"),undefined);
  assert.equal(c.run("activePlan.plan_id"),null);
  assert.doesNotMatch(c.get("plan-result").innerHTML,/data-download/);
  await assert.rejects(c.context.getDownloadData(),/recalculez/);
 }finally{await c.close();}
});

test("manual corrections target the chosen ID/version and never retry a stale write",async()=>{
 const c=client();
 try{
  const lease=await c.context.rememberFact({title:"Lease",content:"The lease is 24 months",source_refs:["synthetic:lease"]});
  const dentist=await c.context.rememberFact({title:"Dentist",content:"The dentist is Dr Ferrand",source_refs:["synthetic:dentist"]});
  await c.context.selectMemory(lease);c.context.openFactCorrection();
  await c.context.applyFactCorrection({content:"The lease is 18 months",source_refs:["synthetic:correction"]});
  assert.equal((await c.tool("history",{memory_id:lease.memory_id})).chain.length,2);
  assert.equal((await c.tool("history",{memory_id:dentist.memory_id})).chain.length,1);
  c.context.openFactCorrection();
  (await c.tool("correct",{memory_id:lease.memory_id,expected_version:2,content:"The lease is 30 months",source_refs:["synthetic:another editor"]}));
  const before=c.calls.filter(v=>v.name==="correct").length;
  await assert.rejects(c.context.applyFactCorrection({content:"The lease is 12 months",source_refs:["synthetic:stale"]}),/VersionConflict/);
  assert.equal(c.calls.filter(v=>v.name==="correct").length,before+1);
  const chain=(await c.tool("history",{memory_id:lease.memory_id})).chain;
  assert.equal(chain.length,3);assert.equal(chain.at(-1).content,"The lease is 30 months");
  assert.equal(c.run("factTarget"),null);
  assert.match(c.get("fact-correction-error").textContent,/pas été appliquée/);
 }finally{await c.close();}
});

test("keyword search does not invent missing topics or choose among ambiguous memories",async()=>{
 const c=client();
 try{
  await c.context.rememberFact({title:"Garage gate",content:"The gate code is 1234",source_refs:["synthetic:garage"]});
  await c.context.rememberFact({title:"Garden gate",content:"The gate code is 5678",source_refs:["synthetic:garden"]});
  const ambiguous=await c.context.searchMemories("gate");
  assert.equal(ambiguous.results.length,2);assert.equal(c.run("selectedMemory"),null);
  const missing=await c.context.searchMemories("passport");
  assert.equal(missing.results.length,0);
  assert.match(c.get("mems").innerHTML,/Aucun fait actuel/);
  assert.equal(c.calls.filter(v=>v.name==="correct").length,0);
 }finally{await c.close();}
});

test("history is read by exact ID even when earlier text is no longer searchable",async()=>{
 const c=client();
 try{
  const row=await c.context.rememberFact({title:"Lease",content:"The lease is 24 months",source_refs:["synthetic:initial"]});
  const changed=await c.context.correctFact(row,{content:"The lease is 18 months",source_refs:["synthetic:changed"]});
  assert.equal((await c.context.searchMemories("lease 24")).results.length,0);
  await c.context.selectMemory(changed);
  assert.match(c.get("memory-detail").innerHTML,/24 months/);
  assert.match(c.get("memory-detail").innerHTML,/18 months/);
  assert.equal(c.run("selectedHistory.chain.length"),2);
 }finally{await c.close();}
});

test("stored HTML is escaped in facts, history, plan sources and decision renderers",async()=>{
 const c=client();
 try{
  const row=await c.context.rememberFact({title:"Hazard <svg onload='bad()'>",content:"Hazard <img src=x onerror='bad()'>",source_refs:["synthetic:<img src=x onerror=bad()>"]});
  await c.context.refreshMemories();await c.context.selectMemory(row);
  for(const id of ["mems","memory-detail"]){assert.doesNotMatch(c.get(id).innerHTML,/<img|<svg/);assert.match(c.get(id).innerHTML,/&lt;/);}
  const data=brief({title:"<svg onload=bad()>"}),workshop=await c.context.rememberFact({title:data.title,content:JSON.stringify(data),source_refs:["synthetic:<img src=x onerror=bad()>"]});
  await c.context.selectMemory(workshop);await c.context.useSelectedBrief();
  assert.doesNotMatch(c.get("brief-summary").innerHTML,/<img|<svg/);
  assert.doesNotMatch(c.get("plan-provenance").innerHTML,/<img|<svg/);
  assert.match(c.get("plan-provenance").innerHTML,/&lt;img/);
 }finally{await c.close();}
});

test("inventory lists uncommon topics and pages beyond twenty memories",async()=>{
 const c=client();
 try{
  for(let i=0;i<21;i++)(await c.tool("remember",{title:"Rainbow "+i,content:"虹 "+i,source_refs:["synthetic:inventory"]}));
  await c.context.refreshMemories();
  assert.match(c.get("page-label").textContent,/1–20 sur 21/);
  assert.equal(c.get("next-page").dataset.offset,"20");
  c.run("memoryOffset=20");await c.context.refreshMemories();
  assert.match(c.get("page-label").textContent,/21–21 sur 21/);
  assert.equal(c.get("next-page").dataset.offset,"");
  assert.equal(c.run("inventory.size"),1);
  assert.match(c.get("mems").innerHTML,/虹/);
 }finally{await c.close();}
});

test("MCP negotiation includes version/session headers and DELETE; an expired write is never retried",async()=>{
 const c=client(false),requests=[];
 try{
  c.context.fetch=async(url,options={})=>{
   requests.push({url,...options});
   if(url==="/session-info")return new Response(JSON.stringify({storage:"temporary_demo"}),{headers:{"Content-Type":"application/json"}});
   if(options.method==="DELETE")return new Response("",{status:200});
   const body=JSON.parse(options.body);
   if(body.method==="notifications/initialized")return new Response("",{status:202});
   const result=body.method==="initialize"?{protocolVersion:"2025-11-25",serverInfo:{name:"Mnemo"}}:
    body.method==="tools/list"?{tools:["remember","recall","correct","history","list_memories","stats","prepare_workshop","export_workshop"].map(name=>({name}))}:
    {content:[{type:"text",text:JSON.stringify({results:[],total_memories:0,has_more:false,next_offset:null})}]};
   return response(body,result,200,{"Mcp-Session-Id":"synthetic-session"});
  };
  await c.context.connect();await c.context.newSession();
  for(const request of requests.filter(r=>r.method==="POST" && JSON.parse(r.body).method!=="initialize")){
   assert.equal(request.headers["MCP-Protocol-Version"],"2025-11-25");assert.equal(request.headers["Mcp-Session-Id"],"synthetic-session");assert.equal(request.credentials,"omit");
  }
  const deletion=requests.find(v=>v.method==="DELETE");
  assert.equal(deletion.headers["MCP-Protocol-Version"],"2025-11-25");assert.equal(deletion.headers["Mcp-Session-Id"],"synthetic-session");
  let count=0;c.context.fetch=async()=>{count++;return new Response("expired",{status:404});};
  await c.context.interaction(()=>c.context.rpc("tools/call",{name:"remember",arguments:{}}));
  assert.equal(count,1);assert.equal(c.run("connected"),false);assert.equal(c.run("session"),null);
  assert.equal(c.get("save-fact").disabled,true);assert.equal(c.get("new-session").disabled,false);
  assert.equal(c.get("connection").dataset.state,"ended");assert.match(c.get("feedback").textContent,/pas été relancée/);
 }finally{await c.close();}
});

test("initialization failure keeps writes disabled and exposes reconnection",async()=>{
 const c=client(false);
 try{
  c.context.fetch=async()=>new Response("unavailable",{status:503});
  await c.context.interaction(()=>c.context.connect());
  assert.equal(c.run("connected"),false);assert.equal(c.run("busy"),false);
  assert.equal(c.get("save-fact").disabled,true);assert.equal(c.get("prepare-example").disabled,true);
  assert.equal(c.get("new-session").disabled,false);assert.equal(c.get("connection").dataset.state,"error");
 }finally{await c.close();}
});

test("a hung request and a hung response body both have a deadline",async()=>{
 const c=client(false);
 try{
  let aborted=false;
  c.context.fetch=(_url,options)=>{options.signal.addEventListener("abort",()=>{aborted=true;});return new Promise(()=>{});};
  await assert.rejects(c.context.readHttp("/mcp",{},20),error=>error.code==="timeout");assert.equal(aborted,true);
  c.context.fetch=async()=>({text:()=>new Promise(()=>{})});
  await assert.rejects(c.context.readHttp("/mcp",{},20),error=>error.code==="timeout");
 }finally{await c.close();}
});

test("an uncertain write blocks further mutation until an explicit inventory reconciliation",async()=>{
 const c=client(false);let count=0;
 try{
  c.run("connected=true;session='synthetic';protocol='2025-11-25'");
  c.context.readHttp=async()=>{count++;throw c.context.appError("timeout");};
  await c.context.interaction(()=>c.context.rpc("tools/call",{name:"remember",arguments:{title:"A"}}));
  assert.equal(count,1);assert.equal(c.run("uncertainWrite"),true);assert.equal(c.get("save-fact").disabled,true);
  await assert.rejects(c.context.rememberFact({title:"A",content:"B",source_refs:["synthetic:C"]}),/Actualisez/);assert.equal(count,1);
 }finally{await c.close();}
});

test("a confirmed write stays acknowledged if the following inventory read fails",async()=>{
 const c=client();
 try{
  const original=c.context.callTool;
  c.context.callTool=async(name,args)=>{if(name==="list_memories")throw new Error("read failed");return original(name,args);};
  await c.context.interaction(async()=>{await c.context.rememberFact({title:"Confirmed",content:"Saved fact",source_refs:["synthetic:source"]});await c.context.refreshMemories();});
  assert.equal(c.run("lastMutationReceipt.operation"),"remember");
  assert.match(c.get("feedback").textContent,/écriture est confirmée/);
  assert.equal((await c.tool("recall",{query:"Confirmed"})).count,1);
 }finally{await c.close();}
});

test("language selection changes the interface but not already stored facts or a server export",async()=>{
 const c=client();
 try{
  await selectWorkshop(c);const before=c.run("JSON.stringify(activePlan)"),title=c.run("workshopMemory.title");
  c.run("language='en'");c.context.renderLanguage();
  assert.equal(c.context.t("download"),"Download the plan");
  assert.equal(c.run("JSON.stringify(activePlan)"),before);assert.equal(c.run("workshopMemory.title"),title);
  assert.match(c.get("plan-result").innerHTML,/Download the plan/);
  assert.match(c.get("plan-result").innerHTML,/1 session\./);assert.doesNotMatch(c.get("plan-result").innerHTML,/session\(s\)/);
 }finally{await c.close();}
});

test("voice is off by default, uses the selected language's installed local voice and has no remote fallback",async()=>{
 const c=client(false),spoken=[];
 try{
  const local={lang:"fr-FR",localService:true},remote={lang:"fr-FR",localService:false};
  c.window.SpeechSynthesisUtterance=class{constructor(text){this.text=text;}};
  c.window.speechSynthesis={getVoices:()=>[remote,local],cancel(){},speak:value=>spoken.push(value)};
  c.context.speakReply("silent");assert.equal(spoken.length,0);
  c.run("voiceOn=true");c.context.speakReply("lecture locale");assert.equal(spoken.length,1);assert.equal(spoken[0].voice,local);
  c.window.speechSynthesis.getVoices=()=>[remote];c.context.speakReply("silence");assert.equal(spoken.length,1);
  c.run("storageMode='local_durable'");await assert.rejects(c.context.createWorkshopExample(),/durable/);
 }finally{await c.close();}
});
