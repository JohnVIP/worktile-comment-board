#!/usr/bin/env python3
"""2026-08 修复批次 的回归测试（mock 网络层，无需真实凭证）。

覆盖：
1. 延期任务缓存不再被 count_only 探测污染（负责人名/描述/排序保持正确）
2. 导出作业接口鉴权：未登录 401；作业只允许启动它的会话访问
3. 附件代理安全头：nosniff + CSP sandbox；HTML/SVG 强制下载
4. 调试接口默认 404，WT_DEBUG=1 才开放
5. _sweep_stale_state：过期会话与过期导出作业被回收
6. WorktileClient 描述缓存：TTL 内命中缓存，不发请求
7. get_tasks_page：V1 失败时抛出真实错误（不再静默返回空列表）
"""

import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_module          # noqa: E402
import wt.client as wt_client_impl  # noqa: E402  —— 常量补丁要打到实现模块
import wt_client                  # noqa: E402
from wt_client import WorktileClient  # noqa: E402


def _make_fake_session(client, member_map=None):
    return {
        "client_id": "cid", "client_secret": "csecret",
        "base_url": "https://dev.worktile.com",
        "tenant_host": "demo.worktile.com",
        "client": client,
        "projects": [{"id": "P1", "name": "项目P1"}],
        "projects_error": None,
        "member_map": member_map,   # 预填 → _ensure_member_map 不再发请求
        "created_at": time.time(),
    }


class OverdueFakeClient(WorktileClient):
    """延期视图专用：_iter_all_tasks 走真实 normalize_task（member_map 生效）。"""

    def __init__(self, raw_tasks):
        self._FETCH_PAGE = 200
        self._file_cache = {}
        self._desc_cache = {}
        self._raw_tasks = raw_tasks
        self.desc_calls = 0

    def _iter_all_tasks(self, projects, project_id, page_size, member_map,
                        force=False):
        for t in self._raw_tasks:
            yield self.normalize_task(t, project_name="项目P1",
                                      member_map=member_map), "项目P1"

    def enrich_tasks_with_desc(self, tasks, concurrency=None):
        # 记录调用即可，不给任务补描述（避免网络）
        self.desc_calls += 1


def _overdue_raw_tasks(now):
    return [
        {"_id": "1", "title": "逾期三天", "task_state": {"name": "进行中", "type": 2},
         "properties": {"assignee": "u1", "desc": {"value": "任务一描述"},
                        "due": {"date": now - 3 * 86400, "with_time": 0}}},
        {"_id": "2", "title": "逾期十天", "task_state": {"name": "进行中", "type": 2},
         "properties": {"assignee": "u2", "desc": {"value": "任务二描述"},
                        "due": {"date": now - 10 * 86400, "with_time": 0}}},
    ]


# --------------------------------------------------------------------------- 1. 延期缓存污染
def test_overdue_count_only_does_not_poison_cache():
    """count_only 探测后，正式视图必须重新计算：负责人显示姓名而非 uid。"""
    now = time.time()
    client = OverdueFakeClient(_overdue_raw_tasks(now))
    session = _make_fake_session(client, member_map={"u1": "张三", "u2": "李四"})
    with mock.patch.object(app_module, "_require_session", lambda: session):
        c = app_module.app.test_client()
        # 1) badge 探测（count_only=1）：只拿 total
        r1 = c.get("/api/tasks/overdue?project_id=P1&count_only=1")
        assert r1.status_code == 200
        assert r1.get_json()["total"] == 2
        # 探测不写缓存（否则正式视图会拿到 uid 而非姓名）
        assert session.get("_overdue_cache") is None
        # 2) 正式视图：负责人必须是 member_map 里的姓名
        r2 = c.get("/api/tasks/overdue?project_id=P1&page=0&page_size=50")
        body = r2.get_json()
        assert body["total"] == 2
        assignees = {it["task_id"]: it["assignee"] for it in body["items"]}
        assert assignees["1"] == "张三", f"应为姓名，实际 {assignees!r}"
        assert assignees["2"] == "李四"
        # 按逾期天数降序
        assert [it["task_id"] for it in body["items"]] == ["2", "1"]


def test_overdue_count_only_can_read_full_cache():
    """反向兼容：正式视图算过之后，count_only 探测可复用缓存（口径一致）。"""
    now = time.time()
    client = OverdueFakeClient(_overdue_raw_tasks(now))
    session = _make_fake_session(client, member_map={"u1": "张三", "u2": "李四"})
    with mock.patch.object(app_module, "_require_session", lambda: session):
        c = app_module.app.test_client()
        r1 = c.get("/api/tasks/overdue?project_id=P1&page=0&page_size=50")
        assert r1.get_json()["total"] == 2
        calls_before = len(client._raw_tasks)  # 仅用于表示已扫过一轮
        r2 = c.get("/api/tasks/overdue?project_id=P1&count_only=1")
        assert r2.get_json()["total"] == 2
        assert calls_before == len(client._raw_tasks)


# --------------------------------------------------------------------------- 2. 导出作业鉴权
class ExportFakeClient(WorktileClient):
    def __init__(self):
        self._FETCH_PAGE = 200
        self._file_cache = {}
        self._desc_cache = {}

    def _iter_all_tasks(self, projects, project_id, page_size, member_map,
                        force=False):
        return iter([])

    def fetch_all_comments(self, task_ids, member_map=None, on_progress=None):
        if on_progress:
            on_progress(0, 0)
        return {}


def test_export_progress_requires_session():
    """未登录（无 sid cookie）访问 progress → 401。"""
    real = app_module._require_session
    try:
        # 不 patch：真实 _require_session 会因无 cookie abort(401)
        c = app_module.app.test_client()
        r = c.get("/api/export/progress?job_id=whatever")
        assert r.status_code == 401
        r2 = c.get("/api/export/download?job_id=whatever")
        assert r2.status_code == 401
    finally:
        app_module._require_session = real


def test_export_job_bound_to_session():
    """作业只允许启动它的会话轮询；其他会话/无会话一律 404。"""
    session = _make_fake_session(ExportFakeClient(), member_map={})
    real = app_module._require_session
    try:
        app_module._require_session = lambda: session
        owner = app_module.app.test_client()
        owner.set_cookie("wt_sid", "sid-owner")
        other = app_module.app.test_client()
        other.set_cookie("wt_sid", "sid-other")

        r = owner.post("/api/export/start",
                       json={"view": "board", "project_id": "P1",
                             "with_comments": False})
        assert r.status_code == 200
        job_id = r.get_json()["job_id"]

        final = None
        for _ in range(50):
            p = owner.get(f"/api/export/progress?job_id={job_id}")
            if p.status_code != 200:
                pytest.fail(f"属主会话轮询失败：{p.status_code}")
            body = p.get_json()
            if body["status"] in ("done", "error"):
                final = body
                break
            time.sleep(0.05)
        assert final and final["status"] == "done", final

        # 他人会话：404（不泄露作业是否存在）
        assert other.get(f"/api/export/progress?job_id={job_id}").status_code == 404
        assert other.get(f"/api/export/download?job_id={job_id}").status_code == 404
        # 属主下载正常
        dl = owner.get(f"/api/export/download?job_id={job_id}")
        assert dl.status_code == 200 and dl.data[:2] == b"PK"
    finally:
        app_module._require_session = real


# --------------------------------------------------------------------------- 3. 附件代理安全头
class FileStubClient:
    def __init__(self, ctype):
        self._ctype = ctype

    def get_file_stream(self, file_id):
        return b"<html><script>alert(1)</script></html>", self._ctype


@pytest.mark.parametrize("ctype,expect_attachment", [
    ("text/html", True),
    ("image/svg+xml", True),
    ("application/xhtml+xml", True),
    ("application/xml", True),
    ("image/png", False),
    ("application/pdf", False),
])
def test_file_proxy_security_headers(ctype, expect_attachment):
    session = _make_fake_session(FileStubClient(ctype))
    real = app_module._require_session
    try:
        app_module._require_session = lambda: session
        r = app_module.app.test_client().get("/api/file/f1")
        assert r.status_code == 200
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert "sandbox" in r.headers.get("Content-Security-Policy", "")
        if expect_attachment:
            assert r.headers.get("Content-Disposition") == "attachment"
        else:
            assert "Content-Disposition" not in r.headers
    finally:
        app_module._require_session = real


# --------------------------------------------------------------------------- 4. 调试接口开关
def test_debug_endpoints_gated(monkeypatch):
    session = _make_fake_session(ExportFakeClient(), member_map={})
    real = app_module._require_session
    try:
        app_module._require_session = lambda: session
        c = app_module.app.test_client()
        monkeypatch.delenv("WT_DEBUG", raising=False)
        assert c.get("/api/debug/desc-audit").status_code == 404
        assert c.get("/api/debug/task-detail/t1").status_code == 404
        assert c.get("/api/debug/comments-raw/t1").status_code == 404
        monkeypatch.setenv("WT_DEBUG", "1")
        assert c.get("/api/debug/desc-audit").status_code == 200
    finally:
        app_module._require_session = real


# --------------------------------------------------------------------------- 5. 后台清理
def test_sweep_stale_state():
    now = time.time()
    sessions_backup = dict(app_module.SESSIONS)
    jobs_backup = dict(app_module.EXPORT_JOBS)
    try:
        app_module.SESSIONS.clear()
        app_module.SESSIONS["fresh"] = {"created_at": now, "client_id": "a",
                                        "client_secret": "b", "base_url": "u",
                                        "tenant_host": "t"}
        app_module.SESSIONS["stale"] = {"created_at": now - 31 * 86400,
                                        "client_id": "a", "client_secret": "b",
                                        "base_url": "u", "tenant_host": "t"}
        app_module.EXPORT_JOBS.clear()
        app_module.EXPORT_JOBS["done-old"] = {"status": "done",
                                              "created_at": now - 3600, "sid": "x"}
        app_module.EXPORT_JOBS["done-new"] = {"status": "done",
                                              "created_at": now - 10, "sid": "x"}
        app_module.EXPORT_JOBS["running-stale"] = {"status": "running",
                                                   "created_at": now - 7200, "sid": "x"}
        app_module.EXPORT_JOBS["running-new"] = {"status": "running",
                                                 "created_at": now - 60, "sid": "x"}
        with mock.patch.object(app_module, "_persist_save"):
            app_module._sweep_stale_state()
        assert "fresh" in app_module.SESSIONS and "stale" not in app_module.SESSIONS
        assert "done-new" in app_module.EXPORT_JOBS
        assert "running-new" in app_module.EXPORT_JOBS
        assert "done-old" not in app_module.EXPORT_JOBS
        assert "running-stale" not in app_module.EXPORT_JOBS
    finally:
        app_module.SESSIONS.clear()
        app_module.SESSIONS.update(sessions_backup)
        app_module.EXPORT_JOBS.clear()
        app_module.EXPORT_JOBS.update(jobs_backup)


# --------------------------------------------------------------------------- 6. 描述缓存
def test_desc_cache_hits_within_ttl():
    c = WorktileClient("id", "secret")
    calls = []

    def fake_req(method, path, params=None, json=None, **kw):
        calls.append(path)
        return {"data": {"value": {"_id": "t1", "properties": {
            "desc": {"value": "描述内容"}}}}}

    with mock.patch.object(c, "_req", side_effect=fake_req):
        d1, _ = c._get_task_desc("t1")
        d2, _ = c._get_task_desc("t1")   # 应命中缓存
    assert d1 == d2 == "描述内容"
    assert len(calls) == 1, f"TTL 内应只请求 1 次，实际 {len(calls)}"


def test_desc_cache_negative_and_disable():
    c = WorktileClient("id", "secret")
    calls = []

    def fake_req(method, path, params=None, json=None, **kw):
        calls.append(path)
        return {"data": {"value": {"_id": "t1", "properties": {}}}}  # 无 desc

    with mock.patch.object(c, "_req", side_effect=fake_req):
        assert c._get_task_desc("t1")[0] == ""
        assert c._get_task_desc("t1")[0] == ""    # 负缓存命中
    assert len(calls) == 1

    # TTL=0 关闭缓存
    c2 = WorktileClient("id", "secret")
    with mock.patch.object(wt_client_impl, "_DESC_CACHE_TTL", 0):
        with mock.patch.object(c2, "_req", side_effect=fake_req):
            c2._get_task_desc("t1")
            c2._get_task_desc("t1")
    assert len(calls) == 3


# --------------------------------------------------------------------------- 6b. 任务列表 TTL 缓存
def test_tasks_list_cache():
    """TTL 内重复全量遍历只打一次接口；force 绕过；缓存命中仍按最新 member_map 规范化。"""
    c = WorktileClient("id", "secret")
    calls = {"n": 0}

    def fake_gtp(project_id, page_index=0, page_size=50):
        calls["n"] += 1
        return {"items": [{"_id": "t1", "title": "T",
                           "properties": {"assignee": "u1"}}],
                "total": 1, "has_more": False}

    projects = [{"id": "P1", "name": "项目P1"}]
    with mock.patch.object(c, "get_tasks_page", side_effect=fake_gtp):
        first = list(c._iter_all_tasks(projects, "P1", 200, {}))
        second = list(c._iter_all_tasks(projects, "P1", 200, {"u1": "张三"}))
        forced = list(c._iter_all_tasks(projects, "P1", 200, {}, force=True))
    assert calls["n"] == 2, f"应只真实拉取 2 次（首次 + force），实际 {calls['n']}"
    assert len(first) == 1 and first[0][0]["task_id"] == "t1"
    # 缓存命中时仍用调用方传入的 member_map 重新 normalize（改名即时生效）
    assert second[0][0]["assignee"] == "张三", \
        f"缓存命中应反映最新 member_map，实际 {second[0][0]['assignee']!r}"
    assert forced[0][0]["assignee"] == "u1"  # force 传空 map → 回退 uid


def test_tasks_list_cache_scopes_are_independent():
    """单项目与全部项目是两个缓存桶，互不串数据。"""
    c = WorktileClient("id", "secret")

    def fake_gtp(project_id, page_index=0, page_size=50):
        return {"items": [{"_id": f"{project_id}-t", "title": "T",
                           "properties": {}}],
                "total": 1, "has_more": False}

    projects = [{"id": "P1", "name": "项目P1"}, {"id": "P2", "name": "项目P2"}]
    with mock.patch.object(c, "get_tasks_page", side_effect=fake_gtp):
        one = [t["task_id"] for t, _ in c._iter_all_tasks(projects, "P1", 200, {})]
        allp = [t["task_id"] for t, _ in c._iter_all_tasks(projects, "__all__", 200, {})]
    assert one == ["P1-t"]
    assert allp == ["P1-t", "P2-t"]


# --------------------------------------------------------------------------- 7. V1 失败抛错
def test_get_tasks_page_raises_when_v1_fails():
    c = WorktileClient("id", "secret")
    c._token = "tok"

    def boom(method, url, **kw):
        raise RuntimeError("接口错误 500 /mission/get-project-tasks-by-page：boom")

    with mock.patch.object(c._session, "request", side_effect=boom):
        with pytest.raises(RuntimeError, match="get-project-tasks-by-page"):
            c.get_tasks_page("P1")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
