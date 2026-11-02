# -*- coding: utf-8 -*-
"""定时任务（M2）：每天固定时间 / 简化 cron / 一次性任务。

- 配置见 config.yaml 的 scheduler.tasks（示例见 config.example.yaml）；
- 触发方式三选一：
    time: "HH:MM"                每天固定时间；
    cron: "分 时 日 月 周"        标准 5 字段（支持 * / - 和逗号）；
    once: "YYYY-MM-DD HH:MM"     一次性，触发后不再执行；
- content 为静态文本；以 "$llm:" 开头则交给 LLM 生成（支持 {memory}
  占位符注入该会话近期长期记忆）；
- {date} / {time} 占位符：替换为触发当天的日期与时间；
- 状态写入 data/scheduler_state.json，避免重启后同一天/同一分钟重复触发。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Callable, Dict, List, Optional

from loguru import logger


def _match_field(expr: str, value: int, lo: int, hi: int) -> bool:
    """匹配 cron 单字段：支持 * / */n a-b a-b/n n 和逗号组合。"""
    for part in str(expr).split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            return True
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                continue
            if base == "*":
                start, end = lo, hi
            elif "-" in base:
                s, e = base.split("-", 1)
                try:
                    start, end = int(s), int(e)
                except ValueError:
                    continue
            else:
                try:
                    start = int(base)
                except ValueError:
                    continue
                end = hi
            if start <= value <= end and (value - start) % step == 0:
                return True
        elif "-" in part:
            s, e = part.split("-", 1)
            try:
                if int(s) <= value <= int(e):
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue
    return False


def match_cron(expr: str, dt: datetime) -> bool:
    """匹配 5 字段 cron 表达式；周字段采用 cron 语义（0/7=周日，1=周一）。"""
    parts = str(expr).strip().split()
    if len(parts) != 5:
        return False
    fields = [dt.minute, dt.hour, dt.day, dt.month, (dt.weekday() + 1) % 7]
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    return all(_match_field(p, v, lo, hi)
               for p, v, (lo, hi) in zip(parts, fields, bounds))


class Scheduler:
    """轻量调度器：后台线程每 20s 轮询一次，匹配到任务即发送。"""

    def __init__(self, config: dict, sender, llm=None, memory=None):
        self.config = config or {}
        self.sender = sender
        self.llm = llm
        self.memory = memory
        self.tasks: List[Dict] = []
        self._resolve: Optional[Callable[[str], Optional[str]]] = None
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._state_path = os.path.join(base, "data", "scheduler_state.json")
        self._state: Dict[str, Dict] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._load_tasks()
        self._load_state()

    # -- 配置解析 --------------------------------------------------------

    def _load_tasks(self) -> None:
        for t in self.config.get("tasks", []) or []:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "").strip()
            to = str(t.get("to") or "").strip()
            content = t.get("content")
            if not name or not to or content is None:
                logger.warning("跳过无效定时任务：name/to/content 必填（{}）", t)
                continue
            task = {
                "name": name,
                "to": to,
                "content": content,
                "enabled": bool(t.get("enabled", True)),
                "time": str(t.get("time") or "").strip(),
                "cron": str(t.get("cron") or "").strip(),
                "once": str(t.get("once") or "").strip(),
            }
            has_time = bool(task["time"])
            has_cron = bool(task["cron"])
            has_once = bool(task["once"])
            if sum([has_time, has_cron, has_once]) != 1:
                logger.warning("跳过定时任务「{}」：time/cron/once 必须且只能填一个", name)
                continue
            self.tasks.append(task)

    def _load_state(self) -> None:
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                self._state = json.load(f)
        except (OSError, ValueError):
            self._state = {}

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning("定时任务状态保存失败: {}", exc)

    # -- 生命周期 --------------------------------------------------------

    def start(self, resolve: Callable[[str], Optional[str]]) -> None:
        """启动后台线程；resolve 把显示名解析为会话 username（dbh.resolve_name）。"""
        if not self.tasks:
            logger.info("没有定时任务，调度器不启动")
            return
        self._resolve = resolve
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="scheduler")
        self._thread.start()
        logger.info("调度器启动，{} 个定时任务", len(self.tasks))

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick(datetime.now())
            except Exception as exc:
                logger.warning("定时任务轮询异常: {}", exc)
            self._stop.wait(20)

    # -- 触发 ------------------------------------------------------------

    def _tick(self, now: datetime) -> None:
        fired = False
        for task in self.tasks:
            if not task.get("enabled", True):
                continue
            if self._due(task, now):
                self._fire(task, now)
                fired = True
        if fired:
            self._save_state()

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """按名称启停任务（内存中立即生效）；找不到返回 False。"""
        for task in self.tasks:
            if task["name"] == name:
                task["enabled"] = bool(enabled)
                logger.info("定时任务「{}」已{}", name, "启用" if enabled else "停用")
                return True
        return False

    def _due(self, task: Dict, now: datetime) -> bool:
        state = self._state.get(task["name"], {})
        minute_str = now.strftime("%Y-%m-%d %H:%M")
        date_str = now.strftime("%Y-%m-%d")
        if task["once"]:
            if state.get("fired"):
                return False
            try:
                target = datetime.strptime(task["once"], "%Y-%m-%d %H:%M")
            except ValueError:
                return False
            return now >= target
        if task["time"]:
            # 每天任务：同日已触发则跳过
            if state.get("date") == date_str:
                return False
            return now.strftime("%H:%M") == task["time"][:5]
        if task["cron"]:
            # cron 任务：同一分钟只触发一次
            if state.get("last") == minute_str:
                return False
            return match_cron(task["cron"], now)
        return False

    def _fire(self, task: Dict, now: datetime) -> bool:
        username = None
        if self._resolve is not None:
            username = self._resolve(task["to"])
        if not username:
            logger.warning("定时任务「{}」目标「{}」无法解析为会话，跳过",
                           task["name"], task["to"])
            return False

        content = self._render_content(task, username, now)
        if not content:
            logger.warning("定时任务「{}」内容为空，跳过", task["name"])
            return False
        if len(content) > 500:
            content = content[:500]

        ok = self.sender.send_text(username, content)
        logger.info("定时任务「{}」→ {} 发送: {} ({})",
                    task["name"], task["to"], "成功" if ok else "失败", content[:40])
        # 更新触发状态
        state = self._state.setdefault(task["name"], {})
        if task["once"]:
            state["fired"] = True
        elif task["time"]:
            state["date"] = now.strftime("%Y-%m-%d")
        elif task["cron"]:
            state["last"] = now.strftime("%Y-%m-%d %H:%M")
        return True

    def _render_content(self, task: Dict, username: str, now: datetime) -> str:
        content = task["content"]
        text = str(content)
        if text.startswith("$llm:"):
            if self.llm is None:
                logger.warning("定时任务「{}」需要 LLM 生成但未配置 LLM", task["name"])
                return ""
            prompt = text[len("$llm:"):].strip()
            prompt = prompt.replace("{memory}", self._memory_summary(username))
            messages = [{"role": "user", "content": prompt}]
            reply = self.llm.chat(messages)
            if not reply:
                return ""
            return reply.strip()
        text = (text.replace("{date}", now.strftime("%Y-%m-%d"))
                    .replace("{time}", now.strftime("%H:%M")))
        return text.strip()

    def _memory_summary(self, username: str) -> str:
        """取该会话最近的长期记忆，拼成给 LLM 的上下文。"""
        if self.memory is None or getattr(self.memory, "long_term", None) is None:
            return ""
        items = self.memory.long_term.all(talker=username, limit=30)
        if not items:
            return ""
        lines = "\n".join(f"- {it['content']}" for it in items)
        return f"近期与用户的对话记忆：\n{lines}"
