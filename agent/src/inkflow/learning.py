from __future__ import annotations

from typing import Any
import json
import sqlite3
import re
from pathlib import Path
from contextlib import closing

from .utils import utc_now, content_hash

from .project import InkFlowProject
from .preferences import preference_identity


def _workflow_scorecard(completed: bool, status: str, selection: list[dict[str, Any]],
                        metrics: dict[str, Any]) -> dict[str, Any]:
    """按真实回执评分；成本与缓存单列，不让高命中抵消失败，也不给等待武断扣分。"""
    known = [value for value in metrics.values() if isinstance(value, dict)]
    uncached = sum(max(0, value.get("known_input", 0) - value.get("known_cached_input", 0)) for value in known)
    failed = sum(item.get("status") == "failed" for item in selection)
    return {
        "version": 1, "completion_score": 100 if completed else 0 if status == "failed" else None,
        "failed_steps": failed,
        "reused_steps": sum(item.get("reused") is True for item in selection),
        "unexecuted_steps": sum(item.get("executed") is False or item.get("skipped") is True for item in selection),
        "known_uncached_input": uncached if known else None,
        "known_cost_micros": sum(value.get("known_cost_micros", 0) for value in known) if known else None,
        "unknown_usage": sum(value.get("unknown_usage", 0) for value in known) if known else None,
        "unknown_cost": sum(value.get("unknown_cost", 0) for value in known) if known else None,
        "lesson": ("参考已完成的有效步骤，先核本次来源和适用条件。" if completed else
            "按具体失败原因调整未完成步骤；不要原样重放失败方案，也不要重做已有有效成果。"
            if status == "failed" or failed else
            "尚未证明完成；先核对等待条件、真实产物与断点，再选择下一步。"),
    }


class WorkflowMemory:
    """独立的本地工作经验库；只提供建议，不进入正史、偏好或权限判定。"""

    def __init__(self, directory: Path):
        """按小说隔离经验，避免别的书的反馈混入当前任务。"""
        self.path = directory / "workflow-experience.sqlite"

    def record(self, identity: str, role: str, kind: str, payload: dict[str, Any], *, observed_at: str | None = None) -> None:
        """同一次实际尝试幂等更新；保存可复核结论，不保存模型私有思维链。"""
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS experiences (identity TEXT PRIMARY KEY, role TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, observed_at TEXT NOT NULL)")
            connection.execute("INSERT INTO experiences VALUES (?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET payload=excluded.payload,observed_at=excluded.observed_at WHERE experiences.payload<>excluded.payload",
                (identity, role, kind, json.dumps(payload, ensure_ascii=False), observed_at or utc_now()))

    def guidance(self, role: str, *, query: str = "", action: str = "") -> list[dict[str, Any]]:
        """近期正反例并列，模型按本次问题选择；未知结果不能升级为成功。"""
        if not self.path.is_file():
            return []
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            rows = connection.execute("SELECT identity,role,kind,payload FROM experiences WHERE (role=? OR ?='coordinator') AND kind NOT IN ('conversation_memory','query_outcome','workflow_feedback','workflow_feedback_audit','skill_improvement_candidate') ORDER BY observed_at DESC,rowid DESC LIMIT 40", (role, role)).fetchall()
        selected = self.query_guidance(query, action=action) if query.strip() else []
        signatures = set()
        counts = {True: 0, False: len(selected)}
        for identity, owner, kind, raw in sorted(rows, key=lambda row: row[2] == "cache"):
            if counts[kind == "cache"] >= 3:
                continue
            payload = json.loads(raw)
            signature = (owner, kind, payload.get("outcome", payload.get("status")),
                         tuple(payload.get("issue_categories", [])), payload.get("action"),
                         payload.get("request_outcome"), json.dumps(payload.get("repair_outcome_counts", {}), sort_keys=True),
                         json.dumps(payload.get("route", {}), sort_keys=True))
            if signature in signatures:
                continue
            signatures.add(signature)
            # 长输入指纹仅供本地诊断，不能再作为模型资料增加消耗、扰动后续缓存。
            selected.append({"id": identity, "role": owner, "kind": kind,
                **{key: value for key, value in payload.items() if key != "input_fingerprints"}})
            counts[kind == "cache"] += 1
            if len(selected) == 6:
                break
        return selected

    def query_guidance(self, query: str, *, action: str = "") -> list[dict[str, Any]]:
        """检索相似原话的正反例；相关性先于经验分，重复续接不重复计分。"""
        from .retrieval import _terms
        # 章号仅影响相似度，不改保存的原话或执行范围；相同写法可参考其他章节的办法。
        def terms(value: str) -> set[str]:
            """复用现有中文分词，屏蔽变化的章号。"""
            return set(_terms(re.sub(r"第\s*\d+\s*章", "第章", value)))
        wanted = terms(query)
        if not self.path.is_file():
            return []
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            rows = connection.execute("SELECT identity,payload FROM experiences WHERE kind='query_outcome' ORDER BY observed_at DESC,rowid DESC LIMIT 400").fetchall()
            feedback_rows = connection.execute("SELECT identity,payload FROM experiences WHERE kind='workflow_feedback'").fetchall()
        # 损坏的单条样本只失去参考资格；原记录保留，不能让经验层阻断正常生产。
        feedback = {}
        for identity, raw in feedback_rows:
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if (isinstance(value, dict) and type(value.get("reward")) is int
                    and value["reward"] in (-1, 0, 1) and isinstance(value.get("source_quote"), str)):
                feedback[identity] = value
        matched, seen, totals = [], set(), {}
        for identity, raw in rows:
            try:
                case = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if (not isinstance(case, dict) or case.get("sample_version") != 1
                    or not all(isinstance(case.get(key), str) and case[key] for key in
                               ("query", "query_hash", "selection_hash", "run_id", "action"))
                    or not isinstance(case.get("selection"), list) or not isinstance(case.get("result"), dict)
                    or not isinstance(case.get("source"), dict) or type(case.get("reward")) is not int
                    or case["reward"] not in (-1, 0, 1) or action and case["action"] != action):
                continue
            candidate = terms(case.get("query", ""))
            similarity = 2 * len(wanted & candidate) / max(1, len(wanted) + len(candidate))
            if wanted and similarity < .2:
                continue
            signature = case["selection_hash"]
            signal_key = (case.get("task_id") or case["run_id"], case["query_hash"], signature)
            if signal_key in seen:
                continue
            seen.add(signal_key)
            explicit = feedback.get("feedback:" + identity)
            if explicit and explicit.get("sample_hash") != content_hash(raw):
                explicit = None  # 同身份结果已修正时，旧评价仍留审计但不能给新结果评分。
            reward = explicit["reward"] if explicit else case["reward"]
            total, count = totals.get(signature, (0, 0))
            # 中性样本可回源，但不稀释正反反馈；小样本向零收敛，避免一例定终身。
            totals[signature] = (total + reward, count + (reward != 0))
            # 全量链和请求身份已保存在本地样本，提示只给短投影，防止长失败链再次撑大输入。
            selection = case["selection"]
            result = case["result"]
            receipts = result.get("receipts") if isinstance(result.get("receipts"), list) else []
            metrics = {}
            recorded_metrics = case.get("metrics_by_role")
            for role, values in (recorded_metrics.items() if isinstance(recorded_metrics, dict) else []):
                if not isinstance(values, dict):
                    continue
                metrics[role] = {key: values.get(key) for key in
                    ("known_input", "known_cached_input", "known_output", "unknown_usage", "known_cost_micros",
                     "unknown_cost", "known_duration_ms", "unknown_duration", "sent_requests")}
                attempts = values.get("attempt_ids")
                routes = values.get("routes")
                metrics[role]["attempt_count"] = len(attempts) if isinstance(attempts, list) else 0
                metrics[role]["routes"] = routes[:4] if isinstance(routes, list) else []
                metrics[role]["route_count"] = len(routes) if isinstance(routes, list) else 0
            matched.append({"id": identity, "role": "coordinator", "kind": "query_outcome",
                "query_excerpt": case["query"][:240], "action": case["action"],
                "selection": selection if len(selection) <= 12 else selection[:6] + selection[-6:],
                "selection_count": len(selection), "selection_is_excerpt": len(selection) > 12,
                "result": {key: result.get(key) for key in ("status", "completed", "assessment", "reason_excerpt")}
                    | {"receipts": receipts[-8:], "receipt_count": len(receipts), "receipts_are_excerpt": len(receipts) > 8},
                "reward": reward, "label": "positive" if reward > 0 else "negative" if reward < 0 else "unknown",
                "feedback_source": "author" if explicit else "program_receipt",
                **({"author_feedback": {"outcome": explicit.get("outcome"),
                    "quote_excerpt": explicit["source_quote"][:240]}} if explicit else {}),
                "similarity": round(similarity, 4), "selection_hash": signature,
                "task_id": case.get("task_id"), "run_id": case["run_id"],
                "source": case["source"], "snapshot_hash": case.get("snapshot_hash"),
                "metrics_by_role": metrics, "skills": case.get("skills", []),
                "scorecard": case.get("scorecard") or _workflow_scorecard(
                    result.get("completed") is True, str(result.get("status", "")), selection,
                    recorded_metrics if isinstance(recorded_metrics, dict) else {}),
                "graphs": [{key: graph.get(key) for key in ("skill_id", "hash", "plan_hash")}
                    for graph in case.get("graphs", [])]})
        for case in matched:
            total, count = totals[case["selection_hash"]]
            case.update(score=round(total / (count + 2), 4), scored_samples=count)
        ranked = sorted(matched, key=lambda item: (-item["similarity"], -item["score"]))
        # 明确保留相关反例，不能因扣分就永远检索不到失败教训。
        selected = [next(item for item in ranked if item["label"] == label)
                    for label in ("positive", "negative") if any(item["label"] == label for item in ranked)]
        selected_ids = {item["id"] for item in selected}
        selected.extend(item for item in ranked if item["id"] not in selected_ids)
        return selected[:3]

    def record_feedback(self, sample_id: str, outcome: str, source_quote: str) -> dict[str, Any]:
        """作者对指定样本的评价覆盖自动分；同一样本反复点击只更新最后评价。"""
        if outcome not in {"correct", "incorrect", "unknown"} or not source_quote.strip():
            raise ValueError("流程反馈须指定正确、错误或待定，并保留作者评价原话。")
        if not self.path.is_file():
            raise ValueError("原流程样本不存在，不能凭空评分。")
        # 样本绑定、历史评价及当前评价共用一次事务，防止中断后只保存半份评价。
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM experiences WHERE identity=? AND kind='query_outcome'", (sample_id,)).fetchone()
            if not row:
                raise ValueError("原流程样本不存在，不能凭空评分。")
            value = {"sample_id": sample_id, "outcome": outcome, "reward": {"correct": 1, "incorrect": -1, "unknown": 0}[outcome],
                "source_quote": source_quote, "sample_hash": content_hash(row[0])}
            identity = "feedback:" + sample_id
            # 原评价另存内容寻址审计；同一评价重复提交幂等，改判只覆盖当前值。
            for key, kind in ((identity + ":" + content_hash(json.dumps(value, ensure_ascii=False, sort_keys=True)),
                               "workflow_feedback_audit"), (identity, "workflow_feedback")):
                connection.execute("INSERT INTO experiences VALUES (?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET payload=excluded.payload,observed_at=excluded.observed_at WHERE experiences.payload<>excluded.payload",
                    (key, "coordinator", kind, json.dumps(value, ensure_ascii=False), utc_now()))
        return value

    def record_cache(self, fragment: dict[str, Any]) -> None:
        """记录实报好坏样本及可观察的输入变化，不把统计相关性冒充未命中的确定原因。"""
        role = str(fragment.get("role") or "unspecified")
        diagnostic = fragment.get("diagnostic", {})
        route = {key: fragment.get(key) for key in ("provider", "model", "prompt_family")}
        route["endpoint"] = diagnostic.get("request_endpoint")
        started_at = fragment.get("started_at") or utc_now()
        previous = None
        if self.path.is_file():
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
                rows = connection.execute("SELECT identity,payload FROM experiences WHERE role=? AND kind='cache' AND observed_at<=? ORDER BY observed_at DESC,rowid DESC LIMIT 80", (role, started_at)).fetchall()
            for identity, raw in rows:
                candidate = json.loads(raw)
                if (route["endpoint"] and identity != fragment["attempt_id"] and candidate.get("started_at")
                        and candidate.get("route") == route):
                    previous = candidate
                    break
        prompt, hit = fragment.get("input_tokens"), fragment.get("cache_hit_tokens")
        reported = type(prompt) is int and prompt > 0 and type(hit) is int and 0 <= hit <= prompt
        ratio = hit / prompt if reported else None
        signatures = {key: diagnostic.get(key) for key in
            ("system_contract_hash", "user_prefix_4096_hash", "reference_prefix_hash", "quality_hash", "thinking", "message_layout")}
        changed = [key for key, value in signatures.items() if previous and value is not None
                   and previous.get("signatures", {}).get(key) is not None
                   and previous["signatures"][key] != value]
        outcome = ("unknown" if ratio is None else "zero_hit" if hit == 0 else "near_zero_hit" if ratio <= .01
            else "high_hit" if ratio >= .75 else "low_hit")
        # 同角色、接口、模型及阶段才比较；长段相同仍不证明供应商已持久化或必然命中。
        fingerprints = str(diagnostic.get("input_prefix_16k_hashes") or "")
        old_fingerprints = str((previous or {}).get("input_fingerprints") or "")
        common_blocks = 0
        if fingerprints and old_fingerprints:
            for current, old in zip(fingerprints.split(","), old_fingerprints.split(",")):
                if current != old:
                    break
                common_blocks += 1
        self.record(fragment["attempt_id"], role, "cache", {
            "outcome": outcome, "route": route, "signatures": signatures, "started_at": started_at,
            "input_tokens": prompt, "cache_hit_tokens": hit, "hit_rate": ratio,
            "uncached_input_tokens": prompt - hit if reported else None,
            "output_tokens": fragment.get("output_tokens"), "duration_ms": fragment.get("duration_ms"),
            "request_outcome": fragment.get("outcome"), "error_type": fragment.get("error_type"),
            "changed_fields": changed, "previous_comparable_observation": previous is not None,
            "input_fingerprints": fingerprints,
            "common_input_characters_lower_bound": common_blocks * 16384 if fingerprints and old_fingerprints else None,
            "task_id": fragment.get("task_id"), "cache_continuation": diagnostic.get("cache_continuation"),
            "context_changes": {key: diagnostic[key] for key in (
                "session_context_status", "session_context_inactive_estimated_tokens",
                "session_context_emergency_compaction", "session_context_retired_material_pressure") if key in diagnostic},
            "assessment": "缺报不能按零计。首次同类记录不能证明冷缓存；已观察到输入变化也不能证明服务商缓存未命中的全部原因。比较同角色同模型同接口及阶段；高命中不等于内容通过审核。"}, observed_at=started_at)


class LearningService:
    """项目内的反馈记录与偏好排序。"""

    def __init__(self, project: InkFlowProject):
        self.project = project

    def record_query_outcome(self, query: str, intent: Any, response: dict[str, Any], *,
                             run_id: str, task_id: str = "", snapshot_hash: str = "", trace_id: str = "") -> None:
        """自动记录真实查询和回执；无回执不加分，不为学习增加请求。"""
        from .utils import workflow_result_status, workflow_failure_reason
        action = getattr(intent, "action", "unclassified")
        if not query.strip() or not run_id or action in {"chat", "discuss", "exit"}:
            return
        receipts, selection, stack = [], [], [response]
        while stack:
            node = stack.pop()
            # 只投影控制回执及产物身份，不把正文、模型完整输出再复制进经验输入。
            receipt = {key: node[key] for key in ("status", "verdict", "chapter_no", "version", "chapter_version",
                "content_hash", "source_hash", "review_id", "trace_id", "error_type", "recovery_node")
                if key in node and isinstance(node[key], (str, int, bool))}
            if receipt:
                receipts.append(receipt)
            tool = node.get("step") or node.get("tool")
            if isinstance(tool, str):
                data = node.get("result") if isinstance(node.get("result"), dict) else {}
                selection.append({"tool": tool, "status": workflow_result_status(node),
                    "step_id": node.get("tool_step_id"), "executed": data.get("executed"),
                    "reused": data.get("reused", False), "skipped": data.get("skipped", False),
                    "condition": data.get("condition")})
            children = [node.get("result")]
            children.extend(child for key in ("steps", "tool_history")
                            for child in (node[key] if isinstance(node.get(key), list) else []))
            stack.extend(reversed([child for child in children if isinstance(child, dict)]))
        status = workflow_result_status(response)
        if response.get("status") in ("failed", "cancelled", "interrupted"):
            status = response["status"]
        # 正向仅代表程序完成了本次工作，不代表作者满意；等待、取消和资料不足不作为错误。
        completed = (status == "completed" and response.get("status") not in ("cancelled", "interrupted")
            and (response.get("status") in ("completed", "accepted", "saved")
                or any(item.get("content_hash") or item.get("review_id") for item in receipts)))
        failed = not completed and status not in {"cancelled", "interrupted", "waiting_user"} and (
            response.get("status") == "failed" or status == "failed" or any(item.get("error_type") for item in receipts))
        reward = 1 if completed else -1 if failed else 0
        query_hash = content_hash(query)
        identity = "outcome:" + content_hash(json.dumps([task_id, run_id, query_hash]))
        metrics, usage_state, bound_skills, actual_attempts = {}, "unavailable", [], []
        from .model_usage import usage_ledger_path
        ledger = usage_ledger_path()
        if ledger.is_file() and task_id:
            try:
                with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
                    connection.row_factory = sqlite3.Row
                    attempts = connection.execute("SELECT attempt_id,role,provider,endpoint,model,prompt_family,input_tokens,cache_hit_tokens,output_tokens,diagnostic_json,"
                        "request_sent,outcome,error_type,usage_state,cost_estimate_micros,price_json,duration_ms FROM model_attempts "
                        "WHERE novel_id=? AND task_id=?", (self.project.project_id, task_id)).fetchall()
                for attempt in attempts:
                    diagnostic = json.loads(attempt["diagnostic_json"] or "{}")
                    if diagnostic.get("run_id") != run_id:
                        continue
                    values = metrics.setdefault(attempt["role"], {"attempt_ids": [], "known_input": 0,
                        "known_cached_input": 0, "known_output": 0, "unknown_usage": 0, "routes": [],
                        "known_cost_micros": 0, "unknown_cost": 0, "known_duration_ms": 0, "unknown_duration": 0,
                        "sent_requests": 0, "not_sent_requests": 0, "unknown_sent_requests": 0})
                    values["attempt_ids"].append(attempt["attempt_id"])
                    sent = attempt["request_sent"]
                    values["sent_requests" if sent == 1 else "not_sent_requests" if sent == 0 else "unknown_sent_requests"] += 1
                    cost, duration = attempt["cost_estimate_micros"], attempt["duration_ms"]
                    if sent != 0:
                        if type(cost) is int and cost >= 0:
                            values["known_cost_micros"] += cost
                        else:
                            values["unknown_cost"] += 1
                    if type(duration) is int and duration >= 0:
                        values["known_duration_ms"] += duration
                    else:
                        values["unknown_duration"] += 1
                    binding = diagnostic.get("skill_binding") or {}
                    for item in binding.get("skills", []):
                        entry = {"id": item["id"], "hash": item["hash"], "role": attempt["role"],
                            "contract": binding.get("contract"), "binding_hash": binding.get("binding_hash")}
                        if entry not in bound_skills:
                            bound_skills.append(entry)
                    actual_attempts.append({key: attempt[key] for key in ("attempt_id", "role", "request_sent",
                        "outcome", "error_type", "usage_state", "cost_estimate_micros", "duration_ms")}
                        | {"price_snapshot": json.loads(attempt["price_json"] or "{}"), "skill_binding": binding})
                    route = {key: attempt[key] for key in ("provider", "model", "prompt_family")}
                    route["endpoint"] = diagnostic.get("request_endpoint") or attempt["endpoint"]
                    if route not in values["routes"]:
                        values["routes"].append(route)
                    prompt, cached, output = (attempt[key] for key in ("input_tokens", "cache_hit_tokens", "output_tokens"))
                    if sent != 0 and type(output) is int and output >= 0:
                        values["known_output"] += output
                    if sent == 0:
                        continue  # 预检失败不能冒充一次供应商调用或零命中请求。
                    if type(prompt) is int and prompt > 0 and type(cached) is int and 0 <= cached <= prompt:
                        values["known_input"] += prompt
                        values["known_cached_input"] += cached
                    else:
                        values["unknown_usage"] += 1
                usage_state = "recorded" if metrics else "no_bound_attempts"
            except (OSError, sqlite3.Error, ValueError, TypeError):
                metrics, bound_skills, actual_attempts = {}, [], []  # 用量不可核对不丢任务样本，也不能把缺报当零。
        # 图是计划，步骤结果来自实际执行回执；两者都保留，不用计划图推断已完成。
        from .skill_workflows import run_workflow_plans
        plans = run_workflow_plans(task_id, run_id) if task_id else []
        graph = [{key: plan.get(key) for key in ("skill_id", "hash", "plan_hash", "steps", "skipped")}
                 for plan in plans]
        for plan in plans:
            entry = {"id": plan["skill_id"], "hash": plan["hash"], "role": "coordinator", "contract": "workflow",
                "binding_hash": plan.get("plan_hash")}
            if entry not in bound_skills:
                bound_skills.append(entry)
        signature = content_hash(json.dumps({"action": action, "tools": [item["tool"] for item in selection],
            "skills": sorted(bound_skills, key=lambda item: json.dumps(item, sort_keys=True)),
            "graphs": [plan.get("plan_hash") for plan in plans]}, sort_keys=True))
        payload = {
            "sample_version": 1, "query": query, "query_hash": query_hash, "action": action,
            "selection": selection, "selection_hash": signature, "task_id": task_id, "run_id": run_id,
            "snapshot_hash": snapshot_hash, "reward": reward,
            "collaboration_mode": None,
            "metrics_by_role": metrics, "usage_state": usage_state, "skills": bound_skills,
            "scorecard": _workflow_scorecard(completed, status, selection, metrics),
            "attempts": actual_attempts, "graphs": graph,
            "scope": {key: getattr(intent, key, None) for key in ("authorization", "chapter_no", "end_chapter_no", "forbidden_actions", "review_before_revision")},
            "result": {"status": status, "receipts": receipts, "completed": completed,
                "reason_excerpt": workflow_failure_reason(response)[:480] if not completed else "",
                "assessment": "程序工作回执，不是作者评价；失败须核对错误类型，不能归咎用户原话。"},
            "source": {"run_id": run_id, "trace_id": trace_id,
                "result_hash": content_hash(json.dumps(response, ensure_ascii=False, sort_keys=True, default=str))}}
        from .task_settings import active_task_settings
        scope = active_task_settings.get()
        if scope is not None and scope.task_id == task_id:
            payload["collaboration_mode"] = scope.effective_collaboration_mode
        WorkflowMemory(self.project.internal).record(identity, "coordinator", "query_outcome", payload)
        if bound_skills:
            from .skill_learning import record_shared_observation, refresh_skill_candidates
            record_shared_observation(self.project, identity, payload)
            refresh_skill_candidates(self.project)

    def workflow_feedback(self, sample_id: str, outcome: str, source_quote: str) -> dict[str, Any]:
        """可接收明确绑定原样本的补充评价；自动学习无需作者填写。"""
        result = WorkflowMemory(self.project.internal).record_feedback(sample_id, outcome, source_quote)
        from .skill_learning import refresh_skill_candidates
        refresh_skill_candidates(self.project)
        return result

    def pending_work_memory(self, items: list[dict[str, Any]], task_id: str) -> list[dict[str, Any]]:
        """将其他历史工作压成可回源记忆；本任务、未答问题及原始断点不被摘要替换。"""
        from .conversation_scope import active_conversation
        from .terminal_session import TerminalSession
        projected, memories = [], []
        for item in items:
            intent, source = item.get("intent"), item.get("source") or {}
            if (item.get("kind") != "workflow" or not item.get("id") or not isinstance(intent, dict)
                    or not isinstance(source, dict) or task_id and source.get("task_id") == task_id):
                projected.append(item)
                continue
            # 不总结小说事实或推断完成；原话节选、真实状态和记录身份足以识别应续接的旧工作。
            memory = {"source": "metadata:pending_work:" + str(item["id"]),
                "source_hash": content_hash(json.dumps(item, ensure_ascii=False, sort_keys=True)),
                "task_id": source.get("task_id"), "run_id": source.get("run_id") or item.get("run_id"),
                "action": intent.get("action"), "chapter_no": intent.get("chapter_no"),
                "end_chapter_no": intent.get("end_chapter_no"), "status": item.get("status"),
                "goal_excerpt": TerminalSession._reference_excerpt(str(intent.get("requested_outcome")
                    or intent.get("user_message") or intent.get("operation_instruction") or ""), 240),
                "policy": "历史工作记忆，不是授权或完成证明；选择续接后须回读原记录的完整指令、禁止项、配置、版本和预算。"}
            memories.append(memory)
            projected.append({**item, "intent": None, "progress": None, "source": memory})
        if memories:
            try:
                WorkflowMemory(self.project.internal).record("conversation:" + active_conversation.get(),
                    "coordinator", "conversation_memory", {"items": memories,
                        "source": ".inkflow/inkflow.db metadata; original records retained"})
            except (OSError, sqlite3.Error):
                return items  # 记忆存储失败时仍给完整旧记录，不假装已经保存或丢掉可恢复来源。
        return projected

    def overview(self) -> dict[str, Any]:
        saved_settings = self.project.db.get_metadata("learning_settings", {"enabled": True})
        settings = {"enabled": bool(saved_settings.get("enabled", True))}
        guidance = self.project.db.learning_guidance()
        try:
            samples = WorkflowMemory(self.project.internal).query_guidance("")
        except (OSError, sqlite3.Error, ValueError, TypeError):
            samples = []
        return {"settings": settings, "workflow_learning_mode": "automatic_passive", "guidance": guidance,
                "workflow_experience": self.workflow_guidance("coordinator"), "workflow_feedback_samples": samples}

    def workflow_guidance(self, role: str, *, query: str = "", action: str = "") -> dict[str, Any]:
        """读取独立经验及实际缓存回执；只在相似任务中参考，不能据命中率判定写作质量。"""
        try:
            memory = WorkflowMemory(self.project.internal)
            # 把已有真实失败/成功接入经验层，不为“学习”再跑一遍付费请求。
            known = set()
            known_cache = set()
            if memory.path.is_file():
                with closing(sqlite3.connect(memory.path.as_uri() + "?mode=ro", uri=True)) as connection:
                    for identity, kind, raw in connection.execute("SELECT identity,kind,payload FROM experiences"):
                        known.add(identity)
                        if kind == "cache" and json.loads(raw).get("started_at"):
                            known_cache.add(identity)
            # 已存原话与实际路由可直接证明范围误判；取消本身不能证明失败，须逐项核对。
            with self.project.db.connect() as connection:
                pending = connection.execute("SELECT key,value_json FROM metadata WHERE key LIKE 'pending_work:%' ORDER BY updated_at DESC LIMIT 80").fetchall()
            for row in pending:
                identity = "scope:" + row[0]
                if identity in known:
                    continue
                work = json.loads(row[1]); intent = work.get("intent") or {}
                original = str(intent.get("user_message") or "")
                focus = re.search(r"(?:^|[，,。；\n])\s*(?:先|现在|本次|这次)?(?:只|仅)(?:写|创作)第\s*(\d+)\s*章", original)
                if (work.get("kind") == "workflow" and focus and intent.get("action") in {"batch_draft", "batch_draft_accept"}
                        and (intent.get("chapter_no"), intent.get("end_chapter_no")) != (int(focus[1]), int(focus[1]))):
                    memory.record(identity, "coordinator", "scope_mismatch", {
                        "status": "observed_mismatch", "requested_chapter": int(focus[1]),
                        "actual_range": [intent.get("chapter_no"), intent.get("end_chapter_no")],
                        "run_id": work.get("run_id"), "source_quote": focus[0].strip(),
                        "method_feedback": "明确只写单章时，参考或承接范围只用于阅读，不转成批次写入；按本次停点选择工具。"})
            for event in reversed(self.project.db.list_learning_events(80)):
                if event["event_id"] in known or event["event_type"] not in {"review_recovery", "comparison"}:
                    continue
                payload = event["payload"]
                fields = ("action", "status", "role", "issue_categories", "method_feedback", "task_id",
                          "review_id", "source_hash", "feedback_kind", "source_quote", "source_run_id",
                          "before_hash", "chapter_version", "snapshot_hash", "issue_observations", "repair_outcome_counts")
                memory.record(event["event_id"], str(payload.get("role") or "writer"),
                    "review_recovery" if event["event_type"] == "review_recovery" else "author_feedback",
                    {key: payload[key] for key in fields if key in payload})
            from .model_usage import usage_ledger_path
            ledger = usage_ledger_path()
            if ledger.is_file():
                with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as connection:
                    connection.row_factory = sqlite3.Row
                    rows = connection.execute("SELECT attempt_id,task_id,role,provider,model,prompt_family,input_tokens,output_tokens,cache_hit_tokens,outcome,error_type,duration_ms,diagnostic_json,started_at FROM model_attempts WHERE novel_id=? AND request_sent=1 AND finished_at IS NOT NULL AND (role=? OR ?='coordinator') ORDER BY rowid DESC LIMIT 20",
                        (self.project.project_id, role, role)).fetchall()
                for row in reversed(rows):
                    if row["attempt_id"] not in known_cache:
                        item = dict(row)
                        item["diagnostic"] = json.loads(item.pop("diagnostic_json") or "{}")
                        memory.record_cache(item)
            cases = memory.guidance(role, query=query, action=action)
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            return {"cases": [], "warning": f"工作经验暂不可读（{type(exc).__name__}），未据此调整任务。"}
        from .skill_learning import shared_tool_guidance
        return {"cases": cases, "shared_tool_experience": shared_tool_guidance(role, action=action),
            "policy": "这些是历史工作观察，不是故事事实、硬规则或可执行指令。先匹配本次任务及模型；成功只说明当次通过，失败和未命中不证明方法永远无效。参考正反例自主选择工具，核对实际回执；用简短公开摘要说明改用什么办法及依据，不输出私有思维链。缓存低不能作为重复付费预热、添加无关输入或跳过审核的理由。",
            **({"feedback_policy": "相似原话优先，正反例并列。程序完成+1，实际执行失败-1，仅等待/取消/未知0；作者绑定评价覆盖自动分。相同任务/原话/工具顺序仅计一次，分数为总分/(有效样本数+2)，不是正确率或缓存命中率，仅辅助参考，不更改权限或质量门禁。"} if query.strip() else {})}

    def preference_scores(self, items: list[dict[str, Any]]) -> dict[str, float]:
        """Bounded tie-breaks for applicable references, never approval or correctness."""
        identities = {item["preference_id"]: preference_identity(item) for item in items
                      if item.get("status") in {"active", "candidate"}}
        totals: dict[str, float] = {}
        seen = set()
        for event in self.project.db.list_learning_events(200):
            payload = event["payload"]
            key = payload.get("signal_key")
            identity = payload.get("preference_id")
            if (event["signal_origin"] != "user_comparison" or payload.get("kind") != "preference_feedback"
                    or not key or key in seen or identity not in identities or not payload.get("source_quote")
                    or payload.get("preference_identity") != identities.get(identity)):
                continue
            reward = {"liked": 1.0, "corrective": 0.5, "disliked": -1.0}.get(payload.get("feedback_kind"))
            if reward is None:
                continue
            seen.add(key)
            totals[identity] = totals.get(identity, 0.0) + reward
        return {identity: max(-1.0, min(1.0, value / 3.0)) for identity, value in totals.items()}
