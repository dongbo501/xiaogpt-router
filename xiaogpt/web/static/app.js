"use strict";
const $ = id => document.getElementById(id);
let csrf = "", data = null, dirty = false, reconnecting = false, toastTimer;
const titles = {overview:"运行概览",connection:"接口与模型",prompts:"提示词与播报",voice:"音箱与声音",maintenance:"维护与安全"};
const fieldIds = ["profileSelect","providerSelect","apiBase","apiKey","model","stream","temperature","maxTokens","tokenField","reasoning","prompt","waitingPrompt","rateLimitPrompt","quotaPrompt","errorPrompt","tts","voice","rate","volume","pitch","muteXiaoai","keywords","startConversation","endConversation","hardware","miDid","useCommand"];

function toast(message, error = false) {
  clearTimeout(toastTimer); $("toast").textContent = message;
  $("toast").className = "toast" + (error ? " error" : "");
  toastTimer = setTimeout(() => $("toast").classList.add("hidden"), error ? 8000 : 4500);
}
function option(select, value, text) { const node = document.createElement("option"); node.value = value; node.textContent = text; select.append(node); }
function selectValue(id, value) { const el = $(id); value = String(value); if (![...el.options].some(o=>o.value===value)) option(el,value,value); el.value=value; }
function markDirty() { dirty = true; $("saveState").textContent = "有尚未保存的修改"; }
function tab(name) {
  if (!titles[name]) name = "overview";
  document.querySelectorAll("[data-tab]").forEach(el => el.classList.toggle("active", el.dataset.tab === name));
  document.querySelectorAll("[data-panel]").forEach(el => el.classList.toggle("active", el.dataset.panel === name));
  $("pageTitle").textContent = titles[name]; history.replaceState(null,"","#"+name);
}
async function api(path, method = "GET", payload) {
  const response = await fetch("/api/" + path, {method, credentials:"same-origin", headers:{"Content-Type":"application/json", "X-CSRF-Token":csrf}, body:payload === undefined ? undefined : JSON.stringify(payload)});
  const result = await response.json().catch(()=>({error:"服务器暂时不可用"}));
  if (!response.ok) {
    if (response.status === 401 && path !== "login" && !reconnecting) showLogin();
    throw new Error(result.error || "操作失败，请重试");
  }
  return result;
}
async function busy(button, action, label = "处理中…") {
  if (button.disabled) return;
  const text = button.textContent; button.disabled = true; button.textContent = label;
  try { return await action(); } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = text; }
}
function confirmAction(title, text) {
  return new Promise(resolve=>{
    const dialog=$("confirmDialog"); $("confirmTitle").textContent=title; $("confirmText").textContent=text;
    const finish=value=>{dialog.close(); resolve(value);};
    $("confirmOk").onclick=()=>finish(true); $("confirmCancel").onclick=()=>finish(false);
    dialog.oncancel=event=>{event.preventDefault();finish(false);}; dialog.showModal();
  });
}
function showLogin() { $("loginView").classList.remove("hidden"); $("appView").classList.add("hidden"); }
function profileOptions(selected = "") {
  $("profileSelect").replaceChildren(); option($("profileSelect"),"","当前配置 / 手动填写");
  data.profiles.forEach(p=>option($("profileSelect"),p.id,p.name)); $("profileSelect").value=selected;
}
function renderSettings(next) {
  data = next; const c=data.config;
  profileOptions(data.active_profile);
  $("providerSelect").replaceChildren(); data.providers.forEach((p,i)=>option($("providerSelect"),String(i),p.name));
  $("apiBase").value=c.api_base||"https://api.openai.com/v1"; syncProvider();
  $("apiKey").value=""; $("apiKey").type="password"; $("toggleKey").textContent="显示";
  $("keyHint").textContent=data.key_saved?"已保存密钥，留空继续使用。更换地址时须填写对应密钥，或选择已保存方案。":"尚未保存密钥，请填写服务商提供的 API Key。";
  const g=c.gpt_options||{}; $("model").value=g.model||"";
  $("stream").checked=Boolean(c.stream); $("temperature").value=g.temperature??"";
  $("maxTokens").value=g.max_completion_tokens??g.max_tokens??"";
  $("tokenField").value=g.max_tokens!==undefined?"max_tokens":"max_completion_tokens";
  selectValue("reasoning",g.reasoning_effort||"");
  for (const [id,key] of [["prompt","prompt"],["waitingPrompt","waiting_prompt"],["rateLimitPrompt","rate_limit_prompt"],["quotaPrompt","quota_prompt"],["errorPrompt","error_prompt"],["startConversation","start_conversation"],["endConversation","end_conversation"]]) $(id).value=c[key]||"";
  $("promptPreset").value=""; selectValue("tts",c.tts||"edge");
  $("voiceList").replaceChildren(); data.voices.forEach(v=>option($("voiceList"),v.id,v.name));
  const t=c.tts_options||{}; $("voice").value=t.voice||"zh-CN-XiaoxiaoNeural";
  for (const [id,suffix,values] of [["rate","%",[-50,-25,-10,0,10,25,50,100]],["volume","%",[-100,-50,-25,0,20,40,60,80,100]],["pitch","Hz",[-50,-25,-10,0,10,25,50]]]) {
    $(id).replaceChildren(); values.forEach(n=>option($(id),(n>=0?"+":"")+n+suffix,n===0?"默认（+0"+suffix+"）":(n>=0?"+":"")+n+suffix));
    selectValue(id,t[id]||"+0"+suffix);
  }
  $("keywords").value=(c.keyword||[]).join("\n"); $("muteXiaoai").checked=Boolean(c.mute_xiaoai);
  $("hardware").replaceChildren(); data.hardware.forEach(h=>option($("hardware"),h,h)); selectValue("hardware",c.hardware||"L05C");
  $("miDid").value=c.mi_did||""; $("useCommand").checked=Boolean(c.use_command);
  $("configPath").textContent=data.config_file; dirty=false; $("saveState").textContent="设置已同步";
  $("summaryModel").textContent=g.model||"未设置";
  try { $("summaryProvider").textContent=new URL(c.api_base).hostname; } catch { $("summaryProvider").textContent="自定义接口"; }
  $("summaryVoice").textContent=c.tts==="edge"?(data.voices.find(v=>v.id===t.voice)?.name.split(" · ")[0]||"Edge 语音"):"小爱原声";
  $("keywordPreview").replaceChildren(); (c.keyword||[]).forEach(k=>{const el=document.createElement("span");el.className="chip";el.textContent=k;$("keywordPreview").append(el);});
}
function syncProvider() {
  const normalized=$("apiBase").value.trim().replace(/\/$/,"");
  const index=data.providers.findIndex(p=>p.url===normalized);
  $("providerSelect").value=String(index>=0?index:data.providers.length-1);
}
function connection() { return {api_base:$("apiBase").value.trim(),openai_key:$("apiKey").value,model:$("model").value.trim(),profile_id:$("profileSelect").value}; }
function payload() {
  const c=structuredClone(data.config), g={...c.gpt_options};
  g.model=$("model").value.trim();
  for (const key of ["temperature","max_tokens","max_completion_tokens","reasoning_effort"]) delete g[key];
  if ($("temperature").value!=="") g.temperature=Number($("temperature").value);
  if ($("maxTokens").value!=="") g[$("tokenField").value]=Number($("maxTokens").value);
  if ($("reasoning").value) g.reasoning_effort=$("reasoning").value;
  c.gpt_options=g; c.api_base=$("apiBase").value.trim(); c.stream=$("stream").checked;
  for (const [id,key] of [["prompt","prompt"],["waitingPrompt","waiting_prompt"],["rateLimitPrompt","rate_limit_prompt"],["quotaPrompt","quota_prompt"],["errorPrompt","error_prompt"],["startConversation","start_conversation"],["endConversation","end_conversation"]]) c[key]=$(id).value;
  c.tts=$("tts").value; c.tts_options={...c.tts_options,voice:$("voice").value.trim(),rate:$("rate").value,volume:$("volume").value,pitch:$("pitch").value};
  c.keyword=$("keywords").value.split(/[\n,，、]+/).map(s=>s.trim()).filter(Boolean);
  c.mute_xiaoai=$("muteXiaoai").checked; c.hardware=$("hardware").value; c.mi_did=$("miDid").value.trim(); c.use_command=$("useCommand").checked;
  return {revision:data.revision,config:c,openai_key:$("apiKey").value,profile_id:$("profileSelect").value};
}
async function updateStatus() {
  if (reconnecting) return;
  const s=await api("status"); const ready=s.ready;
  $("statusPill").textContent=ready?"服务运行中":s.running?"语音服务启动中":"语音服务等待恢复";
  $("statusPill").className="pill"+(ready?"":" neutral"); $("summaryService").textContent=ready?"正在聆听":s.running?"正在启动":"检查设置";
  const minutes=Math.floor(s.uptime/60); $("summaryUptime").textContent="本次运行 "+(minutes>=60?Math.floor(minutes/60)+" 小时 ":"")+minutes%60+" 分钟 · v"+s.version;
  $("pendingNotice").classList.toggle("hidden",!s.pending_changes);
  if (!dirty) $("saveState").textContent=s.pending_changes?"已保存，待重启生效":"设置已生效";
}
async function reconnect() {
  reconnecting=true; $("statusPill").textContent="正在重启"; $("statusPill").className="pill neutral";
  const started=Date.now(); await new Promise(r=>setTimeout(r,2000));
  try {
    while (Date.now()-started<90000) {
      try {
        const s=await api("status");
        if (s.ready && s.uptime<90 && !s.restarting) { reconnecting=false;renderSettings(await api("settings"));await updateStatus();toast("容器已重启，设置已生效");return; }
      } catch {}
      await new Promise(r=>setTimeout(r,1500));
    }
    throw new Error("等待重启超时，请刷新页面或检查容器状态；已保存的设置不会丢失。");
  } finally { reconnecting=false; }
}
async function restart(ask=true) {
  if (ask && !await confirmAction("重启容器？",dirty?"页面还有未保存的修改。此次只应用已保存的配置；未保存修改将丢失。":"当前问答会短暂中断，容器将重新加载配置，页面会自动连接。")) return;
  await api("restart","POST",{}); toast("正在重启容器，请稍候…"); await reconnect();
}
async function save(apply) {
  if (apply && !await confirmAction("保存并应用设置？","当前配置会自动备份。容器会重启一次，当前问答将短暂中断。")) return;
  renderSettings(await api("settings","POST",payload()));
  if (apply) await restart(false); else {await updateStatus();toast("已保存并备份，重启容器后生效");}
}
async function testConnection(current=false) {
  const result=$("testResult");result.className="result-box";result.textContent="正在请求模型，最多等待 45 秒…";
  try {
    const params=current?{api_base:data.config.api_base,model:data.config.gpt_options.model,openai_key:"",profile_id:""}:connection();
    const response=await api("test","POST",params);
    result.textContent="连接成功 · "+response.seconds+" 秒\n"+response.text;
    toast("接口测试成功，已收到真实回答");
  } catch(error) {result.classList.add("error");result.textContent=error.message;throw error;}
}
async function boot() {
  const session=await api("session");if (!session.authenticated){showLogin();return;}
  csrf=session.csrf; $("initialNotice").classList.toggle("hidden",!session.initial_password);
  renderSettings(await api("settings")); $("loginView").classList.add("hidden");$("appView").classList.remove("hidden");
  tab(location.hash.slice(1));await updateStatus();
}
$("loginForm").addEventListener("submit",async event=>{event.preventDefault();$("loginError").textContent="";try {const r=await api("login","POST",{password:$("loginPassword").value});csrf=r.csrf;$("loginPassword").value="";await boot();}catch(e){$("loginError").textContent=e.message;}});
document.querySelectorAll("[data-tab]").forEach(el=>el.onclick=()=>tab(el.dataset.tab));
document.querySelectorAll("[data-go]").forEach(el=>el.onclick=()=>tab(el.dataset.go));
fieldIds.forEach(id=>$(id).addEventListener("input",markDirty));
$("refreshButton").onclick=()=>busy($("refreshButton"),updateStatus);
$("saveButton").onclick=()=>busy($("saveButton"),()=>save(false),"保存中…");
$("applyButton").onclick=()=>busy($("applyButton"),()=>save(true),"正在应用…");
for(const id of ["restartButton","pendingRestart"]) $(id).onclick=()=>busy($(id),()=>restart(),"正在重启…");
$("logoutButton").onclick=()=>busy($("logoutButton"),async()=>{await api("logout","POST",{});showLogin();});
$("providerSelect").onchange=()=>{const p=data.providers[Number($("providerSelect").value)]; if(p.url)$("apiBase").value=p.url;$("profileSelect").value="";$("apiKey").value="";markDirty();};
$("apiBase").addEventListener("change",syncProvider);
$("profileSelect").onchange=()=>{const p=data.profiles.find(p=>p.id===$("profileSelect").value);if(!p)return;$("apiBase").value=p.api_base;$("model").value=p.model;$("apiKey").value="";$("profileName").value=p.name;syncProvider();markDirty();};
$("toggleKey").onclick=()=>{const hidden=$("apiKey").type==="password";$("apiKey").type=hidden?"text":"password";$("toggleKey").textContent=hidden?"隐藏":"显示";};
$("saveProfile").onclick=()=>busy($("saveProfile"),async()=>{const result=await api("profiles","POST",{...connection(),name:$("profileName").value});const updated=await api("settings");data.profiles=updated.profiles;profileOptions(result.id);$("apiKey").value="";toast("接口方案已保存；应用到小爱还需保存并重启");markDirty();});
$("deleteProfile").onclick=()=>busy($("deleteProfile"),async()=>{const id=$("profileSelect").value;if(!id)throw new Error("请先选择一个已保存的方案");if(!await confirmAction("删除接口方案？","只删除方案列表中的记录，不改变当前运行配置。"))return;await api("profiles","DELETE",{id});const updated=await api("settings");data.profiles=updated.profiles;profileOptions();toast("方案已删除");});
$("loadModels").onclick=()=>busy($("loadModels"),async()=>{const result=await api("models","POST",connection());$("modelList").replaceChildren();result.models.forEach(m=>option($("modelList"),m,m));toast("已读取 "+result.models.length+" 个模型，点击模型输入框选择");$("model").focus();},"读取模型中…");
$("testConnection").onclick=()=>busy($("testConnection"),()=>testConnection(),"测试中…");
$("quickTest").onclick=()=>{tab("connection");busy($("testConnection"),()=>testConnection(true),"测试中…");};
const speechRules="请使用自然、清楚的中文纯文本口播，不输出 Markdown、表格、网址和引用标记，不复述链接，不追问或邀请继续提问。不能确认的内容直接说明不确定。";
$("promptPreset").onchange=async()=>{const value=$("promptPreset").value;if(!value)return;if(!await confirmAction("使用提示词模板？","文本框中的提示词会替换为所选模板。保存前你仍可以修改。")){ $("promptPreset").value="";return; }$("prompt").value=({natural:"你是一位通过智能音箱回答问题的中文助手，直接回答本轮问题，按内容需要决定长度。",brief:"你是一位中文语音助手，优先给出结论，再简要说明关键理由，避免重复和铺垫。",explain:"你是一位耐心的中文讲解助手，用容易理解的语言和具体例子解释问题，必要时分步骤说明。"}[value])+speechRules;markDirty();};
$("commonKeywords").onclick=async()=>{if(await confirmAction("填入常用触发词？","将替换触发词文本框中的内容，保存前可以继续调整。")){$("keywords").value="请问\n请介绍\n请对比\n请解释\n补充";markDirty();}};
let devices=[];
$("loadDevices").onclick=()=>busy($("loadDevices"),async()=>{const r=await api("devices","POST",{});devices=r.devices;$("deviceSelect").replaceChildren();option($("deviceSelect"),"","请选择音箱");devices.forEach((d,i)=>option($("deviceSelect"),String(i),d.name+" / "+d.hardware));toast("已读取 "+devices.length+" 台音箱");},"读取中…");
$("deviceSelect").onchange=()=>{if($("deviceSelect").value==="")return;const d=devices[Number($("deviceSelect").value)];selectValue("hardware",d.hardware);$("miDid").value=d.mi_did;markDirty();};
$("loadLogs").onclick=()=>busy($("loadLogs"),async()=>{const r=await api("logs");$("logs").textContent=r.lines.join("\n")||"还没有语音服务日志。";$("logs").scrollTop=$("logs").scrollHeight;});
$("loadBackups").onclick=()=>busy($("loadBackups"),async()=>{const r=await api("backups");$("backupSelect").replaceChildren();option($("backupSelect"),"",r.backups.length?"选择恢复时间":"暂无备份，首次保存后自动创建");r.backups.forEach(b=>option($("backupSelect"),b.id,new Date(b.modified*1000).toLocaleString("zh-CN")));});
$("restoreBackup").onclick=()=>busy($("restoreBackup"),async()=>{const id=$("backupSelect").value;if(!id)throw new Error("请先选择一份备份");if(!await confirmAction("恢复这份备份？","会恢复当时的接口、密钥及其他设置，当前配置会先备份。恢复后还需重启容器。"))return;renderSettings(await api("restore","POST",{id,revision:data.revision}));await updateStatus();toast("备份已恢复，重启容器后生效");});
$("changePassword").onclick=()=>busy($("changePassword"),async()=>{const r=await api("password","POST",{current:$("oldPassword").value,password:$("newPassword").value});csrf=r.csrf;$("oldPassword").value="";$("newPassword").value="";$("initialNotice").classList.add("hidden");toast("管理密码已更新");});
window.addEventListener("beforeunload",event=>{if(dirty&&!reconnecting){event.preventDefault();event.returnValue="";}});
setInterval(()=>{if(!$("appView").classList.contains("hidden")&&!reconnecting)updateStatus().catch(()=>{$("statusPill").textContent="连接中断";$("statusPill").className="pill error";});},10000);
boot().catch(error=>{showLogin();$("loginError").textContent=error.message;});
