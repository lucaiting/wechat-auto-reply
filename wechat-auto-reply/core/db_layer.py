# -*- coding: utf-8 -*-
"""微信数据访问层：封装 wechatauto-replica 的 WeChatDB / Listener。

职责：
- 初始化本地数据库（自动检测目录、提取密钥）；
- 提供昵称/备注 → 会话 username 的解析（监听与发送都必须用 username）；
- 按监听对象注册数据库增量监听回调（水位自动落盘，重启不重复推送）。
"""
from __future__ import annotations

import sys
from typing import Callable, Dict, List, Optional

from loguru import logger

from wechatauto.db import WeChatDB, Listener

_SELF_SENDER_IDS = (2,)  # 历史判据：sender_id==2 视为本机（私聊场景可靠）


class DBHelper:
    def __init__(self, poll_interval: float = 1.0):
        self.db = WeChatDB()
        self.poll_interval = max(0.2, float(poll_interval))
        self.self_info: Dict = self.db.get_self_info() or {}
        self.self_wxid: str = self.self_info.get("username", "")
        self.self_name: str = self.self_info.get("nick_name") or self.self_wxid
        self._listener: Optional[Listener] = None
        logger.info("数据库初始化成功，本机账号: {} ({})", self.self_name, self.self_wxid)

    # -- 会话解析 --------------------------------------------------------

    def sessions(self, limit: int = 200) -> List[Dict]:
        """当前会话列表（username / unread / summary 等）。"""
        try:
            return self.db.get_sessions(limit=limit)
        except Exception as exc:
            logger.warning("获取会话列表失败: {}", exc)
            return []

    def resolve_name(self, raw: str) -> Optional[str]:
        """把昵称/备注/微信号解析为会话 username；失败返回 None。"""
        raw = (raw or "").strip()
        if not raw:
            return None
        sessions = self.sessions(limit=500)
        by_user = {s.get("username"): s for s in sessions}
        if raw in by_user:
            return raw
        try:
            hits = self.db.search_contact(raw)
            if hits:
                return hits[0].get("username")
        except Exception as exc:
            logger.warning("通讯录搜索失败({}): {}", raw, exc)
        return None

    # -- 发送者判断 ------------------------------------------------------

    def is_sent_by_self(self, msg: Dict) -> bool:
        """判断消息是否由本机发出（防止自回复死循环）。

        优先用 sender_username（1.2.6.1 按分片解析，可靠）；
        解析为空串时回退到 sender_id 历史判据。
        """
        su = (msg.get("sender_username") or "").strip()
        if su:
            return su == self.self_wxid
        sid = msg.get("sender_id")
        return sid in _SELF_SENDER_IDS

    # -- 监听 ------------------------------------------------------------

    def register_listener(self, targets: List[Dict], handler: Callable) -> Dict[str, str]:
        """按结构化目标列表注册监听（目标由 core.config.parse_listen 产生）。

        Args:
            handler: 回调函数，签名 handler(msg: dict, lst: Listener, username: str,
                                           target: dict)。

        Returns:
            {username: 显示名}：成功注册的映射；解析不到的会告警并跳过。
        """
        self._listener = Listener(self.db, interval=self.poll_interval)
        registered: Dict[str, str] = {}
        for target in targets:
            display = str(target.get("display") or "")
            uname = self.resolve_name(display)
            if not uname:
                logger.error("无法把「{}」解析为会话 username，已跳过（请确认昵称与微信显示一致）", display)
                continue
            self._listener.add_listener(
                uname,
                lambda m, lst, _target=target, _uname=uname: handler(m, lst, _uname, _target),
            )
            registered[uname] = display
            logger.info("已注册监听: 「{}」→ {}", display, uname)
        return registered

    def start(self) -> None:
        if self._listener is not None:
            self._listener.start()
            logger.info("监听已启动（数据库轮询间隔 {}s）", self.poll_interval)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            logger.info("监听已停止")
