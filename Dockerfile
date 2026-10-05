# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 零第三方依赖：无需 pip install
COPY st_rotator ./st_rotator
COPY config.example.json ./config.example.json

# 非 root 运行；/data 用于挂载配置与日志
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown -R 10001:10001 /app /data

USER 10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"]

# 默认启动网关 + 控制台（--no-open：容器内不打开浏览器窗口）
# console_token 从 config.json 的 ${CONSOLE_TOKEN} 展开，实现固定密钥鉴权
CMD ["python", "-m", "st_rotator", "ui", "-c", "/data/config.json", "--host", "0.0.0.0", "--port", "8080", "--no-open", "--log-file", "/data/rotator.log"]
