"""Long-document chunking (graph-engineering playbook, scaling guidance):
a document over the size threshold is chunked at section boundaries with a
trailing overlap, so a single extraction call never has to hold the whole
document — short documents (the common case) pass through untouched.
"""
from app.tasks.extraction import _chunk_document, _split_sections


def test_short_document_is_not_chunked():
    md = "# 标题\n\n短文档内容。"
    assert _chunk_document(md, max_chars=1000) == [md]


def test_long_document_splits_at_section_headers():
    md = (
        "# 第一节\n\n" + "甲" * 40 + "\n\n"
        "# 第二节\n\n" + "乙" * 40 + "\n\n"
        "# 第三节\n\n" + "丙" * 40
    )
    chunks = _chunk_document(md, max_chars=60, overlap_chars=0)
    assert len(chunks) > 1
    assert "甲" * 40 in "".join(chunks)
    assert "乙" * 40 in "".join(chunks)
    assert "丙" * 40 in "".join(chunks)
    # each section stays whole within its chunk, never split mid-section
    for section in ("# 第一节", "# 第二节", "# 第三节"):
        assert sum(section in c for c in chunks) == 1


def test_chunk_boundary_repeats_overlap_from_previous_chunk():
    md = (
        "# 第一节\n\n" + "甲" * 40 + "\n\n"
        "# 第二节\n\n" + "乙" * 40
    )
    chunks = _chunk_document(md, max_chars=50, overlap_chars=10)
    assert len(chunks) == 2
    # the tail of chunk 1 reappears at the start of chunk 2
    assert chunks[0][-10:] in chunks[1]


def test_document_without_headers_falls_back_to_paragraph_splitting():
    md = "\n\n".join(f"段落{i}" * 20 for i in range(5))
    chunks = _chunk_document(md, max_chars=60, overlap_chars=0)
    assert len(chunks) > 1
    for i in range(5):
        assert sum(f"段落{i}" * 20 in c for c in chunks) == 1


def test_split_sections_single_section_when_no_headers_and_short():
    assert _split_sections("一段没有标题的短文本") == ["一段没有标题的短文本"]
