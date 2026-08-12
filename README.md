# Worktile 任务评论区看板

一个通用的、本地运行的 Web 看板：用户只需输入租户的 **Client ID** 和 **Client Secret**（公有云），即可查看该租户下各项目的任务与评论。

## 功能

- 输入凭证即可切换租户数据；**登录态会被记住**：关闭浏览器再打开、或重启服务进程，都无需重新输入凭证（凭证加密保存在本地 `sessions.json`，密钥在 `.secret`，不下发浏览器）
- 按 **项目** 筛选任务，或选择 **「全部项目」** 跨项目总览（保留分页）
- 任务表格字段：项目名称、任务编号、任务名称、任务负责人、最近更新时间、评论数
- 点击任务行展开 **评论区**，每条评论独立展示：评论人、评论时间、评论内容
- 评论按时间 **倒序** 排列（最新在前）
- 评论中若为图片 / 文件，仅展示其 **名称**（图片 / 文件 标签区分）
- **分页组件**：可自定义每页条数（20 / 50 / 100 / 200），支持上一页 / 下一页
- 评论采用懒加载：展开任务时才拉取该任务详情评论，避免一次性大量请求

## 运行

```bash
# 使用项目自带的虚拟环境（已装好 flask / requests）
PY=/Users/john/.workbuddy/binaries/python/envs/wt-board/bin/python
cd <本目录>
$PY app.py
```

启动后浏览器打开 http://localhost:5000 ，输入凭证即可。

环境变量 `PORT` 可修改端口（默认 5000）。

## 目录结构

```
app.py              Flask 后端（会话管理、接口代理）
wt_client.py        Worktile OpenAPI 客户端（运行时凭证、评论解析）
templates/index.html 前端看板页面
requirements.txt    依赖
```

## 说明与已知限制

- **仅支持公有云**。如需私有部署，可在 `app.py` 的登录接口传入 `base_url`。
- **会话持久化**：为满足「不重复输入凭证」，登录态（含加密后的 Client ID / Secret）
  会保存在项目目录下的 `sessions.json`（以 `.secret` 中的密钥加密，文件权限 600）。
  服务重启或浏览器关闭后打开都会自动恢复登录态，会话有效期 30 天。
  若需彻底清除，点击看板右上角「退出登录」即可；也会删除这两个本地文件。
- 评论数据来自「任务详情」接口，需逐任务查询，因此采用展开时按需加载。
- 评论 JSON 的具体字段名（评论人 / 附件结构）在不同版本可能略有差异，
  `wt_client.py` 已做多字段名兼容；若首次运行发现评论人 / 附件未正确显示，
  请反馈实际返回结构，便于进一步适配。
- 任务列表接口若不返回 `total`，分页栏将以「还有更多 / 上一页下一页」方式呈现。

## 部署到云端（公网可访问，可发给外部用户测试）

项目已改造为生产部署形态，可直接推到 Railway / Render / Fly.io / 腾讯云 CloudBase 等支持 `Procfile` 的平台。这些平台有正常公网出口，能连通 `dev.worktile.com`，因此外部用户能看到真实数据。

新增的生产文件：

- `requirements.txt` —— 增加了 `cryptography`、`gunicorn`
- `Procfile` —— `web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 60`
- `runtime.txt` —— 指定 Python `3.13.12`

> ⚠️ **必须用单 worker（`--workers 1`）**：登录态存在进程内存的 `SESSIONS` 字典里，多 worker 时请求可能被分到没有该会话的进程，导致用户被随机踢回登录页。低流量测试单 worker + 4 线程完全够用。

### 一键部署步骤（以 GitHub 仓库为例）

1. 把本目录初始化为 git 仓库并推到 GitHub：

   ```bash
   cd <本目录>
   git init && git add -A && git commit -m "worktile comment board"
   gh repo create worktile-comment-board --private --source=. --push
   ```

2. 在平台新建项目并关联该仓库：
   - **Railway**：New Project → Deploy from GitHub Repo，自动识别 `Procfile`。
   - **Render**：New Web Service → 关联仓库，`Build Command` 留空（平台会 `pip install -r requirements.txt`），`Start Command` 用 `Procfile` 里的命令。
   - **Fly.io**：`fly launch`（会自动用 Procfile），再 `fly deploy`。
   - **腾讯云 CloudBase**：静态网站/云托管 → 关联仓库，运行命令填 `gunicorn app:app --bind 0.0.0.0:$PORT`。

3. 平台会分配一个公网 `https://xxx.platform.app` 域名，把它发给用户即可。

### 本地用 gunicorn 自测

```bash
$PY -m pip install -r requirements.txt
PORT=5080 gunicorn app:app --bind 0.0.0.0:5080 --workers 1 --threads 4
```

### 部署到云主机（腾讯云 / 阿里云轻量应用服务器，推荐用于长期稳定访问）

不依赖你本机开机，用户随时可访问。用 Docker 打包，常驻运行。

新增的部署文件：

- `Dockerfile` —— 基于 `python:3.13-slim`，装依赖后用 `gunicorn` 单 worker 启动
- `docker-compose.yml` —— `restart: unless-stopped`（崩溃/重启自动拉起），映射 `5080:5080`
- `.dockerignore` / `.env.example` —— 构建上下文与密钥模板
- `deploy.sh` —— 云主机上一键：装 Docker → 拉代码 → 生成密钥 → 构建 → 常驻运行

**方式 A：一键脚本（在云主机上执行）**

```bash
# 已 clone 仓库：
cd /opt/worktile-comment-board && bash deploy.sh

# 或裸机直接跑（自动 clone）：
bash <(curl -fsSL https://raw.githubusercontent.com/JohnVIP/worktile-comment-board/main/deploy.sh)
```

**方式 B：手动**

```bash
git clone https://github.com/JohnVIP/worktile-comment-board.git /opt/worktile-comment-board
cd /opt/worktile-comment-board
cp .env.example .env
# 编辑 .env，把 FERNET_KEY 换成自己生成的随机串：
#   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
docker compose build
docker compose up -d
```

**关键环境变量**

| 变量 | 说明 |
| --- | --- |
| `PORT` | 容器内监听端口，默认 `5080`，一般不用改 |
| `FERNET_KEY` | 会话文件加密密钥。**必须固定**：变了会让已保存会话解密失败、用户需重登。用 `.env` 或云主机环境变量注入 |

> 密钥加载优先级：环境变量 `FERNET_KEY` → 本地 `.secret` 文件 → 临时随机（仅本次进程）。部署时务必通过 `.env` / 平台 Secrets 固定 `FERNET_KEY`。

**访问与放行**

- 应用跑在 `http://<云主机公网IP>:5080`，把它发给用户即可。
- ⚠️ 必须在云厂商控制台「安全组 / 防火墙」放行 **TCP 5080** 入站，否则外网连不上。
- 想要 HTTPS（可选）：在云主机上再跑一个 Caddy / Nginx 反代到 `127.0.0.1:5080`，并把 80/443 也放行。

**日常运维**

```bash
docker compose ps          # 状态
docker compose logs -f     # 日志
docker compose up -d --build   # 代码更新后重建
docker compose down        # 停止
```


