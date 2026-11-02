# -*- coding: utf-8 -*-
"""M2 逻辑自测：长期记忆检索 / cron 匹配 / 记忆注入 / 调度触发。

不依赖微信与 LLM，可在任意时刻运行：
    .\\.venv\\Scripts\\python.exe scripts\\selftest.py
全部通过输出: ALL SELFTESTS PASSED
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.longmem import LongTermMemory, tokenize          # noqa: E402
from core.memory import Memory                              # noqa: E402
from core.scheduler import Scheduler, match_cron            # noqa: E402
from core.config import load_config, parse_listen, in_quiet_time, period_to_minutes  # noqa: E402

PASS = []


def check(name: str, cond: bool) -> None:
    PASS.append(cond)
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"自测失败: {name}")


# 1) tokenize -----------------------------------------------------------
check("tokenize 中文 bigram", tokenize("今天天气不错") == ["今天", "天天", "天气", "气不", "不错"])
check("tokenize 英文词", tokenize("Hello world") == ["hello", "world"])
check("tokenize 中英混合", "python" in tokenize("学 python 很好"))

# 2) 长期记忆 写入 / 去重 / 检索 ----------------------------------------
tmp = tempfile.mkdtemp(prefix="ltm_test_")
ltm = LongTermMemory(db_path=os.path.join(tmp, "m.db"), top_k=3, max_per_talker=10)
ltm.add("wxid_a", "用户问：我下周要去北京出差；我答：好的，需要我帮您安排提醒吗")
ltm.add("wxid_a", "用户问：我的猫叫咪咪；我答：记住了，咪咪")
ltm.add("wxid_b", "用户问：下周北京天气如何")
check("去重：相同内容不重复写入", ltm.add("wxid_a", "用户问：我下周要去北京出差；我答：好的，需要我帮您安排提醒吗") is False)
check("会话隔离：wxid_a 不检索到 wxid_b 内容",
      all(h["talker"] == "wxid_a" for h in ltm.search("wxid_a", "北京出差")))
hits = ltm.search("wxid_a", "北京出差安排")
check("检索命中北京出差", len(hits) >= 1 and "北京出差" in hits[0]["content"])
check("检索无关查询不命中", ltm.search("wxid_a", "外星人入侵") == [])
for i in range(12):
    ltm.add("wxid_a", f"用户问：填充消息{i}；我答：收到")
check("超限裁剪 max_per_talker=10", ltm.count("wxid_a") <= 10)

# 3) cron 匹配 ----------------------------------------------------------
# 2026-11-03 是周二（weekday=1，cron 周字段=2）
d_0900_tue = datetime(2026, 11, 3, 9, 0)
d_0930_tue = datetime(2026, 11, 3, 9, 30)
check("cron 每分匹配", match_cron("* * * * *", d_0900_tue))
check("cron 整点9点匹配", match_cron("0 9 * * *", d_0900_tue))
check("cron 整点9点 09:30 不匹配", not match_cron("0 9 * * *", d_0930_tue))
check("cron 每5分钟 09:30 匹配", match_cron("*/5 * * * *", d_0930_tue))
check("cron 每5分钟 09:33 不匹配", not match_cron("*/5 * * * *", datetime(2026, 11, 3, 9, 33)))
check("cron 周字段 周二匹配(2)", match_cron("0 9 * * 2", d_0900_tue))
check("cron 周字段 周一不匹配(1)", not match_cron("0 9 * * 1", d_0900_tue))
check("cron 范围 9-10点匹配", match_cron("0 9-10 * * *", d_0900_tue))
check("cron 非法表达式不匹配", not match_cron("bad expr", d_0900_tue))

# 4) Memory 集成：append 持久化 + build_messages 注入 -------------------
mem = Memory(short_window=4, long_term=ltm)
mem.append("wxid_a", "我下周要去北京出差三天", "好的，我记下了")
mem.append("wxid_a", "记得帮我订酒店", "已记录，需要哪家酒店？")
msgs = mem.build_messages("wxid_a", "assistant", "北京出差的酒店订好了吗")
roles = [m["role"] for m in msgs]
check("build_messages 含 system 角色卡", roles[0] == "system")
check("长期记忆命中注入 system", "长期记忆" in msgs[1]["content"] and "北京" in msgs[1]["content"])
check("短期窗口 4 轮 + 当前消息共 5 条",
      len([m for m in msgs if m["role"] in ("user", "assistant")]) == 5)

# 5) Scheduler 触发逻辑（不真正发送） ----------------------------------
class FakeSender:
    def __init__(self):
        self.sent = []

    def send_text(self, username, content):
        self.sent.append((username, content))
        return True


sched_cfg = {
    "tasks": [
        {"name": "daily", "time": "09:00", "to": "A", "content": "每日消息 {date}"},
        {"name": "cron5", "cron": "*/5 * * * *", "to": "A", "content": "cron 消息"},
        {"name": "once1", "once": "2026-11-03 09:00", "to": "A", "content": "一次性消息"},
    ]
}
fs = FakeSender()
sch = Scheduler(sched_cfg, fs)
sch._state_path = os.path.join(tmp, "state.json")
sch._resolve = lambda name: name          # 测试：显示名直接当 username

t1 = datetime(2026, 11, 3, 9, 0)
sch._tick(t1)
check("调度：9:00 触发 daily", any(c.startswith("每日消息") for _, c in fs.sent))
check("调度：9:00 触发 cron5", any(c == "cron 消息" for _, c in fs.sent))
check("调度：9:00 触发 once", any(c == "一次性消息" for _, c in fs.sent))
check("调度：占位符 {date} 替换", any(c == "每日消息 2026-11-03" for _, c in fs.sent))

n_before = len(fs.sent)
sch._tick(datetime(2026, 11, 3, 9, 0))          # 同一分钟再 tick
check("调度：同一分钟不重复触发", len(fs.sent) == n_before)

sch._tick(datetime(2026, 11, 3, 9, 5))
check("调度：cron5 到 09:05 再次触发",
      sum(1 for _, c in fs.sent if c == "cron 消息") == 2)
check("调度：daily 同日不重复", sum(1 for _, c in fs.sent if c.startswith("每日消息")) == 1)
check("调度：once 不重复", sum(1 for _, c in fs.sent if c == "一次性消息") == 1)

sch._tick(datetime(2026, 11, 4, 9, 0))          # 次日
check("调度：次日 daily 再触发", sum(1 for _, c in fs.sent if c.startswith("每日消息")) == 2)

# 状态落盘（重启不重复）
sch._save_state()
sch2 = Scheduler(sched_cfg, FakeSender())
sch2._state_path = os.path.join(tmp, "state.json")
sch2._resolve = lambda name: name
sch2._load_state()
sch2._tick(datetime(2026, 11, 4, 9, 0))
check("调度：重启后同日不重复（状态持久化）", len(sch2._state.get("daily", {})) > 0)

# 8.5) M4：任务级 enabled 启停 ----------------------------------------
sched_en = {"tasks": [
    {"name": "off_task", "time": "09:00", "to": "A", "content": "x", "enabled": False},
    {"name": "on_task", "time": "09:00", "to": "A", "content": "y"},
]}
fs3 = FakeSender()
sch3 = Scheduler(sched_en, fs3)
sch3._state_path = os.path.join(tmp, "state3.json")
sch3._resolve = lambda n: n
sch3._tick(datetime(2026, 11, 3, 9, 0))
check("M4 任务级 enabled=false 不触发", [c for _, c in fs3.sent] == ["y"])
sch3.set_enabled("off_task", True)
sch3._tick(datetime(2026, 11, 3, 9, 0))
check("M4 set_enabled 启用后触发", [c for _, c in fs3.sent] == ["y", "x"])
check("M4 set_enabled 未知任务返回 False", sch3.set_enabled("nope", True) is False)

# 8.6) M5：SystemController 状态机 --------------------------------------
class FakeDBH:
    def __init__(self, poll_interval=1.0, name="A", wxid="wxid_a"):
        self.self_name = name
        self.self_wxid = wxid
        self.started = 0
        self.stopped = 0
        self.registered = 0

    def register_listener(self, targets, handler):
        self.registered += 1
        self._handler = handler
        return {t.get("display"): t.get("display") for t in targets}

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def resolve_name(self, raw):
        return raw or None

    def is_sent_by_self(self, msg):
        return False


class FakeBrain:
    def __init__(self):
        self.self_name = ""

    def handle(self, msg, target, username):
        pass


class FakeSched:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self, resolve):
        self.started += 1

    def stop(self):
        self.stopped += 1


from core.system import SystemController

cfg_m5 = {"llm": {"api_key": "k"}, "wechat": {"poll_interval": 1.0},
          "listen": [{"name": "A", "role": "assistant"}]}
dbh5 = FakeDBH(name="double", wxid="wxid_x")
sched5 = FakeSched()
ctrl5 = SystemController(cfg_m5, dbh5, [{"display": "A", "role": "assistant"}],
                         FakeBrain(), scheduler=sched5)
ok, msg = ctrl5.start()
check("M5 系统启动", ok and ctrl5.running and dbh5.started == 1)
ok, msg = ctrl5.start()
check("M5 重复启动被拒", not ok)
ok, msg = ctrl5.stop()
check("M5 系统停止", ok and not ctrl5.running and dbh5.stopped == 1 and sched5.stopped == 1)

cfg_no_key = {"llm": {"api_key": ""}, "wechat": {}, "listen": []}
ctrl6 = SystemController(cfg_no_key, dbh5, [{"display": "A"}], FakeBrain())
ok, msg = ctrl6.start()
check("M5 无 LLM Key 拒绝启动", not ok)

cfg_empty = {"llm": {"api_key": "k"}, "wechat": {}, "listen": []}
ctrl6b = SystemController(cfg_empty, dbh5, [], FakeBrain())
ok, msg = ctrl6b.start()
check("M5 listen 为空拒绝启动", not ok)

ok, msg = ctrl5.start()
ok, msg = ctrl5.reload_targets()
check("M5 热重载后恢复运行", ok and ctrl5.running and dbh5.registered >= 2)

ctrl5.stop()
brain_new = FakeBrain()
ctrl5.brain = brain_new
ctrl5._dbh_factory = lambda poll_interval=1.0: FakeDBH(name="new", wxid="wxid_new")
ok, msg = ctrl5.start()
ok2, msg2 = ctrl5.rebind_account()
check("M5 rebind 切换账号并恢复", ok2 and ctrl5.dbh.self_name == "new"
      and ctrl5.running and brain_new.self_name == "new")


def boom(**kw):
    raise RuntimeError("wechat not logged in")


ctrl7 = SystemController(cfg_m5, dbh5, [{"display": "A"}], FakeBrain(), dbh_factory=boom)
ok, msg = ctrl7.rebind_account()
check("M5 rebind 失败返回错误", not ok and "重新检测失败" in msg)

# 8.7) M6：api_key DPAPI 加密 -------------------------------------------
from core.secrets import encrypt_text, decrypt_text  # noqa: E402
_secret = "sk-dpapi-test-123456"
_enc = encrypt_text(_secret)
check("DPAPI 加密解密 roundtrip", decrypt_text(_enc) == _secret)
check("DPAPI 密文不含明文", _secret not in _enc)
_tmp = os.path.join(tmp, "key_enc.yaml")
with open(_tmp, "w", encoding="utf-8") as _f:
    _f.write(f'llm:\n  provider: deepseek\n  api_key: ""\n  api_key_encrypted: "{_enc}"\n')
_cfg = load_config(_tmp)
check("load_config 解密加密字段", _cfg["llm"]["api_key"] == _secret)
_tmp2 = os.path.join(tmp, "key_bad.yaml")
with open(_tmp2, "w", encoding="utf-8") as _f:
    _f.write('llm:\n  provider: deepseek\n  api_key: ""\n  api_key_encrypted: "not-base64!!"\n')
_cfg2 = load_config(_tmp2)
check("load_config 密文损坏回退环境变量", _cfg2["llm"]["api_key"] == "")

# 6) M3：结构化 listen 解析 -------------------------------------------
from core.config import load_config, parse_listen, in_quiet_time, period_to_minutes  # noqa: E402

mixed_cfg = {"listen": [
    "简写字符串",
    ["好友", "companion"],
    {"name": "群名A", "role": "assistant", "enabled": False, "at_only": True,
     "quiet_periods": [["12:00", "13:30"]]},
]}
tgts = parse_listen(mixed_cfg)
check("M3 listen 字符串简写", tgts[0] == {"display": "简写字符串", "role": "assistant",
                                           "enabled": True, "at_only": False, "quiet_periods": None})
check("M3 listen 列表简写", tgts[1]["display"] == "好友" and tgts[1]["role"] == "companion")
check("M3 listen 对象全字段", tgts[2]["enabled"] is False and tgts[2]["at_only"] is True
      and tgts[2]["quiet_periods"] == [["12:00", "13:30"]])

# 7) M3：静默时段多时段 / 跨天 / 覆盖 --------------------------------
qcfg = {"safety": {"quiet_periods": [["12:00", "13:00"], ["23:00", "08:00"]]}}
check("M3 静默 12:30 命中", in_quiet_time(qcfg, "12:30"))
check("M3 静默 13:00 不命中(半开)", not in_quiet_time(qcfg, "13:00"))
check("M3 静默 23:30 命中(跨天)", in_quiet_time(qcfg, "23:30"))
check("M3 静默 07:59 命中(跨天)", in_quiet_time(qcfg, "07:59"))
check("M3 静默 15:00 不命中", not in_quiet_time(qcfg, "15:00"))
check("M3 静默会话级覆盖", in_quiet_time(qcfg, "20:00",
                                          periods=[period_to_minutes(["19:00", "21:00"])]))
check("M3 静默旧字段兼容", in_quiet_time({"safety": {"quiet_start": "23:00",
                                                       "quiet_end": "08:00"}}, "02:00"))

# 8) M3：Brain 会话级策略（at_only / 防刷屏 / 开关 / 静默覆盖） --------
from core.brain import Brain  # noqa: E402


class FakeLLM:
    def chat(self, messages):
        return "好的，已收到。"


bcfg = {"safety": {"enabled": True, "quiet_periods": [], "quiet_start": "", "quiet_end": "",
                   "blocked_keywords": [], "group_min_interval": 60}}
fs_b = FakeSender()
brain = Brain(bcfg, FakeLLM(), Memory(short_window=4), fs_b, self_name="double")


def gmsg(content, username="g1@chatroom"):
    return {"username": username, "type": 1, "content": content,
            "is_self": False, "create_time": int(time.time())}


def priv(content):
    return gmsg(content, username="wxid_friend")


brain.handle(gmsg("大家好"), {"display": "群名A", "role": "assistant",
                              "enabled": True, "at_only": True,
                              "quiet_periods": None}, "g1@chatroom")
check("M3 群 at_only：未提及本机不回复", len(fs_b.sent) == 0)

brain.handle(gmsg("double 今天开会吗"), {"display": "群名A", "role": "assistant",
                                          "enabled": True, "at_only": True,
                                          "quiet_periods": None}, "g1@chatroom")
check("M3 群 at_only：提及本机回复", len(fs_b.sent) == 1)

# 群防刷屏：60s 内第二条群消息跳过
brain.handle(gmsg("double 还有问题"), {"display": "群名A", "role": "assistant",
                                         "enabled": True, "at_only": True,
                                         "quiet_periods": None}, "g1@chatroom")
check("M3 群防刷屏：60s 内第二条跳过", len(fs_b.sent) == 1)

# 私聊不受 at_only / 防刷屏影响
brain.handle(priv("你好呀"), {"display": "好友", "role": "assistant",
                               "enabled": True, "at_only": True,
                               "quiet_periods": None}, "wxid_friend")
check("M3 私聊不受 at_only 影响", len(fs_b.sent) == 2)

# 会话级 enabled=false 跳过
brain.handle(priv("在吗"), {"display": "好友", "role": "assistant",
                             "enabled": False, "at_only": False,
                             "quiet_periods": None}, "wxid_friend")
check("M3 会话级开关关闭跳过", len(fs_b.sent) == 2)

# 会话级静默覆盖（全天静默 → 跳过）；对照组无覆盖 → 全局无时段 → 回复
brain.handle(priv("测试覆盖"), {"display": "好友", "role": "assistant",
                                 "enabled": True, "at_only": False,
                                 "quiet_periods": [["00:00", "23:59"]]}, "wxid_friend")
check("M3 会话级静默覆盖生效", len(fs_b.sent) == 2)
brain.handle(priv("测试对照组"), {"display": "好友", "role": "assistant",
                                   "enabled": True, "at_only": False,
                                   "quiet_periods": None}, "wxid_friend")
check("M3 无覆盖时用全局时段(当前无静默)回复", len(fs_b.sent) == 3)

print()
print(f"ALL SELFTESTS PASSED ({len(PASS)}/{len(PASS)})")
