import logging
import uuid

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from agentnexus.services.conversations import ConversationService
from agentnexus.services.runs import RunService
from agentnexus.runtime.dispatcher import CancelOutcome, RunDispatcher
from agentnexus.services.errors import RunAlreadyFinished
from agentnexus.storage.postgres import RunStatus
from server.deps import CurrentUser, OwnedRun, get_conversation_service, get_dispatcher, get_run_service
from server.schemas import (
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
    MessageCreate,
    MessageOut,
    RunOut,
)

router = APIRouter(prefix="/api", tags=["conversations"])
log = logging.getLogger(__name__)


@router.post("/conversations", response_model=ConversationOut, status_code=201)
async def create_conversation(
    body: ConversationCreate,
    user_id: CurrentUser,
    svc: ConversationService = Depends(get_conversation_service),
):
    return await svc.create(user_id, body.title)


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    user_id: CurrentUser,
    archived: bool = False,
    svc: ConversationService = Depends(get_conversation_service),
):
    """按最近活动倒序。默认只列未归档的；archived=true 列出已归档的。"""
    return await svc.list_for_user(user_id, archived=archived)


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: uuid.UUID,
    body: ConversationUpdate,
    user_id: CurrentUser,
    svc: ConversationService = Depends(get_conversation_service),
):
    """改标题、归档 / 恢复。只改请求里出现的字段。"""
    kwargs = {"archived": body.archived}
    if "title" in body.model_fields_set:  # 区分“没传 title”和“title 传了 null（清空）”
        kwargs["title"] = body.title
    return await svc.update(user_id, conversation_id, **kwargs)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: uuid.UUID,
    user_id: CurrentUser,
    response: Response,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    svc: ConversationService = Depends(get_conversation_service),
):
    """游标分页：返回 before_seq 之前（不含）的最近 limit 条，按时间正序。

    是否还有更早的消息放在响应头 X-Has-More（true/false）里，保持响应体仍是列表、不破坏已有调用方。
    """
    items, has_more = await svc.messages(user_id, conversation_id, before_seq=before_seq, limit=limit)
    response.headers["X-Has-More"] = "true" if has_more else "false"
    return items


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
    # 这条日志自动带 request_id，同时写出 run_id：从一次请求追到一次执行（执行期间的日志都带 run_id）
    log.info("run submitted", extra={"run_id": str(run.id), "conversation_id": str(conversation_id)})
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


@router.get("/conversations/{conversation_id}/runs", response_model=list[RunOut])
async def list_runs(
    conversation_id: uuid.UUID, user_id: CurrentUser, svc: RunService = Depends(get_run_service)
):
    """对话的所有 run（按创建时间）。前端用它在刷新后显示“回复失败 / 已停止”及重试入口。"""
    return await svc.list_for_conversation(user_id, conversation_id)


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(
    run: OwnedRun,
    response: Response,
    svc: RunService = Depends(get_run_service),
    dispatcher: RunDispatcher = Depends(get_dispatcher),
):
    """取消 run。

    - 200：还在排队，已直接取消；或早已取消（重复请求无害）
    - 202：正在执行，已发出取消信号；最终结果以 run.cancelled 事件 / run 状态为准
    - 409：已经成功或失败，无法取消
    """
    outcome = await dispatcher.cancel(run.id)
    current = await svc.find(run.id)
    if outcome == CancelOutcome.REQUESTED:
        response.status_code = 202
    elif outcome == CancelOutcome.FINISHED and current.status != RunStatus.CANCELLED:
        raise RunAlreadyFinished(str(run.id))
    return current


@router.post("/runs/{run_id}/retry", response_model=RunOut, status_code=202)
async def retry_run(
    run_id: uuid.UUID,
    user_id: CurrentUser,
    svc: RunService = Depends(get_run_service),
    dispatcher: RunDispatcher = Depends(get_dispatcher),
):
    """对失败或已取消的 run 重试：同一条输入消息新建一个 run（不新增用户消息）。只能重试对话的最后一条消息。"""
    new_run = await svc.retry(user_id, run_id)
    dispatcher.enqueue(new_run.id)
    return new_run
