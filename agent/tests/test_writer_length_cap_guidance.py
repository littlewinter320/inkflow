import asyncio

import pytest

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.errors import ValidationGateError
from inkflow.provider import ScriptedProvider
from inkflow.runtime import RunRuntime, active_runtime
from inkflow.schemas import DraftOutput
from inkflow.trace import TraceRecorder


def test_completed_writer_keeps_length_reason_and_candidate_before_cap_blocks_repair(tmp_path, monkeypatch):
    engine = InkFlowEngine(ScriptedProvider([]), Settings(writer_task_attempt_limit=1, workspace_root=tmp_path))
    runtime = RunRuntime(publish=lambda event: None, task_id="original-task")
    runtime.attempt_fragments = [{"role": "writer", "request_sent": True},
                                 {"role": "writer", "request_sent": False}]

    async def forbidden_reservation(*args, **kwargs):
        raise AssertionError("an exhausted request cap must not reserve a length-repair attempt")

    monkeypatch.setattr(engine, "_native_recovery_attempts", forbidden_reservation)

    async def scenario():
        for count in (200, 700):
            trace = TraceRecorder(tmp_path, "length-cap")
            draft = DraftOutput(title="旧信", content="字" * count)
            with pytest.raises(ValidationGateError, match=f"有效 {count} 字.*360～440.*未发送") as failed:
                await engine._repair_short_draft(draft, draft.content, chapter_no=51,
                    card={"target_words": 400}, base_prompt="", system_prompt="", trace=trace,
                    stage="writer.length_repair")
            assert failed.value.recovery_exhausted and failed.value.recovery_node == "writer.length"
            assert (trace.run_dir / "recovery-candidate.md").read_text(encoding="utf-8") == draft.content
        valid = DraftOutput(title="旧信", content="字" * 400)
        assert await engine._repair_short_draft(valid, valid.content, chapter_no=51,
            card={"target_words": 400}, base_prompt="", system_prompt="", trace=TraceRecorder(tmp_path, "length-ok"),
            stage="writer.length_repair") == (valid, valid.content)

    token = active_runtime.set(runtime)
    try:
        asyncio.run(scenario())
    finally:
        active_runtime.reset(token)
    assert not engine.provider.calls and runtime.calls == 0
