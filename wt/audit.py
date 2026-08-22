"""描述抽取诊断：环形缓冲最近 N 次抽取结果，供调试接口展示。

每次 _get_task_desc 都把「详情顶层 keys + properties 摘要 + 是否命中」记录到
_DESC_AUDIT（deque，上限 30 条，线程安全），未命中时一眼能看到真实结构。
"""

import threading
from collections import deque

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
