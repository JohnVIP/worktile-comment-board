#!/usr/bin/env python3
"""验证 WorktileClient._req 的 429/5xx 退避重试逻辑（mock，无需真实网络）。

运行：
    python tests/test_wt_client_retry.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest import mock
import wt_client


class FakeResp:
    def __init__(self, status_code, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def make_client():
    c = wt_client.WorktileClient("dummy_id", "dummy_secret")
    c._token = "tok"  # 跳过真实认证请求
    return c


def _patch_request(seq):
    """返回一个 (patcher, calls) —— requests.request 按 seq 顺序返回 FakeResp。"""
    calls = {"n": 0}

    def fake_request(*a, **k):
        i = calls["n"]
        calls["n"] += 1
        return seq[i]

    patcher = mock.patch.object(wt_client.requests, "request", side_effect=fake_request)
    return patcher, calls


def test_retry_429_then_ok():
    c = make_client()
    seq = [
        FakeResp(429, text="<html>rate limited</html>", headers={"content-type": "text/html"}),
        FakeResp(429, text="<html>rate limited</html>", headers={"content-type": "text/html"}),
        FakeResp(200, json_data={"ok": True, "value": [1, 2, 3]},
                 headers={"content-type": "application/json"}),
    ]
    patcher, calls = _patch_request(seq)
    patcher.start()
    try:
        with mock.patch.object(wt_client.time, "sleep", lambda s: None):
            result = c._req("GET", "/x")
    finally:
        patcher.stop()
    assert result == {"ok": True, "value": [1, 2, 3]}, result
    assert calls["n"] == 3, f"应请求 3 次，实际 {calls['n']}"


def test_retry_after_header():
    c = make_client()
    seq = [
        FakeResp(429, text="<html>", headers={"content-type": "text/html", "Retry-After": "2"}),
        FakeResp(200, json_data={"ok": 1}, headers={"content-type": "application/json"}),
    ]
    patcher, calls = _patch_request(seq)
    patcher.start()
    sleeps = []
    try:
        with mock.patch.object(wt_client.time, "sleep", side_effect=lambda s: sleeps.append(s)):
            result = c._req("GET", "/x")
    finally:
        patcher.stop()
    assert result == {"ok": 1}
    assert calls["n"] == 2, f"应请求 2 次，实际 {calls['n']}"
    assert abs(sleeps[0] - 2.0) < 0.01, f"应尊重 Retry-After=2，实际 {sleeps}"


def test_exponential_backoff_without_retry_after():
    c = make_client()
    seq = [
        FakeResp(503, text="<html>busy</html>", headers={"content-type": "text/html"}),
        FakeResp(503, text="<html>busy</html>", headers={"content-type": "text/html"}),
        FakeResp(503, text="<html>busy</html>", headers={"content-type": "text/html"}),
        FakeResp(200, json_data={"ok": 9}, headers={"content-type": "application/json"}),
    ]
    patcher, calls = _patch_request(seq)
    patcher.start()
    sleeps = []
    try:
        with mock.patch.object(wt_client.time, "sleep", side_effect=lambda s: sleeps.append(s)):
            result = c._req("GET", "/x")
    finally:
        patcher.stop()
    assert result == {"ok": 9}
    # 指数退避：1s, 2s, 4s（attempt=0,1,2）
    assert sleeps == [1.0, 2.0, 4.0], f"退避序列应为 [1,2,4]，实际 {sleeps}"


def test_non_retryable_raises_immediately():
    c = make_client()
    seq = [FakeResp(404, json_data={"error": "not found"},
                    headers={"content-type": "application/json"})]
    patcher, calls = _patch_request(seq)
    patcher.start()
    try:
        with mock.patch.object(wt_client.time, "sleep", lambda s: None):
            try:
                c._req("GET", "/x")
                assert False, "应抛出 RuntimeError"
            except RuntimeError as e:
                assert "404" in str(e), f"错误应含 404，实际 {e}"
    finally:
        patcher.stop()
    assert calls["n"] == 1, f"非可重试错误不应重试，实际请求 {calls['n']} 次"


def test_exhaust_retries_then_raise():
    c = make_client()
    seq = [FakeResp(500, text="<html>err</html>", headers={"content-type": "text/html"})] * 5
    patcher, calls = _patch_request(seq)
    patcher.start()
    try:
        with mock.patch.object(wt_client.time, "sleep", lambda s: None):
            try:
                c._req("GET", "/x")
                assert False, "应抛出 RuntimeError"
            except RuntimeError as e:
                assert "500" in str(e) or "网关" in str(e), f"错误应含状态码，实际 {e}"
    finally:
        patcher.stop()
    # 初始 1 次 + 3 次重试 = 4 次
    assert calls["n"] == 4, f"应请求 4 次（1+3 重试），实际 {calls['n']}"


def test_get_file_stream_retries_429():
    c = make_client()
    ok = FakeResp(200, headers={"Content-Type": "image/png"})
    ok.content = b"PNGDATA"  # 模拟文件字节流
    seq = [
        FakeResp(429, text="<html>rate</html>", headers={"content-type": "text/html"}),
        ok,
    ]
    patcher, calls = _patch_request(seq)
    patcher.start()
    try:
        with mock.patch.object(wt_client.time, "sleep", lambda s: None):
            data, ctype = c.get_file_stream("fid1")
    finally:
        patcher.stop()
    assert data == b"PNGDATA", f"应返回文件字节流，实际 {data!r}"
    assert ctype == "image/png", f"应返回 content-type，实际 {ctype!r}"
    assert calls["n"] == 2, f"429 后重试成功应请求 2 次，实际 {calls['n']}"


def test_normalize_task_extracts_desc():
    c = make_client()
    # 1) 顶层 desc 直接提取
    t1 = c.normalize_task({"_id": "a1", "title": "T1", "desc": "  这是描述  "}, project_name="P")
    assert t1["desc"] == "这是描述", f"顶层 desc 应去空白，实际 {t1['desc']!r}"
    # 2) description 兜底
    t2 = c.normalize_task({"_id": "a2", "title": "T2", "description": "备选描述"}, project_name="P")
    assert t2["desc"] == "备选描述", f"description 兜底，实际 {t2['desc']!r}"
    # 3) properties.desc 兜底
    t3 = c.normalize_task({"_id": "a3", "title": "T3", "properties": {"desc": "属性描述"}}, project_name="P")
    assert t3["desc"] == "属性描述", f"properties.desc 兜底，实际 {t3['desc']!r}"
    # 4) 都没有 → 空串
    t4 = c.normalize_task({"_id": "a4", "title": "T4"}, project_name="P")
    assert t4["desc"] == "", f"无描述应为空串，实际 {t4['desc']!r}"
    # 5) desc 为 None → 空串
    t5 = c.normalize_task({"_id": "a5", "title": "T5", "desc": None}, project_name="P")
    assert t5["desc"] == "", f"desc=None 应为空串，实际 {t5['desc']!r}"


def test_enrich_tasks_with_desc():
    c = make_client()
    tasks = [
        {"task_id": "t1", "desc": ""},            # 需补全
        {"task_id": "t2", "desc": "已有值"},       # 已填值，跳过
        {"task_id": "t3"},                        # 无 desc key，需补全
    ]
    # mock 详情接口：t1 → "详情描述"，t3 → "属性描述"，其它（含已填值的不应被调用）
    def fake_get(task_id):
        return {"t1": "详情描述", "t3": "属性描述"}.get(task_id, "")
    with mock.patch.object(c, "_get_task_desc", side_effect=fake_get):
        enriched, failed = c.enrich_tasks_with_desc(tasks)
    assert enriched == 2, f"应补全 2 条（t1/t3），实际 {enriched}"
    assert failed == 0, f"失败数应为 0，实际 {failed}"
    assert tasks[0]["desc"] == "详情描述", f"t1 应补全，实际 {tasks[0]['desc']!r}"
    assert tasks[1]["desc"] == "已有值", f"t2 已填值不应被覆盖，实际 {tasks[1]['desc']!r}"
    assert tasks[2]["desc"] == "属性描述", f"t3 应补全，实际 {tasks[2]['desc']!r}"
    # 空列表 / 全部已填 → 不发起请求
    with mock.patch.object(c, "_get_task_desc", side_effect=AssertionError("不应被调用")):
        assert c.enrich_tasks_with_desc([{"task_id": "x", "desc": "y"}]) == (0, 0)
        assert c.enrich_tasks_with_desc([]) == (0, 0)


if __name__ == "__main__":
    test_retry_429_then_ok()
    test_retry_after_header()
    test_exponential_backoff_without_retry_after()
    test_non_retryable_raises_immediately()
    test_exhaust_retries_then_raise()
    test_get_file_stream_retries_429()
    test_normalize_task_extracts_desc()
    test_enrich_tasks_with_desc()
    print("全部 7 个重试/字段测试通过 ✓")
