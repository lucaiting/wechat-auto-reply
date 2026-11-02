# -*- coding: utf-8 -*-
"""环境体检脚本：一条命令检查整条链路是否就绪。

用法:
    .venv\\Scripts\\python scripts\\doctor.py [--config config.yaml]

检查项：
1. 微信进程是否在运行（Weixin.exe / WeChat.exe）
2. 本地数据库能否初始化（自动检测目录 + 提取密钥）
3. 本机账号信息
4. 会话清单（前 20 个）
5. config 中监听对象能否解析为会话 username

先跑体检，再启动 main.py，可以省掉大部分启动排障时间。
"""
from __future__ import annotations

import argparse
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)


def check_wechat_process() -> bool:
    try:
        import psutil
    except ImportError:
        print("[SKIP] psutil 未安装，跳过进程检查")
        return True
    try:
        names = set()
        for p in psutil.process_iter():
            try:
                names.add(p.name().lower())
            except Exception:
                continue  # 无权限读取单个进程名时跳过
        found = [n for n in ("weixin.exe", "wechat.exe") if n in names]
    except Exception as exc:
        print(f"[WARN] 进程检查异常（{exc}），跳过该项")
        return True
    if found:
        print(f"[OK] 微信进程在运行: {found}")
        return True
    print("[FAIL] 未检测到微信进程（Weixin.exe / WeChat.exe），请先登录微信")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="wechat-auto-reply 环境体检")
    parser.add_argument("--config", default=os.path.join(BASE, "config.yaml"), help="配置文件路径")
    args = parser.parse_args()

    print("==== wechat-auto-reply 环境体检 ====")
    ok = check_wechat_process()

    # 配置
    from core.config import load_config, parse_listen

    if os.path.exists(args.config):
        cfg = load_config(args.config)
        targets = parse_listen(cfg)
        print(f"[OK] 配置加载成功: {args.config}，监听 {len(targets)} 个对象")
        if not cfg["llm"]["api_key"]:
            print("[WARN] LLM API Key 未配置（config.yaml 或环境变量 LLM_API_KEY），启动 main 前必须配置")
    else:
        print(f"[FAIL] 配置文件不存在: {args.config}（可从 config.example.yaml 复制）")
        return 1

    # 数据库
    try:
        from core.db_layer import DBHelper

        dbh = DBHelper(poll_interval=1.0)
        ok = True
        print(f"[OK] 数据库初始化成功，本机账号: {dbh.self_name} ({dbh.self_wxid})")
    except Exception as exc:
        print(f"[FAIL] 数据库初始化失败: {exc}")
        print("       可能原因：微信未登录 / 数据目录不可访问 / 密钥提取失败")
        return 1

    # 会话清单
    sessions = dbh.sessions(limit=20)
    print(f"[OK] 会话总数(前{len(sessions)}个):")
    for s in sessions[:20]:
        uname = s.get("username", "")
        summary = (s.get("summary") or "")[:24]
        print(f"      {uname:<28} {summary}")

    # 监听对象解析
    for display, role in targets:
        uname = dbh.resolve_name(display)
        if uname:
            print(f"[OK] 监听目标「{display}」→ {uname}（角色 {role}）")
        else:
            print(f"[FAIL] 监听目标「{display}」无法解析为会话 username，请检查昵称与微信显示是否一致")
            ok = False

    print("==== 体检结束 ====")
    if ok:
        print("结论：链路基本就绪，可配置 LLM Key 后运行 python main.py")
    else:
        print("结论：存在 FAIL 项，请按上方提示修复后重试")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
