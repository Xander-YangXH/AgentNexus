# FastAPI 学习 Playground

配合 AgentNexus 阶段 0 剩下的学习内容：异步路径函数 + `Depends` 依赖注入、`BackgroundTasks`、`StreamingResponse` 做 SSE。纯练习代码，不是 AgentNexus 产品代码。

## 环境准备

```bash
cd learning/fastapi
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 怎么跑

```bash
uvicorn fastapi_learning:app --reload --port 8000
```

启动后打开 http://127.0.0.1:8000/docs 能看到 FastAPI 自带的 Swagger UI，三个示例都能在网页上直接点着试；也可以用命令行：

```bash
# 示例 1：Depends 依赖注入，路由薄、逻辑在 service 层
curl "http://127.0.0.1:8000/examples/1/greet?name=yang"

# 示例 2：BackgroundTasks —— 立刻拿到响应，3 秒后终端才打印后台任务完成日志
curl -X POST "http://127.0.0.1:8000/examples/2/notify?name=yang"
```

示例 2 强烈建议自己动手做一次实验：调用完接口之后，在 3 秒任务跑完之前，回到起服务的终端按 `Ctrl+C` 杀掉进程。你会发现"给 yang 发送的通知处理完了"这行日志永远不会出现——**这个任务连同它承诺过的副作用一起消失了，没有任何补偿机制**。这就是为什么阶段 1 还要单独引入 ARQ 这种真正的任务队列：`BackgroundTasks` 只适合"响应发完后顺手做点小事，丢了也无所谓"的场景，扛不住"必须保证执行"的需求，这也是 AgentNexus 里 Agent 任务为什么要走"先落库再投递队列"而不是简单用 `BackgroundTasks` 的直接原因。

```bash
# 示例 3：StreamingResponse 做 SSE，-N 关闭 curl 的输出缓冲才能看到逐字效果
curl -N "http://127.0.0.1:8000/examples/3/stream-chat?message=你好"
```

## 每个示例在验证什么

| 示例 | 验证的概念 | 对应 AgentNexus 里的什么 |
|---|---|---|
| 1 | `Depends` 依赖注入，路由函数只做请求处理，业务逻辑在 service 层 | 后端"路由薄、service 厚、repository 独立"的分层架构 |
| 2 | `BackgroundTasks` 是进程内的、不持久化、不可恢复的后台任务 | 解释了为什么阶段 1 需要引入真正的异步任务队列（ARQ），而不是用 `BackgroundTasks` 对付 |
| 3 | 用异步生成器 + `StreamingResponse` 实现 SSE 流式响应 | 阶段 1"网页里能和 Agent 聊天、回复是流式的"这个里程碑背后的机制，也是 LangGraph `.stream()` 输出最终要对接到的地方 |
