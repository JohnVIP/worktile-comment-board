# Worktile 任务评论区看板

一个通用的、本地运行的 Web 看板：用户只需输入租户的 **Client ID** 和 **Client Secret**（公有云），即可查看该租户下各项目的任务与评论。

## 功能

- 输入凭证即可切换租户数据；**登录态会被记住**：关闭浏览器再打开、或重启服务进程，都无需重新输入凭证（凭证加密保存在本地 `sessions.json`，密钥在 `.secret`，不下发浏览器）
- 按 **项目** 筛选任务，或选择 **「全部项目」** 跨项目总览（保留分页）
- 按 **任务负责人** 进一步筛选（搜索式下拉，选项含「全部负责人」「未分配」与租户下所有成员；切换项目时自动重置）
- 任务表格字段：项目名称、任务编号、任务名称、任务负责人、最近更新时间、评论数
- 点击任务行展开 **评论区**，每条评论独立展示：评论人、评论时间、评论内容
- 评论按时间 **倒序** 排列（最新在前）
- 评论中若为图片 / 文件，仅展示其 **名称**（图片 / 文件 标签区分）
- **分页组件**：可自定义每页条数（20 / 50 / 100 / 200），支持上一页 / 下一页
- 评论采用懒加载：展开任务时才拉取该任务详情评论，避免一次性大量请求
- **延期任务（一键查看）**：顶部「延期任务」按钮，一键列出**全部项目**里「截止时间已过期 **且** 状态未完成」的任务，按逾期天数降序，并按严重程度分级高亮（<3 天浅橙 / 3–7 天橙 / ≥7 天深红）。该视图独立于普通看板，不影响列设置；同样受「任务负责人」筛选影响。
- **一键导出 Excel**：顶部「导出 Excel」按钮，按**当前视图（看板 / 延期任务）+ 当前筛选条件（项目 / 负责人 / 标题关键字）** 在服务端拉全量数据并生成 `.xlsx`。提供「仅任务 / 含评论」开关：「含评论」额外生成「评论」sheet（评论人 / 时间 / 内容 / 附件）；「仅任务」更快。导出内容覆盖两条视图的全部命中任务。

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
- **导出性能与超时**：「一键导出 Excel」「含评论」模式下，每条任务都要单独请求评论接口，因此耗时 ≈ 任务数 × 单次评论请求耗时。当 `project_id=全部项目` 且任务/项目很多时，导出可能持续**数分钟**，并在「仅任务」模式下仍需全量遍历任务列表。部署时务必把 gunicorn worker 超时调大（例如 `--timeout 300`，或用 `timeout` 指令），否则长请求会被 worker 杀掉返回 502。评论拉取已做并发 3 + 429 退避重试兜底。
- **导出文件结构**：`.xlsx` 含「任务」sheet（任务ID / 编号 / 标题 / 项目 / 负责人 / 是否完成 / 更新时间 / 截止时间 / 逾期天数 / 评论数）与（含评论时）「评论」sheet（任务ID / 任务标题 / 项目 / 评论人 / 评论时间 / 评论内容 / 附件）。逾期天数仅在「延期任务」视图导出时有意义，普通看板导出显示 `-`。
- **导出进度**：前端采用「启动任务 → 轮询进度 → 下载」三段式，弹出的浮层会显示真实进度——收集任务数、评论拉取进度（已拉取 N/总数 个任务）、生成文件阶段与完成后的任务/评论总数。后端对应 `POST /api/export/start`、`GET /api/export/progress`、`GET /api/export/download`（旧的同步 `GET /api/export` 仍保留兼容）。

## 延期任务（口径与字段适配）

- **延期定义**：任务的截止时间（候选字段 `due` / `deadline` / `end_at` / `finish_at` 等）早于「当前时间」**且** 状态不是「已完成」（`done` / `completed` / `完成` / `结项` 等）。**无截止时间的任务不计入**。
- **范围**：跨全部项目聚合（含全部活跃项目），首屏需全量遍历，故有 120 秒会话内缓存；点「刷新」按钮可绕过缓存重新扫描。
- **字段名适配**：Worktile 不同版本的任务字段名可能不同。接口 `/api/tasks/overdue` 会返回 `diagnostics`（`scanned` / `with_due` / `with_status` / `sample_due_str` / `sample_status`）。若 `with_due` 或 `with_status` 为 0，说明候选字段名未命中，按 `sample_*` 实际值调整 `wt_client.py` 中 `_first(...)` 的候选列表即可，无需改其他逻辑。
- **已实测公有云字段**（2026-08 真机验证通过）：截止时间位于 `properties.due = {"date": <秒级 epoch>, "with_time": 0}`，取其中的 `date`；状态位于 `task_state`（`{"name":..., "type":...}`）或顶层 `state_type`，其中 **`type == 3` 即结束/完成态**（实测涵盖 已完成 / 关 / 报废清理 等所有终态），据此判定「已完成」并排除。验证时全量扫描 89 个项目、2324 条任务，命中 992 条逾期。

## 多租户数据隔离

本应用支持**多个租户同时使用**：每个用户在前端输入自己的
`Client ID` / `Client Secret`（以及所属 Worktile 域名）登录，
即可查看自己租户的任务与评论数据，**彼此之间完全隔离**。

隔离模型（已在单测中验证）：

- **会话级隔离**：浏览器登录后获得一个随机的 `wt_sid` Cookie（HTTPOnly）。
  服务端 `SESSIONS[sid]` 保存该租户自己的凭证，以及独立的 `WorktileClient`
  实例——`access_token`、`projects`、`member_map`、文件缓存全部为实例级，
  **没有任何跨租户共享的可变全局状态**。所有 API 都经 `_require_session()`
  鉴权，一个租户拿不到别人的 `sid`，自然访问不到别人的数据。
- **可见的隔离证明**：登录后顶栏显示「租户：`clie****1111` · `#dec9010ab47a`」——
  打码的 Client ID + 由凭证派生的稳定指纹。同一租户指纹恒定、不同租户指纹不同，
  用户可一眼确认自己处于独立的租户空间。
- **多进程部署安全**：若用 gunicorn 多 worker 部署，请求可能被分发到没有该
  会话内存的 worker；此时服务端会从加密的 `sessions.json` 恢复凭证并懒重建
  client，租户不会因此随机 401。单 worker（`workers=1`）部署同样完全支持并发多租户。
- **凭证安全**：Client ID / Secret 仅存于服务端内存与加密的 `sessions.json`
  （Fernet 加密，密钥来自 `FERNET_KEY` 环境变量或本地 `.secret`），绝不下发浏览器。
  HTTPS 反向代理下 Cookie 自动加 `Secure` 标记。

> 注意：这是面向「让用户自测各自租户」的 DEMO 级隔离——靠独立的会话与凭证
> 实现租户间数据不可见。若未来要支持「同一租户内多成员协作」或「平台方代管
> 多租户」，需要再引入租户账号体系与更细的权限控制。

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

**增量更新（改完代码后重新部署）**

本地改完代码、确认无误后先提交并推到 GitHub：

```bash
# 本地
git add <有改动的文件，例如 templates/index.html wt_client.py app.py>
git commit -m "描述本次改动"
git push origin main
```

再到云主机上拉取最新代码并重建容器：

```bash
cd /opt/worktile-comment-board
git pull --ff-only                                   # 拉取最新提交
docker compose up -d --build                         # 重建镜像并重启容器
```

> ⚠️ **不要动 `.env` 里的 `FERNET_KEY`**：重建容器不会丢失登录态（密钥固定），但一旦重新生成 `.env` / 改动 `FERNET_KEY`，已保存的加密会话解密失败，用户需重新登录。
> ⚠️ 前端模板（`templates/index.html`）由 Flask 运行时直接渲染，**无需 `collectstatic` 之类操作**，重建镜像即生效。

分步查看每步输出（排查时用）：

```bash
cd /opt/worktile-comment-board
git pull --ff-only
docker compose build
docker compose up -d
docker compose ps              # 确认状态 Up
docker compose logs -f         # 查看启动日志，确认无报错
curl -s -o /dev/null -w "%{http_code}" http://localhost:5080/   # 应返回 200
```

**日常运维**

```bash
docker compose ps          # 状态
docker compose logs -f     # 日志
docker compose up -d --build   # 代码更新后重建
docker compose down        # 停止
```


