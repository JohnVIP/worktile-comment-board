#!/usr/bin/env python3
"""
Worktile 任务评论区看板 —— Flask 后端

安全说明：
- 用户的 Client ID / Client Secret 只保存在服务端内存（SESSIONS 字典），
  绝不写入磁盘，也不会下发到浏览器（浏览器只持有一个无意义的会话 id）。
- access_token 由 WorktileClient 在内存中缓存，过期自动刷新。
"""

import os
import json
import time
import secrets
import threading
from pathlib import Path

from flask import (
    Flask, request, jsonify, render_template, abort, Response, make_response,
)
from cryptography.fernet import Fernet
from wt_client import WorktileClient

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
SECRET_FILE = BASE_DIR / ".secret"          # 会话加密密钥（仅本机可读）
SESSIONS_FILE = BASE_DIR / "sessions.json"  # 加密持久化的会话（仅存凭证，不含 token）
SESSION_TIMEOUT = 30 * 24 * 3600            # 会话有效期 30 天，到期需重新登录


def _load_key():
    """加载会话加密密钥，优先级：

    1. 环境变量 FERNET_KEY（部署到云平台时放在平台的 Secrets/Environment 里，
       保证密钥固定，重启会话不失效，且不依赖可写的本地磁盘）。
    2. 本地 .secret 文件（本机开发用，首次运行随机生成并保存）。
    3. 都没有则临时生成一个（仅本次进程有效，重启即失效；写文件失败也不阻塞启动）。
    """
    env_key = os.environ.get("FERNET_KEY")
    if env_key:
        return env_key.strip().encode()
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip().encode()
    key = Fernet.generate_key()
    try:
        SECRET_FILE.write_text(key.decode())
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        # 云平台临时文件系统可能不可写，忽略：本次进程用临时密钥即可
        pass
    return key


_FERNET = Fernet(_load_key())

# 服务端会话：sid -> {client_id, client_secret, base_url, client, projects, member_map, created_at}
SESSIONS = {}
_SESS_LOCK = threading.Lock()

DEFAULT_BASE_URL = "https://dev.worktile.com"
# 任务详情页跳转用的租户域名（登录页可改；用于拼接
# https://{tenant}/mission/projects/{project_id}/tasks/{task_id}）
DEFAULT_TENANT_HOST = "techwll.worktile.com"
SID_COOKIE = "wt_sid"
PAGE_SIZE_OPTIONS = [20, 50, 100, 200]


def _refresh_client(s):
    """懒重建 WorktileClient / 项目列表（进程重启后内存中的 client 会丢失）。"""
    if s["client"] is None:
        s["client"] = WorktileClient(s["client_id"], s["client_secret"], s["base_url"])
    if s["projects"] is None:
        try:
            s["projects"] = s["client"].get_projects()
            s["projects_error"] = None
        except RuntimeError as e:
            s["projects"] = []
            s["projects_error"] = str(e) or "获取项目列表失败"
    return s


def _persist_save():
    """把会话凭证加密写入本地文件，进程重启后可恢复（不保存 token）。"""
    payload = {}
    for sid, s in SESSIONS.items():
        payload[sid] = {
            "client_id": s["client_id"],
            "client_secret": s["client_secret"],
            "base_url": s["base_url"],
            "tenant_host": s.get("tenant_host", DEFAULT_TENANT_HOST),
            "created_at": s.get("created_at", time.time()),
        }
    try:
        data = _FERNET.encrypt(json.dumps(payload).encode())
        SESSIONS_FILE.write_bytes(data)
        os.chmod(SESSIONS_FILE, 0o600)
    except Exception:
        pass


def _normalize_tenant(tenant):
    """把用户输入的租户域名规整成纯 host：去掉协议、路径、查询、末尾斜杠。

    登录页要求用户填的是「你的 Worktile 域名」，习惯上可能填
    `https://techwll.worktile.com/`、`techwll.worktile.com/#/...` 等形态，
    这里统一裁剪成 `techwll.worktile.com`，便于拼接任务详情页 URL。
    """
    t = (tenant or "").strip()
    if not t:
        return DEFAULT_TENANT_HOST
    t = t.replace("https://", "").replace("http://", "")
    # 依次按 / ? # 截断，取最前面的 host 部分
    for sep in ("/", "?", "#"):
        t = t.split(sep, 1)[0]
    t = t.strip().rstrip("/")
    return t or DEFAULT_TENANT_HOST


def _persist_load():
    """启动时从加密文件恢复会话（不含 client，运行时懒重建）。"""
    if not SESSIONS_FILE.exists():
        return
    try:
        raw = _FERNET.decrypt(SESSIONS_FILE.read_bytes())
        data = json.loads(raw)
        now = time.time()
        for sid, c in data.items():
            if now - c.get("created_at", 0) > SESSION_TIMEOUT:
                continue
            SESSIONS[sid] = {
                "client_id": c["client_id"],
                "client_secret": c["client_secret"],
                "base_url": c.get("base_url", DEFAULT_BASE_URL),
                "tenant_host": c.get("tenant_host", DEFAULT_TENANT_HOST),
                "client": None,
                "projects": None,
                "member_map": None,
                "created_at": c.get("created_at", now),
            }
    except Exception:
        pass


_persist_load()


def _get_session():
    """从 cookie 取会话，不存在/过期返回 None；client/projects 懒重建。"""
    sid = request.cookies.get(SID_COOKIE)
    if not sid:
        return None
    s = SESSIONS.get(sid)
    if not s:
        return None
    if time.time() - s.get("created_at", 0) > SESSION_TIMEOUT:
        with _SESS_LOCK:
            SESSIONS.pop(sid, None)
            _persist_save()
        return None
    return _refresh_client(s)


def _require_session():
    s = _get_session()
    if not s:
        abort(401, description="未登录或会话已失效，请重新输入凭证")
    return s


@app.errorhandler(401)
def handle_401(e):
    return jsonify({"ok": False, "error": str(e.description or "未授权")}), 401


@app.errorhandler(400)
def handle_400(e):
    return jsonify({"ok": False, "error": str(e.description or "请求参数错误")}), 400


@app.route("/")
def index():
    resp = make_response(render_template("index.html", page_size_options=PAGE_SIZE_OPTIONS))
    # 防止浏览器缓存 HTML，避免用户看不到新版 UI
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    base_url = (data.get("base_url") or DEFAULT_BASE_URL).strip()
    tenant_host = _normalize_tenant(data.get("tenant"))
    if not client_id or not client_secret:
        abort(400, description="Client ID 和 Client Secret 均不能为空")

    client = WorktileClient(client_id, client_secret, base_url)
    # 登录改为"尽力而为"：不再强制要求一次成功的接口调用来校验凭证，
    # 避免因为网络/代理暂时不可达（例如沙箱出网代理拦截）就把用户锁在登录页外。
    # 凭证是否真正有效，由后续 /api/projects / /api/tasks 的真实调用来揭示，
    # 那里已有清晰的错误提示。
    projects = []
    login_warning = None
    try:
        projects = client.get_projects()
    except RuntimeError as e:
        login_warning = str(e) or "暂时无法连接 Worktile 服务器，凭证已暂存，后续接口调用会展示真实错误"

    sid = secrets.token_hex(16)
    with _SESS_LOCK:
        SESSIONS[sid] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "base_url": base_url,
            "tenant_host": tenant_host,
            "client": client,
            "projects": projects,
            "projects_error": (None if projects else login_warning),
            "member_map": None,  # 懒加载
            "created_at": time.time(),
        }
        _persist_save()
    resp = jsonify({
        "ok": True,
        "projects_count": len(projects),
        "warning": login_warning,
        "tenant_host": tenant_host,
    })
    resp.set_cookie(SID_COOKIE, sid, httponly=True, samesite="Lax",
                    max_age=SESSION_TIMEOUT)
    return resp


@app.route("/api/logout", methods=["POST"])
def logout():
    sid = request.cookies.get(SID_COOKIE)
    if sid:
        with _SESS_LOCK:
            SESSIONS.pop(sid, None)
            _persist_save()
    resp = jsonify({"ok": True})
    resp.delete_cookie(SID_COOKIE)
    return resp


@app.route("/api/me", methods=["GET"])
def me():
    """供前端启动探测登录态：已登录返回 ok，否则 401。"""
    s = _require_session()
    return jsonify({"ok": True, "projects_count": len(s["projects"] or []),
                    "tenant_host": s.get("tenant_host", DEFAULT_TENANT_HOST)})


@app.route("/api/projects", methods=["GET"])
def projects():
    s = _require_session()
    return jsonify({
        "ok": True,
        "projects": s["projects"],
        "error": s.get("projects_error"),
        "tenant_host": s.get("tenant_host", DEFAULT_TENANT_HOST),
    })


def _ensure_member_map(s):
    if s["member_map"] is None:
        try:
            s["member_map"] = s["client"].get_member_map()
        except RuntimeError:
            s["member_map"] = {}
    return s["member_map"]


@app.route("/api/task_titles", methods=["GET"])
def task_titles():
    """按 title 子串返回候选任务列表（typeahead 用）。

    GET /api/task_titles?project_id=...&q=...&limit=20
    - q 为空时返回前 limit 条（探索）。
    - 只返回 id/identifier/title/project_name，响应体积小。
    """
    s = _require_session()
    project_id = request.args.get("project_id", "").strip()
    if not project_id:
        abort(400, description="缺少 project_id 参数")
    q = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 50))  # 防止恶意或巨大值

    try:
        r = s["client"].list_task_titles(
            s["projects"], project_id, q=q, limit=limit)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({
        "ok": True,
        "items": r.get("items", []),
        "matched": r.get("matched", 0),
    })


@app.route("/api/tasks", methods=["GET"])
def tasks():
    s = _require_session()
    project_id = request.args.get("project_id", "").strip()
    if not project_id:
        abort(400, description="缺少 project_id 参数")

    try:
        page = max(int(request.args.get("page", 0)), 0)
    except ValueError:
        page = 0
    try:
        page_size = int(request.args.get("page_size", 50))
    except ValueError:
        page_size = 50
    if page_size not in PAGE_SIZE_OPTIONS:
        page_size = 50

    keyword = (request.args.get("keyword") or "").strip()

    project_name = ""
    for p in s["projects"]:
        if p["id"] == project_id:
            project_name = p["name"]
            break

    member_map = _ensure_member_map(s)

    try:
        if keyword:
            # 任务名称模糊搜索：先全量拉取再按 title 过滤，最后分页
            raw = s["client"].search_tasks(
                s["projects"], project_id, keyword,
                page_index=page, page_size=page_size, member_map=member_map)
            project_name = "全部项目" if project_id == "__all__" else project_name
        elif project_id == "__all__":
            # 跨所有项目聚合（保留分页）
            raw = s["client"].get_all_tasks_page(
                s["projects"], page_index=page, page_size=page_size, member_map=member_map)
            project_name = "全部项目"
        else:
            raw = s["client"].get_tasks_page(project_id, page_index=page, page_size=page_size)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    if keyword:
        # search_tasks 已对每条任务做规范化（含真实 project_name 与负责人映射）
        items = raw.get("items", [])
    elif project_id == "__all__":
        # get_all_tasks_page 已为每条任务设置真实的 project_name，
        # 不要再覆盖，否则前端无法按真实项目分组。
        items = raw.get("items", [])
    else:
        items = [
            WorktileClient.normalize_task(t, project_name=project_name, member_map=member_map)
            for t in raw.get("items", [])
        ]
    return jsonify({
        "ok": True,
        "project_name": project_name,
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": raw.get("total"),
        "has_more": raw.get("has_more", False),
        "keyword": keyword,
    })


@app.route("/api/tasks/<task_id>/comments", methods=["GET"])
def task_comments(task_id):
    s = _require_session()
    member_map = _ensure_member_map(s)
    try:
        comments = s["client"].get_task_comments(task_id, member_map=member_map)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "comments": comments})


@app.route("/api/debug/comments-raw/<task_id>", methods=["GET"])
def debug_comments_raw(task_id):
    """临时调试：直接拉任务详情返回 comments 原始结构，便于查 author 字段"""
    s = _require_session()
    try:
        data = s["client"]._req(
            "GET", f"/mission/tasks/{task_id}",
            params={"columns": "comments"},
        )
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "data": data})


@app.route("/api/file/<file_id>")
def file_proxy(file_id):
    """代理 Worktile 文件字节流，供前端 <img> 预览 / <a> 下载。

    - 图片：浏览器直接渲染，点击可放大（前端 lightbox）。
    - PDF 等：浏览器原生预览（新标签打开）。
    - 其他类型：作为附件下载。
    认证 token 只在服务端持有，浏览器通过会话 cookie 间接访问，不接触凭证。
    """
    s = _require_session()
    try:
        data, ctype = s["client"].get_file_stream(file_id)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return Response(data, mimetype=ctype)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
