"""Authority-aware hybrid retrieval for a single compiled Context Packet."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import math
import re
from typing import Any

from .project import InkFlowProject
from .utils import content_hash


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
        from .preferences import applicable_preferences
        preferences = applicable_preferences(self._preference_items(), query,
            {"chapter_no": chapter_no, "genre": self.project.db.get_brief().genre})
        allowed_preferences = {item["preference_id"] for item in preferences}
        candidates = [item for item in candidates if item.get("source_type") != "user_preference"
                       or item["source_id"] in allowed_preferences]
        candidates, overlaps, conflicts = self._merge_candidates(candidates)
        exact, lexical, branches = self._local_branches(query, candidates)
        self._embedding_cache_hits = 0
        self._embedding_cache_misses = 0
        if not candidates:
            self.last_diagnostics = {"query": query, "candidate_count": 0, "selected": [], "discarded": [],
                                     "source_gaps": self.source_gaps, "branches": branches,
                                     "overlaps": overlaps, "source_conflicts": conflicts}
            return []
        factors = self._adaptive_factors(query, candidates, chapter_no)
        limit = top_k if top_k is not None else self._adaptive_top_k(query, len(candidates), factors)
        limit = max(0, min(int(limit), 32, len(candidates)))
        semantic = self._semantic_ranking(query, candidates) if self.embedding_model else []
        rankings = (("精确", exact), ("BM25", lexical), ("语义", semantic))
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
            # Unscoped historical citations cannot establish relevance or source quality for this request.
            if candidate["authority_rank"] <= 3:
                scores[source_id] += 0.01
        by_id = {item["source_id"]: item for item in candidates}
        ranked_ids = sorted(
            scores,
            key=lambda source_id: (by_id[source_id]["authority_rank"], -scores[source_id], source_id),
        )
        selected = ranked_ids[:limit]
        initial_ids = set(selected)
        selected = self._relation_expand(selected, candidates, limit=limit)
        relation_ids = set(selected) - initial_ids
        hits = []
        for source_id in selected:
            item = dict(by_id[source_id])
            item["score"] = round(scores.get(source_id, 0.0), 6)
            item["retrieval_reasons"] = (["关系扩展：在最终数量预算内替换低相关项"]
                if source_id in relation_ids else reasons.get(source_id, ["关系扩展"]))
            hits.append(item)
        if self.reranker_model and len(hits) > 1:
            hits = self._rerank(query, hits)
        result = hits[:limit]
        source_reads = []
        validated = []
        for hit in result:
            if hit["source_type"] != "chapter_summary":
                validated.append(hit)
                continue
            try:
                source = self._accepted_source(int(hit["chapter_no"]), before_chapter=chapter_no)
                if source["version"] != hit["version"] or source["content_hash"] != hit["source_hash"]:
                    raise ValueError("摘要命中后正文来源版本已变化。")
                text = source["content"]
                spans = [{"source_id": str(offset), "title": hit["title"], "body": text[offset:offset + 5000]}
                         for offset in range(0, len(text), 1000)]
                ranked_spans = self._bm25_ranking(query, spans)
                offset = int(ranked_spans[0][0]) if ranked_spans else 0
                hit = {**hit, "summary_body": hit["body"], "body": text[offset:offset + 5000],
                    "summary_content_hash": hit.get("content_hash"), "content_hash": content_hash(text[offset:offset + 5000]),
                    "identity": "accepted_source_excerpt", "body_kind": "accepted_prose",
                    "authority": "已接受正文定点回查；摘要仅作定位线索",
                    "resolved_source": {"source_id": f"chapter:{hit['chapter_no']:05d}",
                        "chapter_no": hit["chapter_no"], "version": source["version"],
                        "source_hash": source["content_hash"], "start": offset, "end": min(len(text), offset + 5000)}}
                source_reads.append({**hit["resolved_source"], "state": "read", "via": "chapter_summary"})
                validated.append(hit)
            except (ValueError, OSError, UnicodeError) as exc:
                self.source_gaps.append({"kind": "chapter_summary", "chapter_no": hit["chapter_no"],
                    "version": hit.get("version"), "source_hash": hit.get("source_hash"),
                    "state": "source_unavailable", "owner": "memory_owner", "reason": str(exc)})
                source_reads.append({"source_id": hit["source_id"], "state": "failed", "reason": str(exc)})
        result = validated
        selected_ids = {item["source_id"] for item in result}
        self.last_diagnostics = {
            "source_gaps": self.source_gaps,
            "branches": branches, "overlaps": overlaps, "source_conflicts": conflicts,
            "source_reads": source_reads,
            "relation_expansion": {"included": sorted(relation_ids), "replaced": sorted(initial_ids - set(selected)),
                                   "budget": limit},
            "query": query,
            "candidate_count": len(candidates),
            "initial_top_k": limit,
            "retrieved_count": len(result),
            "not_retrieved_count": len(candidates) - len(result),
            "adaptive_factors": factors,
            "selected": [{"source_id": item["source_id"], "score": item["score"], "reasons": item["retrieval_reasons"]} for item in result],
            "discarded": [
                {"source_id": item["source_id"], "reason": "低于动态预算截断线" if item["source_id"] in scores else "未匹配查询"}
                for item in candidates if item["source_id"] not in selected_ids
            ][:80],
            "discarded_display_limit": 80,
        }
        if self.embedding_model:
            self.last_diagnostics["embedding_cache_hits"] = self._embedding_cache_hits
            self.last_diagnostics["embedding_cache_misses"] = self._embedding_cache_misses
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
        categories = ("facts", "threads", "story_settings", "expression", "preferences", "sources", "task")
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
        scores = model.predict([(query, item["title"] + "\n" + item["body"]) for item in hits])
        for item, score in zip(hits, scores, strict=True):
            item["reranker_score"] = float(score)
        return sorted(hits, key=lambda item: (item["authority_rank"], -item["reranker_score"], -item["score"]))

    @staticmethod
    def _relation_expand(selected: list[str], candidates: list[dict[str, Any]], *, limit: int) -> list[str]:
        if limit < 2 or not selected:
            return selected[:limit]
        by_id = {item["source_id"]: item for item in candidates}
        reserve = min(4, max(1, limit // 4), limit - 1)
        anchors = selected[:max(1, limit - reserve)]
        entities = {entity for source_id in anchors for entity in by_id[source_id]["entities"]}
        related = sorted((item for item in candidates if item["source_id"] not in selected
                          and entities.intersection(item["entities"])),
                         key=lambda item: (item["authority_rank"], -len(entities.intersection(item["entities"])), item["source_id"]))
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


def _remember_vector(key: tuple[str, str, str], vector: list[float]) -> None:
    """Store derived vectors with a bounded FIFO policy."""

    if key in _VECTOR_CACHE:
        _VECTOR_CACHE[key] = vector
        return
    if len(_VECTOR_CACHE) >= _VECTOR_CACHE_LIMIT:
        _VECTOR_CACHE.pop(next(iter(_VECTOR_CACHE)))
    _VECTOR_CACHE[key] = vector
