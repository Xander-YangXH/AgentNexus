class ConversationNotFound(Exception):
    """对话不存在或不属于当前用户（对外不区分，避免泄露 id 是否存在）。"""


class RunNotFound(Exception):
    pass


class RunConflict(Exception):
    """该对话已有未结束的 run。"""


class UserExists(Exception):
    pass


class RunAlreadyFinished(Exception):
    """run 已经成功或失败，不能再取消。"""


class RunNotRetryable(Exception):
    """只有失败或已取消、且输入是对话最后一条消息的 run 才能重试。"""


class RunNotAwaitingApproval(Exception):
    """run 不在等待审批（已被批准、取消或过期）。"""


class ArtifactNotFound(Exception):
    """产物不存在或不属于当前用户（对外不区分）。"""


class ConversationArchived(Exception):
    """对话已归档，恢复后才能继续发消息或重试。"""


# ---------- 知识库（阶段 2） ----------


class KnowledgeBaseNotFound(Exception):
    """知识库不存在或不属于当前用户（对外不区分）。"""


class KnowledgeBaseExists(Exception):
    pass


class DocumentNotFound(Exception):
    pass


class DocumentExists(Exception):
    """同一知识库里已经有内容完全相同的文件。"""

    def __init__(self, existing_id):
        super().__init__(str(existing_id))
        self.existing_id = existing_id


class DocumentNotRetryable(Exception):
    """只有处理失败的文档才能重新处理。"""


class UnsupportedFileType(Exception):
    pass


class FileTooLarge(Exception):
    pass


class EmptyFile(Exception):
    pass


class KnowledgeBaseEmpty(Exception):
    """知识库里还没有可用的分块（没有处理完成的文档），无法构建图谱。"""


class GraphBuildInProgress(Exception):
    """这个知识库已经有一个进行中的图谱构建。"""


class GraphBuildTooLarge(Exception):
    """需要抽取的分块数超过单次构建的上限（防止误操作花掉一大笔模型费用）。"""

    def __init__(self, extract_count: int, limit: int):
        super().__init__(f"{extract_count} > {limit}")
        self.extract_count = extract_count
        self.limit = limit


class EntityNotFound(Exception):
    pass
