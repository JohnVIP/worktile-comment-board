"""Worktile 字段解析纯函数：描述抽取、时间/状态/负责人字段兼容。

Worktile 不同版本的字段名与嵌套形态差异都在这里消化，
client 层只调用这些稳定签名的函数。
"""

import json as _json
import re
import time
from datetime import datetime


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
    head = t[:19]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%Y/%m/%d"):
        try:
            datetime.strptime(head, fmt)
            return True
        except ValueError:
            continue
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
