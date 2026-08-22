"""Worktile OpenAPI 客户端（团队鉴权 Tenant 模式）。

特点：
- 凭证在运行时通过构造函数传入，不依赖 .env 文件，适合 Web 应用按租户动态鉴权
- access_token 缓存在内存，遇到 401 自动刷新一次
- 429/5xx 指数退避重试（尊重 Retry-After）
- 全量任务列表 / 任务描述有实例级 TTL 缓存（见 _TASKS_CACHE_TTL / _DESC_CACHE_TTL）
- 字段解析依赖 wt.textutil / wt.richtext 的兼容函数

参考：worktile-v2 技能的 _worktile_api.py / _config.py
"""

import json as _json
import logging
import mimetypes
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

from .audit import _record_desc_audit, _summarize_for_desc_audit
from .richtext import rich_text_to_plain
from .textutil import (
    _clean_desc_text, _due_to_epoch, _extract_assignee_uid, _extract_desc_images,
    _first, _is_completed, _pick_desc_from_obj, _pick_desc_with_path,
    _start_to_epoch, _status_display_name, _to_sortable, _ts_to_str,
)

logger = logging.getLogger(__name__)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic"}

# 文件信息并发数：Worktile 限流较严格，并发过高（如 8）易触发 429，
# 调小到 3 既够用又更稳，尤其当一条评论带多个附件需并发查文件名时。
_MAX_FILE_INFO_WORKERS = 3

# 任务描述缓存 TTL（秒）：描述几乎不变，且「真正没有描述的任务」如果不做
# 负缓存，会在每次翻页/刷新时被反复打详情接口。设 0 可关闭缓存。
# 注意：TTL 内点「刷新」不会拉到更新的描述（评论缓存不受影响）。
_DESC_CACHE_TTL = float(os.environ.get("WT_DESC_CACHE_TTL", "300"))

# 全量任务列表缓存 TTL（秒）：搜索 / typeahead / 导出 / 延期扫描都要全量
# 遍历所有项目（实测 89 项目约数百请求）；不缓存时每敲一次搜索键都重新扫一遍。
# 缓存的是原始任务（未 normalize），命中后仍按当前 member_map 重新规范化，
# 负责人改名能即时反映。「延期任务」视图点「刷新」会用 force=True 绕过。
# 设 0 可关闭缓存。
_TASKS_CACHE_TTL = float(os.environ.get("WT_TASKS_CACHE_TTL", "120"))


class WorktileClient:
    def __init__(self, client_id, client_secret, base_url="https://dev.worktile.com"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = (base_url or "https://dev.worktile.com").rstrip("/")
        self._token = None
        self._file_cache = {}      # file_id -> {"title", "ext", "type"} 实例级缓存
        self._desc_cache = {}      # task_id -> (cached_at, desc, images)；空结果也缓存（负缓存）
        self._tasks_cache = {}     # project_id -> (cached_at, [(raw_task, project_name)])
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
            logger.warning("%s %s 触发退避重试 (%d/%d)，等待 %.1fs",
                           resp.status_code, path, _attempt + 1, self._MAX_RETRIES, wait)
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
            v1_error = None
            try:
                d1 = f1.result()
            except Exception as e:
                d1 = {}
                v1_error = e
            try:
                d2 = f2.result()
            except Exception:
                d2 = {}
        if v1_error is not None:
            # V1 是任务列表的唯一数据源（items 只从 V1 取）。它失败时若静默返回空，
            # 用户会把「接口故障」误读成「项目没有任务」，必须把真实错误抛出去。
            raise v1_error

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

        注：看板翻页走本方法（按页懒拉、尽早停），与全量缓存 _fetch_raw_tasks
        （搜索/导出/延期用）互不影响。
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

    def _fetch_raw_tasks(self, projects, project_id, page_size, member_map=None, force=False):
        """全量拉取目标范围（单项目或全部项目）的原始任务，带实例级 TTL 缓存。

        搜索 / typeahead / 导出 / 延期扫描都要全量遍历；不缓存时每次按键、每次
        导出都会重新扫所有项目（实测 89 项目 ≈ 数百请求，秒级耗时且易触发 429）。
        - 缓存 key 是 project_id（范围），缓存值是 [(raw_task, project_name)]。
        - 缓存原始任务而非 normalize 后的结果：命中后仍按当前 member_map 重新
          规范化，负责人改名/成员补全能即时反映。
        - force=True 绕过缓存重新拉取并覆盖（延期视图「刷新」按钮用）。
        """
        member_map = member_map or {}
        cached = self._tasks_cache.get(project_id)
        if (not force and _TASKS_CACHE_TTL > 0 and cached is not None
                and time.time() - cached[0] < _TASKS_CACHE_TTL):
            return cached[1]

        out = []
        if project_id == "__all__":
            for p in projects:
                pi = 0
                while True:
                    raw = self.get_tasks_page(p["id"], page_index=pi, page_size=page_size)
                    for uid, name in (raw.get("users_map") or {}).items():
                        member_map.setdefault(uid, name)
                    items = raw.get("items") or []
                    if not items:
                        break
                    for t in items:
                        out.append((t, p.get("name", "")))
                    if not raw.get("has_more", False):
                        break
                    pi += 1
        else:
            project_name = next((p.get("name", "") for p in projects if p.get("id") == project_id), "")
            pi = 0
            while True:
                raw = self.get_tasks_page(project_id, page_index=pi, page_size=page_size)
                for uid, name in (raw.get("users_map") or {}).items():
                    member_map.setdefault(uid, name)
                items = raw.get("items") or []
                if not items:
                    break
                for t in items:
                    out.append((t, project_name))
                if not raw.get("has_more", False):
                    break
                pi += 1

        if _TASKS_CACHE_TTL > 0:
            self._tasks_cache[project_id] = (time.time(), out)
        return out

    def _iter_all_tasks(self, projects, project_id, page_size, member_map, force=False):
        """迭代目标范围（单项目或全部项目）的全部任务，yields (normalize 后的任务, project_name)。

        数据来自 _fetch_raw_tasks（带 TTL 缓存），normalize 用调用方传入的
        member_map 现算，因此缓存命中时负责人姓名也能反映最新成员表。
        """
        member_map = member_map or {}
        for t, pname in self._fetch_raw_tasks(projects, project_id, page_size,
                                              member_map, force=force):
            yield self.normalize_task(t, project_name=pname, member_map=member_map), pname

    def list_task_titles(self, projects, project_id, q, limit=20, page_size=200):
        """按 title 子串（大小写不敏感）取候选任务列表，用于前端 typeahead。

        返回 {items: [{task_id, identifier, title, project_name}], matched: int}：
        - items 至多 limit 条；matched 是过滤后命中总数（前端用于「是否还有更多」提示）。
        - q 为空时返回前 limit 条全量任务（按拉取顺序），方便探索。
        - 只拉 title 类字段，不含评论；全量遍历走 _fetch_raw_tasks 的 TTL 缓存，
          连续敲键不会反复扫全量。
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
        否则分页切片会漏掉不在当前页的命中项。全量数据走 _fetch_raw_tasks
        的 TTL 缓存，连续搜索/翻页不会重复扫描。
        """
        # 归一为 list[str]（OR 语义）
        if keyword_or_keywords is None or keyword_or_keywords == "":
            kws = []
        elif isinstance(keyword_or_keywords, str):
            kws = [k.strip() for k in keyword_or_keywords.split("|") if k.strip()]
        else:
            kws = [str(k).strip() for k in keyword_or_keywords if k and str(k).strip()]
        kws_lc = [k.lower() for k in kws]

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
        desc 审计环形缓冲（wt.audit），便于 _pick_desc_from_obj 漏命中时定位真实字段名。
        """
        # 实例级 TTL 缓存（含负缓存）：描述几乎不变；无描述的任务若无负缓存，
        # 每次翻页/刷新都会被 enrich 重新打一遍详情接口（几百任务的页非常可观）。
        cached = self._desc_cache.get(task_id)
        if cached is not None and _DESC_CACHE_TTL > 0 \
                and time.time() - cached[0] < _DESC_CACHE_TTL:
            return cached[1], cached[2]
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
            logger.warning("[wt-desc] %s: empty. top-level keys=%s",
                           task_id, audit["top_keys"][:30])
        if _DESC_CACHE_TTL > 0:
            self._desc_cache[task_id] = (time.time(), desc, desc_images)
        return (desc, desc_images)

    def enrich_tasks_with_desc(self, tasks, concurrency=None):
        """批量补全任务的 desc / desc_images（列表接口不带描述，需逐任务查详情）。

        原地更新 tasks 中的 dict；返回 (enriched, failed)。
        - 只对「当前 desc 为空」的任务发起请求，已填值的跳过（幂等）。
        - concurrency 默认用模块级 _MAX_FILE_INFO_WORKERS（=3），与附件/评论并发一致，避免 429。
        - 任务量大（如全部项目）时调用方应只传「当前页/当前视图要展示的子集」以控制请求数。
        - 结果有实例级 TTL 缓存（见 _get_task_desc），重复调用同批任务不会重复请求。
        """
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
        """全量收集任务（board 视图导出用），复用 _fetch_raw_tasks 并应用 owner + keyword 过滤。

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
