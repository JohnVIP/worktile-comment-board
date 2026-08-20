#!/usr/bin/env python3
"""
Worktile OpenAPI 客户端（团队鉴权 Tenant 模式）

特点：
- 凭证在运行时通过构造函数传入，不依赖 .env 文件，适合 Web 应用按租户动态鉴权
- access_token 缓存在内存，遇到 401 自动刷新一次
- 所有评论 / 附件解析都做了健壮的兼容处理，字段名不确定时也能尽量取到数据

参考：worktile-v2 技能的 _worktile_api.py / _config.py
"""

import json as _json
import mimetypes
import os
import re
import requests
import threading
import time
from collections import deque
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic"}

# 文件信息并发数：Worktile 限流较严格，并发过高（如 8）易触发 429，
# 调小到 3 既够用又更稳，尤其当一条评论带多个附件需并发查文件名时。
_MAX_FILE_INFO_WORKERS = 3

# Emoji shortcode → unicode 字符
# Worktile 评论里的 emoji 节点使用 Slack-style shortcode（如 "joy"、"tada"），
# 这里用 Python `emoji` 库做转换（库内 EMOJI_DATA 的 alias 字段覆盖了 Slack/GitHub 常用 shortcode）。
# 若 emoji 库不可用则使用内置精简映射表兜底。
try:
    import emoji as _emoji_lib  # type: ignore
    _EMOJI_LOOKUP = {}
    for _char, _meta in _emoji_lib.EMOJI_DATA.items():
        _names = [_meta.get("en", "").strip(":").lower()]
        _names += [a.strip(":").lower() for a in _meta.get("alias", [])]
        for _n in _names:
            if _n and _n not in _EMOJI_LOOKUP:
                _EMOJI_LOOKUP[_n] = _char
    _EMOJI_AVAILABLE = True
except Exception:
    _EMOJI_AVAILABLE = False
    _EMOJI_LOOKUP = {}

# 兜底：emoji 库不可用时常用的 Slack-style shortcode → unicode
_FALLBACK_EMOJI = {
    "joy": "😂", "tada": "🎉", "thumbsup": "👍", "thumbsdown": "👎",
    "+1": "👍", "-1": "👎", "100": "💯", "heart": "❤️",
    "fire": "🔥", "rocket": "🚀", "eyes": "👀", "pray": "🙏",
    "sparkles": "✨", "clap": "👏", "laughing": "😆", "smile": "😄",
    "grin": "😁", "wink": "😉", "blush": "😊", "thinking": "🤔",
    "sunglasses": "😎", "sob": "😭", "rage": "😡", "angry": "😠",
    "poop": "💩", "shit": "💩", "check": "✅", "x": "❌",
    "star": "⭐", "warning": "⚠️", "bulb": "💡", "zap": "⚡",
}


def _resolve_emoji_shortcode(code):
    """
    把 Worktile emoji.shortcode 转成 unicode emoji 字符。
    - 优先用 Python `emoji` 库（含 Slack/GitHub alias）
    - 库不可用时用内置兜底表
    - 都没匹配上时返回 ":code:`（带冒号）以便用户看到 shortcode 文字
    """
    if not code:
        return ""
    key = code.strip(":").lower()
    if not key:
        return ""
    if _EMOJI_AVAILABLE and key in _EMOJI_LOOKUP:
        return _EMOJI_LOOKUP[key]
    if key in _FALLBACK_EMOJI:
        return _FALLBACK_EMOJI[key]
    return f":{code}:"


def _first(d, keys, default=None):
    """从字典里按顺序尝试取第一个存在的键的值"""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _extract_assignee_uid(props):
    """从任务 properties 里提取负责人 uid"""
    if not isinstance(props, dict):
        return None
    val = props.get("assignee")
    if val is None:
        return None
    if isinstance(val, dict):
        # 常见格式 {value: "uid"} 或 {value: {uid: "xxx"}}
        v = val.get("value")
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return _first(v, ["uid", "id", "_id"])
        return None
    if isinstance(val, str):
        return val
    return None


# === 描述字段提取 ===
# Worktile 真实结构（已在 worktile-v2 技能的 get_task_detail.py 中验证）：
#   properties.desc = {"value": "【Tips】：..."}   ← 嵌套对象，value 里是文本/富文本
# 兼容：直接字符串 / {value: ...} / {content: ...} / 嵌套多层 dict / 字符串列表
# 候选键名涵盖各版本命名分歧：desc / description / content / body / note /
#   remark / summary / detail / intro / text / posts（部分版本描述放在 posts[0].content）

_DESC_KEYS = (
    "desc", "description", "content", "body", "note", "remark",
    "summary", "detail", "intro", "text", "post",
)


def _extract_desc_from_pm(node, _depth=0):
    """从 ProseMirror 文档（dict/list 嵌套结构）里递归抽取所有文本。

    形态：
    - doc 是 list of block nodes，每个 block 含 children/content（list of leaf nodes）
    - leaf node 形如 {"text": "..."} 或 {"type":"text","text":"..."}
    - 容器节点的 list 字段名通常叫 children 或 content（兼容 nodes / blocks）
    - 节点之间用 \\n 分隔（paragraph 自然换行）

    限制：_depth 上限 10 防止异常结构死循环。
    """
    if _depth > 10:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        # leaf node 直接返回 text
        if "text" in node and isinstance(node.get("text"), str):
            return node["text"]
        # 容器节点：按已知 children/content 键递归，并记下已处理过的 key，
        # 避免下方兜底分支再把它们重复遍历一次
        handled_keys = set()
        parts = []
        for k in ("children", "content", "nodes", "blocks"):
            if k in node and isinstance(node[k], list):
                handled_keys.add(k)
                for child in node[k]:
                    sub = _extract_desc_from_pm(child, _depth + 1)
                    if sub:
                        parts.append(sub)
        # 兜底：递归 dict 里所有 list 值（兼容非典型容器结构）
        for k, v in node.items():
            if k in handled_keys:
                continue
            if isinstance(v, list) and v and isinstance(v[0], (dict, str)):
                sub_parts = []
                for child in v:
                    sub = _extract_desc_from_pm(child, _depth + 1)
                    if sub:
                        sub_parts.append(sub)
                if sub_parts:
                    parts.append("\n".join(sub_parts))
        return "\n".join(p for p in parts if p)
    if isinstance(node, list):
        parts = []
        for item in node:
            sub = _extract_desc_from_pm(item, _depth + 1)
            if sub:
                parts.append(sub)
        return "\n".join(parts)
    return ""

# 诊断环形缓冲：每次 _get_task_desc 都把「详情顶层 keys + properties 摘要 + 是否命中」
# 记录到这里，便于未命中时一眼看到真实结构。环形 deque 上限 30 条，线程安全。
_DESC_AUDIT_LOCK = threading.Lock()
_DESC_AUDIT = deque(maxlen=30)


def _record_desc_audit(entry):
    """把一条 desc 抽取诊断写入内存环形缓冲。"""
    with _DESC_AUDIT_LOCK:
        _DESC_AUDIT.append(entry)


def get_desc_audit_snapshot(limit=30):
    """读取 desc 诊断环形缓冲的快照（按写入顺序），供调试接口展示。"""
    with _DESC_AUDIT_LOCK:
        items = list(_DESC_AUDIT)
    return items[-limit:]


def _summarize_for_desc_audit(value, depth=0, max_depth=3):
    """把详情响应里某个字段的可视摘要压缩成字符串，避免打印超长 HTML/markdown。"""
    if depth > max_depth:
        return "..."
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        s = value.strip()
        if len(s) > 60:
            return f"str[{len(s)}]: {s[:60]}…"
        return f"str[{len(s)}]: {s!r}"
    if isinstance(value, list):
        head = [_summarize_for_desc_audit(v, depth + 1, max_depth) for v in value[:3]]
        more = "" if len(value) <= 3 else f"…({len(value)} items)"
        return f"list[{len(value)}]:[{', '.join(head)}{more}]"
    if isinstance(value, dict):
        parts = []
        for i, (k, v) in enumerate(value.items()):
            if i >= 6:
                parts.append("…")
                break
            parts.append(f"{k}={_summarize_for_desc_audit(v, depth + 1, max_depth)}")
        return "{" + ", ".join(parts) + "}"
    return type(value).__name__


def _extract_desc_value(raw):
    """从 desc 字段的任意嵌套结构里提取出最终文本。命中返回字符串；未命中返回空串。

    兼容：
    - str / int / float / bool  → str(...).strip()
    - dict  → 按候选键 value → content → text → data 递归取值；首层都不命中再递归所有 v
    - list  → 取第一个非空元素（部分版本描述放在 posts[0].content）
    - None / 其他类型 → 返回空串
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        # 部分 Worktile 版本把描述存为 ProseMirror JSON 字符串（富文本编辑器格式）
        # 形如 [{"type":"paragraph","children":[{"text":"..."}]}]
        # 这种情况下原样 return 会把整段 JSON 源码当描述返回，用户截图反馈过。
        # 策略：先试 json.loads；解析成功且是 dict/list 形态 → 递归抽 text
        # 解析失败 / 结果为空 → fallback 回原字符串（普通文本描述）
        stripped = raw.strip()
        if stripped.startswith(("[", "{")):
            try:
                parsed = _json.loads(stripped)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                inner = _extract_desc_from_pm(parsed)
                if inner:
                    return inner
                # 解析成功但抽不到 text（可能不是 PM 格式）→ fallback 原字符串
        return stripped
    # bool 是 int 的子类，先处理避免被误当数字
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, (int, float)):
        # 数字 0 / 0.0 不是描述（用户截图反馈：测试任务描述列显示「0」）
        if raw == 0:
            return ""
        return str(raw).strip()
    if isinstance(raw, dict):
        # 优先按已知嵌套键递归（覆盖 {"value": "..."} 等真实结构）
        for k in ("value", "content", "text", "data", "body"):
            if k in raw and raw[k] is not None:
                inner = _extract_desc_value(raw[k])
                if inner:
                    return inner
        # 没命中嵌套键 → 在 dict 自己的值里递归（兜底非典型结构）
        for v in raw.values():
            inner = _extract_desc_value(v)
            if inner:
                return inner
        return ""
    if isinstance(raw, list):
        for item in raw:
            inner = _extract_desc_value(item)
            if inner:
                return inner
        return ""
    return ""


def _looks_like_metadata_value(s):
    """判断字符串是否像元数据（ID/UUID/ISO 时间/HTML 标签），扫描时不视作描述。"""
    if not isinstance(s, str):
        return False
    t = s.strip()
    if not t:
        return True
    # ISO 时间
    try:
        from datetime import datetime
        head = t[:19]
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d", "%Y/%m/%d"):
            try:
                datetime.strptime(head, fmt)
                return True
            except ValueError:
                continue
    except Exception:
        pass
    # 24 位 hex ObjectId / 纯字母数字 ID
    if re.fullmatch(r"[a-fA-F0-9]{12,}", t):
        return True
    # 纯链接
    if re.fullmatch(r"https?://\S+", t):
        return True
    # 纯数字（"0" / "0.0" / "12" / "-3.14"）—— 不是描述，用户截图「描述列显示 0」
    if re.fullmatch(r"-?\d+(\.\d+)?%?", t):
        return True
    return False


def _heuristic_desc_from_props(props):
    """properties 兜底：排除已知元数据键后，挑最长的「像描述」的字符串值。

    排除：assignee / start / due / priority / tag / state / task_state / created_by /
          create_by / created_at / updated_at / completed_at / identifier /
          estimated_workload / progress / follower 等「结构化字段」
    """
    if not isinstance(props, dict):
        return ""
    skip = {
        # 元数据/结构化字段（不是描述）
        "assignee", "start", "due", "priority", "tag", "state", "task_state",
        "created_by", "create_by", "created_at", "create_at", "updated_at",
        "update_at", "completed_at", "identifier", "estimated_workload",
        "progress", "follower", "participants", "sub_task", "sub_tasks",
        "related", "depends", "customfields", "attachments",
        # 已被显式候选处理过的字段，兜底扫描跳过避免重复
        *_DESC_KEYS,
    }
    best = ""
    best_len = 0
    for k, v in props.items():
        if k in skip:
            continue
        s = _extract_desc_value(v)
        if not s or _looks_like_metadata_value(s):
            continue
        if len(s) > best_len:
            best = s
            best_len = len(s)
    return best


# markdown 图片/链接清理（描述值常带 ![alt](url) 原文，看板列显示纯文本更友好）
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# markdown 图片 URL 捕获（![alt](url) → 抓 url 分组）
_MD_IMAGE_URL_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")


def _clean_desc_text(s):
    """把描述文本里的 markdown 图片/链接语法压成纯文本，并合并多余空行。"""
    if not s:
        return s
    s = _MD_IMAGE_RE.sub("", s)          # ![alt](url) → 移除（图片不看板展示）
    s = _MD_LINK_RE.sub(r"\1", s)        # [text](url) → text
    s = re.sub(r"\n{3,}", "\n\n", s)     # 3+ 连续空行压成 1 个空行
    return s.strip()


def _collect_pm_images(node, out, _depth=0):
    """递归收集 ProseMirror 文档里的图片节点 URL（attrs.src / src）。"""
    if _depth > 10 or len(out) > 30:
        return
    if isinstance(node, dict):
        # image 节点：{"type":"image","attrs":{"src":...}} 或 {"type":"image","src":...}
        if node.get("type") in ("image", "imageView", "imageBlock"):
            attrs = node.get("attrs") or {}
            src = attrs.get("src") or node.get("src")
            if isinstance(src, str) and src.startswith(("http://", "https://", "/")):
                out.append(src)
            return
        for v in node.values():
            _collect_pm_images(v, out, _depth + 1)
    elif isinstance(node, list):
        for item in node:
            _collect_pm_images(item, out, _depth + 1)


def _extract_desc_images(raw):
    """从描述值里抽图片 URL 列表（markdown 语法 + ProseMirror image 节点）。

    返回去重后的 URL 列表（保序）；无图片返回空列表。
    """
    if raw is None:
        return []
    out = []

    def _push(u):
        if isinstance(u, str) and u.startswith(("http://", "https://", "/")) and u not in out:
            out.append(u)

    if isinstance(raw, str):
        # markdown 图片
        for m in _MD_IMAGE_URL_RE.finditer(raw):
            _push(m.group(1))
        # ProseMirror JSON 字符串
        stripped = raw.strip()
        if stripped.startswith(("[", "{")):
            try:
                parsed = _json.loads(stripped)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                imgs = []
                _collect_pm_images(parsed, imgs)
                for u in imgs:
                    _push(u)
    elif isinstance(raw, dict):
        # 嵌套 dict：先走候选键再递归
        for k in ("value", "content", "text", "data", "body"):
            if k in raw and raw[k] is not None:
                for u in _extract_desc_images(raw[k]):
                    _push(u)
        imgs = []
        _collect_pm_images(raw, imgs)
        for u in imgs:
            _push(u)
    elif isinstance(raw, list):
        for item in raw:
            for u in _extract_desc_images(item):
                _push(u)
    return out


def _pick_desc_with_path(obj, props=None):
    """同 _pick_desc_from_obj，但同时返回 (命中路径, 文本)。

    命中路径形如 "detail.desc" / "properties.desc.value" / "properties[heuristic]:longest_string"。
    任何 _extract_desc_value 命中都视为非空（兼容空字符串被过滤），
    因此诊断时能看到「抽到 dict 还是抽到空串」。

    所有路径都过 _looks_like_metadata_value 闸门：纯 hex ID / 链接 / 时间戳等
    明显是元数据的字符串不会被当作描述，避免「候选键命中但值是 property_id」
    之类的误判（用户截图反馈：多个测试任务描述列都显示同一串 24hex）。
    """
    if isinstance(obj, dict):
        for k in _DESC_KEYS:
            if k in obj and obj[k] is not None:
                s = _extract_desc_value(obj.get(k))
                if s and not _looks_like_metadata_value(s):
                    return (f"detail.{k}", s)
                # 命中了键但抽取为空/元数据，跳过该键继续找下一个
                raw = obj.get(k)
                if isinstance(raw, (dict, list)):
                    continue
                continue
    if isinstance(props, dict):
        for k in _DESC_KEYS:
            if k in props and props[k] is not None:
                s = _extract_desc_value(props.get(k))
                if s and not _looks_like_metadata_value(s):
                    return (f"properties.{k}", s)
                # 命中键但值不可用，继续下一个
                continue
    if isinstance(props, dict):
        heuristic = _heuristic_desc_from_props(props)
        if heuristic:
            return ("properties[heuristic]", heuristic)
    return (None, "")


def _pick_desc_from_obj(obj, props=None):
    """从任务对象 + properties 中按候选顺序匹配 desc，命中即返回字符串。

    顺序：先已知 desc 候选键（顶层 → properties）→ properties 兜底扫描。
    每一步都用 _extract_desc_value 兼容嵌套。
    """
    _, s = _pick_desc_with_path(obj, props)
    return s


def _to_epoch_sec(raw):
    """把 Worktile 时间字段统一成「秒级 epoch」，便于和 time.time() 比较。

    兼容：秒/毫秒级整数时间戳、ISO 字符串（2026-08-12T14:43:18 / 2026-08-12 14:43:18）。
    毫秒级（>1e11）自动转秒。无法解析返回 None（调用方据此跳过该任务）。
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        if v > 1e11:          # 毫秒级时间戳
            v = v / 1000.0
        return v
    s = str(raw).strip()
    if not s:
        return None
    head = s[:19]             # 兼容 .123Z / +08:00 尾巴
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(head, fmt).timestamp()
        except ValueError:
            continue
    return None


def _due_to_epoch(due_raw):
    """把 Worktile 的 due 字段统一成秒级 epoch。

    Worktile 真实结构：properties.due = {"date": <epoch秒>, "with_time": 0}
    （date 已是秒级时间戳，with_time=0 表示只精确到天）。
    兼容旧版扁平时间戳/字符串，以及其它可能的嵌套 key（time/timestamp/value）。
    无法解析返回 None。
    """
    if due_raw is None:
        return None
    if isinstance(due_raw, dict):
        for k in ("date", "time", "timestamp", "deadline", "value", "ts"):
            v = due_raw.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return float(v) if v <= 1e11 else float(v) / 1000.0
        for k in ("date", "time"):
            s = due_raw.get(k)
            if isinstance(s, str) and s.strip():
                return _to_epoch_sec(s)
        return None
    return _to_epoch_sec(due_raw)


def _start_to_epoch(task, props):
    """把 Worktile 的开始时间统一成秒级 epoch。

    真实结构：properties.start = {"value": {"date": <epoch秒>, "with_time": 0}}
    （同 due 的嵌套形态，date 是秒级时间戳）。
    兼容：顶层 start_at / begin_at、扁平时间戳、字符串日期。
    无法解析返回 None。
    """
    raw = (_first(task, ["start_at", "start", "begin_at", "begin"])
           or _first(props, ["start_at", "start", "begin_at", "begin"]))
    if raw is None:
        return None
    if isinstance(raw, dict):
        # {"value": {"date": ts, "with_time": 0}} → value.date
        v = raw.get("value")
        if isinstance(v, dict):
            d = v.get("date")
            if isinstance(d, (int, float)) and d > 0:
                return float(d) if d <= 1e11 else float(d) / 1000.0
            if isinstance(d, str) and d.strip():
                return _to_epoch_sec(d)
        elif isinstance(v, (int, float)) and v > 0:
            return float(v) if v <= 1e11 else float(v) / 1000.0
        # {"date": ts} 直接形态
        d = raw.get("date")
        if isinstance(d, (int, float)) and d > 0:
            return float(d) if d <= 1e11 else float(d) / 1000.0
        if isinstance(d, str) and d.strip():
            return _to_epoch_sec(d)
        return None
    return _to_epoch_sec(raw)


# 任务状态 type → 展示名（Worktile 常规五态；自定义状态用 task_state.name 覆盖）
_STATE_TYPE_NAMES = {0: "未开始", 1: "未开始", 2: "进行中", 3: "已完成", 4: "已取消"}


def _status_display_name(status_raw, is_completed=False):
    """把 task_state / state_type 转成展示名。

    优先级：
    - dict 形态（task_state = {name, type}）→ name（自定义状态名，如「已完成」「测试中」）
      name 缺失时按 type 映射常规名
    - int 形态（state_type）→ 按映射表
    - 兜底：is_completed=True → "已完成"；否则空串（前端显示占位符）
    """
    if isinstance(status_raw, dict):
        name = status_raw.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        t = status_raw.get("type")
        if isinstance(t, (int, float)):
            return _STATE_TYPE_NAMES.get(int(t), str(int(t)))
        return ""
    if isinstance(status_raw, bool):
        return "已完成" if status_raw else ""
    if isinstance(status_raw, (int, float)):
        return _STATE_TYPE_NAMES.get(int(status_raw), str(status_raw))
    if isinstance(status_raw, str) and status_raw.strip():
        return status_raw.strip()
    return "已完成" if is_completed else ""


def _is_completed(task, props):
    """判断任务是否「已完成 / 已结束」。多候选信号，尽量兼容 Worktile 不同版本。

    返回 True/False；信号缺失时返回 False（宁可少判，不误杀未完成任务）。
    """
    # 1) 显式完成标记（部分版本可能在顶层或 properties）
    c = (_first(task, ["complete", "is_done", "finished", "done"])
         or _first(props, ["complete", "is_done", "finished", "done"]))
    if isinstance(c, bool):
        return c
    if isinstance(c, (int, float)):
        return c == 1
    if isinstance(c, str):
        low = c.strip().lower()
        if low in ("true", "1", "yes", "y", "是"):
            return True
        if low in ("false", "0", "no", "n", "否"):
            return False

    # 2) Worktile 真实字段：task_state.type == 3 为「结束/完成态」
    #    （实测包含 已完成 / 关 / 报废清理 等所有终态；顶层 state_type 与之同值）
    ts = task.get("task_state") or (props.get("task_state") if isinstance(props, dict) else None)
    if isinstance(ts, dict):
        if ts.get("type") == 3:
            return True
        name = str(ts.get("name") or "")
        if any(m in name.lower() for m in ("完成", "结项", "交付", "done", "complete", "closed", "close")):
            return True
    stype = task.get("state_type")
    if isinstance(stype, int) and stype == 3:
        return True

    # 3) 旧版候选（status 对象/字符串/数字代码）
    st = (_first(task, ["status", "status_type", "entry_status", "state"])
          or _first(props, ["status", "status_type", "entry_status", "state"]))
    if st is None:
        return False
    if isinstance(st, dict):
        if st.get("type") == 3:
            return True
        val = str(st.get("key") or st.get("id") or st.get("name") or "")
    else:
        val = str(st)
    val = val.lower()
    done_markers = ("done", "complete", "completed", "finish",
                    "closed", "close", "已完", "完成", "结项", "完工", "交付")
    return any(m in val for m in done_markers)


def _ts_to_str(ts, full=False):
    """时间戳 / 时间字符串 -> 可读字符串
    full=False（默认）：MM-DD HH:MM，省年省秒，节省列宽
    full=True：YYYY-MM-DD HH:MM:SS，完整时间戳（用于评论元数据等需要精确时间的场景）
    """
    if ts is None:
        return "-"
    fmt = "%Y-%m-%d %H:%M:%S" if full else "%m-%d %H:%M"
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts).strftime(fmt)
        except (ValueError, OSError):
            return str(ts)
    s = str(ts)
    # ISO 字符串里也尝试取短格式（裁掉秒和年）
    if not full and "T" in s and len(s) >= 16:
        try:
            # 兼容 2026-08-12T14:43:18.xxxZ / +08:00 / 无时区 三种
            date_part, _, rest = s.partition("T")
            mm_dd = "-".join(date_part.split("-")[1:])  # MM-DD
            hh_mm = rest[:5]                             # HH:MM
            return f"{mm_dd} {hh_mm}"
        except Exception:
            return s
    return s


def _to_sortable(ts):
    """把时间转成可比较的数字（用于排序），失败返回 0"""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        s = ts.strip()
        if s.isdigit():
            return float(s)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return time.mktime(datetime.strptime(s, fmt).timetuple())
            except ValueError:
                continue
    return 0.0


class WorktileClient:
    def __init__(self, client_id, client_secret, base_url="https://dev.worktile.com"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = (base_url or "https://dev.worktile.com").rstrip("/")
        self._token = None
        self._file_cache = {}      # file_id -> {"title", "ext", "type"} 实例级缓存
        # 强制不走系统代理：trust_env=False 让 requests 忽略 HTTP_PROXY/HTTPS_PROXY/NO_PROXY
        # 等环境变量。Worktile API（dev.worktile.com）走公网直连即可，本地代理
        # （如 127.0.0.1:10808 沙箱代理）一旦未起，所有调用立刻被拒，
        # 导致认证/拉数据全挂。直连最稳。
        self._session = requests.Session()
        self._session.trust_env = False

    # ------------------------------------------------------------------ 认证
    def _ensure_token(self):
        if self._token:
            return self._token
        url = f"{self.base_url}/open-api/tenant-access-token"
        try:
            resp = self._session.post(
                url,
                json={"client_id": self.client_id, "client_secret": self.client_secret},
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"认证请求失败：{e}")
        if resp.status_code >= 400:
            raise RuntimeError(f"认证失败（{resp.status_code}）：{resp.text[:300]}")
        data = resp.json()
        token = data.get("tenant_access_token") or data.get("access_token")
        if not token:
            raise RuntimeError(f"认证失败，未返回 token：{data}")
        self._token = token
        return token

    # 可重试的 HTTP 状态码：限流 + 上游临时故障。4xx 业务错误（401/403/404）不重试。
    _RETRYABLE_STATUS = frozenset((429, 500, 502, 503, 504))
    _MAX_RETRIES = 3

    def _req(self, method, path, params=None, json=None, _attempt=0, raw=False, timeout=30):
        def _do_request(token):
            p = dict(params or {})
            p["access_token"] = token
            url = f"{self.base_url}/open-api{path}"
            try:
                resp = self._session.request(method, url, params=p, json=json, timeout=timeout)
            except requests.exceptions.RequestException as e:
                raise RuntimeError(f"接口请求失败 {path}：{e}")
            return resp

        token = self._token or self._ensure_token()
        resp = _do_request(token)

        # token 失效重试一次的两种信号：
        # 1) HTTP 401（少数情况下 Worktile 直接返回 401）
        # 2) HTTP 200 但 body 里 error_code 是认证类（Worktile 大多数情况下用这种方式，
        #    常见 error_code=1103 "invalid access token"）—— 不识别的话，
        #    _req 会把 {"ok": false, "error_code": 1103} 当成正常响应返回，
        #    下游解析成空 items，导致「自动登录 OK 但拿不到数据」。
        def _need_refresh():
            if resp.status_code == 401:
                return True
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except Exception:
                    return False
                # 1102: access_token 格式错误；1103: access_token 无效/过期
                if body.get("ok") is False and body.get("error_code") in (1102, 1103):
                    return True
            return False

        if _need_refresh():
            self._token = None
            new_token = self._ensure_token()
            resp = _do_request(new_token)

        # ---- 429 / 5xx 退避重试 ----
        # Worktile 偶发限流（429）或上游 5xx，多为瞬时故障；直接重试通常能恢复，
        # 比把错误直接抛给前端友好得多。优先尊重 Retry-After，缺失则用指数退避。
        if resp.status_code in self._RETRYABLE_STATUS and _attempt < self._MAX_RETRIES:
            wait = self._backoff_seconds(resp, _attempt)
            print(f"[worktile] {resp.status_code} {path} 触发退避重试 "
                  f"({_attempt + 1}/{self._MAX_RETRIES})，等待 {wait:.1f}s",
                  flush=True)
            time.sleep(wait)
            return self._req(method, path, params=params, json=json, _attempt=_attempt + 1,
                             raw=raw, timeout=timeout)

        if resp.status_code >= 400:
            # ⚠️ 反爬/限流/临时维护页：Worktile 偶发用 HTML 响应 4xx/5xx
            # （常见 429 Too Many Requests 直接返 nginx 限流页）。如果不识别就把整页 HTML
            # 塞进错误消息，前端评论单元会被当成评论正文渲染。
            ctype = (resp.headers.get("content-type") or "").lower()
            body_preview = (resp.text or "")[:300]
            is_html = ("html" in ctype) or body_preview.lstrip().startswith(("<!", "<html"))
            if is_html:
                if resp.status_code == 429:
                    msg = f"Worktile 接口限流 (HTTP 429 {path})，请稍后重试"
                elif resp.status_code in (502, 503, 504):
                    msg = f"Worktile 上游网关不可用 (HTTP {resp.status_code} {path})，请稍后重试"
                elif resp.status_code == 403:
                    msg = f"Worktile 拒绝访问 (HTTP 403 {path})，可能被反爬，详见 .secret 后台"
                else:
                    msg = f"Worktile 返回 HTML (HTTP {resp.status_code} {path})，疑似临时维护/反爬，请稍后重试"
            else:
                msg = f"接口错误 {resp.status_code} {path}：{body_preview}"
            raise RuntimeError(msg)
        return resp if raw else resp.json()

    @staticmethod
    def _backoff_seconds(resp, attempt):
        """计算重试前的等待秒数。

        - 优先读取 Retry-After：
          · 数值形式 → 直接当秒数（封顶 30s，避免被异常值卡死）
          · HTTP-date 形式（如 `Wed, 21 Oct 2015 07:28:00 GMT`）→ 取到点的剩余秒数
        - 都没有 → 指数退避 2**attempt（1s, 2s, 4s…），封顶 30s
        """
        raw = (resp.headers.get("Retry-After") or "").strip()
        if raw:
            try:
                secs = float(raw)
                if secs >= 0:
                    return min(secs, 30.0)
            except ValueError:
                pass
            try:
                # %z 不支持 "GMT"，先替换成 "+0000" 再解析
                s = raw.replace("GMT", "+0000")
                dt = datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z")
                delta = dt.timestamp() - time.time()
                if delta > 0:
                    return min(delta, 30.0)
            except Exception:
                pass
        return min(2.0 ** attempt, 30.0)

    # ------------------------------------------------------------------ 项目
    def get_projects(self, limit=500):
        """获取项目列表，返回 [{id, name}]"""
        results = []
        page_index = 0
        page_size = 100
        while len(results) < limit:
            data = self._req(
                "GET", "/mission/get-projects-by-page",
                params={"page_index": page_index, "page_size": page_size},
            )
            items = data.get("items") or data.get("value") or []
            if not items:
                break
            results.extend(items)
            if not data.get("has_more"):
                break
            page_index += 1
        out = []
        for p in results[:limit]:
            pid = p.get("_id") or p.get("id") or p.get("project_id")
            name = p.get("name") or p.get("project_name") or pid or "未命名项目"
            if pid:
                out.append({"id": pid, "name": name})
        return out

    # ------------------------------------------------------------------ 成员
    def get_member_map(self):
        """返回 {uid: display_name}"""
        members = []
        page_index = 0
        while True:
            data = self._req(
                "GET", "/contact/get-members-by-page",
                params={"page_index": page_index, "page_size": 100},
            )
            items = data.get("items") or data.get("value") or []
            if not items:
                break
            members.extend(items)
            if not data.get("has_more"):
                break
            page_index += 1
        m = {}
        for mem in members:
            uid = mem.get("_id") or mem.get("id") or mem.get("uid")
            name = (mem.get("display_name") or mem.get("name")
                    or mem.get("username") or uid)
            if uid:
                m[uid] = name
        return m

    # ------------------------------------------------------------------ 任务
    def get_tasks_page(self, project_id, page_index=0, page_size=50):
        """获取某项目任务某一页，返回 {items, total, has_more, users_map}。

        并行调用 V1（/mission/get-project-tasks-by-page，含 updated_at/created_at 等元数据）
        与 V2（/mission/projects/{id}/tasks，含 properties.assignee 与 references.users），
        按 _id 把 V2 的 properties 合到 V1 任务对象上。V2 失败不阻塞 V1。
        """
        from concurrent.futures import ThreadPoolExecutor

        def _v1():
            return self._req(
                "GET", "/mission/get-project-tasks-by-page",
                params={"project_id": project_id,
                        "page_index": page_index, "page_size": page_size},
            )
        def _v2():
            return self._req(
                "GET", f"/mission/projects/{project_id}/tasks",
                params={"page_index": page_index, "page_size": page_size},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_v1)
            f2 = pool.submit(_v2)
            try:
                d1 = f1.result()
            except Exception:
                d1 = {}
            try:
                d2 = f2.result()
            except Exception:
                d2 = {}

        v1_items = d1.get("items") or d1.get("value") or []
        v2_items = d2.get("value") or []

        # 提取 V2 references.users（用于补全 assignee uid → display_name 的映射）
        users_map = {}
        for u in (d2.get("references") or {}).get("users") or []:
            uid = u.get("uid") or u.get("_id")
            name = u.get("display_name") or u.get("name") or uid
            if uid:
                users_map[uid] = name

        # 按 _id 把 V2 的 properties 合并到 V1 任务上
        v2_by_id = {t.get("_id"): t for t in v2_items if t.get("_id")}
        for t in v1_items:
            v2t = v2_by_id.get(t.get("_id"))
            if v2t and v2t.get("properties"):
                t["properties"] = v2t["properties"]

        return {
            "items": v1_items,
            "total": d1.get("total"),
            "has_more": bool(d1.get("has_more") or (d2.get("meta") or {}).get("has_more", False)),
            "users_map": users_map,
        }

    def get_all_tasks_page(self, projects, page_index=0, page_size=50, member_map=None):
        """
        跨多个项目聚合任务并分页。

        由于 Worktile 任务列表接口按项目分页，这里对projects逐个拉取、合并成一个
        全局列表再做切片分页；has_more 通过检查剩余项目/分页是否还有数据来判定。

        同时把每个项目 V1 接口已知的 total 累加成「全局已知 total」，
        供前端翻页器显示「共 N 页」，避免出现「（还有更多）」的占位提示。
        """
        member_map = member_map or {}
        start = page_index * page_size
        end = start + page_size
        collected = []
        reached_end = False
        stop_idx = -1
        # 累计各项目 V1 接口的 total（每个项目只在第一次拉到 raw 时记录一次）
        project_totals = {}

        for idx, p in enumerate(projects):
            pi = 0
            while True:
                raw = self.get_tasks_page(p["id"], page_index=pi, page_size=page_size)
                # 把 V2 references.users 合并进 member_map（避免负责人 uid 查不到名字）
                for uid, name in (raw.get("users_map") or {}).items():
                    member_map.setdefault(uid, name)
                items = raw.get("items", [])
                if not items:
                    break
                # 记录该项目的 total（仅第一次记录，避免重复页造成的误差）
                if p["id"] not in project_totals:
                    t = raw.get("total")
                    if isinstance(t, int):
                        project_totals[p["id"]] = t
                for t in items:
                    collected.append(self.normalize_task(
                        t, project_name=p.get("name", ""), member_map=member_map))
                if not raw.get("has_more", False):
                    break
                pi += 1
                if len(collected) >= end:
                    reached_end = True
                    stop_idx = idx
                    break
            if reached_end:
                break

        has_more = False
        if reached_end:
            # 当前项目可能还有下一页
            raw = self.get_tasks_page(projects[stop_idx]["id"],
                                      page_index=pi + 1, page_size=1)
            for uid, name in (raw.get("users_map") or {}).items():
                member_map.setdefault(uid, name)
            # 顺便补记这条 raw 的 total（如果第一页的 raw 没拿到 total，
            # 这条 page_size=1 的也会带上 total 字段）
            if projects[stop_idx]["id"] not in project_totals:
                t = raw.get("total")
                if isinstance(t, int):
                    project_totals[projects[stop_idx]["id"]] = t
            if raw.get("items"):
                has_more = True
            else:
                # 检查后续项目是否还有任意任务；同时记录它们的 total
                for p in projects[stop_idx + 1:]:
                    raw = self.get_tasks_page(p["id"], page_index=0, page_size=1)
                    for uid, name in (raw.get("users_map") or {}).items():
                        member_map.setdefault(uid, name)
                    if p["id"] not in project_totals:
                        t = raw.get("total")
                        if isinstance(t, int):
                            project_totals[p["id"]] = t
                    if raw.get("items"):
                        has_more = True
                        break

        # 全局已知 total = 各项目 V1 接口 total 的累加
        # 若没有任何项目返回 total，则 fallback 为 None（前端仍显示「（还有更多）」）
        global_total = sum(project_totals.values()) if project_totals else None

        return {"items": collected[start:end], "total": global_total, "has_more": has_more}

    # 内部全量拉取时使用的单页大小（尽量一次多拉，减少请求次数；
    # 若 Worktile 有上限会自动回落，循环仍靠 has_more 收尾）
    _FETCH_PAGE = 200

    def _iter_all_tasks(self, projects, project_id, page_size, member_map):
        """迭代拉取目标范围（单项目或全部项目）的全部任务，yields (task_dict, project_name)。

        封装 search_tasks/list_task_titles 都要用到的「全量遍历」逻辑，避免重复。
        """
        member_map = member_map or {}

        def _collect(raw, pname):
            for uid, name in (raw.get("users_map") or {}).items():
                member_map.setdefault(uid, name)
            for t in (raw.get("items") or []):
                yield self.normalize_task(t, project_name=pname, member_map=member_map), pname

        if project_id == "__all__":
            for p in projects:
                pi = 0
                while True:
                    raw = self.get_tasks_page(p["id"], page_index=pi, page_size=page_size)
                    yield from _collect(raw, p.get("name", ""))
                    if not raw.get("has_more", False):
                        break
                    pi += 1
        else:
            project_name = next((p.get("name", "") for p in projects if p.get("id") == project_id), "")
            pi = 0
            while True:
                raw = self.get_tasks_page(project_id, page_index=pi, page_size=page_size)
                yield from _collect(raw, project_name)
                if not raw.get("has_more", False):
                    break
                pi += 1

    def list_task_titles(self, projects, project_id, q, limit=20, page_size=200):
        """按 title 子串（大小写不敏感）取候选任务列表，用于前端 typeahead。

        返回 {items: [{task_id, identifier, title, project_name}], matched: int}：
        - items 至多 limit 条；matched 是过滤后命中总数（前端用于「是否还有更多」提示）。
        - q 为空时返回前 limit 条全量任务（按拉取顺序），方便探索。
        - 只拉 title 类字段，不含评论，性能开销很小。
        """
        member_map = {}
        hits = []
        matched = 0
        q_lower = (q or "").strip().lower()
        for t, pname in self._iter_all_tasks(projects, project_id, page_size, member_map):
            title = t.get("title") or ""
            if q_lower and q_lower not in title.lower():
                continue
            matched += 1
            if len(hits) < limit:
                hits.append({
                    "task_id": t.get("task_id"),
                    "identifier": t.get("identifier"),
                    "title": title,
                    "project_name": pname,
                })
        return {"items": hits, "matched": matched}

    def search_tasks(self, projects, project_id, keyword_or_keywords, page_index=0, page_size=50, member_map=None):
        """按任务名称做大小写不敏感的子串模糊搜索，跨页/跨项目聚合后分页返回。

        - project_id 为 "__all__" 时搜索所有项目；否则仅该项目。
        - keyword_or_keywords 为 str（多个用 | 分隔）或 list[str]；任一命中即匹配（OR）。
        - 传空值时退化为对全量任务的普通分页（total 为全量数）。
        - 返回 {items, total（过滤后总数，精确）, has_more}。

        与 get_all_tasks_page 不同：这里必须先把所有任务拉全再做过滤，
        否则分页切片会漏掉不在当前页的命中项。
        """
        # 归一为 list[str]（OR 语义）
        if keyword_or_keywords is None or keyword_or_keywords == "":
            kws = []
        elif isinstance(keyword_or_keywords, str):
            kws = [k.strip() for k in keyword_or_keywords.split("|") if k.strip()]
        else:
            kws = [str(k).strip() for k in keyword_or_keywords if k and str(k).strip()]
        kws_lc = [k.lower() for k in kws]

        # 用内部的 page_size=200，避免 project 大时循环太多，但与 search 结果分页独立
        all_tasks = []
        for t, _pname in self._iter_all_tasks(projects, project_id, self._FETCH_PAGE,
                                              member_map or {}):
            all_tasks.append(t)

        # 按任务名称模糊过滤（OR）
        if kws_lc:
            def _hit(task):
                title = (task.get("title") or "").lower()
                return any(kw in title for kw in kws_lc)
            all_tasks = [t for t in all_tasks if _hit(t)]

        # 去重（按 task_id，避免 OR 出现重复命中）
        seen = set()
        uniq = []
        for t in all_tasks:
            tid = t.get("task_id")
            if tid and tid in seen:
                continue
            if tid:
                seen.add(tid)
            uniq.append(t)
        all_tasks = uniq

        # 分页
        total = len(all_tasks)
        start = page_index * page_size
        end = start + page_size
        return {
            "items": all_tasks[start:end],
            "total": total,
            "has_more": end < total,
        }

    @staticmethod
    def normalize_task(task, project_name="", member_map=None):
        """把一条任务原始数据规范化为看板需要的字段"""
        member_map = member_map or {}
        task_id = task.get("_id") or task.get("id") or task.get("task_id") or ""
        identifier = task.get("identifier") or ""
        title = task.get("title") or task.get("name") or "无标题"
        project_id = task.get("project_id") or ""
        props = task.get("properties") or {}
        assignee_uid = _extract_assignee_uid(props)
        assignee = member_map.get(assignee_uid, assignee_uid) if assignee_uid else "未分配"
        updated_at = task.get("updated_at") or task.get("update_at")
        # ---- 延期任务需要的两个字段 ----
        # due 可能在任务顶层（V1）或 properties（V2），两处都尝试；
        # 真实结构为 properties.due = {"date": <epoch秒>, "with_time": 0}
        due_raw = (_first(task, ["due_at", "due", "deadline", "end_at", "finish_at"])
                   or _first(props, ["due_at", "due", "deadline", "end_at", "finish_at"]))
        due_at = _due_to_epoch(due_raw)
        # 真实状态字段是 task_state（{name,type}）或顶层 state_type（int）
        status_raw = (task.get("task_state")
                      or task.get("state_type")
                      or _first(props, ["task_state", "state_type"]))
        is_completed = _is_completed(task, props)
        # ---- 描述 ----
        # Worktile 任务详情页「描述」属性的真实结构：
        #   properties.desc = {"value": "【Tips】：..."}  ← 嵌套对象
        # （已在 worktile-v2 技能 get_task_detail.py 验证）。列表接口（V1 /mission/
        # get-project-tasks-by-page）通常不带 desc，V2 接口可能带（但也是嵌套）。
        # 用 _pick_desc_from_obj 统一处理：先按 _DESC_KEYS 顺序尝试（每一步都
        # 用 _extract_desc_value 兼容嵌套），命中即返回；都未命中再在 properties
        # 里做兜底扫描（排除元数据键，挑最长最像描述的字符串）。
        desc_raw = _pick_desc_from_obj(task, props)
        desc = _clean_desc_text(desc_raw)
        # 描述里的图片 URL（markdown + ProseMirror image 节点），前端渲染缩略图 + lightbox
        desc_images = _extract_desc_images(desc_raw)
        # ---- 状态 / 开始时间 ----
        # 状态展示名：优先 task_state.name（自定义状态），其次 type 映射常规名
        status = _status_display_name(status_raw, is_completed)
        # 开始时间：properties.start = {"value": {"date": ts, "with_time": 0}}
        start_at = _start_to_epoch(task, props)
        return {
            "task_id": task_id,
            "identifier": identifier,
            "title": title,
            "desc": desc,
            "desc_images": desc_images,
            "status": status,
            "start_at": start_at,
            "start_at_str": _ts_to_str(start_at, full=True),  # 年月日时分秒
            "project_id": project_id,
            "project_name": project_name,
            "assignee": assignee,
            "updated_at": updated_at,
            "updated_at_str": _ts_to_str(updated_at, full=True),
            # 延期判定所需
            "due_at": due_at,                       # 秒级 epoch，None 表示无截止时间
            "due_at_str": _ts_to_str(due_at, full=True),
            "is_completed": is_completed,
            "_status_raw": status_raw,             # 仅供后端诊断使用（前端忽略）
            "_assignee_uid": assignee_uid,         # _ 前缀：仅后端按 uid 过滤时使用（_ 前缀约定前端忽略）
        }

    # ------------------------------------------------------------------ 描述补全
    def _get_task_desc(self, task_id):
        """从任务详情接口 /mission/tasks/{task_id} 取 desc（列表接口不含描述字段）。

        返回 (desc, desc_images)：
        - desc：去空白 + 清理 markdown 后的描述纯文本；异常/未命中返回空串
        - desc_images：描述里图片 URL 列表（markdown + ProseMirror image 节点）
        详情响应可能有 data / data.value 包裹，统一解开后用 _pick_desc_from_obj
        统一从顶层 + properties 抽取；自动兼容 properties.desc.value 嵌套结构。

        诊断：每次调用都把「详情顶层 keys + properties 摘要 + 命中字段」写进
        _DESC_AUDIT 环形缓冲，便于 _pick_desc_from_obj 漏命中时一眼定位真实字段名。
        """
        audit = {
            "task_id": task_id,
            "ok": False,
            "desc_len": 0,
            "hit_path": None,
            "hit_value_preview": None,
            "top_keys": [],
            "props_keys": [],
            "props_desc_summary": None,
            "props_size": 0,
            "error": None,
        }
        try:
            # 关键：详情接口必须显式传 columns=properties 才返回 properties 字段
            # （不传时只回 _id/title；desc 藏在 properties.desc.value 里）。
            # 实测验证：GET /mission/tasks/{id}?columns=properties → properties.desc.value
            data = self._req("GET", f"/mission/tasks/{task_id}",
                             params={"columns": "properties"})
        except Exception as e:
            audit["error"] = f"{type(e).__name__}: {e}"
            _record_desc_audit(audit)
            return ("", [])
        detail = data
        if isinstance(data, dict):
            if "data" in data and isinstance(data["data"], dict):
                inner = data["data"]
                detail = inner.get("value", inner)
        if not isinstance(detail, dict):
            audit["error"] = "detail not dict"
            _record_desc_audit(audit)
            return ("", [])
        props = detail.get("properties") or detail.get("props") or {}
        # 诊断：在调用抽取之前先抓详情结构摘要
        try:
            audit["top_keys"] = sorted(
                [k for k in detail.keys() if isinstance(detail.get(k), (str, dict, list))]
            )[:40]
        except Exception:
            pass
        try:
            audit["props_size"] = len(props) if isinstance(props, dict) else 0
            if isinstance(props, dict):
                audit["props_keys"] = sorted(list(props.keys()))[:40]
                if "desc" in props:
                    audit["props_desc_summary"] = _summarize_for_desc_audit(props["desc"])
                elif "description" in props:
                    audit["props_desc_summary"] = _summarize_for_desc_audit(props["description"])
        except Exception:
            pass
        # 真正抽取（带命中路径，便于诊断）
        hit_path, desc = _pick_desc_with_path(detail, props)
        # 抽图片 URL（markdown + ProseMirror image 节点），在清理文本前抽
        desc_images = _extract_desc_images(
            props.get("desc") if isinstance(props, dict) else None
        ) or _extract_desc_images(
            props.get("description") if isinstance(props, dict) else None
        )
        desc = _clean_desc_text(desc)  # 清掉 ![alt](url) 等 markdown 语法，看板显示纯文本
        if desc:
            audit["ok"] = True
            audit["desc_len"] = len(desc)
            audit["hit_path"] = hit_path
            preview = desc[:120].replace("\n", " ")
            audit["hit_value_preview"] = preview
        _record_desc_audit(audit)
        # 兼容旧版 stderr 钩子
        if not desc and os.environ.get("WT_DESC_DEBUG") == "1":
            print(f"[wt-desc] {task_id}: empty. top-level keys={audit['top_keys'][:30]}", flush=True)
        return (desc, desc_images)

    def enrich_tasks_with_desc(self, tasks, concurrency=None):
        """批量补全任务的 desc / desc_images（列表接口不带描述，需逐任务查详情）。

        原地更新 tasks 中的 dict；返回 (enriched, failed)。
        - 只对「当前 desc 为空」的任务发起请求，已填值的跳过（幂等）。
        - concurrency 默认用模块级 _MAX_FILE_INFO_WORKERS（=3），与附件/评论并发一致，避免 429。
        - 任务量大（如全部项目）时调用方应只传「当前页/当前视图要展示的子集」以控制请求数。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        if concurrency is None:
            concurrency = _MAX_FILE_INFO_WORKERS
        targets = [t for t in tasks
                   if isinstance(t, dict) and not (t.get("desc") or "").strip()]
        if not targets:
            return (0, 0)
        enriched = 0
        failed = 0
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            fut_map = {ex.submit(self._get_task_desc, t["task_id"]): t for t in targets}
            for fut in as_completed(fut_map):
                t = fut_map[fut]
                try:
                    d, imgs = fut.result()
                except Exception:
                    failed += 1
                    continue
                if d:
                    t["desc"] = d
                    if imgs:
                        t["desc_images"] = imgs
                    enriched += 1
        return (enriched, failed)

    # ------------------------------------------------------------------ 评论
    def get_task_comments(self, task_id, member_map=None):
        """获取任务详情中的评论，返回规范化的评论列表（已按时间倒序）"""
        member_map = member_map or {}
        data = self._req(
            "GET", f"/mission/tasks/{task_id}",
            params={"columns": "comments,properties"},
        )
        # 兼容 data / data.value 包裹
        detail = data
        if isinstance(data, dict):
            if "data" in data and isinstance(data["data"], dict):
                inner = data["data"]
                detail = inner.get("value", inner)
        if not isinstance(detail, dict):
            return []
        comments = detail.get("comments") or []
        if not isinstance(comments, list):
            comments = [comments]

        # ---- 先收集本任务所有顶层附件的「文件 ID」，一次性批量查文件名 ----
        # Worktile 评论顶层 attachments 实际返回的是文件 ID 列表（不是文件名），
        # 需要再调 /file/{id}/info 才能拿到真实文件名（data.value.title）。
        file_ids = []
        for raw in comments:
            if not isinstance(raw, dict):
                continue
            atts = _first(raw, ["attachments", "files", "resources", "images", "file_list"])
            if isinstance(atts, list):
                for a in atts:
                    if isinstance(a, str) and a not in file_ids:
                        file_ids.append(a)
        file_map = self.get_file_infos(file_ids) if file_ids else {}

        out = []
        for raw in comments:
            if not isinstance(raw, dict):
                # 评论可能是纯文本
                out.append({
                    "author": "未知",
                    "content": str(raw),
                    "created_at": None,
                    "created_at_str": "-",
                    "attachments": [],
                })
                continue
            author = self._extract_comment_author(raw, member_map)
            raw_content = _first(raw, ["content", "text", "comment", "body",
                                "message", "desc", "detail"]) or ""
            # Worktile 评论 content 是富文本 JSON 字符串，递归展开成纯文本
            # 传入 member_map，用于把 @mention 中的纯 ID 还原成真实姓名
            content = rich_text_to_plain(raw_content, member_map)
            created_at = _first(raw, ["created_at", "create_at", "create_time",
                                      "time", "ts", "created", "create_timestamp"])
            attachments = self._extract_attachments(
                _first(raw, ["attachments", "files", "resources", "images", "file_list"]),
                file_map,
            )
            # 如果评论顶层 attachments 为空，从富文本 children 里抽取 inline image/file
            if not attachments:
                inline_atts = self._extract_inline_attachments(raw_content)
                if inline_atts:
                    attachments = inline_atts
            out.append({
                "author": author if author else "未知",
                "content": content if isinstance(content, str) else str(content),
                "created_at": created_at,
                "created_at_str": _ts_to_str(created_at, full=True),
                "attachments": attachments,
            })

        # 按时间倒序（最新在前）
        out.sort(key=lambda c: _to_sortable(c.get("created_at")), reverse=True)
        return out

    # ------------------------------------------------------------------ 导出
    def _collect_tasks_for_export(self, projects, project_id, owner_filter, keyword, member_map):
        """全量收集任务（board 视图导出用），复用 _iter_all_tasks 并应用 owner + keyword 过滤。

        过滤规则与页面 board 视图完全一致：
        - owner_filter: None/"" → 不过滤；"__unassigned__" → 仅未分配；其他 → 按 _assignee_uid 精确匹配
        - keyword: 多个用 | 分隔，按 title 大小写不敏感 OR 命中（与 search_tasks 一致）
        返回 normalize_task 后的任务列表（不过滤已完成，方便导出全量看板）。
        """
        member_map = member_map or {}
        if keyword:
            kws_lc = [k.strip().lower() for k in keyword.split("|") if k.strip()]
        else:
            kws_lc = []
        out = []
        for t, _pname in self._iter_all_tasks(
                projects, project_id, self._FETCH_PAGE, member_map):
            # 负责人过滤（uid 形态，与 _compute_overdue 同源）
            if owner_filter and owner_filter != "":
                uid = t.get("_assignee_uid")
                if owner_filter == "__unassigned__":
                    if uid:                       # 有负责人 → 不算未分配
                        continue
                elif uid != owner_filter:
                    continue
            # 标题关键字 OR 过滤（keyword 为空表示不过滤）
            if kws_lc:
                title = (t.get("title") or "").lower()
                if not any(kw in title for kw in kws_lc):
                    continue
            out.append(t)
        # 补全描述：列表接口不带 desc，需逐任务查详情（导出为全量，耗时较长属预期）
        self.enrich_tasks_with_desc(out)
        return out

    def fetch_all_comments(self, task_ids, member_map=None, on_progress=None):
        """并发拉取多个任务的评论，返回 {task_id: comments_list}。

        复用 get_task_comments（已含 401 刷新 + 429/5xx 退避重试 + 文件信息补全）。
        并发上限沿用 _MAX_FILE_INFO_WORKERS，避免「全量任务 + 评论」导出时触发限流。
        单条任务失败不影响其他，降级为该 task_id 对应空列表。

        on_progress(done, total)：可选回调，每完成一个任务调用一次（用于导出进度条）。
        不做实时上报的场景传 None 即可，行为不变。
        """
        member_map = member_map or {}
        result = {}
        if not task_ids:
            if on_progress:
                on_progress(0, 0)
            return result
        ids = list(task_ids)
        total = len(ids)
        done = 0
        if on_progress:
            on_progress(0, total)

        def _fetch(tid):
            try:
                return tid, self.get_task_comments(tid, member_map=member_map)
            except Exception:
                return tid, []

        with ThreadPoolExecutor(max_workers=min(_MAX_FILE_INFO_WORKERS, total)) as pool:
            futures = {pool.submit(_fetch, tid): tid for tid in ids}
            for fut in as_completed(futures):
                tid, comments = fut.result()
                result[tid] = comments
                done += 1
                if on_progress:
                    on_progress(done, total)
        return result

    @staticmethod
    def _extract_comment_author(raw, member_map=None):
        """
        从一条评论字典里提取作者显示名。Worktile 实际字段名是 `created_by`（带 d）。
        兼容 author 字段返回 dict（含 display_name / name / uid）或纯字符串 uid。
        解析失败兜底返回 "未知"。
        """
        if not isinstance(raw, dict):
            return "未知"
        member_map = member_map or {}
        # 按顺序找作者字段。Worktile 评论通常使用 created_by（带 d）
        obj = _first(raw, [
            "created_by", "create_by",       # Worktile 实际字段
            "creator", "author", "user", "create_user",
            "operator", "uid", "user_id", "create_uid",
            "from", "from_user", "owner",
        ])
        if isinstance(obj, dict):
            # 1) 直接取显示名
            name = (obj.get("display_name") or obj.get("name")
                    or obj.get("user_name") or obj.get("username")
                    or obj.get("nickname") or obj.get("real_name"))
            if name:
                return str(name)
            # 2) 拿 uid 回查
            uid = (obj.get("_id") or obj.get("id") or obj.get("uid")
                   or obj.get("user_id"))
            if uid and isinstance(uid, str):
                return member_map.get(uid, uid)
            return "未知"
        if isinstance(obj, str) and obj:
            return member_map.get(obj, obj)
        return "未知"

    def get_file_infos(self, file_ids):
        """
        批量获取文件信息（文件名 / 扩展名 / 类型），返回 {file_id: {"title","ext","type"}}。

        - 实例级缓存 _file_cache，重复 ID 不重复请求。
        - 多个未缓存的 ID 用线程池并发请求 /file/{id}/info，避免串行拖慢评论拉取。
        - 单个 ID 请求失败不影响其他，降级为 {title: file_id}。
        """
        result = {}
        pending = []
        for fid in file_ids:
            if fid in self._file_cache:
                result[fid] = self._file_cache[fid]
            elif fid not in pending:
                pending.append(fid)

        if pending:
            def _fetch(fid):
                try:
                    data = self._req("GET", f"/file/{fid}/info")
                    info = data
                    if isinstance(data, dict):
                        if isinstance(data.get("data"), dict):
                            inner = data["data"]
                            info = inner.get("value", inner)
                        elif "value" in data:
                            info = data["value"]
                    val = info if isinstance(info, dict) else {}
                    rec = {
                        "title": val.get("title") or val.get("name") or fid,
                        "ext": (val.get("ext") or "").lower(),
                        "type": val.get("type"),
                    }
                    return fid, rec
                except Exception:
                    return fid, {"title": fid, "ext": "", "type": None}

            with ThreadPoolExecutor(max_workers=min(_MAX_FILE_INFO_WORKERS, len(pending))) as pool:
                for fid, rec in pool.map(_fetch, pending):
                    self._file_cache[fid] = rec
                    result[fid] = rec

        return result

    def get_file_stream(self, file_id):
        """
        代理下载文件字节流，返回 (bytes, content_type)。

        - 自动附带 access_token；遇到 401 刷新 token 重试一次。
        - 复用 _req 的退避重试：429/5xx 瞬时故障时自动重试（最多 3 次），
          与评论接口保持一致，避免附件预览偶发失败时只能靠刷新页面。
        - 用文件信息缓存里的 ext 推断更精确的 MIME（Worktile 的 download 接口
          有时返回 application/octet-stream，浏览器据此无法预览 PDF / 图片），
          从而让前端 <img> 直接展示、<a> 在新标签预览 PDF。
        """
        # raw=True：_req 直接返回 requests.Response（含 .content / .headers），不做 .json()
        # timeout=60：文件可能较大，比普通 JSON 接口（30s）宽松
        resp = self._req("GET", f"/file/{file_id}/download", raw=True, timeout=60)

        ctype = resp.headers.get("Content-Type", "") or ""
        ctype = ctype.split(";")[0].strip()
        # 用 file info 缓存的 ext 推断更精确的 MIME，便于浏览器预览
        rec = self._file_cache.get(file_id)
        if rec and rec.get("ext"):
            guessed = mimetypes.guess_type("f." + rec["ext"])[0]
            if guessed and (not ctype or ctype == "application/octet-stream"):
                ctype = guessed
        if not ctype:
            ctype = "application/octet-stream"
        return resp.content, ctype

    @staticmethod
    def _extract_attachments(raw, file_map=None):
        """
        从评论里提取附件列表 [{name, is_image, file_id}]。

        入参 raw 可能是：
          - 字符串列表：Worktile 评论顶层 attachments 实际返回的是「文件 ID」列表
                        此时用 file_map 把 ID 还原成真实文件名（data.value.title）。
          - dict 列表：每个 dict 自带 name/type 等字段（也可能只有 id，需 file_map 补名字）。

        file_map: {file_id -> {"title", "ext", "type"}}，由 get_file_infos 填充。
        """
        file_map = file_map or {}
        if not raw:
            return []
        if not isinstance(raw, list):
            raw = [raw]
        out = []
        for item in raw:
            if isinstance(item, dict):
                fid = _first(item, ["id", "file_id", "fileId", "_id"])
                name = (_first(item, ["file_name", "name", "filename", "title",
                                      "display_name", "origin_name"]) or "")
                # dict 自带字段没名字但有 id -> 用 file_map 补
                if not name and fid:
                    rec = file_map.get(fid)
                    if rec:
                        name = rec.get("title", fid)
                if not name:
                    name = fid or "未命名文件"
                ftype = (_first(item, ["type", "file_type", "ext", "content_type",
                                       "mime_type"]) or "")
                is_image = bool(_ext(name) in IMAGE_EXT)
                if isinstance(ftype, str):
                    is_image = is_image or ("image" in ftype.lower())
                out.append({"name": str(name), "is_image": is_image, "file_id": fid})
            else:
                # 纯字符串：当作文件 ID 处理
                fid = str(item)
                rec = file_map.get(fid)
                if rec:
                    name = rec.get("title", fid)
                    is_image = bool(_ext(name) in IMAGE_EXT)
                else:
                    # 兜底：没查到信息时直接拿 ID 当名字显示（降级）
                    name = fid
                    is_image = bool(_ext(fid) in IMAGE_EXT)
                out.append({"name": name, "is_image": is_image, "file_id": fid})
        return out

    @staticmethod
    def _extract_inline_attachments(raw_content):
        """从富文本 JSON 树里递归抽取 inline image/file 节点（[{name, is_image}]）"""
        if not raw_content:
            return []
        # 字符串先尝试 JSON 解析
        if isinstance(raw_content, str):
            s = raw_content.strip()
            if s.startswith("[") or s.startswith("{"):
                try:
                    raw_content = _json.loads(s)
                except (ValueError, TypeError):
                    return []
            else:
                return []

        out = []
        stack = [raw_content]
        while stack:
            n = stack.pop()
            if isinstance(n, list):
                for x in n:
                    stack.append(x)
                continue
            if not isinstance(n, dict):
                continue
            ntype = n.get("type", "")
            if ntype in ("image", "inline_image", "inline-image"):
                name = (_first(n, ["display_name", "name", "file_name", "alt"]) or "图片")
                out.append({"name": name, "is_image": True})
            elif ntype in ("file", "attachment", "inline_attachment", "inline-attachment"):
                name = (_first(n, ["display_name", "name", "file_name", "filename"]) or "文件")
                # 通过 mime / type 字段判断是否图片
                ftype = (_first(n, ["type", "content_type", "mime_type"]) or "")
                is_image = isinstance(ftype, str) and "image" in ftype.lower()
                out.append({"name": name, "is_image": is_image})
            if "children" in n and isinstance(n["children"], (list, dict)):
                stack.append(n["children"])
        # 去重（同名图片/文件保留首次出现）
        seen = set()
        uniq = []
        for a in out:
            key = (a.get("name"), a.get("is_image"))
            if key not in seen:
                seen.add(key)
                uniq.append(a)
        return uniq


def _ext(name):
    """取文件扩展名（小写，含点）"""
    if not isinstance(name, str) or "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


# Worktile 评论里把 @mention 序列化成纯文本时的内联语法：
#   [@<uid>|<display_name>]  或  [@<uid>]
# uid 一般是 24 位十六进制的 MongoDB ObjectId；display_name 可能是姓名/昵称，
# 也可能在某些旧评论里是空（只剩 [@uid]）。这种格式是真实数据里观察到的，
# 不是 JSON 富文本树，必须在 JSON 解析前先替换，否则整段被当成纯文本返回。
_INLINE_MENTION_RE = re.compile(
    r"\[@([A-Za-z0-9_\-]+)(?:\|([^\]]*))?\]"
)


def _normalize_inline_mentions(text, member_map=None):
    """把 [@uid|name] / [@uid] 内联语法还原成 @<真实姓名>。

    优先用括号里的 name（一般是作者写下时填的，可能已是姓名）。
    若括号里的 name 缺失或等于 uid，再用 member_map 回查。
    """
    if not text:
        return text

    def _sub(m):
        uid = m.group(1)
        name = (m.group(2) or "").strip()
        # 去掉 [uid|uid] 这种前后相同的退化情况
        if name == uid:
            name = ""
        if not name and member_map:
            name = member_map.get(uid, "") or ""
        if not name:
            # 最后兜底：保留 uid 前 8 位，让人能区分但不至于被整段污染
            name = uid[:8]
        return "@" + name

    return _INLINE_MENTION_RE.sub(_sub, text)


def rich_text_to_plain(content, member_map=None):
    """
    把 Worktile 评论的富文本 schema（类 ProseMirror 的 JSON 树）转成纯文本。

    兼容：
    - 字符串：先尝试 JSON.loads（Worktile 把评论 content 序列化成 JSON 字符串），
      解析失败则原样返回。
    - 已经是 list / dict：直接递归展开。
    - None / 其他：原样转 str 返回。
    - **内联 mention 语法**：若字符串里含 [@uid|name]，先把它还原成 @姓名，
      否则整段会被当纯文本字面量输出。

    参数:
        content: 评论内容（str / list / dict / None）
        member_map: 可选，{uid: display_name}，用于把 @mention 中的纯 ID 还原成真实姓名
    """
    if content is None:
        return ""
    if isinstance(content, str):
        s = content.strip()
        if s.startswith("[") or s.startswith("{"):
            try:
                content = _json.loads(s)
            except (ValueError, TypeError):
                # 不是合法 JSON —— 可能是含 [@uid|name] 的纯文本评论，
                # 走内联 mention 还原后返回
                return _normalize_inline_mentions(s, member_map).strip()
        else:
            return _normalize_inline_mentions(s, member_map).strip()
    if isinstance(content, (list, dict)):
        return _rt_walk(content, member_map).strip()
    return str(content)


def _child_nodes(node):
    """取容器节点的子节点列表。

    Worktile 富文本里，容器节点既可能用 ProseMirror 标准的 ``content`` 键，
    也可能用 ``children`` 键（例如 emoji 节点），统一兼容。
    """
    if not isinstance(node, dict):
        return []
    for key in ("content", "children"):
        v = node.get(key)
        if isinstance(v, list) and v:
            return v
    return []


def _rt_walk(node, member_map=None):
    """递归遍历富文本节点，返回纯文本（多 block 之间用换行）"""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        parts = [_rt_walk(n, member_map) for n in node]
        return "\n".join(p for p in parts if p)
    if not isinstance(node, dict):
        return str(node)

    ntype = node.get("type", "")

    # ---- inline 节点 ----
    # 文本节点（含 marks 但不影响文本内容）
    if ntype == "text" or "text" in node and not node.get("children"):
        return node.get("text", "")

    if ntype in ("mention", "user", "at", "user_mention"):
        return "@" + _resolve_mention_name(node, member_map)

    if ntype in ("image", "inline_image", "inline-image"):
        return "[图片]"

    if ntype in ("file", "attachment", "inline_attachment", "inline-attachment"):
        name = (node.get("display_name") or node.get("name")
                or node.get("file_name") or node.get("filename") or "文件")
        return f"[文件：{name}]"

    if ntype == "emoji":
        # Worktile 表情节点：{"type":"emoji","code":"joy","children":[{"text":""}]}
        # code 是 Slack-style shortcode，转成 unicode emoji 字符
        code = node.get("code") or node.get("name") or node.get("text") or ""
        return _resolve_emoji_shortcode(code)

    if ntype == "link":
        # Worktile 实际 schema：{"type":"link","url":"...","children":[{"text":"..."}]}
        # 也兼容 {"text":"...","href":"..."} 这种
        direct = (node.get("text") or node.get("url")
                  or node.get("href") or "")
        if direct:
            return direct
        for ch in (node.get("children") or []):
            if isinstance(ch, dict) and isinstance(ch.get("text"), str) and ch.get("text"):
                return ch["text"]
        return ""

    if ntype in ("hard_break", "br"):
        return "\n"

    # ---- block 节点 ----
    # 块间换行由顶层 list 的 "\n".join 负责，节点本身返回内容不带末尾 '\n'

    if ntype in ("code_block", "code"):
        text = node.get("text", "")
        content = node.get("content", "")
        # Worktile 偶尔会把整个富文本 JSON 塞进外层 code 节点的 content 字段
        # （常见于纯 emoji 评论或单行内容），这里递归解析
        if not text and isinstance(content, str):
            s = content.strip()
            if s.startswith("[") or s.startswith("{"):
                try:
                    inner = _json.loads(s)
                    return _rt_walk(inner, member_map)
                except (ValueError, TypeError):
                    pass
        if isinstance(content, list):
            return "\n".join(p for p in (_rt_walk(c, member_map) for c in content) if p)
        return text or (content if isinstance(content, str) else "") or ""

    if ntype in ("list_item", "list-item", "li", "todo_item", "todo-item"):
        inner = "".join(_rt_walk(c, member_map) for c in _child_nodes(node))
        return "• " + inner

    if ntype in ("ordered_list", "unordered_list", "bullet_list",
                 "ordered-list", "unordered-list", "list",
                 "todo_list", "todo-list", "check_list", "check-list"):
        return "\n".join(_rt_walk(c, member_map) for c in _child_nodes(node))

    if ntype in ("quote", "blockquote"):
        inner = "".join(_rt_walk(c, member_map) for c in _child_nodes(node))
        return "\n".join("> " + line for line in inner.splitlines() if line)

    if ntype in ("heading",):
        level = node.get("level") or 1
        inner = "".join(_rt_walk(c, member_map) for c in _child_nodes(node))
        return ("#" * level) + " " + inner

    if ntype in ("paragraph", "p", "doc"):
        return "".join(_rt_walk(c, member_map) for c in _child_nodes(node))

    # 默认：尝试 content 或 children（ProseMirror 标准用 content，部分节点用 children）
    kids = _child_nodes(node)
    if kids:
        return _rt_walk(kids, member_map)

    # 兜底：text 字段
    return node.get("text", "") or ""


def _resolve_mention_name(node, member_map=None):
    """
    从 mention 节点里取出被 @ 的真实姓名。
    兼容 Worktile 多种 schema：
      1) 顶层字段 display_name / name / text / username / nickname / user_name
      2) data 嵌套（Worktile 评论实际用这个）: data.name / display_name / user_name
      3) attrs 嵌套: attrs.display_name / name / nickname / username
      4) user 子对象: user.display_name / name / nickname / username
      5) 拿到 ID 时回查 member_map（uid/user_id/_id 在顶层 / attrs / data / user 四处）
      6) children 第一个文本节点
    拿不到则返回 "未知"。
    """
    if not isinstance(node, dict):
        return "未知"

    def _name_in(d):
        """从字典里挑一个非空字符串作为姓名候选"""
        if not isinstance(d, dict):
            return ""
        for k in ("display_name", "name", "user_name",
                  "username", "nickname", "real_name"):
            v = d.get(k)
            if isinstance(v, str) and v:
                return v
        return ""

    # 1) 顶层字段
    name = _name_in(node)

    # 2) attrs / data 嵌套（Worktile 评论实际把 name 放在 data.name）
    if not name:
        for k in ("attrs", "data"):
            name = _name_in(node.get(k))
            if name:
                break

    # 3) user 子对象
    if not name:
        name = _name_in(node.get("user"))

    # 4) children 第一个纯文本节点
    if not name:
        for ch in node.get("children", []) or []:
            if isinstance(ch, dict):
                t = ch.get("text")
                if isinstance(t, str) and t:
                    name = t
                    break

    # 5) ID 回查 member_map（覆盖顶层 / attrs / data / user 四处）
    if not name and member_map:
        uid = None
        for parent_key in (None, "attrs", "data", "user"):
            parent = node if parent_key is None else node.get(parent_key)
            if not isinstance(parent, dict):
                continue
            uid = (parent.get("uid") or parent.get("user_id")
                   or parent.get("_id") or parent.get("id"))
            if uid:
                break
        if uid:
            name = member_map.get(uid) or ""

    return name if name else "未知"
