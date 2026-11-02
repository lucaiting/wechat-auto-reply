# -*- coding: utf-8 -*-
"""三层记忆：角色卡 + 短期窗口 + 长期向量记忆（本地检索）。

- 角色卡：prompts/<角色名>.md，作为 system 消息；
- 短期记忆：每个会话最近 N 轮对话（内存滑动窗口，按轮数截断）；
- 长期记忆：LongTermMemory（core/longmem.py），按会话持久化，
  检索命中拼入 system，帮助模型回忆短期窗口之外的信息。
"""
from __future__ import annotations

import os
from collections import deque
from typing import Deque, Dict, List, Optional

from loguru import logger

from .longmem import LongTermMemory

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")

# 用户消息短于该长度时不写入长期记忆（避免噪音）
LONG_TERM_MIN_LEN = 6


class Memory:
    def __init__(self, short_window: int = 10,
                 long_term: Optional[LongTermMemory] = None):
        self.short_window = max(2, int(short_window))
        self._windows: Dict[str, Deque[Dict]] = {}
        self._role_cache: Dict[str, str] = {}
        self.long_term = long_term  # LongTermMemory 实例或 None

    # -- 角色卡 ----------------------------------------------------------

    def role_prompt(self, role: str) -> str:
        """读取 prompts/<角色>.md；不存在时回退为通用助理人设。"""
        role = (role or "assistant").strip()
        if role in self._role_cache:
            return self._role_cache[role]
        path = os.path.join(PROMPTS_DIR, f"{role}.md")
        text = ""
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read().strip()
            except OSError as exc:
                logger.warning("读取角色文件失败 {}: {}", path, exc)
        if not text:
            text = "你是一个乐于助人的中文助理，请用简洁自然的语气回复微信消息。"
            logger.warning("角色文件 {} 不存在或为空，使用默认人设", path)
        self._role_cache[role] = text
        return text

    # -- 短期记忆 --------------------------------------------------------

    def _win(self, talker: str) -> Deque[Dict]:
        if talker not in self._windows:
            self._windows[talker] = deque(maxlen=self.short_window)
        return self._windows[talker]

    def append(self, talker: str, user_msg: str, assistant_msg: str) -> None:
        """追加一轮对话：进短期窗口，同时沉淀到长期记忆。"""
        win = self._win(talker)
        if user_msg:
            win.append({"role": "user", "content": user_msg})
        if assistant_msg:
            win.append({"role": "assistant", "content": assistant_msg})
        if self.long_term is not None:
            self._persist_long_term(talker, user_msg, assistant_msg)

    def _persist_long_term(self, talker: str, user_msg: str, assistant_msg: str) -> None:
        user = (user_msg or "").strip()
        reply = (assistant_msg or "").strip()
        if len(user) < LONG_TERM_MIN_LEN:
            return
        pair = f"用户问：{user[:200]}"
        if reply:
            pair += f"；我答：{reply[:200]}"
        self.long_term.add(talker, pair)

    # -- 组装 ------------------------------------------------------------

    def build_messages(self, talker: str, role: str, new_msg: str) -> List[Dict]:
        """组装 messages：system 角色卡 + 长期记忆命中 + 短期窗口 + 当前消息。"""
        messages: List[Dict] = [{"role": "system", "content": self.role_prompt(role)}]
        if self.long_term is not None:
            hits = self.long_term.search(talker, new_msg)
            if hits:
                ctx = "\n".join(f"- {h['content']}" for h in hits)
                messages.append({
                    "role": "system",
                    "content": f"[以下是与该会话相关的长期记忆，可能有助于回答]\n{ctx}\n[长期记忆结束]",
                })
        messages.extend(list(self._win(talker)))
        messages.append({"role": "user", "content": new_msg})
        return messages
