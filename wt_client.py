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
import re
import requests
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic"}

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

    # ------------------------------------------------------------------ 认证
    def _ensure_token(self):
        if self._token:
            return self._token
        url = f"{self.base_url}/open-api/tenant-access-token"
        try:
            resp = requests.post(
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

    def _req(self, method, path, params=None, json=None):
        def _do_request(token):
            p = dict(params or {})
            p["access_token"] = token
            url = f"{self.base_url}/open-api{path}"
            try:
                resp = requests.request(method, url, params=p, json=json, timeout=30)
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

        if resp.status_code >= 400:
            raise RuntimeError(f"接口错误 {resp.status_code} {path}：{resp.text[:300]}")
        return resp.json()

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
        return {
            "task_id": task_id,
            "identifier": identifier,
            "title": title,
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
        }

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

            with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
                for fid, rec in pool.map(_fetch, pending):
                    self._file_cache[fid] = rec
                    result[fid] = rec

        return result

    def get_file_stream(self, file_id):
        """
        代理下载文件字节流，返回 (bytes, content_type)。

        - 自动附带 access_token；遇到 401 刷新 token 重试一次。
        - 用文件信息缓存里的 ext 推断更精确的 MIME（Worktile 的 download 接口
          有时返回 application/octet-stream，浏览器据此无法预览 PDF / 图片），
          从而让前端 <img> 直接展示、<a> 在新标签预览 PDF。
        """
        token = self._token or self._ensure_token()
        url = f"{self.base_url}/open-api/file/{file_id}/download"
        resp = requests.get(url, params={"access_token": token}, timeout=60)
        if resp.status_code == 401:
            self._token = None
            token = self._ensure_token()
            resp = requests.get(url, params={"access_token": token}, timeout=60)
        if resp.status_code >= 400:
            raise RuntimeError(f"文件下载失败 {resp.status_code}")

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
