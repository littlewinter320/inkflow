"""共享技能库：标准包读取、受限导入、启用版本与作者可见的目录管理。

Skill 是按需装入的工作说明；本模块不执行导入的 Python、Shell 或安装指令。
确定性步骤只能映射到 SKILL_TOOLS，小说写入仍由原引擎和章节工具授权。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

import yaml

from .config import user_settings_path
from .errors import ValidationGateError
from .utils import content_hash, utc_now


BUILTIN_SKILLS = Path(__file__).with_name("builtin_skills")
SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
ROLES = frozenset({"coordinator", "writer", "editor", "reviewer", "memory_keeper"})
MAX_SKILL_FILES, MAX_SKILL_BYTES, MAX_FILE_BYTES = 80, 2_000_000, 500_000
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".py", ".ps1", ".sh", ".js", ".ts"}
SKILL_TOOLS = {
    "skill.scope": "核对任务身份、角色、当前步骤和技能适用范围；不扩大小说权限。",
    "skill.resources": "按已冻结版本读取本步骤引用，核对资源哈希；不读取目录外文件。",
    "skill.output": "沿用当前请求的输出Schema及后续引擎校验；不替审核代签。",
    "context.inspect": "读取当前角色容量与实际装入材料估算，不调整容量或删硬来源。",
    "skills.validate": "校验候选技能的结构、触发、资源路径及登记工具，返回具体缺项。",
}
CHAPTER_TOOL_NAMES = frozenset({"status.read", "chapter.read", "source.search", "plan.preview",
    "chapter.write", "chapter.revise", "chapter.review_mode", "chapter.accept"})


def personal_skills_root() -> Path:
    """个人技能优先使用D盘专用目录，显式环境变量可指定已有技能库。"""
    configured = os.getenv("INKFLOW_SKILLS_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt" and Path("D:/").is_dir():
        return Path("D:/墨流/skills").resolve()
    return (user_settings_path().parent / "skills").resolve()


def _is_link(path: Path) -> bool:
    """同时拒绝符号链接与Windows目录联接，避免导入沿链接读取别处私有文件。"""
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _relative_path(value: str) -> str:
    """资源仅限包内相对路径，不接受盘符、上级目录、隐藏目录或反斜线。"""
    if not isinstance(value, str):
        raise ValidationGateError("技能资源路径须为文字。")
    path = PurePosixPath(value)
    if (not value or "\\" in value or ":" in value or path.is_absolute()
            or any(part in {".", ".."} or part.startswith(".") for part in path.parts)
            or any(part.endswith((".", " ")) or any(ord(character) < 32 or character in '<>"|?*' for character in part)
                or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
                for part in path.parts)
            or path.as_posix() != value or len(value) > 240):
        raise ValidationGateError("技能资源路径必须是包内普通相对路径。")
    return value


def _canonical(value: Any) -> str:
    """稳定序列化技能身份，文件顺序不改变内容版本。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@contextmanager
def skill_connection() -> Iterator[sqlite3.Connection]:
    """个人配置、冻结任务和使用回执共用本地旁库，不改已签名的任务设置v2。"""
    path = user_settings_path().parent / "skills.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=15) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS skill_preferences (
                identity TEXT PRIMARY KEY, enabled INTEGER NOT NULL, automatic INTEGER NOT NULL,
                approved_hash TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_skill_snapshots (
                task_id TEXT PRIMARY KEY, snapshot_hash TEXT NOT NULL, manifest_hash TEXT NOT NULL,
                manifest_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_skill_requests (
                task_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, selection_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS skill_usage (
                task_id TEXT NOT NULL, contract TEXT NOT NULL, role TEXT NOT NULL,
                binding_hash TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(task_id, contract, role, binding_hash));
            CREATE TABLE IF NOT EXISTS skill_workflow_plans (
                task_id TEXT NOT NULL, steps_hash TEXT NOT NULL, data_json TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(task_id,steps_hash));
            CREATE TABLE IF NOT EXISTS skill_library_settings (
                key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS skill_policy_approvals (
                identity TEXT PRIMARY KEY, policy_hash TEXT NOT NULL,
                policy_json TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_workflow_choices (
                task_id TEXT NOT NULL, action TEXT NOT NULL, data_json TEXT NOT NULL,
                PRIMARY KEY(task_id,action));
            CREATE TABLE IF NOT EXISTS skill_run_plans (
                run_id TEXT NOT NULL, task_id TEXT NOT NULL, plan_hash TEXT NOT NULL,
                PRIMARY KEY(run_id,task_id,plan_hash));
        """)
        yield connection


def _folder_files(root: Path) -> dict[str, bytes]:
    """有界读取单个技能包；导入预览与保存均用同一份经过路径核对的字节。"""
    if not root.is_dir() or _is_link(root):
        raise ValidationGateError("请选择包含SKILL.md的普通文件夹，不使用目录联接。")
    resolved, files, size = root.resolve(), {}, 0
    for parent, directories, names in os.walk(root, followlinks=False):
        for name in directories:
            child = Path(parent) / name
            if _is_link(child) or not child.resolve().is_relative_to(resolved):
                raise ValidationGateError("技能包含目录链接或越界目录，未读取。")
        for name in names:
            child = Path(parent) / name
            if _is_link(child) or not child.resolve().is_relative_to(resolved):
                raise ValidationGateError("技能包含文件链接或越界文件，未读取。")
            relative = _relative_path(child.relative_to(root).as_posix())
            if child.stat().st_size > MAX_FILE_BYTES:
                raise ValidationGateError("技能单个文件超过500KB，请先缩减或拆分素材。")
            data = child.read_bytes()
            size += len(data)
            files[relative] = data
            if len(files) > MAX_SKILL_FILES or size > MAX_SKILL_BYTES:
                raise ValidationGateError("单个技能最多80份文件、合计2MB；未导入过大的包。")
    return files


def read_import_files(source: str | Path) -> dict[str, bytes]:
    """接受标准技能目录、单个SKILL.md或ZIP；ZIP在内存校验后再写入，不直接解压。"""
    path = Path(source).expanduser()
    if not path.exists() or _is_link(path):
        raise ValidationGateError("技能来源不存在或是文件链接。")
    if path.is_dir():
        return _folder_files(path)
    if path.name == "SKILL.md":
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValidationGateError("SKILL.md超过500KB。")
        return {"SKILL.md": path.read_bytes()}
    if path.suffix.lower() != ".zip" or path.stat().st_size > MAX_SKILL_BYTES:
        raise ValidationGateError("请选择技能目录、SKILL.md或不超过2MB的ZIP。")
    try:
        with zipfile.ZipFile(path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) > MAX_SKILL_FILES or sum(item.file_size for item in members) > MAX_SKILL_BYTES:
                raise ValidationGateError("ZIP展开后的技能包超过文件数或容量限制。")
            names = [item.filename for item in members]
            # GitHub常见的单层目录包可以导入，但不猜测多技能仓库的入口。
            prefix = ""
            if "SKILL.md" not in names:
                candidates = [name for name in names if name.endswith("/SKILL.md")]
                if len(candidates) != 1:
                    raise ValidationGateError("ZIP须含唯一SKILL.md；多技能仓库请分别选择技能目录。")
                prefix = candidates[0][:-len("SKILL.md")]
            files: dict[str, bytes] = {}
            for item in members:
                _relative_path(item.filename)
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or item.flag_bits & 1 or item.file_size > MAX_FILE_BYTES:
                    raise ValidationGateError("ZIP含链接、加密内容或超大文件，未导入。")
                if prefix and not item.filename.startswith(prefix):
                    raise ValidationGateError("ZIP含技能目录以外文件，请只打包单个技能。")
                relative = _relative_path(item.filename[len(prefix):])
                if relative in files:
                    raise ValidationGateError("ZIP内有重复资源路径，未覆盖。")
                files[relative] = archive.read(item)
            return files
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ValidationGateError("技能ZIP无法完整读取。") from exc


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """按Agent Skills读取YAML元数据；禁用别名与自定义标签，避免递归或对象构造。"""
    parts = re.split(r"(?m)^---\s*$", text.lstrip("\ufeff"), maxsplit=2)
    if len(parts) != 3 or parts[0].strip() or len(parts[1]) > 16_000:
        raise ValidationGateError("SKILL.md需要开头的YAML元数据和后续工作说明。")
    try:
        if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken))
                for token in yaml.scan(parts[1])):
            raise ValidationGateError("技能元数据使用普通YAML值，不使用别名、锚点或自定义标签。")
        metadata = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise ValidationGateError("SKILL.md的YAML格式不完整。") from exc
    if not isinstance(metadata, dict):
        raise ValidationGateError("技能元数据必须是对象。")
    name, description = metadata.get("name"), metadata.get("description")
    if not isinstance(name, str) or len(name) > 64 or not SKILL_NAME.fullmatch(name):
        raise ValidationGateError("技能名须为1至64位小写字母、数字及单个连字符。")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024 or not parts[2].strip():
        raise ValidationGateError("技能需要1至1024字的说明，以及非空工作步骤。")
    compatibility = metadata.get("compatibility", "")
    if not isinstance(compatibility, str) or len(compatibility) > 500:
        raise ValidationGateError("compatibility须为不超过500字的兼容说明。")
    return metadata, parts[2].strip()


def parse_skill(files: dict[str, bytes], *, expected_name: str | None = None) -> dict[str, Any]:
    """统一校验内置、作者创建和外部包，产出可冻结的文本与资源清单。"""
    if "SKILL.md" not in files or len(files) > MAX_SKILL_FILES or sum(map(len, files.values())) > MAX_SKILL_BYTES:
        raise ValidationGateError("技能必须包含SKILL.md，并在文件数和容量限制内。")
    texts, resources = {}, {}
    if len({path.casefold() for path in files if isinstance(path, str)}) != len(files):
        raise ValidationGateError("技能资源路径不能仅靠大小写区分，避免Windows导入后覆盖。")
    for relative, raw in sorted(files.items()):
        _relative_path(relative)
        if len(raw) > MAX_FILE_BYTES:
            raise ValidationGateError("技能单个文件超过500KB。")
        if Path(relative).suffix.lower() in TEXT_SUFFIXES or relative.upper().startswith("LICENSE"):
            try:
                texts[relative] = raw.decode("utf-8-sig")
            except UnicodeError as exc:
                raise ValidationGateError("技能说明、脚本和配置须使用UTF-8编码。") from exc
        import hashlib
        resources[relative] = {"hash": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "text": relative in texts}
    metadata, body = _frontmatter(texts["SKILL.md"])
    name = metadata["name"]
    if expected_name is not None and name != expected_name:
        raise ValidationGateError("技能目录名与SKILL.md的name不一致，请一并修改。")
    warnings = []
    try:
        adapter = json.loads(texts["agents/inkflow.json"]) if "agents/inkflow.json" in texts else {}
    except json.JSONDecodeError as exc:
        raise ValidationGateError("agents/inkflow.json不是有效JSON。") from exc
    if not isinstance(adapter, dict) or type(adapter.get("schema_version", 1)) is not int or adapter.get("schema_version", 1) != 1:
        raise ValidationGateError("墨流技能适配清单版本不受支持。")
    try:
        _canonical(metadata)
        _canonical(adapter)
    except (TypeError, ValueError) as exc:
        raise ValidationGateError("技能元数据只使用普通文字、数字、布尔值和JSON兼容的列表/对象。") from exc
    for key in ("display_name", "when", "not_when"):
        if key in adapter and (not isinstance(adapter[key], str) or len(adapter[key]) > 2000):
            raise ValidationGateError("技能显示名和触发说明须为不超过2000字的文字。")
    kind = adapter.get("kind", "method")
    if not isinstance(kind, str) or kind not in {"method", "workflow", "hybrid"}:
        raise ValidationGateError("技能kind只能为method、workflow或hybrid。")
    workflow = None
    if adapter.get("workflow"):
        declaration = adapter["workflow"]
        if (not isinstance(declaration, dict) or set(declaration) != {"path"}
                or not isinstance(declaration["path"], str) or declaration["path"] not in texts or not declaration["path"].startswith("scripts/")
                or not declaration["path"].endswith(".json")):
            raise ValidationGateError("流程技能须引用scripts内的JSON工作流声明。")
        try:
            workflow = json.loads(texts[declaration["path"]])
        except json.JSONDecodeError as exc:
            raise ValidationGateError("技能工作流不是有效JSON。") from exc
        from .skill_workflows import validate_skill_workflow
        validate_skill_workflow(workflow)
        if kind == "method":
            raise ValidationGateError("包含流程声明的技能须标为workflow或hybrid。")
    elif kind in {"workflow", "hybrid"}:
        raise ValidationGateError("流程或混合技能需要实际的登记工作流。")
    roles = adapter.get("roles", sorted(ROLES))
    if not isinstance(roles, list) or not roles or any(not isinstance(role, str) or role not in ROLES for role in roles):
        raise ValidationGateError("技能roles只能选择墨流现有五个角色。")
    activation = adapter.get("activation", "manual")
    if metadata.get("disable-model-invocation") is True:
        activation = "manual"
    if not isinstance(activation, str) or activation not in {"manual", "automatic"}:
        raise ValidationGateError("activation只能为manual或automatic。")
    contracts = adapter.get("contracts", ["*"])
    if not isinstance(contracts, list) or not contracts or any(not isinstance(item, str) or len(item) > 100 for item in contracts):
        raise ValidationGateError("技能contracts须为当前步骤标识列表。")
    if activation == "automatic" and ("*" in contracts or not adapter.get("when") or not adapter.get("not_when")):
        raise ValidationGateError("被动技能须限定具体步骤，并明确when与not_when。")
    for key in ("keywords", "exclude_keywords", "inputs", "outputs", "tasks"):
        value = adapter.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) or len(item) > 2000 for item in value):
            raise ValidationGateError(f"技能{key}须为文字列表。")
    references = adapter.get("references", [])
    if not adapter:
        # 标准包未写墨流清单时，只推导入口明确链接的包内文字参考；不猜目录、不联网上找缺文件。
        linked = list(dict.fromkeys(target.split("#", 1)[0] for target in re.findall(r"\]\((references/[^)\s]+)\)", body)))
        references = [{"path": path} for path in linked if path in texts]
        missing = [path for path in linked if path not in texts]
        if missing:
            warnings.append("入口引用的文字资源未随包提供，使用前需补齐或适配：" + "、".join(missing))
    if not isinstance(references, list) or len(references) > 30:
        raise ValidationGateError("技能引用须为最多30项的列表。")
    for reference in references:
        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str) or reference.get("path") not in texts:
            raise ValidationGateError("技能引用的文本文件不存在。")
        if any(key in reference and (not isinstance(reference[key], list) or
                any(not isinstance(value, str) for value in reference[key])) for key in ("contracts", "roles", "keywords")):
            raise ValidationGateError("引用的步骤、角色与触发词须为文字列表。")
    steps = adapter.get("steps", [])
    if not isinstance(steps, list) or len(steps) > 30:
        raise ValidationGateError("登记步骤须为最多30项的列表。")
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("tool"), str) or step.get("tool") not in SKILL_TOOLS:
            raise ValidationGateError("自动脚本步骤只能调用墨流登记的技能工具。")
        script = step.get("script")
        if script is not None:
            if not isinstance(script, str) or script not in texts or not script.startswith("scripts/") or not script.endswith(".json"):
                raise ValidationGateError("自动步骤须引用scripts下的JSON工具调用，不执行外部代码。")
            try:
                declaration = json.loads(texts[script])
            except json.JSONDecodeError as exc:
                raise ValidationGateError("工具调用脚本须为JSON。") from exc
            if declaration != {"schema_version": 1, "tool": step["tool"], "arguments": {}}:
                raise ValidationGateError("工具脚本只绑定登记工具及当前任务输入，不接受shell或任意参数。")
    parameters = adapter.get("parameters", [])
    if not adapter and not parameters:
        # 标准外部提示模板的变量可填写，宿主特有控制字段仍需明确适配。
        inferred = {}
        for match in re.finditer(r"\$\{([a-z][a-z0-9_]{0,39})(?::([^}]*))?\}", body):
            inferred[match.group(1)] = {"name": match.group(1), "label": match.group(1),
                "required": match.group(2) is None, "default": match.group(2) or ""}
        parameters = list(inferred.values())
    if not isinstance(parameters, list) or len(parameters) > 12:
        raise ValidationGateError("技能填空参数最多12项。")
    seen = set()
    for parameter in parameters:
        if (not isinstance(parameter, dict) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", str(parameter.get("name", "")))
                or parameter["name"] in seen or not isinstance(parameter.get("label", ""), str)
                or type(parameter.get("required", False)) is not bool or not isinstance(parameter.get("default", ""), str)):
            raise ValidationGateError("技能填空参数需要唯一name、说明、明确required和文字default。")
        seen.add(parameter["name"])
    if not adapter:
        warnings.append("标准SKILL.md已适配为手动调用；可补agents/inkflow.json限定角色、触发和工具。")
    executable = [path for path in files if path.startswith("scripts/") and not path.endswith(".json")]
    if executable:
        warnings.append("包内程序保留供外部查看，墨流不执行：" + "、".join(executable))
    if metadata.get("allowed-tools"):
        warnings.append("allowed-tools仅作兼容说明，不授予工具权限；实际执行沿墨流登记与原任务授权。")
    if len(body.splitlines()) > 500:
        warnings.append("入口超过500行，建议把详细标准移到references，按步骤装入。")
    # 文字方法与实际控制配置分别计算身份，支持作者选择的两种更新策略。
    policy = {"adapter": {**adapter, "roles": roles, "contracts": contracts, "activation": activation,
        "parameters": parameters, "kind": kind, "references": references, "steps": steps}, "workflow": workflow,
        "scripts": {path: value["hash"] for path, value in resources.items() if path.startswith("scripts/")}}
    return {"name": name, "description": metadata["description"].strip(), "metadata": metadata,
        "body": body, "adapter": {**adapter, "roles": roles, "contracts": contracts, "activation": activation,
            "parameters": parameters, "kind": kind, "references": references, "steps": steps}, "workflow": workflow,
        "texts": texts, "resources": resources, "hash": content_hash(_canonical(resources)), "warnings": warnings,
        "policy": policy, "policy_hash": content_hash(_canonical(policy))}


class SkillLibrary:
    """同一服务供桌面、聊天、CLI与MCP使用；目录编辑只对后续任务生效。"""

    def __init__(self, project_root: str | Path | None = None):
        """本书库须是已打开小说的.inkflow/skills；个人管理可以无小说进行。"""
        self.project_root = Path(project_root).resolve() if project_root else None
        if self.project_root and not (self.project_root / ".inkflow" / "project.json").is_file():
            raise ValidationGateError("本书技能库需要已存在的墨流小说项目。")
        self.novel_id = (str(json.loads((self.project_root / ".inkflow" / "project.json").read_text(encoding="utf-8"))["project_id"])
            if self.project_root else None)
        self.roots = {"builtin": BUILTIN_SKILLS.resolve(), "personal": personal_skills_root()}
        if self.project_root:
            self.roots["book"] = self.project_root / ".inkflow" / "skills"

    def _root(self, scope: str, *, writable: bool = False) -> Path:
        """只定位三类已知库；内置包只读，作者应创建副本后修改。"""
        if scope not in self.roots or writable and scope == "builtin":
            raise ValidationGateError("请选择个人或本书技能库；内置技能可复制后修改。")
        root = self.roots[scope]
        if root.exists() and _is_link(root):
            raise ValidationGateError("技能库目录不能是链接或目录联接。")
        if scope == "book" and not root.resolve().is_relative_to(self.project_root / ".inkflow"):
            raise ValidationGateError("本书技能库路径越界。")
        return root

    def _path(self, identity: str) -> tuple[str, Path]:
        """稳定ID含库名，重名技能不会静默覆盖或自动抢占内置流程。"""
        scope, separator, name = identity.partition("/")
        if not separator or not SKILL_NAME.fullmatch(name) or len(name) > 64:
            raise ValidationGateError("技能标识需要库名/技能名。")
        root = self._root(scope)
        path = root / name
        if path.exists() and (_is_link(path) or not path.resolve().is_relative_to(root.resolve())):
            raise ValidationGateError("技能目录越界。")
        return scope, path

    def _preference_key(self, identity: str, *, control_scope: str = "library") -> str:
        """个人默认与本书覆盖分别保存，关闭一本书的版本不改其他作品。"""
        scope = identity.split("/", 1)[0]
        base = content_hash(str(self._root(scope).resolve())) + ":" + identity
        if control_scope == "book":
            if not self.novel_id:
                raise ValidationGateError("本书开关需要已打开的小说。")
            return base + ":book:" + self.novel_id
        if control_scope != "library":
            raise ValidationGateError("请选择个人默认或本书覆盖。")
        return base

    @staticmethod
    def update_policy() -> str:
        """作者可选方法文字自动更新或所有变化确认；默认保留流程变化确认。"""
        with skill_connection() as connection:
            row = connection.execute("SELECT value_json FROM skill_library_settings WHERE key='update_policy'").fetchone()
        return json.loads(row[0]) if row else "text_auto"

    @staticmethod
    def set_update_policy(value: str) -> dict[str, str]:
        """仅影响后续任务读取新版本，不改变任何运行中任务快照。"""
        if value not in {"text_auto", "confirm_all"}:
            raise ValidationGateError("更新方式请选择文字自动更新或所有改动确认。")
        with skill_connection() as connection:
            connection.execute("INSERT INTO skill_library_settings VALUES ('update_policy',?) ON CONFLICT(key) "
                "DO UPDATE SET value_json=excluded.value_json", (_canonical(value),))
        return {"update_policy": value}

    def _decorate(self, parsed: dict[str, Any], identity: str, path: Path) -> dict[str, Any]:
        """按作者更新策略核文字或控制变更；运行中任务使用自己的冻结版本。"""
        scope = identity.split("/", 1)[0]
        with skill_connection() as connection:
            default_key = self._preference_key(identity)
            default = connection.execute("SELECT * FROM skill_preferences WHERE identity=?", (default_key,)).fetchone()
            book_key = self._preference_key(identity, control_scope="book") if self.novel_id else None
            override = connection.execute("SELECT * FROM skill_preferences WHERE identity=?", (book_key,)).fetchone() if book_key else None
            row, effective_key = (override, book_key) if override else (default, default_key)
            approval = connection.execute("SELECT * FROM skill_policy_approvals WHERE identity=?", (effective_key,)).fetchone()
            default_approval = connection.execute("SELECT * FROM skill_policy_approvals WHERE identity=?", (default_key,)).fetchone()
        changed = bool(row and row["approved_hash"] != parsed["hash"])
        text_updated = bool(changed and self.update_policy() == "text_auto" and approval and approval["policy_hash"] == parsed["policy_hash"])
        enabled = bool(row["enabled"]) and (not changed or text_updated) if row else scope == "builtin"
        automatic = bool(row["automatic"]) if row else parsed["adapter"]["activation"] == "automatic"
        default_changed = bool(default and default["approved_hash"] != parsed["hash"])
        default_text_updated = bool(default_changed and self.update_policy() == "text_auto" and default_approval
            and default_approval["policy_hash"] == parsed["policy_hash"])
        default_enabled = bool(default["enabled"]) and (not default_changed or default_text_updated) if default else scope == "builtin"
        return {**parsed, "id": identity, "scope": scope, "directory": str(path),
            "display_name": parsed["adapter"].get("display_name", parsed["name"]),
            "enabled": enabled, "automatic": automatic, "changed_since_approval": changed and not text_updated,
            "text_updated": text_updated, "control_scope": "book" if override else "library", "inherits_default": bool(self.novel_id and not override),
            "library_enabled": default_enabled,
            "library_automatic": bool(default["automatic"]) if default else parsed["adapter"]["activation"] == "automatic",
            "previous_policy": json.loads(approval["policy_json"]) if changed and approval else None,
            "read_only": scope == "builtin"}

    def get(self, identity: str) -> dict[str, Any]:
        """读取一个当前包；执行与恢复用冻结旁库，不使用此接口重读现行文件。"""
        _, path = self._path(identity)
        return self._decorate(parse_skill(_folder_files(path), expected_name=path.name), identity, path)

    def list(self, *, full: bool = False) -> dict[str, Any]:
        """显示所有技能及损坏项，单个包有误不使整个设置页打不开。"""
        skills, errors = [], []
        for scope in self.roots:
            root = self._root(scope)
            if not root.is_dir():
                continue
            directories = sorted(root.iterdir(), key=lambda path: path.name)
            if len(directories) > 200:
                errors.append({"id": scope, "error": "单库超过200项，请先整理目录。"})
                continue
            for path in directories:
                if not path.is_dir() or path.name.startswith("."):
                    continue
                identity = f"{scope}/{path.name}"
                try:
                    item = self.get(identity)
                    skills.append(item if full else public_skill(item))
                except (OSError, ValueError, ValidationGateError) as exc:
                    errors.append({"id": identity, "error": str(exc)})
        # 同名版本只选一个默认，明确@库/名字仍可指定其他已启用版本。
        rank = {"builtin": 0, "personal": 1, "book": 2}
        defaults = {}
        for item in skills:
            if item["enabled"] and (item["name"] not in defaults or rank[item["scope"]] > rank[defaults[item["name"]]["scope"]]):
                defaults[item["name"]] = item
        for item in skills:
            preferred = defaults.get(item["name"])
            item["shadowed_by"] = preferred["id"] if preferred and preferred["id"] != item["id"] else None
        return {"skills": skills, "errors": errors, "directories": {key: str(value) for key, value in self.roots.items()},
            "tools": SKILL_TOOLS, "chapter_tools": sorted(CHAPTER_TOOL_NAMES), "update_policy": self.update_policy(),
            "has_book": bool(self.novel_id)}

    def preview(self, source: str) -> dict[str, Any]:
        """导入前只读预览：来源、兼容提醒、全部文件及精确版本。"""
        parsed = parse_skill(read_import_files(source))
        return public_skill(parsed, include_texts=True)

    def create(self, *, name: str, description: str, scope: str = "personal",
               files: dict[str, str] | None = None) -> dict[str, Any]:
        """创建未启用候选，不覆盖同名包；复杂包由skill-authoring产出并经同一校验。"""
        if not SKILL_NAME.fullmatch(name) or len(name) > 64:
            raise ValidationGateError("请填写小写字母、数字和连字符组成的技能名。")
        if files is None:
            files = skill_template(name, description)
        encoded = {key: value.encode("utf-8") for key, value in files.items() if isinstance(value, str)}
        if len(encoded) != len(files):
            raise ValidationGateError("创建技能的文件内容须为UTF-8文字。")
        parsed = parse_skill(encoded, expected_name=name)
        return self._save_new(parsed, encoded, scope)

    def _save_new(self, parsed: dict[str, Any], files: dict[str, bytes], scope: str) -> dict[str, Any]:
        """用独占新目录保存包；失败只清理本次刚写文件，保留任何既有技能。"""
        root = self._root(scope, writable=True)
        root.mkdir(parents=True, exist_ok=True)
        destination = root / parsed["name"]
        try:
            destination.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise ValidationGateError("同名技能已存在；请换名或打开现有目录编辑。") from exc
        written = []
        try:
            for relative, raw in files.items():
                target = destination / _relative_path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(raw)
                written.append(target)
            identity = f"{scope}/{parsed['name']}"
            self.configure(identity, enabled=False, automatic=False, expected_hash=parsed["hash"], control_scope="library")
            if self.novel_id:
                self.configure(identity, enabled=False, automatic=False, expected_hash=parsed["hash"], control_scope="book")
            return public_skill(self.get(identity), include_texts=True)
        except BaseException:
            for target in reversed(written):
                if target.resolve().is_relative_to(destination.resolve()):
                    target.unlink(missing_ok=True)
            # 仅删除本轮新建且仍为空的目录，不递归删除未知文件。
            for parent in sorted(destination.rglob("*"), key=lambda value: len(value.parts), reverse=True):
                if parent.is_dir() and not _is_link(parent):
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
            try:
                destination.rmdir()
            except OSError:
                pass
            raise

    def import_skill(self, *, source: str, scope: str, expected_hash: str) -> dict[str, Any]:
        """保存作者刚预览的同版外部包，导入后默认关闭且不执行脚本。"""
        files = read_import_files(source)
        parsed = parse_skill(files)
        if parsed["hash"] != expected_hash:
            raise ValidationGateError("来源在预览后已变化，请重新查看再导入。")
        return self._save_new(parsed, files, scope)

    def clone(self, identity: str, *, scope: str, expected_hash: str) -> dict[str, Any]:
        """内置或其他库的技能复制成未启用副本，不覆盖原包及原任务。"""
        _, path = self._path(identity)
        files = _folder_files(path)
        parsed = parse_skill(files, expected_name=path.name)
        if parsed["hash"] != expected_hash:
            raise ValidationGateError("技能在查看后已变化，请刷新再复制。")
        return self._save_new(parsed, files, scope)

    def configure(self, identity: str, *, enabled: bool, automatic: bool, expected_hash: str,
                  control_scope: str | None = None) -> dict[str, Any]:
        """启用只批准当前内容版本；被动触发还要求包内有具体步骤与排除条件。"""
        item = self.get(identity)
        if type(enabled) is not bool or type(automatic) is not bool or expected_hash != item["hash"]:
            raise ValidationGateError("技能已变化或开关不明确，请刷新当前版本。")
        if automatic and item["adapter"]["activation"] != "automatic":
            raise ValidationGateError("此技能仅支持主动调用；先补齐限定步骤和触发边界再开启被动触发。")
        key = self._preference_key(identity, control_scope=control_scope or ("book" if self.novel_id else "library"))
        with skill_connection() as connection:
            connection.execute("INSERT INTO skill_preferences VALUES (?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET "
                "enabled=excluded.enabled,automatic=excluded.automatic,approved_hash=excluded.approved_hash,updated_at=excluded.updated_at",
                (key, int(enabled), int(automatic), item["hash"], utc_now()))
            connection.execute("INSERT INTO skill_policy_approvals VALUES (?,?,?,?) ON CONFLICT(identity) DO UPDATE SET "
                "policy_hash=excluded.policy_hash,policy_json=excluded.policy_json,updated_at=excluded.updated_at",
                (key, item["policy_hash"], _canonical(item["policy"]), utc_now()))
        return public_skill(self.get(identity))

    def inherit(self, identity: str) -> dict[str, Any]:
        """删除本书开关覆盖后继承个人默认，保留技能文件、历史批准与运行快照。"""
        key = self._preference_key(identity, control_scope="book")
        with skill_connection() as connection:
            connection.execute("DELETE FROM skill_preferences WHERE identity=?", (key,))
            connection.execute("DELETE FROM skill_policy_approvals WHERE identity=?", (key,))
        return public_skill(self.get(identity))

    def directory(self, scope: str) -> str:
        """为作者准备可外部编辑的库目录；调用者用现有openPath打开。"""
        root = self._root(scope)
        root.mkdir(parents=True, exist_ok=True)
        return str(root)


def public_skill(item: dict[str, Any], *, include_texts: bool = False) -> dict[str, Any]:
    """界面清单仅含元数据；详情按需读全文，不把全部技能塞给每次模型调用。"""
    keys = ("id", "name", "display_name", "description", "scope", "directory", "hash", "enabled", "automatic",
        "changed_since_approval", "read_only", "metadata", "adapter", "resources", "warnings", "shadowed_by")
    keys += ("text_updated", "control_scope", "inherits_default", "previous_policy", "policy")
    keys += ("library_enabled", "library_automatic")
    result = {key: item[key] for key in keys if key in item}
    if include_texts:
        result["texts"] = item["texts"]
    return result


def skill_template(name: str, description: str) -> dict[str, str]:
    """简单技能只生成入口与适配清单，作者需要时再添加三类辅助目录。"""
    front = yaml.safe_dump({"name": name, "description": description}, allow_unicode=True, sort_keys=False).strip()
    template = json.loads((BUILTIN_SKILLS / "skill-authoring" / "assets" / "skill-template.json").read_text(encoding="utf-8"))
    body = f"---\n{front}\n---\n\n" + template["body"].replace("${name}", name)
    adapter = template["adapter"]
    adapter["display_name"] = name
    return {"SKILL.md": body, "agents/inkflow.json": json.dumps(adapter, ensure_ascii=False, indent=2) + "\n"}
