# -*- coding: utf-8 -*-
"""发送器：封装 WeChat().SendMsg，带节流 / 重试 / 连续失败自动暂停。

注意：发送会驱动微信窗口（坐标+OCR/UIA 混合），需要微信客户端
已登录且窗口可操作；who 必须传会话 username（非昵称）。
"""
from __future__ import annotations

import random
import threading
import time
from typing import Optional

from loguru import logger


class Sender:
    def __init__(self, config: dict):
        s = config.get("sender", {})
        self.min_interval = float(s.get("min_interval", 3.0))
        self.max_retry = int(s.get("max_retry", 2))
        self.human_typing = bool(s.get("human_typing", True))
        self.max_consecutive_fail = int(s.get("max_consecutive_fail", 3))
        self._wx = None
        self._lock = threading.Lock()
        self._last_sent = 0.0
        self._consecutive_fail = 0
        self.paused = False

    def _get_wx(self):
        if self._wx is None:
            from wechatauto import WeChat

            self._wx = WeChat()
        return self._wx

    def send_text(self, username: str, text: str) -> bool:
        """发送文本消息；成功返回 True。失败重试，连续失败达到阈值自动暂停。"""
        if self.paused:
            logger.warning("发送器已暂停（连续失败保护），本次不发送: {} -> {}", username, text[:20])
            return False
        text = (text or "").strip()
        if not text:
            logger.warning("空回复，不发送")
            return False

        # 节流：距离上次发送不足 min_interval 则等待
        with self._lock:
            wait = self._last_sent + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retry + 1):
            try:
                resp = self._get_wx().SendMsg(text, who=username)
                if resp and resp.is_success:
                    with self._lock:
                        self._last_sent = time.time()
                    self._consecutive_fail = 0
                    logger.info("已发送 -> {}: {}", username, text[:40])
                    return True
                last_err = RuntimeError(f"发送未成功: {resp}")
                logger.warning("发送失败（第 {} 次）-> {}: {}", attempt + 1, username, resp)
            except Exception as exc:
                last_err = exc
                logger.warning("发送异常（第 {} 次）-> {}: {}", attempt + 1, username, exc)
            if attempt < self.max_retry:
                time.sleep(2.0 * (attempt + 1))
                if self.human_typing:
                    time.sleep(random.uniform(1.0, 3.0))

        self._consecutive_fail += 1
        if self._consecutive_fail >= self.max_consecutive_fail:
            self.paused = True
            logger.error("连续 {} 次发送失败，发送器已自动暂停（需手动重启恢复）", self._consecutive_fail)
        logger.error("回复最终失败 -> {}（{}）", username, last_err)
        return False
