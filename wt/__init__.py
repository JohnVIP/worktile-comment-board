"""Worktile OpenAPI 客户端包。

模块划分：
- wt.client      WorktileClient（认证 / 重试 / 项目 / 任务 / 评论 / 文件 / 缓存）
- wt.textutil    字段与时间解析、描述抽取、元数据识别等纯函数
- wt.richtext    评论富文本（类 ProseMirror JSON）→ 纯文本、@mention、emoji
- wt.audit       描述抽取诊断的环形缓冲

历史入口 wt_client.py 仍保留为兼容层（re-export）。
"""

from .client import WorktileClient

__all__ = ["WorktileClient"]
