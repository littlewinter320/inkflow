"""Official MiMo endpoints follow live credentials, never a frozen task's identity."""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from inkflow.app_server import InkFlowAppService
from inkflow.config import Settings, load_user_settings, mimo_endpoint_for_key, save_user_settings
from inkflow.errors import ConfigurationError
from inkflow.task_settings import capture_task_settings, restore_task_settings


NORMAL = "https://api.xiaomimimo.com/v1"
SUBSCRIPTION = "https://token-plan-cn.xiaomimimo.com/v1"


@pytest.fixture
def local_config(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(settings_path))
    for name in ("INKFLOW_API_KEY", "MIMO_API_KEY", "INKFLOW_PROVIDER", "INKFLOW_BASE_URL", "INKFLOW_MODEL"):
        monkeypatch.delenv(name, raising=False)
    credential = {"key": "tp-offline-previous"}
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(
        get_password=lambda *_: credential["key"],
        set_password=lambda _service, _account, key: credential.update(key=key)))
    settings_path.write_text(json.dumps({"provider_kind": "mimo", "model": "mimo-v2.6-flash", "base_url": NORMAL}), encoding="utf-8")
    return settings_path, credential, object.__new__(InkFlowAppService)


async def no_events(_):
    raise AssertionError("设置读取与保存不应产生模型事件")


def test_blank_key_save_repairs_stale_official_endpoint(local_config):
    _, credential, service = local_config
    result = asyncio.run(service.dispatch("provider.configure", {
        "provider_kind": "mimo", "base_url": NORMAL, "api_key": "", "reasoning_effort": "high",
    }, no_events))
    assert result["base_url"] == load_user_settings()["base_url"] == SUBSCRIPTION
    assert result["model"] == "mimo-v2.6-flash" and result["provider_kind"] == "mimo"
    assert result["api_key_storage"] == "windows_credential_manager"
    assert credential["key"] not in str(result)


@pytest.mark.parametrize("old_key,new_key,endpoint", [
    ("tp-offline-old", "sk-offline-new", NORMAL),
    ("sk-offline-old", "tp-offline-new", SUBSCRIPTION),
])
def test_replacing_key_uses_new_type_after_validation(local_config, old_key, new_key, endpoint):
    _, credential, service = local_config
    credential["key"] = old_key
    result = asyncio.run(service.dispatch("provider.configure", {
        "provider_kind": "mimo", "base_url": NORMAL, "api_key": new_key,
    }, no_events))
    assert credential["key"] == new_key
    assert result["base_url"] == load_user_settings()["base_url"] == Settings.from_env().base_url == endpoint
    assert new_key not in str(result) and new_key not in str(load_user_settings())


def test_live_load_pairs_endpoint_but_frozen_mapping_and_task_stay_unchanged(local_config, tmp_path):
    path, _, _ = local_config
    original = json.loads(path.read_text(encoding="utf-8"))
    frozen = Settings.from_mapping(original)
    snapshot = capture_task_settings(frozen, novel_id="offline-book", task_id="original-task")
    restored = restore_task_settings(snapshot, novel_id="offline-book", workspace_root=tmp_path)
    assert Settings.from_env().base_url == SUBSCRIPTION
    assert frozen.base_url == restored.settings.base_url == NORMAL
    assert restored.snapshot_hash == snapshot["snapshot_hash"]
    assert json.loads(path.read_text(encoding="utf-8")) == original
    with pytest.raises(ConfigurationError, match="订阅专用地址"):
        restored.settings.require_api_key()


def test_environment_credential_override_is_effective_and_visible(local_config, monkeypatch):
    _, credential, service = local_config
    monkeypatch.setenv("MIMO_API_KEY", "tp-offline-environment")
    result = asyncio.run(service.dispatch("provider.configure", {
        "provider_kind": "mimo", "base_url": NORMAL, "api_key": "sk-offline-saved",
    }, no_events))
    assert credential["key"] == "sk-offline-saved"
    assert result["base_url"] == load_user_settings()["base_url"] == SUBSCRIPTION
    assert result["api_key_storage"] == "environment:MIMO_API_KEY"
    assert result["api_key_preview"].endswith("ment")
    assert "tp-offline-environment" not in str(result)


def test_status_preview_matches_saving_existing_credential(local_config):
    _, _, service = local_config
    result = asyncio.run(service.dispatch("provider.status", {
        "provider_kind": "mimo", "base_url": NORMAL, "model": "mimo-v2.6-flash",
    }, no_events))
    assert result["base_url"] == SUBSCRIPTION


@pytest.mark.parametrize("endpoint", [
    "https://custom-proxy.example/v1", "https://api.xiaomimimo.com:8443/v1",
    "https://api.xiaomimimo.com/custom/v1", "https://api.xiaomimimo.com/v1?proxy=yes",
])
def test_endpoint_pairing_does_not_redirect_proxy_or_custom_official_route(local_config, endpoint):
    assert mimo_endpoint_for_key(endpoint, "tp-offline") == endpoint
    saved = save_user_settings({"provider_kind": "mimo", "base_url": endpoint})
    assert saved["base_url"] == Settings.from_env().base_url == endpoint


def test_other_provider_is_not_redirected_by_mimo_like_key(local_config):
    saved = save_user_settings({"provider_kind": "custom", "base_url": NORMAL})
    assert saved["base_url"] == Settings.from_env().base_url == NORMAL
