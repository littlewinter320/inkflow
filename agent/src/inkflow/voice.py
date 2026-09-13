from __future__ import annotations

import asyncio
import gc
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
import wave
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
    "voice_engine",
    "voice_asr_model",
    "voice_tts_model",
    "voice_clone_model",
    "voice_light_asr_model",
    "voice_light_tts_model",
    "voice_sample_rate",
    "voice_segment_chars",
    "voice_cache_limit_mb",
    "voice_debug",
)

QWEN_REQUIREMENTS = (
    "qwen-tts>=0.1,<1",
    "soundfile>=0.12,<1",
    "torch>=2.4,<3",
    "torchaudio>=2.4,<3",
)
MOSS_TTS_MODEL_ID = "MOSS-TTS-Nano-100M-ONNX"
MOSS_CODEC_MODEL_ID = "MOSS-Audio-Tokenizer-Nano-ONNX"
MOSS_TTS_REPO_ID = "OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX"
MOSS_CODEC_REPO_ID = "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX"
MOSS_REPOSITORY = "git+https://github.com/OpenMOSS/MOSS-TTS-Nano.git"
MOSS_REQUIREMENTS = (
    f"moss-tts-nano @ {MOSS_REPOSITORY}",
    "huggingface_hub>=0.23,<1",
    "onnxruntime-gpu>=1.20,<2",
    "soundfile>=0.12,<1",
)
LEGACY_VITS_MODEL_ID = "sherpa-onnx-vits-zh-ll"
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
        "moss_voice": "Lingyu",
        "description": "温和、稳定，适合大多数正文旁白。",
        "instruction": "自然普通话，叙述清楚，情绪克制。",
    },
    {
        "profile_id": "female_bright",
        "name": "明快女声",
        "kind": "builtin",
        "gender": "female",
        "speaker": "Vivian",
        "moss_voice": "Xiaoyu",
        "description": "明亮年轻，适合活泼角色。",
        "instruction": "自然普通话，明快但不要夸张。",
    },
    {
        "profile_id": "female_warm",
        "name": "温柔女声",
        "kind": "builtin",
        "gender": "female",
        "speaker": "Serena",
        "moss_voice": "Yuewen",
        "description": "温暖舒缓，适合成熟或安静的女性角色。",
        "instruction": "自然普通话，温柔舒缓，吐字清晰。",
    },
    {
        "profile_id": "narrator_male",
        "name": "男声旁白",
        "kind": "builtin",
        "gender": "male",
        "speaker": "Uncle_Fu",
        "moss_voice": "Weiguo",
        "description": "沉稳低缓，适合悬疑或历史叙事。",
        "instruction": "自然普通话，沉稳克制，保持叙述感。",
    },
    {
        "profile_id": "male_calm",
        "name": "沉静男声",
        "kind": "builtin",
        "gender": "male",
        "speaker": "Uncle_Fu",
        "moss_voice": "Zhiming",
        "description": "沉静自然，适合成年男性角色。",
        "instruction": "自然普通话，语气平静，避免播音腔。",
    },
    {
        "profile_id": "male_firm",
        "name": "坚定男声",
        "kind": "builtin",
        "gender": "male",
        "speaker": "Uncle_Fu",
        "moss_voice": "Junhao",
        "description": "更有力度，适合行动型角色。",
        "instruction": "自然普通话，坚定有力，但不要喊叫。",
    },
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mandarin_speech_units(text: str) -> list[tuple[str, float]]:
    """Split prose one sentence at a time and retain a pause hint.

    MOSS benefits from short, complete requests: the runtime stays loaded while
    each sentence can be decoded and committed independently. Unicode escapes
    keep this splitter reliable even when a Windows console uses another code page.
    """
    normalized = _clean_text_for_speech(text)
    if not normalized:
        return []
    pause_seconds = {
        "\u3002": 0.34, "\uff01": 0.36, "!": 0.36,
        "\uff1f": 0.38, "?": 0.38, "\uff1b": 0.24, ";": 0.24,
        "\uff0c": 0.16, ",": 0.16, "\u3001": 0.14,
        "\uff1a": 0.20, ":": 0.20, "\u2026": 0.42,
        "\u2014": 0.28, "-": 0.16,
    }
    terminal_marks = set(pause_seconds)
    units: list[tuple[str, float]] = []
    buffer: list[str] = []

    def flush(pause: float) -> None:
        phrase = "".join(buffer).strip()
        buffer.clear()
        if phrase and any(character.isalnum() or "\u4e00" <= character <= "\u9fff" for character in phrase):
            units.append((phrase, pause))

    for character in normalized:
        if character == "\n":
            flush(0.46)
            continue
        buffer.append(character)
        if character in terminal_marks:
            flush(pause_seconds[character])
        elif len(buffer) >= 96:
            flush(0.12)
    flush(0.0)
    return units

def _clean_text_for_speech(text: str) -> str:
    """把屏幕文本变成适合朗读的普通话文本，避免念出 Markdown 和链接。"""

    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"```[\s\S]*?```", " 代码内容已省略。 ", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"https?://\S+", " 链接 ", value)
    value = re.sub(r"(?m)^\s{0,3}(?:#{1,6}|>|[-*+] |\d+[.)] )\s*", "", value)
    value = re.sub(r"[*_~]{1,3}", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _voice_root() -> Path:
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


def _safe_extract_tar(archive_path: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive_path, "r:bz2") as archive:
        members = []
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise RuntimeError("语音模型压缩包包含不安全的文件路径，已停止解压。")
            if member.issym() or member.islnk():
                raise RuntimeError("语音模型压缩包包含链接文件，已停止解压。")
            members.append(member)
        archive.extractall(destination, members=members)


class VoiceRuntime:
    """本地普通话语音运行时。它是确定性服务，不是第五个 AI Agent。"""

    def __init__(self) -> None:
        self.root = _voice_root()
        self.profiles_dir = self.root / "profiles"
        self.projects_dir = self.root / "projects"
        self.jobs_dir = self.root / "jobs"
        self.short_cache_dir = self.root / "cache" / "short"
        self.qwen_root = self.root / "qwen"
        self.qwen_packages_dir = self.qwen_root / "packages"
        self.qwen_model_cache_dir = self.qwen_root / "models"
        self.qwen_state_path = self.qwen_root / "install.json"
        self.qwen_models_path = self.qwen_root / "models.json"
        # MOSS is the default local TTS. Keep the old sherpa path only for a
        # clear "input is unavailable" status; do not recreate its files.
        self.moss_root = self.root / "moss"
        self.moss_packages_dir = self.moss_root / "packages"
        self.moss_model_dir = self.moss_root / "models"
        self.moss_state_path = self.moss_root / "install.json"
        self.sherpa_root = self.root / "sherpa"
        self.sherpa_asr_dir = self.sherpa_root / "asr" / SHERPA_ASR_MODEL_ID
        self.kokoro_root = self.root / "kokoro"  # legacy path; never created or loaded
        self.migrations_dir = self.root / "migrations"
        for folder in (
            self.profiles_dir,
            self.projects_dir,
            self.jobs_dir,
            self.short_cache_dir,
            self.qwen_root,
            self.qwen_model_cache_dir,
            self.moss_root,
            self.moss_packages_dir,
            self.moss_model_dir,
            self.migrations_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._short_lock: asyncio.Lock | None = None
        self._long_lock: asyncio.Lock | None = None
        self._inference_lock: asyncio.Lock | None = None
        self._qwen_install_lock: asyncio.Lock | None = None
        self._qwen_install_task: asyncio.Task[dict[str, Any]] | None = None
        self._moss_install_lock: asyncio.Lock | None = None
        self._moss_install_task: asyncio.Task[dict[str, Any]] | None = None
        self._tts_models: dict[str, Any] = {}
        self._moss_tts_models: dict[str, Any] = {}
        self._sherpa_asr_models: dict[str, Any] = {}
        self._activate_optional_packages()
        self._migrate_legacy_voice_assets()
        self._mark_interrupted_jobs()

    def settings(self, workspace_root: str | Path | None = None) -> dict[str, Any]:
        current = Settings.from_env(workspace_root)
        return {name: getattr(current, name) for name in VOICE_SETTING_NAMES}

    def configure(self, updates: dict[str, Any], workspace_root: str | Path | None = None) -> dict[str, Any]:
        allowed = {name: updates[name] for name in VOICE_SETTING_NAMES if name in updates}
        if allowed:
            # Validate and persist the complete normalized value in one atomic write.
            save_user_settings(allowed)
            if {"voice_engine", "voice_compute_device", "voice_debug", "voice_tts_model", "voice_light_tts_model"} & set(allowed):
                self._clear_loaded_tts()
        return self.settings(workspace_root)

    def _clear_loaded_tts(self) -> None:
        self._tts_models.clear()
        self._moss_tts_models.clear()
        gc.collect()
        torch = sys.modules.get("torch")
        if torch is not None and getattr(torch, "cuda", None) is not None:
            try:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

    def _migrate_legacy_voice_assets(self) -> None:
        """Remove only the replaced, downloaded engines once after this upgrade.

        Profiles, reference audio, generated audio, jobs and the whole voice root are
        deliberately outside this list. A failed deletion stays visible in the marker
        and is retried on the next application start.
        """

        marker = self.migrations_dir / "moss-voice-upgrade.json"
        existing = _read_json(marker)
        if existing.get("completed"):
            return
        safe_root = self.root.resolve()
        targets = (
            self.sherpa_root,
            self.kokoro_root,
            self.qwen_model_cache_dir / "hub" / "models--Qwen--Qwen3-TTS-12Hz-0.6B-CustomVoice",
            self.qwen_model_cache_dir / "hub" / "models--Qwen--Qwen3-TTS-12Hz-0.6B-Base",
        )
        removed: list[str] = []
        errors: list[str] = []
        for target in targets:
            resolved = target.resolve()
            if not target.exists():
                continue
            if resolved != target.absolute() or resolved == safe_root or safe_root not in resolved.parents:
                errors.append(f"{target.name}: 路径被重定向，未清理应用目录以外的数据。")
                continue
            try:
                shutil.rmtree(resolved)
                removed.append(target.name)
            except OSError as exc:
                errors.append(f"{target.name}: {exc}")
        _atomic_json(
            marker,
            {
                "completed": not errors,
                "completed_at": _now() if not errors else "",
                "removed": list(dict.fromkeys([*existing.get("removed", []), *removed])),
                "errors": errors,
                "scope": "legacy sherpa/Kokoro directories and Qwen 0.6B model caches",
            },
        )

    def status(self, workspace_root: str | Path | None = None) -> dict[str, Any]:
        settings = Settings.from_env(workspace_root)
        self._activate_optional_packages()
        packages = {
            "moss_tts": self._moss_package_ready(),
            "qwen_tts": _package_available("qwen_tts"),
            "torch": _package_available("torch"),
            "soundfile": _package_available("soundfile"),
            # sherpa is intentionally not part of the current voice bundle.
            "sherpa_onnx": False,
        }
        moss = self._moss_status(packages)
        qwen = self._qwen_status(packages)
        asr = {
            "package_installed": False,
            "asr_ready": False,
            "model_root": str(self.sherpa_asr_dir),
            "model_size_mb": 0.0,
            "estimated_download_mb": 0,
            "message": "sherpa 已移除；当前版本只提供 MOSS 本地朗读。",
        }
        backend = self._selected_backend(settings, moss, qwen)
        return {
            "enabled": settings.voice_enabled,
            "ready_for_input": False,
            "ready_for_output": backend != "unavailable",
            "packages": packages,
            "compute_device": settings.voice_compute_device,
            "data_root": str(self.root),
            "formal_agent": False,
            "mode": "local_mandarin",
            "backend": backend,
            "moss": moss,
            "asr": asr,
            "qwen": qwen,
            "migration": _read_json(self.migrations_dir / "moss-voice-upgrade.json"),
            "models_loaded": {
                "asr": False,
                "tts": bool(self._tts_models or self._moss_tts_models),
            },
            "message": (
                "MOSS 本地朗读已就绪。"
                if backend == "moss"
                else "Qwen 高品质中文朗读已就绪。"
                if backend == "qwen"
                else "当前选择的朗读模型尚未就绪，请在设置中安装 MOSS 或 Qwen。"
            ),
        }

    def _activate_optional_packages(self) -> None:
        # MOSS and Qwen are isolated under the application voice directory.
        for package_dir in (self.moss_packages_dir, self.qwen_packages_dir):
            package_path = str(package_dir.resolve())
            if package_dir.is_dir() and package_path not in sys.path:
                sys.path.insert(0, package_path)

    def _sherpa_status(self, packages: dict[str, bool] | None = None) -> dict[str, Any]:
        package_ready = bool((packages or {}).get("sherpa_onnx", _package_available("sherpa_onnx")))
        asr_model = self._sherpa_asr_model_path()
        return {
            "package_installed": package_ready,
            "asr_ready": package_ready and _package_available("soundfile") and asr_model is not None,
            "asr_model": str(asr_model) if asr_model else "",
            "model_root": str(self.sherpa_asr_dir),
            "model_size_mb": round(_directory_size(self.sherpa_asr_dir) / 1024 / 1024, 1),
            "estimated_download_mb": 230,
        }

    def _moss_package_ready(self) -> bool:
        self._activate_optional_packages()
        # onnx_tts_runtime imports these modules directly; report unavailable
        # until the isolated MOSS environment is complete.
        return all(
            _package_available(name)
            for name in ("onnx_tts_runtime", "onnxruntime", "sentencepiece", "numpy", "soundfile", "torch", "torchaudio")
        )

    def _moss_manifest_path(self) -> Path | None:
        candidates = (
            self.moss_model_dir / MOSS_TTS_MODEL_ID / "browser_poc_manifest.json",
            self.moss_model_dir / "browser_poc_manifest.json",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return next(self.moss_model_dir.rglob("browser_poc_manifest.json"), None) if self.moss_model_dir.is_dir() else None

    def _moss_codec_meta_path(self) -> Path | None:
        candidates = (
            self.moss_model_dir / MOSS_CODEC_MODEL_ID / "codec_browser_onnx_meta.json",
            self.moss_model_dir / "codec_browser_onnx_meta.json",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return next(self.moss_model_dir.rglob("codec_browser_onnx_meta.json"), None) if self.moss_model_dir.is_dir() else None

    def _moss_tts_meta_path(self) -> Path | None:
        manifest_path = self._moss_manifest_path()
        if manifest_path is None:
            return None
        manifest = _read_json(manifest_path)
        model_files = manifest.get("model_files") if isinstance(manifest.get("model_files"), dict) else {}
        relative = str(model_files.get("tts_meta") or "tts_browser_onnx_meta.json")
        candidate = (manifest_path.parent / relative).resolve()
        return candidate if candidate.is_file() else None

    def _moss_models_ready(self) -> bool:
        return (
            self._moss_manifest_path() is not None
            and self._moss_tts_meta_path() is not None
            and self._moss_codec_meta_path() is not None
        )

    def _moss_status(self, packages: dict[str, bool] | None = None) -> dict[str, Any]:
        package_ready = bool((packages or {}).get("moss_tts", self._moss_package_ready()))
        state = _read_json(self.moss_state_path)
        models_ready = self._moss_models_ready()
        return {
            "package_installed": package_ready,
            "dependencies_ready": package_ready,
            "tts_ready": package_ready and models_ready,
            "models_ready": models_ready,
            "model_root": str(self.moss_model_dir),
            "model_size_mb": round(_directory_size(self.moss_model_dir) / 1024 / 1024, 1),
            "estimated_download_mb": 900,
            "estimated_dependency_download_mb": 1800,
            "estimated_model_download_mb": 900,
            "installing": bool(self._moss_install_task and not self._moss_install_task.done()),
            "python_available": bool(self._python_command()),
            "last_error": str(state.get("error") or ""),
            "source": "local_optional" if state.get("status") == "installed" else "none",
        }

    def _qwen_status(self, packages: dict[str, bool] | None = None) -> dict[str, Any]:
        self._activate_optional_packages()
        values = packages or {
            "qwen_tts": _package_available("qwen_tts"),
            "torch": _package_available("torch"),
            "soundfile": _package_available("soundfile"),
        }
        dependencies_ready = all(values.get(name, False) for name in ("qwen_tts", "torch", "soundfile"))
        state = _read_json(self.qwen_state_path)
        optional_ready = self.qwen_packages_dir.is_dir() and bool(state.get("status") == "installed")
        installed = dependencies_ready
        source = "optional" if optional_ready else "current_environment" if installed else "none"
        python_command = self._python_command()
        settings = Settings.from_env()
        return {
            "installed": installed,
            "dependencies_ready": dependencies_ready,
            "source": source,
            "installing": bool(self._qwen_install_task and not self._qwen_install_task.done()),
            "python_available": bool(python_command),
            "python": " ".join(python_command or []),
            "packages_dir": str(self.qwen_packages_dir),
            "model_cache_dir": str(self.qwen_model_cache_dir),
            "model_loaded": bool(self._tts_models),
            "models_ready": all(self._qwen_model_path(name) is not None for name in (settings.voice_tts_model, settings.voice_clone_model)),
            "package_size_mb": round(_directory_size(self.qwen_packages_dir) / 1024 / 1024, 1),
            "model_size_mb": round(_directory_size(self.qwen_model_cache_dir) / 1024 / 1024, 1),
            "estimated_dependency_download_mb": 7000,
            "estimated_model_download_mb": 9200,
            "last_error": str(state.get("error") or ""),
        }

    def _selected_backend(self, settings: Settings, moss: dict[str, Any], qwen: dict[str, Any]) -> str:
        if settings.voice_engine == "qwen":
            return "qwen" if qwen["installed"] and self._qwen_model_path(settings.voice_tts_model) else "unavailable"
        return "moss" if moss["tts_ready"] else "unavailable"

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

    def _sherpa_asr_model_path(self) -> Path | None:
        if not (self.sherpa_asr_dir / "tokens.txt").is_file():
            return None
        candidates = (
            self.sherpa_asr_dir / "model.int8.onnx",
            self.sherpa_asr_dir / "model.onnx",
        )
        return next((path for path in candidates if path.is_file()), None)

    async def install_qwen(self, confirmation: str, emit: VoiceEventSink) -> dict[str, Any]:
        if confirmation != "install_optional_qwen":
            raise ValueError("安装 Qwen 前需要确认会下载数 GB 依赖与模型，并可能占用显存。")
        if self._qwen_install_task and not self._qwen_install_task.done():
            return await self._qwen_install_task
        self._qwen_install_task = asyncio.create_task(self._install_qwen_impl(emit))
        try:
            return await self._qwen_install_task
        finally:
            self._qwen_install_task = None

    async def _install_qwen_impl(self, emit: VoiceEventSink) -> dict[str, Any]:
        if self._qwen_install_lock is None:
            self._qwen_install_lock = asyncio.Lock()
        async with self._qwen_install_lock:
            self._activate_optional_packages()
            existing = self._qwen_status()
            if existing.get("installed") and existing.get("models_ready"):
                return self.status()
            if existing.get("installed"):
                await emit({"type": "voice.qwen.install.progress", "stage": "models", "summary": "Qwen 依赖已存在，正在重新准备语音模型"})
                model_error = ""
                try:
                    await asyncio.to_thread(self._prepare_models_sync, Settings.from_env())
                except Exception as exc:
                    model_error = str(exc)[:500]
                result = self.status()
                result["qwen_setup"] = "installed_but_models_pending" if model_error else "ready"
                if model_error:
                    result["qwen"]["last_error"] = model_error
                return result
            command = self._python_command()
            if not command:
                raise RuntimeError("没有找到可用于安装 Qwen 的 Python 3.12/3.13。请安装 Python 后重试，或设置 INKFLOW_PYTHON 指向 python.exe。")
            staging = self.qwen_root / f"packages-staging-{uuid.uuid4().hex}"
            staging.mkdir(parents=True, exist_ok=False)
            _atomic_json(self.qwen_state_path, {"status": "installing", "started_at": _now(), "error": ""})
            await emit({"type": "voice.qwen.installing", "stage": "dependencies", "summary": "正在安装 Qwen 本地依赖，过程可能持续较长时间"})
            args = command + [
                "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", "--upgrade",
                "--target", str(staging), *QWEN_REQUIREMENTS,
            ]
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output: list[str] = []
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    output.append(text)
                    await emit({"type": "voice.qwen.install.progress", "stage": "dependencies", "summary": text[-240:]})
            return_code = await process.wait()
            if return_code != 0:
                message = next((line for line in reversed(output) if "error" in line.casefold()), "pip 安装失败，请检查网络、Python 版本和磁盘空间。")
                _atomic_json(self.qwen_state_path, {"status": "failed", "finished_at": _now(), "error": message[:500]})
                shutil.rmtree(staging, ignore_errors=True)
                raise RuntimeError(message[:500])
            if self.qwen_packages_dir.exists():
                previous = self.qwen_root / "packages-previous"
                if previous.exists():
                    shutil.rmtree(previous, ignore_errors=True)
                self.qwen_packages_dir.replace(previous)
            staging.replace(self.qwen_packages_dir)
            _atomic_json(
                self.qwen_state_path,
                {
                    "status": "installed",
                    "installed_at": _now(),
                    "python": " ".join(command),
                    "requirements": list(QWEN_REQUIREMENTS),
                    "error": "",
                },
            )
            self._activate_optional_packages()
            await emit({"type": "voice.qwen.install.progress", "stage": "models", "summary": "依赖安装完成，正在下载 Qwen 预设与克隆模型"})
            model_error = ""
            try:
                await asyncio.to_thread(self._prepare_models_sync, Settings.from_env())
            except Exception as exc:
                model_error = str(exc)[:500]
            result = self.status()
            result["qwen_setup"] = "installed_but_models_pending" if model_error else "ready"
            if model_error:
                result["qwen"]["last_error"] = model_error
                result["qwen"]["model_message"] = "Qwen 依赖已安装，但模型下载未完成；可稍后重试模型准备。"
                await emit({"type": "voice.qwen.models_failed", "summary": model_error})
            else:
                await emit({"type": "voice.qwen.ready", "summary": "Qwen 高品质语音已安装并完成适配"})
            return result

    async def install_moss(self, confirmation: str, emit: VoiceEventSink) -> dict[str, Any]:
        if confirmation != "install_moss_voice":
            raise ValueError("安装 MOSS 前需要确认约 900MB 模型和 1.8GB 依赖下载，实际占用会随环境变化。")
        if self._moss_install_task and not self._moss_install_task.done():
            return await self._moss_install_task
        self._moss_install_task = asyncio.create_task(self._install_moss_impl(emit))
        try:
            return await self._moss_install_task
        finally:
            self._moss_install_task = None

    async def _install_moss_impl(self, emit: VoiceEventSink) -> dict[str, Any]:
        if self._moss_install_lock is None:
            self._moss_install_lock = asyncio.Lock()
        async with self._moss_install_lock:
            self._activate_optional_packages()
            if not self._moss_package_ready():
                command = self._python_command()
                if not command:
                    raise RuntimeError("没有找到可用的 Python。请设置 INKFLOW_PYTHON 指向 python.exe。")
                staging = self.moss_root / f"packages-staging-{uuid.uuid4().hex}"
                staging.mkdir(parents=True, exist_ok=False)
                _atomic_json(self.moss_state_path, {"status": "installing", "started_at": _now(), "error": ""})
                await emit({"type": "voice.moss.installing", "stage": "dependencies", "summary": "正在安装 MOSS ONNX 运行库，完成后会单独准备模型。"})
                args = command + [
                    "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", "--upgrade",
                    "--target", str(staging), *MOSS_REQUIREMENTS,
                ]
                process = await asyncio.create_subprocess_exec(
                    *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                output: list[str] = []
                assert process.stdout is not None
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    value = line.decode("utf-8", errors="replace").strip()
                    if value:
                        output.append(value)
                        await emit({"type": "voice.moss.install.progress", "stage": "dependencies", "summary": value[-240:]})
                return_code = await process.wait()
                if return_code != 0:
                    message = next((line for line in reversed(output) if "error" in line.casefold()), "MOSS 依赖安装失败，请查看上方安装日志。")
                    _atomic_json(self.moss_state_path, {"status": "failed", "finished_at": _now(), "error": message[:500]})
                    shutil.rmtree(staging, ignore_errors=True)
                    raise RuntimeError(message[:500])
                if self.moss_packages_dir.exists():
                    previous = self.moss_root / "packages-previous"
                    if previous.exists():
                        shutil.rmtree(previous, ignore_errors=True)
                    self.moss_packages_dir.replace(previous)
                staging.replace(self.moss_packages_dir)
                _atomic_json(self.moss_state_path, {"status": "installed", "installed_at": _now(), "python": " ".join(command), "requirements": list(MOSS_REQUIREMENTS), "error": ""})
                self._activate_optional_packages()
            await emit({"type": "voice.moss.install.progress", "stage": "models", "summary": "正在准备 MOSS 的 ONNX 语音模型。"})
            model_error = ""
            try:
                await asyncio.to_thread(self._prepare_moss_models_sync)
            except Exception as exc:
                model_error = str(exc)[:500]
                state = _read_json(self.moss_state_path)
                state.update({"status": "installed", "error": model_error})
                _atomic_json(self.moss_state_path, state)
            else:
                state = _read_json(self.moss_state_path)
                state.update({"status": "installed", "error": "", "models_ready_at": _now()})
                _atomic_json(self.moss_state_path, state)
                save_user_settings({"voice_engine": "moss", "voice_light_tts_model": MOSS_TTS_MODEL_ID})
            result = self.status()
            if model_error:
                result["moss_setup"] = "installed_but_models_pending"
                result["moss"]["last_error"] = model_error
                await emit({"type": "voice.moss.models_failed", "summary": model_error})
            else:
                result["moss_setup"] = "ready"
                await emit({"type": "voice.moss.ready", "summary": "MOSS 本地普通话朗读已准备完成。"})
            return result

    def _prepare_moss_models_sync(self) -> None:
        """Download MOSS ONNX assets only from an explicit settings action."""
        self._activate_optional_packages()
        try:
            from huggingface_hub import snapshot_download
        except ModuleNotFoundError as exc:
            raise RuntimeError("MOSS 需要 huggingface_hub 下载模型；请先完成依赖安装。")
        tts_dir = self.moss_model_dir / MOSS_TTS_MODEL_ID
        codec_dir = self.moss_model_dir / MOSS_CODEC_MODEL_ID
        if self._moss_manifest_path() is None:
            snapshot_download(
                repo_id=MOSS_TTS_REPO_ID,
                local_dir=str(tts_dir),
                allow_patterns=["*.onnx", "*.data", "*.json", "tokenizer.model"],
            )
        if self._moss_codec_meta_path() is None:
            snapshot_download(
                repo_id=MOSS_CODEC_REPO_ID,
                local_dir=str(codec_dir),
                allow_patterns=["*.onnx", "*.data", "*.json"],
            )
        if not self._moss_models_ready():
            raise RuntimeError("MOSS 模型不完整：缺少 browser_poc_manifest.json 或 codec_browser_onnx_meta.json。")

    async def delete_voice_component(self, component: str, confirmation: str) -> dict[str, Any]:
        """Delete downloaded voice files without blocking the event loop."""
        component = str(component or "").strip().lower()
        confirmations = {
            "moss": "delete_moss_voice",
            "qwen": "delete_qwen_voice",
            "sherpa": "delete_sherpa_voice",
            "kokoro": "delete_kokoro_voice",
        }
        if component not in confirmations:
            raise ValueError("只支持删除 moss 或 qwen；sherpa/kokoro 已在升级时移除。")
        if confirmation != confirmations[component]:
            raise ValueError("删除前需要确认；只会删除语音组件文件，不会删除录音和声音档案。")
        if self._inference_lock is not None and self._inference_lock.locked():
            raise ValueError("当前有朗读任务正在使用语音模型，请先暂停或取消任务后再删除。")
        return await asyncio.to_thread(self._delete_voice_component_sync, component)

    def _delete_voice_component_sync(self, component: str) -> dict[str, Any]:
        self._clear_loaded_tts()
        target_map = {
            "moss": (self.moss_packages_dir, self.moss_model_dir, self.moss_state_path),
            "qwen": (self.qwen_packages_dir, self.qwen_model_cache_dir, self.qwen_state_path, self.qwen_models_path),
            "sherpa": (self.sherpa_root,),
            "kokoro": (self.kokoro_root,),
        }
        removed: list[str] = []
        errors: list[str] = []
        root = self.root.resolve()
        for target in target_map[component]:
            resolved = target.resolve()
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
        settings = Settings.from_env()
        if component == "qwen" and settings.voice_engine == "qwen":
            save_user_settings({"voice_engine": "moss"})
        return self.status()

    @staticmethod
    def _download_voice_model(url: str, destination: Path, required: tuple[str, ...] = ("tokens.txt",)) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() != destination.absolute():
            raise RuntimeError("模型目标目录被重定向，无法安全更新，请检查本地语音目录。")
        if any(destination.glob("*.onnx")) and all((destination / name).is_file() for name in required):
            return
        archive_path = destination.parent / f".{destination.name}.download"
        extraction = destination.parent / f".{destination.name}.extract-{uuid.uuid4().hex}"
        try:
            with urllib.request.urlopen(url, timeout=60) as response, archive_path.open("wb") as output:
                while True:
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
            source.replace(destination)
            shutil.rmtree(extraction, ignore_errors=True)
        finally:
            if archive_path.exists():
                archive_path.unlink()
            if extraction.exists():
                shutil.rmtree(extraction)

    async def prepare_models(self, workspace_root: str | Path | None, confirmation: str, emit: VoiceEventSink) -> dict[str, Any]:
        if confirmation != "download_local_voice_models":
            raise ValueError("准备本地语音模型前需要确认磁盘、等待时间与显存影响。")
        self._activate_optional_packages()
        settings = Settings.from_env(workspace_root)
        missing = [name for name in ("qwen_tts", "torch", "soundfile") if not _package_available(name)]
        if missing:
            raise RuntimeError(f"Qwen 语音依赖尚未安装：{', '.join(missing)}。请在设置中点击‘安装 Qwen 并适配’。")
        await emit({"type": "voice.models.preparing", "summary": "正在下载 Qwen 预设声音和克隆模型；下载期间不加载显卡"})
        if self._inference_lock is None:
            self._inference_lock = asyncio.Lock()
        async with self._inference_lock:
            await asyncio.to_thread(self._prepare_models_sync, settings)
        result = self.status(workspace_root)
        await emit({"type": "voice.models.ready", "summary": "本地语音模型已经准备完成"})
        return result

    def list_profiles(self) -> list[dict[str, Any]]:
        profiles = [dict(item) for item in BUILTIN_PROFILES]
        for path in sorted(self.profiles_dir.glob("*.json")):
            value = _read_json(path)
            if value.get("profile_id") and value.get("name"):
                profiles.append(value)
        return profiles

    async def create_clone(self, params: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(params.get("audio_path") or "")).resolve()
        if not bool(params.get("consent_confirmed")):
            raise ValueError("创建克隆声音前，需要确认你拥有这段声音的使用权并已获得必要同意。")
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            raise ValueError("请选择 WAV、MP3、M4A、FLAC、OGG 或 WEBM 语音文件。")
        size = source.stat().st_size
        if size < 8_000 or size > 100 * 1024 * 1024:
            raise ValueError("参考语音应清晰且大小在 8KB 到 100MB 之间。")
        quality = self._inspect_audio(source)
        duration = float(quality.get("duration_seconds") or 0)
        if not duration:
            raise ValueError("无法确认参考录音时长，请使用内置录音或可读取的 WAV 文件。")
        if not 3 <= duration <= 120:
            raise ValueError("克隆参考语音建议为 3 到 120 秒；当前时长不适合建立稳定声音档案。")
        name = str(params.get("name") or "我的声音").strip()[:40]
        reference_text = str(params.get("reference_text") or "").strip()
        if not reference_text:
            raise ValueError("当前版本已移除 sherpa 语音输入；请先让 Writer 生成朗读稿，并填写与录音一致的原文。")
        if len(re.sub(r"\s+", "", reference_text)) > 400:
            raise ValueError("声音克隆参考朗读稿最多 400 字；请使用 Writer 生成的朗读稿或截短后重试。")
        profile_id = f"clone-{uuid.uuid4().hex}"
        folder = self.profiles_dir / profile_id
        folder.mkdir(parents=True, exist_ok=False)
        copied = folder / f"reference{source.suffix.lower()}"
        shutil.copy2(source, copied)
        profile = {
            "profile_id": profile_id,
            "name": name,
            "kind": "clone",
            "gender": str(params.get("gender") or "other"),
            "description": str(params.get("description") or "本机创建的自定义声音").strip()[:160],
            "reference_audio": str(copied),
            "reference_text": reference_text,
            "instruction": str(params.get("instruction") or "自然普通话，保留原声线。"),
            "speed": _clamp_float(params.get("speed", 1.0), 0.75, 1.35),
            "volume": _clamp_float(params.get("volume", 1.0), 0.25, 1.5),
            "consent_confirmed": True,
            "created_at": _now(),
            "updated_at": _now(),
            "source": "user_owned_local_audio",
            "audio_quality": quality,
        }
        _atomic_json(self.profiles_dir / f"{profile_id}.json", profile)
        return profile

    def update_profile(self, profile_id: str, params: dict[str, Any]) -> dict[str, Any]:
        path = self.profiles_dir / f"{profile_id}.json"
        profile = _read_json(path)
        if not profile:
            raise ValueError("只允许微调本机创建的自定义声音。")
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

    def prepare_finetune(self, profile_id: str, dataset_path: str) -> dict[str, Any]:
        profile = self._profile(profile_id)
        if profile.get("kind") != "clone":
            raise ValueError("本地微调只适用于用户创建的克隆声音。")
        dataset = Path(dataset_path).resolve()
        if not dataset.is_dir():
            raise ValueError("请选择包含授权语音与文本标注的数据集文件夹。")
        plan = {
            "training_id": f"training-{uuid.uuid4().hex}",
            "status": "prepared",
            "profile_id": profile_id,
            "dataset_path": str(dataset),
            "created_at": _now(),
            "message": "微调计划已准备，尚未开始训练、下载模型或占用显卡。开始训练前需要单独确认资源与数据授权。",
        }
        _atomic_json(self.root / "training" / f"{plan['training_id']}.json", plan)
        return plan

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
        raise RuntimeError("sherpa 已移除；当前版本暂不提供语音输入（MOSS 只负责朗读）。")

    async def speak(self, text: str, profile_id: str | None = None) -> dict[str, Any]:
        settings = Settings.from_env()
        if not settings.voice_enabled or not settings.voice_output_enabled:
            raise ValueError("请先在设置中开启本地语音与朗读输出。")
        clean = str(text).strip()
        if not clean:
            raise ValueError("没有可朗读的文字。")
        if len(clean) > 4_000:
            raise ValueError("单条对话朗读最多 4000 字；更长内容请使用听读中心后台转换。")
        self._activate_optional_packages()
        if self._short_lock is None:
            self._short_lock = asyncio.Lock()
        if self._inference_lock is None:
            self._inference_lock = asyncio.Lock()
        async with self._short_lock:
            output = self.short_cache_dir / f"speech-{uuid.uuid4().hex}.wav"
            profile = self._profile(profile_id or settings.voice_default_profile)
            async with self._inference_lock:
                await asyncio.to_thread(self._synthesize_sync, clean, profile, output, settings)
            self._prune_short_cache(settings.voice_cache_limit_mb)
        return {"audio_path": str(output), "profile": profile, "characters": len(clean)}

    def create_job(self, project_root: str | Path, params: dict[str, Any], emit: VoiceEventSink) -> dict[str, Any]:
        settings = Settings.from_env(project_root)
        if not settings.voice_enabled or not settings.voice_output_enabled:
            raise ValueError("请先在设置中开启本地语音与朗读输出。")
        text = str(params.get("text") or "").strip()
        if not text:
            raise ValueError("没有可转换的正文或草稿。")
        if len(text) > 2_000_000:
            raise ValueError("单次后台转换最多 200 万字，请按卷或章节分批处理。")
        job_id = f"voice-{uuid.uuid4().hex}"
        folder = self.jobs_dir / job_id
        segments_dir = folder / "segments"
        segments_dir.mkdir(parents=True, exist_ok=False)
        source_path = folder / "source.txt"
        source_path.write_text(text, encoding="utf-8", newline="\n")
        role_map = self.get_role_map(project_root)
        segments = self._split_segments(text, role_map, settings.voice_segment_chars)
        manifest = {
            "job_id": job_id,
            "project_key": _project_key(project_root),
            "project_root": str(Path(project_root).resolve()),
            "source_name": str(params.get("source_name") or "未命名文本")[:120],
            "source_type": str(params.get("source_type") or "text")[:40],
            "source_path": str(source_path),
            "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
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
        return self._public_job(manifest)

    def list_jobs(self, project_root: str | Path) -> list[dict[str, Any]]:
        key = _project_key(project_root)
        jobs: list[dict[str, Any]] = []
        for path in self.jobs_dir.glob("voice-*/job.json"):
            value = _read_json(path)
            if value.get("project_key") == key:
                jobs.append(self._public_job(value))
        return sorted(jobs, key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def job_status(self, job_id: str) -> dict[str, Any]:
        return self._public_job(self._read_job(job_id))

    def pause_job(self, job_id: str) -> dict[str, Any]:
        value = self._read_job(job_id)
        if value.get("status") in {"queued", "running"}:
            value["status"] = "paused"
            value["updated_at"] = _now()
            self._write_job(value)
        return self._public_job(value)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        value = self._read_job(job_id)
        if value.get("status") not in {"completed", "failed", "cancelled"}:
            value["status"] = "cancelled"
            value["updated_at"] = _now()
            self._write_job(value)
            task = self._jobs.get(job_id)
            if task and not task.done():
                task.cancel()
        return self._public_job(value)

    def resume_job(self, job_id: str, emit: VoiceEventSink) -> dict[str, Any]:
        value = self._read_job(job_id)
        if value.get("status") not in {"paused", "interrupted", "failed"}:
            return self._public_job(value)
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
                self._write_job(value)
                await emit({"type": "voice.job.progress", "job": self._public_job(value), "summary": "后台听读转换已开始"})
                settings = Settings.from_env(value.get("project_root"))
                completed = int(value.get("completed_segments") or 0)
                segments = value.get("segments") if isinstance(value.get("segments"), list) else []
                for index, segment in enumerate(segments):
                    latest = self._read_job(job_id)
                    if latest.get("status") in {"paused", "cancelled"}:
                        return
                    if index < completed and segment.get("audio_path") and Path(str(segment["audio_path"])).is_file():
                        continue
                    output = Path(str(value["output_dir"])) / f"{index + 1:05d}.wav"
                    profile = self._profile(str(segment.get("profile_id") or value["role_map"]["narrator_profile_id"]))
                    async with self._inference_lock:
                        await asyncio.to_thread(self._synthesize_sync, str(segment["text"]), profile, output, settings)
                    value = self._read_job(job_id)
                    segments = value.get("segments") if isinstance(value.get("segments"), list) else []
                    segment = segments[index]
                    segment["audio_path"] = str(output)
                    segment["status"] = "completed"
                    value["completed_segments"] = index + 1
                    value["progress"] = round((index + 1) * 100 / max(1, len(segments)))
                    value["updated_at"] = _now()
                    self._write_job(value)
                    self._write_playlist(value)
                    if value.get("status") in {"paused", "cancelled"}:
                        return
                    await emit({"type": "voice.job.progress", "job": self._public_job(value), "summary": f"听读转换 {value['progress']}%"})
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
            value["updated_at"] = _now()
            self._write_job(value)
            await emit({"type": "voice.job.failed", "job": self._public_job(value), "summary": value["error"]})

    def _split_segments(self, text: str, role_map: dict[str, Any], max_chars: int = 360) -> list[dict[str, Any]]:
        chunks = [chunk.strip() for chunk in re.split(r"(?<=[\u3002\uff01\uff1f!\uff1b;])\s*|\n+", text) if chunk.strip()]
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
                result.append(
                    {
                        "index": len(result) + 1,
                        "text": piece,
                        "speaker": speaker or "旁白",
                        "profile_id": profile_id,
                        "ambiguous": speaker is None and ("“" in piece or '"' in piece),
                        "status": "pending",
                        "audio_path": "",
                    }
                )
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

    def _transcribe_sherpa_sync(self, source: Path, settings: Settings) -> str:
        if not _package_available("sherpa_onnx") or not _package_available("soundfile"):
            raise RuntimeError("sherpa-onnx 或 soundfile 尚未安装。")
        model_path = self._sherpa_asr_model_path()
        if model_path is None:
            raise RuntimeError("普通话识别模型尚未下载，请在设置中点击‘下载 Kokoro’。")
        import sherpa_onnx
        import soundfile as sf

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
                    debug=settings.voice_debug,
                )
            self._sherpa_asr_models[cache_key] = recognizer
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
        if not text:
            raise RuntimeError("没有识别到清晰的普通话内容，请更换安静环境下的录音。")
        return text

    @staticmethod
    def _inspect_audio(source: Path) -> dict[str, Any]:
        try:
            if source.suffix.lower() == ".wav":
                with wave.open(str(source), "rb") as audio:
                    duration = audio.getnframes() / max(1, audio.getframerate())
                    channels = audio.getnchannels()
                    sample_rate = audio.getframerate()
            else:
                import soundfile as sf

                info = sf.info(str(source))
                duration = float(info.duration)
                channels = int(info.channels)
                sample_rate = int(info.samplerate)
            warnings: list[str] = []
            if duration < 10:
                warnings.append("参考语音较短，克隆稳定性可能下降。")
            if channels > 1:
                warnings.append("参考语音为多声道，建议使用单声道人声。")
            if sample_rate < 16_000:
                warnings.append("采样率低于 16kHz，可能影响清晰度。")
            return {
                "duration_seconds": round(duration, 2),
                "channels": channels,
                "sample_rate": sample_rate,
                "warnings": warnings,
            }
        except Exception:
            return {"duration_seconds": None, "channels": None, "sample_rate": None, "warnings": ["当前环境无法读取音频参数，将在首次克隆试听时确认质量。"]}

    def _qwen_model_path(self, model_name: str) -> Path | None:
        value = _read_json(self.qwen_models_path).get(model_name)
        if not isinstance(value, str):
            return None
        folder = Path(value)
        if not folder.is_dir() or not (folder / "config.json").is_file():
            return None
        return folder

    def _prepare_models_sync(self, settings: Settings) -> None:
        """Only an explicit install/prepare action may download Qwen weights."""
        self._activate_optional_packages()
        from huggingface_hub import snapshot_download

        # Download both preset and clone models to disk without reserving GPU memory.
        # The same confirmed preparation action can resume an interrupted download.
        paths = _read_json(self.qwen_models_path)
        for model_name in dict.fromkeys((settings.voice_tts_model, settings.voice_clone_model)):
            local = Path(model_name).expanduser()
            if local.is_dir():
                folder = str(local.resolve())
            else:
                folder = snapshot_download(
                    repo_id=model_name,
                    cache_dir=str(self.qwen_model_cache_dir / "hub"),
                )
            paths[model_name] = folder
            _atomic_json(self.qwen_models_path, paths)

    def _synthesize_sync(self, text: str, profile: dict[str, Any], output: Path, settings: Settings) -> None:
        self._activate_optional_packages()
        moss = self._moss_status()
        qwen = self._qwen_status()
        use_clone = profile.get("kind") == "clone"
        if use_clone or settings.voice_engine == "qwen":
            backend = "qwen"
        elif moss["tts_ready"]:
            backend = "moss"
        else:
            raise RuntimeError("没有可用的朗读模型，请在设置中安装 MOSS 或 Qwen。")
        if backend == "moss":
            self._clear_loaded_qwen_if_needed()
            self._synthesize_moss_sync(text, profile, output, settings)
            return
        if not qwen["installed"]:
            raise RuntimeError("当前选择了 Qwen，但 Qwen 依赖或模型尚未安装。")
        self._synthesize_qwen_sync(text, profile, output, settings)

    def _clear_loaded_qwen_if_needed(self) -> None:
        if self._tts_models:
            self._tts_models.clear()
            gc.collect()

    def _moss_execution_provider(self, settings: Settings) -> str:
        if settings.voice_compute_device == "cpu":
            return "cpu"
        try:
            import onnxruntime as ort
            providers = set(ort.get_available_providers())
        except Exception:
            providers = set()
        cuda_available = "CUDAExecutionProvider" in providers
        if settings.voice_compute_device == "cuda":
            if not cuda_available:
                raise RuntimeError("MOSS 已要求 CUDA，但当前 onnxruntime 没有 CUDAExecutionProvider；请安装 onnxruntime-gpu 和 CUDA/cuDNN，或改为自动/CPU。")
            return "cuda"
        return "cuda" if cuda_available else "cpu"

    def _synthesize_moss_sync(self, text: str, profile: dict[str, Any], output: Path, settings: Settings) -> None:
        if not self._moss_package_ready() or not self._moss_models_ready():
            raise RuntimeError("MOSS 的 ONNX 依赖或模型尚未准备好，请先在设置中安装 MOSS。")
        try:
            import numpy as np
            import soundfile as sf
            from onnx_tts_runtime import OnnxTtsRuntime
        except (ImportError, ModuleNotFoundError) as exc:
            raise RuntimeError("MOSS ONNX 运行库加载失败，请重新安装 MOSS 依赖。")
        provider = self._moss_execution_provider(settings)
        thread_count = min(8, max(2, (os.cpu_count() or 4) // 2))
        cache_key = f"moss:{self.moss_model_dir}:{provider}:{thread_count}:{settings.voice_debug}"
        runtime = self._moss_tts_models.get(cache_key)
        if runtime is None:
            runtime = OnnxTtsRuntime(
                model_dir=self.moss_model_dir,
                thread_count=thread_count,
                max_new_frames=375,
                do_sample=True,
                sample_mode="fixed",
                execution_provider=provider,
            )
            self._moss_tts_models.clear()
            self._moss_tts_models[cache_key] = runtime
        units = _mandarin_speech_units(text)
        if not units:
            raise RuntimeError("没有可朗读的文字。")
        voice = str(profile.get("moss_voice") or "Junhao")
        reference_audio = str(profile.get("reference_audio") or "").strip() if profile.get("kind") == "clone" else ""
        if reference_audio and not Path(reference_audio).is_file():
            raise RuntimeError("声音克隆参考音频不存在，请重新录音或选择文件。")
        speed = _clamp_float(profile.get("speed", settings.voice_speed), 0.75, 1.35)
        volume = _clamp_float(profile.get("volume", settings.voice_volume), 0.25, 1.5)
        pause_scale = _clamp_float(settings.voice_pause_scale, 0.6, 1.8)
        rendered: list[np.ndarray] = []
        sample_rate = 48_000
        for index, (sentence, pause) in enumerate(units, start=1):
            # The model stays warm; only the text request is split. This gives
            # predictable memory use and lets long jobs publish progress per sentence.
            temporary = output.parent / f".{output.stem}.moss-{index}.wav"
            try:
                result = runtime.synthesize(
                    text=sentence,
                    voice=voice,
                    prompt_audio_path=reference_audio or None,
                    output_audio_path=temporary,
                    sample_mode="fixed",
                    do_sample=True,
                    streaming=True,
                    max_new_frames=375,
                    voice_clone_max_text_tokens=96,
                    enable_wetext=False,
                    enable_normalize_tts_text=True,
                )
                audio = np.asarray(result.get("waveform"), dtype=np.float32)
                sample_rate = int(result.get("sample_rate") or sample_rate)
            finally:
                if temporary.exists():
                    temporary.unlink()
            if audio.size == 0:
                raise RuntimeError("MOSS 没有生成有效音频。")
            if audio.ndim == 1:
                audio = audio.reshape(-1, 1)
            if abs(speed - 1.0) > 0.01 and audio.shape[0] > 1:
                target_length = max(1, int(audio.shape[0] / speed))
                original = np.arange(audio.shape[0])
                target = np.linspace(0, audio.shape[0] - 1, target_length)
                audio = np.stack([np.interp(target, original, audio[:, channel]) for channel in range(audio.shape[1])], axis=1).astype(np.float32)
            fade_samples = min(audio.shape[0] // 2, max(1, int(sample_rate * 0.008)))
            if fade_samples > 1:
                fade = np.linspace(0.15, 1.0, fade_samples, dtype=np.float32)
                audio[:fade_samples] *= fade[:, None]
                audio[-fade_samples:] *= fade[::-1, None]
            rendered.append(audio)
            silence_samples = int(sample_rate * pause * pause_scale)
            if silence_samples and index < len(units):
                rendered.append(np.zeros((silence_samples, audio.shape[1]), dtype=np.float32))
        samples = np.concatenate(rendered, axis=0)
        samples = np.clip(samples * volume, -1.0, 1.0)
        target_rate = int(settings.voice_sample_rate)
        if target_rate != sample_rate and samples.shape[0] > 1:
            original = np.arange(samples.shape[0])
            target = np.linspace(0, samples.shape[0] - 1, max(1, int(samples.shape[0] * target_rate / sample_rate)))
            samples = np.stack([np.interp(target, original, samples[:, channel]) for channel in range(samples.shape[1])], axis=1).astype(np.float32)
            sample_rate = target_rate
        output.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(output), samples, sample_rate, subtype="PCM_16")

    def _synthesize_qwen_sync(self, text: str, profile: dict[str, Any], output: Path, settings: Settings) -> None:
        if not _package_available("qwen_tts") or not _package_available("soundfile"):
            raise RuntimeError("尚未安装 Qwen3-TTS 本地语音输出组件。")
        self.qwen_model_cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(self.qwen_model_cache_dir))
        import numpy as np
        import soundfile as sf
        import torch
        from qwen_tts import Qwen3TTSModel

        device = self._resolve_device(settings, torch)
        is_clone = profile.get("kind") == "clone"
        model_name = settings.voice_clone_model if is_clone else settings.voice_tts_model
        model_path = self._qwen_model_path(model_name)
        if model_path is None:
            raise RuntimeError("Qwen 模型未准备完成；请在设置中确认安装或重新准备模型。朗读不会自动下载。")
        cache_key = f"{model_name}:{device}"
        model = self._tts_models.get(cache_key)
        if model is None:
            self._tts_models.clear()
            self._moss_tts_models.clear()
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
            model = Qwen3TTSModel.from_pretrained(str(model_path), device_map=device, dtype=dtype, local_files_only=True)
            self._tts_models[cache_key] = model
        if is_clone:
            wavs, sample_rate = model.generate_voice_clone(
                text=text.strip(),
                language="Chinese",
                ref_audio=str(profile["reference_audio"]),
                ref_text=str(profile["reference_text"]),
            )
        else:
            wavs, sample_rate = model.generate_custom_voice(
                text=text.strip(),
                language="Chinese",
                speaker=str(profile["speaker"]),
                instruct=(
                    str(profile.get("instruction") or "自然普通话。")
                    + " 按中文标点自然换气，逗号短停，句末完整停顿，避免逐字播报。"
                ),
            )
        audio = np.asarray(wavs[0], dtype=np.float32)
        speed = _clamp_float(profile.get("speed", settings.voice_speed), 0.75, 1.35)
        volume = _clamp_float(profile.get("volume", settings.voice_volume), 0.25, 1.5)
        if abs(speed - 1.0) > 0.01 and audio.size > 1:
            original = np.arange(audio.size)
            target = np.linspace(0, audio.size - 1, max(1, int(audio.size / speed)))
            audio = np.interp(target, original, audio).astype(np.float32)
        audio = np.clip(audio * volume, -1.0, 1.0)
        target_rate = int(settings.voice_sample_rate)
        if target_rate != int(sample_rate) and audio.size > 1:
            original = np.arange(audio.size)
            target = np.linspace(0, audio.size - 1, max(1, int(audio.size * target_rate / sample_rate)))
            audio = np.interp(target, original, audio).astype(np.float32)
            sample_rate = target_rate
        output.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(output), audio, int(sample_rate))

    def _prune_short_cache(self, limit_mb: int) -> None:
        limit_bytes = max(128, int(limit_mb)) * 1024 * 1024
        files = []
        total = 0
        for path in self.short_cache_dir.glob("*.wav"):
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

    @staticmethod
    def _resolve_device(settings: Settings, torch_module: Any | None = None) -> str:
        requested = settings.voice_compute_device
        if requested == "cpu":
            return "cpu"
        if torch_module is None and importlib.util.find_spec("torch") is not None:
            import torch as torch_module
        cuda_available = bool(torch_module and torch_module.cuda.is_available())
        if requested == "cuda" and not cuda_available:
            raise RuntimeError("设置选择了 NVIDIA CUDA，但当前语音环境没有检测到可用 CUDA。")
        return "cuda:0" if cuda_available else "cpu"

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

    @staticmethod
    def _public_job(value: dict[str, Any]) -> dict[str, Any]:
        visible = dict(value)
        visible["segments"] = [dict(segment) for segment in value.get("segments", [])]
        visible.pop("project_root", None)
        visible.pop("source_path", None)
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
