"""为缺少小纲的写作章节保存可追溯的临时目标。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import ValidationGateError
from .schemas import ArcPlan, ArcSummary, BookOutlineV2, BookPlan, ChapterCard, PlanBundle, VolumeCompass, VolumePlan
from .task_settings import active_task_settings
from .utils import content_hash, json_dumps, utc_now

if TYPE_CHECKING:
    from .project import InkFlowProject


def _sources(project: InkFlowProject, through: int) -> dict:
    """绑定准备目标时已有的正史前缀，后续写成的章节由正文工作流独立核对。"""
    return {
        "documents": {name: content_hash((project.root / name).read_text(encoding="utf-8"))
                      if (project.root / name).is_file() else None
                      for name in ("BOOK.md", "OUTLINE.md", "STORY_DETAIL.md", "planning/active-v2.json")},
        "prior": [[row["chapter_no"], row["version"], row["content_hash"]]
                  for row in project.db.accepted_chapters() if int(row["chapter_no"]) <= through],
    }


def temporary_goal_bundle(project: InkFlowProject, chapter_no: int) -> PlanBundle | None:
    """恢复原任务保存的临时目标；来源或目标被改动时保留断点，禁止悄悄换卡。"""
    with project.db.connect() as connection:
        row = connection.execute("SELECT parent_key FROM plans WHERE kind='chapter' AND plan_key=? AND status='active'",
                                 (f"chapter:{chapter_no:05d}",)).fetchone()
    parent = str(row["parent_key"] or "") if row else ""
    # 正式章节卡仍沿用现有规划路径，只有明确标记的临时卡进入这里。
    if not parent.startswith("task-goal:"):
        return None
    saved = project.db.get_metadata(parent)
    raw = project.db.get_plan("supplement", parent)
    # 旧的单章目标绑定至紧邻前章；批次先准备范围，之后逐章核对临时前文及记忆。
    through = saved.get("accepted_through", chapter_no - 1) if isinstance(saved, dict) else None
    if (not isinstance(saved, dict) or saved.get("project_id") != project.project_id
            or saved.get("chapter_no") != chapter_no or not saved.get("snapshot_hash")
            or saved.get("authority") != "task_temporary_not_formal_plan"
            or type(through) is not int or not 0 <= through < chapter_no
            or saved.get("sources") != _sources(project, through)
            or saved.get("bundle_hash") != content_hash(json_dumps(raw))):
        raise ValidationGateError("本章临时写作目标的来源已经变化，请先核对原目标与当前资料，未悄悄换用新目标。")
    # 元数据正确还不够，实际被章节索引引用的卡也必须与原目标完全一致。
    bundle = PlanBundle.model_validate(raw)
    card = project.db.get_chapter_card(chapter_no)
    if bundle.current_arc.chapter_cards[0].model_dump(mode="json") != card:
        raise ValidationGateError("本章临时写作目标与保存的章节依据不一致，未采用。")
    return bundle


def ensure_temporary_goals(project: InkFlowProject, chapters: list[int], instruction: str, *,
                           outline: BookOutlineV2 | None) -> None:
    """在项目锁内准备已授权的空白章节，批次后续章的剧情随各自的正文安排。"""
    if not chapters:
        return
    scope = active_task_settings.get()
    base = project.db.get_current_plan_bundle()
    if scope is None or scope.novel_id != project.project_id:
        raise ValidationGateError("本次写作缺少可确认的任务身份，请从原任务继续。")
    through = project.db.latest_accepted_chapter_no()
    source = _sources(project, through)
    # 批次在第一章审查前就保存参考卡，避免稍后添加相邻卡使同版审查的来源指纹失效。
    goals = []
    for chapter_no in chapters:
        previous = temporary_goal_bundle(project, chapter_no)
        if previous is not None:
            continue
        if chapter_no <= through or project.db.get_chapter(chapter_no):
            raise ValidationGateError(f"第 {chapter_no} 章已有稿件，须保留它原来的创作依据。")
        key = f"task-goal:{scope.task_id}:chapter:{chapter_no:05d}"
        bundle = _temporary_bundle(project, chapter_no, key, instruction, base, outline)
        raw = bundle.model_dump(mode="json")
        saved = {"authority": "task_temporary_not_formal_plan", "task_id": scope.task_id,
            "snapshot_hash": scope.snapshot_hash, "project_id": project.project_id, "chapter_no": chapter_no,
            "instruction": instruction, "sources": source, "bundle_hash": content_hash(json_dumps(raw)),
            "accepted_through": through, "previous_card": project.db.get_chapter_card(chapter_no)}
        goals.append((chapter_no, key, raw, saved))
    if not goals:
        return
    with project.db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if through != project.db.latest_accepted_chapter_no() or source != _sources(project, through):
            raise ValidationGateError("准备写作目标期间资料已变化，请按当前资料继续。")
        for chapter_no, key, raw, saved in goals:
            if connection.execute("SELECT 1 FROM chapters WHERE chapter_no=?", (chapter_no,)).fetchone():
                raise ValidationGateError(f"第 {chapter_no} 章已有新稿件，原稿保留。")
            connection.execute("INSERT INTO metadata(key,value_json,updated_at) VALUES (?,?,?)",
                               (key, json_dumps(saved), utc_now()))
            for kind, plan_key, parent, data in [("supplement", key, "task_temporary", raw),
                    ("chapter", f"chapter:{chapter_no:05d}", key, raw["current_arc"]["chapter_cards"][0])]:
                connection.execute("INSERT INTO plans(kind,plan_key,parent_key,data_json,updated_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(kind,plan_key) DO UPDATE SET parent_key=excluded.parent_key,"
                    "data_json=excluded.data_json,version=plans.version+1,status='active',updated_at=excluded.updated_at",
                    (kind, plan_key, parent, json_dumps(data), utc_now()))
        connection.commit()


def _temporary_bundle(project: InkFlowProject, chapter_no: int, key: str, instruction: str,
                      base: PlanBundle | None, outline: BookOutlineV2 | None) -> PlanBundle:
    """复用章节卡结构描述任务边界，具体情节由 Writer 随正文交付。"""
    boundary = (f"承接第{chapter_no - 1}章已经写出的结果；具体人物、物件与时间以有效前文为准。"
                if chapter_no > 1 else "依据本书简介和本次用户要求开篇，尚无已发生的前章事件。")
    pending = "由本次Writer依据前文、上层方向和用户要求安排，随正文交审；不是预先确定的剧情。"
    brief = project.db.get_brief()
    # 没有正式规划时只使用作者简介。为兼容现有合同填入的占位不代表已审定剧情。
    book = base.book if base else BookPlan(title=brief.title, premise=brief.premise,
        reader_promise=brief.core_selling_point or pending, narrative_engine=pending,
        main_conflict=pending, protagonist_start=brief.protagonist, protagonist_end=pending,
        theme_question=pending, ending_direction=pending, estimated_chapters=brief.estimated_chapters,
        estimated_volumes=brief.estimated_volumes, volume_compass=[VolumeCompass(volume_no=1,
            title="尚无正式卷纲", promise=pending, start_state=boundary, end_state=pending,
            estimated_chapters=brief.estimated_chapters)])
    card = ChapterCard(chapter_no=chapter_no, title_working=f"第{chapter_no}章（待写）",
        pov="沿用前文已建立的视角", time_location="衔接前章时间与地点，变化须在正文交代",
        function="承接前文，推进当前故事", goal=instruction or "自然承接前文，推进当前人物的目标",
        obstacle=pending, decision=pending, consequence=pending, irreversible_delta=pending,
        scenes=[pending], information_release="区分已知事实、人物认知与新信息，不强行兑现未满足条件的线索",
        hook_type="余韵", hook_question="由本章实际进展形成期待，不提前编造后续结果",
        target_words=brief.target_chapter_words,
        dependencies=[f"chapter:{chapter_no - 1:05d}"] if chapter_no > 1 else [])
    default_volume = base.current_volume.volume_no if base else 1
    number = next((v.volume_no for v in outline.volumes if v.chapter_start <= chapter_no <= v.chapter_end),
                  default_volume) if outline else default_volume
    # 上层伏笔可以引用卷或篇定义，临时目标保留原定义所在层及依赖，章节行动仍由本次正文决定。
    arc = ArcPlan(arc_id=key, volume_no=number, title="本章临时创作目标（不是正式规划）",
        chapter_start=chapter_no, chapter_end=chapter_no, promise=card.goal,
        central_conflict=pending, start_state=boundary, end_state=pending,
        escalation=["承接前章状态", "通过人物行动推进本章"], midpoint_turn=pending,
        climax=pending, aftermath=pending, exit_bridge="只写当前章，不代替后续规划", chapter_cards=[card],
        promises=base.current_arc.promises if base else [])
    volume = VolumePlan(volume_no=number, title="本章临时写作范围", chapter_start=chapter_no,
        chapter_end=chapter_no, promise=book.reader_promise, start_state=boundary, end_state=pending,
        antagonist_pressure=pending, midpoint_turn=pending, climax=pending, cost_and_result=pending,
        next_volume_bridge="沿用原大纲，不在此发布新走向", arcs=[ArcSummary(arc_id=key, title=arc.title,
            chapter_start=chapter_no, chapter_end=chapter_no, promise=arc.promise, end_state=arc.end_state)],
        promises=base.current_volume.promises if base else [])
    return PlanBundle(book=book, current_volume=volume, current_arc=arc)
