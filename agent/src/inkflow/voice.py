from __future__ import annotations

import asyncio
import io
import hashlib
import importlib
import importlib.util
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
import uuid
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import Settings, save_user_settings


VoiceEventSink = Callable[[dict[str, Any]], Awaitable[None]]

SUPPORTED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"}
VOICE_SETTING_NAMES = (
    "voice_enabled",
    "voice_input_enabled",
    "voice_output_enabled",
    "voice_auto_read",
    "voice_auto_send",
    "voice_default_profile",
    "voice_speed",
    "voice_volume",
    "voice_pause_scale",
    "voice_input_device",
    "voice_output_device",
    "voice_compute_device",
    "voice_input_engine",
    "voice_dialogue_engine",
    "voice_text_engine",
    "voice_novel_engine",
    "voice_light_asr_model",
    "voice_light_tts_model",
    "voice_sample_rate",
    "voice_segment_chars",
    "voice_cache_limit_mb",
    "voice_debug",
    "voice_data_dir",
)
VOICE_JOB_CONFIG_SETTINGS = (
    "voice_novel_engine", "voice_text_engine", "voice_compute_device", "voice_debug",
    "voice_light_tts_model",
    "voice_speed", "voice_volume", "voice_pause_scale",
    "voice_sample_rate", "voice_segment_chars",
)

SHERPA_REQUIREMENTS = (
    "sherpa-onnx>=1.12,<2",
    "soundfile>=0.12,<1",
)
SHERPA_ASR_MODEL_ID = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
SHERPA_ASR_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2"
)

BUILTIN_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "profile_id": "narrator_female",
        "name": "女声旁白",
        "kind": "builtin",
        "gender": "female",
        "speaker": "Serena",
        "description": "温和、稳定，适合大多数正文旁白。",
        "instruction": "自然普通话，叙述清楚，情绪克制。",
    },
    {
        "profile_id": "female_bright",
        "name": "明快女声",
        "kind": "builtin",
        "gender": "female",
        "speaker": "Vivian",
        "description": "明亮年轻，适合活泼角色。",
        "instruction": "自然普通话，明快但不要夸张。",
    },
    {
        "profile_id": "female_warm",
        "name": "温柔女声",
        "kind": "builtin",
        "gender": "female",
        "speaker": "Serena",
        "description": "温暖舒缓，适合成熟或安静的女性角色。",
        "instruction": "自然普通话，温柔舒缓，吐字清晰。",
    },
    {
        "profile_id": "narrator_male",
        "name": "男声旁白",
        "kind": "builtin",
        "gender": "male",
        "speaker": "Uncle_Fu",
        "description": "沉稳低缓，适合悬疑或历史叙事。",
        "instruction": "自然普通话，沉稳克制，保持叙述感。",
    },
    {
        "profile_id": "male_calm",
        "name": "沉静男声",
        "kind": "builtin",
        "gender": "male",
        "speaker": "Uncle_Fu",
        "description": "沉静自然，适合成年男性角色。",
        "instruction": "自然普通话，语气平静，避免播音腔。",
    },
    {
        "profile_id": "male_firm",
        "name": "坚定男声",
        "kind": "builtin",
        "gender": "male",
        "speaker": "Uncle_Fu",
        "description": "更有力度，适合行动型角色。",
        "instruction": "自然普通话，坚定有力，但不要喊叫。",
    },
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _voice_segment_output_ready(segment: dict[str, Any]) -> bool:
    """Resume by each durable segment, not by a fragile completed-prefix count."""
    output = Path(str(segment.get("audio_path") or ""))
    return segment.get("status") == "completed" and output.is_file()


def _completed_voice_segment_count(segments: list[dict[str, Any]]) -> int:
    return sum(_voice_segment_output_ready(segment) for segment in segments)


def _voice_job_delete_scope_reason(expected_folder: Path, resolved_folder: Path, *, folder_link: bool,
                                   resolved_segments: Path | None, segments_link: bool) -> str:
    """只判断已解析路径的删除边界；目录联接也不能越过本任务目录，不在这里执行文件操作。"""
    if folder_link or resolved_folder != expected_folder:
        return "这条旧任务指向其他语音目录，为保护已有文件，本页不能直接删除。请先在语音设置核对保存位置。"
    if segments_link or resolved_segments is not None and resolved_segments != resolved_folder / "segments":
        return "音频文件夹指向本任务以外的位置，已保留文件。请先核对语音保存位置。"
    return ""


def _voice_root() -> Path:
    configured = Settings.from_env().voice_data_dir
    if configured:
        target = Path(configured).expanduser()
        if not target.is_absolute() or target.parent == target:
            raise ValueError("语音保存位置须为磁盘内的专用文件夹，不能使用整个磁盘。")
        return target
    local = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    if local:
        return Path(local) / "InkFlow" / "voice"
    return Path.home() / ".inkflow" / "voice"


def _project_key(project_root: str | Path) -> str:
    resolved = str(Path(project_root).resolve()).casefold().encode("utf-8")
    return hashlib.sha256(resolved).hexdigest()[:20]


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.is_file():
        return dict(fallback or {})
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(fallback or {})
    return value if isinstance(value, dict) else dict(fallback or {})


def _package_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _directory_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return total
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _friendly_pip_error(output: list[str], component: str) -> tuple[str, bool]:
    """Return a short user-facing install error and whether the failure is retryable."""

    joined = "\n".join(output)
    lowered = joined.casefold()
    if ("pip-unpack" in lowered or "no such file or directory" in lowered) and (
        ".whl" in lowered or "oserror" in lowered
    ):
        return (
            f"{component} 依赖安装时，Windows 临时目录中的下载文件被提前清理。"
            "墨流已保留已有组件并清理本次暂存目录；重新安装时会使用应用自己的稳定缓存。",
            True,
        )
    if "no space left" in lowered or "磁盘空间不足" in joined:
        return (f"{component} 安装失败：磁盘空间不足。请释放空间后重试。", True)
    if "ssl" in lowered or "connection" in lowered or "timed out" in lowered:
        return (f"{component} 下载连接中断。已经下载到稳定缓存的文件会在重试时复用。", True)
    detail = next((line for line in reversed(output) if "error" in line.casefold()), "依赖安装未完成。")
    return (f"{component} 安装失败：{detail[:320]}", True)


def _safe_extract_tar(archive_path: Path, destination: Path) -> None:
    # Keep extraction on the caller's lexical path.  Under the Microsoft
    # Store host, resolve() can rewrite LocalAppData to Packages/.../LocalCache;
    # the caller then continues through the original alias and cannot find the
    # files that were just extracted.  absolute() still normalizes traversal
    # segments without switching to the other filesystem view.
    safe_destination = destination.absolute()
    with tarfile.open(archive_path, "r:bz2") as archive:
        members = []
        for member in archive.getmembers():
            target = (safe_destination / member.name).absolute()
            if target != safe_destination and safe_destination not in target.parents:
                raise RuntimeError("语音模型压缩包包含不安全的文件路径，已停止解压。")
            if member.issym() or member.islnk():
                raise RuntimeError("语音模型压缩包包含链接文件，已停止解压。")
            members.append(member)
        archive.extractall(destination, members=members)


class VoiceRuntime:
    """本地普通话语音运行时。它是确定性服务，不是第五个 AI Agent。"""

    def __init__(self, data_root: Path | None = None) -> None:
        self.root = data_root or _voice_root()
        self.migrating = False
        self.profiles_dir = self.root / "profiles"
        self.projects_dir = self.root / "projects"
        self.jobs_dir = self.root / "jobs"
        self.short_cache_dir = self.root / "cache" / "short"
        component_root = Path(str(self.root) if data_root or Settings.from_env().voice_data_dir else os.getenv("INKFLOW_VOICE_COMPONENT_ROOT") or ("D:/墨流/voice" if os.name == "nt" and Path("D:/").exists() else str(self.root)))
        self.edge_root = component_root / "edge"
        self.edge_packages_dir = self.edge_root / "packages"
        self.edge_state_path = self.edge_root / "install.json"
        legacy_sherpa_root = self.root / "sherpa"
        self.sherpa_root = legacy_sherpa_root if legacy_sherpa_root.is_dir() and any(legacy_sherpa_root.iterdir()) else component_root / "sherpa"
        self.sherpa_packages_dir = self.sherpa_root / "packages"
        self.sherpa_asr_dir = self.sherpa_root / "asr" / SHERPA_ASR_MODEL_ID
        self.sherpa_state_path = self.sherpa_root / "install.json"
        self.sherpa_update_path = self.sherpa_root / "update-check.json"
        for folder in (
            self.profiles_dir,
            self.projects_dir,
            self.jobs_dir,
            self.short_cache_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._short_lock: asyncio.Lock | None = None
        self._long_lock: asyncio.Lock | None = None
        self._inference_lock: asyncio.Lock | None = None
        self._asr_lock: asyncio.Lock | None = None
        self._edge_install_lock: asyncio.Lock | None = None
        self._edge_install_task: asyncio.Task[dict[str, Any]] | None = None
        self._edge_update_task: asyncio.Task[dict[str, Any]] | None = None
        self._edge_voices: dict[str, str] = {}
        self._edge_voice_expiry = 0.0
        self._edge_service_status = "unchecked"
        self.edge_update_path = self.edge_root / "update-check.json"
        self._sherpa_install_lock: asyncio.Lock | None = None
        self._sherpa_install_task: asyncio.Task[dict[str, Any]] | None = None
        self._install_cancel_events = {
            "edge": threading.Event(),
            "asr": threading.Event(),
        }
        self._sherpa_asr_models: dict[str, Any] = {}
        self._asr_prepare_task: asyncio.Task[None] | None = None
        self._asr_prepare_error = ""
        self._deleting_jobs: set[str] = set()
        self._activate_optional_packages()
        self._mark_interrupted_jobs()

    def settings(self, workspace_root: str | Path | None = None) -> dict[str, Any]:
        current = Settings.from_env(workspace_root)
        return {name: getattr(current, name) for name in VOICE_SETTING_NAMES}

    def copy_storage(self, destination: str) -> "VoiceRuntime":
        old_root = self.root.resolve()
        target = Path(destination).expanduser()
        if not target.is_absolute() or target.parent == target:
            raise ValueError("请选择磁盘内的专用语音文件夹。")
        target = target.resolve()
        roots = [(old_root, target)]
        for name in ("edge", "sherpa"):
            source = getattr(self, f"{name}_root").resolve()
            if not source.is_relative_to(old_root):
                roots.append((source, target / name))
        if any(target == source or target.is_relative_to(source) or source.is_relative_to(target) for source, _ in roots):
            raise ValueError("新位置不能与当前语音目录互相包含，请选择另一处专用文件夹。")
        if target.exists():
            raise ValueError("新语音文件夹已经存在，未覆盖其中的内容。请另选保存位置。")
        files = []
        for source, folder in roots:
            if not source.exists():
                continue
            for item in source.rglob("*"):
                if item.is_symlink() or not item.resolve().is_relative_to(source):
                    raise ValueError("语音目录含外部链接，未迁移；请先检查这些链接。")
                if item.is_file():
                    files.append((item, folder / item.relative_to(source)))
        if shutil.disk_usage(target.parent).free < sum(item.stat().st_size for item, _ in files) + 64 * 1024 * 1024:
            raise ValueError("新磁盘空间不足，尚未更改语音保存位置。")
        staging = Path(tempfile.mkdtemp(prefix=".inkflow-voice-", dir=target.parent))
        def digest(item):
            with item.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").digest()
        for source, final in files:
            copied = staging / final.relative_to(target)
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, copied)
            if digest(source) != digest(copied):
                raise ValueError("复制内容核对未通过，仍使用原目录；新位置保留未完成副本供检查。")
        def relocate(value):
            if isinstance(value, dict): return {key: relocate(item) for key, item in value.items()}
            if isinstance(value, list): return [relocate(item) for item in value]
            if isinstance(value, str) and Path(value).is_absolute():
                for source, final in sorted(roots, key=lambda pair: len(str(pair[0])), reverse=True):
                    path = Path(value).resolve()
                    if path.is_relative_to(source): return str(final / path.relative_to(source))
            return value
        metadata_files = [*staging.joinpath("profiles").glob("*.json"), *staging.joinpath("jobs").glob("*/job.json"), *staging.joinpath("projects").glob("*/roles.json")]
        for metadata in metadata_files:
            value = json.loads(metadata.read_text(encoding="utf-8-sig"))
            _atomic_json(metadata, relocate(value))
        staging.replace(target)
        replacement = VoiceRuntime(data_root=target)
        for job in replacement.jobs_dir.glob("*/job.json"):
            replacement._write_playlist(_read_json(job))
        save_user_settings({"voice_data_dir": str(target)})
        return replacement

    def configure(self, updates: dict[str, Any], workspace_root: str | Path | None = None) -> dict[str, Any]:
        allowed = {name: updates[name] for name in VOICE_SETTING_NAMES if name in updates and name != "voice_data_dir"}
        if allowed:
            # Validate and persist the complete normalized value in one atomic write.
            save_user_settings(allowed)
        return self.settings(workspace_root)

    def status(self, workspace_root: str | Path | None = None) -> dict[str, Any]:
        """Report installed components; only the desktop can confirm a browser dictation connection."""
        settings = Settings.from_env(workspace_root)
        self._activate_optional_packages()
        packages = {
            "edge_tts": _package_available("edge_tts"),
            "torch": _package_available("torch"),
            "soundfile": _package_available("soundfile"),
            "sherpa_onnx": _package_available("sherpa_onnx"),
        }
        edge = self._edge_status(packages)
        asr = self._sherpa_status(packages)
        backend = settings.voice_dialogue_engine
        output_ready = backend == "edge" and edge["connection_ready"]
        return {
            "enabled": settings.voice_enabled,
            "ready_for_input": settings.voice_input_engine == "local" and bool(asr["asr_ready"]),
            "ready_for_output": output_ready,
            "packages": packages,
            "compute_device": settings.voice_compute_device,
            "data_root": str(self.root),
            "formal_agent": False,
            "mode": "configured",
            "backend": backend,
            "edge": edge,
            "asr": asr,
            "models_loaded": {
                "asr": bool(self._sherpa_asr_models),
            },
            "message": (
                "微软在线朗读组件已加载。" if output_ready else edge["compatibility_message"] if backend == "edge" else "对话朗读已关闭。"
            ),
        }

    def _activate_optional_packages(self) -> None:
        for package_dir in (self.edge_packages_dir, self.sherpa_packages_dir):
            package_path = str(package_dir.resolve())
            if package_dir.is_dir() and package_path not in sys.path:
                sys.path.insert(0, package_path)

    def _edge_status(self, packages: dict[str, bool] | None = None, *, include_storage: bool = True) -> dict[str, Any]:
        ready = bool((packages or {}).get("edge_tts", _package_available("edge_tts")))
        version, compatible = "", False
        if ready:
            try:
                version = str(importlib.import_module("edge_tts.version").__version__)
                client = importlib.import_module("edge_tts")
                compatible = callable(client.Communicate) and callable(client.list_voices) and re.fullmatch(r"7\.\d+\.\d+", version) is not None and tuple(map(int, version.split("."))) >= (7, 3, 1)
            except (ImportError, AttributeError, ValueError, OSError):
                pass
        state = _read_json(self.edge_state_path)
        update = _read_json(self.edge_update_path)
        latest = str(update.get("latest_version") or "")
        version_parts = lambda value: tuple(int(part) for part in value.split(".")) if re.fullmatch(r"\d+\.\d+\.\d+", value) else ()
        update_available = bool(version_parts(version) and version_parts(latest) > version_parts(version))
        supported_update = update_available and version_parts(latest)[0] == 7
        return {
            "installed": ready,
            "connection_ready": ready and compatible,
            "service_status": self._edge_service_status,
            "update_check_failed": bool(update.get("check_failed")),
            "version": version,
            "needs_update": ready and (not compatible or supported_update),
            "latest_version": latest,
            "update_available": update_available,
            "compatibility_message": "当前连接组件缺失、过旧或加载失败。请修复微软语音连接组件。" if not compatible else f"在线朗读连接组件有新版 {latest}，可以更新。" if supported_update else "连接组件有跨版本更新，请先检查墨流应用更新以确认兼容性。" if update_available else "",
            "packages_dir": str(self.edge_packages_dir),
            "storage_size_mb": round(_directory_size(self.edge_root) / 1024 / 1024, 1) if include_storage else 0,
            "has_local_files": self._component_has_local_files(self.edge_root),
            "installing": bool(self._edge_install_task and not self._edge_install_task.done()),
            "last_error": str(state.get("error") or ""),
            "install_status": str(state.get("status") or "idle"),
            "install_stage": str(state.get("stage") or ""),
            "install_progress": int(state.get("progress") or 0),
            "install_summary": str(state.get("summary") or ""),
            "downloaded_mb": float(state.get("downloaded_mb") or 0),
            "retryable": bool(state.get("retryable", False)),
        }

    def schedule_edge_update_check(self, emit: VoiceEventSink) -> None:
        """合并后台版本查询，新结果到达后刷新设置页。"""
        if self._edge_update_task is None or self._edge_update_task.done():
            async def check_and_notify():
                """仅在查询时间发生变化时发送事件，避免状态刷新循环。"""
                previous = _read_json(self.edge_update_path).get("checked_at")
                result = await self.check_component_updates()
                if result["edge"].get("checked_at") != previous:
                    await emit({"type": "voice.edge.versions.checked"})
                return result
            self._edge_update_task = asyncio.create_task(check_and_notify())

    async def check_component_updates(self, *, force: bool = False) -> dict[str, Any]:
        """Check only official package indexes; model weights stay on the known compatible release."""
        await asyncio.gather(
            self._check_package_update("edge-tts", self.edge_update_path, force=force),
            self._check_package_update("sherpa-onnx", self.sherpa_update_path, force=force),
        )
        if force and self._edge_status(include_storage=False)["connection_ready"]:
            # 手动检查仅拉取声线目录，验证实际可达性而不上传任何待朗读文字。
            if self._inference_lock is None:
                self._inference_lock = asyncio.Lock()
            async with self._inference_lock:
                self._edge_voice_expiry = 0.0
                try:
                    await self._online_voice({"gender": "female"}, "zh-CN-XiaoxiaoNeural")
                except Exception:
                    self._edge_service_status = "failed"
                else:
                    self._edge_service_status = "available"
        return {"edge": _read_json(self.edge_update_path), "asr": _read_json(self.sherpa_update_path)}

    async def _check_edge_update(self, *, after_failure: bool = False) -> None:
        """Refresh connection metadata after an Edge failure without retrying synthesis."""
        if not _package_available("edge_tts"):
            return
        await self._check_package_update("edge-tts", self.edge_update_path, after_failure=after_failure)

    async def _check_package_update(self, package: str, state_path: Path, *, force: bool = False, after_failure: bool = False) -> None:
        """Bound untrusted version metadata and retain the last known version when offline."""
        if package not in {"edge-tts", "sherpa-onnx"}:
            raise ValueError("不支持检查这个语音组件。")
        previous = _read_json(state_path)
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(str(previous.get("checked_at") or ""))).total_seconds()
        except ValueError:
            elapsed = float("inf")
        if not force and elapsed < (900 if after_failure or previous.get("check_failed") else 86400):
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=6, follow_redirects=False) as client:
                response = await client.get(f"https://pypi.org/pypi/{package}/json")
                response.raise_for_status()
                if len(response.content) > 1024 * 1024:
                    raise ValueError("版本资料超过读取上限")
                latest = str(response.json()["info"]["version"])
                if not re.fullmatch(r"\d+\.\d+\.\d+", latest):
                    raise ValueError("未取得正式版本号")
            _atomic_json(state_path, {"checked_at": _now(), "latest_version": latest, "source": f"https://pypi.org/project/{package}/"})
        except Exception:
            # A failed version lookup must not disable an otherwise working connector.
            _atomic_json(state_path, {**previous, "checked_at": _now(), "check_failed": True})

    def input_status(self, workspace_root: str | Path | None = None) -> dict[str, Any]:
        """Check the selected local recognizer without treating online dictation as verified."""
        self._activate_optional_packages()
        settings = Settings.from_env(workspace_root)
        asr = self._sherpa_status(include_storage=False)
        return {"ready_for_input": settings.voice_input_engine == "local" and bool(asr["asr_ready"]), "asr": asr}

    def prepare_input(self) -> None:
        """Warm the selected local recognizer without hiding initialization failures."""
        settings = Settings.from_env()
        if settings.voice_input_engine != "local" or not settings.voice_enabled or not settings.voice_input_enabled:
            return
        if self._asr_prepare_task is None or self._asr_prepare_task.done():
            async def warm():
                if self._asr_lock is None:
                    self._asr_lock = asyncio.Lock()
                async with self._asr_lock:
                    try:
                        await asyncio.to_thread(self._sherpa_asr_recognizer_sync, settings)
                        self._asr_prepare_error = ""
                    except Exception as exc:
                        self._asr_prepare_error = f"本地识别初始化失败（{type(exc).__name__}）：{str(exc)[:300]}"
            self._asr_prepare_task = asyncio.create_task(warm())

    def _sherpa_status(self, packages: dict[str, bool] | None = None, *, include_storage: bool = True) -> dict[str, Any]:
        """Report installed files separately from an observed recognizer initialization error."""
        package_ready = bool((packages or {}).get("sherpa_onnx", _package_available("sherpa_onnx")))
        asr_model = self._sherpa_asr_model_path()
        state = _read_json(self.sherpa_state_path)
        update = _read_json(self.sherpa_update_path)
        version = next((item.version for item in importlib.metadata.distributions(path=[str(self.sherpa_packages_dir)])
            if item.metadata.get("Name", "").lower().replace("_", "-") == "sherpa-onnx"), "")
        if not version and package_ready:
            try:
                version = importlib.metadata.version("sherpa-onnx")
            except importlib.metadata.PackageNotFoundError:
                pass
        latest = str(update.get("latest_version") or "")
        parts = lambda value: tuple(map(int, value.split("."))) if re.fullmatch(r"\d+\.\d+\.\d+", value) else ()
        return {
            "package_installed": package_ready,
            "asr_ready": package_ready and _package_available("soundfile") and asr_model is not None,
            "version": version,
            "latest_version": latest,
            "update_available": bool(parts(version) and parts(latest) > parts(version)),
            "update_check_failed": bool(update.get("check_failed")),
            "model_release": SHERPA_ASR_MODEL_ID,
            "asr_model": str(asr_model) if asr_model else "",
            "model_root": str(self.sherpa_asr_dir),
            "model_size_mb": round(_directory_size(self.sherpa_asr_dir) / 1024 / 1024, 1) if include_storage else 0,
            "storage_size_mb": round(_directory_size(self.sherpa_root) / 1024 / 1024, 1) if include_storage else 0,
            "has_local_files": self._component_has_local_files(self.sherpa_root),
            "estimated_download_mb": 230,
            "estimated_dependency_download_mb": 240,
            "installing": bool(self._sherpa_install_task and not self._sherpa_install_task.done()),
            "python_available": bool(self._python_command()),
            "last_error": self._asr_prepare_error or str(state.get("error") or ""),
            "initialization_error": self._asr_prepare_error,
            "install_status": str(state.get("status") or "idle"),
            "install_stage": str(state.get("stage") or ""),
            "install_progress": int(state.get("progress") or 0),
            "install_summary": str(state.get("summary") or ""),
            "downloaded_mb": float(state.get("downloaded_mb") or 0),
            "retryable": bool(state.get("retryable", False)),
        }

    @staticmethod
    def _component_has_local_files(root: Path) -> bool:
        """Count partial downloads; empty installer-created folders do not count."""
        if not root.is_dir():
            return False
        try:
            return any(item.is_file() for item in root.rglob("*"))
        except OSError:
            # Inaccessible local state should still leave the delete action enabled.
            return True

    def _python_command(self) -> list[str] | None:
        configured = os.getenv("INKFLOW_PYTHON")
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_file():
                return [str(candidate)]
        if not getattr(sys, "frozen", False) and Path(sys.executable).is_file():
            return [sys.executable]
        for name in ("python", "python3"):
            found = shutil.which(name)
            if found:
                return [found]
        launcher = shutil.which("py")
        if launcher:
            return [launcher, "-3.12"]
        return None

    async def _publish_install_progress(
        self,
        *,
        component: str,
        state_path: Path,
        emit: VoiceEventSink,
        status: str,
        stage: str,
        progress: int,
        summary: str,
        downloaded_mb: float = 0,
        error: str = "",
        retryable: bool = False,
        event_type: str | None = None,
    ) -> None:
        state = _read_json(state_path)
        state.update(
            {
                "status": status,
                "stage": stage,
                "progress": max(0, min(100, int(progress))),
                "summary": summary,
                "downloaded_mb": round(max(0.0, downloaded_mb), 1),
                "error": error[:500],
                "retryable": retryable,
                "updated_at": _now(),
            }
        )
        _atomic_json(state_path, state)
        await emit(
            {
                "type": event_type or f"voice.{component}.install.progress",
                "component": component,
                "status": status,
                "stage": stage,
                "progress": state["progress"],
                "summary": summary,
                "downloaded_mb": state["downloaded_mb"],
                "retryable": retryable,
                "error": error[:500],
            }
        )

    async def _install_python_packages(
        self,
        *,
        component: str,
        root: Path,
        staging: Path,
        state_path: Path,
        command: list[str],
        requirements: tuple[str, ...],
        estimated_mb: int,
        emit: VoiceEventSink,
    ) -> None:
        cache_dir = root / "pip-cache"
        install_temp = root / "install-temp"
        cache_dir.mkdir(parents=True, exist_ok=True)
        last_output: list[str] = []
        for attempt in range(2):
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(install_temp, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=False)
            install_temp.mkdir(parents=True, exist_ok=False)
            await self._publish_install_progress(
                component=component,
                state_path=state_path,
                emit=emit,
                status="installing",
                stage="dependencies" if attempt == 0 else "retrying",
                progress=4 if attempt == 0 else 10,
                summary="正在准备依赖" if attempt == 0 else "临时文件失效，正在从稳定缓存自动重试",
            )
            args = command + [
                "-m", "pip", "install", "--disable-pip-version-check", "--upgrade",
                "--cache-dir", str(cache_dir), "--target", str(staging),
                *requirements,
            ]
            env = {**os.environ, "TMP": str(install_temp), "TEMP": str(install_temp), "PYTHONUTF8": "1"}
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            output: list[str] = []
            progress = 10
            published_progress = -1
            assert process.stdout is not None
            while True:
                if self._install_cancel_events[component].is_set():
                    process.terminate()
                    await process.wait()
                    shutil.rmtree(staging, ignore_errors=True)
                    shutil.rmtree(install_temp, ignore_errors=True)
                    raise asyncio.CancelledError
                line = await process.stdout.readline()
                if not line:
                    break
                value = line.decode("utf-8", errors="replace").strip()
                if not value:
                    continue
                output.append(value)
                lowered = value.casefold()
                if "successfully installed" in lowered:
                    progress = 55
                elif "installing collected packages" in lowered:
                    progress = max(progress, 46)
                elif "downloading" in lowered or "using cached" in lowered:
                    progress = min(42, max(progress + 2, 18))
                elif "collecting" in lowered:
                    progress = min(17, progress + 1)
                if progress == published_progress and len(output) % 25:
                    continue
                published_progress = progress
                written_mb = (_directory_size(staging) + _directory_size(install_temp)) / 1024 / 1024
                await self._publish_install_progress(
                    component=component,
                    state_path=state_path,
                    emit=emit,
                    status="installing",
                    stage="dependencies",
                    progress=progress,
                    summary=f"正在安装依赖 · 已写入约 {written_mb:.0f}MB / 预计 {estimated_mb}MB",
                    downloaded_mb=written_mb,
                )
            return_code = await process.wait()
            shutil.rmtree(install_temp, ignore_errors=True)
            if return_code == 0:
                await self._publish_install_progress(
                    component=component,
                    state_path=state_path,
                    emit=emit,
                    status="installing",
                    stage="dependencies",
                    progress=56,
                    summary="依赖安装完成，正在整理文件",
                    downloaded_mb=_directory_size(staging) / 1024 / 1024,
                )
                return
            last_output = output
            message, retryable = _friendly_pip_error(output, component)
            if attempt == 0 and "临时目录" in message:
                continue
            await self._publish_install_progress(
                component=component,
                state_path=state_path,
                emit=emit,
                status="failed",
                stage="failed",
                progress=progress,
                summary="安装未完成，可从设置中重试",
                error=message,
                retryable=retryable,
                event_type=f"voice.{component}.install.failed",
            )
            shutil.rmtree(staging, ignore_errors=True)
            raise RuntimeError(message)
        message, retryable = _friendly_pip_error(last_output, component)
        await self._publish_install_progress(
            component=component,
            state_path=state_path,
            emit=emit,
            status="failed",
            stage="failed",
            progress=10,
            summary="自动重试仍未完成，可稍后再次安装",
            error=message,
            retryable=retryable,
            event_type=f"voice.{component}.install.failed",
        )
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(message)

    async def _prepare_models_with_progress(
        self,
        *,
        component: str,
        state_path: Path,
        model_dir: Path,
        estimated_mb: int,
        prepare: Callable[[], None],
        emit: VoiceEventSink,
    ) -> None:
        task = asyncio.create_task(asyncio.to_thread(prepare))
        while not task.done():
            size_mb = _directory_size(model_dir) / 1024 / 1024
            progress = 58 + min(39, int((size_mb / max(1, estimated_mb)) * 39))
            await self._publish_install_progress(
                component=component,
                state_path=state_path,
                emit=emit,
                status="installing",
                stage="models",
                progress=progress,
                summary=f"正在准备模型 · 本地已有 {size_mb:.0f}MB / 预计 {estimated_mb}MB",
                downloaded_mb=size_mb,
            )
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.8)
            except asyncio.TimeoutError:
                continue
        await task

    def _sherpa_asr_model_path(self) -> Path | None:
        if not (self.sherpa_asr_dir / "tokens.txt").is_file():
            return None
        candidates = (
            self.sherpa_asr_dir / "model.int8.onnx",
            self.sherpa_asr_dir / "model.onnx",
        )
        return next((path for path in candidates if path.is_file()), None)

    async def install_edge(self, confirmation: str, emit: VoiceEventSink) -> dict[str, Any]:
        if confirmation != "install_edge_online_voice":
            raise ValueError("安装 Edge 在线语音连接组件前需要确认；它不会下载语音模型。")
        if self._edge_install_task and not self._edge_install_task.done():
            return {**self.status(), "queued": False, "message": "Edge 在线语音组件已在后台安装。"}
        self._install_cancel_events["edge"].clear()
        self._edge_install_task = asyncio.create_task(self._background_install("edge", self._install_edge_impl, emit))
        await emit({"type": "voice.edge.install.queued", "component": "edge", "status": "installing", "stage": "queued", "progress": 0, "summary": "Edge 在线语音连接组件已进入后台安装。"})
        return {**self.status(), "queued": True, "message": "微软在线朗读连接组件正在后台更新。"}

    async def _stage_edge_connector(self, staging: Path) -> None:
        """从 PyPI 获取当前兼容 wheel，核对摘要、依赖和解包范围。"""
        import httpx

        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            metadata = await client.get("https://pypi.org/pypi/edge-tts/json")
            metadata.raise_for_status()
            if len(metadata.content) > 1024 * 1024:
                raise RuntimeError("连接组件版本资料超过大小限制。")
            release = metadata.json()
            info = release["info"]
            version = info["version"]
            if not re.fullmatch(r"7\.\d+\.\d+", version) or tuple(map(int, version.split("."))) < (7, 3, 1):
                raise RuntimeError("上游版本超出当前墨流支持范围，请更新墨流。")
            installed = self._edge_status(include_storage=False)["version"]
            if re.fullmatch(r"7\.\d+\.\d+", installed) and tuple(map(int, version.split("."))) < tuple(map(int, installed.split("."))):
                raise RuntimeError("上游版本早于本机组件，已保留本机版本。")
            # 热更新仅替换纯 Python 连接器。需要新依赖时由完整安装包提供。
            baseline = importlib.metadata.requires("edge-tts") or []
            if set(info.get("requires_dist") or []) != set(baseline):
                raise RuntimeError("新版连接组件的依赖已变化，请使用新版墨流安装包。")
            name = f"edge_tts-{version}-py3-none-any.whl"
            wheel = next((item for item in release["urls"] if item["filename"] == name and not item.get("yanked")), None)
            if not wheel or not re.fullmatch(r"https://files\.pythonhosted\.org/packages/[0-9a-f/]+/" + re.escape(name), wheel["url"]):
                raise RuntimeError("未取得兼容的 PyPI 连接组件。")
            # 服务响应和解包分别限量，避免错误响应造成大文件写入。
            content = bytearray()
            async with client.stream("GET", wheel["url"]) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2_000_000:
                        raise RuntimeError("连接组件下载超过大小限制。")
            if hashlib.sha256(content).hexdigest() != wheel["digests"]["sha256"]:
                raise RuntimeError("连接组件校验失败。")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > 128 or sum(item.file_size for item in members) > 2_000_000:
                raise RuntimeError("连接组件解包超过大小限制。")
            for item in members:
                if item.is_dir():
                    continue
                parts = item.filename.split("/")
                if (len(parts) < 2 or parts[0] not in {"edge_tts", f"edge_tts-{version}.dist-info"}
                        or not all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*[A-Za-z0-9_]|[A-Za-z0-9_]", part) for part in parts)):
                    raise RuntimeError("连接组件包含不受支持的文件路径。")
                destination = staging.joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as handle:
                    handle.write(archive.read(item))

    async def _install_edge_impl(self, emit: VoiceEventSink) -> dict[str, Any]:
        """暂存连接器，核对加载和声线目录，失败时恢复旧版本。"""
        if self._edge_install_lock is None:
            self._edge_install_lock = asyncio.Lock()
        async with self._edge_install_lock:
            self._activate_optional_packages()
            with tempfile.TemporaryDirectory(prefix="packages-staging-", dir=self.edge_root) as folder:
                staging = Path(folder)
                await self._publish_install_progress(
                    component="edge", state_path=self.edge_state_path, emit=emit,
                    status="installing", stage="dependencies", progress=20,
                    summary="正在从 PyPI 获取并校验微软语音连接组件",
                )
                await self._stage_edge_connector(staging)
                if self._inference_lock is None:
                    self._inference_lock = asyncio.Lock()
                async with self._inference_lock:
                    # 切换时保留旧目录和模块，加载失败立即恢复原连接器。
                    previous = self.edge_root / "packages-previous"
                    if previous.exists():
                        if previous.resolve().parent != self.edge_root.resolve() or previous.is_symlink():
                            raise RuntimeError("旧语音组件备份路径异常，无法安全更新。")
                        shutil.rmtree(previous)
                    self.edge_packages_dir.replace(previous)
                    old_modules = {name: module for name, module in sys.modules.items()
                                   if name == "edge_tts" or name.startswith("edge_tts.")}
                    try:
                        staging.replace(self.edge_packages_dir)
                        for name in old_modules:
                            sys.modules.pop(name, None)
                        importlib.invalidate_caches()
                        self._activate_optional_packages()
                        client = importlib.import_module("edge_tts")
                        if (not Path(client.__file__).resolve().is_relative_to(self.edge_packages_dir.resolve())
                                or not self._edge_status(include_storage=False)["connection_ready"]):
                            raise RuntimeError("新版连接组件无法加载。")
                        self._edge_voice_expiry = 0.0
                        await self._online_voice({"gender": "female"}, "zh-CN-XiaoxiaoNeural")
                    except BaseException:
                        if self.edge_packages_dir.exists():
                            self.edge_packages_dir.replace(staging)
                        previous.replace(self.edge_packages_dir)
                        for name in list(sys.modules):
                            if name == "edge_tts" or name.startswith("edge_tts."):
                                del sys.modules[name]
                        sys.modules.update(old_modules)
                        importlib.invalidate_caches()
                        self._edge_voice_expiry = 0.0
                        raise
                    self._edge_service_status = "available"
            await self._publish_install_progress(
                component="edge", state_path=self.edge_state_path, emit=emit,
                status="installed", stage="ready", progress=100,
                summary="微软语音连接组件已加载，声线目录可达。",
                event_type="voice.edge.ready",
            )
            return self.status()

    async def install_asr(self, confirmation: str, emit: VoiceEventSink) -> dict[str, Any]:
        """Install local Mandarin speech recognition as a resumable background job."""

        if confirmation != "install_local_asr":
            raise ValueError("安装本地语音识别前需要确认约 470MB 依赖与模型下载。")
        if self._sherpa_install_task and not self._sherpa_install_task.done():
            return {**self.status(), "queued": False, "message": "普通话识别已在后台安装。"}
        self._install_cancel_events["asr"].clear()
        self._sherpa_install_task = asyncio.create_task(
            self._background_install("asr", self._install_asr_impl, emit)
        )
        await emit({
            "type": "voice.asr.install.queued",
            "component": "asr",
            "status": "installing",
            "stage": "queued",
            "progress": 0,
            "summary": "普通话识别已加入后台安装；关闭设置不会停止任务。",
        })
        return {**self.status(), "queued": True, "message": "普通话识别已加入后台安装。"}

    async def _install_asr_impl(self, emit: VoiceEventSink) -> dict[str, Any]:
        """Install local dictation dependencies and models without changing the selected engine."""
        if self._sherpa_install_lock is None:
            self._sherpa_install_lock = asyncio.Lock()
        async with self._sherpa_install_lock:
            self._activate_optional_packages()
            if not (_package_available("sherpa_onnx") and _package_available("soundfile")):
                command = self._python_command()
                if not command:
                    raise RuntimeError("没有找到可用于安装普通话识别的 Python。")
                staging = self.sherpa_root / f"packages-staging-{uuid.uuid4().hex}"
                await self._install_python_packages(
                    component="asr",
                    root=self.sherpa_root,
                    staging=staging,
                    state_path=self.sherpa_state_path,
                    command=command,
                    requirements=SHERPA_REQUIREMENTS,
                    estimated_mb=240,
                    emit=emit,
                )
                if self.sherpa_packages_dir.exists():
                    previous = self.sherpa_root / "packages-previous"
                    if previous.exists():
                        shutil.rmtree(previous, ignore_errors=True)
                    self.sherpa_packages_dir.replace(previous)
                staging.replace(self.sherpa_packages_dir)
                self._activate_optional_packages()
            await self._publish_install_progress(
                component="asr",
                state_path=self.sherpa_state_path,
                emit=emit,
                status="installing",
                stage="models",
                progress=58,
                summary="依赖已就绪，正在准备普通话识别模型",
            )
            await self._prepare_models_with_progress(
                component="asr",
                state_path=self.sherpa_state_path,
                model_dir=self.sherpa_asr_dir,
                estimated_mb=230,
                prepare=lambda: self._download_voice_model(
                    SHERPA_ASR_MODEL_URL,
                    self.sherpa_asr_dir,
                    self.sherpa_root,
                    cancel_event=self._install_cancel_events["asr"],
                ),
                emit=emit,
            )
            if not self._sherpa_status()["asr_ready"]:
                raise RuntimeError("普通话识别组件不完整：依赖或模型文件仍不可用。")
            await self._publish_install_progress(
                component="asr",
                state_path=self.sherpa_state_path,
                emit=emit,
                status="installed",
                stage="ready",
                progress=100,
                summary="本地普通话识别已准备完成",
                event_type="voice.asr.ready",
            )
            return self.status()

    async def _background_install(
        self,
        component: str,
        operation: Callable[[VoiceEventSink], Awaitable[dict[str, Any]]],
        emit: VoiceEventSink,
    ) -> dict[str, Any]:
        """Run an optional voice installation independently from the settings RPC.

        The settings window only starts this task.  Progress is persisted in the
        component state file and emitted as events, so closing the window cannot
        cancel a long pip/model download.  Errors are converted into a visible,
        retryable status instead of an unobserved asyncio exception.
        """

        try:
            return await operation(emit)
        except asyncio.CancelledError:
            return self.status()
        except Exception as exc:
            state_path = {
                "edge": self.edge_state_path,
                "asr": self.sherpa_state_path,
            }[component]
            state = _read_json(state_path)
            if state.get("status") != "failed":
                await self._publish_install_progress(
                    component=component,
                    state_path=state_path,
                    emit=emit,
                    status="failed",
                    stage=str(state.get("stage") or "failed"),
                    progress=int(state.get("progress") or 0),
                    summary="后台安装未完成，可从设置中重试。",
                    error=str(exc)[:500],
                    retryable=True,
                    event_type=f"voice.{component}.install.failed",
                )
            result = self.status()
            result[f"{component}_setup"] = "failed"
            result[component]["last_error"] = str(exc)[:500]
            return result

    async def delete_voice_component(self, component: str, confirmation: str) -> dict[str, Any]:
        """Delete downloaded voice files without blocking the event loop."""
        component = str(component or "").strip().lower()
        if component != "sherpa":
            raise ValueError("这里只支持删除本地普通话识别组件。")
        if confirmation != "delete_sherpa_voice":
            raise ValueError("删除前需要确认；只会删除语音组件文件，不会删除录音和声音档案。")
        if (self._inference_lock is not None and self._inference_lock.locked()) or (self._asr_lock is not None and self._asr_lock.locked()):
            raise ValueError("当前有听写或朗读任务正在使用语音模型，请先结束相关任务后再删除。")
        task = self._sherpa_install_task
        self._install_cancel_events["asr"].set()
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return await asyncio.to_thread(self._delete_voice_component_sync, component)

    def _delete_voice_component_sync(self, component: str) -> dict[str, Any]:
        if component == "sherpa":
            self._sherpa_asr_models.clear()
        target_map = {
            # Component roots contain no recordings, voice profiles, generated
            # audio or job history. Removing the root also catches partial
            # .download archives, pip caches and failed staging folders.
            "sherpa": (self.sherpa_root,),
        }
        removed: list[str] = []
        errors: list[str] = []
        # The targets above are all constructed internally beneath the voice
        # root. Use lexical containment on Windows: Microsoft Store process
        # virtualization may resolve an existing child through LocalCache while
        # the parent still resolves to LocalAppData, creating a false escape.
        root = target_map[component][0].parent.absolute()
        for target in target_map[component]:
            resolved = target.absolute()
            if resolved == root or root not in resolved.parents:
                errors.append(f"删除失败：{target}")
                continue
            if not target.exists():
                continue
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                removed.append(str(target))
            except OSError as exc:
                errors.append(f"{target}: {exc}")
        if errors:
            raise RuntimeError("语音组件删除失败：" + "；".join(errors))
        return self.status()

    @staticmethod
    def _download_voice_model(
        url: str,
        destination: Path,
        safe_root: Path,
        required: tuple[str, ...] = ("tokens.txt",),
        cancel_event: threading.Event | None = None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Microsoft Store/AppContainer filesystem virtualization can resolve
        # LocalAppData into Packages/.../LocalCache even though it is still
        # the application's own voice directory.  Validate containment after
        # resolving both sides instead of rejecting every redirected path.
        safe_root = safe_root.absolute()
        lexical_destination = destination.absolute()
        # The destination is constructed internally from a fixed model id.
        # Lexical containment is therefore the authoritative boundary on
        # Windows, where an as-yet-uncreated child can resolve through the
        # Microsoft Store LocalCache redirect while its existing root does not.
        if lexical_destination != safe_root and safe_root not in lexical_destination.parents:
            raise RuntimeError("模型目标目录被重定向，无法安全更新，请检查本地语音目录。")
        if any(destination.glob("*.onnx")) and all((destination / name).is_file() for name in required):
            return
        archive_path = destination.parent / f".{destination.name}.download"
        extraction = destination.parent / f".{destination.name}.extract-{uuid.uuid4().hex}"
        try:
            with urllib.request.urlopen(url, timeout=60) as response, archive_path.open("wb") as output:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("安装已由用户取消，正在清理已下载文件。")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            extraction.mkdir(parents=True, exist_ok=False)
            _safe_extract_tar(archive_path, extraction)
            roots = [item for item in extraction.iterdir() if item.is_dir()]
            source = roots[0] if len(roots) == 1 else extraction
            if not any(source.glob("*.onnx")) or not all((source / name).is_file() for name in required):
                raise RuntimeError("下载的模型缺少必要文件，请重试下载。")
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            # A process launched from the Microsoft Store Codex host can see
            # one LocalAppData directory through both its logical path and the
            # package LocalCache path.  MoveFileEx treats those aliases as
            # different locations and can report WinError 3 even though the
            # extracted files are readable.  Copying keeps the commit within
            # one filesystem view and the extraction directory is removed
            # immediately afterwards.
            shutil.copytree(source, destination)
            shutil.rmtree(extraction, ignore_errors=True)
        finally:
            if archive_path.exists():
                archive_path.unlink()
            if extraction.exists():
                shutil.rmtree(extraction)

    def list_profiles(self) -> list[dict[str, Any]]:
        profiles = [dict(item) for item in BUILTIN_PROFILES]
        for path in sorted(self.profiles_dir.glob("*.json")):
            value = _read_json(path)
            if value.get("profile_id") and value.get("name"):
                profiles.append(value)
        return profiles

    def update_profile(self, profile_id: str, params: dict[str, Any]) -> dict[str, Any]:
        path = self.profiles_dir / f"{profile_id}.json"
        profile = _read_json(path)
        if not profile:
            raise ValueError("只允许调整本机创建的自定义声音。")
        for key in ("name", "description", "instruction", "gender"):
            if key in params:
                profile[key] = str(params[key]).strip()
        if "speed" in params:
            profile["speed"] = _clamp_float(params["speed"], 0.75, 1.35)
        if "volume" in params:
            profile["volume"] = _clamp_float(params["volume"], 0.25, 1.5)
        profile["updated_at"] = _now()
        _atomic_json(path, profile)
        return profile

    def get_role_map(self, project_root: str | Path) -> dict[str, Any]:
        path = self._role_map_path(project_root)
        return _read_json(
            path,
            {
                "project_key": _project_key(project_root),
                "narrator_profile_id": "narrator_female",
                "characters": {},
                "updated_at": "",
            },
        )

    def set_role_map(self, project_root: str | Path, params: dict[str, Any]) -> dict[str, Any]:
        valid_ids = {str(item["profile_id"]) for item in self.list_profiles()}
        narrator = str(params.get("narrator_profile_id") or "narrator_female")
        if narrator not in valid_ids:
            raise ValueError("旁白声音不存在，请重新选择。")
        raw_characters = params.get("characters") if isinstance(params.get("characters"), dict) else {}
        characters = {
            str(name).strip()[:40]: str(profile_id)
            for name, profile_id in raw_characters.items()
            if str(name).strip() and str(profile_id) in valid_ids
        }
        value = {
            "project_key": _project_key(project_root),
            "narrator_profile_id": narrator,
            "characters": characters,
            "updated_at": _now(),
        }
        _atomic_json(self._role_map_path(project_root), value)
        return value

    def analyze_roles(self, text: str) -> dict[str, Any]:
        counts: dict[str, int] = {}
        patterns = (
            re.compile(r"([\u4e00-\u9fffA-Za-z0-9·]{1,12})(?:低声|轻声|大声)?(?:说|问|道|喊|答)[：:]?[“\"]"),
            re.compile(r"[”\"](?:，|。|！|？)?([\u4e00-\u9fffA-Za-z0-9·]{1,12})(?:低声|轻声|大声)?(?:说|问|道|喊|答)"),
        )
        for pattern in patterns:
            for match in pattern.finditer(text):
                name = match.group(1)
                counts[name] = counts.get(name, 0) + 1
        roles = [
            {"name": name, "dialogue_count": count, "confidence": "high" if count >= 2 else "medium"}
            for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]
        return {
            "roles": roles,
            "ambiguous_policy": "无法可靠识别说话人时使用旁白声音，并在听读中心标记为待确认。",
        }

    async def transcribe(self, audio_path: str | Path, *, allow_disabled: bool = False) -> str:
        settings = Settings.from_env()
        if not allow_disabled and (not settings.voice_enabled or not settings.voice_input_enabled):
            raise ValueError("语音输入已关闭，请先在设置中开启。")
        source = Path(audio_path).resolve()
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            raise ValueError("音频文件不存在或格式不受支持。")
        if not self._sherpa_status(include_storage=False)["asr_ready"]:
            raise RuntimeError("本地普通话识别尚未准备好，请在设置的语音页后台安装识别组件。")
        if self._asr_lock is None:
            self._asr_lock = asyncio.Lock()
        async with self._asr_lock:
            try:
                return await asyncio.to_thread(self._transcribe_sherpa_sync, source, settings)
            except Exception as first_error:
                # A just-finished background install can leave stale import or
                # model objects in the long-lived engine. Refresh once and let
                # the service repair itself before asking the user to retry.
                self._sherpa_asr_models.clear()
                importlib.invalidate_caches()
                try:
                    return await asyncio.to_thread(self._transcribe_sherpa_sync, source, settings)
                except Exception as retry_error:
                    raise RuntimeError(
                        f"普通话识别自动恢复后仍未完成。首次原因：{first_error}；再次原因：{retry_error}"
                    ) from retry_error

    async def speak(self, text: str, profile_id: str | None = None, purpose: str = "dialogue") -> dict[str, Any]:
        settings = Settings.from_env()
        if not settings.voice_enabled or not settings.voice_output_enabled:
            raise ValueError("请先在设置中开启语音功能与朗读输出。")
        clean = str(text).strip()
        if not clean:
            raise ValueError("没有可朗读的文字。")
        if len(clean) > 4_000:
            raise ValueError("单条对话朗读最多 4000 字；更长内容请使用听读中心后台转换。")
        if purpose not in {"dialogue", "text"}:
            raise ValueError("不支持的朗读用途。")
        engine = settings.voice_dialogue_engine if purpose == "dialogue" else settings.voice_text_engine
        if engine == "off":
            raise ValueError("请先在语音设置中选择此用途的朗读组件。")
        self._activate_optional_packages()
        if self._short_lock is None:
            self._short_lock = asyncio.Lock()
        if self._inference_lock is None:
            self._inference_lock = asyncio.Lock()
        async with self._short_lock:
            output = self.short_cache_dir / f"speech-{uuid.uuid4().hex}{'.mp3' if engine == 'edge' else '.wav'}"
            profile = self._profile(profile_id or settings.voice_default_profile)
            async with self._inference_lock:
                await self._render_audio(clean, profile, output, settings, engine)
            self._prune_short_cache(settings.voice_cache_limit_mb)
        return {"audio_path": str(output), "profile": profile, "characters": len(clean), "engine": engine}

    def create_job(self, project_root: str | Path, params: dict[str, Any], emit: VoiceEventSink) -> dict[str, Any]:
        settings = Settings.from_env(project_root)
        if not settings.voice_enabled or not settings.voice_output_enabled:
            raise ValueError("请先在设置中开启语音功能与朗读输出。")
        text = str(params.get("text") or "").strip()
        if not text:
            raise ValueError("没有可转换的正文或草稿。")
        if len(text) > 2_000_000:
            raise ValueError("单次后台转换最多 200 万字，请按卷或章节分批处理。")
        project_key = _project_key(project_root)
        source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        role_map = self.get_role_map(project_root)
        engine = settings.voice_text_engine if str(params.get("source_type") or "text") == "text" else settings.voice_novel_engine
        if engine == "off":
            raise ValueError("请先在语音设置中选择此用途的朗读组件。")
        if engine == "edge" and not _package_available("edge_tts"):
            raise RuntimeError("Edge 在线朗读组件尚未随当前程序安装。")
        config_hash = self._job_voice_config_hash(role_map, settings, engine)
        matching = [
            value for path in self.jobs_dir.glob("voice-*/job.json")
            if (value := _read_json(path)).get("status") in {"queued", "running"}
            and value.get("project_key") == project_key
            and value.get("source_hash") == source_hash
            and value.get("voice_config_hash") == config_hash
        ]
        if matching:
            existing = max(matching, key=lambda value: (
                value.get("status") == "running", int(value.get("completed_segments") or 0),
            ))
            return {**self._public_job(existing), "reused_existing": True}
        # 先核对本次真正使用的声音。历史克隆不能在部分文字已提交后才报错。
        segments = self._split_segments(text, role_map, settings.voice_segment_chars)
        profile_ids = {str(segment.get("profile_id") or role_map["narrator_profile_id"]) for segment in segments}
        if any(self._profile(profile_id).get("kind") == "clone" for profile_id in profile_ids):
            raise ValueError("本次使用了历史克隆声线，请在听读中心为相关角色选择微软预设声音。")
        job_id = f"voice-{uuid.uuid4().hex}"
        folder = self.jobs_dir / job_id
        segments_dir = folder / "segments"
        segments_dir.mkdir(parents=True, exist_ok=False)
        source_path = folder / "source.txt"
        source_path.write_text(text, encoding="utf-8", newline="\n")
        manifest = {
            "job_id": job_id,
            "project_key": project_key,
            "project_root": str(Path(project_root).resolve()),
            "source_name": str(params.get("source_name") or "未命名文本")[:120],
            "source_type": str(params.get("source_type") or "text")[:40],
            "source_path": str(source_path),
            "source_hash": source_hash,
            "voice_config_hash": config_hash,
            "engine": engine,
            "status": "queued",
            "progress": 0,
            "completed_segments": 0,
            "total_segments": len(segments),
            "characters": len(text),
            "segments": segments,
            "role_map": role_map,
            "created_at": _now(),
            "updated_at": _now(),
            "error": "",
            "output_dir": str(segments_dir),
            "playlist_path": str(folder / "InkFlow-listen.m3u8"),
        }
        self._write_job(manifest)
        self._start_job(manifest, emit)
        return {**self._public_job(manifest), "reused_existing": False}

    def _job_voice_config_hash(self, role_map: dict[str, Any], settings: Settings, engine: str) -> str:
        characters = role_map.get("characters") if isinstance(role_map.get("characters"), dict) else {}
        profile_ids = {str(role_map.get("narrator_profile_id") or "narrator_female")}
        profile_ids.update(str(value) for value in characters.values())
        profiles = {str(profile["profile_id"]): profile for profile in self.list_profiles()}
        profile_fields = ("kind", "gender", "speaker", "reference_audio", "reference_text", "instruction", "speed", "volume")
        config = {
            "roles": {"narrator_profile_id": role_map.get("narrator_profile_id"), "characters": characters},
            "profiles": {
                profile_id: {field: profiles.get(profile_id, {}).get(field) for field in profile_fields}
                for profile_id in profile_ids
            },
            "settings": {name: getattr(settings, name) for name in VOICE_JOB_CONFIG_SETTINGS},
            "engine": engine,
        }
        encoded = json.dumps(config, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def list_jobs(self, project_root: str | Path) -> list[dict[str, Any]]:
        """List persisted audio work without touching the novel database."""
        key = _project_key(project_root)
        jobs: list[dict[str, Any]] = []
        for path in self.jobs_dir.glob("voice-*/job.json"):
            value = _read_json(path)
            if value.get("project_key") == key:
                jobs.append(self._public_job(value))
        return sorted(jobs, key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def background_status(self, project_root: str | Path | None = None) -> dict[str, Any]:
        """Read lightweight job states; progress polling never scans models or imports inference libraries."""
        components = []
        for component, name, state_path, task in (
            ("asr", "本地语音识别", self.sherpa_state_path, self._sherpa_install_task),
            ("edge", "在线朗读组件", self.edge_state_path, self._edge_install_task),
        ):
            state = _read_json(state_path)
            running = bool(task and not task.done())
            status = "installing" if running else "interrupted" if state.get("status") == "installing" else state.get("status", "not_installed")
            components.append({"id": component, "name": name, "status": status,
                **{key: state.get(key) for key in ("stage", "progress", "summary", "error", "updated_at")}})
        # 删除权限来自当前路径和运行状态，轮询也同步它，不能继续显示已经失效的操作按钮。
        fields = ("job_id", "source_name", "engine", "status", "progress", "completed_segments", "total_segments", "updated_at", "error",
                  "audio_delete_available", "audio_delete_block_reason")
        return {"components": components, "jobs": [{key: job.get(key) for key in fields}
                for job in self.list_jobs(project_root)] if project_root else [], "data_root": str(self.root)}

    def delete_job(self, job_id: str, confirmation: str) -> dict[str, Any]:
        """经确认删除本任务的转换副本，保留独立回执；路径或运行状态不符时不触碰音频。"""
        if confirmation != "delete_voice_job":
            raise ValueError("删除前需要确认本任务的音频及转换副本。")
        value = self._read_job(job_id)
        task = self._jobs.get(job_id)
        if value.get("status") == "deleted":
            return self._public_job(value)
        if (task and not task.done()) or value.get("status") in {"queued", "running"}:
            raise ValueError("请先停止此语音任务，再删除音频。")
        # 与界面可用性共用门禁，仍在实际删除前重查，不能把目录联接误认为本地子目录。
        reason = self._job_delete_block_reason(job_id)
        if reason:
            raise ValueError(reason)
        folder = self._job_path(job_id).parent
        segments = folder / "segments"
        if segments.exists():
            shutil.rmtree(segments)
        for name in ("source.txt", "InkFlow-listen.m3u8"):
            path = folder / name
            if path.is_file():
                path.unlink()
        receipt = {key: value.get(key) for key in ("job_id", "project_key", "source_name", "source_type", "engine", "created_at", "total_segments")}
        receipt.update(status="deleted", deleted_at=_now(), updated_at=_now(), segments=[], completed_segments=0, progress=0)
        self._write_job(receipt)
        return self._public_job(receipt)

    def job_status(self, job_id: str) -> dict[str, Any]:
        return self._public_job(self._read_job(job_id))

    def pause_job(self, job_id: str) -> dict[str, Any]:
        """Pause future segments without racing a reserved audio deletion."""
        if job_id in self._deleting_jobs:
            raise ValueError("本任务的音频正在删除，请稍候。")
        value = self._read_job(job_id)
        if value.get("status") in {"queued", "running"}:
            value["status"] = "paused"
            value["updated_at"] = _now()
            self._write_job(value)
        return self._public_job(value)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        """Stop unfinished work while retaining completed audio and deletion receipts."""
        if job_id in self._deleting_jobs:
            raise ValueError("本任务的音频正在删除，请稍候。")
        value = self._read_job(job_id)
        if value.get("status") not in {"completed", "failed", "cancelled", "deleted"}:
            value["status"] = "cancelled"
            value["updated_at"] = _now()
            self._write_job(value)
            task = self._jobs.get(job_id)
            if task and not task.done():
                task.cancel()
        return self._public_job(value)

    def resume_job(self, job_id: str, emit: VoiceEventSink) -> dict[str, Any]:
        """Resume only an inactive retained job, never one whose files are being deleted."""
        if job_id in self._deleting_jobs:
            raise ValueError("本任务的音频正在删除，请稍候。")
        value = self._read_job(job_id)
        if value.get("status") not in {"paused", "interrupted", "failed"}:
            return self._public_job(value)
        if value.get("engine") != "edge":
            raise ValueError("旧本地朗读任务的已有音频可播放。请新建微软在线转换以生成其余内容。")
        value["status"] = "queued"
        value["error"] = ""
        value["updated_at"] = _now()
        self._write_job(value)
        self._start_job(value, emit)
        return self._public_job(value)

    def _start_job(self, manifest: dict[str, Any], emit: VoiceEventSink) -> None:
        job_id = str(manifest["job_id"])
        existing = self._jobs.get(job_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run_job(job_id, emit))
        self._jobs[job_id] = task
        task.add_done_callback(lambda _task, key=job_id: self._jobs.pop(key, None))

    async def _run_job(self, job_id: str, emit: VoiceEventSink) -> None:
        if self._long_lock is None:
            self._long_lock = asyncio.Lock()
        if self._inference_lock is None:
            self._inference_lock = asyncio.Lock()
        try:
            async with self._long_lock:
                value = self._read_job(job_id)
                if value.get("status") == "cancelled":
                    return
                value["status"] = "running"
                value["updated_at"] = _now()
                settings = Settings.from_env(value.get("project_root"))
                segments = value.get("segments") if isinstance(value.get("segments"), list) else []
                value["completed_segments"] = _completed_voice_segment_count(segments)
                value["progress"] = round(value["completed_segments"] * 100 / max(1, len(segments)))
                self._write_job(value)
                await emit({"type": "voice.job.progress", "job": self._public_job(value), "summary": "后台听读转换已开始"})
                if not value.get("engine"):
                    raise RuntimeError("旧听读任务没有记录朗读引擎；已完成片段保留，请按当前语音设置新建转换任务。")
                for index, segment in enumerate(segments):
                    latest = self._read_job(job_id)
                    if latest.get("status") in {"paused", "cancelled"}:
                        return
                    if _voice_segment_output_ready(segment):
                        continue
                    engine = str(value["engine"])
                    output = Path(str(value["output_dir"])) / f"{index + 1:05d}{'.mp3' if engine == 'edge' else '.wav'}"
                    profile = self._profile(str(segment.get("profile_id") or value["role_map"]["narrator_profile_id"]))
                    async with self._inference_lock:
                        await self._render_audio(str(segment["text"]), profile, output, settings, engine)
                    value = self._read_job(job_id)
                    segments = value.get("segments") if isinstance(value.get("segments"), list) else []
                    segment = segments[index]
                    segment["audio_path"] = str(output)
                    segment["status"] = "completed"
                    completed_segments = _completed_voice_segment_count(segments)
                    value["completed_segments"] = completed_segments
                    value["progress"] = round(completed_segments * 100 / max(1, len(segments)))
                    value["updated_at"] = _now()
                    self._write_job(value)
                    self._write_playlist(value)
                    if value.get("status") in {"paused", "cancelled"}:
                        return
                    await emit({"type": "voice.job.progress", "job": self._public_job(value), "summary": f"听读转换 {value['progress']}%"})
                value = self._read_job(job_id)
                segments = value.get("segments") if isinstance(value.get("segments"), list) else []
                value["completed_segments"] = _completed_voice_segment_count(segments)
                value["status"] = "completed"
                value["progress"] = 100
                value["updated_at"] = _now()
                self._write_job(value)
                await emit({"type": "voice.job.completed", "job": self._public_job(value), "summary": "后台听读转换已完成"})
        except asyncio.CancelledError:
            value = self._read_job(job_id)
            value["status"] = "cancelled"
            value["updated_at"] = _now()
            self._write_job(value)
        except Exception as exc:
            value = self._read_job(job_id)
            value["status"] = "failed"
            value["error"] = str(exc)[:500]
            value["failure_stage"] = "inference"
            value["suggested_action"] = "请根据错误说明处理后，从未完成片段继续。"
            value["updated_at"] = _now()
            self._write_job(value)
            await emit({"type": "voice.job.failed", "job": self._public_job(value), "summary": value["error"]})

    def _split_segments(self, text: str, role_map: dict[str, Any], max_chars: int = 360) -> list[dict[str, Any]]:
        # Keep closing quotes attached to their sentence, then pack adjacent
        # sentences that use the same voice up to the configured limit.  The
        # previous one-sentence-per-file strategy turned a 3,700-character
        # chapter into 205 model calls and even created standalone `”` clips.
        chunks: list[str] = []
        for paragraph in re.split(r"\n+", text):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            chunks.extend(
                item.strip()
                for item in re.findall(r".+?(?:[。！？；!?;]+[”’\"]?|$)", paragraph)
                if item.strip()
            )
        characters = role_map.get("characters") if isinstance(role_map.get("characters"), dict) else {}
        narrator = str(role_map.get("narrator_profile_id") or "narrator_female")
        result: list[dict[str, Any]] = []
        for chunk in chunks:
            if len(chunk) > max_chars:
                subchunks = [chunk[index:index + max_chars] for index in range(0, len(chunk), max_chars)]
            else:
                subchunks = [chunk]
            for piece in subchunks:
                speaker = self._speaker_for_segment(piece)
                profile_id = str(characters.get(speaker) or narrator)
                ambiguous = speaker is None and ("“" in piece or '"' in piece)
                previous = result[-1] if result else None
                if (
                    previous
                    and previous["profile_id"] == profile_id
                    and len(str(previous["text"])) + len(piece) <= max_chars
                ):
                    previous["text"] = str(previous["text"]) + piece
                    previous["ambiguous"] = bool(previous["ambiguous"] or ambiguous)
                    if speaker and previous["speaker"] != speaker:
                        previous["speaker"] = "多角色"
                    continue
                result.append({
                    "index": len(result) + 1,
                    "text": piece,
                    "speaker": speaker or "旁白",
                    "profile_id": profile_id,
                    "ambiguous": ambiguous,
                    "status": "pending",
                    "audio_path": "",
                })
        return result

    @staticmethod
    def _speaker_for_segment(text: str) -> str | None:
        match = re.search(r"([\u4e00-\u9fffA-Za-z0-9·]{1,12})(?:低声|轻声|大声)?(?:说|问|道|喊|答)[：:]?[“\"]", text)
        if match:
            return match.group(1)
        match = re.search(r"[”\"](?:，|。|！|？)?([\u4e00-\u9fffA-Za-z0-9·]{1,12})(?:低声|轻声|大声)?(?:说|问|道|喊|答)", text)
        return match.group(1) if match else None

    def _profile(self, profile_id: str) -> dict[str, Any]:
        for profile in self.list_profiles():
            if profile.get("profile_id") == profile_id:
                return profile
        raise ValueError(f"声音角色不存在：{profile_id}")

    def _sherpa_asr_recognizer_sync(self, settings: Settings):
        if not _package_available("sherpa_onnx") or not _package_available("soundfile"):
            raise RuntimeError("sherpa-onnx 或 soundfile 尚未安装。")
        model_path = self._sherpa_asr_model_path()
        if model_path is None:
            raise RuntimeError("普通话识别模型尚未下载，请在语音设置中安装本地识别组件。")
        import sherpa_onnx

        cache_key = f"sherpa:{model_path}:{settings.voice_compute_device}:{settings.voice_debug}"
        recognizer = self._sherpa_asr_models.get(cache_key)
        if recognizer is None:
            provider = "cuda" if settings.voice_compute_device == "cuda" else "cpu"
            if provider == "cuda":
                try:
                    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                        model=str(model_path),
                        tokens=str(self.sherpa_asr_dir / "tokens.txt"),
                        language="zh",
                        use_itn=True,
                        provider=provider,
                        num_threads=max(1, min(4, os.cpu_count() or 1)),
                        debug=settings.voice_debug,
                    )
                except Exception:
                    if settings.voice_compute_device == "cuda":
                        raise
                    provider = "cpu"
            if recognizer is None:
                recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(model_path),
                    tokens=str(self.sherpa_asr_dir / "tokens.txt"),
                    language="zh",
                    use_itn=True,
                    provider=provider,
                    num_threads=max(1, min(4, os.cpu_count() or 1)),
                    debug=settings.voice_debug,
                )
            self._sherpa_asr_models[cache_key] = recognizer
        return recognizer

    def _transcribe_sherpa_sync(self, source: Path, settings: Settings) -> str:
        recognizer = self._sherpa_asr_recognizer_sync(settings)
        import soundfile as sf
        audio, sample_rate = sf.read(str(source), dtype="float32", always_2d=True)
        samples = audio[:, 0]
        stream = recognizer.create_stream()
        stream.accept_waveform(int(sample_rate), samples)
        recognizer.decode_stream(stream)
        # 部分 sherpa-onnx SenseVoice 构建会把整段识别结果包成 JSON 字符串
        # （含 lang/emotion/event 等标记），不能直接当正文返回给输入框。
        raw = str(getattr(stream, "result", "") or "").strip()
        text = ""
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
                text = str(payload.get("text") or "") if isinstance(payload, dict) else ""
            except json.JSONDecodeError:
                text = raw
        else:
            text = raw
        # 去掉 <|yue|>、<|NEUTRAL|>、<|Speech|> 之类的内联标记，只留自然语言。
        text = re.sub(r"<\|[^|]*\|>", "", text).strip()
        # Silence is a normal result, not a model failure requiring a second decode.
        return text

    async def _online_voice(self, profile: dict[str, Any], preferred: str) -> str:
        """从微软当前目录选择可用的普通话声音，同类声线优先。"""
        import edge_tts

        clock = asyncio.get_running_loop().time
        if clock() >= self._edge_voice_expiry:
            # 使用上游客户端协商连接。目录请求只包含服务元数据。
            voices = await asyncio.wait_for(edge_tts.list_voices(), timeout=10)
            self._edge_voices = {
                item["ShortName"]: item.get("Gender", "").lower()
                for item in voices if item.get("Locale") == "zh-CN"
                and re.fullmatch(r"zh-CN-[A-Za-z0-9]+Neural", item.get("ShortName", ""))
            }
            self._edge_voice_expiry = clock() + 3600
        if preferred in self._edge_voices:
            return preferred
        fallback = next((name for name, gender in sorted(self._edge_voices.items())
                         if gender == profile.get("gender")), None)
        if fallback is None:
            raise RuntimeError("微软当前目录中没有可用的同类普通话声线，请切换预设声音或稍后重试。")
        return fallback

    async def _render_audio(self, text: str, profile: dict[str, Any], output: Path, settings: Settings, engine: str) -> None:
        """使用微软在线声线生成完整音频，再原子交付给播放器。"""
        if engine != "edge":
            raise ValueError("旧任务的已有音频可播放，请新建微软在线转换。")
        if profile.get("kind") == "clone":
            raise ValueError("请在听读中心把此角色的历史克隆声线改为微软预设声音。")
        if not self._edge_status(include_storage=False)["connection_ready"]:
            raise RuntimeError("在线朗读的连接组件使用旧协议或无法加载，请在设置 → 语音更新 Edge 组件后再试。")
        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError("Edge 在线朗读组件尚未安装。") from exc
        voice = {
            "narrator_female": "zh-CN-XiaoxiaoNeural",
            "female_bright": "zh-CN-XiaoyiNeural",
            "female_warm": "zh-CN-XiaohanNeural",
            "narrator_male": "zh-CN-YunyangNeural",
            "male_calm": "zh-CN-YunxiNeural",
            "male_firm": "zh-CN-YunjianNeural",
        }.get(str(profile.get("profile_id")), "zh-CN-YunxiNeural" if profile.get("gender") == "male" else "zh-CN-XiaoxiaoNeural")
        temporary = output.with_name(f".{output.stem}-{uuid.uuid4().hex}.mp3")
        try:
            voice = await self._online_voice(profile, voice)
            await edge_tts.Communicate(
                text, voice,
                rate=f"{round((settings.voice_speed - 1) * 100):+d}%",
                volume=f"{round((settings.voice_volume - 1) * 100):+d}%",
            ).save(str(temporary))
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("在线语音没有返回音频。")
            temporary.replace(output)
            self._edge_service_status = "available"
        except Exception as exc:
            self._edge_service_status = "failed"
            # 下次显式重试重读目录，避免继续使用已下架声线。
            self._edge_voice_expiry = 0.0
            status = getattr(exc, "status", None)
            if status == 403:
                await self._check_edge_update(after_failure=True)
                raise RuntimeError("微软在线语音拒绝连接（403）。请在语音设置查看组件更新，或稍后重试。") from exc
            if isinstance(status, int):
                raise RuntimeError(f"微软在线语音返回 HTTP {status}。请稍后重试。") from exc
            if isinstance(exc, (TimeoutError, OSError)):
                raise RuntimeError("连接微软在线语音超时或中断。请检查网络后重试。") from exc
            if isinstance(exc, edge_tts.exceptions.NoAudioReceived):
                raise RuntimeError("微软在线语音没有返回音频。重试时会刷新可用声线。") from exc
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def _prune_short_cache(self, limit_mb: int) -> None:
        limit_bytes = max(128, int(limit_mb)) * 1024 * 1024
        files = []
        total = 0
        for path in (*self.short_cache_dir.glob("*.wav"), *self.short_cache_dir.glob("*.mp3")):
            try:
                stat = path.stat()
                files.append((stat.st_mtime, path, stat.st_size))
                total += stat.st_size
            except OSError:
                continue
        for _mtime, path, size in sorted(files):
            if total <= limit_bytes:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                continue

    def _role_map_path(self, project_root: str | Path) -> Path:
        return self.projects_dir / _project_key(project_root) / "roles.json"

    def _job_path(self, job_id: str) -> Path:
        if not re.fullmatch(r"voice-[a-f0-9]{32}", job_id):
            raise ValueError("语音任务编号无效。")
        return self.jobs_dir / job_id / "job.json"

    def _read_job(self, job_id: str) -> dict[str, Any]:
        value = _read_json(self._job_path(job_id))
        if not value:
            raise ValueError("没有找到这个语音转换任务。")
        return value

    def _write_job(self, value: dict[str, Any]) -> None:
        _atomic_json(self._job_path(str(value["job_id"])), value)

    @staticmethod
    def _write_playlist(value: dict[str, Any]) -> None:
        paths = [
            str(segment.get("audio_path"))
            for segment in value.get("segments", [])
            if segment.get("audio_path") and Path(str(segment["audio_path"])).is_file()
        ]
        if not paths:
            return
        playlist = Path(str(value["playlist_path"]))
        playlist.write_text("#EXTM3U\n" + "\n".join(paths) + "\n", encoding="utf-8", newline="\n")

    def _job_delete_block_reason(self, job_id: str) -> str:
        """只读检查该任务的实际目录归属；旧 Windows 联接须显示保护原因，不能放宽后删除外部文件。"""
        folder = self._job_path(job_id).parent
        segments = folder / "segments"
        return _voice_job_delete_scope_reason(self.jobs_dir.resolve() / job_id, folder.resolve(),
            folder_link=folder.is_symlink(), resolved_segments=segments.resolve() if segments.exists() else None,
            segments_link=segments.is_symlink())

    def _public_job(self, value: dict[str, Any]) -> dict[str, Any]:
        """返回公开任务与当前删除可用性；保留删除回执及创建时间，不把历史进度当作仍在转换。"""
        visible = dict(value)
        visible["segments"] = [dict(segment) for segment in value.get("segments", [])]
        visible.pop("project_root", None)
        visible.pop("source_path", None)
        job_id = str(value.get("job_id") or "")
        task = self._jobs.get(job_id)
        if value.get("status") == "deleted":
            reason = "音频副本已删除，只保留历史记录。"
        elif value.get("status") in {"queued", "running"} or task and not task.done():
            reason = "请先停止转换，并等待当前片段收尾后再删除音频。"
        elif job_id in self._deleting_jobs:
            reason = "本任务的音频正在删除，请稍候。"
        else:
            try:
                reason = self._job_delete_block_reason(job_id)
            except (OSError, ValueError):
                reason = "暂时无法核对音频目录，已保留文件；请刷新或核对语音保存位置。"
        visible["audio_delete_available"] = not bool(reason)
        visible["audio_delete_block_reason"] = reason
        return visible

    def _mark_interrupted_jobs(self) -> None:
        for path in self.jobs_dir.glob("voice-*/job.json"):
            value = _read_json(path)
            if value.get("status") in {"queued", "running"}:
                value["status"] = "interrupted"
                value["updated_at"] = _now()
                _atomic_json(path, value)


def _clamp_float(value: Any, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = 1.0
    return max(minimum, min(maximum, result))
