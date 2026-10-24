"""回答质量评测的裁判（阶段 7.7）：用模型对照参考条目，判断回答是否正确、是否完整。

为什么需要：eval_answer_api.py 只看客观可判断的部分（是否检索、引用是否指向正确文档），回答本身对不对、
有没有漏掉条件，一直没有评估（RAG 走读的“已知局限”）。

为什么要校准：裁判也是模型，可能“谁都给对”。所以同一批回答再造一份“篡改版”——把回答里的数字改掉
（7 天 → 17 天），裁判应该判错。篡改版被判错的比例就是裁判对事实错误的检出率；原版被判对的比例才可信。

两处第一版的错误（第一次运行后发现，见 dev-log）：
- 参考只给了正确答案那一条：回答里引用的其他条目（同样来自知识库）被判成“编造”。参考改为“正确条目 + 回答引用的条目”；
- 篡改时把引用标记 [1] 也改成了 [11]：这不是事实错误，裁判判“正确”是对的，却拉低了检出率。篡改时跳过引用标记
  和列表序号（“1.”）；
- 结论与理由矛盾：理由里写着“13 天应为 3 天，correct 应为 false”，JSON 里却是 true。让裁判先写理由、再给结论。

只在 worker 容器里运行（要用 .env 里的模型配置）。两种用法：
- 被 eval_answer_api.py 在 EVAL_JUDGE=1 时调用；
- 对已保存的结果重新评分（不重新提问，只调用裁判）：python answer_judge.py results/answer_eval_*.json
"""

import asyncio
import dataclasses
import json
import re
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

JUDGE_PROMPT = """你是严格的评测员。根据“参考条目”判断“回答”：
- correct：回答里的事实（数字、条件、范围）与参考条目一致，没有编造参考条目之外的规定；只要有一处与参考条目矛盾就算不正确。
- complete：回答覆盖了用户问题需要的关键条件（例如时限、例外情况）；可以比参考条目简略，但不能漏掉直接影响答案的条件。
先写理由，再下结论。只输出 JSON：{"reason": "一两句话，指出不一致或遗漏的地方", "correct": true/false, "complete": true/false}"""

# 先匹配引用标记 [n] 和行首的列表序号（1. / 1、），原样保留；其余数字才是事实
_NUMBER = re.compile(r"\[\d+\]|^[ \t]*\d+[.、]|\d+", re.M)


def corrupt(answer: str) -> str | None:
    """把回答里的事实数字改掉（加 10），引用标记 [n] 不动。没有事实数字的回答返回 None（不参与校准）。"""
    changed = False

    def bump(m):
        nonlocal changed
        if m.group().startswith("[") or not m.group().strip().isdigit():
            return m.group()
        changed = True
        return str(int(m.group()) + 10)

    out = _NUMBER.sub(bump, answer)
    return out if changed else None


def references(docs: dict[str, str], answer_doc: str, cited: list[str]) -> str:
    """正确条目 + 回答引用的条目（它们同样来自知识库，不算编造）。"""
    ids = list(dict.fromkeys([answer_doc, *cited]))
    return "\n".join(f"- {docs[i]}" for i in ids if i in docs)


@dataclasses.dataclass
class Verdict:
    correct: bool
    complete: bool
    reason: str


class Judge:
    def __init__(self, model):
        self.model = model

    @classmethod
    def from_settings(cls) -> "Judge":
        from agentnexus.config import get_settings
        from agentnexus.models.factory import build_chat_model

        settings = dataclasses.replace(get_settings(), llm_thinking="disabled")
        return cls(build_chat_model(settings))

    async def judge(self, question: str, reference: str, answer: str) -> Verdict:
        reply = await self.model.ainvoke([
            SystemMessage(JUDGE_PROMPT),
            HumanMessage(f"用户问题：{question}\n\n参考条目：{reference}\n\n回答：{answer}"),
        ])
        text = reply.content if isinstance(reply.content, str) else "".join(
            part.get("text", "") for part in reply.content if isinstance(part, dict))
        match = re.search(r"\{.*\}", text, re.S)
        data = json.loads(match.group()) if match else {}
        return Verdict(bool(data.get("correct")), bool(data.get("complete")), str(data.get("reason", text[:80])))


async def grade(rows: list[dict], docs: dict[str, str]) -> None:
    """给每个回答评分，并用篡改版做校准；结果写回 rows，打印汇总。"""
    judge = Judge.from_settings()
    graded = [r for r in rows if r.get("content")]
    for r in graded:
        ref = references(docs, r["answer_doc"], r.get("cited", []))
        v = await judge.judge(r["q"], ref, r["content"])
        r["judge"] = dataclasses.asdict(v)
        bad = corrupt(r["content"])
        r.pop("judge_corrupted", None)
        if bad is not None:
            r["judge_corrupted"] = dataclasses.asdict(await judge.judge(r["q"], ref, bad))
        print(f"{'✓' if v.correct and v.complete else '✗'} {r['q']}  正确 {v.correct} 完整 {v.complete}  {v.reason}")
    n = len(graded)
    calib = [r for r in graded if "judge_corrupted" in r]
    print(f"\n裁判：{n} 个回答，正确 {sum(r['judge']['correct'] for r in graded) / n:.2f}，"
          f"完整 {sum(r['judge']['complete'] for r in graded) / n:.2f}；"
          f"校准：{len(calib)} 个含事实数字的回答篡改后，被判错 "
          f"{sum(not r['judge_corrupted']['correct'] for r in calib) / max(1, len(calib)):.2f}")


if __name__ == "__main__":
    data = json.loads((Path(__file__).parent / "retrieval_zh_v0.json").read_text())
    path = Path(sys.argv[1])
    saved = json.loads(path.read_text())
    asyncio.run(grade(saved, data["docs"]))
    path.write_text(json.dumps(saved, ensure_ascii=False, indent=1))
