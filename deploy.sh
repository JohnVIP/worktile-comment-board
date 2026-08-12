#!/usr/bin/env bash
# ============================================================
#  一键部署到云主机（腾讯云 / 阿里云 轻量应用服务器等）
#  作用：装 Docker -> 拉代码 -> 生成密钥 -> 构建 -> 常驻运行
#  用法（任选其一）：
#    1) 已 git clone 本目录：  bash deploy.sh
#    2) 直接在云主机裸跑：
#       bash <(curl -fsSL https://raw.githubusercontent.com/JohnVIP/worktile-comment-board/main/deploy.sh)
#  前置：用 root 或 sudo 运行；云主机安全组需放行 TCP 5080（见 README）。
# ============================================================
set -e

APP_DIR=/opt/worktile-comment-board
REPO="https://github.com/JohnVIP/worktile-comment-board.git"

echo "==[1/6] 安装 Docker（如未安装）=="
if command -v docker >/dev/null 2>&1; then
  echo "    Docker 已存在，跳过"
else
  # 优先用国内镜像源，失败回退官方
  if curl -fsSL https://get.daocloud.io/docker | bash; then
    echo "    已用 DaoCloud 镜像安装"
  else
    curl -fsSL https://get.docker.com | bash
    echo "    已用官方脚本安装"
  fi
  systemctl enable --now docker
fi

echo "==[2/6] 克隆 / 更新仓库=="
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

echo "==[3/6] 生成 FERNET_KEY（仅当 .env 不存在）=="
if [ ! -f .env ]; then
  # Fernet 规范：32 字节的 urlsafe base64 密钥（44 字符，尾随一个 '='）。
  # 直接用 /dev/urandom 生成，不依赖 python cryptography 是否安装，最稳妥。
  KEY=$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')
  if [ -z "$KEY" ] && command -v python3 >/dev/null 2>&1; then
    KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null)
  fi
  printf 'FERNET_KEY=%s\n' "$KEY" > .env
  echo "    已写入 .env（密钥已固定，会话可跨重启保留；如需重置会话改这个值即可）"
else
  echo "    .env 已存在，沿用现有 FERNET_KEY"
fi

echo "==[4/6] 构建镜像=="
docker compose build --no-cache

echo "==[5/6] 启动（restart=unless-stopped，开机/崩溃自启）=="
docker compose up -d

echo "==[6/6] 完成=="
echo "    应用地址： http://<云主机公网IP>:5080"
echo "    查看日志： cd $APP_DIR && docker compose logs -f"
echo "    停止/更新： docker compose down  /  docker compose up -d --build"
echo "    ⚠️ 别忘了在云厂商控制台「安全组/防火墙」放行 TCP 5080 端口"
