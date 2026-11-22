/* ==========================================================================
   豆包式聊天前端逻辑
   状态：新建对话即落库（刷新不丢失），会话与每轮结果由后端持久化（MySQL）
   接口：POST /api/conversations 新建即持久化；GET /api/conversations(+id) 读取历史；
         PATCH /api/conversations/{id} 自定义命名；DELETE /api/conversations/{id} 删除并重排编号；
         POST /api/uploads 上传附件并按 MD5 去重，可仅用于对话或导入 RAG；
         POST /api/chat/generate（代码生成）/ POST /api/chat/review（仅审核），均为 SSE 流式
   说明：历史项支持悬停重命名与删除，消息编号在删除后重排为连续序号，不引入图片/logo
   展示：对话采用左右布局（用户消息在右、助手输出在左），助手输出仿豆包富文本风格
        （标题、要点列表、代码块、表格、引用），由内置 Markdown 渲染器安全渲染
   ========================================================================== */
"use strict";

/* ---------------- 全局状态 ---------------- */
let conversations = [];                 // 会话列表（部分可能尚未加载消息）
let currentId = null;                   // 当前会话 ID
let busy = false;                       // 是否正在请求
let editingId = null;                   // 正在重命名的会话 ID（同时只有一个）
let uploading = false;                  // 是否正在上传/解析附件
let pendingAttachments = [];            // 待发送消息携带的附件（知识库上传不加入）
let uploadPurpose = "chat";             // chat：当前对话；knowledge：导入 RAG
let testMode = false;                   // 后端是否处于测试模式（会话编号加 test_ 前缀）

/* ---------------- DOM 引用 ---------------- */
const messagesEl = document.getElementById("messages");
const mainInput = document.getElementById("mainInput");
const testInput = document.getElementById("testInput");
const testExtra = document.getElementById("testExtra");
const sendBtn = document.getElementById("sendBtn");
const historyList = document.getElementById("historyList");
const chatTitle = document.getElementById("chatTitle");
const headerSub = document.getElementById("headerSub");
const fileInput = document.getElementById("fileInput");
const attachBtn = document.getElementById("attachBtn");
const knowledgeBtn = document.getElementById("knowledgeBtn");
const attachmentList = document.getElementById("attachmentList");
const uploadStatus = document.getElementById("uploadStatus");

/* ============================== 工具函数 ============================== */
function esc(s) {
  // 转义 HTML，防止用户输入/模型内容破坏页面结构
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/* ============================== 附件上传 ============================== */
function updateSendState() {
  // 上传期间不允许发送，避免请求漏带尚未解析完成的附件。
  sendBtn.disabled = busy || uploading;
  attachBtn.disabled = busy || uploading;
  knowledgeBtn.disabled = busy || uploading;
}

function renderAttachments() {
  attachmentList.replaceChildren();
  attachmentList.hidden = pendingAttachments.length === 0;
  pendingAttachments.forEach((attachment) => {
    const chip = document.createElement("span");
    chip.className = "attachment-chip";
    const label = document.createElement("span");
    label.textContent = attachment.filename + (attachment.in_knowledge ? "（已入库）" : "");
    label.title = attachment.filename;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attachment-remove";
    remove.textContent = "×";
    remove.title = "移除此附件（不会删除知识库）";
    remove.disabled = busy || uploading;
    remove.onclick = () => {
      pendingAttachments = pendingAttachments.filter((item) => item.id !== attachment.id);
      renderAttachments();
    };
    chip.append(label, remove);
    attachmentList.appendChild(chip);
  });
  updateSendState();
}

function clearPendingAttachments() {
  pendingAttachments = [];
  uploadStatus.textContent = "";
  uploadStatus.classList.remove("error");
  renderAttachments();
}

async function uploadFiles(files, purpose) {
  if (!files.length || uploading || busy) return;
  uploading = true;
  uploadStatus.classList.remove("error");
  uploadStatus.textContent = purpose === "knowledge" ? "正在解析并导入知识库…" : "正在解析附件…";
  updateSendState();

  const form = new FormData();
  files.forEach((file) => form.append("files", file, file.name));
  form.append("import_to_knowledge", purpose === "knowledge" ? "true" : "false");
  try {
    const response = await fetch("/api/uploads", { method: "POST", body: form });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `上传失败（${response.status}）`);

    const results = body.results || [];
    const errors = results.filter((item) => item.status === "error");
    let added = 0;
    if (purpose === "chat") {
      results.forEach((item) => {
        if (item.status === "error" || !item.attachment) return;
        const attachment = item.attachment;
        if (!pendingAttachments.some((existing) => existing.id === attachment.id)) {
          pendingAttachments.push({
            id: attachment.id,
            filename: attachment.filename,
            in_knowledge: attachment.in_knowledge,
          });
          added += 1;
        }
      });
      renderAttachments();
      uploadStatus.textContent = `已准备 ${added} 个附件，可随下一条消息发送`;
    } else {
      const imported = results.filter((item) => item.knowledge_changed).length;
      const duplicated = results.filter((item) => item.status === "duplicate").length;
      uploadStatus.textContent = `知识库处理完成：新增 ${imported} 个，MD5 重复 ${duplicated} 个`;
      if (imported > 0) await checkHealth();
    }
    if (body.index_refreshed === false) uploadStatus.textContent += "；索引刷新失败，可重试";
    if (errors.length) {
      uploadStatus.textContent += "；" + errors.map((item) => `${item.filename}：${item.message}`).join("；");
      uploadStatus.classList.add("error");
    }
  } catch (error) {
    uploadStatus.textContent = error.message;
    uploadStatus.classList.add("error");
  } finally {
    uploading = false;
    fileInput.value = ""; // 允许再次选择同一个文件，由后端 MD5 去重
    updateSendState();
  }
}

function current() {
  return conversations.find((c) => c.id === currentId) || null;
}

function newId() {
  return (testMode ? "test_" : "c_") + Date.now() + "_" + Math.random().toString(36).slice(2, 6);
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
  clearPendingAttachments();
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
  clearPendingAttachments();
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
    headerSub.textContent = "粘贴代码或添加代码附件，智能体只做审核与测试，不生成代码";
    mainInput.placeholder = "粘贴需要审核的 Python 代码，或直接添加代码附件…";
    testExtra.style.display = "";
  } else {
    chatTitle.textContent = "代码生成";
    headerSub.textContent = "描述需求或添加需求附件；信息不足时先提问，再生成、审核、测试";
    mainInput.placeholder = "输入需求，或添加 Word、Excel、PDF、图片等需求附件…";
    testExtra.style.display = "none";
  }
}

/* ============================== 发送与流式请求 ============================== */
async function send() {
  if (busy || uploading) return;
  const conv = current();
  if (!conv) { await newConversation(); return; }
  const text = mainInput.value.trim();
  const attachments = [...pendingAttachments];
  if (!text && attachments.length === 0) return;

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
  const attachmentNote = attachments.length
    ? `\n附件：${attachments.map((item) => item.filename).join("、")}`
    : "";

  // 1) 追加用户消息；附件只显示文件名，正文由后端按 ID 读取。
  const userText = mode === "review"
    ? (testCode ? "审核一段代码（含测试）" : "审核一段代码")
    : (text || "根据附件内容实现需求");
  conv.messages.push({ role: "user", kind: "text", text: userText + attachmentNote });
  // 2) 追加加载占位，label 会随阶段事件实时更新
  const loadingMsg = {
    role: "assistant",
    kind: "loading",
    label: mode === "review" ? "正在审核代码…" : "正在确认需求…",
  };
  conv.messages.push(loadingMsg);
  mainInput.value = "";
  testInput.value = "";
  pendingAttachments = [];
  renderAttachments();
  autoResize();
  render();

  busy = true;
  updateSendState();
  try {
    // 第一轮且用户未手动命名时自动给标题；之后保持不变，不覆盖自定义标题
    if (isFirstTurn && !conv.customTitle) {
      conv.title = mode === "review"
        ? "代码审核"
        : (text.slice(0, 18) || attachments[0].filename.slice(0, 18));
    }
    const url = mode === "generate" ? "/api/chat/generate" : "/api/chat/review";
    const attachmentIds = attachments.map((item) => item.id);
    const body = mode === "generate"
      ? { requirement: text, conversation_id: conv.id, title: conv.title, attachment_ids: attachmentIds }
      : { code: text, test_code: testCode, conversation_id: conv.id, title: conv.title, attachment_ids: attachmentIds };

    await streamSSE(url, body, (event) => applyStreamEvent(event, conv, loadingMsg));
  } catch (err) {
    const idx = conv.messages.indexOf(loadingMsg);
    if (idx !== -1) conv.messages[idx] = { role: "assistant", kind: "error", text: err.message };
    render();
  } finally {
    busy = false;
    updateSendState();
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
    if (event.partial) {
      // 增量结果到达：转为流式消息，内容边增长边逐行揭示
      loadingMsg.kind = "streaming";
      loadingMsg.data = event.partial;
      if (typeof loadingMsg.revealed !== "number") loadingMsg.revealed = 0;
      startReveal();
    }
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
    // 最终结果到达：流式揭示未收尾时先补完剩余内容，再切换为完整答案
    if (idx !== -1) {
      const msg = conv.messages[idx];
      if (msg.kind === "streaming") {
        msg.finalData = event.data;
        msg.label = "";
        startReveal();
      } else {
        conv.messages[idx] = { role: "assistant", kind: "result", data: event.data };
      }
    }
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
  updateSendState();
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
    updateSendState();
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
      // 用户消息：右侧蓝色气泡
      messagesEl.appendChild(row(`<div class="user-bubble">${esc(m.text)}</div>`, "user"));
    } else if (m.kind === "loading") {
      // 阶段进度：仿豆包的“正在思考/处理”文案，label 随 SSE 阶段事件实时更新
      messagesEl.appendChild(row(
        `<div class="md-bubble"><div class="loading-card"><span class="spinner"></span>${esc(m.label)}</div></div>`,
        "assistant"
      ));
    } else if (m.kind === "error") {
      // 运行错误：仿豆包的提示文案，直接说明原因
      messagesEl.appendChild(row(
        `<div class="md-bubble"><div class="md md-error"><p>⚠️ <strong>运行出错</strong>：${esc(m.text)}</p></div></div>`,
        "assistant"
      ));
    } else if (m.kind === "question") {
      messagesEl.appendChild(row(questionCard(m, index, conv.messages.length), "assistant"));
    } else if (m.kind === "streaming") {
      // 流式输出：已接收内容逐行揭示，末尾光标 + 当前阶段文案
      const shown = streamMarkdown(m).split("\n").slice(0, m.revealed || 0).join("\n");
      messagesEl.appendChild(row(
        `<div class="md-bubble"><div class="md md-stream">${mdToHtml(shown)}</div>` +
        (m.label ? `<div class="loading-card"><span class="spinner"></span>${esc(m.label)}</div>` : "") +
        `</div>`,
        "assistant"
      ));
    } else {
      // 最终结果：仿豆包富文本答案（需求/代码/审核/测试分节展示）
      messagesEl.appendChild(row(renderResult(m.data), "assistant"));
    }
  });
  bindQuestionCards();
  bindExportButtons();
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

/* ============================== 流式揭示（仿豆包打字机） ============================== */
let revealTimer = null;

/* 流式消息的目标文档：收尾前用增量 payload，收尾后用最终结果 */
function streamMarkdown(m) {
  return resultToMarkdown(m.finalData || m.data);
}

function startReveal() {
  if (revealTimer === null) revealTimer = setInterval(tickReveal, 24);
}

function tickReveal() {
  let changed = false;
  let active = false;
  conversations.forEach((conv) => {
    conv.messages.forEach((m) => {
      if (m.kind !== "streaming") return;
      const lines = streamMarkdown(m).split("\n");
      if (m.revealed < lines.length) {
        // 每 tick 揭示 2 行：代码块逐行生长，与豆包流式输出观感一致
        m.revealed = Math.min(lines.length, m.revealed + 2);
        changed = true;
        active = true;
      } else if (m.finalData) {
        // 揭示追平且最终结果已到：切换为完整答案（含导出按钮）
        m.kind = "result";
        m.data = m.finalData;
        delete m.finalData;
        delete m.revealed;
        changed = true;
      }
    });
  });
  if (changed) render();
  if (!active) {
    clearInterval(revealTimer);
    revealTimer = null;
  }
}

function row(inner, role) {
  const wrap = document.createElement("div");
  // user 靠右、assistant 靠左，由 CSS 的 .msg-row.user / .msg-row.assistant 控制
  wrap.className = "msg-row " + (role === "user" ? "user" : "assistant");
  const body = document.createElement("div");
  body.className = "msg-body";
  // 不使用任何头像/logo，直接渲染消息内容
  body.innerHTML = inner;
  wrap.appendChild(body);
  return wrap;
}

/* ============================== Markdown 渲染（仿豆包输出） ==============================
   轻量 Markdown → HTML：先整体转义再解析，模型/用户内容无法注入 HTML。
   支持：标题、段落、粗体、行内代码、围栏代码块（带复制）、列表、表格、引用、分隔线 */

/* 生成围栏代码块；代码本身含 ``` 时自动加长围栏，保证内容完整 */
function fence(code, lang) {
  const text = String(code ?? "");
  let ticks = "```";
  while (text.includes(ticks)) ticks += "`";
  return ticks + (lang || "") + "\n" + text + "\n" + ticks;
}

/* 深色代码块：语言标签 + 复制按钮，沿用现有复制事件委托 */
function codeBlockHtml(lang, code) {
  const id = "co_" + Math.random().toString(36).slice(2, 9);
  return `
  <div class="code-wrap">
    <div class="code-head"><span>${esc(lang || "text")}</span><button class="copy-btn" data-target="${id}">复制</button></div>
    <pre class="code" id="${id}">${esc(code)}</pre>
  </div>`;
}

/* 行内格式：先保护行内代码，再处理粗体，最后还原代码 */
function inlineMd(s) {
  const codes = [];
  let text = s.replace(/`([^`]+)`/g, (_, c) => {
    codes.push(c);
    return "\u0000C" + (codes.length - 1) + "\u0000";
  });
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return text.replace(/\u0000C(\d+)\u0000/g, (_, n) => `<code class="inline">${codes[Number(n)]}</code>`);
}

function mdToHtml(md) {
  // 1) 按行抽出围栏代码块，避免代码内容被误当作 Markdown 解析
  const blocks = [];
  const kept = [];
  const rawLines = String(md ?? "").split("\n");
  for (let i = 0; i < rawLines.length; i++) {
    const open = rawLines[i].match(/^(`{3,})(.*)$/);
    if (open) {
      const ticks = open[1];
      const lang = open[2].trim();
      const body = [];
      i++;
      while (i < rawLines.length && rawLines[i].trim() !== ticks) { body.push(rawLines[i]); i++; }
      blocks.push({ lang, code: body.join("\n") });
      kept.push("\u0000B" + (blocks.length - 1) + "\u0000");
    } else {
      kept.push(rawLines[i]);
    }
  }
  // 2) 整体转义后再做块级解析，保证输出安全
  const lines = esc(kept.join("\n")).split("\n");
  const out = [];
  let para = [];
  const flush = () => {
    if (para.length) { out.push("<p>" + inlineMd(para.join("<br>")) + "</p>"); para = []; }
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i].trim();
    const bm = line.match(/^\u0000B(\d+)\u0000$/);
    if (bm) { flush(); const b = blocks[Number(bm[1])]; out.push(codeBlockHtml(b.lang, b.code)); i++; continue; }
    if (!line) { flush(); i++; continue; }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { flush(); const lvl = Math.min(h[1].length + 1, 5); out.push(`<h${lvl}>${inlineMd(h[2])}</h${lvl}>`); i++; continue; }
    if (/^-{3,}$/.test(line)) { flush(); out.push("<hr>"); i++; continue; }
    if (line.startsWith("&gt;")) {
      flush();
      const q = [];
      while (i < lines.length && lines[i].trim().startsWith("&gt;")) {
        q.push(lines[i].trim().replace(/^&gt;\s?/, "")); i++;
      }
      out.push("<blockquote>" + inlineMd(q.join("<br>")) + "</blockquote>");
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      flush();
      const items = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*]\s+/, "")); i++;
      }
      out.push("<ul>" + items.map((x) => `<li>${inlineMd(x)}</li>`).join("") + "</ul>");
      continue;
    }
    if (/^\d+\.\s+/.test(line)) {
      flush();
      const items = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^\d+\.\s+/, "")); i++;
      }
      out.push("<ol>" + items.map((x) => `<li>${inlineMd(x)}</li>`).join("") + "</ol>");
      continue;
    }
    if (line.startsWith("|") && i + 1 < lines.length && /^\|[\s:|-]+\|$/.test(lines[i + 1].trim())) {
      flush();
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(lines[i]);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) { rows.push(cells(lines[i])); i++; }
      out.push(
        `<table class="md-table"><thead><tr>${head.map((c) => `<th>${inlineMd(c)}</th>`).join("")}</tr></thead>` +
        `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inlineMd(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`
      );
      continue;
    }
    para.push(line);
    i++;
  }
  flush();
  return out.join("\n");
}

function buildWelcome() {
  const mode = (current() && current().mode) || "generate";
  const div = document.createElement("div");
  div.className = "welcome";
  const genCards = [
    "写一个函数，对字符串列表去重并按字典序排序",
    "用 HTML 做一个购物网页，包含商品列表、搜索和购物车",
    "用 Java 做一个控制台学生管理系统项目",
    "用 C 语言做一个命令行计算器项目",
  ];
  const reviewCards = [
    "def add(a, b):\n    return a + b",
    "def first(items):\n    return items[0]",
    "def run(cmd):\n    return eval(cmd)",
  ];
  const cards = mode === "review" ? reviewCards : genCards;
  div.innerHTML = `
    <h2>多智能体多语言代码生成与辅助审查</h2>
    <p>${mode === "review"
      ? "粘贴已有代码，系统只做静态审核与测试，不生成代码"
      : "输入需求，自动识别 Python / Java / C / HTML，生成单文件或多文件项目并完成审核与检查"}</p>
    <div class="suggestions">
      ${cards.map((c) => `<button class="sug-card">${esc(c)}</button>`).join("")}
    </div>`;
  div.querySelectorAll(".sug-card").forEach((b) => {
    b.onclick = () => { mainInput.value = b.textContent; send(); };
  });
  return div;
}

/* 需求澄清卡：仿豆包提问文案；未回答时保留输入框，保证澄清流程可用 */
function questionCard(m, index, total) {
  const data = m.data || {};
  const questions = data.questions || [];
  const answered = m.answered || index !== total - 1;
  if (answered) {
    const pairs = (m.answers || []).map((a) => `
      <div class="qa-pair">
        ${a.question ? `<div class="qa-q">${esc(a.question)}</div>` : ""}
        <div class="qa-a">${esc(a.answer)}</div>
      </div>`).join("");
    return `
    <div class="md-bubble">
      <div class="md"><p>💬 <strong>需求澄清</strong>（已回答）</p></div>
      ${pairs || `<div class="qa-note">已提交补充说明，继续执行后续流程。</div>`}
    </div>`;
  }
  const fields = questions.map((q, i) => `
    <div class="qa-field">
      <div class="qa-q">${esc(q)}</div>
      <textarea class="qa-input" data-q="${i}" rows="2" placeholder="请填写你的回答…"></textarea>
    </div>`).join("");
  return `
  <div class="md-bubble qa-card" data-index="${index}">
    <div class="md">
      <p>💬 <strong>需求需要确认</strong>（${questions.length} 个问题）</p>
      <p class="qa-tip">回答后将继续生成代码，并完成审核与测试；未回答前不会输出代码。</p>
    </div>
    ${fields}
    <div class="qa-actions"><button class="qa-submit" data-index="${index}">提交回答并继续</button></div>
  </div>`;
}

/* ---------------- 结果输出（仿豆包富文本答案） ---------------- */
const LANGUAGE_NAMES = {
  python: "Python",
  java: "Java",
  c: "C",
  html: "HTML / CSS / JS",
  css: "CSS",
  javascript: "JavaScript",
  markdown: "Markdown",
  json: "JSON",
};

function languageName(code) {
  return LANGUAGE_NAMES[code] || code || "Python";
}

/* 审核报告 → Markdown：结论 + 评分表 + 问题清单 + 知识库参考 + 总结引用 */
function reviewMarkdown(rep) {
  const md = [];
  const counts = rep.counts || {};
  const nums = ["阻断", "主要", "次要"]
    .filter((k) => counts[k] > 0)
    .map((k) => `${k} ${counts[k]}`)
    .join(" · ");
  md.push("## 代码审核");
  md.push((rep.passed ? "✅ **审核通过**" : "❌ **需修复**") + (nums ? `（${nums}）` : ""));
  const sc = rep.scorecard;
  if (sc) {
    md.push("", `**总分 ${sc.overall_score}/100 · 等级 ${sc.grade} · ${sc.gate_passed ? "门禁通过" : "门禁未过"}**`);
    md.push("", "| 维度 | 得分 |", "| --- | --- |");
    (sc.dimensions || []).forEach((dim) => md.push(`| ${dim.name} | ${dim.score}/10 |`));
    if (sc.top_actions && sc.top_actions.length) {
      md.push("", "**优先改进**");
      sc.top_actions.forEach((a, i) => md.push(`${i + 1}. ${a}`));
    }
  }
  if (rep.items && rep.items.length) {
    md.push("", "**问题清单**");
    rep.items.forEach((it, i) => {
      const loc = it.line_no ? `第 ${it.line_no} 行 · ` : "";
      const sug = it.suggestion ? ` 建议：${it.suggestion}` : "";
      md.push(`${i + 1}. **[${it.severity}·${it.view}]** ${loc}${it.issue_type}：${it.description}${sug}`);
    });
  } else {
    md.push("", "未发现阻断/主要问题。");
  }
  if (rep.knowledge_hits && rep.knowledge_hits.length) {
    md.push("", "**知识库参考**");
    rep.knowledge_hits.forEach((h) =>
      md.push(`- ${h.doc} · ${h.section}（相关度 ${Math.round((h.score || 0) * 100)}%）`));
  }
  if (rep.summary) md.push("", `> ${rep.summary}`);
  return md;
}

/* 测试/项目检查结果 → Markdown：结论 + 计数 + 失败详情代码块 */
function pytestMarkdown(p, language) {
  const isPython = !language || language === "python";
  const title = isPython ? "测试结果" : "项目检查";
  const unit = isPython ? "用例" : "检查项";
  const state = p.all_green
    ? "✅ **通过**"
    : (p.total === 0 ? "⏭️ **跳过**（未提供测试）" : "❌ **未通过**");
  const md = ["## " + title,
    `${state} · ${unit} ${p.total} · 通过 ${p.passed} · 失败 ${p.failed} · 耗时 ${p.duration}s`];
  if (p.traceback) md.push("", fence(p.traceback, "text"));
  return md;
}

/* 完整结果 → 仿豆包答案文档：状态开头，需求/代码/审核/测试分节展开 */
function resultToMarkdown(d) {
  const md = [];
  const isReview = d.mode === "review";
  const ok = d.status === "COMPLETED";
  const running = d.status === "RUNNING";
  const head = running
    ? (isReview ? "审核中" : "生成中")
    : (isReview ? "审核完成" : (ok ? "生成完成" : "已保留最优版本"));
  const lang = languageName(d.language);
  const isProject = Boolean(d.project_name || (d.final_files && d.final_files.length > 2));
  const form = isReview ? "只审核" : (isProject ? "多文件项目" : "单函数");
  md.push(`${running ? "⏳" : (ok ? "✅" : "⚠️")} **${head}** · ${lang} · ${form}`);
  if (!ok && !isReview && !running) {
    md.push("", "> 达到最大修复轮次仍未完全收敛，以下为当前最优版本，未解决问题见审核报告。");
  }

  const r = d.requirement;
  if (r) {
    md.push("", "## 需求确认");
    if (r.title) md.push(`- **功能**：${r.title}`);
    if (r.restated_requirement) md.push(`- **需求重述**：${r.restated_requirement}`);
    if (r.inputs && r.inputs.length) md.push(`- **输入**：${r.inputs.join("；")}`);
    if (r.outputs) md.push(`- **输出**：${r.outputs}`);
    if (r.edge_cases && r.edge_cases.length) md.push(`- **边界场景**：${r.edge_cases.join("；")}`);
    if (r.constraints && r.constraints.length) md.push(`- **实现约束**：${r.constraints.join("；")}`);
    if (r.acceptance_criteria && r.acceptance_criteria.length) md.push(`- **验收标准**：${r.acceptance_criteria.join("；")}`);
    if (r.assumptions && r.assumptions.length) md.push(`- **假设**：${r.assumptions.join("；")}`);
  }

  let versions = d.versions || [];
  if (!versions.length && (d.final_code || (d.final_files && d.final_files.length))) {
    // 流式中间态：版本尚未归档（test 节点才入 versions），
    // 用最终字段拼一个临时版本，让代码/审核/测试随进度逐段出现。
    versions = [{
      version_no: 1,
      round: d.repair_rounds || 0,
      language: d.language || "python",
      solution_code: d.final_code || "",
      test_code: d.final_test || "",
      project_name: d.project_name || "",
      files: d.final_files || [],
      entry_file: d.entry_file || "",
      run_command: d.run_command || "",
      review: d.final_review || null,
      pytest: d.final_pytest || null,
    }];
  }
  versions.forEach((v) => {
    const label = isReview ? "待审核代码" : (v.round === 0 ? "初始生成" : `第 ${v.round} 轮修复`);
    const vlang = v.language || d.language || "python";
    if (v.files && v.files.length && (v.project_name || v.files.length > 2)) {
      md.push("", `## ${label}（${v.files.length} 个文件）`);
      if (v.project_name) md.push(`- **工程**：${v.project_name}`);
      md.push(`- **语言**：${languageName(vlang)}`);
      if (v.entry_file) md.push(`- **入口**：\`${v.entry_file}\``);
      if (v.run_command) md.push(`- **运行**：\`${v.run_command}\``);
      v.files.forEach((f) => {
        md.push("", `### \`${f.path}\``, fence(f.content || "", f.language || vlang));
      });
    } else {
      md.push("", `## ${label}`, fence(v.solution_code || "", vlang));
      if (v.test_code) md.push("", "**测试代码**（test_solution.py）", fence(v.test_code, "python"));
    }
    if (v.review) md.push("", ...reviewMarkdown(v.review));
    if (v.pytest) md.push("", ...pytestMarkdown(v.pytest, vlang));
  });
  return md.join("\n");
}

function renderResult(d) {
  // 仿豆包答案：富文本分节展示；保留“导出结果 ZIP”按钮，导出能力不受影响
  return `
  <div class="md-bubble">
    <div class="result-actions"><button class="export-btn" type="button">导出结果 ZIP</button></div>
    <div class="md">${mdToHtml(resultToMarkdown(d))}</div>
  </div>`;
}

/* 导出按钮只在当前会话可用；历史结果统一导出该会话最近一次完整结果。 */
function bindExportButtons() {
  messagesEl.querySelectorAll(".export-btn").forEach((btn) => {
    btn.onclick = async () => {
      const conversation = current();
      if (!conversation || !conversation.id) return;
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = "正在打包…";
      try {
        const response = await fetch(
          `/api/conversations/${encodeURIComponent(conversation.id)}/export?format=zip&scope=latest`
        );
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}));
          throw new Error(detail.detail || `导出失败（HTTP ${response.status}）`);
        }
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `conversation_${conversation.id.slice(0, 12)}.zip`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
      } catch (err) {
        window.alert(err.message || "导出失败");
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    };
  });
}

/* ============================== 事件绑定 ============================== */
sendBtn.onclick = send;
document.getElementById("newChatBtn").onclick = () => newConversation();
attachBtn.onclick = () => {
  uploadPurpose = "chat";
  fileInput.click();
};
knowledgeBtn.onclick = () => {
  uploadPurpose = "knowledge";
  fileInput.click();
};
fileInput.addEventListener("change", () => {
  const files = Array.from(fileInput.files || []);
  uploadFiles(files, uploadPurpose);
});
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
    testMode = Boolean(h.test_mode);
    const prefix = h.mode === "local"
      ? "本地模型 · "
      : (h.mode === "api" ? "真实 API · " : "离线 · ");
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
  // 先读取测试模式标记，确保首个会话编号也使用正确前缀
  await checkHealth();
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
}

bootstrap();
