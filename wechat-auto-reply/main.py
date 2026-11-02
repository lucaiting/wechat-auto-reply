# -*- coding: utf-8 -*-
"""wechat-auto-reply 入口。

用法:
    python main.py                    # 正常启动（读 config.yaml）
    python main.py --config xxx.yaml  # 指定配置
    python main.py --doctor           # 先体检（读库/账号/会话/监听解析）再退出
    python main.py --no-webui         # 关闭本地管理面板（默认按 config.webui.enabled）
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from loguru import logger

BASE = os.path.dirname(os.path.abspath(__file__))


def setup_logging() -> None:
    log_dir = os.path.join(BASE, "logs")
    os.makedirs(log_dir, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(
        os.path.join(log_dir, "run_{time:YYYYMMDD}.log"),
        level="DEBUG",
        rotation="10 MB",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
    )


def _idle_loop() -> None:
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出...")


def main() -> int:
    parser = argparse.ArgumentParser(description="微信消息自动读取与回复")
    parser.add_argument("--config", default=os.path.join(BASE, "config.yaml"),
                        help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--doctor", action="store_true", help="环境体检后退出")
    parser.add_argument("--no-webui", action="store_true", help="关闭本地管理面板")
    args = parser.parse_args()

    setup_logging()
    logger.info("=== wechat-auto-reply 启动 ===")

    from core.config import load_config, parse_listen
    cfg = load_config(args.config)

    from core.db_layer import DBHelper

    # 数据库初始化（可能触发密钥提取，微信需已登录）
    try:
        dbh = DBHelper(poll_interval=cfg["wechat"].get("poll_interval", 1.0))
    except Exception as exc:
        logger.error("数据库初始化失败: {}（请确认微信已登录且数据目录可访问）", exc)
        return 1

    targets = parse_listen(cfg)

    if args.doctor:
        _run_doctor(dbh, targets)
        return 0

    # 长期记忆（WebUI 与主流程共用）
    from core.longmem import LongTermMemory
    ltm_cfg = cfg.get("memory", {}).get("long_term", {})
    long_term = None
    if ltm_cfg.get("enabled", True):
        long_term = LongTermMemory(top_k=ltm_cfg.get("top_k", 5),
                                   max_per_talker=ltm_cfg.get("max_per_talker", 200))

    # LLM / 记忆 / 发送 / 决策（无 Key 时不装配；WebUI 面板仍可用）
    llm = None
    brain = None
    scheduler = None
    if cfg["llm"].get("api_key"):
        from core.llm import LLMClient
        from core.memory import Memory
        from core.sender import Sender
        from core.brain import Brain
        llm = LLMClient(cfg)
        memory = Memory(short_window=cfg["memory"].get("short_window", 10),
                        long_term=long_term)
        sender = Sender(cfg)
        brain = Brain(cfg, llm, memory, sender, self_name=dbh.self_name)
        if cfg.get("scheduler", {}).get("enabled", False):
            from core.scheduler import Scheduler
            scheduler = Scheduler(cfg["scheduler"], sender, llm, memory)
    else:
        logger.warning("LLM API Key 未配置：将仅启动 WebUI 管理面板，不监听回复")

    # 系统控制器：面板启停 / 监听热重载 / 账号切换
    from core.system import SystemController
    controller = SystemController(cfg, dbh, targets, brain,
                                  scheduler=scheduler, long_term=long_term)

    # WebUI 管理面板（常驻，独立于系统启停）
    from core.webui import WebUI
    webui = None
    if cfg.get("webui", {}).get("enabled", True) and not args.no_webui:
        webui = WebUI(cfg, dbh, long_term=long_term, scheduler=scheduler,
                      controller=controller)
        if not webui.start():
            webui = None

    if brain is not None:
        # 启动系统（注册监听 + 启动调度器）
        ok, msg = controller.start()
        if not ok:
            logger.error("系统启动失败: {}", msg)
            logger.error("WebUI 管理面板保持运行，可在面板修复后点击「启动系统」")
        else:
            logger.info("LLM: provider={} model={}",
                        cfg["llm"]["provider"], cfg["llm"]["model"])
            logger.info("监听对象 {} 个", len(targets))
    else:
        if webui is None:
            logger.error("LLM API Key 未配置且 WebUI 已关闭：请设置 config.yaml 的 "
                         "llm.api_key 或环境变量 LLM_API_KEY（或启用 webui）")
            return 1
        logger.info("WebUI 管理面板运行中：http://{}:{}/（面板内可填 Key 后点「启动系统」）",
                    cfg["webui"].get("host", "127.0.0.1"), cfg["webui"].get("port", 8790))

    try:
        _idle_loop()
    finally:
        if controller.running:
            controller.stop()
        if webui is not None:
            webui.stop()
    return 0


def _run_doctor(dbh: DBHelper, targets) -> None:
    logger.info("---- 环境体检 ----")
    sessions = dbh.sessions(limit=200)
    logger.info("会话总数: {}", len(sessions))
    logger.info("本机账号: {} ({})", dbh.self_name, dbh.self_wxid)
    for target in targets:
        display = target.get("display", "")
        role = target.get("role", "assistant")
        uname = dbh.resolve_name(display)
        if uname:
            logger.info("监听目标 OK: 「{}」→ {}（角色: {}）", display, uname, role)
        else:
            logger.error("监听目标 FAIL: 「{}」无法解析为会话 username", display)
    logger.info("体检完成。请对照上方日志确认数据库与监听对象解析均正常。")


if __name__ == "__main__":
    sys.exit(main())
