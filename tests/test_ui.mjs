import assert from "node:assert/strict";
import {test} from "node:test";
import {readFileSync, mkdtempSync, rmSync, existsSync} from "node:fs";
import {tmpdir} from "node:os";
import {join, dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {execFileSync} from "node:child_process";
import vm from "node:vm";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const html = readFileSync(join(root, "ui/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\nstart\(\);\s*$/, "");
const repo = resolve(process.env.MNEMO_TEST_REPO || root);
const venvPython = process.platform === "win32" ? join(repo,".venv","Scripts","python.exe") : join(repo,".venv","bin","python");
const python = process.env.MNEMO_TEST_PYTHON || (existsSync(venvPython) ? venvPython : "python3");
const backendScript = [
  "import json,sys",
  "sys.path.insert(0,sys.argv[1])",
  "from store import MemoryStore",
  "store=MemoryStore(sys.argv[2])",
  "try:",
  " print(json.dumps({'result':getattr(store,sys.argv[3])(**json.loads(sys.argv[4]))}))",
  "except Exception as error:",
  " print(json.dumps({'error':type(error).__name__+': '+str(error)}))",
  "finally: store.close()",
].join("\n");

class Element {
  constructor(){this.children=[];this._html="";this._text="";this.dataset={};this.disabled=false;
    this.classList={add(){},remove(){}};this.value="";}
  set innerHTML(value){this.children=[];this._text="";this._html=value;}
  get innerHTML(){return this._html+this.children.map(c=>c.innerHTML ?? c.textContent).join("");}
  set textContent(value){this.children=[];this._html="";this._text=String(value);}
  get textContent(){return this._text+this._html.replace(/<[^>]+>/g,"")+this.children.map(c=>c.textContent).join("");}
  appendChild(node){this.children.push(node);return node;}
  addEventListener(){}
  setAttribute(name,value){this[name]=value;}
}
function client(backend=true){
  const directory=mkdtempSync(join(tmpdir(),"mnemo-ui-"));
  const elements=new Map();
  const get=id=>{if(!elements.has(id)) elements.set(id,new Element());return elements.get(id);};
  const window={};
  const context=vm.createContext({
    document:{getElementById:get,createElement:()=>new Element(),
      createTextNode:text=>({textContent:text,innerHTML:text}),querySelector:()=>get("hint")},
    window,fetch:async()=>{throw new Error("unexpected network request");},
    setTimeout, Date, Map, JSON, console,
  });
  vm.runInContext(script,context);
  function tool(name,args){
    const data=JSON.parse(execFileSync(python,["-B","-c",backendScript,repo,join(directory,"isolated.sqlite"),name,JSON.stringify(args)],
      {encoding:"utf8",env:{...process.env,PYTHONDONTWRITEBYTECODE:"1"}}));
    if(data.error) throw new Error(data.error);
    return data.result;
  }
  if(backend) context.callTool=async(name,args)=>tool(name,args);
  return {context,elements,get,tool,window,
    run:expression=>vm.runInContext(expression,context),
    close:()=>rmSync(directory,{recursive:true,force:true})};
}
test("passport recall never returns or corrects lease; ambiguous correction waits for choice",async()=>{
  const c=client();
  try{
    await c.context.agentReply("Remember that my lease is 24 months");
    const lease=c.run("lastCreatedMemory.memory_id");
    await c.context.agentReply("Remember that my dentist is Dr Ferrand");
    const reply=await c.context.agentReply("What about my passport?");
    assert.match(reply,/nothing on that topic/);assert.doesNotMatch(reply,/24 months/);
    const correction=await c.context.agentReply("Actually, my passport expires in 2030");
    assert.match(correction,/Which memory/);assert.match(correction,/Nothing has changed/);
    assert.equal(c.tool("history",{memory_id:lease}).chain.length,1);
    assert.equal(c.tool("history",{memory_id:lease}).chain[0].content,"my lease is 24 months");
  }finally{c.close();}
});
test("only explicitly chosen memory is corrected; stale choices never retry writes",async()=>{
  const c=client();
  try{
    await c.context.agentReply("Remember that my lease is 24 months");
    const lease=c.run("lastCreatedMemory.memory_id");
    await c.context.agentReply("Remember that my dentist is Dr Ferrand");
    const dentist=c.run("lastCreatedMemory.memory_id");
    await c.context.agentReply("Actually, my lease is 18 months");
    const token=c.run("Array.from(choiceTargets.entries()).find(([key,value]) => value.row.memory_id === "+JSON.stringify(lease)+")[0]");
    await c.context.chooseMemory(token);
    assert.equal(c.tool("history",{memory_id:lease}).chain.length,2);
    assert.equal(c.tool("history",{memory_id:dentist}).chain.length,1);
    assert.equal(c.tool("recall",{query:"lease 24"}).count,0);
    assert.match(await c.context.agentReply("What about my lease?"),/18 months/);
    await c.context.agentReply("Actually, my lease is 12 months");
    const staleToken=c.run("Array.from(choiceTargets.entries()).find(([key,value]) => value.row.memory_id === "+JSON.stringify(lease)+")[0]");
    c.tool("correct",{memory_id:lease,expected_version:2,content:"the lease is 30 months",source_refs:["synthetic:other"]});
    await c.context.chooseMemory(staleToken);
    const chain=c.tool("history",{memory_id:lease}).chain;
    assert.equal(chain.length,3);assert.equal(chain.at(-1).content,"the lease is 30 months");
    assert.match(c.get("log").innerHTML,/not applied/);
  }finally{c.close();}
});
test("ambiguous recall asks which memory instead of choosing result zero",async()=>{
  const c=client();
  try{
    await c.context.agentReply("Remember that garage gate code is 1234");
    await c.context.agentReply("Remember that garden gate code is 5678");
    assert.match(await c.context.agentReply("What is the gate code?"),/More than one memory matches/);
  }finally{c.close();}
});
test("cancel and an unrelated question after inventory selection never become corrections",async()=>{
  const c=client();
  try{
    await c.context.agentReply("Remember that my lease is 24 months");
    const lease=c.run("lastCreatedMemory.memory_id");
    await c.context.refreshMemories();
    await c.context.editFromInventory(lease);
    assert.match(await c.context.agentReply("cancel"),/Cancelled/);
    await c.context.editFromInventory(lease);
    assert.match(await c.context.agentReply("What about my passport?"),/nothing on that topic/);
    assert.equal(c.tool("history",{memory_id:lease}).chain.length,1);
    assert.equal(c.tool("history",{memory_id:lease}).chain[0].content,"my lease is 24 months");
  }finally{c.close();}
});
test("stored HTML is escaped in recall, history, choices and inventory",async()=>{
  const c=client();
  try{
    const row=c.tool("remember",{title:"hazard <svg onload='bad()'>",content:"hazard <img src=x onerror='bad()'>",
      source_refs:["synthetic:<img src=x onerror=bad()>"]});
    const recall=await c.context.agentReply("hazard");
    assert.match(recall,/&lt;img/);assert.doesNotMatch(recall,/<img|<svg/);
    const history=await c.context.historyReply({memory_id:row.memory_id});
    assert.doesNotMatch(history,/<img|<svg/);
    await c.context.refreshMemories();
    assert.doesNotMatch(c.get("mems").innerHTML,/<img|<svg/);
    const correction=await c.context.agentReply("Actually, <img src=x onerror=bad()>");
    assert.doesNotMatch(correction,/<img|<svg/);
  }finally{c.close();}
});
test("inventory includes uncommon topics and navigates past twenty memories",async()=>{
  const c=client();
  try{
    for(let i=0;i<21;i++) c.tool("remember",{title:"Rainbow "+i,content:"虹 "+i,source_refs:["synthetic:test"]});
    await c.context.refreshMemories();
    assert.match(c.get("page-label").textContent,/1–20 of 21/);
    assert.equal(c.get("next-page").dataset.offset,"20");
    c.run("memoryOffset=20");
    await c.context.refreshMemories();
    assert.match(c.get("page-label").textContent,/21–21 of 21/);
    assert.equal(c.get("next-page").dataset.offset,"");
    assert.equal(c.run("inventory.size"),1);
    assert.match(c.get("mems").innerHTML,/虹/);
  }finally{c.close();}
});
test("MCP client sends negotiated version/session, including DELETE; no expired-write retry",async()=>{
  const c=client(false),requests=[];
  try{
    c.context.fetch=async(url,options={})=>{
      requests.push({url,...options});
      if(url==="/session-info") return new Response(JSON.stringify({storage:"temporary_demo"}),{headers:{"Content-Type":"application/json"}});
      if(options.method==="DELETE") return new Response("",{status:200});
      const body=JSON.parse(options.body);
      if(body.method==="notifications/initialized") return new Response("",{status:202});
      const result=body.method==="initialize" ? {protocolVersion:"2025-11-25",serverInfo:{name:"Mnemo"}} :
        body.method==="tools/list" ? {tools:[{name:"list_memories"}]} :
        {content:[{type:"text",text:JSON.stringify({results:[],total_memories:0,has_more:false,next_offset:null})}]};
      return new Response(JSON.stringify({jsonrpc:"2.0",id:body.id,result}),{headers:{
        "Content-Type":"application/json","Mcp-Session-Id":"synthetic-session"}});
    };
    await c.context.connect();await c.context.newSession();
    const posts=requests.filter(r=>r.method==="POST");
    const subsequent=posts.filter(r=>JSON.parse(r.body).method!=="initialize");
    for(const request of subsequent){
      assert.equal(request.headers["MCP-Protocol-Version"],"2025-11-25");
      assert.equal(request.headers["Mcp-Session-Id"],"synthetic-session");
    }
    const deletion=requests.find(r=>r.method==="DELETE");
    assert.equal(deletion.headers["MCP-Protocol-Version"],"2025-11-25");
    assert.equal(deletion.headers["Mcp-Session-Id"],"synthetic-session");
    let count=0;
    c.context.fetch=async()=>{count++;return new Response("expired",{status:404});};
    await assert.rejects(c.context.rpc("tools/call",{name:"remember",arguments:{}}),/Session ended/);
    assert.equal(count,1);
  }finally{c.close();}
});
test("voice starts off, uses installed local voice only, has no remote fallback",()=>{
  const c=client(false),spoken=[];
  try{
    const local={lang:"en-US",localService:true},remote={lang:"en-US",localService:false};
    c.window.SpeechSynthesisUtterance=class{constructor(text){this.text=text;}};
    c.window.speechSynthesis={getVoices:()=>[remote,local],cancel(){},speak:value=>spoken.push(value)};
    c.context.speakReply("silent by default");assert.equal(spoken.length,0);
    c.run("voiceOn=true");c.context.speakReply("synthetic voice");
    assert.equal(spoken.length,1);assert.equal(spoken[0].voice,local);
    c.window.speechSynthesis.getVoices=()=>[remote];
    c.context.speakReply("must stay silent");assert.equal(spoken.length,1);
    c.run("storageMode='local_durable'");
    return assert.rejects(c.context.playDemo(),/durable/);
  }finally{c.close();}
});
