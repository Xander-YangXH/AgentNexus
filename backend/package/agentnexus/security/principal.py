"""当前用户（“主体”）：认证依赖解析出来后放进上下文变量，数据库会话在每个事务开始时据此设置 RLS 用的身份。

为什么用上下文变量：服务层的大部分方法只接收 user_id，没必要为了 RLS 改所有签名；
而且 SSE 这类流式响应在子任务里执行，上下文变量会被复制过去，身份不会丢。
"""

import contextvars
import dataclasses
import uuid

from sqlalchemy import event, text
from sqlalchemy.orm import Session


@dataclasses.dataclass(frozen=True)
class Principal:
    id: uuid.UUID
    name: str
    role: str
    department_id: uuid.UUID | None


principal_var: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("principal", default=None)

_SET_IDENTITY = text(
    "SELECT set_config('app.user_id', :u, true), set_config('app.department_id', :d, true), "
    "set_config('app.role', :r, true)"
)


def install_rls_identity(session_class: type[Session] = Session) -> None:
    """每个事务开始时设置当前用户（事务级，is_local=true）。

    必须是事务级：会话级 SET 在连接池里会“串”到复用这条连接的下一个请求（实验：下一个请求没设置身份，看到的是上一个用户的数据）。
    没有当前用户（登录接口、后台任务）时不设置：受 RLS 约束的表默认什么都看不到。
    对不受 RLS 约束的角色（迁移、worker）设置了也没有影响。重复安装是幂等的。
    """
    if not event.contains(session_class, "after_begin", _after_begin):
        event.listen(session_class, "after_begin", _after_begin)


def _after_begin(session, transaction, connection) -> None:
    p = principal_var.get()
    if p is not None:
        connection.execute(_SET_IDENTITY, {"u": str(p.id), "d": str(p.department_id or ""), "r": p.role})
