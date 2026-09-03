# -*- coding: utf-8 -*-
"""
手动给 QQ 好友发消息（带表情转换）
用法：
  python manual_send.py <qq号> "<文本，可含 [face:ID] 或 [sticker]>"

跟 bot 一样走 rich_segments 转换，这样 [face:20] 会变成真正的 QQ 表情、
[sticker] 会变成收藏表情包，不会被当成字面文本发出去。
"""

import re
import sys
import io
import os
import json
import random
import asyncio
from pathlib import Path

import aiohttp

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent


def load_config():
    p = BASE / "qq_config.json"
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_stickers():
    d = BASE / "emojis"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            out.append(str(p))
    return out


def rich_segments(text, stickers):
    """把含 [face:ID] / [sticker] 标记的文本转成 OneBot 消息段列表（与 bot 一致）"""
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
            if stickers:
                segs.append({"type": "image", "data": {"file": random.choice(stickers), "sub_type": 1}})
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        segs.append({"type": "text", "data": {"text": tail}})
    if not segs:
        segs.append({"type": "text", "data": {"text": text}})
    return segs


async def main():
    if len(sys.argv) < 3:
        print("用法: python manual_send.py <qq号> \"<文本>\"")
        sys.exit(1)
    user_id = int(sys.argv[1])
    text = " ".join(sys.argv[2:])

    cfg = load_config()
    stickers = load_stickers() or []
    segs = rich_segments(text, stickers)
    payload = {"user_id": user_id, "message": segs}

    base = cfg["onebot"]["http_url"].rstrip("/")
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base}/send_private_msg", json=payload, timeout=20) as r:
            data = await r.json()
            if data.get("status") == "ok":
                print(f"发送成功 -> {user_id}: message_id={data.get('data', {}).get('message_id')}")
            else:
                print(f"发送失败 status={data.get('status')} msg={data.get('msg')}")


if __name__ == "__main__":
    asyncio.run(main())
