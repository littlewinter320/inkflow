import asyncio
import sys
from types import SimpleNamespace

import pytest

from inkflow.app_server import InkFlowAppService
from inkflow.config import save_user_settings
from inkflow.errors import ConfigurationError


@pytest.mark.parametrize("initial_provider,stored_generation,patch,role_label", [
    ("deepseek", {"writer": {"temperature": 1.6}}, {}, "创作温度"),
    ("deepseek", {"memory_keeper": {"top_p": .005}}, {}, "记忆整理 Top P"),
    ("mimo", {"editor": {"temperature": .4, "top_p": .7}}, {"editor": {"top_p": .005}}, "编辑 Top P"),
])
def test_mimo_merged_sampling_rejects_before_changing_disk_or_credential(
        tmp_path, monkeypatch, initial_provider, stored_generation, patch, role_label):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(settings_path))
    for name in ("INKFLOW_API_KEY", "MIMO_API_KEY", "INKFLOW_PROVIDER", "INKFLOW_BASE_URL", "INKFLOW_MODEL"):
        monkeypatch.delenv(name, raising=False)
    credential = {"key": "sk-offline-original", "writes": 0}

    def set_password(_service, _account, key):
        credential.update(key=key, writes=credential["writes"] + 1)

    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(
        get_password=lambda *_: credential["key"], set_password=set_password))
    save_user_settings({"provider_kind": initial_provider,
        "base_url": "https://api.xiaomimimo.com/v1" if initial_provider == "mimo" else "https://api.deepseek.com",
        "model": "mimo-v2.6-flash" if initial_provider == "mimo" else "deepseek-v4-flash",
        "agent_generation": stored_generation})
    original = settings_path.read_bytes()

    async def no_events(_):
        raise AssertionError("非法设置不应触发模型任务")

    with pytest.raises(ConfigurationError, match=role_label):
        asyncio.run(object.__new__(InkFlowAppService).dispatch("provider.configure", {
            "provider_kind": "mimo", "base_url": "https://api.xiaomimimo.com/v1",
            "model": "mimo-v2.6-flash", "api_key": "tp-offline-replacement",
            "agent_generation": patch,
        }, no_events))
    assert settings_path.read_bytes() == original
    assert credential == {"key": "sk-offline-original", "writes": 0}


def test_nested_settings_diff_preserves_unsent_fields_and_roles(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    before = save_user_settings({"provider_kind": "deepseek", "base_url": "https://api.deepseek.com",
        "agent_generation": {"writer": {"temperature": .8, "top_p": .67, "top_k": 17}, "editor": {"top_p": .71}},
        "agent_context_budgets": {"writer": {"soft": 250000, "hard": 460000}, "editor": {"soft": 240000, "hard": 384000}},
        "role_models": {"writer": "writer-current", "editor": "editor-current"}})
    after = save_user_settings({"agent_generation": {"writer": {"temperature": .95}},
        "agent_context_budgets": {"writer": {"soft": 300000}}, "role_models": {"writer": ""}})
    assert after["agent_generation"]["writer"] == {"temperature": .95, "top_p": .67, "top_k": 17}
    assert after["agent_context_budgets"]["writer"] == {"soft": 300000, "hard": 460000}
    assert after["role_models"] == {"writer": "", "editor": "editor-current"}
    for name in ("agent_generation", "agent_context_budgets"):
        assert {role: value for role, value in after[name].items() if role != "writer"} == {
            role: value for role, value in before[name].items() if role != "writer"}
