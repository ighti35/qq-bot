# QQ AI 自动回复机器人

用 **NapCatQQ + OneBot v11** 接入 QQ，用 **DeepSeek** 大模型智能回复，支持 **Bing 联网搜索**、**长期记忆学习**、**最新/最重要消息优先**、**主动找话题**。

```
┌──────────┐  反向 WebSocket    ┌───────────────┐   HTTP API    ┌──────────┐
│ NapCatQQ │ ─────────────────▶ │  qq_bot.py     │ ────────────▶ │ NapCatQQ │
│ (OneBot) │  推送消息事件       │  (本脚本+大脑) │  发回复       │          │
└──────────┘                    └──────┬────────┘               └──────────┘
                                       │
                                ┌──────▼──────┐   ┌──────────────┐
                                │  brain.py   │──▶│ DeepSeek LLM │
                                │ (记忆/排序) │   └──────────────┘
                                └──────┬──────┘   ┌──────────────┐
                                       └──────────▶│ bing_search  │
                                                   └──────────────┘
```

---

## 一、环境准备

```bash
pip install aiohttp playwright
playwright install chromium
```

> `playwright install chromium` 会下载一个无头浏览器内核，用于 Bing 联网搜索。
> 如果暂时不想联网搜索，可以跳过这步，并在 `qq_config.json` 里把 `bing_search.enabled` 改成 `false`。

## 二、部署 NapCatQQ（关键前置）

NapCatQQ 是一个让普通 QQ 号支持 OneBot 协议的工具。二选一：

### 方式 A：NapCatQQ + 官方 QQ（推荐）

1. 到 [NapCatQQ 发布页](https://github.com/NapNeko/NapCatQQ/releases) 下载 Windows 版（`.zip` 或 `_win_install.exe`）。
2. 解压运行，按提示用你的 QQ 号登录（需要手机扫码）。
3. 登录成功后，NapCatQQ 会启动一个 **WebUI**（默认 http://127.0.0.1:6099 ），用浏览器打开它。
4. 在 WebUI 的「网络配置 / Network」里，**新增一个 WebSocket 服务端**：
   - `host`: `127.0.0.1`
   - `port`: `3001`   ← 必须和 `qq_config.json` 里的 `onebot.ws_port` 一致
   - 消息上报格式：`array`
   - 开启「反向 WebSocket」（让 NapCatQQ 主动连到我们的脚本）
5. 同时确保「HTTP 服务端」已开启，地址为 `127.0.0.1:3000`（和 `onebot.http_url` 一致）。

### 方式 B：LLOneBot（QQNT 插件）

1. 在 QQNT 客户端安装 [LLOneBot](https://github.com/LLOneBot/LLOneBot) 插件。
2. 插件设置里开启「反向 WebSocket」，地址填 `ws://127.0.0.1:3001`。
3. 开启 HTTP 服务 `127.0.0.1:3000`。

> 无论是哪种，最终效果都是：QQ 收到消息 → OneBot 把事件推送到本脚本的 `ws://127.0.0.1:3001`；本脚本通过 `http://127.0.0.1:3000` 调 API 发消息。

## 三、配置

先准备配置文件（二选一）：

```bash
copy qq_config.example.json qq_config.json   # 复制模板
python qq_bot.py --init                       # 或让 bot 生成一份默认配置
```

然后编辑 `qq_config.json`：

| 字段 | 说明 |
|------|------|
| `llm.api_key` | 你的 API Key。默认端点是官方 `https://api.deepseek.com/v1/chat/completions` + 模型 `deepseek-chat`；想换 OpenAI / Ollama 等，改 `api_url` 和 `model` 即可 |
| `llm.model` | 模型名 |
| `persona` | 人设 / 回复风格。默认是「友善真人」风格，想变毒舌 / 抽象 / 猫娘等，直接改这段 prompt |
| `whitelist` | 白名单。空 = 回复所有人（私聊）；填 `[123456]`(QQ号) 或 `["昵称"]` 只回复这些人 |
| `blacklist` | 黑名单 |
| `cooldown_seconds` | 同一会话两次回复的最小间隔 |
| `batch_window_seconds` | 合并窗口：N 秒内的多条消息一起处理（实现「最新/最重要」优先级） |
| `group_only_at` | `true` = 群里只回复 @机器人 的消息 |
| `bing_search.enabled` | 是否启用联网搜索 |
| `bing_search.search_keywords` | 命中这些词才触发搜索 |
| `conversation_end_keywords` | 命中这些词判定对方要结束话题，进入静默（不回、不找话题） |
| `idle_topic.enabled` | 是否开启「对方不回时主动找话题」（默认开，2 分钟触发） |

## 四、运行

```bash
python qq_bot.py          # 启动，常驻监控
python qq_bot.py --init   # 重新生成一份默认配置
```

启动后会显示监听地址。看到 `NapCatQQ 已连接 (WebSocket)` 就说明链路通了，直接让朋友给你发消息测试。

## 五、它怎么工作（对应你的需求）

1. **智能回复** — 每条消息都带上人设 + 长期记忆 + 最近上下文一起喂给 DeepSeek。
2. **优先最新/最重要消息** — 3 秒内到达的多条消息会**合并成一批**处理，提示词里强制要求「先跟最新话题，再补重要的未答问题」，避免换话题后还停在旧话题。
3. **联网搜索** — 检测到消息里含「最新/今天/查一下/天气/新闻」等词，就先用 Playwright 抓 Bing 结果再让模型结合回答。
4. **学习习惯** — 每 5 轮对话，模型会从最近聊天里提取对方的兴趣/事实/常聊话题/风格，存到 `memory/conv_*.json`，下次自动带上。
5. **主动找话题** — `idle_topic.enabled` 默认开启：对方 2 分钟没回，机器人就结合对方兴趣（必要时搜一条热点）主动发一条自然的新话题；只对私聊生效，且 30 分钟内最多一次、每天最多 3 次，不会刷屏。
6. **对方说结束话题就静默** — 当对方明确说「晚安 / 拜拜 / 不聊了 / 先睡了」等（见 `conversation_end_keywords`），机器人会**静默**：不回这条、也不再主动找话题，直到对方再次发消息才恢复正常。

## 六、常见问题

- **没连上 / 收不到消息**：检查 NapCatQQ WebUI 里反向 WS 的 `port` 是不是 `3001`，以及 HTTP 是不是 `3000`。
- **搜不了 / 慢**：`playwright install chromium` 没跑，或网络到 bing.com 不通。可关掉 `bing_search.enabled`。
- **记忆文件在哪**：`memory/conv_private_<QQ号>.json`，可直接打开看它学到了什么，也能手动删掉重置。
- **封号风险**：NapCatQQ 走的是官方客户端协议，风险远低于第三方协议库，但仍建议用不太重要的号先测试。

## 七、局限（如实说明）

- 主动找话题现在**默认开启**（2 分钟触发），若不想要可把 `idle_topic.enabled` 改回 `false`。
- 联网搜索用无头浏览器，每次搜索要启动浏览器，比 API 慢（约 2~5 秒），聊天频率不高时无感。
- 电脑关机 / QQ 掉线 / NapCatQQ 退出，机器人就停。
