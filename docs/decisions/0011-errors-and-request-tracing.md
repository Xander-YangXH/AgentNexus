# 0011 - 统一错误格式、request-id 与结构化日志

**状态**：已决定（阶段 1 收尾，2026-10-21）

**背景**：
- 错误响应格式不统一：领域错误是 `{"detail": "..."}`，校验错误是 FastAPI 默认的 `detail` 列表，未处理的异常是纯文本 500。前端只能靠 HTTP 状态码猜测原因。
- 应用从没配置过日志：Python 默认只输出 WARNING 及以上，所有 INFO 被丢弃（在容器里查到 `agentnexus` 日志的生效级别是 WARNING）。
- 用户报“出错了”时，没有任何线索能把这次请求和服务端日志对上；一次请求触发的后台执行（run）也无法与请求关联。

## 决定

1. **统一错误格式**（`server/errors.py`）：所有错误都返回
   `{"error": {"code": "...", "message": "...", "request_id": "...", "details": 可选}}`
   - `code` 稳定、机器可读（`run_not_found`、`run_conflict`、`unauthorized`、`validation_error`、`internal_error` 等），前端据此判断；
   - 领域异常 → 错误码的映射表集中在一处，服务层仍然只抛领域异常；
   - 校验错误只返回字段位置、说明和类型，**不回显用户的输入**；
   - 500 只返回 `internal_error` 和 request_id，**不暴露任何内部细节**，完整堆栈写进日志。
2. **request-id**（`server/middleware.py`）：每个请求一个 id，写进响应头 `X-Request-ID`、错误响应体和该请求期间的所有日志。客户端传入的 `X-Request-ID` 只在符合 `^[A-Za-z0-9._-]{1,64}$` 时沿用，否则重新生成——防止有人通过请求头向日志注入换行、伪造日志行或超长内容。
3. **纯 ASGI 中间件**，不用 Starlette 的 `BaseHTTPMiddleware`：后者会包裹整个响应体并在独立任务里执行下游，对流式响应（SSE）和 contextvars 的传递有已知问题。纯 ASGI 中间件只透传消息，SSE 照常逐段到达（真实 uvicorn 的 SSE 测试与经 Nginx 的实测都确认）。
4. **上下文用 contextvars**（`observability/context.py`）：`request_id` 在中间件里设置；`run_id` 在派发器处理每个 run 时设置，并通过 `create_task` 自动复制进执行 Agent 的任务。一个过滤器把两者附加到每条日志上。
5. **从请求追到执行**：提交消息时记一条同时带 request_id 与 run_id 的日志；执行开始、结束各记一条，结束日志带结局（succeeded / cancelled / timeout / interrupted / 各错误码）和耗时。
6. **日志格式**（`observability/logging.py`，只用标准库）：`LOG_FORMAT=text`（默认，给人看）或 `json`（一行一个 JSON，给日志平台检索、做 Dashboard 统计）；`LOG_LEVEL` 默认 INFO。
7. **访问日志**：每个请求一行（方法、路径、状态码、耗时）。只记路径不记查询串（查询串里可能有不该进日志的参数）；`/health` 降为 DEBUG（容器健康检查每 10 秒一次，会淹没真正的请求）；uvicorn 自带的访问日志关闭（`--no-access-log`），避免同一请求记两行。

## 后果

- 好处：前端能按错误码处理；用户报错时提供请求编号即可定位日志；一次请求触发的整个执行过程可以按 id 串起来；以后接日志平台或做 Dashboard 时数据已经是结构化的。
- 代价：错误格式变更是不兼容的（前端同步修改）；500 时 uvicorn 仍会额外输出一份堆栈（Starlette 的最外层错误处理在发送响应后会重新抛出异常）。
- 已知局限：后台任务（清理器、补投扫描）的日志没有 request_id，这是对的——它们不属于任何请求；request_id 目前不传给 LLM 供应商。

## 验证

- 后端测试 14 个（`test_observability.py`）：request-id 生成、安全沿用与替换；6 类错误的统一格式且 `request_id` 与响应头一致；校验错误不回显输入；500 不泄露内部信息但日志里有同一 request_id 和堆栈；访问日志字段、`/health` 降级、不记查询串；JSON 格式与上下文、显式传入优先；`configure_logging` 可重复调用；run_id 传进 Agent 任务；run 生命周期日志的结局与耗时。另在 SSE 的 HTTP 测试里断言流式响应带 request id。全量 101 个通过。
- 变异检查 5 项均被抓住：接受任意外部 id、500 回显异常、校验错误回显输入、健康检查不降级、记录查询串。
- 前端：`HttpError` 解析统一格式，服务端错误时向用户显示请求编号；新增 3 个测试（共 26 个）。
- 容器实测（JSON 日志）：客户端传入 `X-Request-ID: demo-req-002` → 按它查到“run 已提交”（含 run_id）与访问日志 → 按 run_id 查到“开始执行”“执行结束：succeeded，395.5ms”；INFO 日志里没有 `/health`；经 Nginx 的 SSE 仍为流式。
