# -*- coding: utf-8 -*-
"""配置加载：YAML + 环境变量。

约定：
- config.yaml 中 llm.api_key / llm.base_url 留空时，分别从环境变量
  LLM_API_KEY / LLM_BASE_URL 读取。
- listen 为 [[昵称或群名, 角色名], ...]，运行时会把昵称解析为会话
  username 再注册监听。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import yaml
from loguru import logger

DEFAULTS: Dict[str, Any] = {
    "wechat": {
        "poll_interval": 1.0,      # 数据库轮询间隔（秒）
    },
    "llm": {
        "provider": "deepseek",    # deepseek / qwen / glm
        "model": "",               # 留空则按 provider 使用默认模型
        "base_url": "",            # 留空则按 provider 使用默认端点，或读 LLM_BASE_URL
        "api_key": "",             # 留空则读 LLM_API_KEY
        "temperature": 0.7,
        "timeout": 60,
    },
    "listen": [],                  # 见 parse_listen：支持字符串 / [名, 角色] / {name, role, ...}
    "safety": {
        "enabled": True,           # 全局总开关
        "quiet_periods": [],       # 多时段静默 [[开始HH:MM, 结束HH:MM], ...]；兼容旧 quiet_start/quiet_end
        "quiet_start": "23:00",    # 兼容旧配置：静默时段开始（HH:MM）
        "quiet_end": "08:00",      # 兼容旧配置：静默时段结束（HH:MM）
        "blocked_keywords": ["转账", "付款", "验证码"],
        "group_min_interval": 60,  # 群消息两次回复最小间隔（秒），防刷屏
    },
    "sender": {
        "min_interval": 3.0,       # 两次发送最小间隔（秒）
        "max_retry": 2,            # 发送失败重试次数
        "human_typing": True,      # 模拟打字延迟
        "max_consecutive_fail": 3, # 连续失败达到该值自动暂停
    },
    "memory": {
        "short_window": 10,        # 短期记忆保留轮数
        "long_term": {             # 长期记忆（M2）
            "enabled": True,       # 本地检索，零额外依赖
            "top_k": 5,            # 每次注入的命中条数
            "max_per_talker": 200, # 每会话上限（超出删最旧）
        },
    },
    "scheduler": {                 # 定时任务（M2）
        "enabled": False,
        "tasks": [],
    },
    "webui": {                     # 管理面板（M4）
        "enabled": True,           # 启动时是否开启本地 WebUI
        "host": "127.0.0.1",       # 仅本机访问；请勿改为 0.0.0.0 暴露公网
        "port": 8790,
    },
}

# 各 Provider 官方 OpenAI 兼容端点与默认模型
PROVIDERS: Dict[str, Dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
    },
}


def _deep_merge(base: Dict, override: Dict) -> Dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """加载 YAML 配置并与默认值合并；返回完整配置 dict。"""
    cfg = _deep_merge(DEFAULTS, {})
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, raw)

    # 环境变量兜底
    llm = cfg["llm"]
    # Key 优先级：api_key_encrypted（DPAPI 密文）> api_key（明文，兼容旧配置）
    # > 环境变量 LLM_API_KEY
    enc = (llm.get("api_key_encrypted") or "").strip()
    if enc:
        try:
            from .secrets import decrypt_text
            llm["api_key"] = decrypt_text(enc)
        except Exception as exc:
            logger.warning("api_key_encrypted 解密失败（可能跨用户/跨机器）：{}", exc)
            llm["api_key"] = ""
    if not llm.get("api_key"):
        llm["api_key"] = os.environ.get("LLM_API_KEY", "")
    if not llm.get("base_url"):
        llm["base_url"] = os.environ.get("LLM_BASE_URL", "")

    provider = (llm.get("provider") or "deepseek").lower()
    pconf = PROVIDERS.get(provider)
    if pconf is None:
        raise ValueError(f"不支持的 LLM provider: {provider}（可选 deepseek/qwen/glm）")
    if not llm.get("model"):
        llm["model"] = pconf["model"]
    if not llm.get("base_url"):
        llm["base_url"] = pconf["base_url"]
    cfg["llm"]["provider"] = provider
    return cfg


def parse_listen(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把 listen 配置标准化为结构化目标列表。

    支持三种形式（可混用）：
      - "昵称"                          → 默认角色 assistant
      - ["昵称", "角色"]                → 指定角色
      - {name, role, enabled, at_only, quiet_periods}
    返回每一项：{"display", "role", "enabled", "at_only", "quiet_periods"}。
    """
    out: List[Dict[str, Any]] = []
    for item in cfg.get("listen", []) or []:
        t = _normalize_target(item)
        if t:
            out.append(t)
    return out


def _normalize_target(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, dict):
        display = str(item.get("name") or item.get("display") or "").strip()
        if not display:
            return None
        return {
            "display": display,
            "role": str(item.get("role") or "").strip() or "assistant",
            "enabled": bool(item.get("enabled", True)),
            "at_only": bool(item.get("at_only", False)),
            "quiet_periods": item.get("quiet_periods") or None,
        }
    if isinstance(item, (list, tuple)) and len(item) >= 1:
        display = str(item[0]).strip()
        role = str(item[1]).strip() if len(item) >= 2 and str(item[1]).strip() else "assistant"
        if not display:
            return None
        return {"display": display, "role": role,
                "enabled": True, "at_only": False, "quiet_periods": None}
    if isinstance(item, str) and item.strip():
        return {"display": item.strip(), "role": "assistant",
                "enabled": True, "at_only": False, "quiet_periods": None}
    return None


# -- 静默时段 ------------------------------------------------------------

def period_to_minutes(spec: Any) -> Optional[Tuple[int, int]]:
    """把 ["HH:MM", "HH:MM"] 转成分钟对；非法返回 None。"""
    try:
        sh, sm = map(int, str(spec[0]).split(":")[:2])
        eh, em = map(int, str(spec[1]).split(":")[:2])
        return sh * 60 + sm, eh * 60 + em
    except (ValueError, AttributeError, IndexError, TypeError):
        return None


def global_periods(cfg: Dict[str, Any]) -> List[Tuple[int, int]]:
    """全局静默时段（分钟对列表）；优先 safety.quiet_periods，兼容旧 quiet_start/quiet_end。"""
    safety = cfg.get("safety", {})
    raw = safety.get("quiet_periods") or []
    periods: List[Tuple[int, int]] = []
    if (isinstance(raw, (list, tuple)) and raw
            and isinstance(raw[0], (list, tuple))):
        for p in raw:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                pm = period_to_minutes(p)
                if pm:
                    periods.append(pm)
    elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
        pm = period_to_minutes(raw)
        if pm:
            periods.append(pm)
    if not periods:
        pm = period_to_minutes([safety.get("quiet_start"), safety.get("quiet_end")])
        if pm and pm[0] != pm[1]:
            periods.append(pm)
    return periods


def in_periods(periods: List[Tuple[int, int]], cur_min: int) -> bool:
    """判断分钟数是否命中任一时段（支持跨天，如 23:00–08:00）。"""
    for s, e in periods:
        if s <= e:
            if s <= cur_min < e:
                return True
        elif cur_min >= s or cur_min < e:
            return True
    return False


def in_quiet_time(cfg: Dict[str, Any], now_hm: str,
                  periods: Optional[List[Tuple[int, int]]] = None) -> bool:
    """判断 HH:MM 是否处于静默时段；periods 为会话级覆盖，缺省用全局时段。"""
    ps = periods if periods is not None else global_periods(cfg)
    if not ps:
        return False
    try:
        h, m = map(int, str(now_hm).split(":")[:2])
    except (ValueError, AttributeError):
        return False
    return in_periods(ps, h * 60 + m)
