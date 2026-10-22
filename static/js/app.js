// app.js —— 前端状态管理 + 事件绑定 + DOM 渲染（零框架，纯原生）
// 核心思想：用一个普通 JS 对象 state 当"数据仓库"，任何数据变化后重新渲染对应 DOM。
// 没有 React 的虚拟 DOM，我们手动增删 DOM 节点——更直白，也更好理解原理。
import * as api from "./api.js";

// —— 全局状态（替代框架的 store）——
const state = {
  sessions: [],     // 会话列表 [{id,title,created_at}]
  currentId: null,  // 当前选中的会话 id
  messages: [],     // 当前会话的消息历史 [{role, content}]
  streaming: false, // 流式进行中标志：true 时禁止再发起 send()
};

// 请求序号：每次"选中会话"自增 1，用于丢弃过期的异步响应（防乱序渲染）
let reqSeq = 0;
// 小工具：$ 是 document.getElementById 的缩写，按 id 取页面元素
const $ = (id) => document.getElementById(id);

// ==================== 时间处理 ====================

// 解析后端时间字符串 → 毫秒时间戳。
// 坑：库里存的是 UTC（db.py 连接时 SET time_zone='+00:00'），而 Python 的
// str(datetime) 输出不带时区信息（如 "2026-10-19 02:30:00"），
// JS 的 new Date() 会把这种格式当【本地时区】解析 → 在北京时区差 8 小时。
// 所以：检测到没有时区标记就补 "Z"，强制按 UTC 解析，再交给 JS 转本地时间显示。
function parseUTC(iso) {
  if (!iso) return NaN;
  let s = String(iso).trim().replace(" ", "T");
  if (!/(Z|[+-]\d{2}:?\d{2})$/i.test(s)) s += "Z";
  return new Date(s).getTime();
}

// 相对时间：刚刚 / N 分钟前 / N 小时前 / N 天前；超过 30 天改为显示日期
function relTime(iso) {
  const t = parseUTC(iso);
  if (isNaN(t)) return "";
  const diff = Date.now() - t;
  const MIN = 60e3, HOUR = 60 * MIN, DAY = 24 * HOUR;
  if (diff < MIN) return "刚刚";
  if (diff < HOUR) return Math.floor(diff / MIN) + " 分钟前";
  if (diff < DAY) return Math.floor(diff / HOUR) + " 小时前";
  if (diff < 30 * DAY) return Math.floor(diff / DAY) + " 天前";
  const d = new Date(t);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// 从 JWT 解出用户名，显示在侧栏底部。
// JWT = header.payload.signature 三段，payload 是 base64url 编码的 JSON，在第二段。
// 注意：这里只【解码展示】，不做验签——真正的校验在服务端 get_current_user。
function whoami() {
  const t = api.getToken();
  if (!t) return "";
  try {
    const seg = t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = seg + "=".repeat((4 - (seg.length % 4)) % 4);   // base64 要补足 4 的倍数
    const bytes = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
    // TextDecoder 按 UTF-8 解码，避免中文用户名被 atob 的 latin1 搞乱
    return JSON.parse(new TextDecoder().decode(bytes)).username || "";
  } catch {
    return "";
  }
}

// ==================== 启动 ====================

// 启动：有 token 进主界面，否则进登录
async function boot() {
  const name = whoami();
  if (name) $("user-name").textContent = name;
  if (api.getToken()) {
    showApp();
    // refreshSessions 抛 401（token 失效）时不能中断启动，否则 loadConfig 不执行、界面半残
    try { await refreshSessions(); } catch (e) { console.error("拉会话列表失败", e); }
    loadConfig();          // 拉运行配置（模型/权限），失败不阻塞主流程
  } else {
    showAuth();
  }
}

// 登录 / 注册：成功后存 token，切到主界面并拉会话
async function doAuth(mode) {
  const u = $("auth-user").value.trim();
  const p = $("auth-pass").value;
  try {
    const data = mode === "login"
      ? await api.login(u, p)
      : await api.register(u, p);
    api.setToken(data.access_token);   // 关键：把 JWT 存进 localStorage
    const name = whoami();
    if (name) $("user-name").textContent = name;
    showApp();
    await refreshSessions();
    loadConfig();          // 登录/注册成功后也拉一次运行配置，否则胶囊停在"配置不可用"
  } catch (e) {
    $("auth-msg").textContent = e.message;
  }
}

// ==================== 会话列表 ====================

// 拉会话列表并渲染
async function refreshSessions() {
  state.sessions = await api.listSessions();
  renderSessions();
}

// 渲染左侧会话列表（每次数据变都重画一次）
function renderSessions() {
  const box = $("session-list");
  box.innerHTML = "";                          // 先清空，再重建
  $("session-count").textContent = "(" + state.sessions.length + ")";
  for (const s of state.sessions) {
    const li = document.createElement("div");
    li.className = "session-item" + (s.id === state.currentId ? " active" : "");
    li.onclick = () => selectSession(s.id);

    const title = document.createElement("div");
    title.className = "si-title";
    title.textContent = s.title || "(未命名)";

    const time = document.createElement("div");
    time.className = "si-time";
    time.textContent = relTime(s.created_at);  // 相对时间，如 "3 天前"

    li.appendChild(title);
    li.appendChild(time);
    box.appendChild(li);
  }
}

// 新建会话
async function newSession() {
  const s = await api.createSession("新会话");
  state.sessions.unshift(s);           // 插到列表最前
  renderSessions();
  selectSession(s.id);
}

// 选中会话：更新顶栏标题 → 清空消息区 → 拉历史并逐条渲染
async function selectSession(id) {
  const my = ++reqSeq;                   // 抢占本次序号（单调递增）
  state.currentId = id;
  const cur = state.sessions.find((s) => s.id === id);
  $("chat-title").textContent = cur ? (cur.title || "新会话") : "新会话";
  state.messages = [];
  renderSessions();
  $("messages").innerHTML = "";
  toggleEmpty();
  refreshSessionStatus(id, my);          // 传入序号，两个请求共用一个守卫
  try {
    const msgs = await api.getMessages(id);
    if (my !== reqSeq) return;           // 期间又点了别的会话 → 丢弃这次响应
    state.messages = msgs;
    for (const m of msgs) {
      addMessage(m.role, m.content);
    }
  } catch (e) {
    console.error("加载历史失败", e);
  }
}

// ==================== 图片上传 ====================
// 待发送的图片（base64 data URI）。为什么用 base64：云端模型访问不到本机文件路径，
// 只能把图片数据本身随请求发过去。
let pendingFiles = [];
// 读取链：保证"选完文件立刻点发送"时也等图片全部读完（FileReader 是异步的）
let pendingReads = Promise.resolve();

// 把 File 读成 base64 data URI（包装成 Promise，方便 await）
function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// 点「+」触发文件选择
function onAttachClick() { $("file-input").click(); }

// 渲染待发送附件缩略图：选完文件后有明确反馈，点缩略图可移除
function renderAttachPreview() {
  const box = $("attach-preview");
  if (!box) return;
  box.innerHTML = "";
  pendingFiles.forEach((src, i) => {
    const img = document.createElement("img");
    img.src = src;
    img.title = "点击移除";
    img.onclick = () => { pendingFiles.splice(i, 1); renderAttachPreview(); };
    box.appendChild(img);
  });
}

// 把单个文件排进读取队列（"选择文件"和"Ctrl+V 粘贴"共用同一条队列）
function queueFile(file) {
  pendingReads = pendingReads
    .then(() => readFileAsDataURL(file))
    .then((b64) => { pendingFiles.push(b64); renderAttachPreview(); });
}

// 选中文件后：串行读取，全部读进 pendingFiles，并实时刷新缩略图
function onFileChange() {
  const input = $("file-input");
  for (const f of Array.from(input.files)) queueFile(f);
  input.value = "";   // 清空，允许下次重复选同一个文件
}

// Ctrl+V 直接粘贴图片：从剪贴板取图片文件，走同一条读取队列
function onPaste(e) {
  const items = (e.clipboardData && e.clipboardData.items) || [];
  let hasImage = false;
  for (const it of items) {
    if (it.kind === "file" && it.type.startsWith("image/")) {
      const f = it.getAsFile();
      if (f) { queueFile(f); hasImage = true; }
    }
  }
  // 粘贴的是图片：阻止浏览器把二进制内容塞进 textarea（会造成乱码）
  if (hasImage) e.preventDefault();
}

// ==================== 发消息 ====================

// 发送消息（流式）：先放用户气泡 + 一条空 AI 气泡，再边收边填
async function send() {
  const input = $("input");
  const text = input.value.trim();
  if (!state.currentId) return;
  if (state.streaming) return;           // 上一轮流式未结束，禁止并发发起
  await pendingReads;                    // 先等图片读完，才能判断"是不是只有附件"
  const hasImages = pendingFiles.length > 0;
  if (!text && !hasImages) return;       // 文字和附件都为空，才不发送
  state.streaming = true;                // 上锁
  const sid = state.currentId;           // 记下本次会话 id，回调里校验用
  input.value = "";
  autoGrow();                            // 复位输入框高度
  const images = pendingFiles.slice();   // 本次要发送的图片
  pendingFiles = [];                     // 取走后清空，避免下一轮重复发送
  renderAttachPreview();                 // 清空缩略图
  // 只有图没有字时 message 仍需非空：后端 ChatRequest.message 有 min_length=1
  const message = text || "(图片)";
  addMessage("user", message);
  const aiId = addMessage("ai", "");
  try {
    await api.streamChat(sid, message, (chunk) => {
      const el = $(aiId);
      // 双保险：① 节点可能已被 selectSession 的 innerHTML="" 销毁
      //         ② 流式期间用户可能已切换到别的会话
      if (!el || state.currentId !== sid) return;
      el.textContent += chunk;           // 每来一个片段就追加，实现"逐字显示"
      $("messages").scrollTop = $("messages").scrollHeight;  // 自动滚到底
    }, images);
  } catch (e) {
    // 防御性错误处理：节点可能已不存在，不能假设出错时现场还完好
    const el = $(aiId);
    if (el) el.textContent = "[错误] " + e.message;
  } finally {
    state.streaming = false;             // 无论成功失败都要释放锁
  }
}


// textarea 自动增高：先复位再按内容高度撑开，封顶 160px 后内部滚动
function autoGrow() {
  const el = $("input");
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 160) + "px";
}

// 往消息区加一条消息，返回元素 id 供后续更新（流式填充用）
function addMessage(role, content) {
  const id = "m" + Math.random().toString(36).slice(2);  // 随机 id 保证唯一
  const div = document.createElement("div");
  // 历史接口返回的 role 取值是 user/assistant/tool，而流式 send() 用 "ai"；
  // 此处统一映射，保证历史里的 AI 消息也命中 CSS 的 .msg.ai 样式
  const cls = role === "assistant" ? "ai" : role;
  div.className = "msg " + cls;
  div.id = id;
  div.textContent = content;
  $("messages").appendChild(div);
  toggleEmpty();
  return id;
}

// 空状态提示：消息区没有子节点时显示引导语
function toggleEmpty() {
  $("empty-hint").style.display = $("messages").children.length ? "none" : "flex";
}

// ==================== 运行配置 / 会话状态 ====================

// 拉运行配置（模型 + 权限规则），渲染输入栏胶囊和浮层内容。
// 失败只降级（胶囊显示"配置不可用"），不阻塞聊天主流程。
async function loadConfig() {
  try {
    const cfg = await api.getConfig();
    $("model-name").textContent = cfg.model || "未知模型";
    $("model-detail").textContent =
      `${cfg.provider} · ${cfg.base_url || "(自定义地址)"}`;

    const modeText = { allow: "全部放行", ask: "非白名单命令需确认", deny: "全部拒绝" };
    $("perm-mode").textContent =
      `模式：${modeText[cfg.permission_mode] || cfg.permission_mode}（execute_command）`;

    const white = $("perm-white");
    white.innerHTML = "";
    for (const cmd of cfg.permission_whitelist || []) {
      const li = document.createElement("li");
      li.textContent = cmd;
      white.appendChild(li);
    }
    if (!(cfg.permission_whitelist || []).length) {
      const li = document.createElement("li");
      li.textContent = "（空）";
      white.appendChild(li);
    }

    const tools = $("model-tools");
    tools.innerHTML = "";
    for (const t of cfg.tools || []) {
      const li = document.createElement("li");
      li.textContent = t;
      tools.appendChild(li);
    }
    if (!(cfg.tools || []).length) {
      const li = document.createElement("li");
      li.textContent = "（无）";
      tools.appendChild(li);
    }

    loadProviders();   // 渲染"切换模型"列表（失败不阻塞配置展示）
  } catch (e) {
    $("model-name").textContent = "配置不可用";
    console.error("加载运行配置失败", e);
  }
}

// ==================== 切换模型 ====================

// 拉可选模型列表，渲染到"切换模型"浮层；当前项高亮，支持图片的加徽标
async function loadProviders() {
  const box = $("model-options");
  if (!box) return;
  try {
    const list = await api.getProviders();
    const curProvider = ($("model-detail").textContent || "").split(" · ")[0];
    box.innerHTML = "";
    for (const p of list) {
      const li = document.createElement("li");
      if (p.key === curProvider) li.classList.add("active");

      const name = document.createElement("span");
      // custom 没有预置模型名，不要在末尾留个空的 " · "
      name.textContent = p.model ? `${p.label} · ${p.model}` : p.label;
      li.appendChild(name);

      // 支持图片的模型加徽标，方便配合图片上传功能选对模型
      if (p.supports_images) {
        const badge = document.createElement("span");
        badge.className = "badge-img";
        badge.textContent = "支持图片";
        li.appendChild(badge);
      }

      // custom 需要手填地址/模型名：点击只是展开表单，不直接切换
      li.onclick = () => (p.custom ? toggleCustomForm() : switchModel(p.key));
      box.appendChild(li);
    }
  } catch (e) {
    console.error("加载模型列表失败", e);
  }
}

// 切换模型：调后端热切换 → 重新拉配置刷新胶囊 → 关闭浮层
async function switchModel(provider) {
  try {
    await api.setModel(provider);
    await loadConfig();
    $("pop-model").hidden = true;
  } catch (e) {
    alert(e.message);   // 失败直接告诉用户原因（未知 provider / 网络等）
  }
}

// 展开/收起"自定义模型"表单
function toggleCustomForm() {
  const form = $("custom-form");
  if (!form) return;
  form.hidden = !form.hidden;
  if (!form.hidden) $("custom-base").focus();
}

// 应用自定义模型：Base URL + 模型名必填，API Key 可选
async function applyCustomModel() {
  const baseUrl = ($("custom-base").value || "").trim();
  const model = ($("custom-model").value || "").trim();
  const apiKey = ($("custom-key").value || "").trim();
  if (!baseUrl || !model) {
    alert("自定义模型需要同时填写 Base URL 和模型名");
    return;
  }
  try {
    await api.setModel("custom", { baseUrl, model, apiKey });
    await loadConfig();
    $("custom-form").hidden = true;
    $("pop-model").hidden = true;
  } catch (e) {
    alert(e.message);
  }
}

// 拉会话状态：消息条数 + 上下文压缩情况，渲染在顶栏标题旁
// my 是调用方（selectSession）抢占的请求序号，两个请求共用一个守卫，
// 避免 refreshSessionStatus 自己再 ++reqSeq 把上一条请求判成过期
async function refreshSessionStatus(id, my) {
  try {
    const st = await api.getSessionStatus(id);
    if (my !== undefined && my !== reqSeq) return;   // 已有更新的请求，丢弃
    const meta = $("chat-meta");
    meta.textContent = `${st.message_count} 条消息`;
    if (st.compressed) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = `上下文已压缩 · 摘要 ${st.summary_chars} 字`;
      meta.appendChild(tag);
    }
  } catch (e) {
    $("chat-meta").textContent = "";
    console.error("加载会话状态失败", e);
  }
}

// 浮层开关：点同一个胶囊关闭，点另一个则互斥切换
function togglePopover(id) {
  const target = $(id);
  const willShow = target.hidden;
  $("pop-perm").hidden = true;
  $("pop-model").hidden = true;
  target.hidden = !willShow;
}

// ==================== 界面切换 ====================

// auth 与 app 两个容器互斥显示
function showAuth() { $("auth").style.display = "block"; $("app").style.display = "none"; }
function showApp()  { $("auth").style.display = "none";   $("app").style.display = "flex"; }

// 登出：清 token 后刷新页面回到登录态
function logout() { api.clearToken(); location.reload(); }

// ==================== 事件绑定 ====================

// 事件绑定：等 DOM 就绪再绑，避免取不到元素
window.addEventListener("DOMContentLoaded", () => {
  $("btn-login").onclick = () => doAuth("login");
  $("btn-register").onclick = () => doAuth("register");
  $("btn-new").onclick = newSession;
  $("btn-send").onclick = send;
  $("btn-logout").onclick = logout;

  // 胶囊点击 → 弹出对应浮层；stopPropagation 防止被下面的全局关闭逻辑立刻关掉
  $("pill-perm").onclick = (e) => { e.stopPropagation(); togglePopover("pop-perm"); };
  $("pill-model").onclick = (e) => { e.stopPropagation(); togglePopover("pop-model"); };
  $("btn-attach").onclick = onAttachClick;
  $("file-input").onchange = onFileChange;
  document.addEventListener("paste", onPaste);   // Ctrl+V 粘贴图片到聊天框
  $("btn-custom-ok").onclick = applyCustomModel; // 自定义模型表单的"使用"按钮

  // 点页面其他位置关闭浮层
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".popover") && !e.target.closest(".pill")) {
      $("pop-perm").hidden = true;
      $("pop-model").hidden = true;
    }
  });

  const input = $("input");
  // Enter 发送，Shift+Enter 换行（preventDefault 阻止 textarea 默认插入换行）
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  input.addEventListener("input", autoGrow);   // 打字时自动撑高

  boot();
});
