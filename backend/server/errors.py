"""统一的错误响应（decisions/0011）。

所有错误都是：{"error": {"code": "...", "message": "...", "request_id": "...", "details": ...可选}}
- code：稳定的机器可读标识，前端据此判断；message：给开发者看的简短说明；
- request_id：与响应头 X-Request-ID 相同，用户报错时提供它，就能在日志里找到这次请求；
- 500 不暴露任何内部细节，只记日志。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentnexus.services.errors import (
    DocumentExists,
    DocumentNotFound,
    DocumentNotRetryable,
    EmptyFile,
    FileTooLarge,
    KnowledgeBaseExists,
    KnowledgeBaseNotFound,
    UnsupportedFileType,
    ConversationArchived,
    ConversationNotFound,
    RunAlreadyFinished,
    RunConflict,
    RunNotFound,
    RunNotRetryable,
    UserExists,
)

log = logging.getLogger(__name__)

# 领域异常 -> (HTTP 状态码, 错误码, 说明)。服务层只抛领域异常，HTTP 语义只在这里出现
DOMAIN_ERRORS: dict[type[Exception], tuple[int, str, str]] = {
    ConversationNotFound: (404, "conversation_not_found", "conversation not found"),
    ConversationArchived: (409, "conversation_archived", "conversation is archived"),
    RunNotFound: (404, "run_not_found", "run not found"),
    RunConflict: (409, "run_conflict", "conversation already has an active run"),
    UserExists: (409, "user_exists", "user name already exists"),
    RunAlreadyFinished: (409, "run_already_finished", "run already finished"),
    RunNotRetryable: (409, "run_not_retryable", "run cannot be retried"),
    KnowledgeBaseNotFound: (404, "knowledge_base_not_found", "knowledge base not found"),
    KnowledgeBaseExists: (409, "knowledge_base_exists", "knowledge base name already exists"),
    DocumentNotFound: (404, "document_not_found", "document not found"),
    DocumentNotRetryable: (409, "document_not_retryable", "only failed documents can be reprocessed"),
    UnsupportedFileType: (415, "unsupported_file_type", "supported: .txt .md .markdown .pdf .docx"),
    FileTooLarge: (413, "file_too_large", "file is too large"),
    EmptyFile: (400, "empty_file", "file is empty"),
}

_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    503: "unavailable",
}


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def error_response(request: Request, status: int, code: str, message: str, details=None) -> JSONResponse:
    body = {"code": code, "message": message, "request_id": _request_id(request)}
    if details is not None:
        body["details"] = details
    headers = {"X-Request-ID": body["request_id"]} if body["request_id"] else None
    return JSONResponse(status_code=status, content={"error": body}, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    for exc_type, (status, code, message) in DOMAIN_ERRORS.items():

        async def handler(request: Request, exc: Exception, status=status, code=code, message=message):
            return error_response(request, status, code, message)

        app.add_exception_handler(exc_type, handler)

    async def http_error(request: Request, exc: StarletteHTTPException):
        details = None if isinstance(exc.detail, str) else exc.detail  # 例如就绪探针返回各依赖的状态
        message = exc.detail if isinstance(exc.detail, str) else "request failed"
        return error_response(request, exc.status_code, _HTTP_CODES.get(exc.status_code, "http_error"), message, details)

    async def validation_error(request: Request, exc: RequestValidationError):
        # 只保留位置、说明、类型；不回显 input，避免把用户提交的大段内容原样塞进错误响应
        details = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return error_response(request, 422, "validation_error", "request validation failed", details)

    async def unhandled(request: Request, exc: Exception):
        # 由最外层的 ServerErrorMiddleware 调用，此时请求上下文变量已复原，所以显式传入 request_id
        log.error("unhandled error", exc_info=exc, extra={"request_id": _request_id(request)})
        return error_response(request, 500, "internal_error", "internal server error")

    async def document_exists(request: Request, exc: DocumentExists):
        # 告诉调用方已有的是哪一份，前端可以直接跳过去
        return error_response(request, 409, "document_exists", "same file already exists in this knowledge base",
                              {"existing_document_id": str(exc.existing_id) if exc.existing_id else None})

    app.add_exception_handler(DocumentExists, document_exists)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, unhandled)
