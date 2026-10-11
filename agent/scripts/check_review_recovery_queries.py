"""Only missing source evidence searches canon; attribution repair reuses the loaded packet."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.project import InkFlowProject
from inkflow.schemas import BookBrief, ContextPacket, ContextSection, ModeCheckOutput, ReviewAssessment, ReviewSourceComparison, WriterNoteClarification
from inkflow.writer_notes import notes_hash
from inkflow.review_verifier import resolve_review_quote_refs, packet_sources
from inkflow.project_lock import project_write_lock


def check():
    with TemporaryDirectory(prefix="review-recovery-",dir="D:/墨流/cache") as folder:
        project=InkFlowProject.create(Path(folder)/"book",BookBrief(title="离线归因",genre="现实",protagonist="林安",premise="仅核对补读选择和来源预算，不生成小说。"))
        trace=SimpleNamespace(run_id="fixture",run_dir=Path(folder),record=lambda *a,**k:None,record_model=lambda *a,**k:None)
        packet=ContextPacket(project_id=project.project_id,chapter_no=1,task="归因核验",sections=[
            ContextSection(key="O2",title="当前近期规划",content="## 第1章\n在灯下核对来信。",source_ids=["RECENT_PLAN.md"])],estimated_tokens=0)
        output=ModeCheckOutput(verdict="unknown",confidence=0,summary="仅当前规划对照归因未填",
            assessments=[ReviewAssessment(criterion="readability",status="met",grade=4,reason="当前正文行文可读",alternative="保留待核",chapter_evidence="测试正文")])
        provider=SimpleNamespace(generate_json=AsyncMock(return_value=SimpleNamespace(data=output)))
        engine=InkFlowEngine(provider,Settings())
        async def run(candidate):
            return await engine._recover_review_evidence(project,candidate,"测试正文",packet,trace,
                agent_role="editor",system_prompt="核对归因",role_instruction="不放行未知",primary=True)
        with patch("inkflow.engine._review_evidence_gaps",return_value=["归因仍待核"]),patch(
                "inkflow.engine.HybridRetriever.recover_review_sources",return_value=([],{"queries":[],"searched_chapters":[]})) as search:
            result,_,record=asyncio.run(run(output))
            search.assert_not_called()
            assert result.verdict=="unknown" and record["status"]=="remaining_gaps"
            repair_prompt = provider.generate_json.call_args.kwargs["user_prompt"]
            assert '"source_comparisons"' in repair_prompt and '"focus_observation"' in repair_prompt
            assert '"RECENT_PLAN.md": "\\n## 第1章\\n在灯下核对来信。"' in repair_prompt
            assert '"field": "focus_observation.goal_evidence"' in repair_prompt
            assert "引文不添加‘第N章：’" in repair_prompt and "测试正文" in repair_prompt
            assert "@quote:0" in repair_prompt and "memory_patch必须完整返回" in repair_prompt
            requested=output.model_copy(update={"source_queries":["灯火 来源"]})
            asyncio.run(run(requested))
            search.assert_called_once()
            prior_quote="原件取出。"+"核对"*250+"原件放回。"
            packet.sections.append(ContextSection(key="D1",title="前章原文",content=prior_quote,source_ids=["chapter:00000"]))
            packet.sections.append(ContextSection(key="C",title="当前章节卡",content="当前卡只核对来信。",source_ids=["plan:chapter:00001"]))
            compared=output.model_copy(update={"source_comparisons":[ReviewSourceComparison(
                source_id="chapter:00000",source_evidence="旧报告摘错",chapter_evidence="测试正文",
                relation="aligned",reason="核对前章物件状态")],"assessments":[ReviewAssessment(
                criterion="requirements",status="met",grade=4,reason="核对当前章节卡",alternative="保留待核",
                chapter_evidence="测试正文",source_id="plan:chapter:00001",source_evidence="旧报告摘错")]})
            asyncio.run(run(compared))
            repair_prompt=provider.generate_json.call_args.kwargs["user_prompt"]
            choice_block=repair_prompt.split("# 本次可选择的逐字原文，代码按编号回填，语义仍由你核对\n")[1].split("\n已提供")[0]
            prior_choices=[item["quote"] for item in json.loads(choice_block).values() if item["source_id"]=="chapter:00000"]
            assert prior_quote[:400] in prior_choices and prior_quote[400:] in prior_choices
            assert all(len(quote)<=400 and quote in prior_quote for quote in prior_choices)
            assert any(item["source_id"]=="plan:chapter:00001" for item in json.loads(choice_block).values())
            assert "必须填写对应@quote:编号" in repair_prompt
            del packet.sections[-2:]
            original_packet=packet.model_copy(deep=True)
            packet.sections.append(ContextSection(key="F",title="低优先级参考",content="无关参考。"*9000,hard=False))
            engine.settings.context_hard_tokens=30_000
            engine.settings.context_soft_tokens=25_000
            provider.generate_json.reset_mock()
            _, compressed, record=asyncio.run(run(output))
            assert "低优先级参考" in record["compressed_sections"]
            assert compressed.sections[0].content==original_packet.sections[0].content
            assert len(next(section.content for section in compressed.sections if section.key=="F"))<=10_000
            assert provider.generate_json.call_args.kwargs["max_tokens"]==8_000
            engine.settings.context_hard_tokens=1_000
            provider.generate_json.reset_mock()
            _,_,record=asyncio.run(run(output))
            assert record["status"]=="context_budget_exhausted"
            provider.generate_json.assert_not_called()
            engine.settings=Settings()
            packet=original_packet
            async def concurrent_result():
                acquired=asyncio.Event()
                async def holder():
                    async with project_write_lock(project.root):
                        acquired.set()
                        await asyncio.sleep(0.1)
                owner=asyncio.create_task(holder())
                await acquired.wait()
                return SimpleNamespace(data=output),owner
            async def locked_run():
                result,owner=await concurrent_result()
                provider.generate_json.return_value=result
                restored=await run(output)
                await owner
                return restored
            _,_,record=asyncio.run(locked_run())
            assert record["status"]=="remaining_gaps", "Short concurrent writes must not discard model results"
        print("Review recovery query checks passed: loaded-packet attribution skips unrelated canon search; explicit source gaps search once; unknown gates remain.")
        notes={"chapter_version":1,"hook_note":{"annotations":[]}}
        failed={"artifact_id":"original","status":"current","data":{"status":"failed","source_hash":"body",
            "writer_notes_hash":notes_hash(notes),"story_fingerprint":"story"}}
        artifacts=[failed]
        def save(**kwargs):
            item={"artifact_id":str(len(artifacts)),"status":"current","data":dict(kwargs["data"])}
            artifacts.insert(0,item)
            return item
        fake=SimpleNamespace(root=project.root,db=SimpleNamespace(
            list_agent_artifacts=lambda **kwargs:artifacts,
            save_agent_artifact=save,
            set_agent_artifact_status=lambda identity,status: next(item for item in artifacts if item["artifact_id"]==identity).update(status=status)))
        provider.generate_json.reset_mock()
        provider.generate_json.return_value=SimpleNamespace(data=WriterNoteClarification(answers=["仅更正说明，不改正文"]))
        async def clarify(authorization=""):
            return await engine._clarify_writer_notes(fake,notes,["核对说明"],"测试正文",packet,trace,retry_authorization=authorization)
        with patch.object(engine,"_review_source_fingerprint",return_value="source"),patch.object(
                engine,"_writer_source_fingerprint",return_value="story"),patch("inkflow.engine.content_hash",return_value="body"):
            assert asyncio.run(clarify())["status"]=="failed"
            provider.generate_json.assert_not_called()
            answer=asyncio.run(clarify("本次明确额外授权一次"))
            assert answer["status"]=="answered" and answer["previous_artifact_id"]=="original"
            assert failed["data"]["status"]=="failed"
            assert asyncio.run(clarify("再次要求"))["reused"]
            provider.generate_json.assert_awaited_once()
        print("Writer note authorization checks passed: no automatic extra attempt; one recorded manual extra; original failure retained; no repeat.")
        selections=ModeCheckOutput.model_validate({"verdict":"unknown","confidence":0,"summary":"语义仍由责任角色核对",
            "focus_observation":{"goal_evidence":"@quote:0","change_evidence":"@quote:1"},
            "assessments":[{"criterion":"requirements","status":"met","grade":4,"reason":"目标完成","alternative":"保留其他合理解读","chapter_evidence":"@quote:0","source_id":"RECENT_PLAN.md","source_evidence":"@quote:1"}],
            "source_comparisons":[{"source_id":"RECENT_PLAN.md","source_evidence":"@quote:1","chapter_evidence":"@quote:0","relation":"aligned","reason":"本次目标一致"}],
            "memory_patch":{"chapter_no":1,"chapter_summary":"核对来信","facts":[{"fact_id":"f1","subject":"林安","predicate":"行动","value":"核对来信","valid_from_chapter":1,"evidence":"@quote:0"}]}})
        choices={"@quote:0":{"source_id":"当前正文","quote":"他在灯下核对来信。"},
                 "@quote:1":{"source_id":"RECENT_PLAN.md","quote":"在灯下核对来信。"}}
        located=resolve_review_quote_refs(selections,choices)
        assert located.focus_observation.goal_evidence=="他在灯下核对来信。"
        assert located.focus_observation.change_evidence=="@quote:1", "Planning excerpt cannot impersonate body evidence"
        assert located.source_comparisons[0].source_evidence=="在灯下核对来信。"
        assert located.assessments[0].chapter_evidence=="他在灯下核对来信。" and located.assessments[0].source_evidence=="在灯下核对来信。"
        assert located.memory_patch.facts[0].evidence=="他在灯下核对来信。" and located.verdict=="unknown"
        assert selections.memory_patch.facts[0].evidence=="@quote:0"
        assert resolve_review_quote_refs(selections,{}).model_dump()==selections.model_dump()
        mismatched=selections.model_copy(deep=True)
        mismatched.assessments[0].source_id="plan:chapter:00001"
        located=resolve_review_quote_refs(mismatched,choices)
        assert located.assessments[0].source_id=="RECENT_PLAN.md" and located.assessments[0].source_evidence=="在灯下核对来信。"
        assert mismatched.assessments[0].source_id=="plan:chapter:00001" and located.verdict=="unknown"
        mismatched.source_comparisons[0].source_id="OUTLINE.md"
        located=resolve_review_quote_refs(mismatched,choices)
        assert located.source_comparisons[0].source_id=="RECENT_PLAN.md", "A selected planning excerpt cannot claim another document was read"
        assert located.focus_observation.change_evidence=="@quote:1", "Planning selections cannot become prose evidence"
        memory_packet=ContextPacket(project_id="local",chapter_no=3,task="核对临时身份",estimated_tokens=0,sections=[
            ContextSection(key="B2",title="临时记忆",content=json.dumps([
                {"chapter_no":1,"summary":"第一章来信"},{"chapter_no":2,"summary":"第二章回信"}],ensure_ascii=False),
                source_ids=["batch-memory:b:chapter:00001","batch-memory:b:chapter:00002"])])
        sources=packet_sources(memory_packet)
        assert "第一章来信" in sources["batch-memory:b:chapter:00001"] and "第二章回信" not in sources["batch-memory:b:chapter:00001"]
        assert "第二章回信" in sources["batch-memory:b:chapter:00002"] and "第一章来信" not in sources["batch-memory:b:chapter:00002"]


if __name__=="__main__":
    check()
