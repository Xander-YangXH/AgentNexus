# MCP 网关镜像（decisions/0021）：只有网关代码和第三方 MCP 服务，没有后端的其他代码、模型和密钥。
# 不装 Node.js：mcp-server-fetch 的依赖 readabilipy 检测到 Node 会在运行时执行 npm install
# （实验中 npm 的输出污染了 stdio 协议流，而且绕过了锁文件）。没有 Node 时它用纯 Python 解析网页。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
WORKDIR /app

COPY docker/mcp-gateway/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock && rm /tmp/requirements.lock

# 只拷网关这一个包（它只依赖 mcp / starlette / uvicorn）
COPY backend/package/agentnexus/mcp_gateway agentnexus/mcp_gateway
RUN touch agentnexus/__init__.py

USER 65534:65534
EXPOSE 8080
CMD ["uvicorn", "agentnexus.mcp_gateway.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
