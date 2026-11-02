# -*- coding: utf-8 -*-
"""LLM 客户端：OpenAI 兼容 chat/completions，带指数退避重试。"""
from __future__ import annotations

import time
from typing import List, Optional

from loguru import logger


class LLMClient:
    def __init__(self, config: dict):
        llm = config.get("llm", {})
        self.provider = llm.get("provider", "deepseek")
        self.model = llm.get("model", "")
        self.base_url = llm.get("base_url", "")
        self.api_key = llm.get("api_key", "")
        self.temperature = float(llm.get("temperature", 0.7))
        self.timeout = float(llm.get("timeout", 60))
        self.max_retries = 3
        self._client = None
        if not self.api_key:
            raise ValueError("LLM API Key 未配置：请设置 config.yaml 的 llm.api_key 或环境变量 LLM_API_KEY")

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    def chat(self, messages: List[dict]) -> Optional[str]:
        """调用模型生成回复；失败重试（指数退避）；重试耗尽返回 None。"""
        delay = 1.0
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._get_client().chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                )
                content = resp.choices[0].message.content
                if content is None:
                    raise RuntimeError("模型返回空内容")
                return content.strip()
            except Exception as exc:
                last_err = exc
                logger.warning("LLM 调用失败（第 {} 次）: {}", attempt + 1, exc)
                if attempt >= self.max_retries:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 10.0)
        logger.error("LLM 重试耗尽（{} 次），放弃本次回复: {}", self.max_retries + 1, last_err)
        return None
