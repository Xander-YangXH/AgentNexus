"""数据迁移：给 0002 之前就存在的失败记录补上 error_code

Revision ID: 0003
Revises: 0002

为什么单独一个迁移，而不是改 0002：0002 已经在开发库执行过，改已执行的迁移不会重新运行，
各环境的库就会不一致。迁移只能往后加。

为什么按文字匹配：历史记录的异常对象早已不存在，只剩 error 列里的原始报错文本，
所以无法复用 runtime/failures.py 的“沿异常链找状态码”，只能用文本规则近似。
规则与 failures.py 的分类保持同一套错误码，匹配不上的一律归为 internal（保守）。
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 只处理“失败且还没有错误码”的行，所以重复执行是安全的（幂等）
    op.execute(
        """
        UPDATE agent_runs
        SET error_code = CASE
            WHEN error LIKE 'timeout after%' THEN 'timeout'
            WHEN error LIKE 'interrupted:%' THEN 'interrupted'
            WHEN error ~ '(\\m429\\M|RESOURCE_EXHAUSTED)' THEN 'llm_rate_limited'
            WHEN error ~ '(\\m5[0-9][0-9]\\M|UNAVAILABLE)' THEN 'llm_unavailable'
            WHEN error ~ '(\\m40[0134]\\M|INVALID_ARGUMENT|API_KEY_INVALID)' THEN 'llm_config'
            ELSE 'internal'
        END
        WHERE status = 'failed' AND error_code IS NULL
        """
    )


def downgrade() -> None:
    # 不可逆：无法区分哪些错误码是本迁移补的、哪些是运行时写入的。回退到 0002 时保留这些值也不影响 0002 的结构
    pass
