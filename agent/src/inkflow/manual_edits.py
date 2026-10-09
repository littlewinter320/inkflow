"""Hash-bound manual edit detection and review; never imports text into canon."""
from __future__ import annotations
import asyncio
import os
import json
import re
from pathlib import Path
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field
from .errors import ValidationGateError
from .project_lock import project_write_lock_sync, _pid_alive
from .studio import StudioService
from .utils import SOURCE_RECOVERY_TOKEN_LIMIT, content_hash, utc_now

def _book_brief_from_document(text):
    """Import only the existing BOOK projection format; ambiguous Markdown stays pending."""
    from .schemas import BookBrief
    def field(label):
        matches = re.findall(r"(?m)^- " + re.escape(label) + r"：(.*)$", text)
        if len(matches) != 1:
            raise ValidationGateError("BOOK.md字段不能唯一定位，请保留项目契约格式或在对话中明确需要同步的设定。")
        return matches[0].strip()
    title = re.findall(r"(?m)^# (.+)$", text)
    sections = re.fullmatch(r".*?\n## 故事前提\s*\n(.*?)\n## 用户规则\s*\n(.*)", text, re.S)
    scale = re.fullmatch(r"(\d+) 卷 / (\d+) 章", field("预计规模"))
    words = re.fullmatch(r"(\d+) 字", field("单章目标"))
    if (len(title) != 1 or not sections or not scale or not words
            or len(re.findall(r"(?m)^## 故事前提\s*$", text)) != 1
            or len(re.findall(r"(?m)^## 用户规则\s*$", text)) != 1):
        raise ValidationGateError("BOOK.md结构无法无歧义同步；原文已保留，请明确字段后再导入。")
    if any(line.strip() and not line.startswith("- ") and not line.startswith("> 本文件是数据库的人类可读投影。") for line in sections[2].splitlines()):
        raise ValidationGateError("用户规则中含未识别的续行，未省略或猜测规则；请按独立列表项明确后再同步。")
    rules = [line[2:].strip() for line in sections[2].splitlines() if line.startswith("- ") and line != "- 暂无额外规则"]
    selling = field("核心卖点")
    return BookBrief(title=title[0], genre=field("题材"), target_audience=field("目标读者"),
        protagonist=field("主角"), core_selling_point="" if selling == "待规划时细化" else selling,
        premise=sections[1].strip(), user_rules=rules, target_chapter_words=int(words[1]),
        estimated_volumes=int(scale[1]), estimated_chapters=int(scale[2]))

class ManualEditIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1600)
    quote: str = Field(default="", max_length=2000)
    source_quote: str = Field(default="", max_length=2000)
    source_path: str = Field(default="", max_length=300)
    hard_conflict: bool = False

class RecoveredSettingEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_kind: Literal["canonical"] = "canonical"
    chapter_no: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=2000)
    relation: Literal["support", "counter", "belief", "reference"] = "support"

class ManualEditReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: str = Field(pattern="^(pass|needs_user|insufficient_context)$")
    summary: str = Field(min_length=1, max_length=2000)
    issues: list[ManualEditIssue] = Field(default_factory=list, max_length=12)
    planning_decision: str = Field(default="not_applicable", pattern="^(compatible|needs_update|not_applicable|insufficient_context)$")
    planning_quotes: list[str] = Field(default_factory=list, max_length=8)
    recovered_evidence: list[RecoveredSettingEvidence] = Field(default_factory=list, max_length=8)

class ManualEditsService:
    def __init__(self, project):
        self.project = project
        self.studio = StudioService(project)

    def jobs(self):
        value = self.project.db.get_metadata("manual_edit_jobs", {})
        return value if isinstance(value, dict) else {}

    def status(self):
        jobs = sorted(self.jobs().values(), key=lambda item: item["updated_at"], reverse=True)
        return {"jobs": jobs, "issues": [item for item in jobs if item["status"] in
                {"awaiting_confirmation", "needs_evidence", "failed", "revision_requested"}]}

    def enqueue(self, relative_path, content, *, previous_hash="", kind="document", chapter_no=None, applied=True, record_revision=None):
        relative_path = str(relative_path).replace("\\", "/")
        if kind == "setting_record":
            data = json.loads(content)
            content = json.dumps({key:value for key,value in data.items() if key not in {"history","updated_at","changed","entry_status"}},ensure_ascii=False,sort_keys=True)
        current_hash = content_hash(content)
        with project_write_lock_sync(self.project.root):
            jobs = self.jobs()
            heads = self.project.db.get_metadata("manual_edit_heads", {}) or {}
            current = jobs.get(heads.get(relative_path, ""))
            if current and current["current_hash"] == current_hash and current["status"] not in {"superseded", "discarded"}:
                return current
            for item in jobs.values():
                if item["relative_path"] == relative_path and item["status"] not in {"superseded", "discarded"}:
                    item["status"] = "superseded"
            item = {"job_id": f"manual-{uuid4().hex}", "relative_path": relative_path,
                    "current_hash": current_hash, "previous_hash": previous_hash, "kind": kind,
                    "chapter_no": chapter_no, "status": "pending", "updated_at": utc_now(),
                    "applied": applied, "expected_visible_hash": current_hash if applied else previous_hash,
                    "record_revision": record_revision,
                    "attempts": 0, "summary": "手动内容已变化，等待同版本审查；未修改正史。", "issues": []}
            artifact = self.project.db.save_agent_artifact(artifact_type="manual_edit_candidate",
                run_id=item["job_id"], role="user", chapter_no=chapter_no,
                data={"content": content, "relative_path": relative_path, "content_hash": current_hash},
                status="pending_review")
            item["artifact_id"] = artifact["artifact_id"]
            jobs[item["job_id"]] = item
            self.project.db.set_metadata("manual_edit_heads", {**heads, relative_path: item["job_id"]})
            self.project.db.set_metadata("manual_edit_jobs", jobs)
            return item

    def scan(self):
        """Hash only: no change means no review or new version; engine output is already indexed."""
        with project_write_lock_sync(self.project.root):
            jobs = self.jobs()
            for job_id, job in jobs.items():
                if job["status"] == "reviewing" and not _pid_alive(int(job.get("review_owner_pid") or 0)):
                    jobs[job_id] = {**job, "status":"failed", "summary":"后台核对被中断，候选已保留，请查看断点后再决定续核。"}
            from .config import Settings
            if Settings.from_env(self.project.root).manual_edit_review_enabled:
                for job_id, job in jobs.items():
                    if job["status"] == "paused":
                        jobs[job_id] = {**job, "status": "pending", "updated_at": utc_now()}
            self.project.db.set_metadata("manual_edit_jobs",jobs)
            known = self.project.db.get_metadata("manual_document_hashes", {})
            known = known if isinstance(known, dict) else {}
            chapters = {}
            with self.project.db.connect() as connection:
                rows = connection.execute("SELECT chapter_no,status,path,content_hash FROM chapters WHERE status IN ('draft','accepted')").fetchall()
                chapters = {str(row["path"]): dict(row) for row in rows}
            paths = {name for name in ("BOOK.md", "PLAN.md", "OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md")
                     if (self.project.root / name).is_file()} | set(chapters)
            # ponytail: managed files only; imported chapter drafts enter through document.save.
            created = []
            for relative in sorted(paths):
                path = self.project.resolve_user_path(relative)
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                actual = content_hash(text)
                chapter = chapters.get(relative)
                baseline = str(chapter["content_hash"]) if chapter else known.get(relative)
                if baseline is None and relative == "BOOK.md":
                    from .project import render_book_brief
                    baseline = content_hash(render_book_brief(self.project.db.get_brief()))
                if baseline is None and relative == "PLAN.md":
                    baseline = self.project.db.get_metadata("current_plan_text_hash")
                if baseline is None and relative in {"OUTLINE.md","STORY_DETAIL.md","RECENT_PLAN.md"}:
                    manifest_path=self.project.root/"planning"/"active-v2.json"
                    if manifest_path.is_file():
                        manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
                        key={"OUTLINE.md":"outline_hash","STORY_DETAIL.md":"volume_detail_hash","RECENT_PLAN.md":"recent_plan_hash"}[relative]
                        baseline=manifest.get(key)
                if baseline is None:
                    known[relative] = actual
                    continue
                if chapter and chapter["status"] == "accepted" and actual == baseline:
                    jobs = self.jobs()
                    for job_id, job in jobs.items():
                        if job["relative_path"] == relative and job.get("applied") and job["current_hash"] != actual:
                            jobs[job_id] = {**job, "status": "superseded", "summary": "可见正文已恢复原正史，旧外部候选不再使用。"}
                    self.project.db.set_metadata("manual_edit_jobs", jobs)
                    known[relative] = actual
                if actual != baseline:
                    job = self.enqueue(relative, text, previous_hash=baseline,
                        kind="accepted" if chapter and chapter["status"] == "accepted" else
                        "draft" if chapter else "setting_document",
                        chapter_no=int(chapter["chapter_no"]) if chapter else None)
                    if job["status"] == "pending":
                        created.append(job)
                    if chapter and chapter["status"] == "draft":
                        from .studio import _title_from_document
                        self.project.db.upsert_draft(int(chapter["chapter_no"]),
                            _title_from_document(text, int(chapter["chapter_no"])), relative, text)
                    known[relative] = actual
            self.project.db.set_metadata("manual_document_hashes", known)
            return {"changed": bool(created), "jobs": created, **self.status()}

    def mark_saved(self, relative, content, old_hash, *, source="desktop_manual", applied=True):
        if content_hash(content) == old_hash:
            return None
        kind = "setting_document"
        from .studio import _chapter_number_from_path
        chapter_no = _chapter_number_from_path(relative)
        chapter = self.project.db.get_chapter(chapter_no) if chapter_no else None
        if chapter:
            kind = "accepted" if chapter["status"] == "accepted" else "draft"
        job = self.enqueue(relative, content, previous_hash=old_hash, kind=kind, chapter_no=chapter_no, applied=applied)
        if applied:
            known = self.project.db.get_metadata("manual_document_hashes", {}) or {}
            self.project.db.set_metadata("manual_document_hashes", {**known, relative: content_hash(content)})
        return job

    def note_engine_write(self, relative, content):
        known = self.project.db.get_metadata("manual_document_hashes", {}) or {}
        actual = content_hash(content)
        self.project.db.set_metadata("manual_document_hashes", {**known, relative: actual})
        jobs = self.jobs()
        for key, job in jobs.items():
            if job["relative_path"] == relative and job.get("applied") and job["current_hash"] != actual and job["status"] not in {"discarded", "superseded", "reconciled"}:
                jobs[key] = {**job, "status": "superseded", "summary": "授权流程已写入新版本；旧手动候选保留历史，不作为新内容的审查结论。", "updated_at": utc_now()}
        self.project.db.set_metadata("manual_edit_jobs", jobs)

    def collection_changed(self, previous, current):
        if not previous or not current.get("changed") or not any(previous.get(key) != current.get(key)
                for key in ("instructions", "fields", "read_roles", "write_roles")):
            return
        from .story_settings import StorySettingsService
        for record in StorySettingsService(self.project).records(current["collection_id"]):
            self.enqueue("story-setting:" + record["record_id"],
                json.dumps({**record, "review_collection_revision": current["revision"]}, ensure_ascii=False),
                kind="setting_record", chapter_no=record.get("source_chapter"), record_revision=record["revision"])

    def reconcile_planning(self, reviewed_sources, run_id):
        """Only an approved publication based on this exact manual edit resolves its plan gap."""
        with project_write_lock_sync(self.project.root):
            jobs = self.jobs()
            for key, job in jobs.items():
                if (job["kind"] == "setting_document" and (job.get("planning_gap") or job["status"] == "pending")
                        and job["status"] not in {"discarded", "superseded", "reviewing"}
                        and not job.get("issues") and reviewed_sources.get(job["relative_path"]) == job["current_hash"]):
                    jobs[key] = {**job, "status": "reconciled", "updated_at": utc_now(),
                        "summary": "基于这份手动版本的新规划已审核并正式发布；原核对记录保留。",
                        "resolution": {"type": "reviewed_planning_publication", "run_id": run_id}}
            self.project.db.set_metadata("manual_edit_jobs", jobs)

    def assert_planning_ready(self, chapter_no):
        if chapter_no <= self.project.db.latest_accepted_chapter_no():
            return
        impact = self.project.db.planning_source_impact()
        if not impact["changed_sources"] and not impact["plan_file_changed"]:
            return
        clearance = self.project.db.get_metadata("manual_planning_clearance", {}) or {}
        plan = self.project.db.get_current_plan_bundle()
        if (not impact["plan_file_changed"] and clearance.get("sources") == self.project.db.planning_source_hashes()
                and plan and clearance.get("plan_hash") == content_hash(plan.model_dump_json())):
            return
        raise ValidationGateError("设定或规划文本已经变化，旧章节卡尚未按当前依据核对。请查看后台疑问或定向重新规划，已接受正文不受影响。")

    def _content(self, job):
        with self.project.db.connect() as connection:
            artifact = connection.execute(
                "SELECT data_json FROM agent_artifacts WHERE artifact_id=? AND artifact_type='manual_edit_candidate'",
                (job["artifact_id"],)).fetchone()
        if artifact is None:
            raise ValidationGateError("手动编辑候选缺失，不能按当前文件猜测原候选。")
        data = json.loads(artifact["data_json"])
        if data["content_hash"] != job["current_hash"] or content_hash(data["content"]) != job["current_hash"]:
            raise ValidationGateError("手动候选内容与版本绑定不一致，未发送审查。")
        return str(data["content"])

    def source_hash(self, job):
        from .engine import InkFlowEngine
        boundary = int(job.get("chapter_no") or self.project.db.latest_accepted_chapter_no() + 1)
        return InkFlowEngine._planning_source_fingerprint(self.project, boundary, boundary)

    async def review(self, job_id, engine, *, resume=False):
        from .config import Settings
        from .task_settings import active_task_settings
        job = self.jobs().get(job_id)
        if not job:
            raise ValidationGateError("找不到这次手动编辑。")
        if resume and job["status"] == "failed" and int(job.get("attempts", 0)) < 2:
            with project_write_lock_sync(self.project.root):
                jobs = self.jobs()
                current = jobs.get(job_id)
                if current and current["status"] == "failed" and current["current_hash"] == job["current_hash"]:
                    jobs[job_id] = {**current, "status": "pending", "updated_at": utc_now()}
                    self.project.db.set_metadata("manual_edit_jobs", jobs)
                    job = jobs[job_id]
        if job["status"] != "pending":
            return job
        if not Settings.from_env(self.project.root).manual_edit_review_enabled:
            return {**self.jobs().get(job_id, {}), "auto_review_disabled": True}
        original_scope = active_task_settings.get()
        run_id = "manual-audit-" + job_id
        try:
            scope = self.studio.db.prepare_task_settings(run_id, novel_id=self.project.project_id,
                settings=lambda: engine.settings, workspace_root=self.project.root,
                collaboration_mode=None if job.get("task_id") else original_scope.collaboration_mode if original_scope else "everyday")
        except Exception as exc:
            with project_write_lock_sync(self.project.root):
                jobs = self.jobs()
                if jobs.get(job_id, {}).get("status") == "pending":
                    jobs[job_id] = {**jobs[job_id], "status": "failed", "summary": "配置断点无法恢复：" + str(exc)[:1500], "updated_at": utc_now()}
                    self.project.db.set_metadata("manual_edit_jobs", jobs)
            raise
        token = active_task_settings.set(scope)
        try:
            if engine.settings.to_mapping() != scope.settings.to_mapping():
                from .engine import InkFlowEngine
                from .provider import create_provider
                engine = InkFlowEngine(create_provider(scope.settings), scope.settings)
            return await self._review(job_id, engine)
        finally:
            active_task_settings.reset(token)

    async def _review(self, job_id, engine):
        from .role_protocol import check_owners_for_mode
        from .task_settings import active_task_settings
        jobs = self.jobs()
        job = jobs.get(job_id)
        if not job:
            raise ValidationGateError("找不到这次手动编辑。")
        if job["status"] != "pending":
            return job
        if not engine.settings.manual_edit_review_enabled:
            return {**job, "auto_review_disabled": True}
        with project_write_lock_sync(self.project.root):
            jobs = self.jobs()
            job = jobs[job_id]
            if job["status"] != "pending":
                return job
            if int(job.get("attempts", 0)) >= 2:
                jobs[job_id] = {**job, "status": "needs_evidence", "summary": "同一候选的两次核对机会已用完，请修改具体内容或补充可定位来源后再处理。"}
                self.project.db.set_metadata("manual_edit_jobs", jobs)
                return jobs[job_id]
            scope = active_task_settings.get()
            job = {**job, "status": "reviewing", "review_owner_pid":os.getpid(), "attempts": int(job.get("attempts",0))+1,
                "task_id": scope.task_id, "snapshot_hash": scope.snapshot_hash, "updated_at": utc_now()}
            jobs[job_id] = job
            self.project.db.set_metadata("manual_edit_jobs", jobs)
        try:
            content = self._content(job)
            parsed_brief = _book_brief_from_document(content) if job["relative_path"] == "BOOK.md" else None
            if job["kind"] == "setting_record":
                from .story_settings import StorySettingsService
                scope = active_task_settings.get()
                owners = check_owners_for_mode(scope.collaboration_mode if scope else "everyday")
                role = owners["memory"] if job["kind"] == "setting_record" else owners.get("general") or owners.get("logic_continuity")
                canonical_role = role
                record_id = job["relative_path"].split(":", 1)[1]
                collection = next((item for item in StorySettingsService(self.project).list_collections()
                    if any(record["record_id"] == record_id for record in item["records"])), None)
                record = next((item for item in (collection or {}).get("records", []) if item["record_id"] == record_id), None)
                if not record or record["revision"] != job["record_revision"]:
                    raise ValidationGateError("设定记录在核对开始前已变化，未发送旧候选。")
                if canonical_role not in collection["read_roles"]:
                    raise ValidationGateError("当前审核角色没有读取这份设定的权限；请明确调整权限或选择已启用的审核职责。")
            if job["kind"] != "setting_record":
                visible=self.project.resolve_user_path(job["relative_path"])
                if not visible.is_file() or content_hash(visible.read_text(encoding="utf-8")) != job["expected_visible_hash"]:
                    raise ValidationGateError("候选对应的文件版本已变化，未消耗模型重审旧候选。")
            source_hash = self.source_hash(job)
            if job["kind"] == "draft":
                result = await engine.review_chapter(self.project.root, int(job["chapter_no"]))
                verdict = result.get("verdict")
                issues = result.get("issues", result.get("findings", []))
                outcome = "passed" if verdict == "pass" else "needs_evidence" if verdict in {"insufficient_context", "unknown"} else "awaiting_confirmation"
                summary = "手动草稿同版审查通过；仍须接受授权和原有提交门禁。" if outcome == "passed" else "手动草稿审查有疑问，请查看正文与审核依据。"
            else:
                boundary = int(job.get("chapter_no") or self.project.db.latest_accepted_chapter_no()+1)
                from .schemas import ContextPacket, ContextSection
                from .story_settings import StorySettingsService
                accepted = self.project.db.accepted_chapters()
                recent = [{"path": ch["path"], "chapter_no":ch["chapter_no"],
                           "text": (self.project.db.canonical_chapter_content(int(ch["chapter_no"])) or "")[:10000]}
                          for ch in accepted[-3:]]
                scope = active_task_settings.get()
                owners = check_owners_for_mode(scope.collaboration_mode if scope else "everyday")
                role = owners["memory"] if job["kind"] == "setting_record" else owners.get("general") or owners.get("logic_continuity")
                canonical_role = role
                sources = json.dumps({"book":self.project.db.get_brief().model_dump(mode="json"),
                    "facts":self.project.db.current_facts()[:128],"threads":self.project.db.open_threads()[:64],
                    "recent_canon":recent},ensure_ascii=False)
                packet = ContextPacket(project_id=self.project.project_id,chapter_no=boundary,task="手动修改核对",
                    sections=[ContextSection(key="CANON",title="公开正史与用户约定",content=sources,hard=True),
                        ContextSection(key="SET",title="设定合集：非正史",
                        content=StorySettingsService(self.project).context(chapter_no=boundary,actor=canonical_role),hard=False)],
                    estimated_tokens=0)
                plan_bundle = self.project.db.get_current_plan_bundle()
                plan_text = plan_bundle.model_dump_json() if plan_bundle else ""
                planning_required = job["kind"] == "setting_document" and bool(plan_bundle)
                structural_gap = planning_required and (job["relative_path"] in {"PLAN.md", "RECENT_PLAN.md"}
                    or (job["relative_path"] in {"OUTLINE.md", "STORY_DETAIL.md"}
                        and (self.project.root / "planning" / "active-v2.json").is_file()))
                if planning_required:
                    packet.sections.append(ContextSection(key="PLANNING", title="当前结构化规划：须核对新修改的兼容性",
                        content=plan_text, hard=True))
                from .utils import estimate_tokens
                from .retrieval import HybridRetriever
                queries = [str(job.get("user_reason") or "")[:160]]
                if job["kind"] == "setting_record":
                    candidate_data = json.loads(content)
                    queries.append(str(candidate_data.get("title") or "")[:160])
                    queries.extend(str(value)[:160] for value in list(candidate_data.get("values", {}).values())[:3])
                else:
                    queries.append(content[:160])
                recovered, retrieval_trace = HybridRetriever(self.project).recover_review_sources(
                    queries[:4], chapter_no=boundary)
                included = []
                remaining = min(SOURCE_RECOVERY_TOKEN_LIMIT, max(0, engine.settings.context_budget_for(
                    role)[1] -
                    estimate_tokens(packet.to_model_prompt() + content) - 5000))
                for candidate in recovered[:6]:
                    body = candidate["content"]
                    parts = [{"source_id": str(offset), "title": "", "body": body[offset:offset+5000]}
                        for offset in range(0, len(body), 1000)]
                    ranking = HybridRetriever(self.project)._bm25_ranking(" ".join(queries[:4]), parts)
                    start = int(ranking[0][0]) if ranking else 0
                    candidate = {**candidate, "content": body[start:start+5000], "start": start,
                        "end": min(start+5000, len(body)), "excerpt_only": True}
                    text = json.dumps(candidate, ensure_ascii=False)
                    tokens = estimate_tokens(text)
                    if tokens <= remaining:
                        included.append(candidate)
                        remaining -= tokens
                if included:
                    packet.sections.append(ContextSection(key="RECOVERED", title="本地补读线索：需语义核对",
                        content=json.dumps(included, ensure_ascii=False), hard=False))
                job["retrieval"] = {**retrieval_trace, "included_count": len(included),
                                    "limit_queries": 4, "limit_sources": 6, "max_tokens": SOURCE_RECOVERY_TOKEN_LIMIT}
                if estimate_tokens(packet.to_model_prompt()+content)>max(1000,engine.settings.context_budget_for(role)[1]-5000):
                    raise ValidationGateError("手动候选与核对来源超过当前上下文预算，请拆分需要核对的修改范围。")
                scope = active_task_settings.get()
                owners = check_owners_for_mode(scope.collaboration_mode if scope else "everyday")
                role = owners["memory"] if job["kind"] == "setting_record" else owners.get("general") or owners.get("logic_continuity")
                result = await engine.provider.generate_json(system_prompt=(
                    "你审核用户手动变更。设定是可调整假设与证据参考，不是正史；合理新增、人物信念和谎言不算硬矛盾。"
                    "不得修改文本。pass仅表示参考内容可用，不授权正文或记忆提交。疑似硬矛盾必须给候选原句、"
                    "排他正史原句和来源路径；引文不足给insufficient_context。用户以前同意过也不能消除新发现的矛盾，需再次询问具体取舍。"
                    "存在PLANNING时另核对新修改与当前全书方向、卷级因果及未来章节卡是否相容，返回planning_decision。"
                    "compatible必须给至少一条当前结构化规划中的连续原句planning_quotes及理由；不能仅因正文无矛盾就说规划适用。"
                    "资料不够给insufficient_context；需调整给needs_update，不能自行修改规划或把设想变成事实。"
                    "若设定记录缺引用，先利用已补读正史逐句核对，确有语义支持则用recovered_evidence返回章号、连续原句及关系，"
                    "把客观支持、信念、反证和参考分开，不为填空制造证据。无支持时保留缺口，合理新设想不要求旧章已经发生。"),
                    user_prompt=packet.to_model_prompt()+"\n# 用户对疑问的解释（不自动覆盖正史）\n"+str(job.get("user_reason") or "未提供")+"\n# 手动修改候选\n"+content,
                    output_model=ManualEditReview, max_tokens=min(5000,engine.settings.max_output_tokens),
                    thinking=False, agent_role=role)
                report = result.data
                issues = [item.model_dump() for item in report.issues]
                insufficient = False
                for issue in issues:
                    if not issue["quote"] or issue["quote"] not in content:
                        insufficient = True
                    if issue["hard_conflict"]:
                        path = issue["source_path"]
                        canonical = next((self.project.db.canonical_chapter_content(int(ch["chapter_no"]))
                            for ch in self.project.db.accepted_chapters() if ch["path"] == path
                            and content_hash(self.project.db.canonical_chapter_content(int(ch["chapter_no"])) or "") == ch["content_hash"]), None)
                        if not canonical or not issue["source_quote"] or issue["source_quote"] not in canonical:
                            issue["hard_conflict"] = False
                            insufficient = True
                outcome = "needs_evidence" if insufficient or report.verdict == "insufficient_context" else "passed" if report.verdict == "pass" and not issues else "awaiting_confirmation"
                planning_gap = planning_required and (structural_gap or report.planning_decision != "compatible"
                    or not report.planning_quotes or any(not quote.strip() or quote not in plan_text for quote in report.planning_quotes))
                job["planning_gap"] = planning_gap
                job["planning_decision"] = report.planning_decision
                if planning_gap and outcome == "passed":
                    outcome = "needs_evidence"
                if job["kind"] == "accepted" and outcome == "passed":
                    outcome = "awaiting_confirmation"
                summary = report.summary
                if planning_gap:
                    summary += "；当前规划仍须按这份修改核对或重新发布，旧章节卡未自动更新。"
            with project_write_lock_sync(self.project.root):
                jobs = self.jobs()
                current = jobs.get(job_id)
                visible_valid = True
                if job["kind"] == "setting_record":
                    from .story_settings import StorySettingsService
                    records = [record for collection in StorySettingsService(self.project).list_collections() for record in collection["records"]]
                    record = next((record for record in records if record["record_id"] == job["relative_path"].split(":",1)[1]), None)
                    visible_valid = bool(record and record["revision"] == job["record_revision"])
                else:
                    path = self.project.resolve_user_path(job["relative_path"])
                    visible_valid = path.is_file() and content_hash(path.read_text(encoding="utf-8")) == job["expected_visible_hash"]
                if not current or current["status"] != "reviewing" or not visible_valid or self.source_hash(job) != source_hash:
                    if current and current["status"] == "reviewing":
                        jobs[job_id] = {**current, "status": "needs_evidence", "summary": "审查期间来源变化，结果未用于当前内容。"}
                        self.project.db.set_metadata("manual_edit_jobs", jobs)
                    return jobs.get(job_id)
                if parsed_brief and report.verdict == "pass" and not issues and not insufficient:
                    self.project.db.set_brief(parsed_brief)
                if job["kind"] == "setting_document" and outcome == "passed" and planning_required:
                    self.project.db.set_metadata("manual_planning_clearance", {
                        "sources": self.project.db.planning_source_hashes(), "plan_hash": content_hash(plan_text),
                        "job_id": job_id, "decision": "compatible", "quotes": report.planning_quotes})
                reviewed_revision = job.get("record_revision")
                if job["kind"] == "setting_record":
                    from .story_settings import StorySettingsService
                    settings_service = StorySettingsService(self.project)
                    if report.verdict == "pass" and not issues and report.recovered_evidence:
                        try:
                            record = settings_service.save_record(record["collection_id"], record_id=record["record_id"],
                                title=record["title"], values=record["values"], epistemic_status=record["epistemic_status"],
                                evidence_refs=[*record["evidence_refs"], *[ref.model_dump() for ref in report.recovered_evidence]],
                                expected_revision=job["record_revision"], actor=canonical_role, operation="supplement",
                                chapter_no=record.get("source_chapter"))
                            job["record_revision"] = record["revision"]
                        except Exception as exc:
                            outcome = "needs_evidence"
                            summary += "；补证未写入：" + str(exc)[:500]
                    if outcome == "passed" and record["epistemic_status"] in {"objective", "belief", "rumor"} and not record["evidence_refs"]:
                        outcome = "needs_evidence"
                        summary = "候选已保留，检索线索尚未形成可定位的原文引用；请补依据或明确保留为设想。"
                    reviewed_record = settings_service.record_review(job["relative_path"].split(":",1)[1],
                        expected_revision=job["record_revision"], actor=canonical_role,
                        decision="verified_reference" if outcome == "passed" else "needs_evidence" if outcome == "needs_evidence" else "needs_user",
                        issues=issues, source_fingerprint=settings_service.source_fingerprint())
                    reviewed_revision = reviewed_record["revision"]
                jobs[job_id] = {**current,"status":outcome,"summary":summary,"issues":issues,
                    "source_hash":source_hash,"record_revision":reviewed_revision,"retrieval":job.get("retrieval",{}),
                    "planning_gap":job.get("planning_gap",False),"planning_decision":job.get("planning_decision"),
                    "updated_at":utc_now(),"canon_committed":False}
                self.project.db.set_metadata("manual_edit_jobs",jobs)
                return jobs[job_id]
        except (Exception, asyncio.CancelledError) as exc:
            with project_write_lock_sync(self.project.root):
                jobs=self.jobs()
                current=jobs.get(job_id)
                if current and current["status"]=="reviewing":
                    from .config import Settings
                    paused = isinstance(exc, asyncio.CancelledError) and not Settings.from_env(self.project.root).manual_edit_review_enabled
                    jobs[job_id]={**current,"status":"paused" if paused else "failed",
                        "summary":"后台核对已按设置暂停；候选及尝试记录保留。" if paused else str(exc)[:1600] or "后台核对被中断，候选已保留。",
                        "updated_at":utc_now()}
                    self.project.db.set_metadata("manual_edit_jobs",jobs)
            raise

    def decide(self, job_id, decision, reason, expected_hash):
        with project_write_lock_sync(self.project.root):
            jobs=self.jobs()
            job=jobs.get(job_id)
            if not job or job["current_hash"]!=expected_hash or job["status"] in {"superseded","reviewing"}:
                raise ValidationGateError("手动变更版本已变化，请重新查看疑问再选择。")
            if decision not in {"keep_pending","request_revision","confirm_exception","discard_candidate"}:
                raise ValidationGateError("不支持的手动变更选择。")
            if decision=="confirm_exception" and not reason.strip():
                raise ValidationGateError("请说明这是创作改设定、人物信念，还是审核误读；简单同意不能改写正史。")
            if decision == "discard_candidate" and job["kind"] == "setting_record":
                from .story_settings import StorySettingsService
                StorySettingsService(self.project).archive_record(job["relative_path"].split(":", 1)[1],
                    expected_revision=job["record_revision"])
            if decision == "discard_candidate" and job.get("applied") and job["kind"] != "setting_record":
                path=self.project.resolve_user_path(job["relative_path"])
                if not path.is_file() or content_hash(path.read_text(encoding="utf-8"))!=job["current_hash"]:
                    raise ValidationGateError("文件又有新改动，不会用旧决定覆盖；请重新查看当前候选。")
                previous=(self.project.db.canonical_chapter_content(int(job["chapter_no"]))
                          if job["kind"]=="accepted" else None)
                if previous is None:
                    version=next((item for item in self.studio.db.list_versions(job["relative_path"],200)
                                  if item["content_hash"]==job["previous_hash"]),None)
                    previous=self.studio.db.get_version(version["version_id"])["content"] if version else None
                if previous is None:
                    raise ValidationGateError("没有可核对的修改前版本，候选已保留；请明确恢复内容后再处理。")
                from .utils import atomic_write_text
                atomic_write_text(path,previous)
                if job["kind"]=="draft":
                    from .studio import _title_from_document
                    self.project.db.upsert_draft(int(job["chapter_no"]),_title_from_document(previous,int(job["chapter_no"])),job["relative_path"],previous)
                self.note_engine_write(job["relative_path"],previous)
            status={"keep_pending":"awaiting_confirmation","request_revision":"revision_requested",
                    "confirm_exception":"awaiting_confirmation","discard_candidate":"discarded"}[decision]
            if decision == "confirm_exception" and int(job.get("attempts",0)) < 2:
                status = "pending"
            job={**job,"status":status,"user_decision":decision,"user_reason":reason[:2000],
                 "updated_at":utc_now(),"canon_committed":False,
                 "next_action":"解释已留存；修改候选或明确正史修订范围后，从受影响节点复核。"}
            jobs[job_id]=job
            self.project.db.set_metadata("manual_edit_jobs",jobs)
            return job

    def supersede(self, paths, reason="对应对象已移出使用，旧候选只留历史。"):
        with project_write_lock_sync(self.project.root):
            jobs=self.jobs()
            for key, job in jobs.items():
                if job["relative_path"] in paths and job["status"] not in {"discarded","superseded"}:
                    jobs[key]={**job,"status":"superseded","summary":reason,"updated_at":utc_now()}
            self.project.db.set_metadata("manual_edit_jobs",jobs)

    def acknowledge_hypothesis(self, record_id, reason):
        with project_write_lock_sync(self.project.root):
            jobs = self.jobs()
            for key, job in jobs.items():
                if job["relative_path"] == "story-setting:" + record_id and job["status"] not in {"superseded", "discarded"}:
                    jobs[key] = {**job, "status": "acknowledged_hypothesis", "user_reason": reason,
                        "summary": "用户明确保留为创作设想；原审查与疑问留存，不代表正史或审核通过。", "updated_at": utc_now()}
            self.project.db.set_metadata("manual_edit_jobs", jobs)

    def assert_ready(self, chapter_no):
        from .config import Settings
        from .task_settings import active_task_settings
        scope = active_task_settings.get()
        settings = scope.settings if scope else Settings.from_env(self.project.root)
        if not settings.manual_edit_review_enabled:
            return
        for job in self.jobs().values():
            setting_hard = job["kind"]=="setting_record" and any(issue.get("hard_conflict") for issue in job.get("issues",[]) if isinstance(issue,dict))
            relevant=job["kind"]=="setting_document" or setting_hard or (job.get("chapter_no") and int(job["chapter_no"])<=chapter_no)
            if relevant and job["status"] in {"pending","reviewing","awaiting_confirmation","needs_evidence","failed","revision_requested"}:
                raise ValidationGateError(f"手动修改仍待核对：{job['relative_path']}。原文件和正史已保留，请查看后台核对窗口。")
