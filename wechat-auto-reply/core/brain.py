# -*- coding: utf-8 -*-
"""决策引擎：消息过滤分流 → 记忆组装 → LLM 生成 → 输出校验 → 发送。

同时维护消息流水表（data/chat.db 的 chat_messages），记录每一条
入站消息与处理结果，便于审计与排查。
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from typing import Dict, Optional

from loguru import logger

from .config import in_quiet_time, period_to_minutes
from .llm import LLMClient
from .memory import Memory
from .sender import Sender

MAX_REPLY_LEN = 500          # 回复长度上限（超出截断）
PLACEHOLDER_PATTERNS = ("[", "]", "[图片", "[语音", "[视频", "[文件", "[表情", "[链接")


class Brain:
    def __init__(self, config: dict, llm: LLMClient, memory: Memory, sender: Sender,
                 self_name: str = ""):
        self.config = config
        self.llm = llm
        self.memory = memory
        self.sender = sender
        self.self_name = (self_name or "").strip()
        safety = config.get("safety", {})
        self.enabled = bool(safety.get("enabled", True))
        self.blocked_keywords = [str(k) for k in safety.get("blocked_keywords", []) or []]
        self.group_min_interval = float(safety.get("group_min_interval", 60) or 0)
        self._last_group_reply: Dict[str, float] = {}
        self._init_store()

    # -- 会话策略辅助 ----------------------------------------------------

    @staticmethod
    def is_group(username: str) -> bool:
        """群会话的 username 以 @chatroom 结尾。"""
        return "@chatroom" in (username or "")

    def _is_mentioned(self, content: str) -> bool:
        """群 at_only 模式下判断消息是否提及本机（按昵称匹配）。"""
        if not self.self_name:
            return False  # 无法识别本机昵称时保守不回复
        return self.self_name in (content or "")

    def _group_throttled(self, talker: str) -> bool:
        """群防刷屏：距上次回复不足 group_min_interval 秒则跳过。"""
        if self.group_min_interval <= 0:
            return False
        last = self._last_group_reply.get(talker)
        if last is None:
            return False
        return (time.time() - last) < self.group_min_interval

    def _target_periods(self, target: dict):
        """会话级静默时段覆盖（分钟对列表）；无覆盖返回 None（用全局）。"""
        tp = target.get("quiet_periods") or []
        ps = [p for p in (period_to_minutes(x) for x in tp) if p]
        return ps or None

    # -- 消息流水 --------------------------------------------------------

    def _store_path(self) -> str:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, "data", "chat.db")

    def _init_store(self) -> None:
        path = self._store_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            conn = sqlite3.connect(path)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    talker TEXT, sender TEXT, content TEXT, msg_type TEXT,
                    direction TEXT, status TEXT, msg_time INTEGER, created_at INTEGER
                )"""
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning("消息流水表初始化失败: {}", exc)

    def _record(self, talker: str, sender: str, content: str, msg_type: str,
                direction: str, status: str, msg_time: int) -> None:
        try:
            conn = sqlite3.connect(self._store_path())
            conn.execute(
                "INSERT INTO chat_messages (talker, sender, content, msg_type, direction, status, msg_time, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (talker, sender, content, msg_type, direction, status, msg_time, int(time.time())),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning("写入消息流水失败: {}", exc)

    # -- 过滤 ------------------------------------------------------------

    @staticmethod
    def _is_placeholder(content: str) -> bool:
        """跳过占位符内容（图片/语音等非文本消息的正文占位）。"""
        c = (content or "").strip()
        if not c:
            return True
        if c.startswith("[") and c.endswith("]"):
            return True
        return False

    def _should_block_keyword(self, content: str) -> bool:
        return any(k and k in content for k in self.blocked_keywords)

    # -- 主处理 ----------------------------------------------------------

    def handle(self, msg: Dict, target: dict, username: str) -> None:
        """Listener 回调入口。target 为 parse_listen 的结构化目标（含
        enabled/at_only/quiet_periods 等会话级策略）。"""
        talker = username
        content = (msg.get("content") or "").strip()
        mtype = str(msg.get("type") or "")

        # 1) 全局总开关
        if not self.enabled:
            logger.debug("全局总开关关闭，跳过 {}", talker)
            return
        # 1.5) 会话级开关
        if not target.get("enabled", True):
            logger.debug("会话级开关关闭，跳过 {}", talker)
            return
        # 2) 自己发的消息（防死循环）
        if msg.get("is_self"):
            logger.debug("本机发送的消息，跳过 {}", talker)
            return
        # 3) 非文本内容
        if self._is_placeholder(content):
            logger.info("忽略非文本消息（{}）: {} {}", talker, mtype, content[:30])
            return
        # 3.5) 群策略：at_only（仅回复提及本机的消息）+ 防刷屏
        if self.is_group(talker):
            if target.get("at_only") and not self._is_mentioned(content):
                logger.info("群消息未提及本机，跳过 {}", talker)
                return
            if self._group_throttled(talker):
                logger.debug("群消息过于频繁（间隔 < {}s），跳过 {}", self.group_min_interval, talker)
                return
        # 4) 静默时段（会话级覆盖优先）
        now_hm = datetime.now().strftime("%H:%M")
        if in_quiet_time(self.config, now_hm, periods=self._target_periods(target)):
            logger.info("静默时段（{}），跳过 {}", now_hm, talker)
            return
        # 5) 拦截词
        if self._should_block_keyword(content):
            logger.info("命中拦截词，跳过 {}: {}", talker, content[:30])
            self._record(talker, str(msg.get("sender_username") or msg.get("sender_id") or ""),
                         content, mtype, "in", "blocked", int(msg.get("create_time") or time.time()))
            return

        # 6) 组装 + 生成
        messages = self.memory.build_messages(talker, target.get("role", "assistant"), content)
        reply = self.llm.chat(messages)

        if not reply:
            self._record(talker, str(msg.get("sender_username") or msg.get("sender_id") or ""),
                         content, mtype, "in", "llm_failed", int(msg.get("create_time") or time.time()))
            logger.error("LLM 未生成回复，跳过 {}", talker)
            return

        # 7) 输出校验
        reply = reply.replace("\r\n", "\n").strip()
        # 聊天风格无标点：LLM 偶发用反斜杠分段时，硬转为换行分隔
        reply = reply.replace("\\", "\n")
        reply = "\n".join(ln.strip() for ln in reply.split("\n") if ln.strip())
        if len(reply) > MAX_REPLY_LEN:
            logger.warning("回复超长（{} 字符），截断至 {}", len(reply), MAX_REPLY_LEN)
            reply = reply[:MAX_REPLY_LEN]
        if not reply or self._should_block_keyword(reply):
            logger.warning("回复为空或命中拦截词，不发送: {}", reply[:30])
            return

        # 8) 发送
        ok = self.sender.send_text(talker, reply)
        if ok:
            if self.is_group(talker):
                self._last_group_reply[talker] = time.time()
            self.memory.append(talker, content, reply)
            self._record(talker, str(msg.get("sender_username") or msg.get("sender_id") or ""),
                         content, mtype, "in", "replied", int(msg.get("create_time") or time.time()))
            self._record(talker, "self", reply, "text", "out", "sent", int(time.time()))
        else:
            self._record(talker, str(msg.get("sender_username") or msg.get("sender_id") or ""),
                         content, mtype, "in", "send_failed", int(msg.get("create_time") or time.time()))
