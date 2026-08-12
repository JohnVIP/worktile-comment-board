# 基于官方 Python 3.13 slim 镜像
FROM python:3.13-slim

WORKDIR /app

# 编译依赖：cryptography 在 slim 上通常能直接装 wheel，
# 装 build-essential/libssl-dev 以防个别环境需要本地编译。
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# 先装 Python 依赖（利用 Docker 层缓存，改代码不会重装依赖）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 容器内固定监听 5080（也支持 $PORT 环境变量）
ENV PORT=5080
EXPOSE 5080

# 单 worker：登录态存在进程内存的 SESSIONS 字典里，多 worker 会被随机踢回登录。
# threads=4 足够低流量测试；timeout=60 兼容 Worktile API 偶发慢响应。
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5080", "--workers", "1", "--threads", "4", "--timeout", "60"]
