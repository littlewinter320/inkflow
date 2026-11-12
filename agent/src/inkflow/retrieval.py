"""Authority-aware hybrid retrieval for a single compiled Context Packet."""
from __future__ import annotations

from math import isfinite

from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
import json
import math
import re
from typing import Any

from .project import InkFlowProject
from .retrieval_lifecycle import ambiguity_groups, consolidate_candidates, preserve_alternatives
from .utils import content_hash, estimate_tokens


_MODEL_CACHE: dict[str, Any] = {}
# Keep a small process-local vector cache in front of SQLite. ContextBuilder is
# intentionally short lived, so without this layer repeated Writer/Reviewer
# calls re-read and JSON-decode the same embeddings even when the source hash
# is unchanged. The cache only stores derived vectors; the database remains
# the durable source of truth and a changed source hash always misses safely.
_VECTOR_CACHE: dict[tuple[str, str, str], list[float]] = {}
_VECTOR_CACHE_LIMIT = 512


def load_reference_cards(project: InkFlowProject, limit: int | None = None) -> list[dict[str, Any]]:
    """Read reusable expression references; source samples never enter model memory."""
    paths = sorted((project.internal / "references" / "features").glob("*.json"),
                   key=lambda item: item.stat().st_mtime, reverse=True)
    cards = []
    for path in paths if limit is None else paths[:limit]:
        try:
            text = path.read_text(encoding="utf-8")
            item = json.loads(text)
            if not isinstance(item, dict):
                continue
            item.pop("sample_chapter_endings", None)
            item.pop("samples", None)
            cards.append({**item, "authority": "reference_only", "source_path": str(path.relative_to(project.root)),
                          "source_hash": content_hash(text), "reference_id": item.get("reference_id") or path.stem})
        except (OSError, ValueError):
            continue
    return cards


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
        self.source_gaps: list[dict[str, Any]] = []
        self._embedding_cache_hits = 0
        self._embedding_cache_misses = 0
        self._knowledge_snapshot: dict[str, Any] = {}

    def retrieve(
        self,
        query: str,
        *,
        role: str,
        chapter_no: int,
        chapter_version: int | None = None,
        top_k: int | None = None,
        quality_preset: str | None = None,
        context_token_budget: int | None = None,
        foreshadow_query: str = "",
    ) -> list[dict[str, Any]]:
        """按权限与相关性召回；新任务按可用窗口选材，旧快照保留原数量策略。"""
        if context_token_budget is not None and (type(context_token_budget) is not int or context_token_budget < 1):
            raise ValueError("检索资料预算必须是正整数。")
        self._knowledge_query = query
        candidates = self._candidates(role=role, chapter_no=chapter_no, chapter_version=chapter_version)
        corpus_candidate_count = len(candidates)
        from .preferences import applicable_preferences
        preferences = applicable_preferences(self._preference_items(), query,
            {"chapter_no": chapter_no, "genre": self.project.db.get_brief().genre})
        allowed_preferences = {item["preference_id"] for item in preferences}
        candidates = [item for item in candidates if item.get("source_type") != "user_preference"
                       or item["source_id"] in allowed_preferences]
        candidates, overlaps, conflicts = self._merge_candidates(candidates)
        candidates, duplicates = consolidate_candidates(candidates)
        ambiguities = ambiguity_groups(candidates)
        fingerprints = {item["source_id"]: item["source_fingerprint"] for item in candidates}
        maintenance = self.project.db.maintain_retrieval_cache(
            {item["source_id"]: content_hash(item["title"] + "\n" + item["body"]) for item in candidates},
            revision=content_hash(json.dumps(fingerprints, sort_keys=True)))
        exact, lexical, branches = self._local_branches(query, candidates)
        self._embedding_cache_hits = 0
        self._embedding_cache_misses = 0
        if not candidates:
            self.last_diagnostics = {"query": query, "query_hash": content_hash(query), "candidate_count": 0, "selected": [], "discarded": [],
                                     "source_gaps": self.source_gaps, "branches": branches,
                                     "overlaps": overlaps, "source_conflicts": conflicts,
                                     "duplicates": duplicates, "ambiguities": ambiguities, "maintenance": maintenance}
            if quality_preset is not None:
                self.last_diagnostics.update(corpus_candidate_count=corpus_candidate_count, relevant_candidate_count=0,
                    retrieved_count=0, coverage_policy={"policy_version": 2, "quality_preset": quality_preset,
                        "relevant_candidate_count": 0, "target_count": 0, "target_fraction": 0,
                        "denominator": "matched_source_valid_candidates"})
            return []
        factors = self._adaptive_factors(query, candidates, chapter_no)
        limit = top_k if top_k is not None else self._adaptive_top_k(query, len(candidates), factors)
        limit = max(0, min(int(limit), 32, len(candidates)))
        optional_failures = []
        semantic = []
        if self.embedding_model:
            try:
                semantic = self._semantic_ranking(query, candidates)
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                optional_failures.append({"channel": "semantic", "error_type": type(exc).__name__,
                    "reason": str(exc), "fallback": "exact_and_bm25"})
        # 关系与全文属于主召回；向量只补充相似表达，不能挤掉同权威的主召回证据。
        graph_ids = self._knowledge_related_ids(query)
        graph = [(item["source_id"], 1.0) for item in candidates if item["source_id"] in graph_ids]
        primary_ids = {identity for identity, _ in exact + lexical + graph}
        rankings = (("关系", graph), ("精确", exact), ("BM25", lexical), ("语义辅助", semantic))
        scores: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        for label, ranking in rankings:
            for rank, (source_id, raw_score) in enumerate(ranking, 1):
                if not isfinite(raw_score) or raw_score <= 0:
                    continue
                # RRF fuses rank positions; BM25 and cosine raw scores have different scales.
                scores[source_id] = scores.get(source_id, 0.0) + (0.2 if label == "语义辅助" else 1.0) / (50 + rank)
                reasons.setdefault(source_id, []).append(label)
        for candidate in candidates:
            source_id = candidate["source_id"]
            if source_id not in scores:
                continue
            # Unscoped historical citations cannot establish relevance or source quality for this request.
            if candidate["authority_rank"] <= 3:
                scores[source_id] += 0.01
        if foreshadow_query.strip():
            # 当前卡的推进/兑现目标单独召回，避免被长正文查询稀释；不扩大数量或越过来源核验。
            canon = [item for item in candidates if item["source_type"] in {
                "canon_thread", "canon_fact", "chapter_summary", "canon_source"}]
            focus_exact = self._exact_ranking(foreshadow_query, canon)
            focus_lexical = self._bm25_ranking(foreshadow_query, canon)
            for ranking in (focus_exact, focus_lexical):
                for rank, (source_id, raw_score) in enumerate(ranking, 1):
                    if not isfinite(raw_score) or raw_score <= 0:
                        continue
                    scores[source_id] = scores.get(source_id, 0.0) + 2.0 / (50 + rank)
                    if "本章伏笔目标：双倍排序权重" not in reasons.setdefault(source_id, []):
                        reasons[source_id].append("本章伏笔目标：双倍排序权重")
        try:
            feedback = self.project.db.retrieval_feedback_scores(query=query, role=role, chapter_no=chapter_no,
                                                               fingerprints=fingerprints)
        except (ValueError, TypeError) as exc:
            feedback = {}
            optional_failures.append({"channel": "feedback", "error_type": type(exc).__name__,
                "reason": "反馈绑定记录无法核验，未采用其排序影响。", "fallback": "unadjusted_rrf"})
        for source_id, adjustment in feedback.items():
            if source_id in scores:
                scores[source_id] += adjustment
        by_id = {item["source_id"]: item for item in candidates}
        ranked_ids = sorted(
            scores,
            key=lambda source_id: (by_id[source_id]["authority_rank"], source_id not in primary_ids, -scores[source_id], source_id),
        )
        verified_sources: dict[int, dict[str, Any]] = {}
        candidate_source_reads = []
        invalid_source_ids = set()

        def verified_source(hit: dict[str, Any]) -> dict[str, Any]:
            """新策略先本地核验匹配来源，按章复用已读正文，不靠候选数猜有效比例。"""
            if (type(hit.get("chapter_no")) is not int or hit["chapter_no"] < 1
                    or type(hit.get("version")) is not int or hit["version"] < 1 or not hit.get("source_hash")):
                raise ValueError("正史候选缺少可靠章节、版本或来源哈希，不能计作有效证据。")
            number = int(hit["chapter_no"])
            if number not in verified_sources:
                verified_sources[number] = self._accepted_source(number, before_chapter=chapter_no)
            source = verified_sources[number]
            if ((hit.get("version") is not None and source["version"] != hit["version"])
                    or (hit.get("source_hash") and source["content_hash"] != hit["source_hash"])):
                raise ValueError("命中后正文来源版本或哈希已变化。")
            if hit["source_type"] == "canon_fact" and (not str(hit.get("evidence") or "").strip()
                    or hit["evidence"] not in source["content"]):
                raise ValueError("事实主证据未在当前已接受原文定位。")
            return source

        coverage = {}
        if quality_preset is not None and top_k is None:
            # 分母排除已经证实失效的相关来源；未取回的原文核验也只是本地工作，并非模型已读。
            for source_id in ranked_ids:
                item = by_id[source_id]
                if item["source_type"] not in {"chapter_summary", "canon_fact", "canon_thread", "canon_source"}:
                    continue
                try:
                    verified_source(item)
                except (ValueError, OSError, UnicodeError) as exc:
                    invalid_source_ids.add(source_id)
                    self.source_gaps.append({"kind": item["source_type"], "chapter_no": item["chapter_no"],
                        "version": item.get("version"), "source_hash": item.get("source_hash"),
                        "state": "source_unavailable", "owner": "memory_owner", "reason": str(exc)})
                    candidate_source_reads.append({"source_id": source_id, "state": "failed", "reason": str(exc)})
            ranked_ids = [identity for identity in ranked_ids if identity not in invalid_source_ids]
            # 比例针对有匹配信号的有效候选；零匹配资料不因凑比例被强行装入。
            relevant_count = len(ranked_ids)
            base = self._adaptive_top_k(query, relevant_count, factors)
            economy_ratio = min(0.5, 0.3 + max(0, base - 20) / 100)
            economy_target = math.ceil(relevant_count * economy_ratio)
            balanced_target = min(relevant_count, economy_target * 2)
            limit = economy_target if quality_preset == "economy" else balanced_target
            if quality_preset == "quality":
                limit = max(balanced_target, math.ceil(relevant_count * 0.5))
            limit = min(limit, relevant_count)
            if context_token_budget is not None:
                # 不再按书库大小填充上下文；复杂任务可以多取，但不会随全书条目无界增长。
                tier_limit = {"economy": 12, "balanced": 20, "quality": 28}.get(quality_preset, 20)
                limit = min(relevant_count, max(4, min(tier_limit, base)),
                            max(1, context_token_budget // 1800))
            coverage = {"policy_version": 2, "quality_preset": quality_preset,
                "relevant_candidate_count": relevant_count, "target_count": limit,
                "target_fraction": limit / relevant_count if relevant_count else 0,
                "denominator": "matched_source_valid_candidates", "economy_target_count": economy_target,
                "source_validation_failed_count": len(invalid_source_ids),
                "meaning": "先按适用范围、去重、匹配信号及本地正文来源核验计目标；本地核验不等于已向模型装入。"}
            if context_token_budget is not None:
                coverage.update(policy_version=3, context_token_budget=context_token_budget,
                    meaning="按任务相关性及角色窗口选择背景资料，不以全书比例或填满容量为目标；未选来源仍可定点回查。")
        selected = ranked_ids[:limit]
        initial_ids = set(selected)
        # 新策略扩展与比例使用同一已匹配、已核源池，不能把分母外的零分资料补进分子。
        relevant_ids = set(ranked_ids)
        relation_candidates = [item for item in candidates if item["source_id"] in relevant_ids] if coverage else candidates
        selected = self._relation_expand(selected, relation_candidates, limit=limit)
        comparison_groups = [{**group, "source_ids": [identity for identity in group["source_ids"]
            if identity in relevant_ids]} for group in ambiguities] if coverage else ambiguities
        selected = preserve_alternatives(selected, comparison_groups, limit=limit)
        relation_ids = set(selected) - initial_ids
        hits = []
        for source_id in selected:
            item = dict(by_id[source_id])
            item["semantic_only"] = source_id not in primary_ids
            item["score"] = round(scores.get(source_id, 0.0), 6)
            item["retrieval_reasons"] = (["关系扩展：在最终数量预算内替换低相关项"]
                if source_id in relation_ids else reasons.get(source_id, ["关系扩展"]))
            if source_id in feedback and feedback[source_id]:
                item["retrieval_reasons"].append("同查询、同角色、同来源版本的有界反馈排序")
            if any(source_id in group["source_ids"] for group in ambiguities):
                item["retrieval_status"] = "needs_comparison"
                item["retrieval_reasons"].append("存在不同内容、认识类型或时点，须结合原文对照")
            hits.append(item)
        if self.reranker_model and len(hits) > 1:
            try:
                hits = self._rerank(query, hits)
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                optional_failures.append({"channel": "reranker", "error_type": type(exc).__name__,
                    "reason": str(exc), "fallback": "rrf"})
        # Backfill rejected derived summaries with already ranked, unselected sources.
        # 扩大召回后可能有多条旧来源失效；按原排序补足，实际读取在达到目标时即停止。
        backfill = [source_id for source_id in ranked_ids if source_id not in selected] if limit else []
        if not coverage:
            backfill = backfill[:6]
        hits.extend({**by_id[source_id], "score": round(scores[source_id], 6),
            "retrieval_reasons": reasons[source_id], "backfilled": True} for source_id in backfill)
        result = hits
        source_reads = candidate_source_reads
        validated = []
        for hit in result:
            if len(validated) >= limit:
                break
            if hit["source_type"] not in {"chapter_summary", "canon_fact", "canon_thread", "canon_source"}:
                validated.append(hit)
                continue
            try:
                source = verified_source(hit) if coverage else self._accepted_source(int(hit["chapter_no"]), before_chapter=chapter_no)
                if ((hit.get("version") is not None and source["version"] != hit["version"])
                        or (hit.get("source_hash") and source["content_hash"] != hit["source_hash"])):
                    raise ValueError("命中后正文来源版本或哈希已变化。")
                if hit["source_type"] == "canon_fact" and (not str(hit.get("evidence") or "").strip()
                        or hit["evidence"] not in source["content"]):
                    raise ValueError("事实主证据未在当前已接受原文定位。")
                if hit["source_type"] != "chapter_summary":
                    validated.append(hit)
                    source_reads.append({"source_id": hit["source_id"], "chapter_no": hit["chapter_no"],
                        "version": source["version"], "source_hash": source["content_hash"],
                        "state": "source_validated", "via": hit["source_type"]})
                    continue
                text = source["content"]
                excerpt_size = (max(800, min(2400, context_token_budget // max(1, limit)))
                                if context_token_budget is not None else 5000)
                spans = [{"source_id": str(offset), "title": hit["title"], "body": text[offset:offset + excerpt_size]}
                         for offset in range(0, len(text), min(1000, excerpt_size // 2))]
                ranked_spans = self._bm25_ranking(query, spans)
                offset = int(ranked_spans[0][0]) if ranked_spans else 0
                excerpt, offset, end = _paragraph_excerpt(text, offset, max_chars=excerpt_size)
                hit = {**hit, "summary_body": hit["body"], "body": excerpt,
                    "summary_content_hash": hit.get("content_hash"), "content_hash": content_hash(excerpt),
                    "identity": "accepted_source_excerpt", "body_kind": "accepted_prose",
                    "authority": "已接受正文定点回查；摘要仅作定位线索",
                    "resolved_source": {"source_id": f"chapter:{hit['chapter_no']:05d}",
                        "chapter_no": hit["chapter_no"], "version": source["version"],
                        "source_hash": source["content_hash"], "start": offset, "end": end}}
                source_reads.append({**hit["resolved_source"], "state": "read", "via": "chapter_summary"})
                validated.append(hit)
            except (ValueError, OSError, UnicodeError) as exc:
                self.source_gaps.append({"kind": hit["source_type"], "chapter_no": hit["chapter_no"],
                    "version": hit.get("version"), "source_hash": hit.get("source_hash"),
                    "state": "source_unavailable", "owner": "memory_owner", "reason": str(exc)})
                source_reads.append({"source_id": hit["source_id"], "state": "failed", "reason": str(exc)})
        result = validated
        selected_ids = {item["source_id"] for item in result}
        self.last_diagnostics = {
            "source_gaps": self.source_gaps,
            "branches": branches, "overlaps": overlaps, "source_conflicts": conflicts,
            "source_reads": source_reads,
            "optional_failures": optional_failures,
            "query_hash": content_hash(query), "duplicates": duplicates,
            "ambiguities": [{**group, "selected_source_ids": [identity for identity in group["source_ids"] if identity in selected_ids],
                "omitted_count": sum(identity not in selected_ids for identity in group["source_ids"])} for group in ambiguities][:16],
            "ambiguity_group_count": len(ambiguities), "maintenance": maintenance,
            "feedback_adjustments": {identity: value for identity, value in feedback.items() if identity in selected_ids},
            "backfilled_source_ids": [item["source_id"] for item in result if item.get("backfilled")],
            "relation_expansion": {"included": sorted(relation_ids), "replaced": sorted(initial_ids - set(selected)),
                                   "budget": limit},
            "query": query,
            "candidate_count": len(candidates),
            **({"coverage_policy": {**coverage,
                "retrieved_fraction": len(result) / coverage["relevant_candidate_count"] if coverage["relevant_candidate_count"] else 0},
                "corpus_candidate_count": corpus_candidate_count,
                "relevant_candidate_count": coverage["relevant_candidate_count"]} if coverage else {}),
            "initial_top_k": limit,
            "retrieved_count": len(result),
            "not_retrieved_count": len(candidates) - len(result),
            "adaptive_factors": factors,
            "selected": [{"source_id": item["source_id"], "score": item["score"], "reasons": item["retrieval_reasons"],
                "source_fingerprint": item["source_fingerprint"]} for item in result],
            "discarded": [
                {"source_id": item["source_id"], "title": item.get("title", "相关资料"),
                 "source_type": item["source_type"], "chapter_no": item.get("chapter_no"),
                 "reason": "来源章节、版本、哈希或正文证据核验未通过，未作为有效证据取回。"
                    if item["source_id"] in invalid_source_ids else
                    "本次相关性排序和资料容量下未选中" if item["source_id"] in scores else "没有匹配本次查找内容"}
                for item in candidates if item["source_id"] not in selected_ids
            ][:80],
            "discarded_display_limit": 80,
        }
        if self.embedding_model:
            self.last_diagnostics["embedding_cache_hits"] = self._embedding_cache_hits
            self.last_diagnostics["embedding_cache_misses"] = self._embedding_cache_misses
        return result

    def narrative_context(self, query: str, *, chapter_no: int, token_limit: int,
                          seed_chapters: list[int] | None = None, source_limit: int = 6,
                          provisional_chapters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Compare versioned parent scenes locally, with explicit history and budget gaps."""
        from .narrative_memory import event_history, event_patch_gaps, scene_span
        # shortcut: scan and validate local history on demand; cache by source versions if large-book latency warrants it.
        rows = self.project.db.narrative_history_rows(chapter_no)
        history = event_history(rows)
        events = history["events"]
        temporary_gaps, temporary_sources = [], []
        batch_ids = {str(item.get("batch_id") or "") for item in provisional_chapters or []}
        temporary_counts = Counter(int(item["chapter_no"]) for item in provisional_chapters or [])
        for item in sorted(provisional_chapters or [], key=lambda value: int(value["chapter_no"])):
            number = int(item["chapter_no"])
            if number >= chapter_no:
                continue
            batch_id = str(item.get("batch_id") or "")
            current = self.project.db.get_chapter(number)
            if current and current["status"] == "accepted":
                continue
            try:
                if len(batch_ids) != 1 or not batch_id:
                    raise ValueError("临时对照必须明确绑定同一个原批次")
                if temporary_counts[number] != 1:
                    raise ValueError("临时对照章节重复，未猜测采用哪份输入")
                staged = self.project.db.get_provisional_memory_patch(batch_id, number)
                text = str(item.get("content") or "")
                if (not staged or staged["status"] != "active" or not current or current["status"] != "draft"
                        or current["version"] != staged["chapter_version"] or current["content_hash"] != staged["content_hash"]
                        or content_hash(text) != staged["content_hash"]):
                    raise ValueError("临时事件的当前草稿或原批次暂存版本已变化")
                path = (self.project.root / current["path"]).resolve()
                if not path.is_relative_to(self.project.root.resolve()) or content_hash(path.read_text(encoding="utf-8")) != staged["content_hash"]:
                    raise ValueError("临时事件的草稿文件来源已变化或越出项目")
                patch = staged["patch"]
                if patch.chapter_no != number:
                    raise ValueError("临时事件的章节身份不一致")
                gaps = event_patch_gaps(patch, text, prior_events=history["events"], sources=history["sources"],
                                       source_bindings=history["source_bindings"])
                if gaps:
                    raise ValueError("；".join(gaps))
                temporary_sources.append({"chapter_no": number, "version": current["version"],
                    "content_hash": staged["content_hash"], "text": text, "batch_id": batch_id})
                if not patch.narrative_events:
                    temporary_gaps.append({"chapter_no": number, "reason": "同批次草稿尚无事件索引，仅提供已核原文"})
                for event in patch.narrative_events:
                    value = event.model_dump(mode="json")
                    start, end = scene_span(text, value)
                    events.append({**value, "chapter_no": number, "source_version": current["version"],
                        "source_hash": staged["content_hash"], "start": start, "end": end,
                        "source_id": f"batch-event:{batch_id}:{number:05d}:{event.event_id}",
                        "source_status": "provisional", "batch_id": batch_id, "index_origin": "reviewed_batch_memory"})
            except (ValueError, OSError, UnicodeError) as exc:
                temporary_gaps.append({"chapter_no": number, "reason": str(exc)})
        by_chapter: dict[int, list[dict[str, Any]]] = {}
        for event in events:
            by_chapter.setdefault(event["chapter_no"], []).append(event)
        candidates, unavailable = [], []
        for row in rows:
            number = int(row["chapter_no"])
            text = history["sources"].get(number)
            if text is None:
                try:
                    source = self._accepted_source(number, before_chapter=chapter_no)
                    if source["version"] != row["version"] or source["content_hash"] != row["content_hash"]:
                        raise ValueError("对照来源版本已变化")
                    text = source["content"]
                except (ValueError, OSError, UnicodeError) as exc:
                    unavailable.append({"chapter_no": number, "reason": str(exc)})
                    continue
            chapter_events = by_chapter.get(number, [])
            spans = {(item["start"], item["end"]) for item in chapter_events} or {(0, len(text))}
            for start, end in sorted(spans):
                related = [item for item in chapter_events if item["start"] == start and item["end"] == end]
                candidates.append({"source_id": f"chapter:{number:05d}:scene:{start}:{end}",
                    "chapter_source_id": f"chapter:{number:05d}", "source_status": "accepted",
                    "chapter_no": number, "version": int(row["version"]), "source_hash": row["content_hash"],
                    "start": start, "end": end, "body": text[start:end],
                    "title": " ".join(item["summary"] + " " + " ".join(item["participants"]) for item in related),
                    "scope": "indexed_scene" if related else "whole_chapter_fallback",
                    "event_ids": [item["event_id"] for item in related]})
        for source in temporary_sources:
            number, text, batch_id = source["chapter_no"], source["text"], source["batch_id"]
            chapter_events = by_chapter.get(number, [])
            for start, end in sorted({(item["start"], item["end"]) for item in chapter_events} or {(0, len(text))}):
                related = [item for item in chapter_events if item["start"] == start and item["end"] == end]
                candidates.append({"source_id": f"batch:{batch_id}:chapter:{number:05d}:scene:{start}:{end}",
                    "chapter_source_id": f"batch:{batch_id}:chapter:{number:05d}", "source_status": "provisional",
                    "chapter_no": number, "version": source["version"], "source_hash": source["content_hash"],
                    "start": start, "end": end, "body": text[start:end], "batch_id": batch_id,
                    "title": " ".join(item["summary"] for item in related),
                    "scope": "indexed_scene" if related else "whole_chapter_fallback",
                    "event_ids": [item["event_id"] for item in related]})
        # Keep the full query; bounded extra views stop one prominent action from monopolizing recall.
        queries = list(dict.fromkeys([query, *[part.strip() for part in re.split(r"[\n。；]", query)
                                               if len(part.strip()) >= 4][:4]]))
        scores: dict[str, float] = {}
        for view in queries:
            for rank, (identity, _) in enumerate(self._bm25_ranking(view, candidates), 1):
                scores[identity] = scores.get(identity, 0.0) + 1 / (50 + rank)
        seeds = set(seed_chapters or [])
        for item in candidates:
            if item["chapter_no"] in seeds:
                scores[item["source_id"]] = scores.get(item["source_id"], 0.0) + 1 / 100
        ranked = sorted((item for item in candidates if item["source_id"] in scores),
            key=lambda item: (-scores[item["source_id"]], item["chapter_no"], item["start"]))
        event_map = {(item["chapter_no"], item["event_id"]): item for item in events}
        neighbors: dict[tuple, set[tuple]] = {}
        for key, event in event_map.items():
            for link in event["relations"]:
                target = (link["target_chapter"], link["target_event_id"])
                if target in event_map:
                    neighbors.setdefault(key, set()).add(target)
                    neighbors.setdefault(target, set()).add(key)
        roots = {(item["chapter_no"], event_id) for item in ranked[:6] for event_id in item["event_ids"]}
        connected, frontier = set(roots), deque(sorted(roots))
        chain_order = []
        while frontier:
            key = frontier.popleft()
            chain_order.append(key)
            for target in sorted(neighbors.get(key, ())):
                if target not in connected:
                    connected.add(target)
                    frontier.append(target)
        # Read linked prerequisites even when their wording does not match the current query.
        for item in candidates:
            keys = {(item["chapter_no"], identity) for identity in item["event_ids"]}
            if keys & connected and item["source_id"] not in scores:
                ranked.append(item)
        first_roots = {item["source_id"] for item in ranked[:2]}
        ranked.sort(key=lambda item: (0 if item["source_id"] in first_roots else 1 if
            {(item["chapter_no"], identity) for identity in item["event_ids"]} & (connected - roots) else 2,
            -scores.get(item["source_id"], 0), item["chapter_no"], item["start"]))
        result: dict[str, Any] = {"sources": [], "timeline": [], "continuity_sources": [], "expression_sources": [], "coverage": {
            **history["coverage"], "readable_chapters": len(rows) - len(unavailable),
            "candidate_scenes": len(candidates), "matched_scenes": len(ranked),
            "history_gaps": history["gaps"][:32], "history_gap_count": len(history["gaps"]),
            "provisional_chapters": len(temporary_sources), "provisional_gaps": temporary_gaps[:16],
            "provisional_gap_count": len(temporary_gaps),
            "unavailable_sources": unavailable[:16], "unavailable_source_count": len(unavailable),
            "omitted_sources": [], "timeline_events_omitted": 0,
            "meaning": "全量本地候选扫描不等于全书事件已提取或模型已读完。结果按相关性与预算选择；保留未知，不证明没有矛盾。"}}
        result["coverage"]["indexed_chapters_all_present"] = result["coverage"].pop("complete")
        result["coverage"]["timeline_order"] = "source_order_only_not_story_time"
        selected_chapters = set()
        omitted = []
        # Reserve different chapters first; never discard a conflicting scene by semantic deduplication.
        ordered = []
        for item in ranked:
            if item["chapter_no"] not in selected_chapters:
                ordered.append(item)
                selected_chapters.add(item["chapter_no"])
        first_ids = {item["source_id"] for item in ordered}
        ordered.extend(item for item in ranked if item["source_id"] not in first_ids)
        for item in ordered:
            if len(result["sources"]) >= source_limit:
                omitted.append({"source_id": item["source_id"], "reason": "本轮共享补读来源上限"})
                continue
            source = {**item, "use": "continuity_and_expression",
                      "authority": "同批次已审核临时原文；未接受，不是正史" if item["source_status"] == "provisional"
                          else "已接受同版原文；相似不等于矛盾，也不要求改写"}
            trial = {**result, "sources": [*result["sources"], source]}
            if estimate_tokens(json.dumps(trial, ensure_ascii=False)) > max(0, token_limit * 3 // 4):
                omitted.append({"source_id": item["source_id"], "reason": "完整场景或整章超过剩余对照预算，未截断冒充全文"})
                continue
            result["sources"].append(source)
        chosen_events = [(item["chapter_no"], identity) for item in result["sources"] for identity in item["event_ids"]]
        remaining_events = [(item["chapter_no"], item["event_id"]) for item in reversed(events)
                            if item["chapter_no"] in seeds or any(name.casefold() in query.casefold() for name in item["participants"])]
        event_order = list(dict.fromkeys([*chosen_events, *chain_order, *remaining_events]))
        timeline_tokens = estimate_tokens(json.dumps(result, ensure_ascii=False))
        for key in event_order:
            event = event_map[key]
            event_tokens = estimate_tokens(json.dumps(event, ensure_ascii=False)) + 1
            if timeline_tokens + event_tokens > max(0, token_limit - 512):
                result["coverage"]["timeline_events_omitted"] += 1
                continue
            result["timeline"].append(event)
            timeline_tokens += event_tokens
        result["timeline"].sort(key=lambda item: (item["chapter_no"], item["start"], item["event_id"]))
        # 保留原截取范围，仅稳定诊断清单顺序，避免同一未读集合扰动恢复身份及缓存。
        result["coverage"]["omitted_sources"] = sorted(omitted[:16], key=lambda item: (item["source_id"], item["reason"]))
        result["coverage"]["omitted_source_count"] = len(omitted)
        result["coverage"]["selected_scenes"] = len(result["sources"])
        result["coverage"]["selected_timeline_events"] = len(result["timeline"])
        unread = [item["source_id"] for item in result["timeline"] if not any(
            source["chapter_no"] == item["chapter_no"] and source["start"] <= item["start"]
            and source["end"] >= item["end"] for source in result["sources"])]
        result["coverage"]["timeline_sources_not_in_comparison"] = unread[:16]
        result["coverage"]["timeline_sources_not_in_comparison_count"] = len(unread)
        result["continuity_sources"] = [item["source_id"] for item in result["sources"]]
        represented = set()
        for item in result["sources"]:
            if item["chapter_no"] not in represented:
                result["expression_sources"].append(item["source_id"])
                represented.add(item["chapter_no"])
        # Metadata also shares the cap; retain counts if detailed gap lists are too large.
        for field in ("history_gaps", "unavailable_sources", "provisional_gaps", "omitted_sources", "timeline_sources_not_in_comparison"):
            if estimate_tokens(json.dumps(result, ensure_ascii=False)) > max(0, token_limit):
                result["coverage"][field] = []
        while estimate_tokens(json.dumps(result, ensure_ascii=False)) > max(0, token_limit) and result["timeline"]:
            result["timeline"].pop()
            result["coverage"]["timeline_events_omitted"] += 1
        while estimate_tokens(json.dumps(result, ensure_ascii=False)) > max(0, token_limit) and result["sources"]:
            result["sources"].pop()
            result["coverage"]["omitted_source_count"] += 1
            result["continuity_sources"] = [item["source_id"] for item in result["sources"]]
            result["expression_sources"] = [identity for identity in result["expression_sources"] if identity in result["continuity_sources"]]
        result["coverage"]["selected_scenes"] = len(result["sources"])
        result["coverage"]["selected_timeline_events"] = len(result["timeline"])
        unread = [item["source_id"] for item in result["timeline"] if not any(
            source["chapter_no"] == item["chapter_no"] and source["start"] <= item["start"]
            and source["end"] >= item["end"] for source in result["sources"])]
        result["coverage"]["timeline_sources_not_in_comparison_count"] = len(unread)
        if result["coverage"]["timeline_sources_not_in_comparison"]:
            result["coverage"]["timeline_sources_not_in_comparison"] = unread[:16]
        if estimate_tokens(json.dumps(result, ensure_ascii=False)) > max(0, token_limit):
            return {"sources": [], "timeline": [], "continuity_sources": [], "expression_sources": [],
                "coverage": {"reason": "本轮容量不足以装入事件对照及覆盖说明，未执行完整对照"}}
        return result

    def _accepted_source(self, chapter_no: int, *, before_chapter: int) -> dict[str, Any]:
        row = self.project.db.get_chapter(chapter_no)
        if chapter_no >= before_chapter or not row or row["status"] != "accepted":
            raise ValueError("来源不属于本次章节边界内的已接受正文。")
        text = self.project.db.canonical_chapter_content(chapter_no)
        if text is None:
            path = (self.project.root / row["path"]).resolve()
            text = path.read_text(encoding="utf-8") if path.is_relative_to(self.project.root.resolve()) else ""
        if not text or content_hash(text) != row["content_hash"]:
            raise ValueError("已接受原文缺失或哈希不匹配，不能把摘要当作原文依据。")
        return {**row, "content": text}

    def _preference_items(self) -> list[dict[str, Any]]:
        from .preferences import apply_preference_decisions, preference_candidates
        return apply_preference_decisions(self.project.db.effective_preferences() + preference_candidates(self.project.db))

    @staticmethod
    def _adaptive_top_k(query: str, corpus_size: int, factors: dict[str, int] | None = None) -> int:
        complexity = len(set(_terms(query)))
        values = factors or {}
        return max(4, min(40, 4 + int(math.log2(max(2, corpus_size))) + min(8, complexity // 6) + min(6, values.get("entity_count", 0) // 2) + min(6, values.get("open_thread_count", 0) // 2) + min(4, values.get("time_span", 0) // 20)))

    def recover_review_sources(self, queries: list[str], *, chapter_no: int, role: str = "reviewer") -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Search canon itself as well as its indexes; hits are leads, never proof.

        # ponytail: lexical scan is linear in accepted text; reuse a versioned
        # paragraph index only if large-book recovery becomes a measured bottleneck.
        """
        queries = list(dict.fromkeys(value.strip()[:160] for value in queries if value.strip()))[:4]
        chapters: dict[int, dict[str, Any]] = {}
        candidates: list[dict[str, Any]] = []
        skipped: list[int] = []
        for row in self.project.db.accepted_chapters():
            number = int(row["chapter_no"])
            if number >= chapter_no:
                continue
            try:
                source = self._accepted_source(number, before_chapter=chapter_no)
                text = source["content"]
            except (OSError, UnicodeError, ValueError):
                skipped.append(number)
                continue
            if not text or content_hash(text) != row["content_hash"]:
                skipped.append(number)
                continue
            chapters[number] = {**row, "content": text}
            # Overlap keeps nearby pronouns and transitions searchable. The
            # selected chapter is subsequently read in full within the packet budget.
            for offset in range(0, len(text), 1000):
                candidates.append({"source_id": f"chapter:{number:05d}:offset:{offset}",
                    "source_type": "canon_source", "chapter_no": number, "version": source["version"],
                    "source_hash": source["content_hash"], "authority_rank": 2,
                    "authority": "已接受正文定位候选", "entities": _entities(str(row["title"]) + text[offset:offset + 5000]),
                    "title": str(row["title"]), "body": text[offset:offset + 5000]})
        for item in self._candidates(role=role, chapter_no=chapter_no, chapter_version=None):
            if (item["source_type"] in {"canon_fact", "canon_thread", "chapter_summary"} and item["chapter_no"] in chapters
                    or item["source_type"] in {"story_reference", "expression_reference", "user_preference"}):
                candidates.append(item)
        for item in candidates:
            item["memory_layer"], item["category"], item["identity"] = self._memory_identity(item["source_type"])
            if item["source_type"] == "user_preference" and item.get("status") == "candidate":
                item["identity"] = "preference_candidate"
        candidates, overlaps, conflicts = self._merge_candidates(candidates)
        by_id = {item["source_id"]: item for item in candidates}
        scores: dict[int, float] = {}
        matched: dict[int, list[str]] = {}
        reference_scores: dict[str, float] = {}
        branch_diagnostics = []
        from .preferences import applicable_preferences
        for query in queries:
            preferences = applicable_preferences(self._preference_items(), query,
                {"chapter_no": chapter_no, "genre": self.project.db.get_brief().genre})
            allowed_preferences = {item["preference_id"] for item in preferences}
            eligible = [item for item in candidates if item["source_type"] != "user_preference"
                        or item["source_id"] in allowed_preferences]
            exact, lexical, branch = self._local_branches(query, eligible)
            branch_diagnostics.append({"query": query, "branches": branch})
            ranking = list(dict.fromkeys(source_id for source_id, _ in exact + lexical))
            seen: set[int] = set()
            for rank, source_id in enumerate(ranking):
                if by_id[source_id]["source_type"] in {"story_reference", "expression_reference", "user_preference"}:
                    reference_scores[source_id] = reference_scores.get(source_id, 0.0) + 1 / (rank + 1)
                    continue
                number = int(by_id[source_id]["chapter_no"])
                if number in seen:
                    continue
                seen.add(number)
                scores[number] = scores.get(number, 0.0) + 1 / (rank + 1)
                matched.setdefault(number, []).append(query)
        selected = sorted(scores, key=lambda number: (-scores[number], -number))[:6]
        hits = [{**chapters[number], "queries": matched[number]} for number in selected]
        return hits, {"queries": queries, "searched_chapters": sorted(chapters),
            "unreadable_chapters": skipped, "matched_chapters": selected,
            "branches": branch_diagnostics, "overlaps": overlaps, "source_conflicts": conflicts,
            "source_gaps": self.source_gaps,
            "reference_hits": [by_id[key] for key in sorted(reference_scores, key=lambda key: (-reference_scores[key], key))[:6]],
            "meaning": "词法相关性只用于定位；未命中或排名低均不能证明事实不存在，正文支持关系由责任角色复核。"}

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
        self.source_gaps = []
        for fact in self.project.db.facts_as_of(chapter_no - 1):
            items.append({
                "source_id": str(fact["fact_id"]), "source_type": "canon_fact",
                "title": f"{fact['subject']} / {fact['predicate']}",
                "body": json.dumps(fact["value"], ensure_ascii=False) + "\n" + str(fact.get("evidence") or ""),
                "chapter_no": int(fact["source_chapter"]), "version": fact.get("source_version"),
                "source_hash": fact.get("source_hash"), "evidence": fact.get("evidence", ""),
                "subject": fact["subject"], "predicate": fact["predicate"], "value": fact["value"],
                "valid_from_chapter": fact.get("valid_from_chapter"), "valid_to_chapter": fact.get("valid_to_chapter"),
                "epistemic_kind": fact.get("epistemic_kind", "objective"),
                "event_time": fact.get("event_time"), "narrative_time": fact.get("narrative_time"),
                "authority_rank": 2, "authority": "已验收正史", "entities": [str(fact["subject"])],
            })
        thread_state = self.project.db.threads_state_as_of(chapter_no - 1)
        recovered_chapters = {int(item["chapter_no"]) for item in thread_state["recovery_sources"]}
        for gap in thread_state["coverage_gaps"]:
            number = int(gap["chapter_no"])
            if number in recovered_chapters:
                continue
            try:
                source = self._accepted_source(number, before_chapter=chapter_no)
                recovered_chapters.add(number)
                gap.update(state="canonical_source_available", source_version=source["version"], source_hash=source["content_hash"])
                gap.pop("source_problem", None)
                thread_state["recovery_sources"].append({"chapter_no": number, "title": source["title"],
                    "content": source["content"], "source_version": source["version"], "source_hash": source["content_hash"],
                    "authority": "已接受正文回源；历史线索状态未知，需原记忆责任角色核对"})
            except (OSError, UnicodeError, ValueError):
                pass
        self.source_gaps.extend(thread_state["coverage_gaps"])
        for source in thread_state["recovery_sources"]:
            items.append({"source_id": f"chapter:{source['chapter_no']:05d}", "source_type": "canon_source",
                "title": source["title"], "body": source["content"], "chapter_no": source["chapter_no"],
                "version": source["source_version"], "source_hash": source["source_hash"],
                "authority_rank": 2, "authority": source["authority"],
                "entities": _entities(source["title"] + source["content"])})
        for thread in thread_state["threads"]:
            items.append({
                "source_id": str(thread["thread_id"]), "source_type": "canon_thread",
                "title": str(thread["title"]), "body": str(thread["description"]),
                "chapter_no": int(thread.get("last_advanced_chapter") or thread.get("planted_chapter") or 0), "version": thread.get("source_version"),
                "source_hash": thread.get("source_hash"),
                "authority_rank": 2, "authority": "已验收正史", "entities": _entities(str(thread["title"]) + str(thread["description"])),
            })
        for chapter in self.project.db.accepted_chapters():
            number = int(chapter["chapter_no"])
            if number >= chapter_no:
                continue
            body = str(chapter.get("summary") or "").strip()
            rebuilt = self.project.db.rebuilt_chapter_summary(number) if chapter.get("summary_stale") else None
            if rebuilt:
                body = rebuilt["chapter_summary"]
            if chapter.get("summary_stale") and not rebuilt:
                gap = {"kind": "chapter_summary", "chapter_no": number,
                       "version": int(chapter["version"]), "source_hash": chapter["content_hash"],
                       "state": "source_unavailable", "owner": "memory_owner",
                       "reason": "摘要/记忆补丁与当前已接受正文的提交版本不同，旧摘要已隔离。"}
                try:
                    text = self.project.db.canonical_chapter_content(number)
                    if text is None:
                        path = (self.project.root / chapter["path"]).resolve()
                        text = path.read_text(encoding="utf-8") if path.is_relative_to(self.project.root.resolve()) else ""
                    if text and content_hash(text) == chapter["content_hash"]:
                        items.append({"source_id": f"chapter:{number:05d}", "source_type": "canon_source",
                            "title": str(chapter["title"]), "body": text, "chapter_no": number,
                            "version": int(chapter["version"]), "source_hash": chapter["content_hash"],
                            "authority_rank": 2, "authority": "已接受正文回源，摘要尚未重建",
                            "entities": _entities(str(chapter["title"]) + text)})
                        gap["state"] = "canonical_source_available"
                    else:
                        gap["source_problem"] = "正史正文缺失或哈希不匹配。"
                except (OSError, ValueError) as exc:
                    gap["source_problem"] = str(exc)
                self.source_gaps.append(gap)
                body = ""
            if body:
                items.append({
                    "source_id": f"chapter-summary:{number:05d}", "source_type": "chapter_summary",
                    "title": str(chapter["title"]), "body": body, "chapter_no": number,
                    "version": int(chapter["version"]), "authority_rank": 2,
                    "source_hash": chapter["content_hash"],
                    "summary_origin": ({key: rebuilt[key] for key in ("actor", "task_id", "run_id", "authority")}
                                       if rebuilt else {"authority": "committed_memory_summary"}),
                    "authority": "已接受正文派生摘要；必要时回源，不代替正文",
                    "entities": _entities(str(chapter["title"]) + body),
                })
        for preference in self._preference_items():
            candidate = preference.get("status") == "candidate"
            hard = preference["strength"] == "hard" and not candidate
            body = f"范围：{preference['scope']}；层级：{preference.get('level', 'project')}；状态：{preference.get('status', 'active')}；{preference['text']}"
            items.append({
                "source_id": str(preference["preference_id"]), "source_type": "user_preference",
                "title": f"{preference['scope']} 用户偏好", "body": body,
                "chapter_no": None, "version": preference.get("revision", 0),
                "revision": preference.get("revision", 0), "status": preference.get("status", "active"),
                "text": preference["text"], "scope": preference["scope"], "strength": "hard" if hard else "weak",
                "level": preference.get("level", "project"), "source_quote": preference.get("source_quote", ""),
                "source_ref": preference.get("source_ref", ""),
                "source_hash": content_hash(json.dumps({key: preference.get(key) for key in
                    ("preference_id", "revision", "status", "text", "scope", "strength", "level", "source_quote")}, ensure_ascii=False, sort_keys=True)),
                "authority_rank": 8 if candidate else 3 if hard else 6,
                "authority": "待确认反馈参考；非硬要求" if candidate else "用户长期硬规则" if hard else "用户弱偏好",
                "entities": _entities(str(preference["text"])),
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
        from .story_settings import StorySettingsService
        for record in StorySettingsService(self.project).retrieval_records(chapter_no=chapter_no, actor=role):
            body = json.dumps(record, ensure_ascii=False)
            items.append({"source_id": str(record["record_id"]), "source_type": "story_reference",
                "title": record["collection_name"] + " / " + record["title"], "body": body,
                "chapter_no": record.get("source_chapter"), "version": record["revision"],
                "source_hash": content_hash(body), "authority_rank": 7, "authority": "reference_only",
                "epistemic_status": record["epistemic_status"], "status": record["status"],
                "entities": _entities(record["title"] + body)})
        # ponytail: local reference files are scanned linearly; index only after measured large-library latency.
        for reference in load_reference_cards(self.project):
            body = json.dumps(reference, ensure_ascii=False)
            title = str(reference.get("title") or reference.get("name") or reference["reference_id"])
            items.append({"source_id": f"reference:{reference['reference_id']}", "source_type": "expression_reference",
                "title": title, "body": body, "chapter_no": None, "version": None,
                "source_hash": reference["source_hash"], "source_path": reference["source_path"],
                "authority_rank": 8, "authority": "reference_only", "entities": _entities(title + body)})
        from .knowledge import KnowledgeService, STATES
        self._knowledge_snapshot = KnowledgeService(self.project).snapshot(actor=role, before_chapter=chapter_no)
        for source in KnowledgeService(self.project).reference_hits(getattr(self, "_knowledge_query", "") or "\0"):
            items.append({"source_id": source["id"], "source_type": "expression_reference", "title": source["title"],
                "body": source["body"], "chapter_no": None, "version": None, "source_hash": source["hash"],
                "authority_rank": 8, "authority": "外部参考原文，不属于小说正史", "entities": _entities(source["title"])})
        existing_ids = {item["source_id"] for item in items}
        # 全文成为可检索候选；摘要仍可先读，但不能因摘要省略而找不到原文。
        for source in self._knowledge_snapshot["sources"]:
            if source["state"] == "canon":
                identity = f"chapter:{source['chapter']:05d}"
                if identity not in existing_ids:
                    items.append({"source_id": identity, "source_type": "canon_source", "title": source["title"],
                        "body": source["body"], "chapter_no": source["chapter"], "version": source["version"], "source_hash": source["hash"],
                        "authority_rank": 2, "authority": "已接受正文全文", "entities": _entities(source["title"] + source["body"])})
        for node in self._knowledge_snapshot["nodes"]:
            sections = [section for section in node["sections"] if section["state"] != "conflict"]
            if not sections:
                continue
            body = "\n\n".join(f"{section['title']}（{STATES.get(section['state'], section['state'])}"
                f"{'，历史状态' if section.get('current') is False else ''}，来源 {section['source_id']}）\n"
                f"{section['body']}\n原文：{section['quote']}" for section in sections)
            if node["note"]:
                body = "作者档案补充（未改写已确认事实）：\n" + node["note"] + "\n\n" + body
            items.append({"source_id": "knowledge:" + node["id"], "source_type": "knowledge_page",
                "title": node["name"], "body": body, "chapter_no": None, "version": node["revision"],
                "source_hash": content_hash(body), "authority_rank": 5,
                "authority": "分层知识档案：含历史、规划与有据归纳，不能代替原文或提高来源效力",
                "entities": [node["name"], *node["aliases"]]})
        for item in items:
            item["content_hash"] = content_hash(str(item["body"]))
            item["memory_layer"], item["category"], item["identity"] = self._memory_identity(item["source_type"])
            if item["source_type"] == "user_preference" and item.get("status") == "candidate":
                item["identity"] = "preference_candidate"
        return items

    @staticmethod
    def _memory_identity(source_type: str) -> tuple[str, str, str]:
        return {"canon_fact": ("canon", "facts", "accepted_fact"),
            "canon_thread": ("canon", "threads", "accepted_thread_state"),
            "canon_source": ("canon", "sources", "accepted_prose"),
            "chapter_summary": ("canon", "sources", "derived_summary"),
            "story_reference": ("reference_design", "story_settings", "reference_only"),
            "expression_reference": ("reference_design", "expression", "reference_only"),
            "knowledge_page": ("reference_design", "knowledge", "source_bound_knowledge"),
            "user_preference": ("author_preferences", "preferences", "preference"),
            "work_decision": ("task_temporary", "task", "collaboration")}.get(
                source_type, ("task_temporary", "task", "unclassified"))

    @staticmethod
    def _merge_candidates(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in candidates:
            groups.setdefault(str(item["source_id"]), []).append(item)
        merged, overlaps, conflicts = [], [], []
        for source_id, values in groups.items():
            fingerprints = {(item.get("chapter_no"), item.get("version"), item.get("source_hash"),
                             content_hash(str(item["body"])), item.get("identity")) for item in values}
            if len(fingerprints) > 1:
                conflicts.append({"source_id": source_id, "state": "conflict", "excluded": True,
                    "reason": "同一来源 ID 的正文内容、版本、哈希或权威身份不一致，交原责任角色核对。",
                    "sources": [{key: item.get(key) for key in ("source_type", "chapter_no", "version", "source_hash", "identity")}
                                for item in values]})
                continue
            chosen = dict(min(values, key=lambda item: item["authority_rank"]))
            chosen["retrieval_uses"] = sorted({str(item["category"]) for item in values})
            merged.append(chosen)
            if len(values) > 1:
                overlaps.append({"source_id": source_id, "state": "merged", "uses": chosen["retrieval_uses"],
                    "reason": "同源同版本同内容的合法多用途命中，合并读取并保留用途。"})
        return merged, overlaps, conflicts

    def _local_branches(self, query: str, candidates: list[dict[str, Any]]) -> tuple[list[tuple[str, float]], list[tuple[str, float]], list[dict[str, Any]]]:
        categories = ("facts", "threads", "story_settings", "knowledge", "expression", "preferences", "sources", "task")
        groups = {category: tuple(item for item in candidates if category in item["retrieval_uses"])
                  for category in categories}
        def rank(category: str) -> tuple[str, list[tuple[str, float]], list[tuple[str, float]], str]:
            # Only immutable candidate snapshots and local lexical ranking cross
            # threads. Database, embedding models and provider calls stay outside.
            try:
                values = list(groups[category])
                return category, self._exact_ranking(query, values), self._bm25_ranking(query, values), ""
            except (ValueError, TypeError, KeyError) as exc:
                return category, [], [], str(exc)
        active = [category for category in categories if groups[category]]
        if len(active) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(active))) as pool:
                results = list(pool.map(rank, active))
        else:
            results = [rank(category) for category in active]
        exact, lexical, diagnostics = [], [], []
        by_category = {item[0]: item for item in results}
        for category in categories:
            _, first, second, error = by_category.get(category, (category, [], [], ""))
            exact.extend(first)
            lexical.extend(second)
            diagnostics.append({"category": category, "state": "failed" if error else "matched" if first or second else "empty",
                "candidate_count": len(groups[category]), "matched_source_ids": list(dict.fromkeys(key for key, _ in first + second)),
                "reason": error or ("本地分类匹配完成。" if first or second else "无适用匹配，停止此分支。")})
        return sorted(exact, key=lambda item: (-item[1], item[0])), sorted(lexical, key=lambda item: (-item[1], item[0])), diagnostics

    def _knowledge_related_ids(self, query: str) -> set[str]:
        """沿明确对象的一跳有据关系补资料，不将连接数当事实权重。"""
        nodes = self._knowledge_snapshot.get("nodes", [])
        matched = {node["id"] for node in nodes if any(name and name in query for name in [node["name"], *node["aliases"]])}
        expanded = set(matched)
        for edge in self._knowledge_snapshot.get("edges", []):
            if edge["state"] != "conflict" and (edge["source"] in matched or edge["target"] in matched):
                expanded.update((edge["source"], edge["target"]))
        result = {"knowledge:" + identity for identity in expanded}
        for node in nodes:
            if node["id"] in expanded:
                result.update(f"chapter:{int(source.split(':')[1]):05d}" for source in node["sources"] if source.startswith("chapter:"))
        return result

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
        query_key = ("__query__", self.embedding_model, content_hash(query))
        query_vector = _VECTOR_CACHE.get(query_key)
        if query_vector is None:
            encoded_query = model.encode([query], normalize_embeddings=True)[0]
            query_vector = encoded_query.tolist() if hasattr(encoded_query, "tolist") else list(encoded_query)
            _remember_vector(query_key, query_vector)
        vectors = []
        missing_items = []
        missing_texts = []
        for item in candidates:
            text = item["title"] + "\n" + item["body"]
            source_hash = content_hash(text)
            cache_key = (item["source_id"], self.embedding_model, source_hash)
            cached = _VECTOR_CACHE.get(cache_key)
            if cached is None:
                cached = self.project.db.get_cached_embedding(item["source_id"], self.embedding_model, source_hash)
                if cached is not None:
                    _remember_vector(cache_key, cached)
            if cached is None:
                self._embedding_cache_misses += 1
                vectors.append(None)
                missing_items.append((len(vectors) - 1, item, source_hash))
                missing_texts.append(text)
            else:
                self._embedding_cache_hits += 1
                vectors.append(cached)
        if missing_texts:
            encoded = model.encode(missing_texts, normalize_embeddings=True)
            for (index, item, source_hash), vector in zip(missing_items, encoded, strict=True):
                values = vector.tolist() if hasattr(vector, "tolist") else list(vector)
                vectors[index] = values
                self.project.db.cache_embedding(item["source_id"], self.embedding_model, source_hash, values)
                _remember_vector((item["source_id"], self.embedding_model, source_hash), values)
        return sorted(
            [(item["source_id"], float(sum(float(a) * float(b) for a, b in zip(query_vector, vector, strict=True)))) for item, vector in zip(candidates, vectors, strict=True)],
            key=lambda value: (-value[1], value[0]),
        )

    def _rerank(self, query: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        model = _cached_cross_encoder(self.reranker_model)
        scores = [float(score) for score in model.predict([(query, item["title"] + "\n" + item["body"]) for item in hits])]
        if len(scores) != len(hits) or any(not isfinite(score) for score in scores):
            raise ValueError("重排模型没有返回与候选对应的有效分数，继续采用排名融合结果。")
        for item, score in zip(hits, scores, strict=True):
            item["reranker_score"] = float(score)
        return sorted(hits, key=lambda item: (item["authority_rank"], item.get("semantic_only", False), -item["reranker_score"], -item["score"]))

    @staticmethod
    def _relation_expand(selected: list[str], candidates: list[dict[str, Any]], *, limit: int) -> list[str]:
        if limit < 2 or not selected:
            return selected[:limit]
        by_id = {item["source_id"]: item for item in candidates}
        reserve = min(4, max(1, limit // 4), limit - 1)
        anchors = selected[:max(1, limit - reserve)]
        entities = {entity for source_id in anchors for entity in by_id[source_id]["entities"]}
        evidence_anchors = [by_id[source_id] for source_id in anchors if by_id[source_id].get("source_type") == "canon_fact"
                            and by_id[source_id].get("version") is not None]
        def evidence_link(item: dict[str, Any]) -> bool:
            return item.get("source_type") in {"canon_source", "chapter_summary"} and any(
                anchor.get("chapter_no") == item.get("chapter_no") and anchor["version"] == item.get("version")
                for anchor in evidence_anchors)
        related = sorted((item for item in candidates if item["source_id"] not in selected
                          and (evidence_link(item) or entities.intersection(item["entities"]))),
                         key=lambda item: (item["authority_rank"], not evidence_link(item), -len(entities.intersection(item["entities"])), item["source_id"]))
        if not related:
            return selected[:limit]
        additions = [item["source_id"] for item in related[:reserve]]
        return selected[:limit - len(additions)] + additions


def _entities(text: str) -> list[str]:
    return sorted(set(re.findall(r"[\u3400-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_-]{2,}", text)))[:20]


def _cached_sentence_model(model_name: str):
    key = f"embedding:{model_name}"
    if key not in _MODEL_CACHE:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("启用 BGE-M3 语义召回前，请安装 `pip install -e .[rag]`。") from exc
        _MODEL_CACHE[key] = SentenceTransformer(model_name, trust_remote_code=False, local_files_only=True)
    return _MODEL_CACHE[key]


def _cached_cross_encoder(model_name: str):
    key = f"reranker:{model_name}"
    if key not in _MODEL_CACHE:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise RuntimeError("启用 BGE reranker 前，请安装 `pip install -e .[rag]`。") from exc
        _MODEL_CACHE[key] = CrossEncoder(model_name, trust_remote_code=False, local_files_only=True)
    return _MODEL_CACHE[key]


def _paragraph_excerpt(text: str, offset: int, *, max_chars: int = 5000) -> tuple[str, int, int]:
    """Keep nearby paragraph boundaries while retaining exact source offsets."""
    offset = max(0, min(len(text), offset))
    boundary = text.rfind("\n", max(0, offset - 300), offset)
    start = boundary + 1 if boundary >= 0 else offset
    end = min(len(text), start + max_chars)
    if end < len(text):
        boundary = text.rfind("\n", max(start + 1, end - 300), end)
        if boundary > start:
            end = boundary
    return text[start:end], start, end


def _remember_vector(key: tuple[str, str, str], vector: list[float]) -> None:
    """Store derived vectors with a bounded FIFO policy."""

    if key in _VECTOR_CACHE:
        _VECTOR_CACHE[key] = vector
        return
    if len(_VECTOR_CACHE) >= _VECTOR_CACHE_LIMIT:
        _VECTOR_CACHE.pop(next(iter(_VECTOR_CACHE)))
    _VECTOR_CACHE[key] = vector
