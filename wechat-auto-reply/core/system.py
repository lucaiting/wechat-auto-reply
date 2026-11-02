# -*- coding: utf-8 -*-
"""系统控制器：统一管理监听/调度器的启停与账号重建（WebUI 面板操作入口）。

职责：
- start / stop：启动/停止监听（Listener）与调度器，供面板"系统启动/结束"按钮调用；
- rebind_account：微信切换账号后重新检测本机账号并替换数据库连接、重注册监听；
- reload_targets：listen 配置变化后热重载监听（无需重启 main.py）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from loguru import logger


class SystemController:
    def __init__(self, config: dict, dbh, targets: list, brain, scheduler=None,
                 long_term=None, dbh_factory=None):
        self.config = config
        self.dbh = dbh                 # 可变：rebind 时替换为新账号的 DBHelper
        self.targets = targets         # parse_listen 结果（config 变化后由 reload_targets 刷新）
        self.brain = brain
        self.scheduler = scheduler
        self.long_term = long_term
        self.running: bool = False     # 监听是否运行中（不持久化，重启回到默认）
        self._dbh_factory = dbh_factory  # 测试可注入；默认 DBHelper

    # -- 状态 ------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        targets = []
        for t in self.targets:
            display = str(t.get("display") or "")
            uname = None
            try:
                uname = self.dbh.resolve_name(display) if display else None
            except Exception:
                uname = None
            targets.append({
                "display": display,
                "role": t.get("role", "assistant"),
                "enabled": bool(t.get("enabled", True)),
                "at_only": bool(t.get("at_only", False)),
                "resolved": uname or None,
            })
        return {
            "running": self.running,
            "account": {"name": self.dbh.self_name, "wxid": self.dbh.self_wxid},
            "targets": targets,
            "scheduler": self.scheduler is not None,
        }

    # -- 启停 ------------------------------------------------------------

    def start(self) -> Tuple[bool, str]:
        """启动监听与调度器（每次重建 Listener，保证 stop 后可再次 start）。"""
        if self.running:
            return False, "系统已在运行"
        if not self.config.get("llm", {}).get("api_key"):
            return False, "未配置 LLM API Key（请先在配置页填写）"
        if not self.targets:
            return False, "listen 为空：请先添加监听对象"
        registered = self.dbh.register_listener(self.targets, self._build_handler())
        if not registered:
            return False, "没有可用的监听对象（全部解析失败，请核对微信显示名或切换账号）"
        self.dbh.start()
        if self.scheduler is not None:
            try:
                self.scheduler.start(self.dbh.resolve_name)
            except Exception as exc:
                logger.warning("调度器启动失败（不影响监听）: {}", exc)
        self.running = True
        logger.info("系统已启动：监听 {} 个对象", len(registered))
        return True, f"系统已启动（监听 {len(registered)} 个对象）"

    def stop(self) -> Tuple[bool, str]:
        """停止监听与调度器（WebUI 面板保持可用）。"""
        if not self.running:
            return False, "系统未在运行"
        if self.scheduler is not None:
            try:
                self.scheduler.stop()
            except Exception as exc:
                logger.warning("调度器停止异常: {}", exc)
        self.dbh.stop()
        self.running = False
        logger.info("系统已停止（监听与调度器均已停止）")
        return True, "系统已停止（监听与调度器均已停止）"

    # -- 配置热重载 ------------------------------------------------------

    def reload_targets(self) -> Tuple[bool, str]:
        """listen 配置写盘后热重载：停止 → 重新解析 → 恢复运行。"""
        was_running = self.running
        if was_running:
            self.stop()
        from core.config import parse_listen
        self.targets = parse_listen(self.config)
        if not was_running:
            return True, "配置已更新（系统未运行，下次启动生效）"
        ok, msg = self.start()
        if not ok:
            return False, f"配置已写回，但重载启动失败：{msg}"
        return True, "配置已更新并热重载生效"

    # -- 账号切换 --------------------------------------------------------

    def rebind_account(self) -> Tuple[bool, str]:
        """重新检测当前微信登录账号并替换数据库连接（微信登录页切号后调用）。

        流程：若运行中先停止 → 新建 DBHelper（检测新账号目录/密钥）→
        替换引用 → 更新 self_name（@提及判定）→ 按原运行状态恢复。
        """
        was_running = self.running
        if was_running:
            self.stop()

        from core.db_layer import DBHelper
        factory = self._dbh_factory or DBHelper
        poll = float(self.config.get("wechat", {}).get("poll_interval", 1.0))
        try:
            new_dbh = factory(poll_interval=poll)
        except Exception as exc:
            logger.error("重新检测账号失败: {}", exc)
            return False, f"重新检测失败：{exc}（请先在微信登录页切换账号并扫码登录）"

        old_name = self.dbh.self_name
        self.dbh = new_dbh
        if self.brain is not None:
            self.brain.self_name = new_dbh.self_name
        logger.info("账号已切换: {} → {}", old_name, new_dbh.self_name)

        if was_running:
            ok, msg = self.start()
            if not ok:
                return False, f"已切换到账号 {new_dbh.self_name}，但启动失败：{msg}"
            return True, f"已切换到账号 {new_dbh.self_name}（{new_dbh.self_wxid}），系统已恢复运行"

        return True, f"已切换到账号 {new_dbh.self_name}（{new_dbh.self_wxid}），系统未运行"

    # -- 内部 ------------------------------------------------------------

    def _build_handler(self):
        """生成 Listener 回调；闭包绑定当前 dbh（rebind 后重建）。"""
        dbh = self.dbh
        brain = self.brain

        def on_msg(msg: dict, lst, username: str, target: dict) -> None:
            try:
                msg["is_self"] = dbh.is_sent_by_self(msg)
                brain.handle(msg, target, username)
            except Exception as exc:
                logger.exception("处理消息异常: {}", exc)

        return on_msg
