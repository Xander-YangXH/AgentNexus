import uuid

from fastapi import APIRouter, Depends

from agentnexus.services.conversations import ConversationService
from agentnexus.services.runs import RunService
from agentnexus.runtime.dispatcher import RunDispatcher
from server.deps import CurrentUser, get_conversation_service, get_dispatcher, get_run_service
from server.schemas import (
    ConversationCreate,
    ConversationOut,
    MessageCreate,
    MessageOut,
    RunOut,
)

router = APIRouter(prefix="/api", tags=["conversations"])


@router.post("/conversations", response_model=ConversationOut, status_code=201)
async def create_conversation(
    body: ConversationCreate,
    user_id: CurrentUser,
    svc: ConversationService = Depends(get_conversation_service),
):
    return await svc.create(user_id, body.title)


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    user_id: CurrentUser, svc: ConversationService = Depends(get_conversation_service)
):
    return await svc.list_for_user(user_id)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: uuid.UUID,
    user_id: CurrentUser,
    svc: ConversationService = Depends(get_conversation_service),
):
    return await svc.messages(user_id, conversation_id)


@router.post("/conversations/{conversation_id}/messages", response_model=RunOut, status_code=202)
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageCreate,
    user_id: CurrentUser,
    svc: RunService = Depends(get_run_service),
    dispatcher: RunDispatcher = Depends(get_dispatcher),
):
    """202 Accepted：消息和 run 已落库，执行是异步的，用 run 的 id 查进度。"""
    run = await svc.submit(user_id, conversation_id, body.content)
    # 投递在提交成功之后；即使这里什么都没发生，周期扫描也会把 queued 的 run 补投
    dispatcher.enqueue(run.id)
    return run


@router.get("/conversations/{conversation_id}/active-run", response_model=RunOut | None)
async def get_active_run(
    conversation_id: uuid.UUID, user_id: CurrentUser, svc: RunService = Depends(get_run_service)
):
    """对话正在排队/执行的 run，没有则返回 null。前端刷新后用它重新订阅事件流（从头回放已生成的部分）。"""
    return await svc.active_for_conversation(user_id, conversation_id)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: uuid.UUID, user_id: CurrentUser, svc: RunService = Depends(get_run_service)
):
    return await svc.get(user_id, run_id)
