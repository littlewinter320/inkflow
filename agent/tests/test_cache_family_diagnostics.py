import json

from inkflow.config import Settings
from inkflow.model_usage import UsageLedger


def test_writer_cache_compares_same_contract_across_auxiliary_calls(tmp_path):
    (tmp_path / ".inkflow").mkdir()
    (tmp_path / ".inkflow/project.json").write_text(json.dumps({"project_id": "cache-book"}), encoding="utf-8")
    settings = Settings(workspace_root=tmp_path, provider_kind="deepseek", model="offline")
    ledger = UsageLedger(tmp_path / "usage.sqlite")
    ids = []
    for index, (family, hit) in enumerate((("draft", 80), ("expansion", 0), ("draft", 40))):
        attempt = ledger.begin(settings, request_id=f"request-{index}", model="offline", role="writer",
            input_estimate=100, input_bound=100, max_output_tokens=10, prompt_family=family,
            diagnostic={"stage": family, "materials_hash": "same", "quality_hash": "same",
                "system_contract_hash": family, "user_prefix_4096_hash": "same",
                "reference_prefix_hash": "first" if index == 0 else "changed"})
        ids.append(attempt)
        ledger.settle(attempt, {"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": hit})
        ledger.finish(attempt, "completed", duration_ms=5)
    result = ledger.recent_deepseek_cache("cache-book")
    comparisons = result["performance_diagnostic"]["writer_sequence"]
    assert len(comparisons) == 1
    assert comparisons[0]["before_attempt_id"] == ids[0] and comparisons[0]["after_attempt_id"] == ids[2]
    assert comparisons[0]["reference_prefix_changed"] and comparisons[0]["observed_decline"]
    assert not comparisons[0]["same_request_retry"]
    assert result["calls"] == 3 and result["input_tokens"] == 300 and result["hit_rate"] == 0.4
