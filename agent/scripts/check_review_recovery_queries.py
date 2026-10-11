"""Only missing source evidence searches canon; attribution repair reuses the loaded packet."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.project import InkFlowProject
from inkflow.schemas import BookBrief, ContextPacket, ModeCheckOutput, ReviewAssessment


def check():
    with TemporaryDirectory(prefix="review-recovery-",dir="D:/墨流/cache") as folder:
        project=InkFlowProject.create(Path(folder)/"book",BookBrief(title="离线归因",genre="现实",protagonist="林安",premise="仅核对补读选择和来源预算，不生成小说。"))
        trace=SimpleNamespace(run_dir=Path(folder),record=lambda *a,**k:None,record_model=lambda *a,**k:None)
        packet=ContextPacket(project_id=project.project_id,chapter_no=1,task="归因核验",sections=[],estimated_tokens=0)
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
            requested=output.model_copy(update={"source_queries":["灯火 来源"]})
            asyncio.run(run(requested))
            search.assert_called_once()
        print("Review recovery query checks passed: loaded-packet attribution skips unrelated canon search; explicit source gaps search once; unknown gates remain.")


if __name__=="__main__":
    check()
