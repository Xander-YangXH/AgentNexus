"""测试用的替身。"""


class FakeEmbedder:
    """确定性的假向量：按字的二元组哈希到固定维度再归一化。字面越像的文本越相近，足够测试检索流程，不加载模型。"""

    dim = 64

    def embed_documents(self, texts):
        return [self._one(t) for t in texts]

    def embed_query(self, text):
        return self._one(text)

    def _one(self, text):
        import hashlib
        import math

        v = [0.0] * self.dim
        grams = [text[i:i + 2] for i in range(max(1, len(text) - 1))]
        for g in grams:
            v[int(hashlib.md5(g.encode()).hexdigest(), 16) % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


# 是否加载过真实的 ONNX 模型（fastembed）。见 conftest.py 的 pytest_unconfigure
NATIVE_MODELS = {"loaded": False}


# ---------- 测试用的鉴权（decisions/0023） ----------

def install_test_auth(app) -> None:
    """接口测试的鉴权替身：用请求头 X-User-Id 直接指定当前用户（阶段 5 之前的占位方式），并保留“创建用户”的测试接口。

    只在测试里使用：真实的登录、Cookie 会话、CSRF、角色检查由 test_auth.py 覆盖。这样已有的几百个接口断言不用改。
    """
    import uuid as _uuid

    from fastapi import Depends, Header, HTTPException
    from pydantic import BaseModel, Field

    from agentnexus.security.auth import AuthService
    from agentnexus.security.principal import Principal, principal_var
    from agentnexus.services.users import UserService
    from agentnexus.storage.postgres import User
    from server.deps import current_principal, get_sessionmaker
    from server.schemas import UserOut

    async def principal_from_header(sm=Depends(get_sessionmaker), x_user_id: str | None = Header(default=None)):
        try:
            user_id = _uuid.UUID(x_user_id or "")
        except ValueError:
            raise HTTPException(status_code=401, detail="missing or invalid X-User-Id") from None
        async with sm() as session:
            user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="unknown user")
        p = Principal(id=user.id, name=user.name, role=user.role, department_id=user.department_id)
        principal_var.set(p)
        return p

    app.dependency_overrides[current_principal] = principal_from_header

    class _UserCreate(BaseModel):
        name: str = Field(min_length=1, max_length=64)

    @app.post("/api/users", response_model=UserOut, status_code=201, include_in_schema=False)
    async def create_test_user(body: _UserCreate, sm=Depends(get_sessionmaker)):
        return await UserService(sm).create(body.name)

    @app.middleware("http")
    async def _auth_service(request, call_next):  # 真实登录接口需要 app.state.auth_service
        if not hasattr(request.app.state, "auth_service"):
            override = app.dependency_overrides.get(get_sessionmaker)
            request.app.state.auth_service = AuthService(override() if override else request.app.state.sessionmaker)
        return await call_next(request)
