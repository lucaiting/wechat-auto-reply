# -*- coding: utf-8 -*-
"""把 config.yaml 中明文 llm.api_key 加密为 api_key_encrypted（Windows DPAPI）。

用法（在项目根目录）:
    .\.venv\Scripts\python.exe scripts\encrypt_key.py
    .\.venv\Scripts\python.exe scripts\encrypt_key.py --config <路径>

加密后：
- config.yaml 中 api_key 置空，api_key_encrypted 保存 base64 密文；
- 只有本机当前 Windows 用户能解密（load_config 自动解密，程序无感）；
- 原文件先备份为 config.yaml.bak。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.secrets import encrypt_text  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="加密 config.yaml 中的 llm.api_key")
    ap.add_argument("--config", default=os.path.join(BASE, "config.yaml"),
                    help="配置文件路径（默认项目 config.yaml）")
    args = ap.parse_args()
    path = args.config

    if not os.path.exists(path):
        print(f"配置不存在: {path}")
        return 1
    if re.search(r"api_key_encrypted:", open(path, encoding="utf-8").read()):
        print("已存在 api_key_encrypted，无需迁移。")
        return 0

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    m = re.search(r'^([ \t]*)api_key:\s*(?:"([^"]*)"|(\S+))', text, re.M)
    if not m:
        print("未找到 api_key 行")
        return 1
    indent = m.group(1)
    key = (m.group(2) if m.group(2) is not None else m.group(3) or "").strip()
    if not key:
        print("api_key 为空：没有明文可加密（可用环境变量 LLM_API_KEY，或先填写再运行）")
        return 1

    enc = encrypt_text(key)
    new_line = (f'{indent}api_key: ""                # 留空读环境变量 LLM_API_KEY；或改用下方加密字段\n'
                f'{indent}api_key_encrypted: "{enc}"   # DPAPI 加密（仅本机当前用户可解密）')
    new_text = re.sub(r'^[ \t]*api_key:.*$', lambda mm: new_line,
                      text, count=1, flags=re.M)

    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)

    # 回读验证：load_config 应能解密出原 Key
    from core.config import load_config
    cfg = load_config(path)
    if cfg["llm"]["api_key"] != key:
        print("ERROR：解密回读与原文不一致，已中止（配置已备份于 .bak）")
        return 1
    print(f"OK：api_key 已加密为 api_key_encrypted（备份于 {path}.bak）")
    print("解密回读验证通过，系统加载时自动解密，无需其他改动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
