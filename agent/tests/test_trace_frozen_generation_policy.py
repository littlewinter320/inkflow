"""Started traces and ledger diagnostics preserve the frozen request policy."""
import json
from dataclasses import replace

from inkflow.config import Settings
from inkflow.model_usage import ModelAttempt, UsageLedger
from inkflow.runtime import RunRuntime, active_runtime
from inkflow.task_settings import capture_task_settings, restore_task_settings, use_task_settings
from inkflow.trace import TraceRecorder


def test_trace_uses_frozen_profile_and_timeout_nulls_survive_replay(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"quality_preset": "economy"}), encoding="utf-8")
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(settings_path))
    base = Settings(provider_kind="mimo", model="mimo-v2.6-flash", base_url="https://token-plan-cn.xiaomimimo.com/v1",
                    workspace_root=tmp_path, quality_preset="balanced", quality_custom_context=True)
    runtime = RunRuntime(publish=lambda event: None)
    token = active_runtime.set(runtime)
    trace = TraceRecorder(tmp_path, "frozen-policy")
    try:
        cases = [("balanced", "coordinator", False, True, 360.0, 720.0),
                 ("economy", "writer", True, False, 180.0, 360.0),
                 ("economy", "editor", False, True, 180.0, 360.0),
                 ("quality", "memory_keeper", False, True, None, None)]
        attempts = []
        for index, (preset, role, requested, effective, soft, hard) in enumerate(cases):
            settings = replace(base, quality_preset=preset)
            scope = restore_task_settings(capture_task_settings(settings, novel_id="offline-trace"),
                                          novel_id="offline-trace", workspace_root=tmp_path)
            with use_task_settings(scope):
                trace.record_model_started(f"probe.{index}", model=settings.model, agent_role=role,
                                           max_tokens=1600, timeout_seconds=120, thinking=requested)
                metadata = trace.events[-1].metadata
                assert metadata["thinking"] is effective
                assert metadata["max_tokens"] == (settings.max_output_tokens if effective else 1600)
                assert metadata["timeout_seconds"] == metadata["request_timeout_seconds"] == hard
                assert metadata["soft_wait_seconds"] == soft and metadata["batch_chapter_count"] == 1
                attempt = ModelAttempt(settings, request_id=f"request-{index}", task_id=scope.task_id,
                    role=role, model=settings.model, input_estimate=100, input_bound=100, max_output_tokens=1600,
                    diagnostic={"request_timeout_seconds": hard, "soft_wait_seconds": soft,
                                "timeout_policy": metadata["timeout_policy"], "batch_chapter_count": 1})
                attempt.finish()
                attempts.append(attempt.attempt_id)
                fragment = runtime.attempt_fragments[-1]["diagnostic"]
                assert fragment["request_timeout_seconds"] == hard and fragment["soft_wait_seconds"] == soft
                assert fragment["batch_chapter_count"] == "1"  # Existing non-time diagnostics retain their format.
        trace.record_model_started("without.scope", model=base.model, agent_role="reviewer", max_tokens=1600,
                                   timeout_seconds=7, thinking=False)
        assert trace.events[-1].metadata["thinking"] is False
        assert trace.events[-1].metadata["timeout_seconds"] == 7 and trace.events[-1].metadata["max_tokens"] == 1600
        assert "soft_wait_seconds" not in trace.events[-1].metadata
        rows = [json.loads(line) for line in trace.events_path.read_text(encoding="utf-8").splitlines()]
        quality = next(row["metadata"] for row in rows if row["stage"] == "probe.3")
        assert quality["request_timeout_seconds"] is None and quality["soft_wait_seconds"] is None
        with UsageLedger()._connect() as db:
            for index, attempt_id in enumerate(attempts):
                diagnostic = json.loads(db.execute("SELECT diagnostic_json FROM model_attempts WHERE attempt_id=?",
                                                  (attempt_id,)).fetchone()[0])
                assert diagnostic["request_timeout_seconds"] == cases[index][5]
                assert diagnostic["soft_wait_seconds"] == cases[index][4]
                assert diagnostic["batch_chapter_count"] == "1"
    finally:
        active_runtime.reset(token)
