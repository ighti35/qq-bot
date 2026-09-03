#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 控制台 —— 在电脑上直接和机器人对话

一边打字发消息，一边实时显示机器人收到的消息和回复（抓取 qq_bot 的日志）。

用法:
    python console.py                    # 默认发给机器人自己（自聊测试）
    python console.py --to 好友昵称      # 发给指定好友（按昵称/备注/QQ号）

命令:
    直接输入文字            → 发给当前目标
    /to <昵称或QQ号>        → 切换目标
    /friends                → 列出好友
    /whoami                 → 查看登录账号
    /clear                  → 清屏
    /exit 或 Ctrl+C         → 退出

前置: NapCatQQ 运行中，HTTP API 在 127.0.0.1:3000；qq_bot.py 运行中（用于实时回复显示）
"""

import sys
import io
import os
import json
import time
import re
import threading
import urllib.request
import urllib.error

# 修复 Windows 控制台 GBK 编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

HTTP = "http://127.0.0.1:3000"
BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, "logs")

# 颜色（Windows 10+ 支持 ANSI）
C = {
    "me": "\033[92m",     # 我（控制台发送）
    "bot": "\033[96m",    # 机器人回复
    "recv": "\033[93m",   # 收到消息
    "sys": "\033[90m",    # 系统
    "reset": "\033[0m",
}


def api(action, params=None, method="GET"):
    url = f"{HTTP}/{action}"
    data = None
    if method == "POST":
        data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def get_self_id():
    try:
        res = api("get_login_info")
        return res.get("data", {}).get("user_id"), res.get("data", {}).get("nickname")
    except Exception:
        return None, None


def get_friends():
    try:
        res = api("get_friend_list")
        return res.get("data", [])
    except Exception:
        return []


def resolve_target(name, friends):
    s = str(name).strip()
    # 纯数字当 QQ 号
    if s.isdigit():
        return int(s)
    for f in friends:
        nick = str(f.get("nickname", ""))
        remark = str(f.get("remark", ""))
        if s in nick or s in remark:
            return f.get("user_id")
    return None


def send_private(user_id, text):
    api("send_private_msg", {
        "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }, method="POST")


# ---------------- 实时日志显示 ----------------
def tail_log(stop):
    """后台线程：抓取 qq_bot 日志，实时打印收/发消息"""
    today = time.strftime("%Y%m%d")
    logfile = os.path.join(LOG_DIR, f"qq_bot_{today}.log")
    pos = 0
    # 初次打开，从文件尾开始（只显示新消息）
    try:
        with open(logfile, encoding="utf-8") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
    except Exception:
        pos = 0

    while not stop.is_set():
        try:
            if not os.path.exists(logfile):
                time.sleep(1)
                continue
            with open(logfile, encoding="utf-8") as f:
                f.seek(pos)
                for line in f:
                    line = line.rstrip("\n")
                    if "收到 " in line and ":" in line:
                        m = re.search(r"收到 \S+: (.+)", line)
                        if m:
                            print(f"{C['recv']}⇐ 收到: {m.group(1)}{C['reset']}", flush=True)
                    elif "回复 " in line and ":" in line:
                        m = re.search(r"回复 \S+: (.+)", line)
                        if m:
                            print(f"{C['bot']}🤖 机器人: {m.group(1)}{C['reset']}", flush=True)
                pos = f.tell()
        except Exception:
            pass
        time.sleep(0.5)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", help="目标好友（昵称/备注/QQ号），默认机器人自己")
    args = ap.parse_args()

    # 1. 检查 NapCat HTTP API
    try:
        self_id, nickname = get_self_id()
    except Exception as e:
        print(f"{C['sys']}[失败] 连不上 NapCatQQ HTTP API ({HTTP})：{e}{C['reset']}")
        print(f"{C['sys']}       请先启动 NapCatQQ。{C['reset']}")
        return 1

    if not self_id:
        print(f"{C['sys']}[失败] 未获取到登录信息，NapCatQQ 可能未登录。{C['reset']}")
        return 1

    friends = get_friends()

    # 2. 确定目标
    target = self_id
    target_label = f"自己 ({nickname})"
    if args.to:
        t = resolve_target(args.to, friends)
        if not t:
            print(f"{C['sys']}[失败] 好友里没找到「{args.to}」{C['reset']}")
            return 1
        target = t
        target_label = args.to

    # 3. 启动日志显示线程
    stop = threading.Event()
    t = threading.Thread(target=tail_log, args=(stop,), daemon=True)
    t.start()

    # 4. 横幅
    print()
    print("=" * 60)
    print(f"  💬 QQ 控制台")
    print(f"  登录账号: {nickname} (QQ:{self_id})")
    print(f"  当前目标: {target_label}")
    print(f"  直接输入文字发送；/friends 看好友；/to 切目标；/exit 退出")
    print("=" * 60)
    print()

    # 5. 主循环
    while True:
        try:
            line = input(f"{C['me']}你 ➜ {C['reset']}")
        except (EOFError, KeyboardInterrupt):
            line = "/exit"

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            cmd = line.split(None, 1)
            c = cmd[0].lower()
            if c in ("/exit", "/quit", "/q"):
                break
            elif c == "/whoami":
                print(f"{C['sys']}账号: {nickname} (QQ:{self_id})，目标: {target_label}{C['reset']}")
            elif c == "/friends":
                print(f"{C['sys']}好友列表（共 {len(friends)} 人）：{C['reset']}")
                for f in friends:
                    print(f"   {f.get('nickname')} (备注:{f.get('remark') or '-'}) QQ:{f.get('user_id')}")
            elif c == "/clear":
                os.system("cls" if os.name == "nt" else "clear")
            elif c == "/to":
                if len(cmd) < 2:
                    print(f"{C['sys']}用法: /to <昵称或QQ号>{C['reset']}")
                    continue
                t = resolve_target(cmd[1], friends)
                if not t:
                    print(f"{C['sys']}[失败] 好友里没找到「{cmd[1]}」{C['reset']}")
                    continue
                target = t
                target_label = cmd[1]
                print(f"{C['sys']}已切换目标 → {target_label}{C['reset']}")
            else:
                print(f"{C['sys']}未知命令 {c}。可用: /to /friends /whoami /clear /exit{C['reset']}")
            continue

        # 普通消息 → 发送
        try:
            send_private(target, line)
            print(f"{C['me']}你 ➜ {target_label}: {line}{C['reset']}", flush=True)
        except Exception as e:
            print(f"{C['sys']}[失败] 发送失败: {e}{C['reset']}")

    stop.set()
    print(f"\n{C['sys']}已退出控制台。{C['reset']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
