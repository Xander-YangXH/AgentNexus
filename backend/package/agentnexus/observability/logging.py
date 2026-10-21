"""日志配置：一行一条，自动带上 request_id / run_id（decisions/0011）。

- text：给人看（开发时 docker compose logs）；
- json：给机器看（以后接日志平台、按 request_id 检索、做 Dashboard 统计）。
不引入第三方日志库：标准库 logging + 一个过滤器 + 一个格式化器就够了。
"""

import json
import logging
import sys
from datetime import UTC, datetime

from agentnexus.observability.context import request_id_var, run_id_var

_RESERVED = set(vars(logging.makeLogRecord({})))  # LogRecord 自带的属性，其余视为 extra 字段


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # 已经通过 extra 显式传入的值优先（例如 500 错误处理时请求上下文已经退出）
        if getattr(record, "request_id", None) is None:
            record.request_id = request_id_var.get()
        if getattr(record, "run_id", None) is None:
            record.run_id = run_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
            "run_id": getattr(record, "run_id", None),
        }
        for key, value in vars(record).items():  # extra={...} 传入的结构化字段
            if key not in _RESERVED and key not in entry and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [req=%(request_id)s run=%(run_id)s] %(message)s"
_HANDLER_NAME = "agentnexus"


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """可重复调用（测试、重载时不会叠加多个处理器）。"""
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if h.get_name() != _HANDLER_NAME]
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(level.upper())
