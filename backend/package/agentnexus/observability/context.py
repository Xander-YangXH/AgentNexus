"""贯穿一次请求 / 一次执行的上下文，自动附加到每条日志上（decisions/0011）。

用 contextvars 而不是线程局部变量：asyncio 下多个请求在同一线程里交替执行，
contextvars 会随协程与 asyncio 任务传递（create_task 时复制当前上下文）。
"""

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
