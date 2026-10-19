"""Current model observations cannot borrow another route/model's cache hits."""
import json
from dataclasses import replace
from types import SimpleNamespace

from inkflow.app_server import _usage_overview
from inkflow.config import Settings
from inkflow.database import ProjectDatabase
from inkflow.model_usage import UsageLedger, cache_endpoint


def test_cache_statistics_isolate_frozen_endpoint_request_model_and_unknowns(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    internal = tmp_path / ".inkflow"
    internal.mkdir()
    (internal / "project.json").write_text(json.dumps({"project_id": "offline-scopes"}), encoding="utf-8")
    settings = Settings(provider_kind="mimo", base_url="https://TOKEN-PLAN-CN.xiaomimimo.com:443/v1/",
        model="mimo-v2.6-flash", workspace_root=tmp_path, role_models={"editor": "mimo-v2.6-pro"},
        writer_task_attempt_limit=1)
    ledger = UsageLedger()
    count = 0

    def add(config=settings, model=None, role="writer", prompt=1000, hit=700, sent=True,
            response_model=None, legacy=False, family="DraftOutput", quality="thinking", thinking="enabled"):
        nonlocal count
        count += 1
        attempt = ledger.begin(config, request_id=f"request-{count}", task_id=f"task-{count}",
            model=model or config.model, role=role, input_estimate=1000, input_bound=1000, max_output_tokens=50,
            prompt_family=f"{config.provider_kind}:{model or config.model}:{role}:{family}",
            diagnostic={"stage": family, "system_contract_hash": "schema-one", "quality_hash": quality,
                        "cache_policy": "mimo", "message_layout": "system,reference,request", "thinking": thinking,
                        "reference_prefix_tokens": 750})
        if sent:
            ledger.mark_request_sent(attempt, max_task_role_attempts=1 if role == "writer" else None)
        if prompt is not None:
            ledger.settle(attempt, {"prompt_tokens": prompt, "completion_tokens": 50,
                                   "prompt_tokens_details": {"cached_tokens": hit}}, response_model=response_model)
        ledger.finish(attempt, "completed" if prompt is not None else "failed", request_sent=sent)
        if legacy:
            with ledger._connect() as db:
                row = db.execute("SELECT diagnostic_json FROM model_attempts WHERE attempt_id=?", (attempt,)).fetchone()
                diagnostic = json.loads(row[0])
                diagnostic.pop("request_endpoint")
                db.execute("UPDATE model_attempts SET diagnostic_json=? WHERE attempt_id=?", (json.dumps(diagnostic), attempt))
        return attempt

    add(response_model="mimo-v2.6-flash-response-alias")
    add(prompt=None, thinking="disabled", quality="nonthinking")  # Unknown usage is not a zero-hit input.
    add(prompt=None, sent=False)  # A gate/queue stop is not an actual request.
    add(replace(settings, base_url="https://token-plan-cn.xiaomimimo.com/other/v1"), hit=0)
    add(replace(settings, base_url="https://token-plan-cn.xiaomimimo.com:8443/v1"), hit=1000)
    add(replace(settings, provider_kind="deepseek", base_url="https://api.deepseek.com", model="deepseek-v4-flash"), hit=1000)
    add(model="mimo-v2.6-pro", hit=1000)
    add(model="mimo-v2.6-flash-response-alias", hit=0, response_model="mimo-v2.6-flash")
    add(model="mimo-v2.6-pro", role="editor", prompt=500, hit=400, family="ReviewReport")
    add(role="editor", hit=0, family="ReviewReport")
    add(hit=1000, legacy=True)
    add(replace(settings, base_url="https://private:credential@token-plan-cn.xiaomimimo.com/v1?key=secret"), hit=1000)
    assert cache_endpoint(settings.base_url) == "https://token-plan-cn.xiaomimimo.com/v1"
    assert cache_endpoint("https://private:credential@host/v1?key=secret") is None
    with ledger._connect() as db:
        before = [tuple(row) for row in db.execute("SELECT attempt_id,request_sent,usage_json,diagnostic_json FROM model_attempts ORDER BY rowid")]
        assert all("credential" not in (row[3] or "") and "secret" not in (row[3] or "") for row in before)
        assert json.loads(before[0][3])["reference_prefix_tokens"] == "750"
    scoped = ledger.cache_observations("offline-scopes", settings)
    writer = scoped["by_agent_role"]["writer"]
    assert writer["model"] == "mimo-v2.6-flash" and writer["calls"] == 2 and writer["not_sent_calls"] == 1
    assert writer["prompt_cache_hit_rate"] == .7 and writer["cache_reported_input_tokens"] == 1000
    assert writer["cache_unknown_calls"] == 1 and writer["input_unknown_calls"] == 1
    assert writer["by_contract"][0]["response_models"] == ["mimo-v2.6-flash-response-alias"]
    assert {group["thinking"] for group in writer["by_contract"]} == {"enabled", "disabled"}
    assert scoped["by_agent_role"]["editor"]["model"] == "mimo-v2.6-pro"
    assert scoped["by_agent_role"]["editor"]["prompt_cache_hit_rate"] == .8
    assert scoped["by_agent_role"]["reviewer"]["prompt_cache_hit_rate"] is None
    assert scoped["unscoped_calls"] == 2 and len(scoped["history_groups"]) == 9

    run = internal / "runs" / "history"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(json.dumps({"stage": "writer.model", "metadata": {
        "model": "historical-model", "agent_role": "writer", "usage": {
            "prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 99}}}), encoding="utf-8")
    project = SimpleNamespace(internal=internal, db=ProjectDatabase(internal / "inkflow.db"), project_id="offline-scopes")
    overview = _usage_overview(project, settings)
    assert overview["by_agent_role"]["writer"]["prompt_cache_hit_rate"] == .99  # Original audit stays historical.
    assert overview["current_model_cache"]["by_agent_role"]["writer"]["prompt_cache_hit_rate"] == .7
    with ledger._connect() as db:
        after = [tuple(row) for row in db.execute("SELECT attempt_id,request_sent,usage_json,diagnostic_json FROM model_attempts ORDER BY rowid")]
    assert after == before and settings.writer_task_attempt_limit == 1
