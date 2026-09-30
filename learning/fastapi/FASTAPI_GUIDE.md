# FastAPI 详细学习指南

配合 `fastapi_basics.py`（基础）和 `fastapi_learning.py`（Depends / BackgroundTasks / SSE）食用。文中所有代码行为都在 fastapi 0.141 + uvicorn 0.54 下实测过。

## 1. FastAPI 是什么

FastAPI 是一个 Python Web 框架，用来写 HTTP API。它自己不重复造轮子，而是把两个成熟组件粘合起来：

- **Starlette**：负责 Web 部分——路由、中间件、请求/响应、WebSocket、StreamingResponse、BackgroundTasks 都来自它
- **Pydantic**：负责数据部分——根据 Python 类型注解自动校验请求数据、序列化响应数据

FastAPI 自己贡献的核心价值是：**用类型注解声明接口，其余全部自动完成**——参数解析、数据校验、错误响应、OpenAPI 文档。你写 `ticket_id: int`，它就自动保证进来的是整数，不是就返回 422 并告诉调用方哪里错了。

## 2. 为什么 AgentNexus 选它

- 原生 `async/await`：Agent 平台大量时间在等 LLM API、数据库、文件解析，都是 I/O 等待，异步能让一个进程同时挂着很多请求
- `StreamingResponse`：流式输出 Agent 回复（SSE）是核心体验
- `Depends`：把"拿数据库连接、解析当前用户"这类横切逻辑抽出来，支撑"路由薄、service 厚"的分层
- 自动生成 OpenAPI 文档（`/docs`）：前后端联调、自己调试都很方便
- Pydantic 校验：工具调用参数、请求体在边界处被严格校验

## 3. 运行机制：ASGI 与 uvicorn

FastAPI 应用本身只是一个遵循 **ASGI** 协议的 Python 对象（`app`），它自己不监听端口。真正接收 TCP 连接、解析 HTTP 的是 **ASGI 服务器**，最常用的是 **uvicorn**：

```
浏览器/curl  ──HTTP──>  uvicorn（监听端口、解析HTTP）──调用──>  FastAPI app（路由、校验、你的函数）
```

所以启动命令是 `uvicorn 模块名:变量名`，例如 `uvicorn fastapi_basics:app` 意思是"从 `fastapi_basics.py` 里找名叫 `app` 的对象来跑"。

**事件循环**：uvicorn 用一个事件循环（单线程）调度所有 `async def` 路由。遇到 `await` 就把控制权让出去处理别的请求。这就引出最重要的一条规则，见第 6 节。

## 4. 核心概念（对应 fastapi_basics.py）

**路由与请求方法**：`@app.get("/x")`、`@app.post`、`@app.patch`、`@app.delete`。装饰器把"URL + 方法"绑定到函数。

**路径参数**：写在 URL 路径里，`/tickets/{ticket_id}`，函数参数同名接收，类型注解决定转换和校验。`Path(ge=1)` 加约束，`/tickets/0` 会得到 422。

**查询参数**：URL 问号后面的，`/tickets?status=open&limit=5`。函数里"不在路径里、又是简单类型"的参数就是查询参数。`Query(default=10, ge=1, le=100)` 加默认值和范围。

**请求体 + Pydantic 模型**：POST/PATCH 提交的 JSON。定义一个继承 `BaseModel` 的类，`Field(min_length=3, ge=1, le=5)` 写校验规则，把它作为函数参数类型，FastAPI 就自动解析 JSON 并校验。实测提交 `{"title":"a","priority":9}` 会得到 422，并且一次性列出两个字段各自的错误。

**response_model**：声明"响应长什么样"。FastAPI 会按它过滤/序列化返回值（比如返回对象里有多余的敏感字段会被丢掉），同时写进文档。

**状态码与错误**：`status_code=201` 指定成功状态码；业务错误用 `raise HTTPException(status_code=404, detail="...")`，会自动变成对应状态码的 JSON 响应。实测：查不存在的工单 → 404，重复关闭 → 409，参数不合法 → 422（校验错误由框架自动产生）。

**APIRouter**：把一组相关路由放到独立的 `router` 对象里（带统一 `prefix` 和 `tags`），最后 `app.include_router(router)` 挂载。AgentNexus 的 `backend/server/routers/` 每个文件就是一个 router，路由层保持薄、按领域拆分。

**lifespan**：应用启动/关闭时各执行一次的逻辑，用 `@asynccontextmanager` 写，`yield` 前是启动、后是关闭。真实项目在这里建数据库连接池、Redis 客户端，关闭时释放。运行时你会在终端看到 `[lifespan] 应用启动`。

**CORS 中间件**：前端 Vue 开发服务器（`localhost:5173`）与后端（`localhost:8000`）端口不同，属于跨域，浏览器会拦截。后端用 `CORSMiddleware` 声明允许的来源。实测预检请求（OPTIONS）会返回 `access-control-allow-origin: http://localhost:5173`。

**Depends / BackgroundTasks / StreamingResponse**：见 `fastapi_learning.py`，README 里有说明。一句话回顾：Depends 抽公共逻辑；BackgroundTasks 是进程内、不持久的后台任务；StreamingResponse 把异步生成器变成流式响应。

## 5. 运行方式

### 环境准备

```bash
cd learning/fastapi
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # fastapi[standard] 已包含 uvicorn 和 fastapi 命令行工具
```

### 启动（两种等价方式）

```bash
# 方式 1：uvicorn，最通用，Docker/生产里用的就是它
uvicorn fastapi_basics:app --reload --port 8000

# 方式 2：fastapi 命令行（需要 fastapi[standard]），开发模式默认自带热重载
fastapi dev fastapi_basics.py
```

常用参数：`--reload` 改代码自动重启（只用于开发）；`--port` 端口；`--host 0.0.0.0` 允许外部访问（**在 Docker 容器里必须设成 0.0.0.0**，默认的 127.0.0.1 只能容器内部访问，这是 Docker 部署最常见的坑）；生产环境用 `--workers N` 开多进程。

### 访问入口

| 地址 | 是什么 |
|---|---|
| http://127.0.0.1:8000/docs | Swagger UI：网页上直接点着调接口，学习首选 |
| http://127.0.0.1:8000/redoc | 另一种风格的文档 |
| http://127.0.0.1:8000/openapi.json | 机器可读的接口描述，前端可据此生成 SDK |

### 命令行测试（fastapi_basics.py）

```bash
# 创建工单 → 201
curl -X POST http://127.0.0.1:8000/tickets -H 'Content-Type: application/json' -d '{"title":"无法登录","priority":2}'
# 提交非法数据 → 422，看返回的错误明细
curl -X POST http://127.0.0.1:8000/tickets -H 'Content-Type: application/json' -d '{"title":"a","priority":9}'
# 列表、过滤、详情
curl "http://127.0.0.1:8000/tickets?status=open&limit=5"
curl http://127.0.0.1:8000/tickets/1
curl -i http://127.0.0.1:8000/tickets/99      # 404
# 关闭工单；再关一次 → 409
curl -X PATCH http://127.0.0.1:8000/tickets/1/close
```

## 6. 最重要的一条规则：别在 async def 里做阻塞调用

`async def` 路由跑在**同一个事件循环**里。如果里面调用了同步阻塞函数（`time.sleep`、`requests.get`、同步数据库驱动、同步读大文件），整个事件循环被卡住，期间所有其他请求全部排队。`fastapi_basics.py` 里放了三个对照接口，各睡 1 秒，我实测同时发 3 个并发请求：

| 接口 | 写法 | 3 个并发总耗时 |
|---|---|---|
| `/blocking/bad` | `async def` + `time.sleep(1)` | **3.02 秒**（被串行化了） |
| `/blocking/good` | `async def` + `await asyncio.sleep(1)` | **1.01 秒** |
| `/blocking/sync-ok` | 普通 `def` + `time.sleep(1)` | **1.03 秒** |

结论：`async def` 里只能调用可 `await` 的异步库；必须用同步阻塞库时，要么把路由写成普通 `def`（FastAPI 自动放进线程池），要么用 `await asyncio.to_thread(...)` / `run_in_threadpool` 包起来。这条对 AgentNexus 很关键：数据库要选异步驱动（asyncpg / psycopg 异步模式），调用 LLM 用异步客户端，否则一个慢请求会拖垮整个服务。建议自己用 `curl` 并发打一下 bad 和 good，亲眼看耗时差异：

```bash
for i in 1 2 3; do curl -s -o /dev/null http://127.0.0.1:8000/blocking/bad & done; wait
```

## 7. 其他常见坑

- **忘了 `--host 0.0.0.0`**：容器里服务起来了但外面访问不了
- **用内存字典当数据库**：`--reload` 每次改代码进程重启，数据全没，这只是学习用，真实项目走 PostgreSQL
- **BackgroundTasks 当可靠队列用**：进程一死任务就丢，见 `fastapi_learning.py` 示例 2
- **`response_model` 与返回值不匹配**：会得到 500 而不是 422，因为这是服务端自己的错

## 8. 学习路线与练习建议

1. 跑 `fastapi_basics.py`，在 `/docs` 页面把每个接口点一遍，观察 422/404/409 的返回结构
2. 自己加一个接口：`DELETE /tickets/{id}`，返回 204；再给 `TicketCreate` 加一个 `customer_email` 字段，用 Pydantic 的 `EmailStr`（需要 `pip install email-validator`）
3. 做上面的并发阻塞实验，记下耗时
4. 把 `fastapi_learning.py` 的 `get_greeting_service` 改成"从请求头取 token 校验用户"的依赖，体会 Depends 怎么复用鉴权逻辑
5. 读官方教程（https://fastapi.tiangolo.com/tutorial/）的 Path/Query 参数、Request Body、Dependencies、Background Tasks 几章

## 9. 与 AgentNexus 各阶段的对应

| FastAPI 特性 | 用在哪 |
|---|---|
| APIRouter + 薄路由 | `backend/server/routers/`，阶段 1 起 |
| Depends | 注入 service、当前用户、数据库 session，阶段 1、阶段 5 权限校验 |
| lifespan | 应用启动时初始化 Postgres 连接池 / Redis / LangGraph checkpointer，阶段 1 |
| StreamingResponse | Agent 流式回复 SSE，阶段 1 里程碑 |
| Pydantic | 请求体、Agent 配置、工具调用参数校验 |
| CORS | 前端 Vue 开发服务器联调，阶段 1 |
| async 规则 | 全部数据库/模型调用选异步实现，贯穿所有阶段 |
