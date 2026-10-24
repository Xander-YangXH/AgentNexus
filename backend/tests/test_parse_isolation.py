"""不可信文件在子进程里解析（阶段 6.4，decisions/0025）：死循环被超时终止、内存炸弹被上限拦下，入库流水线不被卡死。

恶意解析函数必须定义在模块级：子进程按“模块名 + 函数名”导入它。
"""

import multiprocessing
import sys
import time

import pytest

from agentnexus.knowledge.isolation import parse_isolated
from agentnexus.knowledge.parsing import ParseError, Section, parse
from agentnexus.repositories.knowledge import DocumentRepository
from agentnexus.storage.postgres import DocumentStatus
from tests.test_ingest import _doc, svc  # noqa: F401 —— 复用入库测试的夹具
from tests.test_parsing import make_pdf


def hang(filename, data):
    while True:
        pass


def bomb(filename, data):
    chunks = []
    while True:
        chunks.append(bytearray(64 << 20))


def explode(filename, data):
    raise RuntimeError("boom")


def refuse(filename, data):
    raise ParseError("encrypted", "文件已加密")


def test_normal_pdf_parses_the_same_as_in_process():
    data = make_pdf(["Return policy", "Refund timing"])
    assert parse_isolated("a.pdf", data) == parse("a.pdf", data)


def test_plain_text_is_parsed_in_process():
    # .txt / .md 只是解码，不启动子进程：传入的解析函数在本进程里被直接调用（局部函数无法被子进程导入，能成功就说明没走子进程）
    assert parse_isolated("a.txt", b"x", parser=lambda f, d: [Section("in-process", None)]) == [Section("in-process", None)]


def test_infinite_loop_is_killed_after_the_timeout():
    start = time.monotonic()
    with pytest.raises(ParseError) as exc:
        parse_isolated("evil.pdf", b"%PDF", timeout=1.0, parser=hang)
    assert exc.value.code == "parse_timeout"
    assert time.monotonic() - start < 5
    assert not multiprocessing.active_children()  # 卡住的子进程被杀掉了，不会留在后台继续占 CPU


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS 只在 Linux 上生效（macOS 上只有超时这一层）")
def test_memory_bomb_is_stopped_by_the_limit():
    with pytest.raises(ParseError) as exc:
        parse_isolated("evil.pdf", b"%PDF", timeout=30, mem_mb=256, parser=bomb)
    assert exc.value.code in ("too_complex", "parse_failed")


def test_errors_inside_the_child_keep_their_meaning():
    with pytest.raises(ParseError) as exc:
        parse_isolated("a.docx", b"PK", parser=refuse)
    assert exc.value.code == "encrypted"  # 解析器自己的判断原样传回
    with pytest.raises(ParseError) as exc:
        parse_isolated("a.docx", b"PK", parser=explode)
    assert exc.value.code == "corrupt" and "RuntimeError" in str(exc.value)


async def test_a_hanging_document_fails_and_the_next_one_is_still_processed(svc, user):  # noqa: F811
    svc.parse_timeout, svc.parser = 1.0, hang
    evil = await _doc(svc, user, name="evil.pdf")
    assert await svc.process(evil.id)
    async with svc.sessionmaker() as session:
        got = await DocumentRepository(session).get(evil.id)
        assert (got.status, got.error_code) == (DocumentStatus.FAILED, "parse_timeout")

    svc.parser = parse  # 之后上传的文档照常入库：没有被卡死的线程占住执行槽
    good = await _doc(svc, user, text="发票：下单后 30 天内可以申请。", name="ok.txt")
    assert await svc.process(good.id)
    async with svc.sessionmaker() as session:
        assert (await DocumentRepository(session).get(good.id)).status == DocumentStatus.READY
