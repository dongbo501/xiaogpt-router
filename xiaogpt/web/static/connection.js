"use strict";
// Pure draft helpers shared by the form and its offline regression tests.
const ConnectionForm = {
  NEW: "__new__",
  MANUAL: "__manual__",
  normalize(value) {
    const text = String(value || "").trim().replace(/\/+$/, "");
    let url;
    try { url = new URL(text); } catch { throw new Error("请在第 1 步填写完整接口地址，以 https:// 或 http:// 开头。"); }
    if (!["https:", "http:"].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash || /\s/.test(text))
      throw new Error("接口地址中不能包含密钥、账号、空格或查询参数，请只粘贴接口地址。");
    let path = url.pathname.replace(/\/+$/, "").replace(/\/chat\/completions$/, "");
    return url.origin + (path || "/v1");
  },
  source(data, choice) {
    if (choice === this.NEW) return {api_base:"", model:"", key_saved:false, profile_id:""};
    if (!choice) return {api_base:data.config.api_base || "https://api.openai.com/v1", model:data.config.gpt_options.model || "", key_saved:data.key_saved, profile_id:""};
    const profile = data.profiles.find(p => p.id === choice);
    if (!profile) throw new Error("这个已保存接口已不存在，请刷新页面后重试。");
    return {...profile, profile_id:profile.id};
  },
  prepare(data, choice, draft, requireModel = true) {
    const source = this.source(data, choice), address = this.normalize(draft.api_base);
    const key = draft.openai_key.trim(), model = draft.model.trim();
    if (key && (/\s/.test(key) || /^bearer\s/i.test(key))) throw new Error("请只粘贴密钥本身，不要包含空格或 Bearer 前缀。");
    if (!key && (!source.key_saved || address !== this.normalize(source.api_base)))
      throw new Error("请在第 1 步填写这个接口的密钥。已有接口可以从下拉菜单直接选择，不用重填。");
    if (requireModel && !model) throw new Error("请在第 2 步选择模型，或选择“手动填写模型名称”。");
    return {api_base:address, openai_key:key, model, profile_id:source.profile_id};
  },
  identity(draft) { return JSON.stringify([draft.api_base.trim(), draft.openai_key.trim(), draft.profile_id, draft.model.trim()]); },
  modelOptions(models, current) {
    const unique = [...new Set(models)].filter(m => typeof m === "string" && m && m !== this.MANUAL).sort();
    return current && !unique.includes(current) ? [current, ...unique] : unique;
  }
};
if (typeof module !== "undefined") module.exports = ConnectionForm;
