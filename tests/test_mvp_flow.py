from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.errors import ProjectError, ValidationGateError
from inkflow.project import InkFlowProject
from inkflow.provider import ScriptedProvider
from inkflow.references import ReferenceService
from inkflow.terminal_session import TerminalSession
from inkflow.schemas import (
    ArcAuditReport,
    ArcPlan,
    ArcPlanningBeat,
    ArcPlanningBrief,
    ArcPlanningGroundedItem,
    ArcSummary,
    BookBrief,
    BookPlan,
    ChapterCard,
    DraftOutput,
    EvidenceRepairBatch,
    EvidenceSelectionBatch,
    FactEvidenceRepair,
    FactEvidenceSelection,
    FactMutation,
    MemoryPatch,
    PlanBundle,
    ReviewReport,
    ReviewFinding,
    ThreadMutation,
    TerminalIntent,
    VolumeCompass,
    VolumePlan,
)


def make_brief() -> BookBrief:
    return BookBrief(
        title="雾港来信",
        genre="都市悬疑",
        premise="失踪记者留下七封定时寄出的信，迫使档案员林照追查一场被所有人共同遗忘的火灾。",
        protagonist="林照",
        core_selling_point="每封信改变一名角色对旧案的立场",
        target_chapter_words=500,
        estimated_chapters=20,
        estimated_volumes=1,
    )


def make_plan() -> PlanBundle:
    cards = [
        ChapterCard(
            chapter_no=1,
            title_working="第一封信",
            pov="林照",
            time_location="周一傍晚 / 市档案馆",
            function="触发事件",
            goal="确认匿名信是否来自失踪记者",
            obstacle="信封上的日期是三天后",
            decision="林照选择私下拆阅而不是上交",
            consequence="他得到旧仓库钥匙，也留下违规记录",
            irreversible_delta="林照成为旧案的主动调查者",
            scenes=["闭馆后的异常来信", "核对笔迹", "拆信并作出选择"],
            information_release="只确认笔迹，不解释信件如何寄出",
            foreshadow_advance=["thread:future_postmark"],
            payoff=[],
            hook_type="问题",
            hook_question="日期尚未来临，信是谁寄出的？",
            target_words=500,
            dependencies=[],
        ),
        ChapterCard(
            chapter_no=2,
            title_working="封存仓库",
            pov="林照",
            time_location="周一深夜 / 河西旧仓库",
            function="发现与代价",
            goal="用铜钥匙打开失踪记者留下的柜子",
            obstacle="仓库已被提前清理，保安正在巡查",
            decision="林照放弃完整撤离，带走一页烧焦名册",
            consequence="保安看见他的侧脸",
            irreversible_delta="调查留下第一名目击者",
            scenes=["潜入", "空柜与烧焦名册", "被发现后的选择"],
            information_release="揭示火灾名单少了一个人",
            foreshadow_advance=["thread:missing_name"],
            payoff=["thread:future_postmark"],
            hook_type="危险",
            hook_question="保安为什么准确喊出了林照的名字？",
            target_words=500,
            dependencies=["fact:linzhao_has_key"],
        ),
    ]
    return PlanBundle(
        book=BookPlan(
            title="雾港来信",
            premise="七封未来来信牵出被集体遗忘的火灾。",
            reader_promise="每封信解决一个问题，同时制造更危险的新证据。",
            narrative_engine="来信倒计时、旧案调查与人物立场反转",
            main_conflict="林照必须在真相与保护幸存者之间选择",
            protagonist_start="只相信档案记录",
            protagonist_end="理解记录也可能由恐惧共同制造",
            theme_question="被所有人同意的记忆还是真相吗？",
            ending_direction="林照公开证据，但保留一个人的匿名选择",
            estimated_chapters=20,
            estimated_volumes=1,
            volume_compass=[
                VolumeCompass(
                    volume_no=1,
                    title="七封信",
                    promise="找出来信机制与火灾缺席者",
                    start_state="林照与旧案无关",
                    end_state="林照掌握旧案证据并成为被追查者",
                    estimated_chapters=20,
                )
            ],
        ),
        current_volume=VolumePlan(
            volume_no=1,
            title="七封信",
            chapter_start=1,
            chapter_end=20,
            promise="逐封破解未来来信",
            start_state="林照收到第一封信",
            end_state="七封信的发送者身份被确认",
            antagonist_pressure="旧案相关者不断回收证据",
            midpoint_turn="林照发现自己也在幸存者名单上",
            climax="第七封信要求他销毁唯一原件",
            cost_and_result="真相公开，但重要盟友离开",
            next_volume_bridge="寄信设备仍在发送第八封信",
            arcs=[
                ArcSummary(
                    arc_id="v01-a01",
                    title="第一封信",
                    chapter_start=1,
                    chapter_end=2,
                    promise="确认来信与旧案确实相连",
                    end_state="林照取得火灾名单",
                ),
                ArcSummary(
                    arc_id="v01-a02",
                    title="名单缺口",
                    chapter_start=3,
                    chapter_end=4,
                    promise="确认名单缺失者与来信链条的关系",
                    end_state="林照确认缺失者曾参与寄信",
                ),
            ],
        ),
        current_arc=ArcPlan(
            arc_id="v01-a01",
            volume_no=1,
            title="第一封信",
            chapter_start=1,
            chapter_end=2,
            promise="确认来信与旧案确实相连",
            central_conflict="守规矩才能自保，违规才能接近真相",
            start_state="林照只是档案员",
            end_state="林照主动进入旧案且被人看见",
            escalation=["未来日期制造异常", "仓库证据被提前清理"],
            revelations=["笔迹属于失踪记者", "火灾名单缺少一个人"],
            relationship_movement=[],
            planted_threads=["future_postmark", "missing_name"],
            advanced_threads=[],
            payoff_threads=[],
            midpoint_turn="林照决定违规拆信",
            climax="林照在被发现前抢走名册",
            aftermath="违规记录与目击者同时留下",
            exit_bridge="第二封信寄到保安手里",
            replan_triggers=["来信机制提前公开"],
            chapter_cards=cards,
        ),
    )


def make_next_arc() -> ArcPlan:
    cards = []
    for chapter_no, title in ((3, "名单缺口"), (4, "第二封信")):
        cards.append(
            ChapterCard(
                chapter_no=chapter_no,
                title_working=title,
                pov="林照",
                time_location=f"周二 / 雾港旧城区 / 第 {chapter_no} 章",
                function="推进旧案并制造新的可追踪代价",
                goal="确认火灾名单缺失者的身份",
                obstacle="证人只肯用第二封信交换证词",
                decision="林照选择公开一部分违规记录换取证词",
                consequence="旧案相关者知道他掌握了名单",
                irreversible_delta=f"调查在第 {chapter_no} 章留下新的公开痕迹",
                scenes=["核对名单缺口", "与证人交换条件", "承担公开痕迹的后果"],
                information_release="只确认缺失者与寄信人有关，不揭示寄信机制",
                foreshadow_advance=["thread:missing_name"],
                payoff=[],
                hook_type="揭示" if chapter_no == 3 else "决定",
                hook_question="第二封信为何要求林照先暴露自己？",
                target_words=500,
                dependencies=["chapter:00002"],
            )
        )
    return ArcPlan(
        arc_id="v01-a02",
        volume_no=1,
        title="名单缺口",
        chapter_start=3,
        chapter_end=4,
        promise="确认名单缺失者与来信链条的关系",
        central_conflict="公开调查可换来证词，也会让对手锁定林照",
        start_state="林照只掌握一页烧焦名单",
        end_state="林照确认缺失者曾参与寄信",
        escalation=["证人拒绝无条件作证", "第二封信把交换条件变成公开风险"],
        revelations=["缺失者与寄信链条有关"],
        relationship_movement=["林照与证人建立不稳定互信"],
        planted_threads=["thread:second_letter_condition"],
        advanced_threads=["thread:missing_name"],
        payoff_threads=[],
        midpoint_turn="林照决定用自己的违规记录交换证词",
        climax="证人在追踪者到场前交出第二封信",
        aftermath="林照的位置和调查进度同时暴露",
        exit_bridge="第二封信指向火灾当晚的广播记录",
        replan_triggers=["证人身份被提前公开"],
        chapter_cards=cards,
    )


def make_planning_brief() -> ArcPlanningBrief:
    return ArcPlanningBrief(
        title="名单缺口的公开判断",
        central_question="林照是否要用自己的违规记录换取失踪者身份线索？",
        start_state="林照拿着烧焦名单，保安已看见他的侧脸。",
        desired_end_state="林照确认缺失者与来信链条有关，并暴露调查位置。",
        constraints_checked=[
            ArcPlanningGroundedItem(
                text="第一篇已接受正文中的违规记录不能抹掉",
                canon_refs=["fact:linzhao_has_key:v1"],
            ),
            ArcPlanningGroundedItem(
                text="第二封信尚未在正文出现",
                canon_refs=["thread:future_postmark"],
            ),
        ],
        chosen_direction=[
            ArcPlanningGroundedItem(
                text="建议用证人交换把名单谜题推进为人物选择",
                canon_refs=["thread:future_postmar"],
            ),
            ArcPlanningGroundedItem(
                text="建议把公开代价留到篇末，给下一篇章可追踪的压力",
                canon_refs=["fact:linzhao_has_key:v1"],
            ),
        ],
        beats=[
            ArcPlanningBeat(
                chapter_no=3,
                intended_turn="证人提出交换条件",
                hook="第二封信先一步送到证人手中",
                basis_refs=["thread:future_postmark"],
            ),
            ArcPlanningBeat(
                chapter_no=4,
                intended_turn="林照用违规记录换取证词",
                hook="来信指向火灾当晚的广播记录",
                basis_refs=["thread:future_postmark"],
            ),
        ],
        risks_to_verify=["证人知道的信息不能超过已建立的来源", "公开违规记录后必须保留实际追踪后果"],
    )


def make_draft() -> DraftOutput:
    base = (
        "雨沿档案馆的旧窗往下淌。闭馆铃响过两遍，林照才在退件筐底看见那只没有邮票的信封。"
        "信封写着他的名字，墨水尚未干，右上角的邮戳却是三天后的日期。"
        "他先把信压在登记册下面，绕过一排排铁柜去调监控。画面里没有邮差，也没有人靠近退件筐。"
        "停电前的最后一帧中，筐底还是空的。林照回到桌边，用尺子对照档案里的旧稿，认出那道向左拖长的收笔。"
        "那是失踪记者周迟的字。按规定，他应该立刻上交异常物件。林照的手停在内线电话上，最终却拔掉了电话线。"
        "他拆开封口，里面只有一张仓库平面图和一句话：别相信火灾名单。"
        "纸角粘着一把发暗的铜钥匙。走廊传来值班员的脚步，林照把信塞进衬衣内袋，把空信封留在桌上。"
        "值班员推门时，他正在填写一张普通得不能再普通的调阅单。对方看了一眼断开的电话线，没有追问。"
        "林照拿到了铜钥匙，却也在调阅系统里留下了自己的名字。"
    )
    return DraftOutput(
        title="第一封信",
        content=base + base[:180] + "窗外雨声没有停，调阅单上的墨迹却已经干了。",
        decision_summary=["用未来邮戳建立异常", "让主角主动违规并留下代价"],
        new_fact_candidates=["林照持有铜钥匙"],
        thread_changes=["种下未来邮戳之谜"],
    )


def make_review() -> ReviewReport:
    return ReviewReport(
        verdict="pass",
        confidence=0.91,
        summary="章节完成触发事件、主动选择与可追踪代价，信息释放符合章节卡。",
        strengths=["异常物件具体", "决定由人物主动做出"],
        findings=[],
    )


def make_arc_audit() -> ArcAuditReport:
    return ArcAuditReport(
        verdict="aligned",
        confidence=0.9,
        summary="两章完成了来信异常、主动违规与被看见的篇章出口，可作为下一篇章起点。",
        fulfilled_commitments=["确认来信与旧案存在可追踪关联"],
        deviations=[],
        future_impact=["下一篇章应承接林照已留下违规记录这一代价。"],
        replan_recommended=False,
        proposed_future_changes=[],
    )


def make_memory() -> MemoryPatch:
    return MemoryPatch(
        chapter_no=1,
        chapter_summary="林照收到带未来邮戳的信，确认笔迹属于失踪记者，并违规拆信取得铜钥匙。",
        scene_summaries=["闭馆后收到异常来信", "核对笔迹并决定拆信"],
        facts=[
            FactMutation(
                fact_id="fact:linzhao_has_key:v1",
                subject="林照",
                predicate="state.has_item",
                value="旧仓库铜钥匙",
                valid_from_chapter=1,
                confidence=1.0,
                evidence="林照拿到了铜钥匙",
            )
        ],
        threads=[
            ThreadMutation(
                thread_id="thread:future_postmark",
                kind="mystery",
                title="未来邮戳",
                status="open",
                description="来信邮戳来自三天后，发送机制未知。",
                planted_chapter=1,
                due_chapter=8,
            )
        ],
        unresolved_conflicts=[],
    )


def test_complete_mvp_flow(tmp_path: Path) -> None:
    provider = ScriptedProvider([make_plan(), make_draft(), make_review(), make_memory()])
    settings = Settings(trace_level="full", context_soft_tokens=20_000)
    engine = InkFlowEngine(provider, settings)
    root = tmp_path / "novel"

    created = engine.create_project(root, make_brief())
    assert created["project_id"].startswith("inkflow-")
    assert (root / "BOOK.md").exists()

    plan_result = asyncio.run(engine.generate_plan(root))
    assert plan_result["chapter_range"] == [1, 2]
    assert "第 1 章" in (root / "PLAN.md").read_text(encoding="utf-8")

    context = engine.build_context(root, 1)
    assert [item["key"] for item in context["sections"]] == list("ABCDEFGHIJ")

    draft_result = asyncio.run(engine.write_chapter(root, 1))
    assert Path(draft_result["draft_path"]).exists()

    review_result = asyncio.run(engine.review_chapter(root, 1))
    assert review_result["verdict"] == "pass"
    review_call = provider.calls[2]
    assert review_call["effort"] == "low"
    assert review_call["max_tokens"] == 16_000
    assert review_call["thinking"] is True

    accepted = asyncio.run(engine.accept_chapter(root, 1))
    assert accepted["status"] == "accepted"
    assert (root / "chapters" / "chapter_00001.md").exists()
    assert not (root / "chapters" / "chapter_00001.draft.md").exists()
    state = (root / "STATE.md").read_text(encoding="utf-8")
    assert "旧仓库铜钥匙" in state
    assert "未来邮戳" in state
    assert len(provider.calls) == 4


def test_natural_session_discusses_then_writes_a_draft_without_realtime_review(tmp_path: Path) -> None:
    discuss = TerminalIntent(
        action="discuss",
        visible_reason="用户正在讨论方向，先不执行写作。",
        conversation_reply="我理解你希望先保留悬念、让冲突更晚揭示。建议先确认第 1 章仍由主角主动做决定；确认后我再写草稿。",
    )
    write_draft = TerminalIntent(
        action="write_draft",
        chapter_no=1,
        requested_outcome="按刚才讨论的方向写第 1 章草稿，但先不审查。",
        confidence="high",
        authorization="approved",
        visible_reason="用户已明确要求只写草稿，不审查。",
        operation_instruction="保留悬念，不提前揭示寄信机制。",
    )
    provider = ScriptedProvider([make_plan(), discuss, write_draft, make_draft()])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    session = TerminalSession(engine)

    discussed = asyncio.run(session.handle(root, "我想让第 1 章先保留寄信机制，先和我讨论。"))
    assert "确认后" in discussed["reply"]
    assert provider.calls[-1]["output_model"] == "TerminalIntent"

    drafted = asyncio.run(session.handle(root, "确认，按刚才方案写第 1 章草稿，不审查。"))
    assert drafted["session"]["route"] == "write_draft"
    assert InkFlowProject(root).db.get_chapter(1)["status"] == "draft"
    assert InkFlowProject(root).db.latest_review_record(1) is None
    assert (root / "DIALOGUE.md").is_file()


def test_plan_range_preview_returns_cards_without_changing_plan(tmp_path: Path) -> None:
    provider = ScriptedProvider([make_plan()])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))

    preview = engine.preview_plan_range(root, 1, 2)

    assert preview["count"] == 2
    assert preview["changed"] is False
    assert [item["章节"] for item in preview["cards"]] == [1, 2]
    assert len(provider.calls) == 1


def test_natural_session_routes_one_batch_plan_preview(tmp_path: Path) -> None:
    preview_intent = TerminalIntent(
        action="plan_preview",
        chapter_no=1,
        end_chapter_no=2,
        visible_reason="用户希望一次查看两张章节卡，不修改规划。",
    )
    provider = ScriptedProvider([make_plan(), preview_intent])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))

    result = asyncio.run(TerminalSession(engine).handle(root, "把第1到第2章规划一起给我看。"))

    assert result["session"]["route"] == "plan_preview"
    assert result["result"]["chapter_range"] == [1, 2]
    assert result["result"]["changed"] is False
    assert len(provider.calls) == 2


def test_deterministic_review_failure_skips_paid_model_call(tmp_path: Path) -> None:
    short_draft = make_draft().model_copy(update={"content": "短正文。" * 40})
    provider = ScriptedProvider([make_plan(), short_draft])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    asyncio.run(engine.write_chapter(root, 1))

    reviewed = asyncio.run(engine.review_chapter(root, 1))

    assert reviewed["verdict"] == "patch"
    assert reviewed["model_skipped"] is True
    assert len(provider.calls) == 2


def test_draft_output_tolerates_missing_trace_only_summary() -> None:
    draft = DraftOutput.model_validate({"title": "缺少摘要", "content": "正文内容。" * 30})
    assert draft.decision_summary == ["模型未提供结构化摘要；正文将由后续审查确认。"]


def test_rolling_plan_waits_for_canon_then_opens_only_next_arc(tmp_path: Path) -> None:
    memory_two = MemoryPatch(
        chapter_no=2,
        chapter_summary="林照进入旧仓库取得烧焦名单，并被保安看见。",
        scene_summaries=["潜入旧仓库", "带走烧焦名单"],
        facts=[],
        threads=[],
        unresolved_conflicts=[],
    )
    provider = ScriptedProvider(
        [
            make_plan(),
            make_draft(),
            make_review(),
            make_memory(),
            make_draft(),
            make_review(),
            memory_two,
            make_planning_brief(),
            make_next_arc(),
        ]
    )
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"

    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    with pytest.raises(ValidationGateError, match="当前篇章尚未全部进入正史"):
        asyncio.run(engine.advance_plan(root))

    for chapter_no in (1, 2):
        asyncio.run(engine.write_chapter(root, chapter_no))
        asyncio.run(engine.review_chapter(root, chapter_no))
        asyncio.run(engine.accept_chapter(root, chapter_no))

    public_brief = asyncio.run(
        engine.preview_next_arc(root, instruction="先给我能核对的第二篇判断单，不要改正式规划。")
    )
    assert public_brief["chapter_range"] == [3, 4]
    assert public_brief["rationale"]["title"] == "名单缺口的公开判断"
    brief_path = Path(public_brief["brief_path"])
    assert brief_path.is_file()
    rendered_brief = brief_path.read_text(encoding="utf-8")
    assert "公开篇章判断单" in rendered_brief
    assert "不是模型的逐步隐藏思维" in rendered_brief
    assert "来源编号自动更正" in rendered_brief
    assert "thread:future_postmar" in rendered_brief
    assert public_brief["rationale"]["chosen_direction"][0]["canon_refs"] == ["thread:future_postmark"]
    assert InkFlowProject(root).db.get_current_plan_bundle().current_arc.arc_id == "v01-a01"
    assert provider.calls[-1]["output_model"] == "ArcPlanningBrief"
    assert provider.calls[-1]["max_tokens"] == 6_000
    assert provider.calls[-1]["timeout_seconds"] == 180.0
    assert "可用来源编号（只能逐字复制）" in provider.calls[-1]["user_prompt"]

    advanced = asyncio.run(engine.advance_plan(root))
    assert advanced["chapter_range"] == [3, 4]
    assert advanced["current_arc"] == "v01-a02"
    project = InkFlowProject(root)
    assert project.db.get_current_plan_bundle().current_arc.arc_id == "v01-a02"
    assert project.db.get_plan("arc", "v01-a01") is not None
    assert project.db.get_chapter_card(2) is not None
    assert project.db.get_chapter_card(3)["title_working"] == "名单缺口"
    assert provider.calls[-1]["output_model"] == "ArcPlan"
    assert "# Context Packet" in provider.calls[-1]["user_prompt"]
    assert provider.calls[-1]["effort"] == "high"
    assert provider.calls[-1]["max_tokens"] == 16_000
    assert provider.calls[-1]["thinking"] is True
    assert provider.calls[-1]["timeout_seconds"] == 600.0
    rendered = (root / "PLAN.md").read_text(encoding="utf-8")
    assert "公开推理摘要" in rendered
    assert "旧版本未提供公开推理摘要" in rendered
    planning_prompt = provider.calls[-1]["user_prompt"]
    assert "紧凑语义视图" in planning_prompt
    assert "最近公开篇章判断单" in planning_prompt
    assert "名单缺口的公开判断" in planning_prompt
    assert '"evidence"' not in planning_prompt


def test_public_planning_brief_rejects_unknown_canon_reference(tmp_path: Path) -> None:
    memory_two = MemoryPatch(
        chapter_no=2,
        chapter_summary="林照进入旧仓库取得烧焦名单，并被保安看见。",
        scene_summaries=["潜入旧仓库", "带走烧焦名单"],
        facts=[],
        threads=[],
        unresolved_conflicts=[],
    )
    bad_brief = make_planning_brief().model_copy(
        update={
            "constraints_checked": [
                ArcPlanningGroundedItem(text="伪造的既有事实", canon_refs=["fact:not_in_canon"])
            ]
        }
    )
    provider = ScriptedProvider(
        [make_plan(), make_draft(), make_review(), make_memory(), make_draft(), make_review(), memory_two, bad_brief]
    )
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    for chapter_no in (1, 2):
        asyncio.run(engine.write_chapter(root, chapter_no))
        asyncio.run(engine.review_chapter(root, chapter_no))
        asyncio.run(engine.accept_chapter(root, chapter_no))

    with pytest.raises(ValidationGateError, match="不存在的正史/线索编号"):
        asyncio.run(engine.preview_next_arc(root))

    assert not (root / "planning").exists()
    assert InkFlowProject(root).db.get_current_plan_bundle().current_arc.arc_id == "v01-a01"


def test_long_run_coordinator_uses_all_gates_and_stops_on_canon_target(tmp_path: Path) -> None:
    memory_two = MemoryPatch(
        chapter_no=2,
        chapter_summary="林照进入旧仓库取得烧焦名单，并被保安看见。",
        scene_summaries=["潜入旧仓库", "带走烧焦名单"],
        facts=[],
        threads=[],
        unresolved_conflicts=[],
    )
    provider = ScriptedProvider(
        [make_plan(), make_draft(), make_review(), make_memory(), make_draft(), make_review(), memory_two]
    )
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))

    result = asyncio.run(
        engine.continue_until(
            root,
            1_000,
            instruction="保持线索可追踪，不跳过审查",
        )
    )

    assert result["status"] == "target_reached"
    assert result["accepted_characters"] >= 1_000
    assert result["accepted_chapters"] == 2
    assert InkFlowProject(root).db.project_status()["chapters"] == {"accepted": 2}
    assert Path(result["progress_path"]).is_file()
    assert provider.balance_checks == 0
    assert [call["output_model"] for call in provider.calls[1:]] == [
        "DraftOutput",
        "ReviewReport",
        "MemoryPatch",
        "DraftOutput",
        "ReviewReport",
        "MemoryPatch",
    ]


def test_natural_session_can_run_to_an_explicit_end_chapter(tmp_path: Path) -> None:
    intent = TerminalIntent(
        action="continue_run",
        end_chapter_no=1,
        requested_outcome="完整完成第 1 章并在审查通过后进入正史。",
        confidence="high",
        authorization="approved",
        visible_reason="用户要求完整完成到第 1 章。",
        operation_instruction="保持线索可追踪。",
    )
    provider = ScriptedProvider([make_plan(), intent, make_draft(), make_review(), make_memory()])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))

    result = asyncio.run(TerminalSession(engine).handle(root, "连续完成第 1 章，审查通过才进入正史"))

    assert result["session"]["route"] == "continue_run"
    assert result["result"]["status"] == "target_reached"
    assert result["result"]["accepted_chapters"] == 1
    assert "第 1 章进入正史" in Path(result["result"]["progress_path"]).read_text(encoding="utf-8")


def test_batch_draft_uses_provisional_ledger_then_accepts_continuous_prefix(tmp_path: Path) -> None:
    memory_two = MemoryPatch(
        chapter_no=2,
        chapter_summary="林照带走烧焦名单。",
        scene_summaries=["带走名单"],
        facts=[],
        threads=[],
        unresolved_conflicts=[],
    )
    provider = ScriptedProvider(
        [
            make_plan(),
            make_draft(),
            make_review(),
            make_draft(),
            make_review(),
            make_memory(),
            memory_two,
        ]
    )
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))

    batch = asyncio.run(engine.draft_batch(root, 1, 2, instruction="保留线索可追踪"))

    assert batch["status"] == "ready_for_acceptance"
    assert [item["chapter_no"] for item in batch["chapters"]] == [1, 2]
    assert batch["chapters"][0]["score_total"] == 100
    assert batch["chapters"][0]["review_summary"]
    assert InkFlowProject(root).db.project_status()["chapters"] == {"draft": 2}
    assert Path(batch["batch_path"]).is_file()
    assert "批次临时草稿 · 第 1 章" in provider.calls[3]["user_prompt"]

    accepted = asyncio.run(engine.accept_batch(root, batch["batch_id"]))

    assert accepted["status"] == "accepted"
    assert InkFlowProject(root).db.project_status()["chapters"] == {"accepted": 2}
    assert (root / "chapters" / "chapter_00001.md").is_file()
    assert (root / "chapters" / "chapter_00002.md").is_file()


def test_batch_repair_replaces_stale_review_versions_without_touching_canon(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            make_plan(),
            make_draft(),
            make_review(),
            make_draft(),
            make_review(),
            make_draft(),
            make_review(),
            make_draft(),
            make_review(),
        ]
    )
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    batch = asyncio.run(engine.draft_batch(root, 1, 2))

    repaired = asyncio.run(
        engine.repair_batch(
            root,
            batch["batch_id"],
            start_chapter_no=1,
            end_chapter_no=2,
            instruction="修正跨章委托关系，但保留两章已有事件。",
        )
    )

    assert repaired["status"] == "ready_for_acceptance"
    assert [item["version"] for item in repaired["chapters"]] == [2, 2]
    assert InkFlowProject(root).db.project_status()["chapters"] == {"draft": 2}
    manifest = Path(repaired["batch_path"]).read_text(encoding="utf-8")
    assert "最近一次批次修复" in manifest
    assert "第 1～2 章" in manifest


def test_natural_arc_audit_reads_ready_batch_without_changing_canon_or_plan(tmp_path: Path) -> None:
    audit_intent = TerminalIntent(
        action="arc_audit",
        chapter_no=1,
        end_chapter_no=2,
        requested_outcome="检查这两章是否与当前篇章规划自然衔接。",
        confidence="high",
        authorization="approved",
        visible_reason="用户要求将两章批量草稿与第一篇章规划对照复审。",
    )
    provider = ScriptedProvider(
        [
            make_plan(),
            make_draft(),
            make_review(),
            make_draft(),
            make_review(),
            audit_intent,
            make_arc_audit(),
        ]
    )
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    batch = asyncio.run(engine.draft_batch(root, 1, 2))
    plan_before = (root / "PLAN.md").read_text(encoding="utf-8")

    result = asyncio.run(TerminalSession(engine).handle(root, "复审第1到第2章并和规划对比。"))

    assert result["session"]["route"] == "arc_audit"
    assert result["result"]["verdict"] == "aligned"
    assert result["result"]["score_total"] == 100
    assert len(result["result"]["scorecard"]) == 5
    assert result["result"]["source_batch_id"] == batch["batch_id"]
    assert result["result"]["requires_user_confirmation"] is False
    assert Path(result["result"]["review_path"]).is_file()
    assert InkFlowProject(root).db.project_status()["chapters"] == {"draft": 2}
    assert (root / "PLAN.md").read_text(encoding="utf-8") == plan_before
    assert provider.calls[-1]["output_model"] == "ArcAuditReport"
    assert "批次临时草稿，尚未进入正史" in provider.calls[-1]["user_prompt"]


def test_future_replan_needs_natural_language_confirmation(tmp_path: Path) -> None:
    engine = InkFlowEngine(ScriptedProvider([]), Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    intent = TerminalIntent(
        action="plan_next_arc",
        operation_instruction="根据复审结果调整后续篇章。",
        authorization="proposed",
        visible_reason="用户要求改变未来规划，但尚未明确确认。",
    )

    result = asyncio.run(TerminalSession(engine)._dispatch(root, intent))

    assert "还没有明确要求现在执行" in result["gate"]


def test_natural_router_resolves_one_draft_from_colloquial_reference(tmp_path: Path) -> None:
    review_intent = TerminalIntent(
        action="review",
        requested_outcome="看看刚才写好的那章有没有硬伤。",
        confidence="medium",
        authorization="approved",
        missing_fields=["chapter_no"],
        visible_reason="用户要求检查唯一的当前草稿。",
    )
    provider = ScriptedProvider([make_plan(), make_draft(), review_intent, make_review()])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    asyncio.run(engine.write_chapter(root, 1))

    result = asyncio.run(TerminalSession(engine).handle(root, "帮我看看刚才那章有没有硬伤。"))

    assert result["session"]["route"] == "review"
    assert result["session"]["chapter_no"] == 1
    assert result["steps"][0]["result"]["verdict"] == "pass"
    assert "needs_clarification" not in result


def test_natural_router_asks_when_two_actions_are_both_plausible(tmp_path: Path) -> None:
    ambiguous = TerminalIntent(
        action="review",
        alternative_action="arc_audit",
        chapter_no=1,
        requested_outcome="看看这一段到底有没有问题。",
        confidence="low",
        authorization="approved",
        clarification_question="你是只检查第 1 章，还是把当前篇章一起对照规划检查？",
        visible_reason="单章审查与篇章复审都可能符合用户表达。",
    )
    provider = ScriptedProvider([ambiguous])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())

    result = asyncio.run(TerminalSession(engine).handle(root, "你帮我看看这一段到底行不行。"))

    assert result["needs_clarification"] is True
    assert result["routing"]["candidate_action"] == "review"
    assert result["routing"]["alternative_action"] == "arc_audit"
    assert "只检查第 1 章" in result["reply"]
    assert InkFlowProject(root).db.latest_review_record(1) is None
    assert len(provider.calls) == 1


def test_natural_router_does_not_execute_a_proposed_canon_run(tmp_path: Path) -> None:
    proposed = TerminalIntent(
        action="continue_run",
        end_chapter_no=2,
        requested_outcome="也许可以连续写到第 2 章并进入正史。",
        confidence="high",
        authorization="proposed",
        clarification_question="你是想先讨论这个安排，还是现在连续完成到第 2 章？",
        visible_reason="用户提出了可能方案，但没有要求立即开始。",
    )
    provider = ScriptedProvider([proposed])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())

    result = asyncio.run(TerminalSession(engine).handle(root, "要不后面可以直接写到第二章？"))

    assert result["needs_clarification"] is True
    assert "现在连续完成" in result["reply"]
    assert InkFlowProject(root).db.get_chapter(1) is None
    assert len(provider.calls) == 1


def test_colloquial_approval_authorizes_confirmed_future_replan(tmp_path: Path) -> None:
    approved = TerminalIntent(
        action="plan_next_arc",
        requested_outcome="按刚才复审建议调整下一篇章。",
        confidence="high",
        authorization="approved",
        operation_instruction="根据最近复审调整下一篇章。",
        visible_reason="用户用自然表达明确要求立即执行。",
    )
    engine = InkFlowEngine(ScriptedProvider([]), Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())

    resolved, response = TerminalSession(engine)._resolve_intent(
        InkFlowProject(root),
        "行，就照你刚才说的改后面的篇章。",
        approved,
    )

    assert response is None
    assert resolved.authorization == "approved"
    assert resolved.plan_change_confirmed is True


def test_router_ignores_missing_fields_that_do_not_belong_to_selected_action(tmp_path: Path) -> None:
    intent = TerminalIntent(
        action="accept",
        chapter_no=1,
        requested_outcome="接收已经通过审查的第 1 章。",
        confidence="high",
        authorization="approved",
        missing_fields=["batch_id"],
        visible_reason="用户要接收单章，不是接收批次。",
    )
    engine = InkFlowEngine(ScriptedProvider([]), Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())

    resolved, response = TerminalSession(engine)._resolve_intent(
        InkFlowProject(root),
        "把第一章收进去。",
        intent,
    )

    assert resolved.missing_fields == []
    assert response is None


def test_router_copies_explicit_chapter_number_when_model_omits_it(tmp_path: Path) -> None:
    intent = TerminalIntent(
        action="review_accept",
        requested_outcome="第 10 章审查通过后接收。",
        confidence="high",
        authorization="approved",
        missing_fields=["chapter_no"],
        visible_reason="用户给出了带门禁的明确接收命令。",
    )
    engine = InkFlowEngine(ScriptedProvider([]), Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())

    resolved, response = TerminalSession(engine)._resolve_intent(
        InkFlowProject(root),
        "第10章检查没问题的话就收进正式内容。",
        intent,
    )

    assert resolved.chapter_no == 10
    assert resolved.missing_fields == []
    assert response is None


def test_router_copies_explicit_batch_chapter_range(tmp_path: Path) -> None:
    intent = TerminalIntent(
        action="batch_draft",
        requested_outcome="批量生成第 9 到第 12 章草稿。",
        confidence="high",
        authorization="approved",
        missing_fields=["chapter_range"],
        visible_reason="用户给出了明确批量范围。",
    )
    engine = InkFlowEngine(ScriptedProvider([]), Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())

    resolved, response = TerminalSession(engine)._resolve_intent(
        InkFlowProject(root),
        "先一口气写第9到第12章给我看看，别收进正式内容。",
        intent,
    )

    assert (resolved.chapter_no, resolved.end_chapter_no) == (9, 12)
    assert resolved.missing_fields == []
    assert response is None


def test_router_fills_unique_batch_repair_reference_and_range(tmp_path: Path) -> None:
    provider = ScriptedProvider([make_plan(), make_draft(), make_review(), make_draft(), make_review()])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    batch = asyncio.run(engine.draft_batch(root, 1, 2))
    intent = TerminalIntent(
        action="batch_repair",
        requested_outcome="按复审结论修复当前临时草稿。",
        confidence="high",
        authorization="approved",
        missing_fields=["batch_id", "chapter_range"],
        visible_reason="用户要求修复唯一的临时批次。",
    )

    resolved, response = TerminalSession(engine)._resolve_intent(
        InkFlowProject(root),
        "按复审结论修正这批草稿，先别接收。",
        intent,
    )

    assert resolved.batch_id == batch["batch_id"]
    assert (resolved.chapter_no, resolved.end_chapter_no) == (1, 2)
    assert resolved.missing_fields == []
    assert response is None


def test_router_uses_final_range_when_message_quotes_an_audit_scope(tmp_path: Path) -> None:
    intent = TerminalIntent(
        action="batch_repair",
        batch_id="batch-example",
        requested_outcome="根据复审修复第 9 到第 10 章。",
        confidence="high",
        authorization="approved",
        missing_fields=["chapter_range"],
        visible_reason="用户先引用复审范围，再明确修复范围。",
    )
    engine = InkFlowEngine(ScriptedProvider([]), Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())

    resolved, response = TerminalSession(engine)._resolve_intent(
        InkFlowProject(root),
        "按第1到10章复审报告修正第9到第10章，先别接收。",
        intent,
    )

    assert (resolved.chapter_no, resolved.end_chapter_no) == (9, 10)
    assert resolved.missing_fields == []
    assert response is None


def test_review_scorecard_only_deducts_for_cited_findings(tmp_path: Path) -> None:
    minor_planning = make_review().model_copy(
        update={
            "findings": [
                ReviewFinding(
                    category="planning",
                    severity="minor",
                    evidence="正文未直接说明证人为何提前离场。",
                    explanation="这会让章节卡的交接场景略显跳跃，但不改变主角的决定或后果。",
                    repair_instruction="如需精修，可补一笔证人离场的可见动机。",
                )
            ]
        },
        deep=True,
    )
    provider = ScriptedProvider([make_plan(), make_draft(), minor_planning])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    asyncio.run(engine.write_chapter(root, 1))

    reviewed = asyncio.run(engine.review_chapter(root, 1))
    rendered = Path(reviewed["review_path"]).read_text(encoding="utf-8")

    assert reviewed["verdict"] == "pass"
    assert reviewed["score_total"] == 97
    assert reviewed["findings"][0]["evidence"] == "正文未直接说明证人为何提前离场。"
    assert "章节卡履约：17/20" in rendered
    assert "正文未直接说明证人为何提前离场。" in rendered
    assert "未见可引用的扣分证据，因此本项满分。" in rendered


def test_workspace_guard_and_recoverable_delete(tmp_path: Path) -> None:
    root = tmp_path / "novel"
    InkFlowProject.create(root, make_brief())
    project = InkFlowProject(root)
    project.write_file("notes/test.md", "hello")
    result = project.delete_file("notes/test.md")
    assert result["recoverable"] is True
    assert Path(result["trash_path"]).exists()
    with pytest.raises(ProjectError):
        project.read_file("../outside.txt")
    with pytest.raises(ProjectError):
        project.write_file(".inkflow/project.json", "{}")


def test_reference_import_and_analysis(tmp_path: Path) -> None:
    root = tmp_path / "novel"
    InkFlowProject.create(root, make_brief())
    source = tmp_path / "reference.txt"
    source.write_text("第一章 雨夜\n\n“你来晚了。”他说。\n\n" + "潮水拍着码头。" * 80, encoding="utf-8")
    service = ReferenceService(InkFlowProject(root))
    imported = service.import_text(source)
    feature = service.analyze(imported["reference_id"])
    assert feature["characters"] > 200
    assert feature["dialogue_ratio"] > 0


def test_fanqie_adapter_rejects_non_fanqie_hosts_without_a_network_call(tmp_path: Path) -> None:
    root = tmp_path / "novel"
    InkFlowProject.create(root, make_brief())
    service = ReferenceService(InkFlowProject(root))
    with pytest.raises(ProjectError, match="fanqienovel.com"):
        asyncio.run(service.fetch_fanqie_public("https://example.com/book/123"))


def test_accept_requires_review(tmp_path: Path) -> None:
    provider = ScriptedProvider([make_plan(), make_draft()])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"
    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    asyncio.run(engine.write_chapter(root, 1))
    with pytest.raises(ValidationGateError):
        asyncio.run(engine.accept_chapter(root, 1))


def test_memory_evidence_is_repaired_before_canon_commit(tmp_path: Path) -> None:
    invalid_memory = make_memory().model_copy(deep=True)
    invalid_memory.facts[0].evidence = "林照获得了一把旧仓库的铜钥匙"
    repair = EvidenceSelectionBatch(
        selections=[FactEvidenceSelection(fact_id="fact:linzhao_has_key:v1", candidate_id="c01")]
    )
    provider = ScriptedProvider([make_plan(), make_draft(), make_review(), invalid_memory, repair])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"

    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    asyncio.run(engine.write_chapter(root, 1))
    asyncio.run(engine.review_chapter(root, 1))
    accepted = asyncio.run(engine.accept_chapter(root, 1))

    assert accepted["facts_committed"] == 1
    assert len(provider.calls) == 5
    assert provider.calls[-1]["effort"] == "low"
    assert provider.calls[-1]["output_model"] == "EvidenceSelectionBatch"
    assert "程序截取的原文候选" in provider.calls[-1]["user_prompt"]
    assert provider.calls[-1]["thinking"] is False


def test_memory_evidence_small_copy_error_is_aligned_without_second_model_call(tmp_path: Path) -> None:
    near_memory = make_memory().model_copy(deep=True)
    near_memory.facts[0].evidence = "林照拿到了旧铜钥匙"
    provider = ScriptedProvider([make_plan(), make_draft(), make_review(), near_memory])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"

    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    asyncio.run(engine.write_chapter(root, 1))
    asyncio.run(engine.review_chapter(root, 1))
    accepted = asyncio.run(engine.accept_chapter(root, 1))

    assert accepted["facts_committed"] == 1
    assert len(provider.calls) == 4
    facts = InkFlowProject(root).db.current_facts()
    assert facts[0]["evidence"] == "林照拿到了铜钥匙"


def test_memory_unknown_story_question_does_not_block_canon(tmp_path: Path) -> None:
    memory = make_memory().model_copy(
        update={"unresolved_conflicts": ["铜钥匙究竟开启哪里尚未证实，正文无法确认"]},
        deep=True,
    )
    provider = ScriptedProvider([make_plan(), make_draft(), make_review(), memory])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"

    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    asyncio.run(engine.write_chapter(root, 1))
    asyncio.run(engine.review_chapter(root, 1))
    accepted = asyncio.run(engine.accept_chapter(root, 1))

    assert accepted["status"] == "accepted"
    assert len(provider.calls) == 4


def test_memory_true_conflict_still_runs_bounded_resolution(tmp_path: Path) -> None:
    memory = make_memory().model_copy(
        update={"unresolved_conflicts": ["上一正史称铜钥匙已销毁，本章称仍由林照持有，两个版本无法同时成立"]},
        deep=True,
    )
    provider = ScriptedProvider([make_plan(), make_draft(), make_review(), memory, make_memory()])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"

    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    asyncio.run(engine.write_chapter(root, 1))
    asyncio.run(engine.review_chapter(root, 1))
    accepted = asyncio.run(engine.accept_chapter(root, 1))

    assert accepted["status"] == "accepted"
    assert len(provider.calls) == 5
    assert "冲突自解析要求" in provider.calls[-1]["user_prompt"]


def test_checkpoint_preview_and_branch_restore_remove_later_draft(tmp_path: Path) -> None:
    provider = ScriptedProvider([make_plan(), make_draft(), make_review(), make_memory(), make_draft()])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"

    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    asyncio.run(engine.write_chapter(root, 1))
    asyncio.run(engine.review_chapter(root, 1))
    accepted = asyncio.run(engine.accept_chapter(root, 1))
    checkpoint_id = accepted["checkpoint"]["checkpoint_id"]
    assert accepted["checkpoint"]["boundary_chapter"] == 1

    asyncio.run(engine.write_chapter(root, 2, "模拟后来发现第二章方向不合适"))
    draft_path = root / "chapters" / "chapter_00002.draft.md"
    assert draft_path.is_file()
    assert InkFlowProject(root).db.get_chapter(2)["status"] == "draft"

    preview = engine.rollback_preview(root, checkpoint_id=checkpoint_id)
    assert "chapters/chapter_00002.draft.md" in preview["impact"]["remove_to_recoverable_trash"]
    with pytest.raises(ValidationGateError, match="确认码无效"):
        engine.rollback_restore(
            root,
            checkpoint_id=checkpoint_id,
            confirmation_token="stale-or-wrong-token",
        )

    restored = engine.rollback_restore(
        root,
        checkpoint_id=checkpoint_id,
        confirmation_token=preview["confirmation_token"],
    )
    project = InkFlowProject(root)
    assert restored["status"] == "restored"
    assert restored["project_status"]["chapters"] == {"accepted": 1}
    assert project.db.get_chapter(2) is None
    assert not draft_path.exists()
    assert Path(restored["recoverable_trash"]).is_dir()
    checkpoint_ids = {item["checkpoint_id"] for item in engine.checkpoint_list(root)["checkpoints"]}
    assert checkpoint_id in checkpoint_ids
    assert restored["safety_checkpoint_id"] in checkpoint_ids
    assert restored["new_branch_id"].startswith("branch-")


def test_rewrite_invalidates_previous_review(tmp_path: Path) -> None:
    provider = ScriptedProvider([make_plan(), make_draft(), make_review(), make_draft()])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"

    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    asyncio.run(engine.write_chapter(root, 1))
    asyncio.run(engine.review_chapter(root, 1))
    asyncio.run(engine.write_chapter(root, 1, "仅调整措辞"))

    with pytest.raises(ValidationGateError, match="正文修改后必须重新审查"):
        asyncio.run(engine.accept_chapter(root, 1))
    assert len(provider.calls) == 4


def test_revise_reads_current_draft_and_bound_review(tmp_path: Path) -> None:
    revised = make_draft().model_copy(deep=True)
    revised.title = "第一封信（修订）"
    revised.content = revised.content.replace("墨水尚未干", "墨迹在灯下泛着潮光")
    revised.decision_summary = ["读取当前草稿并按同版本审查证据定点调整"]
    provider = ScriptedProvider([make_plan(), make_draft(), make_review(), revised])
    engine = InkFlowEngine(provider, Settings(context_soft_tokens=20_000))
    root = tmp_path / "novel"

    engine.create_project(root, make_brief())
    asyncio.run(engine.generate_plan(root))
    first = asyncio.run(engine.write_chapter(root, 1))
    review = asyncio.run(engine.review_chapter(root, 1))
    result = asyncio.run(engine.revise_chapter(root, 1, "保留有效情节，只修具体问题"))

    revise_call = provider.calls[-1]
    assert revise_call["output_model"] == "DraftOutput"
    assert "# 当前草稿" in revise_call["user_prompt"]
    assert "雨沿档案馆的旧窗往下淌" in revise_call["user_prompt"]
    assert "# 当前版本 Reviewer 报告" in revise_call["user_prompt"]
    assert "章节完成触发事件" in revise_call["user_prompt"]
    assert result["previous_version"] == first["version"] == 1
    assert result["version"] == 2
    assert result["next_action"] == "重新审查当前版本"
    assert Path(review["review_path"]).exists()
    with pytest.raises(ValidationGateError, match="正文修改后必须重新审查"):
        asyncio.run(engine.accept_chapter(root, 1))
