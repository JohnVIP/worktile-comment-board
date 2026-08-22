"""Worktile 评论富文本解析：类 ProseMirror JSON 树 → 纯文本。

- 节点类型兼容：text / mention / image / file / emoji / link / code_block /
  list / quote / heading / paragraph 等（详见 _rt_walk）
- @mention：JSON 节点与 [@uid|name] 内联字符串两种形态都还原成 @姓名
- emoji：Slack-style shortcode → unicode（优先 emoji 库，缺失时用内置兜底表）
"""

import json as _json
import re

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
