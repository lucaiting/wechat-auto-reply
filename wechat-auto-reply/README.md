# wechat-auto-reply

微信消息自动读取与回复系统（M1 最小闭环 + M2 长期记忆与定时任务 + M3 多角色/群管理与静默细化 + M4 本地 WebUI 管理面板 + M5 面板系统控制）。

技术路线：**本地数据库解密读取 + 坐标/OCR 混合发送 + 国内 LLM API + 三层提示词记忆**
目标环境：Windows 10/11 + 微信 **4.2.0.12**（锁版本）+ Python 3.11

> ⚠️ 非官方手段，违反《腾讯微信软件许可及服务协议》，存在封号风险。
> **请使用测试小号**，关闭微信自动更新，勿用于营销/群发。

## 架构

```
微信客户端 4.2.0.12
 ├─ 读取：本地加密数据库解密（wechatauto-replica 内置 SQLCipher 解密）
 └─ 发送：坐标+OCR/UIA 混合（驱动微信窗口）
        ↓ 新消息
Listener（数据库增量轮询，水位自动落盘，重启不重复推送）
        ↓
Brain（会话级策略：开关/群@提及/防刷屏/静默覆盖 → 三层记忆组装 → LLM 生成 → 输出校验）
        ↓                          ↑
Sender（节流 / 重试 / 连续失败自动暂停）
        ↑
Scheduler（定时任务：每天 / cron / 一次性，支持 LLM 生成内容）
        ↓
WebUI（本地管理面板 http://127.0.0.1:8790/：概览 / 配置 / 角色卡 / 流水 / 记忆 / 调度 / 日志）
```

记忆三层：
1. **角色卡**：`prompts/<角色>.md`（system）；
2. **短期窗口**：每会话最近 N 轮（内存）；
3. **长期记忆**：`data/memory.db` 持久化，中文 bigram + 简化 BM25 检索，命中拼入 system（M2，零额外依赖）。

## 目录结构

```
wechat-auto-reply/
├── main.py                # 入口（--doctor 体检模式；自动装配记忆/调度器）
├── config.example.yaml    # 配置模板（复制为 config.yaml）
├── requirements.txt
├── core/
│   ├── config.py          # 配置加载（YAML + 环境变量）
│   ├── db_layer.py        # WeChatDB/Listener 封装、昵称→username 解析、自发送过滤
│   ├── llm.py             # OpenAI 兼容客户端（指数退避重试）
│   ├── longmem.py         # 长期记忆：sqlite 存取 + 简化 BM25 检索（M2）
│   ├── memory.py          # 三层记忆组装（角色卡 + 短期窗口 + 长期记忆注入）
│   ├── brain.py           # 过滤分流 + 记忆组装 + 输出校验 + 消息流水
│   ├── scheduler.py       # 定时任务：每天/cron/一次性 + LLM 生成（M2）
│   ├── webui.py           # 本地管理面板 HTTP 服务与 API（M4）
│   └── sender.py          # 发送节流/重试/连续失败暂停
├── webui/index.html       # 管理面板前端（单页，零外部依赖）
├── prompts/assistant.md   # 角色人设（新增角色 = 新增 .md 文件；companion.md 为闲聊示例）
├── scripts/
│   ├── doctor.py          # 环境体检
│   └── selftest.py        # M2 逻辑自测（不依赖微信/LLM）
├── data/                  # 运行时数据（自动创建）：chat.db / memory.db / scheduler_state.json
└── logs/                  # 运行日志（自动创建）
```

## 环境配置

1. **Python 3.11 虚拟环境**（依赖 winsdk→uiautomation 链，3.14+ 可能无 wheel）：

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. **准备微信**：安装微信 4.2.0.12 并登录测试小号；**关闭自动更新**（设置 → 关于微信 → 更新设置 → 手动更新）；聊天窗口保持可见、不最小化（发送依赖窗口驱动）。

3. **配置**：复制 `config.example.yaml` 为 `config.yaml`，填写：

   - `llm.api_key`：DeepSeek/Qwen/GLM 的 API Key（或设环境变量 `LLM_API_KEY`）；
   - `listen`：监听对象列表（见下）。

### listen 配置（M3 多角色 / 群管理）

三种写法可混用，`name` 须与微信显示的昵称/群名**完全一致**：

```yaml
listen:
  - name: "好友昵称"            # 完整对象（推荐）
    role: "assistant"          # 对应 prompts/<角色>.md
    enabled: true              # 会话级开关
    at_only: false             # 仅群生效：true 时只回复提及本机昵称的消息
    quiet_periods: []          # 会话级静默覆盖，如 [["12:00", "13:30"]]
  - ["某好友", "companion"]    # 列表简写
  - "某好友"                   # 字符串简写
```

### 静默时段（M3 多时段）

`safety.quiet_periods` 支持多条、跨天；不填则兼容旧字段 `quiet_start`/`quiet_end`。
会话可用 `quiet_periods` 覆盖全局。群防刷屏：`safety.group_min_interval`（默认 60s）。

## 运行

```powershell
# 0. 逻辑自测（不依赖微信/LLM，验证记忆检索与定时匹配）
.\.venv\Scripts\python.exe scripts\selftest.py

# 1. 体检（读库/账号/会话/监听解析）
.\.venv\Scripts\python.exe scripts\doctor.py

# 2. 启动（监听 + 定时任务 + WebUI 管理面板）
.\.venv\Scripts\python.exe main.py

# 2b. 只开管理面板（未配 LLM Key 时自动进入此模式；配了 Key 则全功能）
#     --no-webui 可关闭面板
```

启动后浏览器打开 **http://127.0.0.1:8790/**（仅本机可访问）：

- **系统状态**：状态灯 + 「启动系统 / 停止系统」按钮（控制监听，不关面板）+ 「重新检测账号」（微信切号后一键跟随，无需重启）；
- **概览**：本机账号、LLM 状态、监听对象解析结果、流水/记忆计数（5s 自动刷新）；
- **配置**：监听对象可视化增删（添加前自动校验能否解析为真实会话，立即生效并写回）+ 整文件 YAML 编辑（保存前校验并备份 `.bak`，重启生效）；
- **消息流水** / **长期记忆**：按会话查看数据；
- **定时任务**：一键启停（内存立即生效并同步写回配置）；
- **日志**：当日运行日志尾部（8s 自动刷新）。

> 安全：面板仅绑定 127.0.0.1 并校验 Host 头（防 DNS rebinding）；配置页 api_key 明文展示，请勿把面板暴露到公网（`webui.host` 勿改 0.0.0.0）。API Key 建议加密存储：运行 `scripts\encrypt_key.py` 后，Key 以 DPAPI 密文写入 `api_key_encrypted`（仅本机当前用户可解密），配置加载时自动解密，不再明文落盘。

验证链路（对照日志）：
- 出现「数据库初始化成功，本机账号: …」→ 解密读取 OK；
- 出现「已注册监听: 「昵称」→ wxid_xxx」→ 监听对象 OK；
- 手机/另一台设备给监听对象发消息，日志出现「已发送 → …」→ 全链路 OK。

## 定时任务（M2）

`config.yaml` 的 `scheduler` 段配置，`to` 用微信显示名（运行时会解析为会话 username）。
每个任务 `time`（每天 HH:MM）/ `cron`（5 字段）/ `once`（一次性）三选一：

```yaml
scheduler:
  enabled: true
  tasks:
    - name: "早间提醒"
      time: "09:00"
      to: "文件传输助手"
      content: "早上好！{date}，记得喝水。"
    - name: "每日总结"
      time: "22:00"
      to: "文件传输助手"
      content: "$llm:请根据{memory}，用 3 句话总结今天和用户聊了什么"
```

- `{date}` / `{time}`：替换为触发当天的日期 / 时间；
- `content` 以 `$llm:` 开头 → 用 LLM 生成（`{memory}` 注入该会话近期长期记忆）；
- 触发状态写入 `data/scheduler_state.json`：每天任务同日不重复，cron 同一分钟不重复，一次性任务触发后失效；
- 发送复用 Sender（节流/重试/连续失败暂停），无 LLM Key 时 `$llm:` 任务自动跳过并告警。

## 安全与运维

- **防自回复死循环**：`sender_username == 本机 wxid`（回退 `sender_id==2`）过滤自己发的消息；
- **防误发**：总开关 `safety.enabled`、静默时段、拦截词表、连续发送失败自动暂停；
- **日志**：`logs/run_YYYYMMDD.log`（DEBUG 级，保留 14 天）；消息流水在 `data/chat.db`；长期记忆在 `data/memory.db`；
- **守护**：稳定后可注册计划任务/nssm 常驻；微信窗口被最小化或锁屏时发送可能失败，注意观察日志。

## 已知限制

- 只处理文本消息语义（图片/语音/文件等仅记录状态）；
- 长期记忆为本地关键词/BM25 检索，非真正语义向量（后续可接入 embedding API 替换 `core/longmem.py` 的 tokenize/score，接口不变）；
- 记忆条目为原始对话沉淀，未做 LLM 摘要压缩（`use_llm_summary` 为预留项）；
- 群消息 `@提及` 检测依赖本机昵称出现在消息文本中，微信对 @ 的渲染格式变化可能影响命中（`at_only` 未命中时保守不回复）；
- WebUI 配置保存后需**重启 main.py 生效**；「定时任务一键启停」写回配置时 YAML 会被重排（丢失注释），建议日常用「配置」页整文件编辑保留格式；
- `wechatauto-replica` 为个人维护项目（作者学业原因更新放缓），微信小版本升级可能需校准布局常量；升级前先跑 `doctor.py` 验证。

## 里程碑

- M0 环境与微信准备 ✅　M1 最小闭环（读库+LLM+回复+流水）✅
- M2 长期记忆 + 定时任务 ✅
- M3 多角色/群管理 + 静默时段细化 ✅
- M4 本地 WebUI 管理面板 ✅
- M5 面板系统控制（启停 / 监听对象增删 / 账号切换）✅（本轮）
- 角色卡可视化管理（WebUI 角色卡 Tab：查看/新建/修改/删除 prompts/*.md，内置 assistant 禁删）✅
- 后续可选：配置热加载、多账号并行、RAG 语义检索升级

详细需求与设计见桌面《微信自动回复系统-需求分析与系统设计.md》。
