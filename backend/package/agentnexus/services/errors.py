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


class ConversationArchived(Exception):
    """对话已归档，恢复后才能继续发消息或重试。"""
