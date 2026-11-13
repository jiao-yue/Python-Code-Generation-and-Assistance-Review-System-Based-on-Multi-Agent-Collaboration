"""
ChatMemory：基于 MySQL 的会话持久记忆。

把“会话”和“每一轮消息”持久化到 MySQL，重启服务、换浏览器或清空浏览器缓存后，
历史对话都不会丢失。只有两张表，结构简单：

    chat_conversations：一个会话一行（编号、模式、标题、时间）；
    chat_messages     ：一条消息一行（属于哪个会话、角色、类型、内容）。

如果 MySQL 暂时不可用，本服务会把 available 置为 False 并打印提示，
其它接口（健康检查、流式生成）照常工作，不会因为数据库故障而整体崩溃。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pymysql

from config import settings


class ChatMemory:
    """会话与消息的持久化仓库（MySQL 实现）。"""

    def __init__(self) -> None:
        """尝试连接 MySQL 并建好表；失败时标记为不可用。"""
        self.available = False
        try:
            self._ensure_database()
            self._ensure_tables()
            self.available = True
        except Exception as exc:
            print(f"[ChatMemory] MySQL 不可用，持久记忆已停用：{exc}")

    # ------------------------------------------------------------------
    # 连接与建表
    # ------------------------------------------------------------------
    def _connect(self, database: str | None = None) -> pymysql.connections.Connection:
        """建立一个短连接（每次操作独立连接，用完即关）。"""
        return pymysql.connect(
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            database=database or settings.DB_NAME,
            charset=settings.DB_CHARSET,
            autocommit=True,
        )

    def _ensure_database(self) -> None:
        """库名含空格，先连到服务器确保数据库存在，不存在则创建。"""
        conn = self._connect(database=None)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{settings.DB_NAME}` "
                    f"DEFAULT CHARACTER SET {settings.DB_CHARSET}"
                )
        finally:
            conn.close()

    def _ensure_tables(self) -> None:
        """创建会话表与消息表（已存在则跳过）。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_conversations (
                        conversation_id VARCHAR(64) PRIMARY KEY,
                        mode            VARCHAR(16) NOT NULL,
                        title           VARCHAR(255) NOT NULL,
                        created_at      DATETIME NOT NULL,
                        updated_at      DATETIME NOT NULL
                    ) CHARACTER SET utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_messages (
                        id              BIGINT AUTO_INCREMENT PRIMARY KEY,
                        conversation_id VARCHAR(64) NOT NULL,
                        turn_no         INT NOT NULL,
                        role            VARCHAR(16) NOT NULL,
                        kind            VARCHAR(16) NOT NULL,
                        content         LONGTEXT NULL,
                        created_at      DATETIME NOT NULL,
                        INDEX idx_conv (conversation_id)
                    ) CHARACTER SET utf8mb4
                    """
                )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def create_conversation(self, conversation_id: str, mode: str, title: str) -> None:
        """新建一个会话；若已存在则保持原样。"""
        if not self.available:
            return
        now = datetime.now()
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT IGNORE INTO chat_conversations "
                    "(conversation_id, mode, title, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (conversation_id, mode, title, now, now),
                )
        finally:
            conn.close()

    def add_message(
        self,
        conversation_id: str,
        role: str,
        kind: str,
        content: str,
        turn_no: int,
        title: str | None = None,
    ) -> None:
        """追加一条消息，并刷新会话的更新时间；需要时顺带更新标题。"""
        if not self.available:
            return
        now = datetime.now()
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO chat_messages "
                    "(conversation_id, turn_no, role, kind, content, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (conversation_id, turn_no, role, kind, content, now),
                )
                if title:
                    cur.execute(
                        "UPDATE chat_conversations SET title=%s, updated_at=%s "
                        "WHERE conversation_id=%s",
                        (title, now, conversation_id),
                    )
                else:
                    cur.execute(
                        "UPDATE chat_conversations SET updated_at=%s WHERE conversation_id=%s",
                        (now, conversation_id),
                    )
        finally:
            conn.close()

    def save_result(
        self,
        conversation_id: str,
        mode: str,
        user_text: str,
        result: dict[str, Any],
        turn_no: int,
        title: str | None = None,
    ) -> None:
        """一次性保存“用户提问 + 助手结果”两条消息（最常用的便捷方法）。"""
        self.create_conversation(conversation_id, mode, title or user_text[:18])
        self.add_message(conversation_id, "user", "text", user_text, turn_no)
        payload = json.dumps(result, ensure_ascii=False)
        self.add_message(conversation_id, "assistant", "result", payload, turn_no, title=title)

    def save_clarification(
        self,
        conversation_id: str,
        mode: str,
        user_text: str,
        payload: dict[str, Any],
        turn_no: int,
        title: str | None = None,
    ) -> None:
        """保存“用户需求 + 待澄清问题”：刷新页面后仍能看到问题卡片。"""
        self.create_conversation(conversation_id, mode, title or user_text[:18])
        self.add_message(conversation_id, "user", "text", user_text, turn_no)
        self.add_message(
            conversation_id,
            "assistant",
            "question",
            json.dumps(payload, ensure_ascii=False),
            turn_no,
            title=title,
        )

    def save_answer(self, conversation_id: str, answer_text: str, turn_no: int) -> None:
        """保存用户对澄清问题的补充回答。"""
        self.add_message(conversation_id, "user", "text", answer_text, turn_no)

    def save_assistant_result(
        self,
        conversation_id: str,
        result: dict[str, Any],
        turn_no: int,
        title: str | None = None,
    ) -> None:
        """只保存助手结果（用于中断恢复后，避免重复写入用户原始需求）。"""
        self.add_message(
            conversation_id,
            "assistant",
            "result",
            json.dumps(result, ensure_ascii=False),
            turn_no,
            title=title,
        )

    def save_error(
        self, conversation_id: str, user_text: str, error_message: str, turn_no: int
    ) -> None:
        """保存一次失败：用户提问 + 助手错误消息。"""
        self.create_conversation(conversation_id, "generate", user_text[:18])
        self.add_message(conversation_id, "user", "text", user_text, turn_no)
        self.add_message(conversation_id, "assistant", "error", error_message, turn_no)

    def rename_conversation(self, conversation_id: str, title: str) -> bool:
        """更新会话标题，返回是否真的更新了一行（不存在则为 False）。"""
        if not self.available:
            return False
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                affected = cur.execute(
                    "UPDATE chat_conversations SET title=%s, updated_at=%s "
                    "WHERE conversation_id=%s",
                    (title, datetime.now(), conversation_id),
                )
        finally:
            conn.close()
        return affected > 0

    def delete_conversation(self, conversation_id: str) -> None:
        """删除一个会话及其全部消息（先删消息，再删会话元信息），随后重排编号。"""
        if not self.available:
            return
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM chat_messages WHERE conversation_id=%s",
                    (conversation_id,),
                )
                cur.execute(
                    "DELETE FROM chat_conversations WHERE conversation_id=%s",
                    (conversation_id,),
                )
        finally:
            conn.close()
        # 删除会在编号上留下空洞，重新编号保持连续
        self.renumber_messages()

    def renumber_messages(self) -> None:
        """
        把消息主键 id 重排为从 1 开始的连续整数，不留空洞。

        规范顺序：先按会话（conversation_id），再按轮次（turn_no），
        同轮内按原 id（即真实写入先后，用户消息在前）。
        主键更新可能与现有值冲突，因此分两阶段：先整体平移到高位区间，
        再依次写回 1..N，最后把自增计数器重置为 N+1，保证后续新增继续连续。
        """
        if not self.available:
            return
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM chat_messages ORDER BY conversation_id, turn_no, id")
                old_ids = [row[0] for row in cur.fetchall()]
                if not old_ids:
                    # 表空：把自增计数器归位，避免下一条从旧值开始
                    cur.execute("ALTER TABLE chat_messages AUTO_INCREMENT = 1")
                    return
                offset = max(old_ids) + 1000
                # 第一阶段：全部移到高位，消除与最终编号的主键冲突
                for seq, old_id in enumerate(old_ids, start=1):
                    cur.execute(
                        "UPDATE chat_messages SET id=%s WHERE id=%s",
                        (offset + seq, old_id),
                    )
                # 第二阶段：从 1 开始连续写回
                for seq in range(1, len(old_ids) + 1):
                    cur.execute(
                        "UPDATE chat_messages SET id=%s WHERE id=%s",
                        (seq, offset + seq),
                    )
                cur.execute(
                    "ALTER TABLE chat_messages AUTO_INCREMENT = %s",
                    (len(old_ids) + 1,),
                )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def list_conversations(self) -> list[dict[str, Any]]:
        """返回全部会话（按更新时间倒序），用于左侧历史列表。"""
        if not self.available:
            return []
        conn = self._connect()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT conversation_id, mode, title, created_at, updated_at "
                    "FROM chat_conversations ORDER BY updated_at DESC"
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        return [
            {
                "id": row["conversation_id"],
                "mode": row["mode"],
                "title": row["title"],
                "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat(),
            }
            for row in rows
        ]

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        """读取单个会话的元信息与全部消息（结果类消息会还原成对象）。"""
        if not self.available:
            return None
        conn = self._connect()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT conversation_id, mode, title FROM chat_conversations "
                    "WHERE conversation_id=%s",
                    (conversation_id,),
                )
                meta = cur.fetchone()
                if meta is None:
                    return None
                cur.execute(
                    "SELECT role, kind, content FROM chat_messages "
                    "WHERE conversation_id=%s ORDER BY id ASC",
                    (conversation_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        messages = [self._decode_message(row) for row in rows]
        return {
            "id": meta["conversation_id"],
            "mode": meta["mode"],
            "title": meta["title"],
            "messages": messages,
        }

    @staticmethod
    def _decode_message(row: dict[str, Any]) -> dict[str, Any]:
        """把数据库一行还原成前端使用的消息结构。"""
        kind = row["kind"]
        if kind == "result":
            # 结果类内容存的是 JSON，解析回对象
            try:
                return {"role": row["role"], "kind": "result", "data": json.loads(row["content"])}
            except (TypeError, json.JSONDecodeError):
                return {"role": row["role"], "kind": "error", "text": "历史结果无法解析"}
        if kind == "question":
            # 待澄清问题同样存 JSON：问题列表 + thread_id（用于恢复执行）
            try:
                return {"role": row["role"], "kind": "question", "data": json.loads(row["content"])}
            except (TypeError, json.JSONDecodeError):
                return {"role": row["role"], "kind": "error", "text": "历史问题无法解析"}
        # 文本与错误都直接使用字符串
        return {"role": row["role"], "kind": kind, "text": row["content"] or ""}


# 模块级单例：导入时即完成建表，整个应用共用一份记忆
chat_memory = ChatMemory()
