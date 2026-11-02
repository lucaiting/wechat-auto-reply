# -*- coding: utf-8 -*-
"""长期记忆（M2）：本地零依赖持久化 + 检索。

设计说明：
- 存储：data/memory.db 的 long_term_memory 表，按会话（talker=username）隔离；
- 写入：Brain 每轮成功对话后通过 Memory.append 自动追加（自动去重、
  每会话按 max_per_talker 裁剪最旧）；
- 检索：中文 bigram + 英文/数字词切分的简化 BM25 打分（纯标准库，
  不依赖 numpy/torch/外部向量库）；
- 注入：Memory.build_messages 命中后拼入 system 消息，帮助模型回忆
  短期窗口之外的信息。

后续若接入 embedding API（如 Qwen text-embedding-v3），只需替换
tokenize/score 为向量余弦，表结构与本类对外接口保持不变。
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import time
from collections import Counter
from typing import Dict, List, Optional

from loguru import logger

# 中文按字切分，英文/数字按词切分；检索用 bigram 组合提高短语命中率
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+")


def tokenize(text: str) -> List[str]:
    """把文本切成检索 token：英文/数字整词，中文 2-gram。"""
    text = (text or "").lower()
    tokens: List[str] = []
    for m in _TOKEN_RE.findall(text):
        if m.isascii():
            tokens.append(m)
        elif len(m) == 1:
            tokens.append(m)
        else:
            tokens.extend(m[i:i + 2] for i in range(len(m) - 1))
    return tokens


class LongTermMemory:
    """长期记忆：按会话存取记忆条目，支持简化 BM25 检索。"""

    def __init__(self, db_path: Optional[str] = None,
                 top_k: int = 5, max_per_talker: int = 200,
                 enabled: bool = True):
        self.enabled = bool(enabled)
        self.top_k = max(1, int(top_k))
        self.max_per_talker = max(10, int(max_per_talker))
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.db_path = db_path or os.path.join(base, "data", "memory.db")
        if self.enabled:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._init_table()

    # -- 存储 ------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self) -> None:
        try:
            conn = self._conn()
            conn.execute(
                """CREATE TABLE IF NOT EXISTS long_term_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    talker TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'conversation',
                    content TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ltm_talker ON long_term_memory(talker)")
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning("长期记忆表初始化失败: {}", exc)

    def add(self, talker: str, content: str, kind: str = "conversation",
            ts: Optional[int] = None) -> bool:
        """追加一条记忆；重复内容跳过；超出上限裁剪最旧。"""
        if not self.enabled or not talker or not content:
            return False
        ts = int(ts) if ts else int(time.time())
        try:
            conn = self._conn()
            dup = conn.execute(
                "SELECT 1 FROM long_term_memory WHERE talker=? AND content=? LIMIT 1",
                (talker, content)).fetchone()
            if dup:
                conn.close()
                return False
            conn.execute(
                "INSERT INTO long_term_memory (talker, kind, content, ts, created_at) "
                "VALUES (?,?,?,?,?)",
                (talker, kind, content, ts, int(time.time())))
            conn.execute(
                """DELETE FROM long_term_memory WHERE talker=? AND id NOT IN (
                       SELECT id FROM long_term_memory WHERE talker=?
                       ORDER BY id DESC LIMIT ?
                   )""",
                (talker, talker, self.max_per_talker))
            conn.commit()
            conn.close()
            return True
        except Exception as exc:
            logger.warning("写入长期记忆失败: {}", exc)
            return False

    # -- 检索 ------------------------------------------------------------

    def search(self, talker: str, query: str,
               top_k: Optional[int] = None) -> List[Dict]:
        """按查询返回该会话最相关的 top_k 条记忆（简化 BM25）。"""
        if not self.enabled or not query:
            return []
        top_k = top_k or self.top_k
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        try:
            conn = self._conn()
            rows = conn.execute(
                "SELECT * FROM long_term_memory WHERE talker=? ORDER BY id DESC LIMIT 500",
                (talker,)).fetchall()
            conn.close()
        except Exception as exc:
            logger.warning("检索长期记忆失败: {}", exc)
            return []
        if not rows:
            return []
        docs = [dict(r) for r in rows]
        df: Counter = Counter()
        doc_tokens: List[Counter] = []
        for d in docs:
            toks = Counter(tokenize(d["content"]))
            df.update(set(toks))
            doc_tokens.append(toks)
        n = len(docs)
        scored = []
        for d, toks in zip(docs, doc_tokens):
            s = 0.0
            for t in set(q_tokens):
                if t in toks:
                    idf = math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0
                    s += toks[t] * idf
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: -x[0])
        return [d for _, d in scored[:top_k]]

    def all(self, talker: str, limit: int = 50) -> List[Dict]:
        """取该会话最近的记忆条目（供定时总结 / 调试使用）。"""
        if not self.enabled:
            return []
        try:
            conn = self._conn()
            rows = conn.execute(
                "SELECT * FROM long_term_memory WHERE talker=? ORDER BY id DESC LIMIT ?",
                (talker, int(limit))).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("读取长期记忆失败: {}", exc)
            return []

    def count(self, talker: str) -> int:
        try:
            conn = self._conn()
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM long_term_memory WHERE talker=?",
                (talker,)).fetchone()
            conn.close()
            return int(row["c"]) if row else 0
        except Exception:
            return 0
