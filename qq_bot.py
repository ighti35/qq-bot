# -*- coding: utf-8 -*-
"""
QQ AI 自动回复机器人

通过 OneBot v11「反向 WebSocket」接入 NapCatQQ，用 DeepSeek 大模型智能回复：
  - 优先回应最新消息 + AI 判定最重要的消息，自动跟随话题切换
  - 记录并学习对方习惯/兴趣/事实（长期记忆）
  - 需要实时信息时自动联网搜索（Bing）
  - 对方长时间不回时主动找话题

依赖: pip install aiohttp playwright && playwright install chromium
前置: 已部署并登录 NapCatQQ（见 README.md）

用法:
    python qq_bot.py              # 启动（常驻）
    python qq_bot.py --init       # 生成配置文件
"""

import sys
import io
import json
import time
import re
import hashlib
import random
import argparse
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web

from brain import Brain
import bing_search

# 修复 Windows 控制台 GBK 编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "qq_config.json"

# ============================================================
# 日志
# ============================================================
log_dir = BASE / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_dir / f"qq_bot_{datetime.now().strftime('%Y%m%d')}.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("qq_bot")


# ============================================================
# 默认配置（与 qq_config.json 一致，api_key 留空）
# ============================================================
DEFAULT_CONFIG = {
    "onebot": {
        "ws_host": "127.0.0.1",   # 本脚本的 WebSocket 服务地址（NapCatQQ 会连过来）
        "ws_port": 3001,
        "http_url": "http://127.0.0.1:3000",  # NapCatQQ 的 HTTP API 地址（用于发消息）
    },
    "llm": {
        # OpenAI 兼容端点：改成 DeepSeek / OpenAI / Ollama 等均可（改 api_url + model 即可）
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.9,
        "max_tokens": 400,
    },
    "persona": (
        "你是一个在 QQ 上和好友聊天的真人用户，友善、自然、接地气。你的说话风格：\n"
        "1. 短句为主，节奏快，像真人打字一样自然，别端着\n"
        "2. 对朋友友善真诚，可以偶尔轻松调侃，但别怼人、别毒舌、别挖苦\n"
        "3. 用词口语化，可以适度用点网络流行语，但别为了玩梗而玩梗\n"
        "4. QQ 表情要克制自然，情绪到位时才在句末加一个 [face:ID]（可用：[face:178] 斜眼笑、[face:37] 骷髅、[face:5] 流泪、[face:9] 大哭、[face:13] 呲牙、[face:20] 偷笑、[face:27] 流汗、[face:36] 衰），一条消息最多一个表情\n"
        "5. 表情包更要少见，只有情绪很强烈时才发一个 [sticker]（只写 [sticker] 本身，后面不要加任何说明文字），平时不要发\n"
        "6. 可以一次连发 1~3 条短消息，像真人聊天，用单独一行「---」分隔\n"
        "7. 说话风格跟着对方当前的话题走：聊文学、电影、音乐这些就自然文艺一点、认真一点；聊生活就接地气。别不管聊什么都往游戏上硬带\n"
        "8. 绝对不要承认自己是 AI / 机器人 / 程序，你就是真人\n"
        "9. 不要每条消息都用问句收尾、别追着反问对方；该陈述就陈述，像真人聊天一样自然，别让对方觉得你在审问"
    ),
    "whitelist": [],   # 空 = 回复所有人（私聊）；可填 QQ 号(数字)或昵称(字符串)
    "blacklist": [],
    "stopped": False,  # True = 全局静默：bot 完全不回任何消息、也不主动找话题
    "cooldown_seconds": 5,      # 同一会话两次回复之间的最小间隔
    "batch_window_seconds": 3,  # 合并窗口：3 秒内的多条消息一起处理
    "max_history": 40,          # 每个会话保留的最近消息条数
    "memory_update_every": 5,   # 每 N 轮做一次记忆学习
    "group_only_at": True,      # 群里只回复 @机器人 的消息
    "conversation_end_keywords": [
        "晚安", "晚安啦", "拜拜", "再见",
        "先睡了", "去睡了", "我睡了", "睡了哦", "睡觉了", "睡觉去了",
        "不聊了", "先不聊了", "不说了", "就这样吧", "先这样吧",
        "先忙", "忙去了", "先忙了", "有事去了",
        "下线了", "先下了",
        "别回了", "别发了", "不用回了", "不用发了",
        "下次聊", "改天聊", "有空再聊", "回头聊", "回头再说",
        "先走了", "我走了", "结束话题",
    ],
    "bing_search": {
        "enabled": True,
        "max_results": 5,
        # 时效/事实类触发词；问句标记（吗/呢/？/什么/怎么/多少…）在代码里单独判断。
        # 真正的「要不要搜、搜什么」由 LLM 决定，这里只是便宜地先筛掉明显闲聊。
        "search_keywords": [
            "最新", "最近", "今天", "现在", "新闻", "天气", "价格", "多少钱",
            "几点", "在哪", "排行", "版本", "更新", "出了", "上线", "发售", "上市",
        ],
    },
    "idle_topic": {
        "enabled": True,           # 对方不回时主动找话题
        "idle_seconds": 60,        # 对方多久没回才触发（秒）——1 分钟
        "min_interval_seconds": 300,   # 两次主动找话题的最小间隔（秒）
        "max_per_day": 3,          # 每个会话每天最多主动找几次
    },
}


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = Path(path) if path else CONFIG_FILE
    if p.exists():
        try:
            with open(p, encoding='utf-8') as f:
                cfg.update(json.load(f))
        except Exception as e:
            log.warning("配置加载失败: %s", e)
    return cfg


# ============================================================
# OneBot 事件解析
# ============================================================
def extract_text(event):
    """从事件里提取纯文本（兼容 array / string 两种 messagePostFormat）"""
    msg = event.get("message")
    if isinstance(msg, list):
        parts = []
        for seg in msg:
            if seg.get("type") == "text":
                parts.append(seg.get("data", {}).get("text", ""))
        return "".join(parts).strip()
    if isinstance(msg, str):
        s = re.sub(r"\[CQ:[^\]]+\]", "", msg)
        s = re.sub(r"\[[a-z]+:[^\]]*\]", "", s)
        return s.strip()
    return ""


def is_at_me(event):
    """判断群消息是否 @ 了机器人"""
    self_id = str(event.get("self_id"))
    msg = event.get("message")
    if isinstance(msg, list):
        for seg in msg:
            if seg.get("type") == "at":
                qq = str(seg.get("data", {}).get("qq", ""))
                if qq in (self_id, "all"):
                    return True
    elif isinstance(msg, str):
        if f"[CQ:at,qq={self_id}]" in msg or "[CQ:at,qq=all]" in msg:
            return True
    return False


def is_blacklisted(cfg, uid, nick=""):
    """是否在黑名单里（按 QQ 号或昵称匹配）"""
    def match(target):
        if isinstance(target, int):
            return uid == target
        s = str(target).lower()
        return (s in str(uid).lower()) or (nick and s in nick.lower())

    return any(match(b) for b in cfg.get("blacklist", []))


def is_allowed(app, event):
    cfg = app["cfg"]
    uid = event.get("user_id")
    nick = (event.get("sender") or {}).get("nickname", "")

    def match(target):
        if isinstance(target, int):
            return uid == target
        s = str(target).lower()
        return (s in str(uid).lower()) or (nick and s in nick.lower())

    if is_blacklisted(cfg, uid, nick):
        return False
    wl = cfg.get("whitelist", [])
    if wl and not any(match(w) for w in wl):
        return False
    return True


def is_duplicate(app, event, text):
    """按 message_id 去重；相同内容 60 秒内也算重复"""
    now = time.time()
    seen = app["seen"]
    for k in list(seen):
        if now - seen[k] > 3600:
            del seen[k]
    mid = event.get("message_id")
    if mid and mid in seen:
        return True
    h = hashlib.md5(text.encode('utf-8')).hexdigest()
    hk = f"h_{h}"
    if hk in seen and now - seen[hk] < 60:
        return True
    if mid:
        seen[mid] = now
    seen[hk] = now
    return False


# ============================================================
# 发送 / 搜索
# ============================================================
def load_stickers():
    """加载本地表情包库（emojis/ 目录下的图片）"""
    d = BASE / "emojis"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            out.append(str(p))
    return out


def rich_segments(app, text):
    """把含 [face:ID] / [sticker] 标记的回复文本转成 OneBot 消息段列表"""
    # [sticker] 后面 LLM 有时会补一句（描述），剥掉，只发图不带说明文字
    text = re.sub(r"\[sticker\]\s*[（(][^）)]*[）)]", "[sticker]", text)
    segs = []
    pos = 0
    for m in re.finditer(r"\[face:(\d+)\]|\[sticker(?::(\d+))?\]", text):
        if m.start() > pos:
            seg = text[pos:m.start()]
            if seg.strip():
                segs.append({"type": "text", "data": {"text": seg}})
        if m.group(1) is not None:
            segs.append({"type": "face", "data": {"id": m.group(1)}})
        else:
            stickers = app.get("stickers") or []
            if stickers:
                segs.append({"type": "image", "data": {"file": random.choice(stickers), "sub_type": 1}})
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        segs.append({"type": "text", "data": {"text": tail}})
    if not segs:
        segs.append({"type": "text", "data": {"text": text}})
    return segs


async def send_segments(app, kind, user_id, group_id, segments):
    """发送一组消息段，并记录纯文本哈希用于回显去重"""
    cfg = app["cfg"]
    base = cfg["onebot"]["http_url"].rstrip("/")
    if kind == "private":
        url = f"{base}/send_private_msg"
        body = {"user_id": user_id, "message": segments}
    else:
        url = f"{base}/send_group_msg"
        body = {"group_id": group_id, "message": segments}
    try:
        async with app["session"].post(url, json=body, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                log.warning("发送失败 HTTP %s", r.status)
                return False
            # 记录纯文本内容的哈希，供 handle_event 跳过回显（自聊测试防死循环）
            now = time.time()
            txt = "".join(s.get("data", {}).get("text", "") for s in segments if s.get("type") == "text").strip()
            sh = app.setdefault("sent_hashes", {})
            sh[hashlib.md5(txt.encode("utf-8")).hexdigest()] = now
            for k in list(sh):
                if now - sh[k] > 120:
                    del sh[k]
            return True
    except Exception as e:
        log.warning("发送异常: %s", e)
        return False


async def send_text(app, kind, user_id, group_id, text):
    """发送纯文本消息（兼容旧调用）"""
    return await send_segments(app, kind, user_id, group_id, [{"type": "text", "data": {"text": text}}])


async def do_search(app, query):
    cfg = app["cfg"]
    if not cfg.get("bing_search", {}).get("enabled"):
        return None
    try:
        return await bing_search.search(query, max_results=cfg["bing_search"].get("max_results", 5))
    except Exception as e:
        log.warning("搜索失败: %s", e)
        return None


# ============================================================
# 消息批处理（合并 3 秒内的多条消息，实现「最新/最重要」优先级）
# ============================================================
async def enqueue(app, conv_id, batch):
    """把消息塞进对应会话的合并队列，刷新合并计时器"""
    pending = app["pending"]
    p = pending.get(conv_id)
    if p is None:
        p = {"msgs": [], "kind": batch["kind"], "user_id": batch["user_id"], "group_id": batch["group_id"]}
        pending[conv_id] = p
    else:
        p["kind"] = batch["kind"]
        p["user_id"] = batch["user_id"]
        p["group_id"] = batch["group_id"]
    p["msgs"].extend(batch["msgs"])
    if p.get("task"):
        p["task"].cancel()
    p["task"] = asyncio.create_task(flush(app, conv_id))


async def flush(app, conv_id):
    cfg = app["cfg"]
    try:
        await asyncio.sleep(cfg.get("batch_window_seconds", 3))
    except asyncio.CancelledError:
        return
    p = app["pending"].pop(conv_id, None)
    if not p or not p["msgs"]:
        return
    await process_batch(app, conv_id, p)


async def process_batch(app, conv_id, p):
    cfg = app["cfg"]
    brain = app["brain"]
    now = time.time()

    async with app["brain_lock"]:
        # 0. 彻底静默（手动 silence 的会话）：对方发消息也不回，也不解除静默
        if brain.is_silenced(conv_id):
            log.info("彻底静默会话，跳过 %s", conv_id)
            return

        # 1. 之前被静默，对方又发消息了 → 解除静默，恢复正常
        if brain.is_muted(conv_id):
            brain.unmute(conv_id)

        # 2. 最新一条是「明确结束话题」→ 进入静默：不回复、也不再主动找话题
        latest = p["msgs"][-1]["text"] if p["msgs"] else ""
        if brain.detect_end(latest):
            brain.mute(conv_id)
            brain.save_memory(conv_id)
            log.info("对方提出结束话题，静默等待 %s", conv_id)
            return

        # 3. 冷却检查（避免刷屏）
        if now - app["cooldowns"].get(conv_id, 0) < cfg.get("cooldown_seconds", 5):
            brain.save_memory(conv_id)
            return

        # 4. 正常回复（可能一次连发多条）
        replies = await brain.reply(conv_id, p["msgs"], search_fn=lambda q: do_search(app, q))
        if replies:
            for i, r in enumerate(replies):
                if i > 0:
                    await asyncio.sleep(1.2)  # 多条之间隔一下，像真人连发
                ok = await send_segments(app, p["kind"], p["user_id"], p["group_id"], rich_segments(app, r))
                if ok:
                    app["cooldowns"][conv_id] = time.time()
                    log.info("回复 %s: %s", conv_id, r[:80])
                else:
                    log.warning("回复发送失败 %s", conv_id)
        brain.save_memory(conv_id)


# ============================================================
# 事件处理
# ============================================================
async def handle_event(app, event):
    # 全局静默（stopped）：bot 完全不回任何消息，也不响应。
    if app["cfg"].get("stopped"):
        return
    pt = event.get("post_type")
    self_id = event.get("self_id")

    # 常规收到的消息 post_type=message；自己账号发出的消息（reportSelfMessage=true）
    # 会上报为 post_type=message_sent。自聊测试里手机端的回复就是 message_sent(self)。
    if pt == "message":
        is_self_sent = False
    elif pt == "message_sent":
        # 只处理「发给自己」的自聊上报（target_id == 自己QQ）；发给别人的都是机器人自己发出的消息回显，直接跳过，防止串台
        if str(event.get("target_id")) != str(self_id):
            return
        is_self_sent = True
    else:
        return

    text = extract_text(event)
    if not text:
        return

    # 回显去重：机器人自己刚发出去的内容会原样回显，跳过（防死循环）；
    # 手机端用同一账号回复的消息（内容不同）不在 sent_hashes 里，正常处理。
    h = hashlib.md5(text.encode("utf-8")).hexdigest()
    if is_self_sent and h in app.get("sent_hashes", {}):
        return

    cfg = app["cfg"]
    mt = event.get("message_type")
    if mt == "private":
        uid = event.get("user_id")
        conv_id = f"private_{uid}"
        kind, gid = "private", None
    elif mt == "group":
        if cfg.get("group_only_at", True) and not is_at_me(event):
            return
        uid = event.get("user_id")
        gid = event.get("group_id")
        conv_id = f"group_{gid}_{uid}"
        kind = "group"
    else:
        return

    if not is_allowed(app, event):
        return

    if is_duplicate(app, event, text):
        return

    brain = app["brain"]
    brain.set_meta(conv_id, kind, user_id=uid, group_id=gid)
    brain.touch(conv_id)

    log.info("收到 %s: %s", conv_id, text[:80])
    await enqueue(app, conv_id, {
        "msgs": [{"text": text, "ts": time.time()}],
        "kind": kind, "user_id": uid, "group_id": gid,
    })


# ============================================================
# 主动找话题（对方长时间没回）
# ============================================================
async def idle_loop(app):
    cfg = app["cfg"]
    brain = app["brain"]
    while True:
        try:
            await asyncio.sleep(30)
            if not cfg.get("idle_topic", {}).get("enabled"):
                continue
            if cfg.get("stopped"):
                continue
            for conv_id in brain.list_active():
                if not brain.should_nudge(conv_id):
                    continue
                meta = brain.get_meta(conv_id)
                # 黑名单里的人不主动找话题（回复在 is_allowed 已拦）
                if is_blacklisted(cfg, meta.get("user_id")):
                    continue
                async with app["brain_lock"]:
                    topic = await brain.find_topic(conv_id, search_fn=lambda q: do_search(app, q))
                    if not topic:
                        continue
                    ok = await send_segments(app, meta.get("kind", "private"), meta.get("user_id"), meta.get("group_id"), rich_segments(app, topic))
                    if ok:
                        brain.mark_nudged(conv_id)
                        brain.save_memory(conv_id)
                        log.info("主动找话题 %s: %s", conv_id, topic[:80])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("idle 循环异常: %s", e)


# ============================================================
# WebSocket 服务 + 应用
# ============================================================
async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    log.info("NapCatQQ 已连接 (WebSocket)")
    async for msg in ws:
        if msg.type == web.WSMsgType.TEXT:
            try:
                event = json.loads(msg.data)
            except Exception:
                continue
            log.info("[RAW] %s", json.dumps(event, ensure_ascii=False)[:500])
            asyncio.create_task(handle_event(request.app, event))
        elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
            break
    log.info("NapCatQQ 已断开")
    return ws


def build_app(cfg):
    app = web.Application()
    app["cfg"] = cfg

    async def on_startup(app):
        app["session"] = aiohttp.ClientSession()
        app["brain"] = Brain(cfg, app["session"])
        app["brain_lock"] = asyncio.Lock()
        app["pending"] = {}
        app["cooldowns"] = {}
        app["seen"] = {}
        app["sent_hashes"] = {}   # 机器人自己发过的消息内容哈希 → 时间，用于自聊测试里跳过回显
        app["stickers"] = load_stickers()   # 本地表情包库
        app["idle_task"] = asyncio.create_task(idle_loop(app))

    async def on_cleanup(app):
        if app.get("idle_task"):
            app["idle_task"].cancel()
        await app["session"].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    # NapCatQQ 反向 WS 默认连到 "/"，也兼容 "/onebot/v11/ws"
    app.router.add_get("/", ws_handler)
    app.router.add_get("/onebot/v11/ws", ws_handler)
    return app


# ============================================================
# 入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='🤖 QQ AI 自动回复机器人')
    parser.add_argument('-c', '--config', help='配置文件路径')
    parser.add_argument('--init', action='store_true', help='生成配置文件')
    args = parser.parse_args()

    if args.init:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"✅ 已生成配置文件: {CONFIG_FILE}")
        print("   请填入 llm.api_key、whitelist 后运行 python qq_bot.py")
        return

    cfg = load_config(args.config)
    if not cfg["llm"]["api_key"]:
        print("⚠️  未配置 llm.api_key！请编辑 qq_config.json 填入 DeepSeek API Key")
        return

    host = cfg["onebot"]["ws_host"]
    port = cfg["onebot"]["ws_port"]

    print()
    print("=" * 60)
    print("  🤖 QQ AI 自动回复机器人")
    print("=" * 60)
    print(f"  WebSocket 监听:   ws://{host}:{port}")
    print(f"  NapCatQQ HTTP:    {cfg['onebot']['http_url']}")
    print(f"  LLM 模型:         {cfg['llm']['model']}")
    print(f"  白名单:           {cfg['whitelist'] or '(空=回复所有人)'}")
    print(f"  联网搜索:         {'开' if cfg['bing_search']['enabled'] else '关'}")
    print(f"  主动找话题:       {'开' if cfg['idle_topic']['enabled'] else '关'}")
    print("=" * 60)
    print("  按 Ctrl+C 停止\n")

    app = build_app(cfg)
    web.run_app(app, host=host, port=port)


if __name__ == '__main__':
    main()
