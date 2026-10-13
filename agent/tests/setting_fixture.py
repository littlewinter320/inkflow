"""Small offline project fixture for the settings integration checks."""
from inkflow.project import InkFlowProject
from inkflow.schemas import ArcPlan, BookBrief, PlanBundle
from inkflow.utils import content_hash, utc_now

def arc(start, end):
    card = dict(title_working="继续核实", pov="主人公", time_location="次日街口", function="核对线索",
                goal="找出证人", obstacle="证人不信任", decision="出示已有材料", consequence="获准核对",
                irreversible_delta="对方知道来意", scenes=["核对已有材料"], information_release="一条可核实线索",
                hook_type="决定", hook_question="下一步怎样核对", target_words=2900)
    return ArcPlan(arc_id=f"arc-{start}-{end}", volume_no=1, title="核对线索", chapter_start=start, chapter_end=end,
                   promise="继续核对", central_conflict="证据不足", start_state="尚未核实", end_state="取得线索",
                   escalation=["寻找依据", "当面核对"], midpoint_turn="确认线索", climax="作出选择",
                   aftermath="承担结果", exit_bridge="继续核对", chapter_cards=[dict(card, chapter_no=n) for n in range(start, end + 1)])


def project_fixture(tmp_path):
    brief = BookBrief(title="规划衔接", genre="现实悬疑", protagonist="修锁匠", premise="一位修锁匠逐步寻找突然失踪的街坊。",
                      estimated_chapters=45, estimated_volumes=3, target_chapter_words=2900)
    project = InkFlowProject.create(tmp_path, brief)
    first = arc(1, 10)
    compass = [dict(volume_no=n, title=f"卷{n}", promise="核对线索", start_state="开始", end_state="结束", estimated_chapters=15) for n in range(1, 4)]
    bundle = PlanBundle.model_validate({
        "book": dict(title=brief.title, premise=brief.premise, reader_promise="事实有据", narrative_engine="追查线索",
                     main_conflict="信息不足", protagonist_start="独处", protagonist_end="参与", theme_question="为何参与",
                     ending_direction="对清疑问", estimated_chapters=45, estimated_volumes=3, volume_compass=compass),
        "current_volume": dict(volume_no=1, title="卷1", chapter_start=1, chapter_end=15, promise="核对线索",
                               start_state="开始", end_state="结束", antagonist_pressure="被阻拦", midpoint_turn="找到依据",
                               climax="对清疑问", cost_and_result="付出时间", next_volume_bridge="新的问题",
                               arcs=[dict(arc_id=first.arc_id, title=first.title, chapter_start=1, chapter_end=10, promise=first.promise, end_state=first.end_state),
                                     dict(arc_id="arc-11-15", title="后续核对", chapter_start=11, chapter_end=15, promise="完成本卷", end_state="对清疑问")]),
        "current_arc": first.model_dump(mode="json"),
    })
    (tmp_path / "OUTLINE.md").write_text("全书大纲：沿已有证据逐步对清疑问。", encoding="utf-8")
    (tmp_path / "STORY_DETAIL.md").write_text("剧情细纲：取得信任后进行当面核对，不把它当成已发生事实。", encoding="utf-8")
    project.db.save_plan_bundle(bundle)
    with project.db.connect() as connection:
        for number in range(1, 11):
            content = f"# 第{number}章\n\n此前已核对的事实。\n"
            path = f"chapters/chapter_{number:05d}.md"
            (tmp_path / path).write_text(content, encoding="utf-8")
            connection.execute("INSERT INTO chapters(chapter_no,title,status,path,content_hash,content_text,summary,updated_at,accepted_at) VALUES (?,?,'accepted',?,?,?,?,?,?)",
                               (number, "已接受", path, content_hash(content), content, "已核对事实", utc_now(), utc_now()))
        connection.commit()
    return project
