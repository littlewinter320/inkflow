"""技能复用现有工作经验闭环；学习选择建议，内容改动始终保留为候选。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

from .learning import WorkflowMemory
from .utils import content_hash


def _samples(memory: WorkflowMemory, *, limit: int = 120) -> list[dict[str, Any]]:
    """读取原任务及绑定原结果哈希的作者评价；损坏样本不能参与排序。"""
    if not memory.path.is_file():
        return []
    with closing(sqlite3.connect(memory.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        rows = connection.execute("SELECT identity,payload,observed_at FROM experiences WHERE kind='query_outcome' "
            "ORDER BY observed_at DESC,rowid DESC LIMIT ?", (limit,)).fetchall()
        feedback_rows = connection.execute("SELECT identity,payload FROM experiences WHERE kind='workflow_feedback'").fetchall()
    feedback = {}
    for identity, raw in feedback_rows:
        try:
            feedback[identity] = json.loads(raw)
        except (ValueError, TypeError):
            continue
    result = []
    for identity, raw, observed_at in rows:
        try:
            case = json.loads(raw)
            if (not isinstance(case, dict) or not isinstance(case.get("skills"), list) or not case["skills"]
                    or any(not isinstance(item, dict) or not all(isinstance(item.get(key), str) and item[key]
                        for key in ("id", "hash")) for item in case["skills"])
                    or not isinstance(case.get("query"), str) or not case.get("run_id")):
                continue
            explicit = feedback.get("feedback:" + identity)
            if not isinstance(explicit, dict) or explicit.get("sample_hash") != content_hash(raw):
                explicit = None
            reward = explicit.get("reward") if explicit else case.get("reward")
            if type(reward) is not int or reward not in {-1, 0, 1}:
                continue
            result.append({**case, "id": identity, "observed_at": observed_at, "reward": reward,
                "feedback": explicit, "feedback_source": "author" if explicit else "program_receipt"})
        except (ValueError, TypeError, KeyError):
            continue
    return result


def shared_workflow_memory() -> WorkflowMemory:
    """通用经验复用同一经验库结构，放个人技能目录；不混入本书正文与风格反馈。"""
    from .skills import personal_skills_root
    directory = personal_skills_root().parent / "skill-experience"
    directory.mkdir(parents=True, exist_ok=True)
    return WorkflowMemory(directory)


def record_shared_observation(project: Any, identity: str, payload: dict[str, Any]) -> None:
    """仅共享可核对的工具故障和调用成本；原话、文风评价和书稿不离开本书经验库。"""
    metrics = payload.get("metrics_by_role", {})
    errors = sorted({entry["error_type"] for entry in payload.get("attempts", []) if entry.get("error_type")})
    shared_workflow_memory().record("technical:" + content_hash(project.project_id + ":" + identity),
        "coordinator", "skill_tool_experience", {
            "skills": [{"id": item["id"], "hash": item["hash"]} for item in payload.get("skills", [])],
            "action": payload["action"], "error_types": errors,
            "metrics_by_role": {role: {key: values.get(key) for key in (
                "known_input", "known_cached_input", "known_output", "unknown_usage", "known_cost_micros",
                "unknown_cost", "known_duration_ms", "unknown_duration", "sent_requests", "routes")}
                for role, values in metrics.items()},
            "source": {"novel_id": project.project_id, "sample_id": identity,
                "result_hash": payload["source"]["result_hash"]},
            "policy": "通用调用观察；只适用于同模型、工具和版本，不是创作满意度、因果收益或权限。"})


def shared_tool_guidance(role: str, *, action: str = "") -> list[dict[str, Any]]:
    """通用经验只回传短投影，模型核对适用模型/版本后参考，不载入其他小说的评价。"""
    try:
        memory = shared_workflow_memory()
        if not memory.path.is_file():
            return []
        with closing(sqlite3.connect(memory.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            rows = connection.execute("SELECT identity,payload FROM experiences WHERE kind='skill_tool_experience' "
                "ORDER BY observed_at DESC LIMIT 40").fetchall()
        result, seen = [], set()
        for identity, raw in rows:
            case = json.loads(raw)
            if action and case.get("action") != action:
                continue
            metrics = case.get("metrics_by_role", {})
            if role != "coordinator":
                metrics = {role: metrics[role]} if role in metrics else {}
            if not metrics:
                continue
            signature = json.dumps([case.get("skills"), case.get("error_types"),
                {owner: values.get("routes") for owner, values in metrics.items()}], sort_keys=True)
            if signature in seen:
                continue
            seen.add(signature)
            result.append({"id": identity, "action": case.get("action"), "skills": case.get("skills"),
                "metrics_by_role": metrics, "error_types": case.get("error_types"),
                "source": case.get("source"), "policy": case.get("policy")})
            if len(result) == 3:
                break
        return result
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return []  # 通用经验暂缺不能使正常小说流程失败。


def refresh_skill_candidates(project: Any) -> None:
    """作者明确差评或跨任务重复失败时，幂等形成有来源的改进建议；不调用模型或改包。"""
    memory = WorkflowMemory(project.internal)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for case in _samples(memory):
        if case["reward"] >= 0:
            continue
        for item in case["skills"]:
            groups.setdefault((item["id"], item["hash"]), []).append(case)
    for (identity, digest), cases in groups.items():
        distinct = {item.get("task_id") or item["run_id"] for item in cases}
        if not any(case["feedback_source"] == "author" for case in cases) and len(distinct) < 2:
            continue
        proposal_id = "skill-improvement:" + content_hash(identity + ":" + digest)
        # 仅表达待调查建议，不武断把供应商错误或作者差评归因于某一份技能。
        memory.record(proposal_id, "coordinator", "skill_improvement_candidate", {
            "skill_id": identity, "skill_hash": digest, "status": "suggested",
            "sample_ids": [case["id"] for case in cases[:8]], "independent_tasks": len(distinct),
            "reason": "出现作者明确问题反馈或多个独立任务未完成，建议先核对共同原因。",
            "proposed_change": "对照原任务、工具回执和原技能，检查触发范围、必要资料、条件与交接输出；确认根因后再生成修改候选。",
            "activation": "不会自动改写或启用；明确要求改进时复用技能创建入口，生成未启用副本。"})


def skill_choice_guidance(project: Any, query: str, action: str, candidates: list[dict[str, Any]], *,
                          review_before_revision: bool, collaboration_mode: str) -> dict[str, Any]:
    """按本书、同内容版本和同目标给选择参考；小样本保守收敛，零额外模型调用。"""
    from .retrieval import _terms
    import re
    wanted = set(_terms(re.sub(r"第\s*\d+\s*章", "第章", query)))
    groups = {(item["id"], item["hash"]): {"id": item["id"], "hash": item["hash"], "total": 0,
        "samples": 0, "author_samples": 0, "source_ids": [], "seen": set()} for item in candidates}
    for case in _samples(WorkflowMemory(project.internal)):
        if (action and case.get("action") != action or case.get("collaboration_mode") != collaboration_mode
                or case.get("scope", {}).get("review_before_revision") != review_before_revision):
            continue
        terms = set(_terms(re.sub(r"第\s*\d+\s*章", "第章", case.get("query", ""))))
        similarity = 2 * len(wanted & terms) / max(1, len(wanted) + len(terms))
        if wanted and similarity < .2 or case["reward"] == 0:
            continue
        for item in case["skills"]:
            group = groups.get((item["id"], item["hash"]))
            signal = case.get("task_id") or case["run_id"]
            if group is None or signal in group["seen"]:
                continue
            group["seen"].add(signal)
            group["samples"] += 1
            group["total"] += case["reward"]
            group["author_samples"] += case["feedback_source"] == "author"
            if len(group["source_ids"]) < 3:
                group["source_ids"].append(case["id"])
    choices = [{key: value for key, value in item.items() if key != "seen"}
        | {"score": round(item["total"] / (item["samples"] + 2), 4)} for item in groups.values()]
    ranked = sorted(choices, key=lambda item: (-item["score"], -item["samples"]))
    # 至少三个独立任务的真实回执且有明显差距才自动参考；作者明确反馈仍优先，无需手动评分才能学习。
    preferred = (ranked[0]["id"] if len(ranked) > 1 and ranked[0]["samples"] >= 3
        and ranked[0]["score"] > 0
        and ranked[0]["score"] - ranked[1]["score"] >= .25 else None)
    return {"choices": choices, "preferred": preferred,
        "policy": "本书同版相似任务的建议分，不是命中率或正确率；改版从新证据积累，权限门禁始终保留。"}


def skill_learning_overview(project: Any) -> dict[str, Any]:
    """设置中展示真实样本、精确版本、作者反馈、成本及建议候选，全文保留在原库。"""
    memory = WorkflowMemory(project.internal)
    samples = _samples(memory, limit=60)
    proposals = []
    if memory.path.is_file():
        with closing(sqlite3.connect(memory.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            rows = connection.execute("SELECT identity,payload FROM experiences WHERE kind='skill_improvement_candidate' "
                "ORDER BY observed_at DESC LIMIT 30").fetchall()
        for identity, raw in rows:
            try:
                item = json.loads(raw)
                if not isinstance(item, dict):
                    continue
                # 已撤销差评/修正结果后的旧建议仍保留审计，但不继续当成当前有效候选。
                current = [case for case in samples if case["id"] in item.get("sample_ids", []) and case["reward"] < 0]
                item["status"] = "suggested" if any(case["feedback_source"] == "author" for case in current) or len(
                    {case.get("task_id") or case["run_id"] for case in current}) >= 2 else "evidence_changed"
                proposals.append({"id": identity, **item})
            except (ValueError, TypeError):
                continue
    # 保留完整请求身份和当时价格供核对；不复制正文，不对每份技能平分同一请求的成本。
    return {"enabled": True, "mode": "automatic_passive",
        "samples": samples[:20], "proposals": proposals,
        "policy": "选择自动参考真实结果；改技能先给候选。本书效果隔离，通用工具故障和成本经验可共享。不做后台付费自我优化。"}
