# 构建上下文是仓库根目录（见 docker-compose.yml 的 build.context）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend/package

WORKDIR /app

# 先只拷依赖清单再装依赖：改业务代码时这一层缓存不失效。按锁文件安装：每次构建的版本完全一致
COPY backend/requirements.lock backend/requirements.lock
RUN pip install --no-cache-dir -r backend/requirements.lock

# 构建镜像时就把模型下载进镜像（embedding 约 100MB、重排 int8 约 570MB）：
# fastembed 默认下到系统临时目录，容器重建就丢、每次启动都要联网重下；放在业务代码之前，改代码不会重下。
# 两个模型分两层：只换其中一个时，另一个的缓存层不失效。
# 运行时设 HF_HUB_OFFLINE=1，保证启动不联网；换模型要改对应的 ARG 并重新构建（否则启动报找不到模型）。
ENV FASTEMBED_CACHE_PATH=/opt/models
ARG EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding(os.environ['EMBEDDING_MODEL'])"
ARG RERANK_MODEL=BAAI/bge-reranker-v2-m3-int8
RUN python -c "import os; from fastembed.rerank.cross_encoder import TextCrossEncoder; TextCrossEncoder(os.environ['RERANK_MODEL'])"
ENV HF_HUB_OFFLINE=1

COPY backend backend

EXPOSE 8000
# 容器内必须监听 0.0.0.0，否则宿主机映射的端口访问不到
# --no-access-log：访问日志由 RequestContextMiddleware 记录（带 request_id、耗时），避免同一请求记两行
CMD ["uvicorn", "server.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
