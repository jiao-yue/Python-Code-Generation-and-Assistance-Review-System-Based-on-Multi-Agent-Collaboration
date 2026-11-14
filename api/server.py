"""
FastAPI 后端：把“多智能体 Python 代码生成与审查系统”通过 HTTP 暴露给前端。

只保留当前前端实际使用的能力：
    GET    /api/health                健康检查（模型、运行模式）
    GET    /api/conversations         读取持久化会话列表（MySQL）
    POST   /api/conversations         新建对话并立即持久化（刷新不丢失）
    GET    /api/conversations/{id}    读取单个会话的全部消息
    PATCH  /api/conversations/{id}    重命名会话（自定义标题）
    DELETE /api/conversations/{id}    删除会话及其全部消息（删除后重排消息编号）
    POST   /api/uploads               上传附件、MD5 去重，可选导入 RAG
    GET    /api/knowledge/documents   查看已入库的上传附件
    POST   /api/chat/generate         流式执行“需求确认→生成→审核→测试”（SSE）
    POST   /api/chat/review           流式执行“仅审核”（SSE）
    POST   /api/chat/resume           回答澄清问题并恢复生成（SSE）
    GET    /                          打开前端网页（web/index.html）

两个聊天接口都以 SSE（Server-Sent Events）逐段推送：
    {"type": "stage", ...}   阶段进度（正在确认需求/生成/审核/测试/修复）；
    {"type": "result", ...}  最终完整结果；
    {"type": "error", ...}   运行出错信息。

启动方式（项目根目录）：
    python -m uvicorn api.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from config import settings
from graph.chat_graph import has_pending_clarification, new_thread_id, stream_chat_pipeline
from models.chat_schemas import ChatPipelineResult
from services.chat_memory import chat_memory
from services.document_parser import UploadError
from services.rag_retriever import rag_retriever
from services.upload_service import AttachmentText, upload_service

# 项目根目录（本文件在 <root>/api/server.py，向上一级即根）
ROOT_DIR = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT_DIR / "web"


# ===========================================================================
# 请求体模型
# ===========================================================================
class ChatGenerateRequest(BaseModel):
    """POST /api/chat/generate 的请求体。"""

    requirement: str = ""  # 用户原始需求；有附件时可以为空
    conversation_id: str = ""  # 会话编号（为空则服务端生成）
    title: str = ""  # 会话标题（前端自定义或自动命名；为空则后端按需求截取）
    attachment_ids: list[str] = Field(default_factory=list)  # 已上传附件 ID，正文由服务端读取


class ChatReviewRequest(BaseModel):
    """POST /api/chat/review 的请求体。"""

    code: str = ""  # 待审核代码；有代码附件时可以为空
    test_code: str = ""  # 可选：配套测试代码
    context: str = ""  # 可选：需求上下文
    conversation_id: str = ""  # 会话编号（为空则服务端生成）
    title: str = ""  # 会话标题（为空则后端默认“代码审核”）
    attachment_ids: list[str] = Field(default_factory=list)  # 已上传的代码/文档附件 ID


class RenameRequest(BaseModel):
    """PATCH /api/conversations/{id} 的请求体。"""

    title: str  # 新标题


class CreateConversationRequest(BaseModel):
    """POST /api/conversations 的请求体：新建对话时立即持久化。"""

    conversation_id: str  # 前端生成的会话编号
    mode: str = "generate"  # 模式：generate / review
    title: str = ""  # 初始标题（为空则后端给默认标题）


class AnswerItem(BaseModel):
    """用户对一条澄清问题的回答。"""

    question: str = ""
    answer: str


class ChatResumeRequest(BaseModel):
    """POST /api/chat/resume 的请求体：提交补充回答并恢复被中断的执行。"""

    conversation_id: str
    thread_id: str = ""  # 中断事件返回的 thread_id
    requirement: str = ""  # 原始需求（检查点丢失时用于重新执行）
    title: str = ""
    answers: list[AnswerItem] = []


# ===========================================================================
# 序列化与 SSE 工具
# ===========================================================================
def serialize_chat(state: dict[str, Any]) -> dict[str, Any]:
    """把图的最终状态整理成前端使用的 JSON 友好结构。"""
    result = ChatPipelineResult(
        mode=state.get("mode", "generate"),
        run_id=state.get("run_id", ""),
        status=state.get("status", "COMPLETED"),
        requirement=state.get("confirmed"),
        versions=state.get("versions", []),
        final_code=state.get("solution_code", ""),
        final_review=state.get("review"),
        final_pytest=state.get("pytest_result"),
        repair_rounds=state.get("round", 0),
        raw_input=state.get("raw_requirement", ""),
    )
    return result.to_frontend()


def _sse_line(data: dict[str, Any]) -> str:
    """把一条数据包装成 SSE 的 data 行。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _next_turn_no(conversation_id: str) -> int:
    """计算该会话下一轮的轮次序号（已有用户消息数 + 1）。"""
    existing = chat_memory.get_conversation(conversation_id)
    if not existing:
        return 1
    user_turns = sum(1 for m in existing["messages"] if m["role"] == "user")
    return user_turns + 1


def _get_attachments(attachment_ids: list[str]) -> list[AttachmentText]:
    """读取并校验附件；解析/清单错误统一转成 HTTP 400。"""
    try:
        return upload_service.attachments_for_chat(attachment_ids)
    except (UploadError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _generation_input(requirement: str, attachments: list[AttachmentText]) -> str:
    """把附件正文作为有明确来源的需求材料拼入生成链路。"""
    parts = [requirement.strip()] if requirement.strip() else []
    parts.extend(
        f"【附件：{attachment.filename}】\n{attachment.text}" for attachment in attachments
    )
    return "\n\n".join(parts)


def _review_input(code: str, attachments: list[AttachmentText]) -> str:
    """把代码附件按文件边界拼接；注释分隔符不会破坏 Python 语法。"""
    parts = [code.strip()] if code.strip() else []
    parts.extend(
        f"# ===== 附件：{attachment.filename} =====\n{attachment.text}"
        for attachment in attachments
    )
    return "\n\n".join(parts)


# ===========================================================================
# 流式生成器：把图的阶段事件转成 SSE，并在结束时持久化
# ===========================================================================
def chat_event_stream(
    mode: str,
    conversation_id: str,
    raw_requirement: str,
    pasted_code: str,
    pasted_test: str,
    persist_title: str,
    thread_id: str = "",
    answers: list[dict[str, str]] | None = None,
    skip_interrupt: bool = False,
    result_only: bool = False,
) -> Iterator[str]:
    """执行图并逐段 yield SSE 文本。

    :param answers: 用户补充回答；不为 None 时从中断点恢复执行
    :param skip_interrupt: 恢复执行时不再重复中断（问题已经回答过）
    :param result_only: 只写助手结果，不重复写用户原始需求（恢复场景使用）
    """
    try:
        final_payload: dict[str, Any] | None = None
        for event in stream_chat_pipeline(
            mode=mode,
            raw_requirement=raw_requirement,
            pasted_code=pasted_code,
            pasted_test=pasted_test,
            thread_id=thread_id,
            answers=answers,
            skip_interrupt=skip_interrupt,
        ):
            if event["type"] == "interrupt":
                # 需求存在必须澄清的歧义：保存问题卡片并结束本次流，等待用户回答
                value = event.get("value") or {}
                questions = list(value.get("questions", []))
                turn_no = _next_turn_no(conversation_id)
                chat_memory.save_clarification(
                    conversation_id=conversation_id,
                    mode=mode,
                    user_text=raw_requirement,
                    payload={"questions": questions, "thread_id": event.get("thread_id", "")},
                    turn_no=turn_no,
                    title=persist_title,
                )
                yield _sse_line(
                    {
                        "type": "interrupt",
                        "conversation_id": conversation_id,
                        "thread_id": event.get("thread_id", ""),
                        "questions": questions,
                    }
                )
                return
            if event["type"] == "stage":
                # 阶段事件：只把前端需要的字段透传
                yield _sse_line(
                    {
                        "type": "stage",
                        "node": event["node"],
                        "label": event["label"],
                        "solution_code": event["solution_code"],
                        "round": event["round"],
                    }
                )
            else:
                # 最终状态：序列化后持久化，并作为结果事件推送
                final_payload = serialize_chat(event["state"])
                turn_no = _next_turn_no(conversation_id)
                if result_only:
                    chat_memory.save_assistant_result(
                        conversation_id, final_payload, turn_no, persist_title
                    )
                else:
                    chat_memory.save_result(
                        conversation_id=conversation_id,
                        mode=mode,
                        user_text=raw_requirement,
                        result=final_payload,
                        turn_no=turn_no,
                        title=persist_title,
                    )
                yield _sse_line(
                    {
                        "type": "result",
                        "conversation_id": conversation_id,
                        "data": final_payload,
                    }
                )
        if final_payload is None:
            yield _sse_line({"type": "error", "message": "未产生任何结果，请重试。"})
    except Exception as exc:  # 运行期异常：以错误事件返回，前端可直接展示
        message = f"{type(exc).__name__}: {exc}"
        # 记忆失败不应影响错误事件的返回，用 suppress 显式忽略记忆层异常
        with contextlib.suppress(Exception):
            if result_only:
                chat_memory.add_message(
                    conversation_id,
                    "assistant",
                    "error",
                    message,
                    _next_turn_no(conversation_id),
                )
            else:
                chat_memory.save_error(
                    conversation_id,
                    raw_requirement,
                    message,
                    _next_turn_no(conversation_id),
                )
        yield _sse_line({"type": "error", "message": message})


# ===========================================================================
# 创建 FastAPI 应用
# ===========================================================================
app = FastAPI(title="多智能体 Python 代码生成与审查系统 API", version="2.0.0")
# 索引刷新失败后，即使用户重传的是重复附件，也必须允许再次尝试刷新。
_knowledge_refresh_pending = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===========================================================================
# 接口实现
# ===========================================================================
@app.get("/api/health")
def health() -> dict[str, Any]:
    """健康检查：返回模型提供方、模型名、运行模式、持久记忆与知识库状态。"""
    api_key, _base, model = settings.get_llm_endpoint()
    mode = "mock" if (settings.FORCE_MOCK or not api_key) else "api"
    # 首次调用时确保向量索引已建好，让前端能展示“知识库 N 条”
    rag_available = rag_retriever.ensure_index()
    rag_stats = rag_retriever.stats()
    rag_stats["available"] = rag_available
    return {
        "status": "ok",
        "provider": settings.LLM_PROVIDER,
        "model": model,
        "mode": mode,
        "max_rounds": settings.MAX_ROUNDS,
        "memory": "mysql" if chat_memory.available else "unavailable",
        "rag": rag_stats,
    }


@app.get("/api/conversations")
def list_conversations() -> dict[str, Any]:
    """返回持久化会话列表。"""
    return {"conversations": chat_memory.list_conversations()}


@app.post("/api/conversations")
def create_conversation(req: CreateConversationRequest) -> dict[str, Any]:
    """新建对话时立即落库，保证刷新或重开浏览器后对话不消失。"""
    if not req.conversation_id.strip():
        raise HTTPException(status_code=400, detail="会话编号不能为空")
    mode = req.mode if req.mode in ("generate", "review") else "generate"
    default_title = "新的代码审核" if mode == "review" else "新的代码生成"
    title = req.title.strip() or default_title
    if len(title) > 255:
        raise HTTPException(status_code=400, detail="标题过长（最多 255 字）")
    if not chat_memory.available:
        raise HTTPException(status_code=503, detail="数据库不可用，无法创建会话")
    chat_memory.create_conversation(req.conversation_id, mode, title)
    return {"status": "created", "id": req.conversation_id, "mode": mode, "title": title}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict[str, Any]:
    """返回单个会话；不存在时返回 404。"""
    conversation = chat_memory.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在或数据库不可用")
    return conversation


@app.patch("/api/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, req: RenameRequest) -> dict[str, Any]:
    """重命名会话；标题为空返回 400，会话不存在返回 404。"""
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    if len(title) > 255:
        raise HTTPException(status_code=400, detail="标题过长（最多 255 字）")
    if not chat_memory.available:
        raise HTTPException(status_code=503, detail="数据库不可用，无法重命名")
    updated = chat_memory.rename_conversation(conversation_id, title)
    if not updated:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"status": "renamed", "id": conversation_id, "title": title}


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str) -> dict[str, Any]:
    """删除会话及其全部消息。"""
    if not chat_memory.available:
        raise HTTPException(status_code=503, detail="数据库不可用，无法删除")
    chat_memory.delete_conversation(conversation_id)
    return {"status": "deleted", "id": conversation_id}


@app.post("/api/uploads")
async def upload_attachments(
    files: Annotated[list[UploadFile], File()],
    import_to_knowledge: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    """上传并解析附件；相同 MD5 复用记录，入库后统一刷新一次 RAG。

    多文件上传采用逐文件结果：某一个文件格式错误不会撤销其他成功文件。
    图片和扫描 PDF 的 OCR 在线程池中执行，避免阻塞 FastAPI 事件循环。
    """
    global _knowledge_refresh_pending
    if len(files) > settings.UPLOAD_MAX_FILES:
        raise HTTPException(
            status_code=413, detail=f"一次最多上传 {settings.UPLOAD_MAX_FILES} 个文件"
        )

    results: list[dict[str, Any]] = []
    refresh_needed = False
    for upload in files:
        filename = upload.filename or "未命名附件"
        try:
            data = await upload.read(settings.UPLOAD_MAX_FILE_BYTES + 1)
            if len(data) > settings.UPLOAD_MAX_FILE_BYTES:
                raise UploadError(
                    f"单个文件不能超过 {settings.UPLOAD_MAX_FILE_BYTES // (1024 * 1024)} MB"
                )
            outcome = await run_in_threadpool(
                upload_service.save_upload, filename, data, import_to_knowledge
            )
            if import_to_knowledge and outcome.record.get("in_knowledge"):
                refresh_needed = (
                    refresh_needed or outcome.knowledge_changed or _knowledge_refresh_pending
                )
            if import_to_knowledge and outcome.knowledge_changed:
                message = (
                    "MD5 重复，复用附件并已导入知识库"
                    if outcome.duplicate
                    else "已上传并导入知识库"
                )
            elif import_to_knowledge:
                message = "MD5 重复，知识库中已存在，已跳过"
            elif outcome.duplicate:
                message = "MD5 重复，已复用原附件"
            else:
                message = "附件已上传"
            results.append(
                {
                    "status": "duplicate" if outcome.duplicate else "uploaded",
                    "message": message,
                    "knowledge_changed": outcome.knowledge_changed,
                    "attachment": outcome.record,
                }
            )
        except UploadError as exc:
            results.append({"status": "error", "filename": filename, "message": str(exc)})
        except Exception as exc:
            print(f"[Upload] {filename} 处理失败：{type(exc).__name__}: {exc}")
            results.append(
                {
                    "status": "error",
                    "filename": filename,
                    "message": f"附件处理失败：{type(exc).__name__}",
                }
            )
        finally:
            await upload.close()

    index_refreshed: bool | None = None
    if refresh_needed:
        index_refreshed = await run_in_threadpool(rag_retriever.refresh)
        _knowledge_refresh_pending = not index_refreshed
        if not index_refreshed:
            for result in results:
                if result.get("knowledge_changed"):
                    result["message"] += "；资料已保存，但索引刷新失败，可稍后重试"
    return {
        "results": results,
        "index_refreshed": index_refreshed,
        "knowledge_chunks": rag_retriever.chunk_count if index_refreshed else None,
    }


@app.get("/api/knowledge/documents")
def list_knowledge_documents() -> dict[str, Any]:
    """列出已导入 RAG 的上传附件；内置 Markdown 不在此重复展示。"""
    try:
        documents = upload_service.list_records(knowledge_only=True)
    except UploadError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"documents": documents, "total": len(documents)}


@app.post("/api/chat/generate")
def chat_generate(req: ChatGenerateRequest) -> StreamingResponse:
    """生成模式（SSE）：需求正文和附件材料一起进入确认、生成、审核与测试。"""
    attachments = _get_attachments(req.attachment_ids)
    requirement = _generation_input(req.requirement, attachments)
    if not requirement.strip():
        raise HTTPException(status_code=400, detail="需求或附件不能为空")
    conversation_id = req.conversation_id or f"c_{uuid.uuid4().hex[:12]}"
    fallback_title = req.requirement.strip()[:18]
    if not fallback_title and attachments:
        fallback_title = attachments[0].filename[:18]
    title = req.title.strip() or fallback_title or "附件代码生成"
    return StreamingResponse(
        chat_event_stream(
            mode="generate",
            conversation_id=conversation_id,
            raw_requirement=requirement,
            pasted_code="",
            pasted_test="",
            persist_title=title,
            thread_id=new_thread_id(conversation_id),
        ),
        media_type="text/event-stream",
    )


@app.post("/api/chat/review")
def chat_review(req: ChatReviewRequest) -> StreamingResponse:
    """仅审核模式（SSE）：粘贴代码与代码附件可单独或共同参与审核。"""
    attachments = _get_attachments(req.attachment_ids)
    code = _review_input(req.code, attachments)
    if not code.strip():
        raise HTTPException(status_code=400, detail="待审核代码或代码附件不能为空")
    conversation_id = req.conversation_id or f"c_{uuid.uuid4().hex[:12]}"
    requirement = req.context or "请审核以下代码的质量、安全与逻辑。"
    # 优先用前端标题，否则默认“代码审核”
    review_title = req.title.strip() or "代码审核"
    return StreamingResponse(
        chat_event_stream(
            mode="review",
            conversation_id=conversation_id,
            raw_requirement=requirement,
            pasted_code=code,
            pasted_test=req.test_code,
            persist_title=review_title,
            thread_id=new_thread_id(conversation_id),
        ),
        media_type="text/event-stream",
    )


@app.post("/api/chat/resume")
def chat_resume(req: ChatResumeRequest) -> StreamingResponse:
    """提交需求澄清回答，恢复此前被中断的生成流程（SSE 流式）。"""
    answers = [item for item in req.answers if item.answer.strip()]
    if not answers:
        raise HTTPException(status_code=400, detail="补充回答不能为空")
    conversation = chat_memory.get_conversation(req.conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在或数据库不可用")

    # 优先使用持久化的原始需求：其中可能包含附件正文，服务重启后的恢复不能丢附件。
    stored_requirement = next(
        (
            message.get("text", "")
            for message in conversation["messages"]
            if message.get("role") == "user" and message.get("kind") == "text"
        ),
        "",
    )
    requirement = stored_requirement or req.requirement.strip() or "按已确认需求继续"
    title = req.title.strip() or conversation["title"]
    answer_text = "；".join(
        f"{item.question.strip()}：{item.answer.strip()}"
        if item.question.strip()
        else item.answer.strip()
        for item in answers
    )
    chat_memory.save_answer(req.conversation_id, answer_text, _next_turn_no(req.conversation_id))
    payload = [
        {"question": item.question.strip(), "answer": item.answer.strip()} for item in answers
    ]

    if req.thread_id and has_pending_clarification(req.thread_id):
        # 正常恢复：从断点继续，不重复写入用户原始需求
        return StreamingResponse(
            chat_event_stream(
                mode="generate",
                conversation_id=req.conversation_id,
                raw_requirement=requirement,
                pasted_code="",
                pasted_test="",
                persist_title=title,
                thread_id=req.thread_id,
                answers=payload,
                skip_interrupt=True,
                result_only=True,
            ),
            media_type="text/event-stream",
        )

    # 检查点丢失（如服务重启）：把回答并入需求后重新执行，并跳过再次中断
    merged_requirement = f"{requirement}\n\n补充说明：\n" + "\n".join(
        f"- {item.question.strip()}：{item.answer.strip()}"
        if item.question.strip()
        else f"- {item.answer.strip()}"
        for item in answers
    )
    return StreamingResponse(
        chat_event_stream(
            mode="generate",
            conversation_id=req.conversation_id,
            raw_requirement=merged_requirement,
            pasted_code="",
            pasted_test="",
            persist_title=title,
            skip_interrupt=True,
            result_only=True,
        ),
        media_type="text/event-stream",
    )


# ===========================================================================
# 托管前端静态文件与首页
# ===========================================================================
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    """返回前端首页 web/index.html。"""
    index_path = WEB_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="前端页面不存在（web/index.html）")
    return FileResponse(index_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=False)
