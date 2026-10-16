class ConversationNotFound(Exception):
    """对话不存在或不属于当前用户（对外不区分，避免泄露 id 是否存在）。"""


class RunNotFound(Exception):
    pass


class RunConflict(Exception):
    """该对话已有未结束的 run。"""


class UserExists(Exception):
    pass
