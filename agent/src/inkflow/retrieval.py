"""Authority-aware hybrid retrieval for a single compiled Context Packet."""
from __future__ import annotations

from collections import Counter
import json
import math
import re
from typing import Any

from .project import InkFlowProject
from .utils import content_hash


_MODEL_CACHE: dict[str, Any] = {}


def _terms(text: str) -> list[str]:
    normalized = text.casefold()
    terms = re.findall(r"[a-z0-9_.:-]{2,}", normalized)
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        terms.extend(run)
        terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return terms


class HybridRetriever:
    def __init__(
        self,
        project: InkFlowProject,
        *,
        embedding_model: str = "",
        reranker_model: str = "",
    ) -> None:
        self.project = project
        self.embedding_model = embedding_model.strip()
        self.reranker_model = reranker_model.strip()
        self.last_diagnostics: dict[str, Any] = {}

    def retrieve(
        self,
        query: str,
        *,
        role: str,
        chapter_no: int,
        chapter_version: int | None = None,
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        candidates = self._candidates(role=role, chapter_no=chapter_no, chapter_version=chapter_version)
        if not candidates:
            self.last_diagnostics = {"query": query, "candidate_count": 0, "selected": [], "discarded": []}
            return []
        factors = self._adaptive_factors(query, candidates, chapter_no)
        limit = top_k if top_k is not None else self._adaptive_top_k(query, len(candidates), factors)
        limit = max(3, min(int(limit), 32, len(candidates)))
        exact = self._exact_ranking(query, candidates)
        lexical = self._bm25_ranking(query, candidates)
        semantic = self._semantic_ranking(query, candidates) if self.embedding_model else []
        rankings = (("精确", exact), ("BM25", lexical), ("语义", semantic))
        feedback = self.project.db.retrieval_feedback_scores()
        scores: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        for label, ranking in rankings:
            for rank, (source_id, raw_score) in enumerate(ranking, 1):
                scores[source_id] = scores.get(source_id, 0.0) + 1.0 / (50 + rank) + min(0.01, raw_score * 0.001)
                reasons.setdefault(source_id, []).append(label)
        for candidate in candidates:
            source_id = candidate["source_id"]
            if source_id not in scores:
                continue
            scores[source_id] += feedback.get(source_id, 0.0)
            if candidate["authority_rank"] <= 3:
                scores[source_id] += 0.01
        by_id = {item["source_id"]: item for item in candidates}
        ranked_ids = sorted(
            scores,
            key=lambda source_id: (by_id[source_id]["authority_rank"], -scores[source_id], source_id),
        )
        selected = ranked_ids[:limit]
        selected = self._relation_expand(selected, candidates, limit=min(32, limit + 4))
        hits = []
        for source_id in selected:
            item = dict(by_id[source_id])
            item["score"] = round(scores.get(source_id, 0.0), 6)
            item["retrieval_reasons"] = reasons.get(source_id, ["关系扩展"])
            hits.append(item)
        if self.reranker_model and len(hits) > 1:
            hits = self._rerank(query, hits)
        result = hits[:limit]
        selected_ids = {item["source_id"] for item in result}
        self.last_diagnostics = {
            "query": query,
            "candidate_count": len(candidates),
            "initial_top_k": limit,
            "adaptive_factors": factors,
            "selected": [{"source_id": item["source_id"], "score": item["score"], "reasons": item["retrieval_reasons"]} for item in result],
            "discarded": [
                {"source_id": item["source_id"], "reason": "低于动态预算截断线" if item["source_id"] in scores else "未匹配查询"}
                for item in candidates if item["source_id"] not in selected_ids
            ][:80],
        }
        return result

    @staticmethod
    def _adaptive_top_k(query: str, corpus_size: int, factors: dict[str, int] | None = None) -> int:
        complexity = len(set(_terms(query)))
        values = factors or {}
        return max(4, min(40, 4 + int(math.log2(max(2, corpus_size))) + min(8, complexity // 6) + min(6, values.get("entity_count", 0) // 2) + min(6, values.get("open_thread_count", 0) // 2) + min(4, values.get("time_span", 0) // 20)))

    def _adaptive_factors(self, query: str, candidates: list[dict[str, Any]], chapter_no: int) -> dict[str, int]:
        query_entities = set(_entities(query))
        matched_entities = {entity for item in candidates for entity in item["entities"] if entity in query_entities}
        chapters = [int(item["chapter_no"]) for item in candidates if item.get("chapter_no") is not None]
        return {
            "query_terms": len(set(_terms(query))),
            "entity_count": len(matched_entities),
            "open_thread_count": sum(item["source_type"] == "canon_thread" for item in candidates),
            "time_span": chapter_no - min(chapters) if chapters else 0,
        }

    def _candidates(self, *, role: str, chapter_no: int, chapter_version: int | None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for fact in self.project.db.current_facts():
            if int(fact["valid_from_chapter"]) > chapter_no:
                continue
            items.append({
                "source_id": str(fact["fact_id"]), "source_type": "canon_fact",
                "title": f"{fact['subject']} / {fact['predicate']}",
                "body": json.dumps(fact["value"], ensure_ascii=False) + "\n" + str(fact.get("evidence") or ""),
                "chapter_no": int(fact["source_chapter"]), "version": None,
                "authority_rank": 2, "authority": "已验收正史", "entities": [str(fact["subject"])],
            })
        for thread in self.project.db.open_threads():
            if int(thread.get("last_advanced_chapter") or 0) > chapter_no:
                continue
            items.append({
                "source_id": str(thread["thread_id"]), "source_type": "canon_thread",
                "title": str(thread["title"]), "body": str(thread["description"]),
                "chapter_no": int(thread.get("last_advanced_chapter") or 0), "version": None,
                "authority_rank": 2, "authority": "已验收正史", "entities": _entities(str(thread["title"]) + str(thread["description"])),
            })
        for chapter in self.project.db.accepted_chapters():
            number = int(chapter["chapter_no"])
            if number >= chapter_no:
                continue
            body = str(chapter.get("summary") or "").strip()
            if body:
                items.append({
                    "source_id": f"chapter-summary:{number:05d}", "source_type": "chapter_summary",
                    "title": str(chapter["title"]), "body": body, "chapter_no": number,
                    "version": int(chapter["version"]), "authority_rank": 2,
                    "authority": "已验收正史", "entities": _entities(str(chapter["title"]) + body),
                })
        for preference in self.project.db.list_preferences():
            hard = preference["strength"] == "hard"
            items.append({
                "source_id": str(preference["preference_id"]), "source_type": "user_preference",
                "title": f"{preference['scope']} 用户偏好", "body": str(preference["text"]),
                "chapter_no": None, "version": None, "authority_rank": 3 if hard else 6,
                "authority": "用户长期硬规则" if hard else "用户弱偏好", "entities": _entities(str(preference["text"])),
            })
        for message in self.project.db.list_collaboration_messages(chapter_no=chapter_no, active_only=True, limit=100):
            if message["recipient_role"] != role:
                continue
            if chapter_version is not None and message.get("chapter_no") == chapter_no and message.get("chapter_version") not in {None, chapter_version}:
                continue
            body = str(message["claim"]) + "\n" + str(message["requested_response"])
            items.append({
                "source_id": str(message["message_id"]), "source_type": "work_decision",
                "title": f"{message['sender_role']} → {message['recipient_role']} / {message['message_type']}",
                "body": body, "chapter_no": message.get("chapter_no"), "version": message.get("chapter_version"),
                "authority_rank": 5, "authority": "当前版本协作记忆（非正史）", "entities": _entities(body),
            })
        return items

    @staticmethod
    def _exact_ranking(query: str, candidates: list[dict[str, Any]]) -> list[tuple[str, float]]:
        folded = query.casefold()
        result = []
        for item in candidates:
            score = 0.0
            if item["source_id"].casefold() in folded:
                score += 100.0
            if item["title"] and item["title"].casefold() in folded:
                score += 40.0
            score += sum(10.0 for entity in item["entities"] if entity and entity.casefold() in folded)
            if score:
                result.append((item["source_id"], score))
        return sorted(result, key=lambda value: (-value[1], value[0]))

    @staticmethod
    def _bm25_ranking(query: str, candidates: list[dict[str, Any]]) -> list[tuple[str, float]]:
        query_terms = _terms(query)
        documents = [_terms(item["title"] + "\n" + item["body"]) for item in candidates]
        if not query_terms or not any(documents):
            return []
        average = max(1.0, sum(map(len, documents)) / len(documents))
        frequency = Counter(term for document in documents for term in set(document))
        ranked = []
        for item, document in zip(candidates, documents, strict=True):
            counts, length, score = Counter(document), max(1, len(document)), 0.0
            for term in query_terms:
                count = counts.get(term, 0)
                if not count:
                    continue
                inverse = math.log(1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5))
                score += inverse * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * length / average))
            if score > 0:
                ranked.append((item["source_id"], score))
        return sorted(ranked, key=lambda value: (-value[1], value[0]))

    def _semantic_ranking(self, query: str, candidates: list[dict[str, Any]]) -> list[tuple[str, float]]:
        model = _cached_sentence_model(self.embedding_model)
        query_vector = model.encode([query], normalize_embeddings=True)[0]
        vectors = []
        missing_items = []
        missing_texts = []
        for item in candidates:
            text = item["title"] + "\n" + item["body"]
            source_hash = content_hash(text)
            cached = self.project.db.get_cached_embedding(item["source_id"], self.embedding_model, source_hash)
            if cached is None:
                vectors.append(None)
                missing_items.append((len(vectors) - 1, item, source_hash))
                missing_texts.append(text)
            else:
                vectors.append(cached)
        if missing_texts:
            encoded = model.encode(missing_texts, normalize_embeddings=True)
            for (index, item, source_hash), vector in zip(missing_items, encoded, strict=True):
                values = vector.tolist() if hasattr(vector, "tolist") else list(vector)
                vectors[index] = values
                self.project.db.cache_embedding(item["source_id"], self.embedding_model, source_hash, values)
        return sorted(
            [(item["source_id"], float(sum(float(a) * float(b) for a, b in zip(query_vector, vector, strict=True)))) for item, vector in zip(candidates, vectors, strict=True)],
            key=lambda value: (-value[1], value[0]),
        )

    def _rerank(self, query: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        model = _cached_cross_encoder(self.reranker_model)
        scores = model.predict([(query, item["title"] + "\n" + item["body"]) for item in hits])
        for item, score in zip(hits, scores, strict=True):
            item["reranker_score"] = float(score)
        return sorted(hits, key=lambda item: (item["authority_rank"], -item["reranker_score"], -item["score"]))

    @staticmethod
    def _relation_expand(selected: list[str], candidates: list[dict[str, Any]], *, limit: int) -> list[str]:
        by_id = {item["source_id"]: item for item in candidates}
        entities = {entity for source_id in selected for entity in by_id[source_id]["entities"]}
        result = list(selected)
        for item in candidates:
            if len(result) >= limit:
                break
            if item["source_id"] not in result and entities.intersection(item["entities"]):
                result.append(item["source_id"])
        return result


def _entities(text: str) -> list[str]:
    return sorted(set(re.findall(r"[\u3400-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_-]{2,}", text)))[:20]


def _cached_sentence_model(model_name: str):
    key = f"embedding:{model_name}"
    if key not in _MODEL_CACHE:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("启用 BGE-M3 语义召回前，请安装 `pip install -e .[rag]`。") from exc
        _MODEL_CACHE[key] = SentenceTransformer(model_name, trust_remote_code=True)
    return _MODEL_CACHE[key]


def _cached_cross_encoder(model_name: str):
    key = f"reranker:{model_name}"
    if key not in _MODEL_CACHE:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise RuntimeError("启用 BGE reranker 前，请安装 `pip install -e .[rag]`。") from exc
        _MODEL_CACHE[key] = CrossEncoder(model_name, trust_remote_code=True)
    return _MODEL_CACHE[key]
