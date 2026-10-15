"""Selected providers use distinct wire projections without dropping source material."""
import asyncio
import copy
import json

import httpx
from pydantic import BaseModel

from inkflow.config import Settings
from inkflow.errors import ProviderError
from inkflow.provider import DeepSeekProvider
from inkflow.schemas import ContextPacket, ContextSection
from inkflow.task_settings import capture_task_settings, restore_task_settings


class CachePolicyAnswer(BaseModel):
    answer: str


def test_provider_selection_changes_cache_projection_and_wire(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-test")
    payload = {"authority": "参考不是正史", "coverage": [{"selected_record_ids": ["r2", "r1"], "omitted": []}],
        "collections": [{"collection_id": "c1", "revision": 7, "records": [
            {"record_id": "r2", "revision": 2, "values": {"state": "原句甲。"}, "evidence_refs": [{"source_hash": "hash2"}]},
            {"record_id": "r1", "revision": 1, "values": {"state": "原句乙。"}, "evidence_refs": [{"source_hash": "hash1"}]}]}]}

    def packet(policy, changed=False):
        settings = copy.deepcopy(payload)
        if changed:
            settings["coverage"][0]["omitted"] = [{"record_id": "not-loaded", "reason": "本次不适用"}]
            settings["collections"][0]["records"].reverse()
        return ContextPacket(project_id="offline", chapter_no=52 if changed else 51, task="续写", estimated_tokens=0,
            cache_policy=policy, sections=[
                ContextSection(key="J", title="本书约定", content="不可越过的原要求", hard=True, cache_scope="book"),
                ContextSection(key="B", title="客观事实", content="带版本的事实原文", hard=True, cache_scope="canon"),
                ContextSection(key="D", title="人物状态", content="新状态" if changed else "旧状态", hard=True, cache_scope="canon"),
                ContextSection(key="CSET", title="设定", content=json.dumps(settings, ensure_ascii=False),
                               source_ids=["r2", "r1"], hard=True, cache_scope="canon"),
                ContextSection(key="G", title="伏笔", content="已推进" if changed else "未推进", hard=True, cache_scope="canon"),
                ContextSection(key="A", title="用户本次要求", content="写下一章", hard=True)])

    mimo, later = packet("mimo"), packet("mimo", True)
    gate_before = mimo.gate_material()
    prefix, tail = mimo.model_prompt_parts()
    assert prefix == later.model_prompt_parts()[0]
    assert "旧状态" in tail and "未推进" in tail and "coverage" in tail
    assert "coverage" not in prefix and "原句甲。" in prefix and "hash2" in prefix
    assert mimo.gate_material() == gate_before and json.loads(mimo.sections[3].content) == payload
    sections = {item.key: item for item in mimo.model_sections()}
    assert json.loads(sections["CSET-coverage"].content)["coverage"] == payload["coverage"]
    assert {item["record_id"] for item in json.loads(sections["CSET"].content)["collections"][0]["records"]} == {"r1", "r2"}
    deepseek = packet("deepseek")
    ds_prefix, ds_tail = deepseek.model_prompt_parts()
    assert ds_prefix.index("## B.") < ds_prefix.index("## CSET.") < ds_prefix.index("## D.") < ds_prefix.index("## G.")
    assert "coverage" not in ds_prefix and "coverage" in ds_tail
    assert "旧状态" in ds_prefix and "未推进" in ds_prefix

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "response-model", "choices": [{"finish_reason": "stop",
            "message": {"content": '{"answer":"ok"}'}}], "usage": {"prompt_tokens": 100,
            "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 80}}})

    client = httpx.AsyncClient
    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs:
        client(transport=httpx.MockTransport(respond), **kwargs))

    for kind, url, model in (("deepseek", "https://api.deepseek.com/v1", "deepseek-flash"),
                             ("mimo", "https://token-plan-cn.xiaomimimo.com/v1", "mimo-v2.6-flash"),
                             ("openai", "https://api.deepseek.com/v1", "deepseek-flash")):
        selected = Settings(provider_kind=kind, base_url=url, model=model, workspace_root=tmp_path)
        frozen = capture_task_settings(selected, novel_id="offline")
        restored = restore_task_settings(frozen, novel_id="offline", workspace_root=tmp_path).settings
        assert restored.prompt_cache_policy == ("mimo" if kind == "mimo" else "deepseek")
        compiled = packet(restored.prompt_cache_policy)
        stable, _ = compiled.model_prompt_parts()
        result = asyncio.run(DeepSeekProvider(restored).generate_json(system_prompt="相同责任合同",
            user_prompt=compiled.to_model_prompt(), cache_prefix=stable, output_model=CachePolicyAnswer,
            agent_role="writer", thinking=False, max_tokens=100))
        sent = requests[-1]
        assert result.data.answer == "ok" and result.usage["cache_prefix_diagnostic"]["cache_policy"] == restored.prompt_cache_policy
        assert "prompt_cache_key" not in sent and "cache_control" not in sent
        if kind == "mimo":
            assert len(sent["messages"]) == 2 and sent["stream"] and "max_completion_tokens" in sent
            assert sent["messages"][1]["content"] == compiled.to_model_prompt()
        else:
            assert len(sent["messages"]) == 3 and not sent["stream"] and "max_tokens" in sent
            assert "".join(item["content"] for item in sent["messages"][1:]) == compiled.to_model_prompt()
    assert len(requests) == 3
    try:
        asyncio.run(DeepSeekProvider(selected).generate_json(system_prompt="合同", user_prompt="输入",
            cache_prefix="不同来源", output_model=CachePolicyAnswer, agent_role="writer", thinking=False))
    except ProviderError:
        pass
    else:
        raise AssertionError("Mismatched cache prefix must fail before HTTP")
    assert len(requests) == 3
