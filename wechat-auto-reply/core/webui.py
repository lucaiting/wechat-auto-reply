# -*- coding: utf-8 -*-
"""M4 本地 WebUI 管理面板：零依赖（标准库 http.server）。

- 仅绑定 127.0.0.1，浏览器访问 http://127.0.0.1:<port>/
- REST API（JSON）：
    GET  /api/overview             概览：账号/监听解析/LLM/调度/流水计数
    GET  /api/config               当前 config.yaml 文本
    POST /api/config               保存配置（校验通过才写回，自动备份 .bak）
    GET  /api/messages?limit&talker 消息流水
    GET  /api/memory?talker&limit  长期记忆
    GET  /api/logs?lines           当日日志尾部
    GET  /api/scheduler            调度任务 + 触发状态
    POST /api/scheduler/toggle     启停任务（内存立即生效 + 同步写回 config.yaml）
    GET  /api/system               系统运行状态（监听/账号/监听解析）
    POST /api/system/start|stop    启动 / 停止系统（监听 + 调度器）
    POST /api/listen/add|remove    可视化增删监听对象（解析预检 + 热重载 + 写回备份）
    POST /api/account/rebind       重新检测当前微信登录账号（切换测试账号）
    GET  /api/roles                角色卡列表（名称/内容/内置标记）
    POST /api/roles/save|delete    保存/新建、删除角色卡（内置 assistant 禁删）
- 安全：仅本机地址（Host 头校验防 DNS rebinding）；POST 限 JSON 且
  请求体 ≤ 1MB；配置页 api_key 原样展示（仅本机访问可接受）。
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from loguru import logger

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_BODY = 1024 * 1024  # 1MB
PROMPTS_DIR = os.path.join(BASE, "prompts")
ROLE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}$")  # 字母开头 ≤32 位，防路径穿越
BUILTIN_ROLES = {"assistant"}                              # 内置角色：可改不可删


class WebUI:
    """本地管理面板：start() 起后台线程，stop() 关闭。"""

    def __init__(self, config: dict, dbh, long_term=None, scheduler=None,
                 controller=None):
        self.config = config
        self.dbh = dbh
        self.long_term = long_term
        self.scheduler = scheduler
        self.controller = controller   # SystemController（系统启停 / 账号切换 / 热重载）
        w = config.get("webui", {})
        self.host = str(w.get("host") or "127.0.0.1")
        self.port = int(w.get("port") or 8790)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- 生命周期 --------------------------------------------------------

    def start(self) -> bool:
        handler = self._make_handler()
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            logger.error("WebUI 启动失败（{}:{}）: {}", self.host, self.port, exc)
            return False
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True, name="webui")
        self._thread.start()
        logger.info("WebUI 已启动: http://{}:{}/（仅本机）", self.host, self.port)
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            logger.info("WebUI 已关闭")

    def _make_handler(self):
        ui = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "wechat-auto-reply-webui/1.0"
            protocol_version = "HTTP/1.1"

            # -- 基础 ------------------------------------------------------

            def log_message(self, fmt, *args):  # 静默默认访问日志，避免刷屏
                logger.debug("WebUI %s", fmt % args)

            def _host_ok(self) -> bool:
                host = (self.headers.get("Host") or "").lower().strip()
                return host.startswith("127.0.0.1") or host.startswith("localhost")

            def _json(self, data: Any, status: int = 200) -> None:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self) -> Optional[dict]:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return None
                if n <= 0 or n > MAX_BODY:
                    return None
                raw = self.rfile.read(n)
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    return None

            # -- 路由 ------------------------------------------------------

            def do_GET(self) -> None:
                if not self._host_ok():
                    self._json({"ok": False, "error": "非法 Host"}, 403)
                    return
                path = self.path.split("?", 1)[0]
                if path == "/" or path == "/index.html":
                    self._serve_index()
                elif path == "/api/overview":
                    self._json(ui.overview())
                elif path == "/api/config":
                    self._json({"yaml": ui.config_yaml()})
                elif path == "/api/messages":
                    self._json(ui.messages(self._query_param("talker"),
                                           self._int_param("limit", 50)))
                elif path == "/api/memory":
                    self._json(ui.memory(self._query_param("talker"),
                                         self._int_param("limit", 50)))
                elif path == "/api/logs":
                    self._json({"logs": ui.logs(self._int_param("lines", 200))})
                elif path == "/api/scheduler":
                    self._json(ui.scheduler_info())
                elif path == "/api/system":
                    self._json(ui.system_info())
                elif path == "/api/roles":
                    self._json({"roles": ui.roles_list()})
                else:
                    self._json({"ok": False, "error": "未找到接口"}, 404)

            def do_POST(self) -> None:
                if not self._host_ok():
                    self._json({"ok": False, "error": "非法 Host"}, 403)
                    return
                path = self.path.split("?", 1)[0]
                if path == "/api/config":
                    body = self._read_body()
                    if not body or not isinstance(body.get("yaml"), str):
                        self._json({"ok": False, "error": "请求体须为 {yaml: 文本}"}, 400)
                        return
                    ok, msg = ui.save_config(body["yaml"])
                    self._json({"ok": ok, "error": None if ok else msg})
                elif path == "/api/scheduler/toggle":
                    body = self._read_body()
                    if not body or not isinstance(body.get("name"), str):
                        self._json({"ok": False, "error": "请求体须为 {name, enabled}"}, 400)
                        return
                    ok, msg = ui.toggle_task(body["name"], bool(body.get("enabled", True)))
                    self._json({"ok": ok, "error": None if ok else msg})
                elif path == "/api/system/start":
                    ok, msg = ui.system_start()
                    self._json({"ok": ok, "error": None if ok else msg})
                elif path == "/api/system/stop":
                    ok, msg = ui.system_stop()
                    self._json({"ok": ok, "error": None if ok else msg})
                elif path == "/api/listen/add":
                    body = self._read_body()
                    if not body or not isinstance(body.get("name"), str):
                        self._json({"ok": False, "error": "请求体须为 {name, role?, enabled?, at_only?}"}, 400)
                        return
                    ok, msg = ui.listen_add(
                        body["name"],
                        role=str(body.get("role") or "assistant"),
                        enabled=bool(body.get("enabled", True)),
                        at_only=bool(body.get("at_only", False)))
                    self._json({"ok": ok, "error": None if ok else msg})
                elif path == "/api/listen/remove":
                    body = self._read_body()
                    if not body or not isinstance(body.get("name"), str):
                        self._json({"ok": False, "error": "请求体须为 {name}"}, 400)
                        return
                    ok, msg = ui.listen_remove(body["name"])
                    self._json({"ok": ok, "error": None if ok else msg})
                elif path == "/api/account/rebind":
                    ok, msg = ui.account_rebind()
                    self._json({"ok": ok, "error": None if ok else msg})
                elif path == "/api/roles/save":
                    body = self._read_body()
                    if not body or not isinstance(body.get("name"), str) or \
                            not isinstance(body.get("content"), str):
                        self._json({"ok": False, "error": "请求体须为 {name, content}"}, 400)
                        return
                    ok, msg = ui.role_save(body["name"], body["content"])
                    self._json({"ok": ok, "error": None if ok else msg})
                elif path == "/api/roles/delete":
                    body = self._read_body()
                    if not body or not isinstance(body.get("name"), str):
                        self._json({"ok": False, "error": "请求体须为 {name}"}, 400)
                        return
                    ok, msg = ui.role_delete(body["name"])
                    self._json({"ok": ok, "error": None if ok else msg})
                else:
                    self._json({"ok": False, "error": "未找到接口"}, 404)

            def _serve_index(self) -> None:
                path = os.path.join(BASE, "webui", "index.html")
                if not os.path.exists(path):
                    self._json({"ok": False, "error": "webui/index.html 缺失"}, 404)
                    return
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        html = f.read().encode("utf-8")
                except OSError:
                    self._json({"ok": False, "error": "读取页面失败"}, 500)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)

            def _query_param(self, name: str) -> str:
                q = self.path.split("?", 1)
                if len(q) < 2:
                    return ""
                from urllib.parse import parse_qs, unquote
                vals = parse_qs(q[1]).get(name, [])
                return unquote(vals[0]).strip() if vals else ""

            def _int_param(self, name: str, default: int) -> int:
                try:
                    return max(1, min(int(self._query_param(name) or default), 500))
                except ValueError:
                    return default

        return Handler

    # -- API 数据 --------------------------------------------------------

    def overview(self) -> Dict[str, Any]:
        cfg = self.config
        llm = cfg.get("llm", {})
        controller = self.controller
        if controller is not None:
            st = controller.status()
            account, targets = st["account"], st["targets"]
            system_running = bool(st.get("running"))
        else:
            account = {"name": self.dbh.self_name, "wxid": self.dbh.self_wxid}
            targets = []
            for t in cfg.get("listen", []) or []:
                display = str(t.get("display") or t.get("name") or "")
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
            system_running = False
        return {
            "account": account,
            "system": {"running": system_running},
            "llm": {"provider": llm.get("provider"), "model": llm.get("model"),
                    "has_key": bool(llm.get("api_key"))},
            "targets": targets,
            "scheduler": {"enabled": bool(cfg.get("scheduler", {}).get("enabled")),
                          "count": len((cfg.get("scheduler", {}) or {}).get("tasks", []) or [])},
            "message_count": self._count("chat.db", "chat_messages"),
            "memory_count": self._count("memory.db", "long_term_memory"),
        }

    def config_yaml(self) -> str:
        path = os.path.join(BASE, "config.yaml")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def save_config(self, text: str) -> tuple:
        """校验并保存 config.yaml；成功先备份 .bak。"""
        import tempfile
        import yaml
        from .config import load_config
        if not text.strip():
            return False, "配置为空"
        # 1) YAML 语法校验
        try:
            raw = yaml.safe_load(text)
        except Exception as exc:
            return False, f"YAML 解析失败: {exc}"
        if not isinstance(raw, dict):
            return False, "配置必须是 YAML 顶层映射"
        # 2) 语义校验（合并默认值 / provider 合法性）
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        try:
            tmp.write(text)
            tmp.close()
            load_config(tmp.name)
        except Exception as exc:
            return False, f"配置校验失败: {exc}"
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        # 3) 备份 + 写回
        path = os.path.join(BASE, "config.yaml")
        try:
            if os.path.exists(path):
                import shutil
                shutil.copy2(path, path + ".bak")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as exc:
            return False, f"写入失败: {exc}"
        logger.info("WebUI 已保存配置（备份于 config.yaml.bak）")
        # 明文 Key 提示（防回潮）：检测 sk- 明文且无加密字段
        import re as _re
        plain_key = _re.search(r'^\s*api_key:\s*"?\s*(sk-[^"\s#]+)', text, _re.M)
        has_enc = _re.search(r'^\s*api_key_encrypted:', text, _re.M)
        msg = "已保存，重启 main.py 生效"
        if plain_key and not has_enc:
            msg += "；注意：api_key 为明文存储，建议运行 scripts\\encrypt_key.py 加密"
        return True, msg

    # -- 角色卡管理 ------------------------------------------------------

    def roles_list(self) -> list:
        """列出 prompts 目录下所有角色卡（含内容全文，前端直接编辑）。"""
        out = []
        if not os.path.isdir(PROMPTS_DIR):
            return out
        for fn in sorted(os.listdir(PROMPTS_DIR)):
            if not fn.endswith(".md"):
                continue
            name = fn[:-3]
            if not ROLE_NAME_RE.fullmatch(name):
                continue
            try:
                with open(os.path.join(PROMPTS_DIR, fn), "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue
            out.append({"name": name, "content": content,
                        "builtin": name in BUILTIN_ROLES, "size": len(content)})
        return out

    def role_save(self, name: str, content: str) -> tuple:
        """新建或保存角色卡；name 安全校验，content 非空且有大小上限。"""
        if not name or not ROLE_NAME_RE.fullmatch(name):
            return False, "角色名须为字母开头，仅字母/数字/下划线，≤32 字符"
        if not content or not content.strip():
            return False, "角色内容不能为空"
        if len(content) > 20 * 1024:
            return False, "角色内容过长（上限 20KB）"
        path = os.path.join(PROMPTS_DIR, name + ".md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            return False, f"写入失败: {exc}"
        logger.info("WebUI 已保存角色卡 {}", name)
        return True, f"已保存角色「{name}」，重启 main.py 生效"

    def role_delete(self, name: str) -> tuple:
        """删除角色卡；内置角色 assistant 禁止删除。"""
        if name in BUILTIN_ROLES:
            return False, "内置角色「assistant」是默认兜底角色，禁止删除"
        if not name or not ROLE_NAME_RE.fullmatch(name):
            return False, "非法角色名"
        path = os.path.join(PROMPTS_DIR, name + ".md")
        if not os.path.isfile(path):
            return False, f"角色「{name}」不存在"
        try:
            os.remove(path)
        except OSError as exc:
            return False, f"删除失败: {exc}"
        logger.info("WebUI 已删除角色卡 {}", name)
        return True, f"已删除角色「{name}」"

    def messages(self, talker: str = "", limit: int = 50) -> Dict[str, Any]:
        path = os.path.join(BASE, "data", "chat.db")
        if not os.path.exists(path):
            return {"rows": []}
        import sqlite3
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            if talker:
                rows = conn.execute(
                    "SELECT * FROM chat_messages WHERE talker=? ORDER BY id DESC LIMIT ?",
                    (talker, int(limit))).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?",
                    (int(limit),)).fetchall()
            conn.close()
            out = []
            for r in rows:
                d = dict(r)
                d["msg_time"] = datetime.fromtimestamp(int(d.get("msg_time") or 0)).strftime(
                    "%Y-%m-%d %H:%M:%S")
                d["created_at"] = datetime.fromtimestamp(int(d.get("created_at") or 0)).strftime(
                    "%Y-%m-%d %H:%M:%S")
                out.append(d)
            return {"rows": out}
        except Exception as exc:
            return {"rows": [], "error": str(exc)}

    def memory(self, talker: str = "", limit: int = 50) -> Dict[str, Any]:
        if self.long_term is None or not talker:
            return {"rows": [], "hint": "需指定会话 username（见流水/概览）"}
        rows = self.long_term.all(talker=talker, limit=limit)
        out = []
        for r in rows:
            d = dict(r)
            d["ts"] = datetime.fromtimestamp(int(d.get("ts") or 0)).strftime("%Y-%m-%d %H:%M:%S")
            out.append(d)
        return {"rows": out}

    def logs(self, lines: int = 200) -> str:
        path = os.path.join(BASE, "logs", f"run_{datetime.now().strftime('%Y%m%d')}.log")
        if not os.path.exists(path):
            return "(今日暂无日志)"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return "".join(deque(f, maxlen=int(lines)))
        except OSError as exc:
            return f"(读取日志失败: {exc})"

    def scheduler_info(self) -> Dict[str, Any]:
        scfg = self.config.get("scheduler", {}) or {}
        tasks = []
        if self.scheduler is not None:
            for t in self.scheduler.tasks:
                trigger = (f"每天 {t['time']}" if t.get("time") else
                           f"cron {t['cron']}" if t.get("cron") else
                           f"一次性 {t['once']}")
                tasks.append({
                    "name": t["name"], "to": t["to"], "enabled": bool(t.get("enabled", True)),
                    "trigger": trigger,
                    "content": str(t.get("content"))[:80],
                    "state": self.scheduler._state.get(t["name"], {}),
                })
        return {"enabled": bool(scfg.get("enabled")), "tasks": tasks}

    def toggle_task(self, name: str, enabled: bool) -> tuple:
        """启停调度任务：内存立即生效 + 同步写回 config.yaml。"""
        if self.scheduler is None:
            return False, "调度器未启动"
        if not self.scheduler.set_enabled(name, enabled):
            return False, f"找不到任务「{name}」"
        # 同步写回 config.yaml（yaml.dump 重写会重排格式、丢失注释）
        import yaml
        path = os.path.join(BASE, "config.yaml")
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            for t in (cfg.get("scheduler", {}) or {}).get("tasks", []) or []:
                if isinstance(t, dict) and t.get("name") == name:
                    t["enabled"] = enabled
            if os.path.exists(path):
                import shutil
                shutil.copy2(path, path + ".bak")
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        except OSError as exc:
            return False, f"已生效但写回配置失败: {exc}"
        logger.info("WebUI 启停任务「{}」→ {}", name, enabled)
        return True, "已生效并写回 config.yaml（格式已重排）"

    # -- 系统启停（M5） --------------------------------------------------

    def system_info(self) -> Dict[str, Any]:
        if self.controller is None:
            return {"ok": False, "running": False, "error": "控制器未装配"}
        return {"ok": True, **self.controller.status()}

    def system_start(self) -> tuple:
        if self.controller is None:
            return False, "控制器未装配"
        return self.controller.start()

    def system_stop(self) -> tuple:
        if self.controller is None:
            return False, "控制器未装配"
        return self.controller.stop()

    def account_rebind(self) -> tuple:
        """微信切换账号后重新检测本机账号（WebUI 一键跟随，无需重启）。"""
        if self.controller is None:
            return False, "控制器未装配"
        return self.controller.rebind_account()

    # -- 监听对象可视化增删（M5） ---------------------------------------

    @staticmethod
    def _listen_name(t) -> str:
        """从 listen 条目的三种写法中提取显示名。"""
        if isinstance(t, dict):
            return str(t.get("name") or "")
        if isinstance(t, list) and t:
            return str(t[0])
        return str(t)

    def listen_add(self, name: str, role: str = "assistant",
                   enabled: bool = True, at_only: bool = False) -> tuple:
        """添加监听对象：解析预检 → 去重 → 写盘备份 → 热重载。"""
        name = (name or "").strip()
        if not name:
            return False, "微信名不能为空"
        try:
            uname = self.dbh.resolve_name(name)
        except Exception as exc:
            return False, f"解析异常：{exc}"
        if not uname:
            return False, f"「{name}」无法解析为真实会话（请核对微信显示名）"
        import yaml
        path = os.path.join(BASE, "config.yaml")
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except OSError as exc:
            return False, f"读取配置失败：{exc}"
        listens = cfg.get("listen") or []
        for t in listens:
            if self._listen_name(t) == name:
                return False, f"「{name}」已在监听列表中"
        listens.append({"name": name, "role": role or "assistant",
                        "enabled": bool(enabled), "at_only": bool(at_only),
                        "quiet_periods": []})
        cfg["listen"] = listens
        try:
            if os.path.exists(path):
                import shutil
                shutil.copy2(path, path + ".bak")
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        except OSError as exc:
            return False, f"写入失败：{exc}"
        self.config["listen"] = listens
        logger.info("WebUI 已添加监听对象「{}」→ {}", name, uname)
        if self.controller is not None:
            ok, msg = self.controller.reload_targets()
            if not ok:
                return False, msg
        return True, f"已添加「{name}」（解析为 {uname}）并写回配置"

    def listen_remove(self, name: str) -> tuple:
        """移除监听对象：定位 → 删除 → 写盘备份 → 热重载。"""
        name = (name or "").strip()
        if not name:
            return False, "微信名不能为空"
        import yaml
        path = os.path.join(BASE, "config.yaml")
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except OSError as exc:
            return False, f"读取配置失败：{exc}"
        listens = cfg.get("listen") or []
        kept, removed = [], False
        for t in listens:
            if self._listen_name(t) == name:
                removed = True
            else:
                kept.append(t)
        if not removed:
            return False, f"「{name}」不在监听列表中"
        cfg["listen"] = kept
        try:
            if os.path.exists(path):
                import shutil
                shutil.copy2(path, path + ".bak")
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        except OSError as exc:
            return False, f"写入失败：{exc}"
        self.config["listen"] = kept
        logger.info("WebUI 已移除监听对象「{}」", name)
        if self.controller is not None:
            ok, msg = self.controller.reload_targets()
            if not ok:
                return False, msg
        return True, f"已移除「{name}」并写回配置"

    def _count(self, db_file: str, table: str) -> int:
        path = os.path.join(BASE, "data", db_file)
        if not os.path.exists(path):
            return 0
        import sqlite3
        try:
            conn = sqlite3.connect(path)
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.close()
            return int(n)
        except Exception:
            return 0
