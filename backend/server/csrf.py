"""CSRF 防护（decisions/0023）：会话放在 Cookie 里，浏览器会自动带上，所以要防“别的网站让用户的浏览器发请求”。

两层：
1. Cookie 设了 SameSite=Lax：别的网站发起的跨站 POST / PUT / DELETE 不会带上它；
2. 本中间件：写请求如果带了 Origin（浏览器发起的都会带），必须与本站一致；浏览器标了 Sec-Fetch-Site: cross-site 的写请求一律拒绝。
不带 Origin 的请求（curl、脚本）放行：CSRF 的前提是受害者的浏览器，非浏览器客户端没有“被冒用的 Cookie”。
"""

import json
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class CsrfMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS and not self._allowed(scope):
            body = json.dumps({"error": {"code": "csrf_rejected", "message": "cross-site request rejected"}}).encode()
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _allowed(scope: Scope) -> bool:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if headers.get("sec-fetch-site") == "cross-site":
            return False
        origin = headers.get("origin")
        if origin is None:
            return True
        # 经 Nginx 转发时 Host 是原始的（proxy_set_header Host $host），与浏览器看到的源一致
        return urlsplit(origin).netloc == headers.get("host", "")
