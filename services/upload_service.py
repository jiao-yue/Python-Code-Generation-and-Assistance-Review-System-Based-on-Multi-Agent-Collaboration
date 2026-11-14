"""附件持久化、MD5 去重与知识库文件清单。

目录结构（全部位于被 Git 忽略的 storage/uploads/）：

    files/       原始附件，文件名使用附件 ID，不使用用户输入路径
    text/        对话使用的纯文本
    knowledge/   RAG 使用的 Markdown
    manifest.json  附件元数据、原始文件 MD5 与是否已导入知识库

MD5 在这里仅用于内容去重，不用于安全认证。重复上传相同字节时复用已有附件，
不会重复解析或重复进入 RAG；同名但内容不同的文件会得到不同附件 ID。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config import settings
from services.document_parser import ParsedDocument, UploadError, parse_document

_ATTACHMENT_ID = re.compile(r"^att_[0-9a-f]{32}$")
_MANIFEST_VERSION = 1


@dataclass(frozen=True)
class UploadOutcome:
    """一次上传的结果：元数据、是否重复，以及本次是否新加入知识库。"""

    record: dict[str, Any]
    duplicate: bool
    knowledge_changed: bool


@dataclass(frozen=True)
class AttachmentText:
    """准备注入当前对话的一份附件文本。"""

    attachment_id: str
    filename: str
    text: str
    truncated: bool


def _atomic_write(path: Path, data: bytes) -> None:
    """先写临时文件再原子替换，避免异常退出留下半份文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(data)
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _atomic_write_text(path: Path, text: str) -> None:
    """以 UTF-8 无 BOM、LF 换行保存提取文本。"""
    _atomic_write(path, text.encode("utf-8"))


class UploadService:
    """进程内附件仓库；清单读写和去重由同一把锁保护。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loaded = False
        self._records: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 路径与清单
    # ------------------------------------------------------------------
    def _path(self, relative: str) -> Path:
        """把清单中的相对路径定位到当前项目运行目录。"""
        return settings.UPLOAD_DIR / relative

    def _load_locked(self) -> None:
        """首次使用时读取清单；损坏的清单不能被静默覆盖。"""
        if self._loaded:
            return
        settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        path = settings.UPLOAD_MANIFEST_PATH
        records: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise UploadError("附件清单无法读取，请检查 storage/uploads/manifest.json") from exc
            raw_records = payload.get("records")
            if payload.get("version") != _MANIFEST_VERSION or not isinstance(raw_records, list):
                raise UploadError("附件清单版本或结构不受支持")
            for record in raw_records:
                attachment_id = record.get("id", "")
                if not _ATTACHMENT_ID.fullmatch(attachment_id):
                    raise UploadError("附件清单包含无效的附件 ID")
                for key in ("raw_file", "text_file", "knowledge_file"):
                    relative = record.get(key, "")
                    candidate = Path(relative)
                    if candidate.is_absolute() or ".." in candidate.parts:
                        raise UploadError("附件清单包含不安全的文件路径")
                records[attachment_id] = record
        self._records = records
        self._loaded = True

    def _save_locked(self) -> None:
        """按上传时间稳定排序后原子写入清单。"""
        records = sorted(self._records.values(), key=lambda item: (item["created_at"], item["id"]))
        payload = json.dumps(
            {"version": _MANIFEST_VERSION, "records": records},
            ensure_ascii=False,
            indent=2,
        )
        _atomic_write_text(settings.UPLOAD_MANIFEST_PATH, payload + "\n")

    @staticmethod
    def _safe_filename(filename: str) -> str:
        """清理控制字符和路径分隔符，只把结果用作显示名。"""
        name = Path(filename or "未命名附件").name
        name = "".join(character for character in name if ord(character) >= 32).strip()
        if not name:
            name = "未命名附件"
        return name[:240]

    # ------------------------------------------------------------------
    # 上传与去重
    # ------------------------------------------------------------------
    def save_upload(
        self, filename: str, data: bytes, import_to_knowledge: bool = False
    ) -> UploadOutcome:
        """保存附件；MD5 相同且原始字节一致时复用，不重复建立知识条目。"""
        if not data:
            raise UploadError("不能上传空文件")
        if len(data) > settings.UPLOAD_MAX_FILE_BYTES:
            raise UploadError(
                f"单个文件不能超过 {settings.UPLOAD_MAX_FILE_BYTES // (1024 * 1024)} MB"
            )

        safe_name = self._safe_filename(filename)
        extension = Path(safe_name).suffix.lower()
        if extension not in settings.UPLOAD_ALLOWED_EXTENSIONS:
            supported = "、".join(sorted(settings.UPLOAD_ALLOWED_EXTENSIONS))
            raise UploadError(f"不支持 {extension or '无扩展名'}；支持：{supported}")

        digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
        attachment_id = f"att_{digest}"
        with self._lock:
            self._load_locked()
            existing = self._records.get(attachment_id)
            if existing is not None:
                return self._reuse_existing(existing, data, import_to_knowledge=import_to_knowledge)

            parsed = parse_document(safe_name, data)
            now = datetime.now(UTC).isoformat()
            record = {
                "id": attachment_id,
                "md5": digest,
                "filename": safe_name,
                "extension": extension,
                "size": len(data),
                "text_chars": len(parsed.plain_text),
                "truncated": parsed.truncated,
                "warnings": parsed.warnings,
                "created_at": now,
                "in_knowledge": bool(import_to_knowledge),
                "raw_file": f"files/{attachment_id}{extension}",
                "text_file": f"text/{attachment_id}.txt",
                "knowledge_file": f"knowledge/{attachment_id}.md",
            }
            self._write_new_files(record, data, parsed)
            self._records[attachment_id] = record
            try:
                self._save_locked()
            except Exception:
                # 清单未能保存时，记录不能留在内存里伪装成已持久化。
                self._records.pop(attachment_id, None)
                raise
            return UploadOutcome(
                record=self.public_record(record),
                duplicate=False,
                knowledge_changed=bool(import_to_knowledge),
            )

    def _reuse_existing(
        self, record: dict[str, Any], data: bytes, import_to_knowledge: bool
    ) -> UploadOutcome:
        """复用已有记录；若此前未入库，本次仍可补入知识库。

        纯重复上传不改变元数据，因此不重复写 manifest。若清单保存失败，
        内存状态也回滚到操作前，避免“磁盘未入库、内存却已入库”的不一致。
        """
        original_record = record.copy()  # 后续字段都是替换而非原地修改，浅拷贝足够
        raw_path = self._path(record["raw_file"])
        if raw_path.exists() and raw_path.read_bytes() != data:
            # MD5 冲突极罕见，但不能因此覆盖另一份真实文件。
            raise UploadError("检测到 MD5 相同但内容不同的文件，已停止覆盖")
        if not raw_path.exists():
            _atomic_write(raw_path, data)

        # 允许从异常中断中恢复缺失的提取文本，而不是重新建立一条记录。
        text_path = self._path(record["text_file"])
        knowledge_path = self._path(record["knowledge_file"])
        if not text_path.exists() or not knowledge_path.exists():
            parsed = parse_document(record["filename"], data)
            _atomic_write_text(text_path, parsed.plain_text)
            _atomic_write_text(knowledge_path, parsed.markdown)
            record["text_chars"] = len(parsed.plain_text)
            record["truncated"] = parsed.truncated
            record["warnings"] = parsed.warnings

        knowledge_changed = bool(import_to_knowledge and not record.get("in_knowledge"))
        if knowledge_changed:
            record["in_knowledge"] = True
        if record != original_record:
            try:
                self._save_locked()
            except Exception:
                record.clear()
                record.update(original_record)
                raise
        return UploadOutcome(
            record=self.public_record(record),
            duplicate=True,
            knowledge_changed=knowledge_changed,
        )

    def _write_new_files(self, record: dict[str, Any], data: bytes, parsed: ParsedDocument) -> None:
        """写入新附件的三个文件；失败时只清理本次新建的文件。"""
        paths = [
            self._path(record["raw_file"]),
            self._path(record["text_file"]),
            self._path(record["knowledge_file"]),
        ]
        try:
            _atomic_write(paths[0], data)
            _atomic_write_text(paths[1], parsed.plain_text)
            _atomic_write_text(paths[2], parsed.markdown)
        except Exception:
            for path in paths:
                if path.exists():
                    path.unlink()
            raise

    # ------------------------------------------------------------------
    # 对话与 RAG 读取
    # ------------------------------------------------------------------
    def attachments_for_chat(self, attachment_ids: list[str]) -> list[AttachmentText]:
        """读取指定附件并限制注入字符数，预算在所有附件间均匀分配。"""
        unique_ids = list(dict.fromkeys(attachment_ids))
        if not unique_ids:
            return []
        if len(unique_ids) > settings.UPLOAD_MAX_FILES:
            raise UploadError(f"一次最多使用 {settings.UPLOAD_MAX_FILES} 个附件")

        with self._lock:
            self._load_locked()
            records: list[dict[str, Any]] = []
            for attachment_id in unique_ids:
                if not _ATTACHMENT_ID.fullmatch(attachment_id):
                    raise UploadError("附件 ID 格式无效")
                record = self._records.get(attachment_id)
                if record is None:
                    raise UploadError("附件不存在或已被清理，请重新上传")
                records.append(record)

            remaining_budget = settings.UPLOAD_CHAT_TOTAL_CHARS
            results: list[AttachmentText] = []
            for index, record in enumerate(records):
                try:
                    text = self._path(record["text_file"]).read_text(encoding="utf-8")
                except OSError as exc:
                    raise UploadError("附件提取文本缺失或无法读取，请重新上传") from exc
                remaining_files = len(records) - index
                share = min(
                    settings.UPLOAD_CHAT_FILE_CHARS,
                    max(0, remaining_budget // remaining_files),
                )
                selected = text[:share]
                truncated = bool(record.get("truncated")) or len(selected) < len(text)
                if len(selected) < len(text):
                    marker = "\n[附件内容过长，已截断]"
                    if share > len(marker):
                        selected = text[: share - len(marker)].rstrip() + marker
                    else:
                        selected = text[:share]
                results.append(
                    AttachmentText(
                        attachment_id=record["id"],
                        filename=record["filename"],
                        text=selected,
                        truncated=truncated,
                    )
                )
                remaining_budget = max(0, remaining_budget - len(selected))
            return results

    def knowledge_files(self) -> list[Path]:
        """返回已明确导入知识库的 Markdown；仅聊天附件不会进入 RAG。"""
        with self._lock:
            self._load_locked()
            paths: list[Path] = []
            for record in sorted(
                self._records.values(), key=lambda item: (item["created_at"], item["id"])
            ):
                if not record.get("in_knowledge"):
                    continue
                path = self._path(record["knowledge_file"])
                if path.exists():
                    paths.append(path)
            return paths

    # ------------------------------------------------------------------
    # 对外元数据
    # ------------------------------------------------------------------
    @staticmethod
    def public_record(record: dict[str, Any]) -> dict[str, Any]:
        """移除服务端路径，只返回前端需要的元数据。"""
        return {
            key: value
            for key, value in record.items()
            if key not in {"raw_file", "text_file", "knowledge_file"}
        }

    def list_records(self, knowledge_only: bool = False) -> list[dict[str, Any]]:
        """按上传顺序列出附件元数据。"""
        with self._lock:
            self._load_locked()
            records = sorted(
                self._records.values(), key=lambda item: (item["created_at"], item["id"])
            )
            return [
                self.public_record(record)
                for record in records
                if not knowledge_only or record.get("in_knowledge")
            ]


# 整个应用共用一份清单和去重状态。
upload_service = UploadService()
