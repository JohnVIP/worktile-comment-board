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
    """返回一个 (patcher, calls) —— Session.request 按 seq 顺序返回 FakeResp。

    wt_client 现在用 self._session.request（trust_env=False 的 Session），所以
    直接 patch Session 类的 request 方法，覆盖所有 Session 实例。
    """
    calls = {"n": 0}

    def fake_request(*a, **k):
        i = calls["n"]
        calls["n"] += 1
        return seq[i]

    patcher = mock.patch.object(wt_client.requests.Session, "request",
                                side_effect=fake_request)
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
    # 6) Worktile 真实结构：properties.desc = {"value": "..."} 嵌套对象
    t6 = c.normalize_task(
        {"_id": "a6", "title": "T6",
         "properties": {"desc": {"value": "【Tips】：通过屏幕左侧功能导航"}}},
        project_name="P")
    assert t6["desc"] == "【Tips】：通过屏幕左侧功能导航", \
        f"properties.desc.value 嵌套应能解析，实际 {t6['desc']!r}"
    # 7) 多层嵌套：{data: {value: "..."}}
    t7 = c.normalize_task(
        {"_id": "a7", "title": "T7",
         "properties": {"desc": {"data": {"value": "多层嵌套描述"}}}},
        project_name="P")
    assert t7["desc"] == "多层嵌套描述", \
        f"properties.desc.data.value 嵌套，实际 {t7['desc']!r}"
    # 8) description 键 + 嵌套
    t8 = c.normalize_task(
        {"_id": "a8", "title": "T8",
         "properties": {"description": {"value": "描述英文键"}}},
        project_name="P")
    assert t8["desc"] == "描述英文键", \
        f"properties.description.value 嵌套，实际 {t8['desc']!r}"
    # 9) 兜底：properties 里没 desc/description/content 等已知键，但有「用户备忘」这类
    #    长字符串时，仍然能扫到（排除元数据后取最长）
    t9 = c.normalize_task(
        {"_id": "a9", "title": "T9",
         "properties": {"assignee": "u1",
                        "user_memo": "这是个长一点的非字段描述内容，用于覆盖兜底扫描逻辑"}},
        project_name="P")
    assert "非字段描述" in t9["desc"], f"properties 兜底扫描应命中，实际 {t9['desc']!r}"
    # 10) 兜底不会把短元数据当描述
    t10 = c.normalize_task(
        {"_id": "a10", "title": "T10",
         "properties": {"assignee": "u1", "tag": "x"}},
        project_name="P")
    assert t10["desc"] == "", f"无可信描述应返回空串，实际 {t10['desc']!r}"
    # 11) 候选键命中但值是 hex ID（用户误填 / 接口默认值），应被元数据闸门过滤
    t11 = c.normalize_task(
        {"_id": "a11", "title": "T11",
         "properties": {"description": "61a7966411b2e46844c8c71f"}},
        project_name="P")
    assert t11["desc"] == "", \
        f"候选键命中但值是 24hex ID 应被过滤，实际 {t11['desc']!r}"
    # 12) 候选键命中但值是纯链接也应被过滤
    t12 = c.normalize_task(
        {"_id": "a12", "title": "T12",
         "properties": {"desc": "https://example.com/x"}},
        project_name="P")
    assert t12["desc"] == "", f"纯链接不应当描述，实际 {t12['desc']!r}"
    # 13) 但真正可用的 desc 字段嵌套 hex 内容仍应保留
    t13 = c.normalize_task(
        {"_id": "a13", "title": "T13",
         "properties": {"desc": {"value": "Tips这是一段正常描述"}}},
        project_name="P")
    assert t13["desc"].startswith("Tips"), f"正常 desc 应保留，实际 {t13['desc']!r}"


def test_extract_desc_value_robust():
    """_extract_desc_value 处理各种嵌套层级"""
    # 直接字符串
    assert wt_client._extract_desc_value("hello") == "hello"
    assert wt_client._extract_desc_value("  trim me  ") == "trim me"
    # None / 空
    assert wt_client._extract_desc_value(None) == ""
    assert wt_client._extract_desc_value({}) == ""
    assert wt_client._extract_desc_value([]) == ""
    # 嵌套 dict：{value: ...}
    assert wt_client._extract_desc_value({"value": "v1"}) == "v1"
    # 嵌套 dict：{content: ...}
    assert wt_client._extract_desc_value({"content": "c1"}) == "c1"
    # 多层嵌套
    assert wt_client._extract_desc_value({"data": {"value": "d1"}}) == "d1"
    assert wt_client._extract_desc_value({"value": {"text": "nested"}}) == "nested"
    # dict 候选键都不命中 → 兜底在 values 里递归
    assert wt_client._extract_desc_value({"foo": "bar1", "baz": {"value": "bar2"}}) in ("bar1", "bar2")
    # 列表：取第一个非空元素
    assert wt_client._extract_desc_value(["skip", "first valid", "another"]) == "skip"
    assert wt_client._extract_desc_value(["", "   ", "real"]) == "real"
    # bool 不当作数字
    assert wt_client._extract_desc_value(True) == ""
    # 数字 0 不是描述（用户截图反馈：描述列显示「0」）
    assert wt_client._extract_desc_value(0) == ""
    assert wt_client._extract_desc_value(0.0) == ""
    # 非零数字正常转字符串（兼容某些 description 字段存评分数字）
    assert wt_client._extract_desc_value(1.5) == "1.5"
    # 嵌套 dict 里包含数字 0 也应被识别为空
    assert wt_client._extract_desc_value({"value": 0}) == ""
    # ProseMirror JSON 字符串（用户截图：描述列显示 JSON 源码）
    pm_str = '[{"type":"paragraph","key":"HzEpG","children":[{"text":"学习项目中的各种功能细节"}]},{"type":"paragraph","key":"krDFN","children":[{"text":"下午学习目标模块"}]}]'
    assert wt_client._extract_desc_value(pm_str) == \
        "学习项目中的各种功能细节\n下午学习目标模块", \
        f"PM JSON 字符串应抽文本，实际 {wt_client._extract_desc_value(pm_str)!r}"
    # 嵌套更深的 PM 结构（children → children → text）
    nested_pm = '[{"type":"doc","content":[{"type":"paragraph","content":[{"type":"text","text":"嵌套文本"}]}]}]'
    assert wt_client._extract_desc_value(nested_pm) == "嵌套文本"
    # 单段 paragraph 字符串
    single_pm = '[{"type":"paragraph","children":[{"text":"单段文本"}]}]'
    assert wt_client._extract_desc_value(single_pm) == "单段文本"
    # 解析失败 / 不是 JSON 形态 → 原样返回
    assert wt_client._extract_desc_value("[abc not json") == "[abc not json"
    assert wt_client._extract_desc_value("普通文本描述") == "普通文本描述"


def test_looks_like_metadata_value_zero():
    """元数据闸门：纯数字（含 0）应被识别为元数据。"""
    md = wt_client._looks_like_metadata_value
    assert md("0") is True, f"'0' 应为元数据"
    assert md("0.0") is True, f"'0.0' 应为元数据"
    assert md("12") is True, f"'12' 应为元数据"
    assert md("-3.14") is True, f"'-3.14' 应为元数据"
    assert md("50%") is True, f"'50%' 应为元数据"
    # 真描述不应被误判
    assert md("正常的描述文字") is False, f"真描述不应判为元数据"
    assert md("Tips提示") is False, f"短文本不应判为元数据"
    # 已有规则保持：hex ID / 链接 / ISO 时间仍为元数据
    assert md("61a7966411b2e46844c8c71f") is True
    assert md("https://example.com/x") is True


def test_normalize_task_filters_zero_desc():
    """normalize_task：候选键命中但值是数字 0 / 字符串「0」应返回空。"""
    c = make_client()
    # 候选键命中 description = 数字 0
    t1 = c.normalize_task(
        {"_id": "b1", "title": "T1", "properties": {"description": 0}},
        project_name="P")
    assert t1["desc"] == "", f"数字 0 应被过滤，实际 {t1['desc']!r}"
    # 候选键命中 description = 字符串 "0"
    t2 = c.normalize_task(
        {"_id": "b2", "title": "T2", "properties": {"description": "0"}},
        project_name="P")
    assert t2["desc"] == "", f"字符串 '0' 应被过滤，实际 {t2['desc']!r}"
    # 候选键命中 desc = 数字 0
    t3 = c.normalize_task(
        {"_id": "b3", "title": "T3", "properties": {"desc": 0}},
        project_name="P")
    assert t3["desc"] == "", f"desc=0 应被过滤，实际 {t3['desc']!r}"


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


def test_get_task_desc_passes_columns_param():
    """根因回归：详情接口必须传 columns=properties 才返回 properties（desc 藏在里面）。"""
    c = make_client()
    captured = {}

    def fake_req(method, path, params=None, json=None, **kw):
        captured["path"] = path
        captured["params"] = params
        # 模拟真实结构：data.value.properties.desc.value
        return {
            "code": 200,
            "data": {"value": {
                "_id": "t1",
                "title": "任务",
                "properties": {
                    "desc": {
                        "property_id": "p1",
                        "value": "【Tips】：通过屏幕左侧功能导航，可邀请您的同事加入 Worktile 。\n\n![08.gif](https://wt-box.worktile.com/x.gif)",
                        "updated_by": None,
                        "updated_at": 1652786254,
                    }
                },
            }},
        }

    with mock.patch.object(c, "_req", side_effect=fake_req):
        desc = c._get_task_desc("t1")
    assert captured["path"] == "/mission/tasks/t1", f"请求路径，实际 {captured.get('path')!r}"
    assert (captured.get("params") or {}).get("columns") == "properties", \
        f"必须传 columns=properties，实际 {captured.get('params')!r}"
    assert desc.startswith("【Tips】：通过屏幕左侧"), f"应抽到描述文本，实际 {desc!r}"
    assert "![" not in desc and "wt-box.worktile.com" not in desc, f"markdown 图片应被清理，实际 {desc!r}"


def test_clean_desc_text():
    """markdown 清理：图片移除、链接留文字、空行压缩。"""
    cd = wt_client._clean_desc_text
    assert cd("![08.gif](https://x.com/a.gif)") == ""
    assert cd("前文\n\n![img](https://x.com/a.gif)\n后文") == "前文\n\n后文"
    assert cd("[官网](https://x.com)") == "官网"
    assert cd("A\n\n\n\n\nB") == "A\n\nB"
    assert cd("  纯文本  ") == "纯文本"
    assert cd("") == ""
    assert cd(None) is None


if __name__ == "__main__":
    test_retry_429_then_ok()
    test_retry_after_header()
    test_exponential_backoff_without_retry_after()
    test_non_retryable_raises_immediately()
    test_exhaust_retries_then_raise()
    test_get_file_stream_retries_429()
    test_normalize_task_extracts_desc()
    test_extract_desc_value_robust()
    test_enrich_tasks_with_desc()
    test_get_task_desc_passes_columns_param()
    test_clean_desc_text()
    test_looks_like_metadata_value_zero()
    test_normalize_task_filters_zero_desc()
    print("全部 13 个重试/字段测试通过 ✓")
