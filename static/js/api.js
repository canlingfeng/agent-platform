// api.js —— 前端与后端通信层（所有 HTTP 细节集中在这里）
// 为什么独立成一层：app.js 只管界面和状态，不碰 fetch；关注点分离（separation of concerns）。
// 前后端同源（都由 FastAPI 提供），所以 fetch 用相对路径 "/api/..."，不需要跨域（CORS 与此无关）。

const TOKEN_KEY = "agent_token";

// —— token 管理：用 Web Storage API 的 localStorage 持久化，刷新页面不丢 ——
// localStorage 是浏览器提供的键值存储，字符串读写，关页面仍在。
export function getToken() { return localStorage.getItem(TOKEN_KEY); }
export function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }
export function clearToken() { localStorage.removeItem(TOKEN_KEY); }

// 统一构造请求头：有 token 就带 Authorization（JWT 无状态鉴权的客户端落地方式）
function authHeaders() {
  const h = { "Content-Type": "application/json" };
  const t = getToken();
  if (t) h["Authorization"] = "Bearer " + t;
  return h;
}

// 注册：POST /api/v1/auth/register → { access_token, token_type, username }
export async function register(username, password) {
  const r = await fetch("/api/v1/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!r.ok) throw new Error("注册失败 " + r.status);
  return r.json();
}

// 登录：POST /api/v1/auth/login → 同样回 token。拿到 token 后调 setToken 存起来。
export async function login(username, password) {
  const r = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!r.ok) throw new Error("登录失败 " + r.status);
  return r.json();
}

// 会话列表：GET /api/v1/sessions（Bearer）。401 说明 token 失效，清掉重登。
export async function listSessions() {
  const r = await fetch("/api/v1/sessions", { headers: authHeaders() });
  if (r.status === 401) { clearToken(); throw new Error("登录已失效"); }
  if (!r.ok) throw new Error("获取会话失败 " + r.status);
  return r.json();   // [{ id, title, created_at }]
}

// 新建会话：POST /api/v1/sessions → { id, title, created_at }
export async function createSession(title) {
  const r = await fetch("/api/v1/sessions", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ title: title || "新会话" }),
  });
  if (r.status === 401) { clearToken(); throw new Error("登录已失效"); }
  if (!r.ok) throw new Error("创建会话失败 " + r.status);
  return r.json();
}

// 历史消息：GET /api/v1/sessions/{id}/messages → [{ role, content }]
export async function getMessages(sessionId) {
  const r = await fetch(`/api/v1/sessions/${sessionId}/messages`, { headers: authHeaders() });
  if (r.status === 401) { clearToken(); throw new Error("登录已失效"); }
  if (!r.ok) throw new Error("拉取历史失败 " + r.status);
  return r.json();   // [{ role, content }]
}

// 会话状态：GET /api/v1/sessions/{id}/status
// → { id, title, message_count, summary_chars, compressed }
// compressed=true 表示早期消息已被摘要压缩进 session.summary
export async function getSessionStatus(sessionId) {
  const r = await fetch(`/api/v1/sessions/${sessionId}/status`, { headers: authHeaders() });
  if (r.status === 401) { clearToken(); throw new Error("登录已失效"); }
  if (!r.ok) throw new Error("拉取会话状态失败 " + r.status);
  return r.json();
}

// 运行配置：GET /api/v1/config
// → { provider, model, base_url, tools, permission_mode, permission_whitelist }
// 注意：后端【绝不返回 api_key】，这里只拿展示用的脱敏信息
export async function getConfig() {
  const r = await fetch("/api/v1/config", { headers: authHeaders() });
  if (r.status === 401) { clearToken(); throw new Error("登录已失效"); }
  if (!r.ok) throw new Error("拉取运行配置失败 " + r.status);
  return r.json();
}

// 可选模型列表：GET /api/v1/providers
// → [{ key, label, model, supports_images }]
export async function getProviders() {
  const r = await fetch("/api/v1/providers", { headers: authHeaders() });
  if (r.status === 401) { clearToken(); throw new Error("登录已失效"); }
  if (!r.ok) throw new Error("拉取模型列表失败 " + r.status);
  return r.json();
}

// 切换模型：POST /api/v1/config → 新的运行配置
// opts 仅 provider="custom" 时需要：{ baseUrl, model, apiKey }
// 后端会热重建 LLM 并写回 config.json（重启后仍生效）
export async function setModel(provider, opts = {}) {
  const body = { provider };
  if (opts.baseUrl) body.base_url = opts.baseUrl;
  if (opts.model) body.model = opts.model;
  if (opts.apiKey) body.api_key = opts.apiKey;
  const r = await fetch("/api/v1/config", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(body),
  });
  if (r.status === 401) { clearToken(); throw new Error("登录已失效"); }
  if (!r.ok) {
    // 后端错误体：{"detail": {"error": "...", "message": "..."}}，优先取人话 message
    let msg = "切换模型失败 " + r.status;
    try {
      const body = await r.json();
      const d = body.detail;
      if (d && typeof d === "object" && d.message) msg = d.message;
      else if (typeof d === "string") msg = d;
    } catch { /* 非 JSON 错误体，保留默认文案 */ }
    throw new Error(msg);
  }
  return r.json();
}
// 非流式对话（兜底用）：POST /api/v1/chat → { reply, session_id }
export async function chatSync(sessionId, message) {
  const r = await fetch("/api/v1/chat", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!r.ok) throw new Error("对话失败 " + r.status);
  return r.json();
}

// 流式对话：POST /api/v1/chat/stream，用 Fetch Streams 消费 SSE（重点！）
// onChunk 是回调：每收到一个增量片段就调用一次，app.js 拿它去更新界面。
export async function streamChat(sessionId, message, onChunk, images = []) {
  const res = await fetch("/api/v1/chat/stream", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ session_id: sessionId, message, images }),
  });
  if (!res.ok) {
    // 后端的模型能力闸门返回 {"detail": {"error":"model_unsupported","message":"..."}}。
    // 这里把 message 提出来直接抛给界面，避免用户只看到"流式对话失败 400"这种模糊提示。
    let msg = "流式对话失败 " + res.status;
    try {
      const body = await res.json();
      const d = body.detail;
      if (d && typeof d === "object" && d.message) msg = d.message;
      else if (typeof d === "string") msg = d;
    } catch { /* 非 JSON 错误体，保留默认文案 */ }
    throw new Error(msg);
  }

  const reader = res.body.getReader();        // ① ReadableStream 读取器：逐块读字节
  const decoder = new TextDecoder("utf-8");    // ② 字节(Uint8Array)→字符串，UTF-8 中文不乱码
  let buf = "";                               // ③ 缓冲区：网络分块可能切断一帧，需拼接
  while (true) {
    const { value, done } = await reader.read(); // ④ 每次 read() 返回一块字节 {value, done}
    if (done) break;
    buf += decoder.decode(value, { stream: true }); // ⑤ 解码并拼进缓冲（stream:true 表示还有后续）
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {      // ⑥ SSE 一帧以空行(\n\n)结束
      const frame = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 2);
      if (!frame.startsWith("data:")) continue;       // ⑦ 只处理 data: 帧
      const raw = frame.slice(5).trim();              // ⑧ 去掉 "data: " 前缀（"data:" 是5字符）
      let payload;
      try { payload = JSON.parse(raw); }              // ⑨ 后端把内容 JSON 编码成单行，原文空行不再切断帧
      catch { continue; }                             //   解析失败（极少见）跳过，避免渲染半个碎片
      if (payload.done) return;                       // ⑩ 结束标记（{"done":true}），收工
      if (typeof payload.t === "string") onChunk(payload.t);  // ⑪ 增量片段交给上层渲染
    }
  }
}
