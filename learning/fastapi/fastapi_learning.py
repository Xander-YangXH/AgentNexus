"""
FastAPI 学习示例 —— 配合 AgentNexus 阶段 0 学习内容食用

运行方式：
    uvicorn fastapi_learning:app --reload --port 8000

启动后打开 http://127.0.0.1:8000/docs 用 Swagger UI 交互式测试，
或者看 README.md 里的 curl 命令。三个示例分别验证：
    1. 异步路径函数 + Depends 依赖注入（路由薄、service 厚）
    2. BackgroundTasks（进程内后台任务，理解它和真正任务队列的区别）
    3. StreamingResponse 做 SSE 流式输出
"""

import asyncio
import time
from typing import AsyncGenerator

from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI(title="AgentNexus FastAPI 学习示例")


# ---------- 示例 1：异步路径函数 + Depends 依赖注入 ----------
class GreetingService:
    """假装这是一个 service 层对象——真实项目里这里会是数据库查询、业务逻辑等。"""

    def greet(self, name: str) -> str:
        return f"你好，{name}！这句话是从 GreetingService（service 层）返回的，不是路由函数自己拼的。"


def get_greeting_service() -> GreetingService:
    """
    这是一个"依赖提供者"函数。FastAPI 在每次请求进来时调用它，
    把返回值注入进路由函数的参数里。真实项目里，这里常见的是
    "拿数据库 session""解析并校验当前登录用户"这类跟业务本身无关、
    但每个路由都要用的逻辑，所以抽成依赖复用，而不是每个路由自己写一遍。
    """
    return GreetingService()


@app.get("/examples/1/greet")
async def example_1_greet(
    name: str = "world",
    service: GreetingService = Depends(get_greeting_service),
):
    """
    路由函数本身很"薄"：只负责接收参数、调用 service、组装响应，
    不自己写业务逻辑——这是 AgentNexus 分层架构"路由薄、service 厚、
    repository 独立"的最小示范。
    """
    return {"message": service.greet(name)}


# ---------- 示例 2：BackgroundTasks ----------
def slow_side_effect(name: str):
    """模拟一个耗时的后台操作，比如发通知、写日志。"""
    time.sleep(3)
    print(f"[后台任务] {time.strftime('%H:%M:%S')} 给 {name} 发送的通知处理完了")


@app.post("/examples/2/notify")
async def example_2_notify(name: str, background_tasks: BackgroundTasks):
    """
    这个接口会立刻返回响应，不等 slow_side_effect 跑完——观察终端日志，
    3 秒后才会打印"处理完了"，说明响应和后台任务是解耦的。

    关键提醒：这个任务只存在于当前进程内存里。响应返回之后、
    slow_side_effect 真正跑完之前，如果把这个 uvicorn 进程杀掉（Ctrl+C），
    这条"发通知"的任务会彻底消失，没有任何补偿机制——
    这正是阶段 1 为什么还要单独引入 ARQ 这种真正的任务队列：
    BackgroundTasks 只适合"响应发完后顺手做点小事，丢了也无所谓"的场景，
    扛不住"必须保证执行"的需求。建议自己动手试一次这个杀进程的实验，
    亲眼确认任务真的丢了，比看这段注释记得牢。
    """
    background_tasks.add_task(slow_side_effect, name)
    return {"message": f"已收到，正在后台处理 {name} 的通知（不会阻塞这次响应）"}


# ---------- 示例 3：StreamingResponse 做 SSE ----------
async def fake_llm_token_stream(message: str) -> AsyncGenerator[str, None]:
    """
    模拟一个 LLM 逐字生成的过程——真实场景里这里会是
    LangGraph 的 `.stream()` 输出，或者调用模型 API 的流式返回。
    """
    reply = f"收到你的消息「{message}」，这是逐字流式吐出来的回复示例。"
    for ch in reply:
        # SSE 协议格式：每条消息以 "data: ...\n\n" 结尾，浏览器/前端按这个格式解析
        yield f"data: {ch}\n\n"
        await asyncio.sleep(0.08)
    yield "data: [DONE]\n\n"


@app.get("/examples/3/stream-chat")
async def example_3_stream_chat(message: str = "你好"):
    """
    用 curl -N 这个接口能看到字符一个个蹦出来的效果
    （-N 关闭 curl 自己的输出缓冲，不然会等全部数据到了才一次性显示）。

    这就是阶段 1"网页里能和 Agent 聊天、回复是流式的"这个里程碑背后的机制：
    把一个异步生成器（持续吐出小块数据的函数）包成 StreamingResponse，
    HTTP 连接保持打开，服务端每生成一点就往外推一点，
    而不是等全部生成完了再一次性返回——这条 HTTP 连接后面接的东西，
    以后会换成 LangGraph 的 .stream()。
    """
    return StreamingResponse(
        fake_llm_token_stream(message),
        media_type="text/event-stream",
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
