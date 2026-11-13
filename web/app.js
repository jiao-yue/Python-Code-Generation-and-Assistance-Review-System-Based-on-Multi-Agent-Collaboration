/* ==========================================================================
   豆包式聊天前端逻辑
   状态：新建对话即落库（刷新不丢失），会话与每轮结果由后端持久化（MySQL）
   接口：POST /api/conversations 新建即持久化；GET /api/conversations(+id) 读取历史；
         PATCH /api/conversations/{id} 自定义命名；DELETE /api/conversations/{id} 删除并重排编号；
         POST /api/chat/generate（代码生成）/ POST /api/chat/review（仅审核），均为 SSE 流式
   说明：历史项支持悬停重命名与删除，消息编号在删除后重排为连续序号，不引入图片/logo
   ========================================================================== */
"use strict";

/* ---------------- 全局状态 ---------------- */
let conversations = [];                 // 会话列表（部分可能尚未加载消息）
let currentId = null;                   // 当前会话 ID
let busy = false;                       // 是否正在请求
let editingId = null;                   // 正在重命名的会话 ID（同时只有一个）

/* ---------------- DOM 引用 ---------------- */
const messagesEl = document.getElementById("messages");
const mainInput = document.getElementById("mainInput");
const testInput = document.getElementById("testInput");
const testExtra = document.getElementById("testExtra");
const sendBtn = document.getElementById("sendBtn");
const historyList = document.getElementById("historyList");
const chatTitle = document.getElementById("chatTitle");
const headerSub = document.getElementById("headerSub");

/* ============================== 工具函数 ============================== */
function esc(s) {
  // 转义 HTML，防止用户输入/模型内容破坏页面结构
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function current() {
  return conversations.find((c) => c.id === currentId) || null;
}

function newId() {
  return "c_" + Date.now() + "_" + Math.random().toString(36).slice(2, 6);
}

/* ============================== 会话管理 ============================== */
async function newConversation(mode) {
  const m = mode || document.querySelector(".mode-btn.active")?.dataset.mode || "generate";
  const conv = {
    id: newId(),
    title: m === "generate" ? "新的代码生成" : "新的代码审核",
    mode: m,
    messages: [],
    loaded: true,
    persisted: false,       // 后端是否已落库
    customTitle: false,     // 用户是否手动改过标题
  };
  conversations.unshift(conv);
  currentId = conv.id;
  setModeUI(m);
  render();
  mainInput.focus();
  // 立即落库：保证刷新或重开浏览器后这个空对话也不会消失
  try {
    const created = await apiCall("/api/conversations", "POST", {
      conversation_id: conv.id,
      mode: conv.mode,
      title: conv.title,
    });
    conv.title = created.title;
    conv.persisted = true;
  } catch (err) {
    // 后端暂时不可用时保留本地，首次发送时 save_result 仍会尝试建会话
    console.warn("会话创建失败，暂存本地：", err.message);
  }
}

async function selectConversation(id) {
  currentId = id;
  const conv = current();
  if (!conv) return;
  // 列表里只有元信息，第一次打开时拉取完整消息
  if (!conv.loaded) {
    try {
      const full = await fetchJson("/api/conversations/" + encodeURIComponent(id));
      conv.mode = full.mode;
      conv.title = full.title;
      conv.messages = full.messages || [];
      conv.loaded = true;
    } catch (err) {
      conv.messages = [{ role: "assistant", kind: "error", text: "历史加载失败：" + err.message }];
      conv.loaded = true;
    }
  }
  setModeUI(conv.mode);
  render();
}

async function setMode(mode) {
  const conv = current();
  if (conv && conv.mode !== mode) {
    await newConversation(mode);        // 切换模式即开新会话
  } else {
    setModeUI(mode);
  }
}

function setModeUI(mode) {
  document.querySelectorAll(".mode-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
  });
  if (mode === "review") {
    chatTitle.textContent = "只审核代码";
    headerSub.textContent = "粘贴已有代码，智能体只做审核与测试，不生成代码";
    mainInput.placeholder = "粘贴需要审核的 Python 代码…";
    testExtra.style.display = "";
  } else {
    chatTitle.textContent = "代码生成";
    headerSub.textContent = "描述你的需求，系统会先确认需求；信息不足时先向你提问，再生成、审核、测试";
    mainInput.placeholder = "输入你的需求，Enter 发送，Shift+Enter 换行";
    testExtra.style.display = "none";
  }
}

/* ============================== 发送与流式请求 ============================== */
async function send() {
  if (busy) return;
  const conv = current();
  if (!conv) { await newConversation(); return; }
  const text = mainInput.value.trim();
  if (!text) return;

  // 上一条是待回答的需求澄清卡：先答完问题，避免中途插入新需求
  const last = conv.messages[conv.messages.length - 1];
  if (last && last.kind === "question") {
    const input = document.querySelector(".qa-input");
    if (input) input.focus();
    return;
  }

  const mode = conv.mode;
  const testCode = testInput.value.trim();
  // 发送前还没有任何消息，说明是这个会话的第一轮
  const isFirstTurn = conv.messages.length === 0;

  // 1) 追加用户消息（审核模式沿用简短文案，避免把整段代码写进气泡）
  conv.messages.push({
    role: "user",
    kind: "text",
    text: mode === "review" ? (testCode ? "审核一段代码（含测试）" : "审核一段代码") : text,
  });
  // 2) 追加加载占位，label 会随阶段事件实时更新
  const loadingMsg = {
    role: "assistant",
    kind: "loading",
    label: mode === "review" ? "正在审核代码…" : "正在确认需求…",
  };
  conv.messages.push(loadingMsg);
  mainInput.value = "";
  testInput.value = "";
  autoResize();
  render();

  busy = true;
  sendBtn.disabled = true;
  try {
    // 第一轮且用户未手动命名时自动给标题；之后保持不变，不覆盖自定义标题
    if (isFirstTurn && !conv.customTitle) {
      conv.title = mode === "review" ? "代码审核" : text.slice(0, 18);
    }
    const url = mode === "generate" ? "/api/chat/generate" : "/api/chat/review";
    const body = mode === "generate"
      ? { requirement: text, conversation_id: conv.id, title: conv.title }
      : { code: text, test_code: testCode, conversation_id: conv.id, title: conv.title };

    await streamSSE(url, body, (event) => applyStreamEvent(event, conv, loadingMsg));
  } catch (err) {
    const idx = conv.messages.indexOf(loadingMsg);
    if (idx !== -1) conv.messages[idx] = { role: "assistant", kind: "error", text: err.message };
    render();
  } finally {
    busy = false;
    sendBtn.disabled = false;
  }
}

/* 读取普通 JSON（用于历史接口） */
async function fetchJson(url) {
  const r = await fetch(url);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || "请求失败（" + r.status + "）");
  return body;
}

/* 通用带方法的 JSON 请求（用于重命名 PATCH、删除 DELETE） */
async function apiCall(url, method, payload) {
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || "请求失败（" + r.status + "）");
  return body;
}

const conversationUrl = (id) => "/api/conversations/" + encodeURIComponent(id);
const apiRename = (id, title) => apiCall(conversationUrl(id), "PATCH", { title });
const apiDelete = (id) => apiCall(conversationUrl(id), "DELETE");

/* 统一处理流式事件：阶段更新 / 中断提问 / 最终结果 / 错误 */
function applyStreamEvent(event, conv, loadingMsg) {
  const idx = conv.messages.indexOf(loadingMsg);
  if (event.type === "stage") {
    loadingMsg.label = event.label;
    render();
  } else if (event.type === "interrupt") {
    // 需求存在必须澄清的歧义：把加载占位替换成问题卡，等待用户回答
    if (idx !== -1) {
      conv.messages[idx] = {
        role: "assistant",
        kind: "question",
        answered: false,
        data: {
          questions: event.questions || [],
          thread_id: event.thread_id || "",
        },
      };
    }
    conv.persisted = true;
    render();
  } else if (event.type === "result") {
    // 只有在审核与测试全部跑完后，后端才会推送 result，前端此时才渲染码与报告
    if (idx !== -1) conv.messages[idx] = { role: "assistant", kind: "result", data: event.data };
    conv.persisted = true;
    if (!conv.messages.some((m) => m.kind === "loading")) render();
  } else if (event.type === "error") {
    if (idx !== -1) conv.messages[idx] = { role: "assistant", kind: "error", text: event.message };
    render();
  }
}

/* 提交需求澄清回答，并从断点恢复执行 */
async function submitAnswers(msg) {
  if (busy) return;
  const conv = current();
  if (!conv || !msg) return;
  const card = messagesEl.querySelector(".qa-card");
  const inputs = card ? Array.from(card.querySelectorAll(".qa-input")) : [];
  const answers = [];
  inputs.forEach((el) => {
    const text = el.value.trim();
    if (text) {
      const question = (msg.data.questions || [])[Number(el.dataset.q)] || "";
      answers.push({ question, answer: text });
    }
  });
  if (answers.length === 0) {
    const tip = card && card.querySelector(".qa-tip");
    if (tip) tip.textContent = "至少填写一条回答后再提交。";
    return;
  }

  // 问题卡转为已回答状态，并在对话中留下补充说明
  msg.answered = true;
  msg.answers = answers;
  const answerText = answers
    .map((a) => (a.question ? `${a.question}：${a.answer}` : a.answer))
    .join("；");
  conv.messages.push({ role: "user", kind: "text", text: "补充说明：" + answerText });
  const loadingMsg = { role: "assistant", kind: "loading", label: "正在生成代码…" };
  conv.messages.push(loadingMsg);
  render();

  busy = true;
  sendBtn.disabled = true;
  try {
    const firstUser = conv.messages.find((m) => m.role === "user" && m.kind === "text");
    await streamSSE(
      "/api/chat/resume",
      {
        conversation_id: conv.id,
        thread_id: (msg.data && msg.data.thread_id) || "",
        requirement: firstUser ? firstUser.text : "",
        title: conv.title,
        answers,
      },
      (event) => applyStreamEvent(event, conv, loadingMsg)
    );
  } catch (err) {
    const idx = conv.messages.indexOf(loadingMsg);
    if (idx !== -1) conv.messages[idx] = { role: "assistant", kind: "error", text: err.message };
    render();
  } finally {
    busy = false;
    sendBtn.disabled = false;
  }
}

/* 绑定问题卡上的提交按钮（每次渲染后节点都会重建，重新绑定即可） */
function bindQuestionCards() {
  messagesEl.querySelectorAll(".qa-submit").forEach((btn) => {
    btn.onclick = () => {
      const index = Number(btn.dataset.index);
      const conv = current();
      const msg = conv && conv.messages[index];
      if (msg) submitAnswers(msg);
    };
  });
}

/* 读取 SSE 流：按空行拆分事件，逐条回调 onEvent */
async function streamSSE(url, body, onEvent) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const errBody = await r.json().catch(() => ({}));
    throw new Error(errBody.detail || "请求失败（" + r.status + "）");
  }

  const reader = r.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE 以两个换行分隔事件，最后一段可能不完整，留到下一轮
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop();
    for (const chunk of chunks) {
      const dataLine = chunk.split("\n").find((l) => l.startsWith("data:"));
      if (!dataLine) continue;
      const jsonText = dataLine.slice(5).trim();
      if (jsonText) onEvent(JSON.parse(jsonText));
    }
  }
}

/* ============================== 渲染 ============================== */
function render() {
  renderHistory();
  renderMessages();
}

function renderHistory() {
  historyList.innerHTML = "";
  conversations.forEach((c) => {
    const item = c.id === editingId ? buildRenameEditor(c) : buildHistoryItem(c);
    historyList.appendChild(item);
  });
}

/* 普通历史项：标题 + 悬停显示的“重命名/删除”按钮 */
function buildHistoryItem(c) {
  const wrap = document.createElement("div");
  wrap.className = "history-item" + (c.id === currentId ? " active" : "");

  const name = document.createElement("span");
  name.className = "history-name";
  name.textContent = c.title;
  wrap.appendChild(name);

  const actions = document.createElement("span");
  actions.className = "history-actions";

  const renameBtn = document.createElement("button");
  renameBtn.type = "button";
  renameBtn.className = "icon-btn";
  renameBtn.title = "重命名";
  renameBtn.textContent = "✎";
  renameBtn.onclick = (e) => { e.stopPropagation(); startRename(c.id); };

  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.className = "icon-btn delete";
  deleteBtn.title = "删除";
  deleteBtn.textContent = "✕";
  deleteBtn.onclick = (e) => { e.stopPropagation(); confirmDelete(c.id); };

  actions.appendChild(renameBtn);
  actions.appendChild(deleteBtn);
  wrap.appendChild(actions);

  // 点击整行才切换会话；按钮的点击已 stopPropagation，不会触发这里
  wrap.onclick = () => selectConversation(c.id);
  return wrap;
}

/* 重命名编辑器：输入框，Enter 保存、Esc 取消、失焦自动保存 */
function buildRenameEditor(c) {
  const wrap = document.createElement("div");
  wrap.className = "history-item editing";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "rename-input";
  input.value = c.title;
  input.maxLength = 255;
  wrap.appendChild(input);

  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    editingId = null;
    if (save) await commitRename(c.id, input.value);
    renderHistory();
  };
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));

  // 插入后再聚焦并选中，便于直接替换
  setTimeout(() => { input.focus(); input.select(); }, 0);
  return wrap;
}

function startRename(id) {
  if (busy) return;
  editingId = id;
  renderHistory();
}

async function commitRename(id, rawTitle) {
  const title = rawTitle.trim();
  const conv = conversations.find((c) => c.id === id);
  if (!conv || !title || title === conv.title) return;
  conv.title = title;
  conv.customTitle = true;
  // 未持久化的空会话只改本地，首次发送时会带上新标题
  if (conv.persisted) {
    try {
      await apiRename(id, title);
    } catch (err) {
      window.alert("重命名失败：" + err.message);
    }
  }
}

async function confirmDelete(id) {
  const conv = conversations.find((c) => c.id === id);
  if (!conv) return;
  if (!window.confirm(`确定删除对话「${conv.title}」吗？此操作不可恢复。`)) return;
  await deleteConversation(id);
}

async function deleteConversation(id) {
  const index = conversations.findIndex((c) => c.id === id);
  if (index === -1) return;
  const conv = conversations[index];
  const wasCurrent = id === currentId;
  if (conv.persisted) {
    try {
      await apiDelete(id);
    } catch (err) {
      window.alert("删除失败：" + err.message);
      return;
    }
  }
  conversations.splice(index, 1);
  if (!wasCurrent) {
    renderHistory();
  } else if (conversations.length > 0) {
    await selectConversation(conversations[0].id);
  } else {
    newConversation("generate");
  }
}

function renderMessages() {
  const conv = current();
  if (!conv || conv.messages.length === 0) {
    messagesEl.innerHTML = "";
    messagesEl.appendChild(buildWelcome());
    return;
  }
  messagesEl.innerHTML = "";
  conv.messages.forEach((m, index) => {
    if (m.role === "user") {
      messagesEl.appendChild(row(`<div class="user-bubble">${esc(m.text)}</div>`));
    } else if (m.kind === "loading") {
      messagesEl.appendChild(row(
        `<div class="a-card"><div class="loading-card"><span class="spinner"></span>${esc(m.label)}</div></div>`
      ));
    } else if (m.kind === "error") {
      messagesEl.appendChild(row(
        `<div class="a-card" style="color:var(--blocker)">运行出错：${esc(m.text)}</div>`
      ));
    } else if (m.kind === "question") {
      messagesEl.appendChild(row(questionCard(m, index, conv.messages.length)));
    } else {
      messagesEl.appendChild(row(renderResult(m.data)));
    }
  });
  bindQuestionCards();
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function row(inner) {
  const wrap = document.createElement("div");
  wrap.className = "msg-row";
  const body = document.createElement("div");
  body.className = "msg-body";
  // 不使用任何头像/logo，直接渲染消息内容
  body.innerHTML = inner;
  wrap.appendChild(body);
  return wrap;
}

function buildWelcome() {
  const mode = (current() && current().mode) || "generate";
  const div = document.createElement("div");
  div.className = "welcome";
  const genCards = [
    "写一个函数，对字符串列表去重并按字典序排序",
    "实现一个用户名校验函数，判断长度与合法字符",
    "写一个求数字列表平均值的函数，处理空列表",
  ];
  const reviewCards = [
    "def add(a, b):\n    return a + b",
    "def first(items):\n    return items[0]",
    "def run(cmd):\n    return eval(cmd)",
  ];
  const cards = mode === "review" ? reviewCards : genCards;
  div.innerHTML = `
    <h2>多智能体 Python 代码生成与辅助审查</h2>
    <p>${mode === "review"
      ? "粘贴已有代码，系统只做静态审核与测试，不生成代码"
      : "输入需求，系统将先确认需求，再生成代码并完成审核与测试"}</p>
    <div class="suggestions">
      ${cards.map((c) => `<button class="sug-card">${esc(c)}</button>`).join("")}
    </div>`;
  div.querySelectorAll(".sug-card").forEach((b) => {
    b.onclick = () => { mainInput.value = b.textContent; send(); };
  });
  return div;
}

/* 需求澄清卡：未回答时可填写；已回答或已翻篇时只读展示 */
function questionCard(m, index, total) {
  const data = m.data || {};
  const questions = data.questions || [];
  const answered = m.answered || index !== total - 1;
  if (answered) {
    const answers = (m.answers || []).map((a) => `
      <div class="qa-pair">
        ${a.question ? `<div class="qa-q">${esc(a.question)}</div>` : ""}
        <div class="qa-a">${esc(a.answer)}</div>
      </div>`).join("");
    return `
    <div class="a-card">
      <div class="a-card-title">需求澄清 <span class="tag">已回答</span></div>
      ${answers || `<div class="qa-note">已提交补充说明，继续执行后续流程。</div>`}
    </div>`;
  }
  const fields = questions.map((q, i) => `
    <div class="qa-field">
      <div class="qa-q">${esc(q)}</div>
      <textarea class="qa-input" data-q="${i}" rows="2" placeholder="请填写你的回答…"></textarea>
    </div>`).join("");
  return `
  <div class="a-card qa-card" data-index="${index}">
    <div class="a-card-title">需求需要确认 <span class="tag">${questions.length} 个问题</span></div>
    <div class="qa-tip">回答后将继续生成代码，并完成审核与测试；未回答前不会输出代码。</div>
    ${fields}
    <div class="qa-actions"><button class="qa-submit" data-index="${index}">提交回答并继续</button></div>
  </div>`;
}

/* ---------------- 结果卡片 ---------------- */
function renderResult(d) {
  let html = "";
  if (d.requirement) html += requirementCard(d.requirement);
  (d.versions || []).forEach((v) => { html += versionBlock(v, d); });
  return html;
}

function requirementCard(r) {
  const grid = (k, v) => `<div class="k">${k}</div><div class="v">${v}</div>`;
  const list = (arr) => arr && arr.length
    ? `<ul class="req-list">${arr.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "无";
  return `
  <div class="a-card">
    <div class="a-card-title">需求确认 <span class="tag">${esc(r.function_name)}</span></div>
    <div class="req-grid">
      ${grid("功能标题", esc(r.title))}
      ${grid("需求重述", esc(r.restated_requirement))}
      ${grid("输入", esc((r.inputs || []).join("；")))}
      ${grid("输出", esc(r.outputs))}
      ${grid("边界场景", list(r.edge_cases))}
      ${grid("实现约束", list(r.constraints))}
      ${grid("验收标准", list(r.acceptance_criteria))}
      ${grid("假设", list(r.assumptions))}
    </div>
    ${r.questions && r.questions.length ? `<div style="margin-top:10px;color:var(--major)">待澄清：${esc(r.questions.join("；"))}</div>` : ""}
  </div>`;
}

function versionBlock(v, d) {
  const label = d.mode === "review"
    ? "待审核代码"
    : (v.round === 0 ? "初始生成" : `第 ${v.round} 轮修复`);
  let html = `<div class="version-head"><span class="num">${v.version_no}</span>${label}</div>`;
  html += codeCard(v.solution_code);
  if (v.test_code) {
    html += `<details class="a-card" style="padding:10px 14px"><summary style="cursor:pointer;font-size:12.5px;color:var(--text-2)">查看测试代码 test_solution.py</summary>${codeCardInner(v.test_code)}</details>`;
  }
  if (v.review) html += reviewCard(v.review);
  if (v.pytest) html += pytestCard(v.pytest);
  return html;
}

function codeCard(code) {
  return `<div class="a-card"><div class="a-card-title">实现代码</div>${codeCardInner(code)}</div>`;
}
function codeCardInner(code) {
  const id = "co_" + Math.random().toString(36).slice(2, 9);
  return `
  <div class="code-wrap">
    <div class="code-head"><span>Python</span><button class="copy-btn" data-target="${id}">复制</button></div>
    <pre class="code" id="${id}">${esc(code)}</pre>
  </div>`;
}

function scorecardBlock(sc) {
  if (!sc) return "";
  const dims = sc.dimensions.map((d) => `
    <div class="dim-row">
      <span class="dim-name">${esc(d.name)}</span>
      <div class="dim-bar"><div class="dim-fill" style="width:${d.score * 10}%"></div></div>
      <span class="dim-score">${d.score}</span>
    </div>`).join("");
  const actions = sc.top_actions && sc.top_actions.length
    ? `<div class="top-actions">
         <div class="ta-title">优先改进</div>
         ${sc.top_actions.map((a) => `<div class="ta-item">${esc(a)}</div>`).join("")}
       </div>` : "";
  return `
  <div class="scorecard">
    <div class="sc-head">
      <div class="sc-score">
        <span class="sc-num">${sc.overall_score}</span><span class="sc-max">/100</span>
        <span class="sc-grade grade-${esc(sc.grade)}">${esc(sc.grade)}</span>
      </div>
      <span class="pill ${sc.gate_passed ? "pass" : "fail"}">
        ${sc.gate_passed ? "门禁通过" : "门禁未过"}</span>
    </div>
    <div class="dim-list">${dims}</div>
    ${actions}
  </div>`;
}

function knowledgeBlock(hits) {
  if (!hits || !hits.length) return "";
  const rows = hits.map((h) => `
    <div class="kb-item">
      <div class="kb-head">
        <span class="kb-src">${esc(h.doc)} · ${esc(h.section)}</span>
        <span class="kb-score">相关度 ${Math.round((h.score || 0) * 100)}%</span>
      </div>
      <div class="kb-text">${esc(h.content)}</div>
    </div>`).join("");
  return `
  <details class="kb-hits">
    <summary>知识库命中 ${hits.length} 条</summary>
    <div class="kb-list">${rows}</div>
  </details>`;
}

function reviewCard(rep) {
  const counts = rep.counts || {};
  const badges = ["阻断", "主要", "次要"]
    .filter((k) => counts[k] > 0)
    .map((k) => `<span class="count-badge cb-${k}">${k} ${counts[k]}</span>`).join("");
  const issues = rep.items && rep.items.length
    ? `<div class="issue-list">${rep.items.map((it) => `
        <div class="issue sev-${esc(it.severity)}">
          <div class="issue-top">
            <span class="issue-view">${esc(it.view)}</span>
            <span class="count-badge cb-${esc(it.severity)}">${esc(it.severity)}</span>
            ${esc(it.issue_type)} ${it.line_no ? `<span class="line">第 ${it.line_no} 行</span>` : ""}
            ${it.source === "llm" ? `<span class="conf">置信 ${Math.round(it.confidence * 100)}%</span>` : ""}
          </div>
          <div class="issue-desc">${esc(it.description)}</div>
          ${it.suggestion ? `<div class="issue-sug">建议：${esc(it.suggestion)}</div>` : ""}
        </div>`).join("")}</div>`
    : `<div class="no-issue">未发现阻断/主要问题</div>`;
  return `
  <div class="a-card">
    <div class="a-card-title">代码审核</div>
    <div class="review-status">
      <span class="pill ${rep.passed ? "pass" : "fail"}">${rep.passed ? "审核通过" : "需修复"}</span>
      <div class="counts">${badges}</div>
    </div>
    ${scorecardBlock(rep.scorecard)}
    ${knowledgeBlock(rep.knowledge_hits)}
    ${issues}
    <div style="margin-top:10px;font-size:12.5px;color:var(--text-2)">${esc(rep.summary)}</div>
  </div>`;
}

function pytestCard(p) {
  const green = p.all_green;
  const skipped = p.total === 0;
  const bigCls = green ? "green" : (skipped ? "" : "red");
  const bigTxt = green ? "通过" : (skipped ? "跳过" : "未通过");
  return `
  <div class="a-card">
    <div class="a-card-title">pytest 测试</div>
    <div class="pytest-card">
      <span class="pytest-big ${bigCls}">${bigTxt}</span>
      <div class="pytest-meta">
        <span>用例 <b>${p.total}</b></span>
        <span>通过 <b>${p.passed}</b></span>
        <span>失败 <b>${p.failed}</b></span>
        <span>耗时 <b>${p.duration}s</b></span>
      </div>
    </div>
    ${p.traceback ? `<details style="margin-top:10px"><summary class="tb-toggle">查看详情</summary>
      <div class="tb-text">${esc(p.traceback)}</div></details>` : ""}
  </div>`;
}

/* ============================== 事件绑定 ============================== */
sendBtn.onclick = send;
document.getElementById("newChatBtn").onclick = () => newConversation();
document.querySelectorAll(".mode-btn").forEach((b) => {
  b.onclick = () => setMode(b.dataset.mode);
});

// Enter 发送，Shift+Enter 换行
mainInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
function autoResize() {
  mainInput.style.height = "auto";
  mainInput.style.height = Math.min(mainInput.scrollHeight, 220) + "px";
}
mainInput.addEventListener("input", autoResize);

// 复制按钮（事件委托）
messagesEl.addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  const code = document.getElementById(btn.dataset.target)?.textContent || "";
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = "已复制";
    setTimeout(() => (btn.textContent = "复制"), 1500);
  });
});

/* ============================== 健康检查 ============================== */
async function checkHealth() {
  try {
    const h = await fetchJson("/api/health");
    const prefix = h.mode === "api" ? "真实 API · " : "离线 · ";
    const memory = h.memory === "mysql" ? " · 记忆已持久化" : " · 记忆不可用";
    const rag = h.rag && h.rag.available
      ? ` · 知识库 ${h.rag.chunks} 条`
      : " · 知识库不可用";
    document.getElementById("healthText").textContent = prefix + h.model + memory + rag;
  } catch {
    document.getElementById("healthText").textContent = "后端未连接";
  }
}

/* ============================== 启动 ============================== */
async function bootstrap() {
  setModeUI("generate");
  try {
    const body = await fetchJson("/api/conversations");
    conversations = (body.conversations || []).map((c) => ({
      ...c, loaded: false, messages: [], persisted: true, customTitle: false,
    }));
  } catch {
    conversations = [];
  }

  if (conversations.length === 0) {
    await newConversation("generate");
  } else {
    currentId = conversations[0].id;
    await selectConversation(currentId);
  }
  checkHealth();
}

bootstrap();
