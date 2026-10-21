"""分块（阶段 2.1）：不超限、不在句中切断、有重叠、不丢内容。"""

import random

from agentnexus.knowledge.chunking import chunk_sections, split_sentences
from agentnexus.knowledge.parsing import Section
from agentnexus.knowledge.tokens import EstimatingCounter


class LenCounter:
    """测试用：一个字符一个 token，结果可预期。"""

    def count(self, text):
        return len(text)


def _sentences(n, rng, prefix="句"):
    return [f"{prefix}{i}" + "字" * rng.randint(5, 40) + "。" for i in range(n)]


def test_split_sentences_keeps_punctuation_and_handles_newlines():
    assert split_sentences("第一句。第二句！\n第三句？结尾没有标点") == ["第一句。", "第二句！", "第三句？", "结尾没有标点"]
    assert split_sentences("他说：“可以。”然后走了。") == ["他说：“可以。”", "然后走了。"]  # 引号跟着句号走


def test_short_text_is_one_chunk():
    chunks = chunk_sections([Section("只有一句话。")], LenCounter())
    assert [(c.seq, c.text, c.token_count) for c in chunks] == [(0, "只有一句话。", 6)]


def test_limits_boundaries_overlap_and_coverage():
    rng = random.Random(1)
    sentences = _sentences(80, rng)
    counter = LenCounter()
    chunks = chunk_sections([Section("".join(sentences))], counter, target_tokens=100, hard_limit=150, overlap_tokens=30)
    assert len(chunks) > 5
    for c in chunks:
        assert c.token_count <= 150  # 不超过硬上限（超过会被模型截断）
        assert c.text.endswith("。")  # 不在句子中间切断
    with_overlap = 0
    for a, b in zip(chunks, chunks[1:]):
        shared = [s for s in split_sentences(b.text) if s in split_sentences(a.text)]
        # 重叠只取整句：上一块最后一句不超过重叠上限时必须有重叠；超过时宁可不重叠，也不带半句话
        if counter.count(split_sentences(a.text)[-1]) <= 30:
            assert shared
        assert sum(len(s) for s in shared) <= 30  # 重叠不超过设定值
        with_overlap += bool(shared)
    assert with_overlap > 0
    for s in sentences:
        assert any(s in c.text for c in chunks)  # 不丢内容
    assert [c.seq for c in chunks] == list(range(len(chunks)))


def test_last_chunk_is_not_just_overlap():
    rng = random.Random(2)
    for n in range(1, 40):
        chunks = chunk_sections([Section("".join(_sentences(n, rng)))], LenCounter(), target_tokens=60, hard_limit=90, overlap_tokens=20)
        if len(chunks) >= 2:
            last_sentences = split_sentences(chunks[-1].text)
            assert any(s not in chunks[-2].text for s in last_sentences)  # 最后一块必须有新内容


def test_overlong_sentence_is_hard_split_within_limit():
    long_sentence = "无标点" * 200  # 600 个字符、没有任何句末标点
    chunks = chunk_sections([Section(long_sentence)], LenCounter(), target_tokens=100, hard_limit=150, overlap_tokens=0)
    assert all(c.token_count <= 150 for c in chunks)
    assert "".join(c.text for c in chunks) == long_sentence


def test_page_numbers_follow_sections():
    sections = [Section("第一页的内容。" * 10, page=1), Section("第二页的内容。" * 10, page=2)]
    chunks = chunk_sections(sections, LenCounter(), target_tokens=40, hard_limit=60, overlap_tokens=0)
    assert chunks[0].page == 1 and chunks[-1].page == 2
    assert all(("第二页" not in c.text) or c.page == 2 or "第一页" in c.text for c in chunks)


def test_estimating_counter_matches_bert_chinese_behaviour():
    c = EstimatingCounter()
    assert c.count("退货政策") == 4  # 中文每字一个 token
    assert c.count("退货，7天内。") >= 6
    assert c.count("API rate limit") >= 3


def test_overlap_is_dropped_when_it_would_break_the_hard_limit():
    """上一块带过来的重叠 + 下一个长句会超过硬上限时，必须放弃重叠，而不是生成超限的块。"""
    short = "短句十四个字的内容在这里。"  # 14 个字符
    long_sentence = "长" * 54 + "。"  # 55 个字符：14 + 55 = 69 > 60
    text = short * 3 + long_sentence
    chunks = chunk_sections([Section(text)], LenCounter(), target_tokens=50, hard_limit=60, overlap_tokens=20)
    assert all(c.token_count <= 60 for c in chunks)
    assert chunks[-1].text == long_sentence  # 没有带上重叠


def test_overlap_never_copies_the_whole_previous_chunk():
    """上一块很短（因为下一句太长装不下而提前输出）时，不能把它整块带进下一块，否则下一块完全包含上一块。"""
    short = "短句。"  # 3 个字符，远小于重叠上限
    long_sentence = "长" * 98 + "。"  # 3 + 99 = 102 > 目标 100：短句被单独输出
    chunks = chunk_sections([Section(short + long_sentence)], LenCounter(), target_tokens=100, hard_limit=150, overlap_tokens=30)
    assert [c.text for c in chunks] == [short, long_sentence]


class WordPieceLikeCounter:
    """模仿 bge 的 WordPiece：按空白分词，超过 100 个字符的词整体算 1 个 [UNK]。记录被要求计数的最长文本。"""

    def __init__(self):
        self.longest = 0

    def count(self, text):
        self.longest = max(self.longest, len(text))
        return sum(1 if len(w) > 100 else len(w) for w in text.split())


def test_giant_word_is_split_by_characters_even_if_the_model_counts_it_as_one_token():
    """实测踩到：3MB 的连续字符被模型记成 1 个 token，整段成了一块。分块必须有字符上限。"""
    text = "a" * 1_000_000
    counter = WordPieceLikeCounter()
    chunks = chunk_sections([Section(text)], counter, target_tokens=300, hard_limit=480, overlap_tokens=0)
    assert max(len(c.text) for c in chunks) <= 480 * 6
    assert "".join(c.text for c in chunks) == text
    # 性能：每次只对不超过“上限 × 6”个字符的文本计数，而不是每切一段都把剩下的几 MB 重新分词
    assert counter.longest <= 480 * 6


def test_stored_token_count_is_the_models_own_count():
    chunks = chunk_sections([Section("b" * 1000)], WordPieceLikeCounter(), target_tokens=300, hard_limit=480, overlap_tokens=0)
    assert [c.token_count for c in chunks] == [1]  # 1000 个字符 < 2880：一块；记录的是模型真实看到的 token 数
