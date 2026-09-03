# -*- coding: utf-8 -*-
"""
QQ 机器人 · 大脑模块
负责：
  1. 调用 DeepSeek 大模型生成回复
  2. 长期记忆：记录并学习对方习惯/兴趣/事实（存 JSON）
  3. 消息优先级：优先回应「最新消息」，其次是 AI 判定「最重要」的未答问题
  4. 话题切换：跟随最新话题，绝不停留在旧话题
  5. 主动找话题：对方长时间不回时起一个新话题
"""

import json
import re
import aiohttp
from pathlib import Path
from datetime import datetime, timedelta

MEMORY_DIR = Path(__file__).parent / "memory"

DEFAULT_PROFILE = {
    "name": None,
    "interests": [],       # 兴趣爱好
    "facts": [],           # 关于对方的事实
    "tone": "",            # 对方聊天风格
    "preferred_topics": [],  # 常聊话题
}


def _extract_json(text):
    """从 LLM 输出里抠出第一个合法 JSON 对象（容忍 markdown 围栏等噪声）"""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def _split_replies(text):
    """把 LLM 输出按「单独一行 ---」拆成多条消息；无分隔符时返回单条"""
    if not text:
        return []
    parts = re.split(r"^[ \t]*---+[ \t]*$", text, flags=re.MULTILINE)
    out = []
    for p in parts:
        p = p.strip().strip('"').strip("「").strip("」").strip()
        if p:
            out.append(p)
    return out


def _empty_memory(conv_id):
    return {
        "conv_id": conv_id,
        "meta": {},  # kind / user_id / group_id，用于发消息时定位目标
        "updated": datetime.now().isoformat(),
        "profile": dict(DEFAULT_PROFILE),
        "summary": "",                 # 滚动对话摘要（最近聊到啥）
        "history": [],                 # [{role, content, time}] 最近 N 条
        "turn_count": 0,
        "last_other_active": None,     # 对方最近一次发消息时间
        "last_bot_nudge": None,        # 上次主动找话题时间
        "nudges_today": 0,
        "nudge_date": "",
        "muted": False,  # 对方明确说结束话题后静默，等待对方再次发消息
        "silenced": False,  # 手动彻底静默：对方发消息也不回、也不主动找话题
    }


class Brain:
    def __init__(self, cfg, session):
        self.cfg = cfg
        self.session = session
        self._mem_cache = {}

    # ============================================================
    # 记忆存取
    # ============================================================
    def _mem_path(self, conv_id):
        safe = re.sub(r"[^\w\-]", "_", conv_id)
        return MEMORY_DIR / f"conv_{safe}.json"

    def get_memory(self, conv_id):
        if conv_id in self._mem_cache:
            return self._mem_cache[conv_id]
        p = self._mem_path(conv_id)
        if p.exists():
            try:
                mem = json.loads(p.read_text(encoding="utf-8"))
                mem.setdefault("profile", dict(DEFAULT_PROFILE))
                mem.setdefault("history", [])
                self._mem_cache[conv_id] = mem
                return mem
            except Exception:
                pass
        mem = _empty_memory(conv_id)
        self._mem_cache[conv_id] = mem
        return mem

    def save_memory(self, conv_id):
        mem = self.get_memory(conv_id)
        mem["updated"] = datetime.now().isoformat()
        MEMORY_DIR.mkdir(exist_ok=True)
        self._mem_path(conv_id).write_text(
            json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")

    def touch(self, conv_id):
        """对方发来消息时调用，刷新活跃时间"""
        mem = self.get_memory(conv_id)
        mem["last_other_active"] = datetime.now().isoformat()

    def set_meta(self, conv_id, kind, user_id=None, group_id=None):
        mem = self.get_memory(conv_id)
        mem["meta"] = {"kind": kind}
        if user_id is not None:
            mem["meta"]["user_id"] = user_id
        if group_id is not None:
            mem["meta"]["group_id"] = group_id

    def get_meta(self, conv_id):
        return self.get_memory(conv_id).get("meta", {})

    def is_muted(self, conv_id):
        return bool(self.get_memory(conv_id).get("muted"))

    def mute(self, conv_id):
        """对方明确说结束话题后进入静默"""
        mem = self.get_memory(conv_id)
        mem["muted"] = True
        mem["muted_at"] = datetime.now().isoformat()

    def unmute(self, conv_id):
        mem = self.get_memory(conv_id)
        mem["muted"] = False

    def is_silenced(self, conv_id):
        """是否被彻底静默（手动标记）：对方发消息也不回，也不主动找话题"""
        return bool(self.get_memory(conv_id).get("silenced"))

    def silence(self, conv_id):
        mem = self.get_memory(conv_id)
        mem["silenced"] = True
        mem["silenced_at"] = datetime.now().isoformat()

    def unsilence(self, conv_id):
        mem = self.get_memory(conv_id)
        mem["silenced"] = False

    def detect_end(self, text):
        """判断对方是否明确提出结束话题（要睡了/再见/不聊了等）"""
        if not text:
            return False
        kws = self.cfg.get("conversation_end_keywords", [])
        return any(k in text for k in kws)

    def list_active(self):
        """返回 72 小时内有过对话的会话 id（用于主动找话题遍历）"""
        out = []
        if not MEMORY_DIR.exists():
            return out
        for f in MEMORY_DIR.glob("conv_*.json"):
            try:
                mem = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            last = mem.get("last_other_active")
            if not last:
                continue
            try:
                last_dt = datetime.fromisoformat(last)
            except Exception:
                continue
            if datetime.now() - last_dt < timedelta(hours=72):
                out.append(mem.get("conv_id") or f.stem[5:])
        return out

    # ============================================================
    # LLM 调用
    # ============================================================
    async def llm(self, messages, temperature=None, max_tokens=None):
        llm_cfg = self.cfg["llm"]
        body = {
            "model": llm_cfg["model"],
            "messages": messages,
            "temperature": temperature if temperature is not None else llm_cfg.get("temperature", 0.9),
            "max_tokens": max_tokens or llm_cfg.get("max_tokens", 300),
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {llm_cfg['api_key']}",
        }
        try:
            async with self.session.post(
                llm_cfg["api_url"], json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                return data["choices"][0]["message"]["content"].strip()
        except Exception:
            return None

    # ============================================================
    # 回复生成（核心）
    # ============================================================
    async def reply(self, conv_id, incoming, search_fn=None):
        """根据一批新消息生成一条回复。

        incoming: [{"text": str, "ts": float}, ...]  按时间从旧到新
        """
        mem = self.get_memory(conv_id)
        now = datetime.now().isoformat()
        for m in incoming:
            mem["history"].append({"role": "user", "content": m["text"], "time": now})

        maxh = self.cfg.get("max_history", 40)
        if len(mem["history"]) > maxh:
            mem["history"] = mem["history"][-maxh:]
        mem["turn_count"] += 1

        # 会话级 persona 覆盖：若该会话存了 persona_override（如「猫娘模式」）就用它，
        # 否则回退到全局 persona（如串子模式）。避免改全局把其他会话一起带偏。
        persona = mem.get("persona_override") or self.cfg.get("persona", "")
        system = (
            persona + "\n\n"
            + f"【当前时间】{self._current_time()}\n"
            + self._format_profile(mem) + "\n\n"
            + self._reply_rules()
        )
        user_parts = [self._format_transcript(mem["history"])]

        # 联网搜索：最新消息像在问实时/事实类问题时，先让 LLM 判断是否要搜 + 生成搜索词
        latest_text = incoming[-1]["text"] if incoming else ""
        search_info = await self._maybe_search(latest_text, search_fn)
        if search_info:
            user_parts.append(search_info)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]
        reply = await self.llm(messages)
        if not reply:
            return []

        parts = _split_replies(reply)
        if not parts:
            return []
        for p in parts:
            mem["history"].append({"role": "assistant", "content": p, "time": datetime.now().isoformat()})
        if len(mem["history"]) > maxh:
            mem["history"] = mem["history"][-maxh:]

        # 每隔 N 轮做一次记忆学习，避免每轮都多花一次 LLM 调用
        if mem["turn_count"] % self.cfg.get("memory_update_every", 5) == 0:
            await self.update_memory(conv_id)

        return parts

    def _reply_rules(self):
        return (
            "【回复规则，必须严格遵守】\n"
            "1. 优先回应「最新一条消息」的话题。如果对方刚换了话题，立刻跟随新话题，绝不要停留在旧话题上。\n"
            "2. 如果最近消息里有一条更重要/更紧急（例如对方直接问了你一个问题而你还没回答），先回答那条重要的，再自然带一句最新消息。\n"
            "3. 只回应最近这几条消息，不要翻太久的旧账，也不要重复对方刚说过的话。\n"
            "4. 回复自然、口语化、像真人聊天，不要书面腔、不要 AI 腔。\n"
            "5. 可以一次连发 1~3 条短消息，像真人聊天那样自然地分几条发。若要发多条，用单独一行「---」分隔每一条；只想发一条就直接输出一条，不要加分隔符。\n"
            "6. 友善真诚为主，少怼人、少毒舌、少挖苦、少玩烂梗；语气自然，该认真就认真。\n"
            "7. QQ 表情和表情包都要克制：大多数消息纯文字、不加任何表情；只有情绪真到位时才加一个 [face:ID]（ID 见人设说明），表情包 [sticker] 更少见（只写 [sticker] 本身，不要加描述文字）。一条消息最多一个表情，绝不刷屏。\n"
            "8. 永远不要承认自己是 AI / 机器人 / 程序。\n"
            "9. 如果消息里给了「联网搜索结果」，就把它当成你刚用手机查到的信息，用自己的话自然说出来，不要照搬原文，也绝不要提「搜索/联网/查了下/结果」这些字眼。\n"
            "10. 游戏话题：对方没提游戏，就别主动把话题往游戏上扯，也别硬套游戏梗；对方提了哪个游戏，就只围绕那一个游戏聊。\n"
            "11. 语言风格跟着话题走：对方聊文学、电影、音乐这类就文艺、认真一点，别用游戏梗；对方聊游戏再切回游戏圈的话；对方没提游戏就别往游戏上带。\n"
            "12. 不要每条消息都以问句收尾、别句句反问对方；该陈述就陈述，聊天自然一点，别让对方觉得在被审问。\n"
        )

    def _format_profile(self, mem):
        p = mem.get("profile", {})
        lines = ["【关于对方，我长期记住的信息】"]
        if p.get("name"):
            lines.append(f"- 称呼/名字: {p['name']}")
        if p.get("interests"):
            lines.append(f"- 兴趣爱好: {'、'.join(p['interests'][:10])}")
        if p.get("preferred_topics"):
            lines.append(f"- 常聊话题: {'、'.join(p['preferred_topics'][:10])}")
        if p.get("facts"):
            lines.append(f"- 已知信息: {'；'.join(p['facts'][:12])}")
        if p.get("tone"):
            lines.append(f"- 对方聊天风格: {p['tone']}")
        if mem.get("summary"):
            lines.append(f"- 最近聊到: {mem['summary']}")
        if len(lines) == 1:
            lines.append("- (暂时还没积累到什么信息)")
        return "\n".join(lines)

    def _format_transcript(self, history):
        lines = ["【最近的消息（按时间从旧到新，最后一条是最新）】"]
        for h in history[-20:]:
            who = "对方" if h["role"] == "user" else "我"
            lines.append(f"{who}: {h['content']}")
        if history:
            last = history[-1]
            who = "对方" if last["role"] == "user" else "我"
            lines.append(f"（最新一条消息是：{who} 说「{last['content']}」）")
        return "\n".join(lines)

    def _current_time(self):
        d = datetime.now()
        wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][d.weekday()]
        return f"{d.strftime('%Y-%m-%d %H:%M:%S')}（{wd}）"

    def _is_time_question(self, text):
        kws = ("几点", "几点了", "现在时间", "几号", "星期几", "周几", "几月几号", "今天日期", "现在日期", "几几年", "哪一年")
        return any(k in text for k in kws)

    def _looks_like_question(self, text):
        """便宜的前置判断：是不是「值得考虑搜索」的问题型消息。

        只做必要条件的粗筛，避免每条消息都多花一次 LLM 调用去判断。
        真正的「要不要搜」交给 _search_query 里的 LLM 决定。
        """
        if not text or len(text) < 2:
            return False
        # 问句标记
        if any(k in text for k in ("？", "?", "吗", "呢", "嘛", "多少", "几", "谁", "哪", "怎么", "什么", "为什么", "是不是", "有没有")):
            return True
        # 时效/事实类触发词（配置里）
        kws = self.cfg.get("bing_search", {}).get("search_keywords", [])
        return any(k in text for k in kws)

    async def _search_query(self, text):
        """让 LLM 判断是否需要联网，需要则返回一个干净的搜索词，不需要返回 None"""
        messages = [
            {"role": "system", "content": "判断下面这条聊天消息是否需要联网搜索实时/事实信息才能答好。需要就只输出一个简短中文搜索词（3~10 字，去掉人称和语气词）；不需要就只输出 NO。不要输出别的。"},
            {"role": "user", "content": text},
        ]
        out = await self.llm(messages, temperature=0, max_tokens=20)
        if not out:
            return None
        out = out.strip().strip('"').strip("「").strip("」").strip()
        if not out or out.upper() in ("NO", "NONE", "不需要", "无需", "不用") or "不需要" in out or "不用搜" in out:
            return None
        out = re.sub(r"^(搜索|搜|查|帮我查|查一下)[：: ]*", "", out)
        return out[:40] if out else None

    async def _maybe_search(self, text, search_fn):
        if not search_fn or not text:
            return None
        if self._is_time_question(text):
            return None  # 时间/日期问题机器人自己知道（上下文已注入当前时间），不用搜
        if not self._looks_like_question(text):
            return None
        query = await self._search_query(text)
        if not query:
            return None
        results = await search_fn(query)
        if not results:
            return None
        return self._format_search(query, results)

    def _format_search(self, query, results):
        lines = [f"【联网搜索结果（关于「{query}」）】"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title', '')}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet'][:200]}")
        lines.append("（把这些结果当成你刚用手机查到的，用自己的话、口语化地答，不要照搬原文，也绝不要提「搜索/联网/结果」这些字眼）")
        return "\n".join(lines)

    # ============================================================
    # 记忆学习：从对话中提取对方习惯/兴趣/事实
    # ============================================================
    async def update_memory(self, conv_id):
        mem = self.get_memory(conv_id)
        transcript = self._format_transcript(mem["history"][-12:])
        prompt = (
            "根据下面的对话，更新你对「对方」的长期记忆。请严格只输出一个 JSON 对象，不要输出任何解释或多余文字。\n"
            "JSON 结构：\n"
            '{"name": 对方称呼或名字或null, "interests": [新发现的兴趣爱好], '
            '"preferred_topics": [对方喜欢/常聊的话题], "facts": [关于对方的新事实], '
            '"tone": 对方的聊天风格(简短描述), "summary": 用一句话概括最近聊的内容}\n'
            "没有新信息的字段给空数组 [] 或 null。所有内容用中文。\n\n" + transcript
        )
        messages = [
            {"role": "system", "content": "你是一个记忆提取助手，只输出合法 JSON，不要输出任何其他内容。"},
            {"role": "user", "content": prompt},
        ]
        out = await self.llm(messages, temperature=0.2, max_tokens=400)
        data = _extract_json(out)
        if not data:
            return

        p = mem.setdefault("profile", dict(DEFAULT_PROFILE))
        if data.get("name"):
            p["name"] = str(data["name"]).strip()
        for key in ("interests", "preferred_topics", "facts"):
            vals = data.get(key) or []
            if not isinstance(vals, list):
                continue
            cur = p.setdefault(key, [])
            for v in vals:
                v = str(v).strip()
                if v and v not in cur:
                    cur.append(v)
            p[key] = cur[:20]
        if data.get("tone"):
            p["tone"] = str(data["tone"]).strip()
        if data.get("summary"):
            mem["summary"] = str(data["summary"]).strip()

    # ============================================================
    # 主动找话题（对方长时间没回时）
    # ============================================================
    async def find_topic(self, conv_id, search_fn=None):
        mem = self.get_memory(conv_id)
        parts = [
            "对方已经有一阵子没回消息了。请你用「我」的身份，自然、不刻意地起一个新话题，主动给对方发一条消息，把天聊起来。",
            "要求：\n"
            "1. 优先结合你记住的对方兴趣/常聊话题来找话题。\n"
            "2. 语气自然，像朋友突然想到什么就发一句，不要问「在吗」「最近怎么样」这种尬聊开场。\n"
            "3. 只输出你要发的那条消息本身，不要加任何解释、引号或前缀。\n"
            "4. 简短，不超过 60 字。\n"
            "5. 不要聊工作、上班、加班、游戏这些话题（就算对方兴趣里存着游戏也别聊，尤其别提只狼、弦一郎）；也不要聊凶杀、事故、灾难、猎奇、八卦这类奇怪的新闻，更不要编造事实或提未经证实的猎奇传闻、历史穿越之类的脑洞，只聊真实、日常、自然的生活话题。",
            "6. 不要用问句收尾，直接陈述即可，像朋友顺口说一句那样自然。",
            "",
            self._format_profile(mem),
        ]
        if search_fn:
            try:
                results = await search_fn("今日轻松话题")
                if results:
                    parts += ["", "可以参考下面这些轻松话题，挑一个自然切入（别提搜索）：", self._format_search("轻松话题", results)]
            except Exception:
                pass
        messages = [
            {"role": "system", "content": self.cfg.get("persona", "") + f"\n\n【当前时间】{self._current_time()}"},
            {"role": "user", "content": "\n".join(parts)},
        ]
        topic = await self.llm(messages, temperature=0.85, max_tokens=120)
        if not topic:
            return None
        return topic.strip().strip('"').strip("「").strip("」")

    def should_nudge(self, conv_id):
        it = self.cfg.get("idle_topic", {})
        if not it.get("enabled"):
            return False
        mem = self.get_memory(conv_id)
        # 静默中（对方明确说结束话题），不主动找话题
        if mem.get("muted"):
            return False
        # 被彻底静默（手动标记），不主动找话题
        if mem.get("silenced"):
            return False
        # 默认只对私聊主动找话题，避免在群里刷屏
        if mem.get("meta", {}).get("kind") != "private":
            return False
        last = mem.get("last_other_active")
        if not last:
            return False
        try:
            last_dt = datetime.fromisoformat(last)
        except Exception:
            return False
        idle = it.get("idle_seconds", 1800)
        if (datetime.now() - last_dt).total_seconds() < idle:
            return False
        if mem.get("last_bot_nudge"):
            try:
                lb = datetime.fromisoformat(mem["last_bot_nudge"])
                if (datetime.now() - lb).total_seconds() < it.get("min_interval_seconds", 10800):
                    return False
            except Exception:
                pass
        today = datetime.now().strftime("%Y-%m-%d")
        if mem.get("nudge_date") != today:
            mem["nudges_today"] = 0
            mem["nudge_date"] = today
        return mem["nudges_today"] < it.get("max_per_day", 3)

    def mark_nudged(self, conv_id):
        mem = self.get_memory(conv_id)
        mem["last_bot_nudge"] = datetime.now().isoformat()
        today = datetime.now().strftime("%Y-%m-%d")
        if mem.get("nudge_date") != today:
            mem["nudges_today"] = 0
            mem["nudge_date"] = today
        mem["nudges_today"] += 1
