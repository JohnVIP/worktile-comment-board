#!/usr/bin/env python3
"""Worktile OpenAPI 客户端 —— 兼容入口。

实现已拆分到 wt/ 包：
- wt.client   WorktileClient（认证 / 重试 / 任务 / 评论 / 文件 / 缓存）
- wt.textutil 字段与时间解析、描述抽取、元数据识别等纯函数
- wt.richtext 评论富文本 → 纯文本、@mention、emoji
- wt.audit    描述抽取诊断环形缓冲

本模块保留全部历史导出（app.py、tests/ 与外部脚本仍可 from wt_client import ...）。
新代码请直接 from wt.client import WorktileClient。
"""

import time  # noqa: F401  —— 测试通过 wt_client.time / wt_client.requests 打补丁（与实现模块是同一单例）
import requests  # noqa: F401

from wt.client import (          # noqa: F401
    WorktileClient, IMAGE_EXT, _MAX_FILE_INFO_WORKERS,
    _DESC_CACHE_TTL, _TASKS_CACHE_TTL, _ext,
)
from wt.textutil import (        # noqa: F401
    _first, _extract_assignee_uid, _DESC_KEYS, _extract_desc_value,
    _extract_desc_from_pm, _extract_desc_images, _pick_desc_from_obj,
    _pick_desc_with_path, _looks_like_metadata_value, _heuristic_desc_from_props,
    _clean_desc_text, _to_epoch_sec, _due_to_epoch, _start_to_epoch,
    _status_display_name, _is_completed, _ts_to_str, _to_sortable,
)
from wt.richtext import (        # noqa: F401
    rich_text_to_plain, _resolve_emoji_shortcode, _normalize_inline_mentions,
)
from wt.audit import (           # noqa: F401
    get_desc_audit_snapshot, _record_desc_audit,
)
