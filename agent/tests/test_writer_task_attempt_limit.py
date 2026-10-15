"""A task's explicit Writer cap survives new calls and ledger instances."""
import json
from unittest.mock import patch

import pytest

from inkflow.config import Settings
from inkflow.errors import ProviderError
from inkflow.model_usage import ModelAttempt, UsageLedger
from inkflow.task_settings import capture_task_settings, restore_task_settings
from inkflow.utils import content_hash


def test_writer_task_attempt_limit_is_persistent_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    settings = Settings.from_mapping({"writer_task_attempt_limit": 1})
    snapshot = capture_task_settings(settings, novel_id="offline-book")
    assert snapshot["settings"]["writer_task_attempt_limit"] == 1
    legacy = json.loads(json.dumps(snapshot))
    legacy["settings"].pop("writer_task_attempt_limit")
    legacy["snapshot_hash"] = content_hash(json.dumps({k: v for k, v in legacy.items() if k != "snapshot_hash"},
                                          ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    assert restore_task_settings(legacy, novel_id="offline-book", workspace_root=tmp_path).settings.writer_task_attempt_limit is None
    assert Settings.from_mapping({"writer_task_attempt_limit": None}).writer_task_attempt_limit is None

    def attempt(role="writer", task="one-authorized-task"):
        return ModelAttempt(settings, request_id="independent-request", model=settings.model, role=role,
                            task_id=task, input_estimate=10, input_bound=10, max_output_tokens=10)

    first = attempt()
    first.mark_request_sent(max_task_role_attempts=1)
    second = attempt()
    assert first.ledger is not second.ledger and not first.finished
    with pytest.raises(ProviderError, match="已用完") as blocked:
        second.mark_request_sent(max_task_role_attempts=1)
    assert blocked.value.recovery_exhausted and not second.request_sent
    second.finish()
    first.finish()
    third = attempt()
    with pytest.raises(ProviderError, match="已用完"):
        third.mark_request_sent(max_task_role_attempts=1)
    third.finish()
    editor = attempt(role="editor")
    editor.mark_request_sent()
    assert editor.request_sent
    editor.finish()
    unrelated = attempt(task="another-authorized-task")
    unrelated.mark_request_sent(max_task_role_attempts=1)
    unrelated.finish()
    with UsageLedger()._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM model_attempts WHERE task_id=? AND role='writer' AND request_sent=1",
                          ("one-authorized-task",)).fetchone()[0] == 1

    old = attempt(task="unknown-history")
    with old.ledger._connect() as db:
        db.execute("UPDATE model_attempts SET request_sent=NULL WHERE attempt_id=?", (old.attempt_id,))
    unknown_history = attempt(task="unknown-history")
    with pytest.raises(ProviderError, match="发送状态未知"):
        unknown_history.mark_request_sent(max_task_role_attempts=1)
    assert not unknown_history.request_sent
    no_task = attempt(task="")
    with pytest.raises(ProviderError, match="任务身份"):
        no_task.mark_request_sent(max_task_role_attempts=1)
    with patch("inkflow.model_usage.UsageLedger", side_effect=OSError("offline unreadable ledger")):
        with pytest.warns(RuntimeWarning, match="账本暂不可用"):
            unavailable = attempt()
    with pytest.raises(ProviderError, match="账本不可用") as unavailable_error:
        unavailable.mark_request_sent(max_task_role_attempts=1)
    assert unavailable_error.value.recovery_exhausted and not unavailable.request_sent
    unavailable.mark_request_sent()
    assert unavailable.request_sent  # Uncapped requests retain the existing behavior.
