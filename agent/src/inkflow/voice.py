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
KOKORO_TTS_MODEL_ID = "kokoro-int8-multi-lang-v1_1"
KOKORO_TTS_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
    "kokoro-int8-multi-lang-v1_1.tar.bz2"
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
        "kokoro_speaker_id": 3,
        "kokoro_speed_multiplier": 0.98,
        "description": "温和、稳定，适合大多数正文旁白。",
        "instruction": "自然普通话，叙述清楚，情绪克制。",
    },
    {
        "profile_id": "female_bright",
        "name": "明快女声",
        "kind": "builtin",
        "gender": "female",
        "speaker": "Vivian",
        "kokoro_speaker_id": 15,
        "kokoro_speed_multiplier": 1.04,
        "description": "明亮年轻，适合活泼角色。",
        "instruction": "自然普通话，明快但不要夸张。",
    },
    {
        "profile_id": "female_warm",
        "name": "温柔女声",
        "kind": "builtin",
        "gender": "female",
        "speaker": "Serena",
        "kokoro_speaker_id": 32,
        "kokoro_speed_multiplier": 0.92,
        "description": "温暖舒缓，适合成熟或安静的女性角色。",
        "instruction": "自然普通话，温柔舒缓，吐字清晰。",
    },
    {
        "profile_id": "narrator_male",
        "name": "男声旁白",
        "kind": "builtin",
        "gender": "male",
        "speaker": "Uncle_Fu",
        "kokoro_speaker_id": 58,
        "kokoro_speed_multiplier": 0.94,
        "description": "沉稳低缓，适合悬疑或历史叙事。",
        "instruction": "自然普通话，沉稳克制，保持叙述感。",
    },
    {
        "profile_id": "male_calm",
        "name": "沉静男声",
        "kind": "builtin",
        "gender": "male",
        "speaker": "Uncle_Fu",
        "kokoro_speaker_id": 72,
        "kokoro_speed_multiplier": 0.92,
        "description": "沉静自然，适合成年男性角色。",
        "instruction": "自然普通话，语气平静，避免播音腔。",
    },
    {
        "profile_id": "male_firm",
        "name": "坚定男声",
        "kind": "builtin",
        "gender": "male",
        "speaker": "Uncle_Fu",
        "kokoro_speaker_id": 86,
        "kokoro_speed_multiplier": 1.02,
        "description": "更有力度，适合行动型角色。",
        "instruction": "自然普通话，坚定有力，但不要喊叫。",
    },
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mandarin_speech_units(text: str) -> list[tuple[str, float]]:
    """Split Chinese prose into speakable units and retain a natural pause hint.

    Kokoro does not consistently pause at every written punctuation mark, so
    punctuation has to become audio silence instead of relying on the model to
    infer prose rhythm from one long input string.
    """

    normalized = _clean_text_for_speech(text)
    if not normalized:
        return []
    pause_seconds = {
        "、": 0.10,
        "，": 0.16,
        ",": 0.16,
        "；": 0.24,
        ";": 0.24,
        "：": 0.20,
        ":": 0.20,
        "。": 0.34,
        ".": 0.34,
        "！": 0.36,
        "!": 0.36,
        "？": 0.38,
        "?": 0.38,
        "…": 0.42,
        "—": 0.28,
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
            previous_count = len(units)
            flush(0.46)
            if len(units) == previous_count and units:
                units[-1] = (units[-1][0], max(units[-1][1], 0.46))
            continue
        buffer.append(character)
        if character in terminal_marks:
            flush(pause_seconds[character])
        elif len(buffer) >= 96:
            # A hard safety split for long unpunctuated text. It is shorter
            # than the outer job segment so the voice keeps stable breath length.
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
        self.sherpa_root = self.root / "sherpa"
        self.kokoro_root = self.root / "kokoro"
        self.kokoro_tts_dir = self.kokoro_root / KOKORO_TTS_MODEL_ID
        self.sherpa_asr_dir = self.sherpa_root / "asr" / SHERPA_ASR_MODEL_ID
        self.migrations_dir = self.root / "migrations"
        for folder in (
            self.profiles_dir,
            self.projects_dir,
            self.jobs_dir,
            self.short_cache_dir,
            self.qwen_root,
            self.qwen_model_cache_dir,
            self.sherpa_root,
            self.kokoro_root,
            self.migrations_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._short_lock: asyncio.Lock | None = None
        self._long_lock: asyncio.Lock | None = None
        self._inference_lock: asyncio.Lock | None = None
        self._qwen_install_lock: asyncio.Lock | None = None
        self._qwen_install_task: asyncio.Task[dict[str, Any]] | None = None
        self._kokoro_model_lock: asyncio.Lock | None = None
        self._tts_models: dict[str, Any] = {}
        self._kokoro_tts_models: dict[str, Any] = {}
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
            save_user_settings(allowed)
        return self.settings(workspace_root)

    def _migrate_legacy_voice_assets(self) -> None:
        """Remove only the replaced, downloaded engines once after this upgrade.

        Profiles, reference audio, generated audio, jobs and the whole voice root are
        deliberately outside this list. A failed deletion stays visible in the marker
        and is retried on the next application start.
        """

        marker = self.migrations_dir / "kokoro-qwen-1.7.json"
        existing = _read_json(marker)
        if existing.get("completed"):
            return
        safe_roots = (
            (self.sherpa_root / "tts").resolve(),
            (self.qwen_model_cache_dir / "hub").resolve(),
        )
        targets = (
            self.sherpa_root / "tts" / LEGACY_VITS_MODEL_ID,
            self.qwen_model_cache_dir / "hub" / "models--Qwen--Qwen3-TTS-12Hz-0.6B-CustomVoice",
            self.qwen_model_cache_dir / "hub" / "models--Qwen--Qwen3-TTS-12Hz-0.6B-Base",
        )
        removed: list[str] = []
        errors: list[str] = []
        for target in targets:
            resolved = target.resolve()
            if not target.exists():
                continue
            if resolved != target.absolute() or self.root.resolve() not in resolved.parents or not any(root in resolved.parents for root in safe_roots):
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
                "scope": "only legacy VITS and Qwen 0.6B model caches",
            },
        )

    def status(self, workspace_root: str | Path | None = None) -> dict[str, Any]:
        settings = Settings.from_env(workspace_root)
        self._activate_optional_packages()
        packages = {
            "qwen_tts": _package_available("qwen_tts"),
            "torch": _package_available("torch"),
            "soundfile": _package_available("soundfile"),
            "sherpa_onnx": _package_available("sherpa_onnx"),
        }
        sherpa = self._sherpa_status(packages)
        kokoro = self._kokoro_status(packages)
        qwen = self._qwen_status(packages)
        ready_for_input = sherpa["asr_ready"]
        backend = self._selected_backend(settings, kokoro, qwen)
        ready_for_output = backend != "unavailable"
        return {
            "enabled": settings.voice_enabled,
            "ready_for_input": ready_for_input,
            "ready_for_output": ready_for_output,
            "packages": packages,
            "compute_device": settings.voice_compute_device,
            "data_root": str(self.root),
            "formal_agent": False,
            "mode": "local_mandarin",
            "backend": backend,
            "kokoro": kokoro,
            "asr": sherpa,
            "qwen": qwen,
            "migration": _read_json(self.migrations_dir / "kokoro-qwen-1.7.json"),
            "models_loaded": {
                "asr": bool(self._sherpa_asr_models),
                "tts": bool(self._tts_models or self._kokoro_tts_models),
            },
            "message": (
                "Kokoro 中文朗读已就绪。"
                if backend == "kokoro"
                else "Qwen 高品质中文朗读已就绪。"
                if backend == "qwen"
                else "当前选择的朗读模型尚未就绪，请在设置中完成对应安装。"
            ),
        }

    def _activate_optional_packages(self) -> None:
        package_path = str(self.qwen_packages_dir.resolve())
        if self.qwen_packages_dir.is_dir() and package_path not in sys.path:
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

    def _kokoro_status(self, packages: dict[str, bool] | None = None) -> dict[str, Any]:
        package_ready = bool((packages or {}).get("sherpa_onnx", _package_available("sherpa_onnx")))
        model_path = self._kokoro_tts_model_path()
        return {
            "package_installed": package_ready,
            "tts_ready": package_ready and _package_available("soundfile") and model_path is not None,
            "tts_model": str(model_path) if model_path else "",
            "model_root": str(self.kokoro_root),
            "model_size_mb": round(_directory_size(self.kokoro_root) / 1024 / 1024, 1),
            "estimated_download_mb": 500,
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

    def _selected_backend(self, settings: Settings, kokoro: dict[str, Any], qwen: dict[str, Any]) -> str:
        if settings.voice_engine == "qwen":
            return "qwen" if qwen["installed"] and self._qwen_model_path(settings.voice_tts_model) else "unavailable"
        return "kokoro" if kokoro["tts_ready"] else "unavailable"

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

    def _kokoro_tts_model_path(self) -> Path | None:
        if not all((self.kokoro_tts_dir / name).exists() for name in (
            "voices.bin", "tokens.txt", "lexicon-zh.txt", "espeak-ng-data",
            "phone-zh.fst", "date-zh.fst", "number-zh.fst",
        )):
            return None
        candidates = (
            self.kokoro_tts_dir / "model.int8.onnx",
            self.kokoro_tts_dir / "model.onnx",
        )
        return next((path for path in candidates if path.is_file()), None)

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

    async def prepare_kokoro_models(self, confirmation: str, emit: VoiceEventSink) -> dict[str, Any]:
        if confirmation != "download_kokoro_voice_models":
            raise ValueError("下载 Kokoro 前需要确认约 500MB 下载量，解压还需额外磁盘空间。")
        if not _package_available("sherpa_onnx"):
            raise RuntimeError("当前安装包未包含 Kokoro 所需的 sherpa-onnx 运行库，请使用包含本地语音组件的安装包。")
        if self._kokoro_model_lock is None:
            self._kokoro_model_lock = asyncio.Lock()
        async with self._kokoro_model_lock:
            await emit({"type": "voice.kokoro.installing", "stage": "tts", "summary": "正在下载 Kokoro 多语种中文朗读模型（约 270MB）"})
            await asyncio.to_thread(
                self._download_voice_model, KOKORO_TTS_MODEL_URL, self.kokoro_tts_dir,
                ("tokens.txt", "voices.bin", "lexicon-zh.txt", "espeak-ng-data/phontab",
                 "phone-zh.fst", "date-zh.fst", "number-zh.fst"),
            )
            await emit({"type": "voice.kokoro.installing", "stage": "asr", "summary": "正在下载普通话识别模型（约 230MB）"})
            await asyncio.to_thread(self._download_voice_model, SHERPA_ASR_MODEL_URL, self.sherpa_asr_dir)
            save_user_settings({"voice_engine": "kokoro"})
            result = self.status()
            await emit({"type": "voice.kokoro.ready", "summary": "Kokoro 本地普通话语音已准备完成"})
            return result

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
            reference_text = await self.transcribe(source, allow_disabled=True)
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
            raise ValueError("请先在设置中开启本地语音与语音输入。")
        source = Path(audio_path).resolve()
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            raise ValueError("没有找到可识别的语音文件。")
        self._activate_optional_packages()
        sherpa = self._sherpa_status()
        if not sherpa["asr_ready"]:
            raise RuntimeError("尚未安装本地普通话识别组件；请在设置中下载 Kokoro 语音组件。")
        if self._inference_lock is None:
            self._inference_lock = asyncio.Lock()
        async with self._inference_lock:
            return await asyncio.to_thread(self._transcribe_sherpa_sync, source, settings)

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
        chunks = [chunk.strip() for chunk in re.split(r"(?<=[。！？!?])\s*|\n+", text) if chunk.strip()]
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
        kokoro = self._kokoro_status()
        qwen = self._qwen_status()
        use_clone = profile.get("kind") == "clone"
        if use_clone or settings.voice_engine == "qwen":
            backend = "qwen"
        elif kokoro["tts_ready"]:
            backend = "kokoro"
        else:
            raise RuntimeError("尚未安装可用的本地语音输出组件，请在设置中下载 Kokoro 或安装 Qwen。")
        if backend == "kokoro":
            if self._tts_models:
                self._tts_models.clear()
                gc.collect()
                if "torch" in sys.modules and sys.modules["torch"].cuda.is_available():
                    sys.modules["torch"].cuda.empty_cache()
            self._synthesize_kokoro_sync(text, profile, output, settings)
            return
        if not qwen["installed"]:
            raise RuntimeError("声音克隆只使用 Qwen3-TTS；请先在设置中安装 Qwen 高品质组件。")
        self._synthesize_qwen_sync(text, profile, output, settings)

    def _synthesize_kokoro_sync(self, text: str, profile: dict[str, Any], output: Path, settings: Settings) -> None:
        if not _package_available("sherpa_onnx") or not _package_available("soundfile"):
            raise RuntimeError("sherpa-onnx 或 soundfile 尚未安装。")
        model_path = self._kokoro_tts_model_path()
        voices = self.kokoro_tts_dir / "voices.bin"
        tokens = self.kokoro_tts_dir / "tokens.txt"
        data_dir = self.kokoro_tts_dir / "espeak-ng-data"
        lexicons = [
            self.kokoro_tts_dir / name
            for name in ("lexicon-us-en.txt", "lexicon-zh.txt")
            if (self.kokoro_tts_dir / name).is_file()
        ]
        if model_path is None or not voices.is_file() or not tokens.is_file() or not data_dir.is_dir() or not lexicons:
            raise RuntimeError("Kokoro 模型尚未下载完整，请在设置中点击‘下载 Kokoro’后重试。")
        import sherpa_onnx
        import soundfile as sf

        cache_key = f"kokoro:{model_path}:{settings.voice_compute_device}:{settings.voice_debug}"
        tts = self._kokoro_tts_models.get(cache_key)
        if tts is None:
            config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                        model=str(model_path),
                        voices=str(voices),
                        tokens=str(tokens),
                        lexicon=",".join(str(item) for item in lexicons),
                        data_dir=str(data_dir),
                    ),
                    provider="cuda" if settings.voice_compute_device == "cuda" else "cpu",
                    debug=settings.voice_debug,
                    num_threads=2,
                ),
                rule_fsts=",".join(str(self.kokoro_tts_dir / name) for name in ("phone-zh.fst", "date-zh.fst", "number-zh.fst")),
            )
            if not config.validate():
                raise RuntimeError("Kokoro 中文朗读模型配置无效。")
            tts = sherpa_onnx.OfflineTts(config)
            self._kokoro_tts_models[cache_key] = tts
        import numpy as np

        units = _mandarin_speech_units(text)
        if not units:
            raise RuntimeError("没有可朗读的有效文字。")
        generation = sherpa_onnx.GenerationConfig()
        generation.sid = max(3, min(102, int(profile.get("kokoro_speaker_id", 3))))
        base_speed = _clamp_float(profile.get("speed", settings.voice_speed), 0.75, 1.35)
        natural_speed = _clamp_float(
            base_speed * float(profile.get("kokoro_speed_multiplier", 1.0)),
            0.75,
            1.35,
        )
        pause_scale = _clamp_float(settings.voice_pause_scale, 0.6, 1.8)
        sample_rate = 0
        rendered: list[Any] = []
        for phrase, pause in units:
            phrase_speed = natural_speed
            if phrase.endswith(("？", "?", "……", "…")):
                phrase_speed *= 0.96
            elif phrase.endswith(("！", "!")):
                phrase_speed *= 1.02
            elif len(phrase) > 55:
                phrase_speed *= 0.98
            generation.speed = _clamp_float(phrase_speed, 0.75, 1.35)
            audio = tts.generate(phrase, generation)
            samples = getattr(audio, "samples", None)
            current_rate = int(getattr(audio, "sample_rate", 16_000) or 16_000)
            if samples is None or len(samples) == 0:
                raise RuntimeError("Kokoro 语音模型没有生成有效音频。")
            if sample_rate and current_rate != sample_rate:
                raise RuntimeError("Kokoro 在同一次朗读中返回了不同采样率。")
            sample_rate = current_rate
            phrase_samples = np.asarray(samples, dtype=np.float32)
            fade_samples = min(len(phrase_samples) // 2, max(1, int(current_rate * 0.008)))
            if fade_samples > 1:
                phrase_samples[:fade_samples] *= np.linspace(0.15, 1.0, fade_samples, dtype=np.float32)
                phrase_samples[-fade_samples:] *= np.linspace(1.0, 0.15, fade_samples, dtype=np.float32)
            rendered.append(phrase_samples)
            silence_samples = int(current_rate * pause * pause_scale)
            if silence_samples:
                rendered.append(np.zeros(silence_samples, dtype=np.float32))
        samples_array = np.concatenate(rendered)
        samples_array = np.clip(samples_array * _clamp_float(profile.get("volume", settings.voice_volume), 0.25, 1.5), -1.0, 1.0)
        target_rate = int(settings.voice_sample_rate)
        if target_rate != sample_rate and samples_array.size > 1:
            original = np.arange(samples_array.size)
            target = np.linspace(0, samples_array.size - 1, max(1, int(samples_array.size * target_rate / sample_rate)))
            samples_array = np.interp(target, original, samples_array).astype(np.float32)
            sample_rate = target_rate
        output.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(output), samples_array, sample_rate, subtype="PCM_16")

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
            self._kokoro_tts_models.clear()
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
