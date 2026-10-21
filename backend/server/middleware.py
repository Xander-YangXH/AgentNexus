"""请求上下文中间件：request-id + 访问日志（decisions/0011）。

写成纯 ASGI 中间件，而不是 Starlette 的 BaseHTTPMiddleware：后者会把响应体整个包一层再转发，
对流式响应（SSE）和 contextvars 的传递都有已知问题；纯 ASGI 中间件只是透传消息，SSE 照常逐段到达。
"""

import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentnexus.observability.context import request_id_var

access_log = logging.getLogger("agentnexus.access")

# 只沿用格式安全的外部 request id：防止有人通过请求头往日志里注入换行、伪造日志行或超长内容
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get("x-request-id")
        request_id = incoming if incoming and _SAFE_ID.match(incoming) else uuid.uuid4().hex
        # 同时放进 scope：500 错误处理器在本中间件之外执行，那时上下文变量已经复原，只能从 request.state 取
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)
        status = 500  # 如果还没发出响应头就抛了异常，按 500 记
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                if "x-request-id" not in headers:  # 错误处理器可能已经设置过（500 发生在本中间件之外，必须自己带）
                    headers.append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            path = scope["path"]  # 只记路径不记查询串：查询串里可能有不该进日志的参数
            # 容器健康检查每 10 秒一次，记成 DEBUG，避免淹没真正的请求
            level = logging.DEBUG if path.startswith("/health") else logging.INFO
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            access_log.log(
                level,
                "%s %s %s %.1fms",
                scope["method"],
                path,
                status,
                duration_ms,
                extra={"method": scope["method"], "path": path, "status": status, "duration_ms": duration_ms},
            )
            request_id_var.reset(token)
