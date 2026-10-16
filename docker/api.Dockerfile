# 构建上下文是仓库根目录（见 docker-compose.yml 的 build.context）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend/package

WORKDIR /app

# 先只拷依赖清单再装依赖：改业务代码时这一层缓存不失效
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend

EXPOSE 8000
# 容器内必须监听 0.0.0.0，否则宿主机映射的端口访问不到
CMD ["uvicorn", "server.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
