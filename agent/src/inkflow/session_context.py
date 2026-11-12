"""持续角色上下文：追加已闭合回合，近满时以当前完整资料替换旧过程。"""
from __future__ import annotations

import json
import re
import sqlite3
import warnings
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonpatch

from .config import Settings
from .conversation_scope import active_conversation
from .utils import content_hash, utc_now


_BASE_INDEX = "\n\n# 持续会话资料索引（仅定位已有原文）\n"


def _continuity(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    """提取公开交付中的决定、依据及未完成项；不读取或生成隐藏思考链。"""
    records = []
    keys = {"summary", "findings", "missing_source_ids", "source_queries", "questions",
            "next_action", "steps", "preserve_constraints", "requested_outcome", "remaining"}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        try:
            value = json.loads(message["content"])
        except (ValueError, TypeError):
            # 未确认返回保留原文供补救；不能因结构不合格而在重建时丢掉已有工作。
            records.append({"source_hash": content_hash(message["content"]),
                            "record": {"unparsed_candidate": message["content"]}})
            continue
        if isinstance(value, dict):
            record = {key: value[key] for key in sorted(keys & value.keys()) if value[key]}
            if record:
                records.append({"source_hash": content_hash(message["content"]), "record": record})
    return records


def _parts(prompt: str, *, structured: bool = False) -> list[str]:
    """保留每个字符及顺序；窄补核按原JSON字段分块，避免改一个坏槽重发全部来源。"""
    pattern = r"(?m)(?=^#{1,3} )"
    if structured:
        pattern = r"(?m)(?=^#{1,3} |^【本次交付格式】|^【本次工作方法】)"
    parts = [part for part in re.split(pattern, prompt) if part]
    if not structured:
        return parts  # 旧索引必须按原标题分段回证，不能用新分法判定历史损坏。
    result, decoder = [], json.JSONDecoder()
    for part in parts:
        boundaries, consumed = [0], 0
        for match in re.finditer(r"(?m)^\{", part):
            if match.start() < consumed:
                continue
            try:
                value, end = decoder.raw_decode(part, match.start())
            except ValueError:
                continue
            consumed = end
            if not isinstance(value, dict) or not (
                    {"current_body", "sources", "source_hashes"} <= value.keys()
                    or {"findings", "chapter_content"} <= value.keys()):
                continue
            # 只切原字节，不改写JSON、不删引文；当前清单按顺序拼回完整原任务。
            cursor = match.start() + 1
            while cursor < end:
                cursor += len(part[cursor:]) - len(part[cursor:].lstrip())
                if part[cursor] == "}":
                    break
                boundaries.append(cursor)
                _, cursor = decoder.raw_decode(part, cursor)
                cursor += len(part[cursor:]) - len(part[cursor:].lstrip())
                cursor += 1  # 上面完整JSON解析已证明这里是字段和值之间的冒号。
                cursor += len(part[cursor:]) - len(part[cursor:].lstrip())
                _, cursor = decoder.raw_decode(part, cursor)
                cursor += len(part[cursor:]) - len(part[cursor:].lstrip())
                if part[cursor] == ",":
                    cursor += 1
                else:
                    break
            boundaries.append(end)
        boundaries = sorted(set([*boundaries, len(part)]))
        result.extend(part[start:end] for start, end in zip(boundaries, boundaries[1:]) if start < end)
    return result


def _part_index(parts: list[str]) -> list[dict[str, Any]]:
    """索引位于完整原文之后；模型可按标题与次序找到原文，发送前缀保持原样。"""
    return [{"id": content_hash(part)[:20], "section": part.splitlines()[0][:160], "part": number}
            for number, part in enumerate(parts, 1)]


def _dump_json(value: Any, style: str) -> str:
    """仅复用项目已有两种JSON表示，重建不等字节时回退整块。"""
    if style == "pretty":
        return json.dumps(value, ensure_ascii=False, indent=2)
    if style != "canonical":
        raise ValueError("未知资料JSON表示")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_block(body: str) -> tuple[str, Any, str, str] | None:
    """识别可逐字还原的项目JSON；小说原文与未知格式不做增量补丁。"""
    match = re.search(r"(?m)^[\[{]", body)
    if match is None:
        return None
    try:
        value, end = json.JSONDecoder().raw_decode(body, match.start())
    except ValueError:
        return None
    for style in ("canonical", "pretty"):
        if body[match.start():end] == _dump_json(value, style):
            return body[:match.start()], value, body[end:], style
    return None


def _patch_allowed(prefix: str, operations: list[dict[str, Any]], value: Any = None) -> bool:
    """仅动态资料可补丁；硬规则、正文、输出格式和规划合同定义保留完整原文。"""
    match = re.match(r"^## (F|F1|MEMORY|WORK|RECOVERY|CSET-records|P0)\.", prefix)
    if match is None:
        # 原责任角色的窄补核使用无标题JSON数据；只识别实际两类结构，不泛化到任意指令或输出Schema。
        return not prefix.strip() and isinstance(value, dict) and (
            {"current_body", "sources", "source_hashes"} <= value.keys()
            or {"findings", "chapter_content"} <= value.keys())
    if match[1] == "P0":
        allowed = {"prior_threads", "beats", "chapter_priority"}

        def allowed_path(path: Any) -> bool:
            """不允许借move/copy从固定合同定义取值或移除原定义。"""
            return isinstance(path, str) and path.startswith("/") and path.split("/", 2)[1] in allowed

        return all(isinstance(op, dict) and isinstance(op.get("path"), str)
            and allowed_path(op["path"]) and ("from" not in op or allowed_path(op["from"])) for op in operations)
    return True


def _replay_blocks(messages: list[dict[str, str]], *, wire_blocks: dict[str, str] | None = None) -> dict[str, str]:
    """从实际已发送的原文及可验证JSON补丁恢复资料，不能凭缓存清单声称已读。"""
    blocks: dict[str, str] = {}
    for message in messages[::2]:
        text = message["content"]
        if _BASE_INDEX in text:
            original, index = text.rsplit(_BASE_INDEX, 1)
            index_value = json.loads(index)
            if not isinstance(index_value, dict) or index_value.get("block_layout", 1) not in {1, 2}:
                raise ValueError("未知持续资料索引格式")
            parts = _parts(original, structured=index_value.get("block_layout", 1) == 2)
            if index_value.get("blocks") != _part_index(parts):
                raise ValueError("首轮原文与会话索引不符")
            additions = [(content_hash(part)[:20], part) for part in parts]
        else:
            additions = []
            for kind, identity, body in re.findall(r'<context-(block|patch) id="([0-9a-f]{20})">\n(.*?)\n</context-\1>\n', text, flags=re.S):
                if wire_blocks is not None:
                    # 失效占用按实际发过的补丁文本估算，不用重建后的大对象夸大历史压力。
                    wire_blocks[identity] = body
                if kind == "patch":
                    patch = json.loads(body)
                    source = _json_block(blocks[patch["base"]])
                    if (source is None or patch["prefix"].splitlines()[:1] != source[0].splitlines()[:1]
                            or not _patch_allowed(patch["prefix"], patch["operations"], source[1])):
                        raise ValueError("历史增量缺少完整来源或触及不可增量的资料")
                    value = jsonpatch.apply_patch(source[1], patch["operations"], in_place=False)
                    if not _patch_allowed(patch["prefix"], patch["operations"], value):
                        raise ValueError("增量改变了资料结构身份")
                    body = patch["prefix"] + _dump_json(value, patch.get("style", "canonical")) + patch["suffix"]
                additions.append((identity, body))
        for identity, body in additions:
            if (content_hash(body)[:20] != identity or identity in blocks and blocks[identity] != body):
                raise ValueError("历史资料块原文无法核验")
            blocks[identity] = body
            if wire_blocks is not None:
                wire_blocks.setdefault(identity, body)
    return blocks


def _block_patch(body: str, blocks: dict[str, str], *, metadata_only: bool = False) -> str | None:
    """复用已安装JSON Patch库；仅接受可逐字还原、且明显小于整块的变化。"""
    # v20关键事实、事件、设定、承诺及待办整块提供；不要求模型心算跨轮补丁才能判断剧情。
    if metadata_only and not re.match(r"^## (MEMORY|RECOVERY)\.", body):
        return None
    if len(body) < 4096 or (current := _json_block(body)) is None:
        return None
    heading = body.splitlines()[0]
    for identity, previous in reversed(list(blocks.items())):
        if previous.splitlines()[0] != heading or (old := _json_block(previous)) is None:
            continue
        if not _patch_allowed(old[0], [], old[1]):
            continue
        operations = jsonpatch.make_patch(old[1], current[1]).patch
        if not _patch_allowed(current[0], operations, current[1]):
            return None
        value = jsonpatch.apply_patch(old[1], operations, in_place=False)
        if current[0] + _dump_json(value, current[3]) + current[2] != body:
            return None
        patch = json.dumps({"base": identity, "prefix": current[0], "operations": operations,
                            "suffix": current[2], "style": current[3]}, ensure_ascii=False, separators=(",", ":"))
        return patch if len(patch) < len(body) * .55 else None
    return None


def response_input_key(raw: dict[str, Any], expected: Any, settings: Settings) -> str | None:
    """恢复时把真实持续请求绑定回原完整资料；双哈希须由原付费账本同时证明。"""
    wire = raw.get("input_hash")
    if not isinstance(wire, str):
        return None
    if wire in expected:
        return wire
    logical = raw.get("logical_input_hash")
    from .task_settings import active_task_settings
    from .model_usage import usage_ledger_path, cache_endpoint
    scope = active_task_settings.get()
    identity = raw.get("task_settings", {})
    if (settings.prompt_cache_layout_version < 17 or scope is None or not isinstance(logical, str)
            or logical not in expected or not isinstance(identity, dict)
            or identity.get("task_id") != scope.task_id or identity.get("snapshot_hash") != scope.snapshot_hash
            or identity.get("novel_id") != scope.novel_id):
        return None
    path = usage_ledger_path()
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            row = connection.execute("SELECT task_id,novel_id,role,model,request_id,request_sent,diagnostic_json FROM model_attempts WHERE attempt_id=?",
                                     (raw.get("attempt_id"),)).fetchone()
        if row is None:
            return None
        diagnostic = json.loads(row[6] or "{}")
        if (row[:6] != (scope.task_id, scope.novel_id, raw.get("role"), raw.get("model"), raw.get("request_id"), 1)
                or diagnostic.get("materials_hash") != wire or diagnostic.get("logical_input_hash") != logical
                or diagnostic.get("stage") != raw.get("schema")
                or diagnostic.get("request_endpoint") != cache_endpoint(settings.base_url)):
            return None
        return logical
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None  # 缺账本证明保持未核验，不用本地映射替代真实发送证据。


@dataclass
class SessionContext:
    """一次请求的乐观版本票据；不拥有小说、工具执行或审核权限。"""

    path: Path
    key: str
    revision: int
    state: dict[str, Any]
    messages: list[dict[str, str]]
    diagnostic: dict[str, Any]

    @classmethod
    def prepare(cls, settings: Settings, *, role: str | None, model: str, system: str,
                prompt: str, contract: str) -> SessionContext | None:
        """按书、聊天、角色及实际请求合同隔离；旧任务和无项目请求保持原输入。"""
        from .task_settings import active_task_settings
        scope = active_task_settings.get()
        if (settings.prompt_cache_layout_version < 17 or scope is None or not role
                or settings.workspace_root is None):
            return None
        root = Path(settings.workspace_root).resolve()
        if scope.settings.workspace_root is None or Path(scope.settings.workspace_root).resolve() != root:
            return None
        if not (root / ".inkflow" / "inkflow.db").is_file():
            return None
        path = root / ".inkflow" / "session-context.sqlite"
        key = content_hash(json.dumps([scope.novel_id, active_conversation.get(), role,
            settings.base_url.rstrip("/"), model, system, contract], ensure_ascii=False))
        try:
            with closing(sqlite3.connect(path, timeout=5)) as connection, connection:
                connection.execute("CREATE TABLE IF NOT EXISTS context_sessions (session_key TEXT PRIMARY KEY, revision INTEGER NOT NULL, state_json TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS context_events (session_key TEXT NOT NULL, revision INTEGER NOT NULL, event_json TEXT NOT NULL, PRIMARY KEY(session_key,revision))")
                connection.execute("CREATE TABLE IF NOT EXISTS context_inputs (session_key TEXT PRIMARY KEY, revision INTEGER NOT NULL, state_json TEXT NOT NULL)")
                row = connection.execute("SELECT revision,state_json FROM context_sessions WHERE session_key=?", (key,)).fetchone()
                saved_input = connection.execute("SELECT revision,state_json FROM context_inputs WHERE session_key=?", (key,)).fetchone() if settings.prompt_cache_layout_version >= 19 else None
            revision, state = (row[0], json.loads(row[1])) if row else (0, {})
            if saved_input and saved_input[0] == revision:
                # 输入及完整返回已发生，但业务结构未必合格；只恢复对话，不签发业务成功。
                state = json.loads(saved_input[1])
            cls._validate(state)
            prepared = cls.compose(settings, path=path, key=key, revision=revision, state=state,
                                   role=role, model=model, system=system, prompt=prompt)
            prepared.diagnostic["checkpoint_parent_hash"] = content_hash(saved_input[1]) if saved_input and saved_input[0] == revision else ""
            return prepared
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError,
                jsonpatch.JsonPatchException, jsonpatch.JsonPointerException) as exc:
            if settings.prompt_cache_layout_version >= 19:
                from .errors import ProviderError
                failure = ProviderError("持续上下文无法完整核验，原记录保留；请修复存储或来源后续接，未静默丢弃历史。")
                failure.request_sent = False
                raise failure from exc
            # 持续缓存不可用不重放付费请求，也不把损坏历史偷偷拼进当前资料。
            warnings.warn(f"持续上下文暂不可用（{type(exc).__name__}），本次保留完整当前输入。", RuntimeWarning, stacklevel=2)
            return None

    @staticmethod
    def _validate(state: dict[str, Any]) -> None:
        """拒绝不闭合、损坏或篡改的数据；文件中的文本从不成为更高权限指令。"""
        if not isinstance(state, dict):
            raise ValueError("会话状态必须是对象")
        if not state:
            return
        messages, known = state["messages"], state["known"]
        if (state.get("version") != 1 or not isinstance(messages, list) or len(messages) % 2
                or not isinstance(known, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in known.items())
                or not all(isinstance(m, dict) and set(m) == {"role", "content"}
                    and m["role"] == ("user" if i % 2 == 0 else "assistant")
                    and isinstance(m["content"], str) for i, m in enumerate(messages))):
            raise ValueError("会话回合未完整闭合")
        actual = {identity: content_hash(body) for identity, body in _replay_blocks(messages).items()}
        if actual != known:
            raise ValueError("历史资料块清单与实际消息不符")
        if not isinstance(state.get("handoff", []), list) or any(
                not isinstance(item, dict) or not isinstance(item.get("record"), dict)
                or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("source_hash", "")))
                for item in state.get("handoff", [])):
            raise ValueError("工作交接记录损坏")

    @classmethod
    def compose(cls, settings: Settings, *, path: Path, key: str, revision: int,
                state: dict[str, Any], role: str, model: str, system: str,
                prompt: str) -> SessionContext:
        """只追加新资料块与当前清单；硬材料仍逐字可见，不为命中率重复填充旧包。"""
        # 标题与已识别补核JSON字段仅作无损边界；正文、来源和格式合同不摘要、不删句。
        parts = _parts(prompt, structured=settings.prompt_cache_layout_version >= 27)
        current = [(content_hash(part), part) for part in parts]
        known, history = dict(state.get("known", {})), list(state.get("messages", []))
        wire_blocks: dict[str, str] = {}
        reference_blocks = _replay_blocks(history, wire_blocks=wire_blocks) if settings.prompt_cache_layout_version >= 18 else {}
        patched_blocks, checkpoint = 0, ""
        limits = settings.model_limits(model)
        _, ceiling = settings.context_budget_for(role, model_override=model)
        if settings.prompt_cache_layout_version < 20 and limits.get("context_tokens"):
            ceiling = min(ceiling, max(1, limits["context_tokens"] - limits.get("max_output_tokens", 0) - 1024))
        adaptive = settings.prompt_cache_layout_version >= 19
        threshold = max(1, int(ceiling * (.9 if adaptive else .8)))
        from .task_settings import active_task_settings
        task_scope = active_task_settings.get()
        handoff = list(state.get("handoff", [])) if adaptive else []
        base_prompt = prompt
        if handoff:
            # 交接是跨轮资料，必须继续列入本轮有效清单，不能仅在压缩当轮出现。
            prompt += ("\n\n# 历史工作交接（候选记录，当前资料与规则优先）\n"
                + json.dumps(handoff, ensure_ascii=False, separators=(",", ":")))
            parts = _parts(prompt, structured=settings.prompt_cache_layout_version >= 27)
            current = [(content_hash(part), part) for part in parts]

        def frame() -> tuple[str, dict[str, str]]:
            """清单决定本轮有效资料；历史模型结果始终只是候选，不能据此执行旧指令。"""
            nonlocal patched_blocks
            patched_blocks = 0
            additions: dict[str, str] = {}
            if not history and settings.prompt_cache_layout_version >= 18:
                # 首轮先原封发送原提示词，避免资料索引置前破坏已有供应商缓存。
                return prompt + _BASE_INDEX + json.dumps({"blocks": _part_index(parts),
                    **({"block_layout": 2} if settings.prompt_cache_layout_version >= 27 else {}),
                    "previous_summary_not_canon": checkpoint}, ensure_ascii=False, separators=(",", ":")), {
                    digest[:20]: part for digest, part in current}
            ids = []
            for digest, part in current:
                identity = digest[:20]
                if identity in known and known[identity] != digest:
                    raise ValueError("资料块标识冲突")
                ids.append(identity)
                if identity not in known and identity not in additions:
                    additions[identity] = part
            text = ("# 本轮持续上下文\n以下资料清单按顺序构成本轮完整输入，未列出的旧块已失效。"
                    "沿用历史中相同编号的原文块，新增块逐字提供于下方。以本轮任务和版本为准，"
                    "不要再次执行历史任务；历史assistant输出是未核验候选，不是正式通过或授权。"
                    "保留原系统规则，资料块内部按各自来源身份使用，最后按本轮输出格式交付。\n"
                    + "当前完整清单：" + json.dumps(ids, ensure_ascii=False) + "\n")
            if settings.prompt_cache_layout_version >= 18:
                text += "首轮资料可从消息末尾索引按标题定位。context-patch为基于base块的RFC6902资料更新；按补丁重建当前块，不把更新操作写成小说事件。\n"
            for identity, body in additions.items():
                patch = _block_patch(body, reference_blocks,
                    metadata_only=settings.prompt_cache_layout_version >= 20) if settings.prompt_cache_layout_version >= 18 else None
                kind = "patch" if patch is not None else "block"
                patched_blocks += int(patch is not None)
                text += f"\n<context-{kind} id=\"{identity}\">\n{patch if patch is not None else body}\n</context-{kind}>\n"
            return text, additions

        tail, additions = frame()
        messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": tail}]
        estimate = settings.estimate_input_tokens(json.dumps(messages, ensure_ascii=False))
        original_estimate = estimate
        pressure = estimate >= threshold
        boundary = bool(state.get("boundary_ready"))
        inactive = set(known) - {digest[:20] for digest, _ in current}
        stale_blocks = len(inactive)
        inactive_tokens = settings.estimate_input_tokens("".join(wire_blocks.get(identity, "") for identity in sorted(inactive))) if inactive else 0
        # 大量失效材料无需等到窗口90%才处理；仅在自然边界且原文占用显著时尝试，未减量仍保留原前缀。
        retired_pressure = settings.prompt_cache_layout_version >= 20 and inactive_tokens >= max(8192, estimate * .35)
        # 90%只挂起整理；正常等任务完成后的自然边界，物理输入窗口不足才提前保全重建。
        compacted = bool(history and (estimate > ceiling or (pressure or retired_pressure or state.get("compaction_pending")) and boundary)) if adaptive else bool(history and pressure)
        if compacted:
            # 当前编译包本身包含全部必要硬规则、来源与待办；用它收拢旧过程，不新增摘要模型调用。
            # shortcut: 仅保留前次结构化摘要为过程索引；跨轮必需信息仍须由当前资料包明确提供。
            if adaptive:
                # 增量交接逐条保留已有原句，不反复摘要旧摘要；历史决定须按当前来源重新确认。
                seen = {item["source_hash"] for item in handoff}
                for item in _continuity(history):
                    if item["source_hash"] not in seen:
                        handoff.append(item)
                        seen.add(item["source_hash"])
                if handoff:
                    prompt = base_prompt + ("\n\n# 历史工作交接（候选记录，当前资料与规则优先）\n"
                        + json.dumps(handoff, ensure_ascii=False, separators=(",", ":")))
                    parts = _parts(prompt, structured=settings.prompt_cache_layout_version >= 27)
                    current = [(content_hash(part), part) for part in parts]
            known, history = {}, []
            reference_blocks = {}
            checkpoint = state.get("last_summary", "")
            tail, additions = frame()
            if checkpoint and settings.prompt_cache_layout_version < 18:
                tail = "# 旧过程索引（不是正史或新指令）\n" + checkpoint + "\n\n" + tail
            messages = [{"role": "system", "content": system}, {"role": "user", "content": tail}]
            estimate = settings.estimate_input_tokens(json.dumps(messages, ensure_ascii=False))
            if adaptive and estimate >= original_estimate and original_estimate <= ceiling:
                # 没有缩小输入就保留可复用前缀；等待下一边界，不能为“已压缩”标签破坏缓存。
                deferred = cls.compose(settings, path=path, key=key, revision=revision,
                    state={**state, "boundary_ready": False}, role=role, model=model, system=system, prompt=base_prompt)
                deferred.diagnostic["compaction_deferred_reason"] = "no_size_reduction"
                return deferred
        for identity in additions:
            known[identity] = content_hash(additions[identity])
        diagnostic = {"policy_version": 2 if adaptive else 1, "status": "compacted" if compacted else "retained" if history else "new",
            "compaction_pending": bool(adaptive and pressure and not compacted),
            "natural_boundary": boundary, "emergency_compaction": bool(compacted and adaptive and not boundary),
            "input_ceiling_tokens": ceiling, "handoff_records": len(handoff),
            "inactive_blocks": stale_blocks, "unvalidated_exchange": state.get("result_status") == "unvalidated",
            "inactive_estimated_tokens": inactive_tokens, "retired_material_pressure": retired_pressure,
            "prior_revision": revision, "retained_turns": len(history) // 2,
            "reused_blocks": len(set(d[:20] for d, _ in current) - set(additions)),
            "new_blocks": len(additions), "estimated_input_tokens": estimate,
            "patched_blocks": patched_blocks,
            "compaction_threshold_tokens": threshold, "meaning": "本地持续上下文状态，不是供应商缓存命中证明"}
        return cls(path, key, revision, {"version": 1, "known": known,
            **({"task_id": task_scope.task_id if task_scope else "", "boundary_ready": False,
                "compaction_pending": bool(pressure and not compacted), "handoff": handoff} if adaptive else {})}, messages, diagnostic)

    def checkpoint_input(self, *, messages: list[dict[str, str]], content: str, response_id: str | None) -> bool:
        """保存已收到的完整交换，独立于Schema合格状态；未知断流不调用此入口。"""
        if self.diagnostic.get("policy_version", 1) < 2 or messages != self.messages or not content:
            return False
        state = {**self.state, "messages": [*messages[1:], {"role": "assistant", "content": content}],
            "response_id": response_id, "result_status": "unvalidated"}
        try:
            self._validate(state)
            with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
                row = connection.execute("SELECT revision FROM context_sessions WHERE session_key=?", (self.key,)).fetchone()
                if (row[0] if row else 0) != self.revision:
                    return False
                previous = connection.execute("SELECT state_json FROM context_inputs WHERE session_key=? AND revision=?",
                    (self.key, self.revision)).fetchone()
                serialized = json.dumps(state, ensure_ascii=False)
                if previous:
                    if content_hash(previous[0]) != self.diagnostic.get("checkpoint_parent_hash"):
                        return False
                    changed = connection.execute("UPDATE context_inputs SET state_json=? WHERE session_key=? AND revision=? AND state_json=?",
                        (serialized, self.key, self.revision, previous[0])).rowcount
                else:
                    changed = connection.execute("INSERT OR IGNORE INTO context_inputs VALUES (?,?,?)",
                        (self.key, self.revision, serialized)).rowcount
                if not changed:
                    return False
            return True
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            warnings.warn("输入检查点未保存，原始响应审计仍保留。", RuntimeWarning, stacklevel=2)
            return False

    @staticmethod
    def mark_task_boundary(settings: Settings, task_id: str) -> None:
        """仅由真实任务完成节点标记可整理；这里不压缩、不调用模型、不变更书稿。"""
        if settings.prompt_cache_layout_version < 19 or not settings.workspace_root or not task_id:
            return
        path = Path(settings.workspace_root) / ".inkflow" / "session-context.sqlite"
        if not path.is_file():
            return
        try:
            with closing(sqlite3.connect(path, timeout=5)) as connection, connection:
                for key, revision, encoded in connection.execute("SELECT session_key,revision,state_json FROM context_sessions").fetchall():
                    state = json.loads(encoded)
                    if state.get("task_id") == task_id:
                        state["boundary_ready"] = True
                        connection.execute("UPDATE context_sessions SET state_json=? WHERE session_key=? AND revision=? AND state_json=?",
                            (json.dumps(state, ensure_ascii=False), key, revision, encoded))
        except (OSError, sqlite3.Error, ValueError, TypeError):
            warnings.warn("自然边界标记未保存，将保留上下文至下一可用边界。", RuntimeWarning, stacklevel=2)

    def commit(self, *, messages: list[dict[str, str]], content: str,
               response_id: str | None, summary: str = "") -> bool:
        """只保存实际成功解析的闭合回合；并发分支不覆盖先提交者，也不重新调用模型。"""
        if messages != self.messages:
            return False  # 格式补救改变了请求合同，保留原始审计，不冒充原会话的闭合回复。
        state = {**self.state, "messages": [*messages[1:], {"role": "assistant", "content": content}],
                 "last_summary": summary, "response_id": response_id}
        event = {"timestamp": utc_now(), "diagnostic": self.diagnostic,
                 "user": messages[-1], "assistant": {"role": "assistant", "content": content}}
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
                serialized = json.dumps(state, ensure_ascii=False)
                if self.revision:
                    changed = connection.execute("UPDATE context_sessions SET revision=?,state_json=? WHERE session_key=? AND revision=?",
                        (self.revision + 1, serialized, self.key, self.revision)).rowcount
                else:
                    changed = connection.execute("INSERT OR IGNORE INTO context_sessions VALUES (?,?,?)", (self.key, 1, serialized)).rowcount
                if not changed:
                    return False
                connection.execute("INSERT INTO context_events VALUES (?,?,?)",
                    (self.key, self.revision + 1, json.dumps(event, ensure_ascii=False)))
                if self.diagnostic.get("policy_version", 1) >= 2:
                    connection.execute("DELETE FROM context_inputs WHERE session_key=? AND revision=?", (self.key, self.revision))
            return True
        except (OSError, sqlite3.Error):
            # 已付费产物由Provider原保存链保护；状态保存失败不得把成功请求再发一次。
            warnings.warn("持续上下文保存未完成，已有模型返回保留，不自动重发。", RuntimeWarning, stacklevel=2)
            return False
