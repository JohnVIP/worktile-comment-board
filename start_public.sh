#!/bin/bash
# ============================================================
#  一键启动：Flask 看板 + cloudflared 公网隧道
#  用途：让外部用户通过公网 https 链接访问本机看板并测真实数据
#  用法：bash start_public.sh
# ============================================================
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Python 解释器：优先项目内 .venv，其次历史路径，最后回退系统 python3
if [ -x "$DIR/.venv/bin/python" ]; then
  VENV_PY="$DIR/.venv/bin/python"
elif [ -x "/Users/john/.workbuddy/binaries/python/envs/wt-board/bin/python" ]; then
  VENV_PY="/Users/john/.workbuddy/binaries/python/envs/wt-board/bin/python"
else
  VENV_PY="$(command -v python3)"
fi
CF="/usr/local/bin/cloudflared"
PORT=5080

# 1) 关掉旧进程
echo "[1/3] 关闭旧进程..."
lsof -ti :$PORT 2>/dev/null | xargs -r kill -9 2>/dev/null || true
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 1

# 2) 启动 Flask（单 worker，登录态在内存）
echo "[2/3] 启动 Flask 看板 (:$PORT)..."
PORT=$PORT nohup "$VENV_PY" app.py > /tmp/wt-board.log 2>&1 &
echo "      Flask pid $!"

# 3) 启动 cloudflared quick tunnel（免账号，自动分配 *.trycloudflare.com）
echo "[3/3] 启动 cloudflared 公网隧道..."
nohup "$CF" tunnel --url http://localhost:$PORT --metrics 127.0.0.1:5088 > /tmp/cf.log 2>&1 &
echo "      cloudflared pid $!"

echo "等待隧道建立..."
sleep 9
echo ""
echo "============================================"
echo " 公网访问地址（发给用户测试用）："
grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" /tmp/cf.log | head -1
echo "============================================"
echo ""
echo "本机自检："
curl -s -o /dev/null -w "  local  HTTP %{http_code}\n" http://127.0.0.1:$PORT/ 2>&1 || true
echo ""
echo "提示："
echo "  - quick tunnel 为临时地址，本会话/进程结束后失效，重新运行本脚本即可换新地址。"
echo "  - 如需稳定固定域名，请改用 cloudflared 命名隧道（需 Cloudflare 账号）或部署到云主机。"
echo "  - 外部用户打开链接后，用你的 Worktile Client ID / Secret 登录即可看到真实数据。"
