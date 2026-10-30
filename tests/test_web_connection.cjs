"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const root = path.join(__dirname, "../xiaogpt/web/static");
const form = require(path.join(root, "connection.js"));
const fixture = () => ({config:{api_base:"https://first.example/v1",gpt_options:{model:"first-model"},tts_options:{},keyword:["请问"]},key_saved:true,profiles:[{id:"backup",name:"备用",api_base:"https://second.example/v1",model:"second-model",key_saved:true}],providers:[{name:"第一接口",url:"https://first.example/v1"},{name:"第二接口",url:"https://second.example/v1"},{name:"自定义",url:""}],voices:[],hardware:["L05C"],config_file:"/config/config.json",revision:"revision"});
const draft = values => ({api_base:"https://first.example/v1",openai_key:"",model:"first-model",...values});

test("current interface uses stored credentials without asking users to re-enter them",()=>{
  assert.deepEqual(form.prepare(fixture(),"",draft()),{...draft(),profile_id:""});
});
test("saved interface selects its own credential source",()=>{
  const result=form.prepare(fixture(),"backup",draft({api_base:"https://second.example",model:"second-model"}));
  assert.equal(result.profile_id,"backup");assert.equal(result.api_base,"https://second.example/v1");
});
test("new interface always requires a key, even at the current URL",()=>{
  assert.throws(()=>form.prepare(fixture(),form.NEW,draft()),/密钥/);
});
test("editing an address never silently reuses another interface's key",()=>{
  assert.throws(()=>form.prepare(fixture(),"",draft({api_base:"https://second.example"})),/密钥/);
});
test("new key is trimmed; embedded spaces, credentials and token-bearing URLs are rejected",()=>{
  assert.equal(form.prepare(fixture(),form.NEW,draft({openai_key:"  new-key  "})).openai_key,"new-key");
  for(const key of ["sk bad","Bearer abc"])assert.throws(()=>form.prepare(fixture(),form.NEW,draft({openai_key:key})));
  for(const address of ["https://user:pass@example.com","https://example.com/?key=secret","https://example.com/#key","ftp://example.com","not-a-url"])assert.throws(()=>form.normalize(address));
});
test("model listing works before a model is selected; tests require a model",()=>{
  assert.equal(form.prepare(fixture(),"",draft({model:""}),false).model,"");
  assert.throws(()=>form.prepare(fixture(),"",draft({model:""})),/第 2 步/);
});
test("unknown saved interface is rejected instead of falling back to the live key",()=>{
  assert.throws(()=>form.source(fixture(),"deleted"),/不存在/);
});
test("model dropdown preserves a custom model and removes duplicates",()=>{
  assert.deepEqual(form.modelOptions(["b","a","b"],"custom"),["custom","a","b"]);
});

// Exercise the shipped UI handlers with a small DOM adapter. API responses are
// controlled so late responses and failed requests can be tested deterministically.
function app() {
  class Element {
    constructor(){this.value="";this.type="text";this.checked=false;this.disabled=false;this.textContent="";this.options=[];this.listeners={};this.classes=new Set();this.classList={add:(v)=>this.classes.add(v),remove:(v)=>this.classes.delete(v),contains:(v)=>this.classes.has(v),toggle:(v,on)=>{const set=on??!this.classes.has(v);set?this.classes.add(v):this.classes.delete(v);return set;}};}
    append(el){this.options.push(el);}
    replaceChildren(){this.options=[];}
    addEventListener(name,fn){this.listeners[name]=fn;}
    focus(){}
  }
  const elements={};
  for(const match of fs.readFileSync(path.join(root,"index.html"),"utf8").matchAll(/id="([^"]+)"/g)) elements[match[1]]=new Element();
  const context=vm.createContext({URL,JSON,Error,Boolean,String,Number,Date,Set,Promise,structuredClone,console,setTimeout:()=>0,clearTimeout(){},setInterval(){},history:{replaceState(){}},location:{hash:""},window:{addEventListener(){}},document:{getElementById:id=>{assert.ok(elements[id],"missing DOM id: "+id);return elements[id];},createElement:()=>new Element(),querySelectorAll:()=>[]}});
  vm.runInContext(fs.readFileSync(path.join(root,"connection.js"),"utf8"),context);
  const script=fs.readFileSync(path.join(root,"app.js"),"utf8").replace(/boot\(\)\.catch\(error=>\{showLogin\(\);\$\("loginError"\)\.textContent=error.message;\}\);\s*$/,"");
  vm.runInContext(script,context);
  context.fixture=fixture(); vm.runInContext("renderSettings(fixture);tab('connection')",context);
  return {elements,context,run:code=>vm.runInContext(code,context)};
}
test("selecting a new interface clears the old address, key, model and test state",()=>{
  const ui=app();ui.elements.profileSelect.value=form.NEW;ui.run("chooseConnection(ConnectionForm.NEW)");
  assert.equal(ui.elements.apiBase.value,"");assert.equal(ui.elements.apiKey.value,"");assert.equal(ui.elements.model.value,"");assert.equal(ui.elements.connectionDetails.open,true);
  assert.throws(()=>ui.run("preparedConnection(false)"));
});
test("returning to current interface restores the actual current model",()=>{
  const ui=app();ui.elements.profileSelect.value="backup";ui.run("chooseConnection('backup')");
  assert.equal(ui.elements.model.value,"second-model");
  ui.elements.profileSelect.value="";ui.run("chooseConnection('')");assert.equal(ui.elements.model.value,"first-model");assert.equal(ui.elements.apiBase.value,"https://first.example/v1");
});
test("changing the model invalidates a previously successful test",async()=>{
  const ui=app();ui.context.apiResult={seconds:1,text:"ok"};ui.run("api=async()=>apiResult");await ui.run("testConnection()");
  assert.equal(ui.elements.applyButton.textContent,"应用到小爱");
  ui.elements.model.value="different";ui.run("invalidateConnection();markDirty()");
  assert.equal(ui.elements.applyButton.textContent,"测试并应用到小爱");assert.equal(ui.elements.testResult.className,"result-box hidden");
});
test("late model list from a different interface is ignored",async()=>{
  const ui=app();let resolve;ui.context.pending=new Promise(r=>{resolve=r;});ui.run("api=()=>pending");
  const request=ui.run("$('loadModels').onclick()");
  ui.elements.profileSelect.value="backup";ui.run("chooseConnection('backup')");resolve({models:["old-interface-model"]});await request;
  assert.ok(!ui.elements.modelSelect.options.some(o=>o.value==="old-interface-model"));assert.equal(ui.elements.model.value,"second-model");
});
test("late test success cannot authorize applying a different draft",async()=>{
  const ui=app();let resolve;ui.context.pending=new Promise(r=>{resolve=r;});ui.run("api=()=>pending");const request=ui.run("testConnection()");
  ui.elements.model.value="new-model";ui.run("invalidateConnection()");resolve({seconds:1,text:"ok"});
  await assert.rejects(request,/设置已改变/);assert.equal(ui.run("testedDraft"),"");
});
test("failed test never saves settings or restarts the container",async()=>{
  const ui=app(), calls=[];ui.context.fakeApi=async endpoint=>{calls.push(endpoint);throw new Error("invalid key");};ui.run("api=fakeApi");
  await assert.rejects(ui.run("save(true)"),/invalid key/);assert.deepEqual(calls,["test"]);
});
test("manual model entry remains available when model listing fails",async()=>{
  const ui=app();ui.context.fakeApi=async()=>{throw new Error("列表不可用");};ui.run("api=fakeApi");await ui.run("$('loadModels').onclick()");
  assert.match(ui.elements.modelHint.textContent,/手动填写/);
  ui.elements.modelSelect.value=form.MANUAL;ui.run("$('modelSelect').onchange()");assert.equal(ui.elements.manualModelField.classList.contains("hidden"),false);
});
test("one-click apply tests, saves and restarts in order",async()=>{
  const ui=app(),calls=[];ui.context.fakeApi=async endpoint=>{calls.push(endpoint);return endpoint==="test"?{seconds:1,text:"ok"}:endpoint==="settings"?fixture():{ok:true};};
  ui.run("api=fakeApi;confirmAction=async()=>true;reconnect=async()=>{}");await ui.run("save(true)");assert.deepEqual(calls,["test","settings","restart"]);
});
test("canceling the apply confirmation keeps saved configuration untouched",async()=>{
  const ui=app(),calls=[];ui.context.fakeApi=async endpoint=>{calls.push(endpoint);return {seconds:1,text:"ok"};};
  ui.run("api=fakeApi;confirmAction=async()=>false");await ui.run("save(true)");assert.deepEqual(calls,["test"]);
});
test("edits during the confirmation cannot be applied using an earlier successful test",async()=>{
  const ui=app(),calls=[];ui.context.fakeApi=async endpoint=>{calls.push(endpoint);return {seconds:1,text:"ok"};};
  ui.run("api=fakeApi;confirmAction=async()=>{$('model').value='changed';return true;}");
  await assert.rejects(ui.run("save(true)"),/设置已改变/);assert.deepEqual(calls,["test"]);
});
test("switching tabs cannot bypass the key requirement for a new interface",async()=>{
  const ui=app();ui.elements.profileSelect.value=form.NEW;ui.run("chooseConnection(ConnectionForm.NEW)");ui.elements.apiBase.value="https://first.example/v1";ui.elements.model.value="first-model";ui.run("tab('prompts')");
  await assert.rejects(ui.run("save(true)"),/密钥/);assert.equal(ui.run("activeTab"),"connection");
});
