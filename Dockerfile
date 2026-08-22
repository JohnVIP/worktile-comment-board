# ---- 构建阶段：装编译工具链与依赖 ----
# cryptography 在 slim 上通常能直接装 wheel；装 build-essential/libssl-dev
# 以防个别环境需要本地编译。编译工具只留在本阶段，最终镜像不带。
FROM python:3.13-slim AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libssl-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# 装到独立前缀，下一阶段整目录拷进 /usr/local
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- 运行阶段：只带运行时依赖，镜像更小、攻击面更少 ----
FROM python:3.13-slim
WORKDIR /app
COPY --from=builder /install /usr/local
COPY . .

# 容器内固定监听 5080（也支持 $PORT 环境变量）
ENV PORT=5080
EXPOSE 5080

# 健康检查：首页无需登录即可探测（slim 无 curl，用 python 标准库）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5080/', timeout=5).status == 200 else 1)"

# 单 worker：登录态存在进程内存的 SESSIONS 字典里，多 worker 会被随机踢回登录。
# threads=4 足够低流量测试；timeout=300：兼容同步导出接口 GET /api/export 的
# 长耗时（异步导出在后台线程不受影响，但保留的兼容接口会被 60s 超时杀掉返回 502）。
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5080", "--workers", "1", "--threads", "4", "--timeout", "300"]
