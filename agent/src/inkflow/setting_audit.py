"""On-demand settings diagnosis through the existing editor and versioned edit service."""
from __future__ import annotations

from .errors import ProjectError, ProviderError, ValidationGateError
from .project_lock import project_write_lock
from .role_protocol import check_owners_for_mode
from .schemas import StorySettingsAudit, SettingRecordSet
from .story_settings import StorySettingsService
from .task_settings import active_task_settings
from .utils import content_hash, estimate_tokens, json_dumps


def corrected_rules(rules, corrections, sources):
    result = list(rules)
    seen = set()
    for correction in corrections:
        source = sources.get(correction.source_id, "")
        if not correction.source_quote.strip() or correction.source_quote not in source:
            raise ValidationGateError("设定纠正缺少当前依据的原句，未采用模型建议。")
        if correction.old_rule not in rules and rules.count(correction.new_rule) == 1:
            continue
        if correction.old_rule in seen or rules.count(correction.old_rule) != 1:
            raise ValidationGateError("待纠正的旧要求无法唯一定位，未改动书籍设定。")
        seen.add(correction.old_rule)
        result[rules.index(correction.old_rule)] = correction.new_rule
    return result


async def audit_story_settings(engine, project, *, instruction, apply_corrections=False, conversation_context="", allow_evidence_repair=True, collection_names=None, new_record_limit=None, apply_rule_corrections=None):
    from .manual_edits import ManualEditsService
    from .trace import TraceRecorder
    scope = active_task_settings.get()
    owners = check_owners_for_mode(scope.collaboration_mode if scope else "everyday")
    role = owners.get("general") or owners.get("logic_continuity")
    service = StorySettingsService(project)
    brief = project.db.get_brief()
    latest = project.db.latest_accepted_chapter_no()
    before = engine._planning_source_fingerprint(project, 1, max(1, latest + 1))
    sources = {name: (project.root / name).read_text(encoding="utf-8")
               for name in ("BOOK.md", "OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md")
               if (project.root / name).is_file()}
    from .terminal_session import TerminalSession
    def user_history():
        return "\n\n".join(item["user"] for item in TerminalSession.history(project.root, limit=20))[-12000:]
    sources["近期对话·用户"] = user_history()
    from .context import named_prior_chapters
    accepted = project.db.accepted_chapters()
    named = named_prior_chapters(sources.get("RECENT_PLAN.md", ""), latest + 1, set())
    selected = list(accepted[-3:])
    selected_numbers = {item["chapter_no"] for item in selected}
    by_number = {item["chapter_no"]: item for item in accepted}
    for number in named:
        if number in by_number and number not in selected_numbers and len(selected) < 6:
            selected.append(by_number[number])
            selected_numbers.add(number)
    canon = []
    for chapter in sorted(selected, key=lambda item: item["chapter_no"]):
        source_id = f"chapter:{chapter['chapter_no']:05d}"
        sources[source_id] = project.db.canonical_chapter_content(chapter["chapter_no"]) or ""
        canon.append(chapter["chapter_no"])
    payload = service.context_payload(chapter_no=latest + 1, actor=role,
        max_chars=min(64000, max(1000, engine.settings.context_budget_for(role)[1] // 8)))
    if collection_names is not None:
        if not isinstance(collection_names, list) or not 1 <= len(collection_names) <= 3 or any(not isinstance(name, str) for name in collection_names):
            raise ValidationGateError("请点名一至三份需要建立记录的设定。")
        targets = [item for item in payload["collections"] if item["name"] in collection_names]
        if len(targets) != len(set(collection_names)):
            raise ValidationGateError("指定设定没有全部装入本次核对，未把未读取的合集当作空设定。")
        payload["collections"] = targets
        target_ids = {item["collection_id"] for item in targets}
        payload["coverage"] = [item for item in payload["coverage"] if item["collection_id"] in target_ids]
    if new_record_limit is not None and (type(new_record_limit) is not int or not 1 <= new_record_limit <= 3):
        raise ValidationGateError("新增记录上限须为一至三条。")
    prompt = json_dumps({"当前书籍资料": brief.model_dump(mode="json"),
        "来源": {key: value for key, value in sources.items() if key != "近期对话·用户"}, "设定读取范围": payload,
        "正史事实": project.db.current_facts()[:128], "未结线索": project.db.open_threads()[:64],
        "范围说明": "正文只读取所列章节和有界正史记录；不可声称逐章审完全部小说。",
        "补充来源": {"近期对话·用户": sources["近期对话·用户"]},
        "用户目标": instruction, "新增具体记录上限": new_record_limit,
        "新增说明": "只为本次点名的设定建立有明确追踪目标和当前来源的记录；每份最多一条，不把空模板算作完成。" if collection_names else "按实际问题提出必要记录。"})
    record_capacity = min(8, 2 * sum(bool(item.get("recording_allowed")) for item in payload["collections"]))
    requested_output = 4096 + record_capacity * 1000 + min(4000, 2 * estimate_tokens(json_dumps(brief.user_rules)))
    output_budget = engine.settings.effective_output_tokens(requested_output, role)
    if estimate_tokens(prompt) + output_budget > engine.settings.context_budget_for(role)[1]:
        raise ValidationGateError("本次设定与规划超过当前审核角色的资料预算，请缩小检查范围。")
    trace = TraceRecorder(project.root, "setting-audit", engine.settings.trace_level)
    try:
        trace.record("settings.audit", "started", "正在对照本书设定、当前规划和已读取正文",
            metadata={"role": role, "source_count": len(sources), "output_budget": output_budget})
        result = await engine.provider.generate_json(system_prompt=(
            "你是当前设定审查责任角色。先阅读实际资料，主动比较书籍要求、正式规划和正文依据，不等待用户告诉你错误位置。"
            "区分书籍要求、模板、具体设定参考与正史事实；空合集只报告尚未填写，不冒充审查通过，也不强行填满不必要的分类。"
            "用户已明确改变的目标与过期规则冲突时，可在rule_corrections提出完整旧规则和完整新规则，保留其余未被取代的约束，"
            "每项给当前来源ID、连续原句及原因；不能仅凭模型偏好改规则或把规划假设当已发生事实。"
            "近期用户明确改过的目标优先于旧规则中的不可改写标签，不再次询问已确定选择；模型先前回复不等于用户批准。old_rule必须复制当前user_rules中完整的一条，new_rule只改过期部分、保留其他要求。"
            "以本次当前书籍资料、设定coverage与records为现状；历史用户要求只用来确认选择，不把其中提及的旧问题误当作现在仍存在。索引已有对象必须沿record_id与revision更新；未装入不等于空合集。"
            "仅建议已有明确依据的书籍规则同步，不改规划或正文、不新增设定合集、不降低审核门禁。"
            "无法判断哪项仍有效时放入needs_choice，给差异明确的可选方向、推荐理由和影响；已有明确要求不再重复询问。"
            "实际需要选择时同时填写questions，复用结构化提问卡，每题2至4个具体options及description，最多一个recommended；不列只有其他的空选项卡。"
            "需要用户取舍的条目不能同时放入rule_corrections；无关疑问不能阻止其他有明确依据的修正。"
            "已有设定合集缺记录时，判断所给已接受正文中是否有值得持续追踪的对象；在setting_updates同次返回必要的首次建档或补充，每条有canonical来源章号和连续原句，epistemic_status用objective/belief/rumor。只提取已有文字，不凭空创造、不补满未知字段；已有同一对象复用record_id与expected_revision。没有有用资料就明确空缺。"
            "世界基础设定中的明确用户边界可从BOOK.md原句整理，epistemic_status=user_constraint、source_kind=planning、path=BOOK.md；用户约定不是已经发生的情节，不推测未写出的世界细节。"
            "基础合集不要求所有字段填满：年代未说明，不代表已有背景、前提与边界都无法记录。缺什么字段就留空，只把有明确原句的部分保存。已有同名条目必须用其record_id和revision补充，不能当空合集再创建。"
            "summary和findings用自然中文解释具体问题和证据，不输出内部编号或长路径；不声称未执行的修改已完成。"),
            user_prompt=prompt + "\n报告保持精简：summary不超过400字，findings只列需处理的问题，不逐项复述正常情节。设定候选只填有用字段，每字段一两句，每条最多两处200字以内的连续原句；相同对象合并，不写重复长报告。",
            output_model=StorySettingsAudit, max_tokens=output_budget,
            thinking=False, agent_role=role)
        trace.record_model("settings.audit", result, "设定审查返回，核对当前来源与修正范围")
        if (before != engine._planning_source_fingerprint(project, 1, max(1, latest + 1))
                or sources["近期对话·用户"] != user_history()):
            raise ValidationGateError("检查期间设定或规划发生变化，结论已留档，未覆盖当前资料。")
        report = result.data
        changes = None
        rules_unchanged = False
        rule_pending = []
        if apply_corrections and apply_rule_corrections is not False and report.rule_corrections:
            try:
                rules = corrected_rules(brief.user_rules, report.rule_corrections, sources)
                changes = await engine.edit_story_setting(project.root, document_kind="book",
                    setting_change={"user_rules": rules}, instruction=instruction,
                    expected_book_hash=content_hash(sources.get("BOOK.md", "")))
                job = changes.get("manual_review")
                if not changes.get("changed"):
                    rules_unchanged = True
                    changes = None
                if job and job.get("job_id"):
                    changes["verification"] = await ManualEditsService(project).review(job["job_id"], engine)
            except ValidationGateError as exc:
                rule_pending.append(str(exc))
        candidates = list(report.setting_updates)
        if collection_names or new_record_limit:
            allowed = {item["collection_id"] for item in payload["collections"]}
            selected, seen_collections = [], set()
            for candidate in candidates:
                if candidate.collection_id not in allowed:
                    continue
                if collection_names and candidate.collection_id in seen_collections:
                    continue
                if new_record_limit and len(selected) >= new_record_limit:
                    continue
                selected.append(candidate); seen_collections.add(candidate.collection_id)
            if len(selected) != len(candidates):
                trace.record("settings.scope", "completed", "仅采用本次指定分类与数量内的候选，其他输出留在原报告，不扩大记录范围。",
                             metadata={"returned": len(candidates), "selected": len(selected)})
            candidates = selected

        if changes and changes.get("after"):
            sources["BOOK.md"] = changes["after"]
        if apply_corrections and apply_rule_corrections is not False and not changes and allow_evidence_repair:
            manual = ManualEditsService(project)
            for job in manual.jobs().values():
                if (job.get("relative_path") == "BOOK.md" and job.get("status") in {"pending", "failed"}
                        and int(job.get("attempts", 0)) < 2 and job.get("current_hash") == content_hash(sources["BOOK.md"])):
                    checked = await manual.review(job["job_id"], engine, resume=True)
                    if checked.get("status") != "passed":
                        rule_pending.append("当前书籍要求的原核对尚未通过：" + str(checked.get("summary") or "请查看该核对记录"))
                    break
        invalid = []
        if apply_corrections:
            for index, candidate in enumerate(candidates):
                try:
                    service._evidence(candidate.evidence_refs)
                except ProjectError as exc:
                    invalid.append({"index": index, "candidate": candidate.model_dump(mode="json"), "reason": str(exc)})
        if invalid and allow_evidence_repair:
            # One joint, same-owner evidence repair; valid candidates are never regenerated.
            trace.record("settings.evidence_repair", "started", "仅补核未定位的设定依据，保留有效候选",
                metadata={"attempt": 1, "candidate_count": len(invalid)})
            repair_sources = {key: value for key, value in sources.items() if key.startswith("chapter:") or key == "BOOK.md"}
            try:
                repaired = await engine.provider.generate_json(system_prompt=(
                    "你仅修复本次设定候选的证据定位与归类。保持collection_id、record_id及title，不新增对象、不写正文。"
                    "只从给出的原文逐字摘连续短句；实际发生引用canonical及正整数chapter_no；BOOK.md用户约定用planning/path与user_constraint。"
                    "旧规划不能代替已发生事实。没有实际支持的候选不返回，records可以为空。"),
                    user_prompt=json_dumps({"待补核候选": invalid, "实际原文": repair_sources}),
                    output_model=SettingRecordSet, max_tokens=engine.settings.effective_output_tokens(2000 + 1000 * len(invalid), role),
                    thinking=False, agent_role=role)
                trace.record_model("settings.evidence_repair", repaired, "原责任角色已补核设定引用")
                by_identity = {(item.collection_id, item.record_id, item.title): item for item in repaired.data.records}
                for failure in invalid:
                    old = candidates[failure["index"]]
                    replacement = by_identity.get((old.collection_id, old.record_id, old.title))
                    if replacement is not None:
                        candidates[failure["index"]] = replacement
            except ProviderError as exc:
                trace.record("settings.evidence_repair", "failed", "设定引用补核未完成，仍保存其他有据条目", str(exc))
        adopted, pending = [], rule_pending
        if apply_corrections:
            for candidate in candidates:
                try:
                    args = candidate.model_dump()
                    args["evidence_refs"] = service._evidence(candidate.evidence_refs)
                    for ref in args["evidence_refs"]:
                        if ref["source_kind"] == "planning" and ref.get("path") == "BOOK.md" and candidate.epistemic_status == "user_constraint":
                            current_book = (project.root / "BOOK.md").read_text(encoding="utf-8")
                            expected = changes.get("after", sources["BOOK.md"]) if changes else sources["BOOK.md"]
                            if current_book != expected or ref["quote"] not in sources["BOOK.md"]:
                                raise ValidationGateError("书籍约定已变化，不能沿用这份设定候选")
                            continue
                        source_id = f"chapter:{int(ref.get('chapter_no', 0)):05d}"
                        if (ref.get("source_kind") != "canonical" or not ref.get("quote")
                                or ref["quote"] not in sources.get(source_id, "")
                                or project.db.canonical_chapter_content(int(ref["chapter_no"])) != sources[source_id]):
                            raise ValidationGateError("设定引文没有绑定本次实际读取且仍有效的正文")
                    async with project_write_lock(project.root):
                        expected_collection = next((item for item in payload["collections"] if item["collection_id"] == candidate.collection_id), None)
                        current_collection = service._get(candidate.collection_id, "collection")
                        if (not expected_collection or not expected_collection.get("recording_allowed")
                                or not current_collection or current_collection["revision"] != expected_collection["revision"]):
                            raise ValidationGateError("这份设定未授权本轮记录或配置已变化，未写入旧候选")
                        record = service.save_record(**args, actor=role,
                            operation="supplement" if candidate.record_id else "new")
                        unchanged_verified = not record.get("changed") and record["status"] == "verified_reference"
                        if role == owners["memory"] and not unchanged_verified:
                            service.record_review(record["record_id"], record["revision"], actor=role,
                                decision="verified_reference", source_fingerprint=service.source_fingerprint())
                        elif role != owners["memory"] and not unchanged_verified:
                            manual = ManualEditsService(project)
                            job = manual.enqueue("story-setting:" + record["record_id"], json_dumps(record),
                                kind="setting_record", record_revision=record["revision"])
                    if role != owners["memory"] and not unchanged_verified:
                        checked = await manual.review(job["job_id"], engine)
                        if checked.get("status") != "passed":
                            pending.append(f"《{record['title']}》已记录，等待当前记忆责任角色完成核对")
                            continue
                    adopted.append({"title": record["title"], "record_id": record["record_id"], "collection_id": record["collection_id"], "changed": bool(record.get("changed")), "evidence_refs": record["evidence_refs"]})
                except Exception as exc:
                    pending.append(f"《{candidate.title}》尚未保存通过：{exc}")
        if apply_corrections and collection_names:
            from .story_settings import has_setting_content
            current = service.context_payload(chapter_no=latest + 1, actor=role, max_chars=64000)
            for collection in current["collections"]:
                if collection["name"] in collection_names and not any(
                    has_setting_content(record.get("values")) and record.get("status") == "verified_reference"
                    for record in collection["records"]
                ):
                    pending.append(f"《{collection['name']}》仍缺可核对的具体记录，分类已保留，但内容补齐尚未完成")
        status = "needs_input" if report.needs_choice else "needs_evidence" if pending else "reviewed"
        if changes:
            status = "corrected" if (changes.get("verification") or {}).get("status") == "passed" else "needs_evidence"
        if pending:
            status = "needs_evidence"
        if report.needs_choice or report.questions:
            status = "needs_input"
        summary = report.summary
        if changes:
            summary += "\n" + ("已同步过期的书籍要求，并核对了当前规划。" if (changes.get("verification") or {}).get("status") == "passed" else "书籍要求已保存修订，仍需完成其与当前规划的核对。")
        elif report.rule_corrections:
            summary += "\n" + ("当前书籍要求已经与这份建议一致，没有重复保存或增加版本。" if rules_unchanged else "已找到需要同步的要求，本次尚未写回。")
        if adopted:
            summary += "\n已核对可用的设定：" + "、".join(item["title"] for item in adopted) + "；每条都有来源、原句和版本。"
        if pending:
            summary += "\n" + "；".join(pending)
        for item in payload["coverage"]:
            item["stored_record_count"] = len(service.records(item["collection_id"]))
            item["adopted_record_count"] = sum(record["collection_id"] == item["collection_id"] and record["changed"] for record in adopted)
        trace.finish(summary=summary)
        return {"status": status, "summary": summary, "findings": report.findings,
                "needs_choice": report.needs_choice, "changes": changes, "adopted_settings": adopted, "pending_settings": pending,
                "questions": [question.model_dump(mode="json") for question in report.questions],
                "coverage": {"documents": list(sources), "canonical_chapters": canon,
                             "settings": payload["coverage"]}, "trace_id": trace.run_id}
    except Exception as exc:
        trace.finish(status="failed", summary=str(exc))
        raise
