"""Explicit offline check for optional retrieval fallback and source boundaries."""
from types import SimpleNamespace
from unittest.mock import patch

from inkflow.retrieval import HybridRetriever, _paragraph_excerpt


def check():
    body = "第一段。\n" + "中间内容。" * 900 + "\n最后一段。"
    excerpt, start, end = _paragraph_excerpt(body, 8)
    assert excerpt == body[start:end] and len(excerpt) <= 5000
    rows = [{"source_id": "a", "source_type": "canon_fact", "title": "钥匙", "body": "黄铜钥匙在老陈手里", "category": "facts",
             "identity": "accepted_fact", "memory_layer": "canon", "authority_rank": 2, "entities": ["老陈"], "chapter_no": 1, "version": 1}]
    retriever = object.__new__(HybridRetriever)
    retriever.project = SimpleNamespace(db=SimpleNamespace(get_brief=lambda: SimpleNamespace(genre="现实")))
    retriever.embedding_model = "missing-local-model"
    retriever.reranker_model = ""
    retriever.source_gaps = []
    with patch.object(retriever, "_candidates", return_value=rows), patch.object(retriever, "_preference_items", return_value=[]), patch.object(retriever, "_semantic_ranking", side_effect=RuntimeError("local model unavailable")):
        result = retriever.retrieve("黄铜钥匙", role="writer", chapter_no=2, top_k=1)
    assert result and result[0]["source_id"] == "a"
    assert retriever.last_diagnostics["optional_failures"][0]["fallback"] == "exact_and_bm25"
    linked = [rows[0], {**rows[0], "source_id": "b", "source_type": "chapter_summary", "entities": [], "version": 1},
              {**rows[0], "source_id": "c", "source_type": "chapter_summary", "entities": [], "version": 2}]
    assert HybridRetriever._relation_expand(["a", "c"], linked, limit=2) == ["a", "b"]


if __name__ == "__main__":
    check()
