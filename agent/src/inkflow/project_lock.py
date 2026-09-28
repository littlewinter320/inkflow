"""Cross-entry project mutation lock used by desktop, MCP, and batch flows."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import logging
import os
from pathlib import Path
import time
import threading
from typing import Any, Awaitable, Callable, Iterator
from uuid import uuid4

from .errors import ProjectBusyError, ProjectError


_owned: ContextVar[dict[str, tuple[str, int]]] = ContextVar("inkflow_owned_project_locks", default={})
_WAIT_SECONDS = 30.0
_STALE_SECONDS = 6 * 60 * 60
_RELEASE_ATTEMPTS = 5
_RELEASE_RETRY_SECONDS = 0.05
_lock_wait_policy: ContextVar[tuple[float, Callable[[int, float], Awaitable[None]]] | None] = ContextVar(
    "inkflow_project_lock_wait_policy", default=None,
)
_abandoned_tokens: set[tuple[str, str]] = set()
_abandoned_guard = threading.Lock()
_logger = logging.getLogger(__name__)


def _owner_id() -> str:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return f"task:{id(task)}" if task is not None else f"thread:{threading.get_ident()}"


def _key(root: str | Path, lock_name: str = "project.write.lock") -> tuple[str, Path]:
    resolved = Path(root).expanduser().resolve()
    path = resolved / ".inkflow" / lock_name
    return os.path.normcase(str(path)), path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # Permission denied (or an unknown probe failure) does not prove exit.
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:
    # os.kill(pid, 0) is NOT a probe on Windows: it calls TerminateProcess.
    # Request only wait access and never send a signal or request termination.
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        # ERROR_INVALID_PARAMETER means this PID does not exist. In
        # particular, ERROR_ACCESS_DENIED must conservatively retain the lock.
        return ctypes.get_last_error() != 87
    try:
        return kernel.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0: exited
    finally:
        kernel.CloseHandle(handle)


def _is_stale(path: Path) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
        payload = path.read_text(encoding="utf-8")
    except OSError:
        # An unreadable owner is unknown, not evidence of a crashed process.
        return False
    try:
        data = json.loads(payload)
        pid = int(data.get("pid", 0)) if isinstance(data, dict) else 0
    except (ValueError, TypeError, OverflowError):
        pid = 0
    # A long-running live owner is not stale just because six hours passed.
    # Only a successfully read invalid/missing owner falls back to lock age.
    return not _pid_alive(pid) if pid > 0 else age > _STALE_SECONDS


def _lock_identity(path: Path) -> tuple[int, int, int, bytes]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, path.read_bytes()


def _abandoned_key(path: Path, token: str) -> tuple[str, str]:
    return os.path.normcase(str(path)), token


def _local_abandoned_token(path: Path, payload: bytes) -> str | None:
    try:
        data = json.loads(payload)
        if not isinstance(data, dict) or int(data.get("pid", 0)) != os.getpid():
            return None
        token = data.get("token")
    except (ValueError, TypeError, OverflowError):
        return None
    if not isinstance(token, str):
        return None
    with _abandoned_guard:
        return token if _abandoned_key(path, token) in _abandoned_tokens else None


def _try_acquire(path: Path, lock_token: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"pid": os.getpid(), "token": lock_token, "created_at": time.time()},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            identity = _lock_identity(path)
            abandoned_token = _local_abandoned_token(path, identity[3])
            if (abandoned_token or _is_stale(path)) and _lock_identity(path) == identity:
                # Recheck owner bytes/token and file identity before reclaiming.
                # This narrows, but cannot eliminate, a cross-process TOCTOU.
                path.unlink()
                if abandoned_token:
                    with _abandoned_guard:
                        _abandoned_tokens.discard(_abandoned_key(path, abandoned_token))
        except FileNotFoundError:
            pass
        return False
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    return True


def _release(path: Path, lock_token: str) -> None:
    key = _abandoned_key(path, lock_token)
    for attempt in range(_RELEASE_ATTEMPTS if os.name == "nt" else 1):
        try:
            identity = _lock_identity(path)
            data = json.loads(identity[3])
            if not isinstance(data, dict) or data.get("token") != lock_token:
                # A different owner is never ours to remove.
                with _abandoned_guard:
                    _abandoned_tokens.discard(key)
                return
            if _lock_identity(path) != identity:
                continue
            path.unlink()
            with _abandoned_guard:
                _abandoned_tokens.discard(key)
            return
        except FileNotFoundError:
            with _abandoned_guard:
                _abandoned_tokens.discard(key)
            return
        except (OSError, ValueError, TypeError):
            # Windows can briefly deny deletion while another process scans
            # the file. Retry, then remember only this exact released token.
            pass
        if attempt + 1 < (_RELEASE_ATTEMPTS if os.name == "nt" else 1):
            time.sleep(_RELEASE_RETRY_SECONDS)
    with _abandoned_guard:
        _abandoned_tokens.add(key)
    _logger.warning("Project lock release failed; exact local token marked for recovery: %s", path)


def _wait_error(path: Path, access_error: PermissionError | None) -> ProjectError:
    if access_error is not None:
        code = getattr(access_error, "winerror", None) or access_error.errno
        return ProjectError(
            f"等待项目锁时仍无法访问锁文件（系统错误 {code}，可能是临时共享占用或文件权限）：{path}。"
            "本次失败没有自动删除锁或放宽权限，也不会强行接管仍在运行的任务。"
            "请确认其他墨流窗口已完成操作后重试；若持续出现，请检查项目目录访问权限。"
        )
    return ProjectBusyError("当前项目正在执行另一项写作、审查或正史操作，请等待它完成后重试。")


@contextmanager
def project_lock_wait_policy(
    *,
    on_wait: Callable[[int, float], Awaitable[None]],
    probe_timeout: float = 2.0,
) -> Iterator[None]:
    """Retry only lock acquisition; the protected operation has not started yet."""
    timeout = float(probe_timeout)
    if timeout <= 0:
        raise ValueError("Project lock probe timeout must be positive.")
    token = _lock_wait_policy.set((timeout, on_wait))
    try:
        yield
    finally:
        _lock_wait_policy.reset(token)


@asynccontextmanager
async def project_write_lock(root: str | Path, *, timeout: float = _WAIT_SECONDS):
    async with _named_project_lock(root, "project.write.lock", timeout=timeout):
        yield


@asynccontextmanager
async def chapter_operation_lock(root: str | Path, chapter_no: int, *, timeout: float = _WAIT_SECONDS):
    """Serialize one chapter's multi-stage workflow without blocking other chapters."""
    if chapter_no < 1:
        raise ValueError("Chapter number must be positive.")
    async with _named_project_lock(root, f"chapter-{chapter_no:05d}.operation.lock", timeout=timeout):
        yield


@asynccontextmanager
async def batch_workflow_lock(root: str | Path, *, timeout: float = _WAIT_SECONDS):
    """Serialize batch/long-run coordinators, not unrelated project writes."""
    async with _named_project_lock(root, "batch.workflow.lock", timeout=timeout):
        yield


@asynccontextmanager
async def _named_project_lock(root: str | Path, lock_name: str, *, timeout: float):
    project_key, path = _key(root, lock_name)
    ownership = _owned.get()
    owner_id = _owner_id()
    if project_key in ownership and ownership[project_key][0] == owner_id:
        nested = dict(ownership)
        nested[project_key] = (owner_id, nested[project_key][1] + 1)
        state = _owned.set(nested)
        try:
            yield
        finally:
            _owned.reset(state)
        return

    policy = _lock_wait_policy.get()
    wait_timeout = min(timeout, policy[0]) if policy else timeout
    lock_token = uuid4().hex
    deadline = time.monotonic() + wait_timeout
    wait_attempt = 0
    retry_delay = 0.5
    while True:
        access_error = None
        try:
            if _try_acquire(path, lock_token):
                break
        except PermissionError as exc:
            # Windows can briefly deny CREATE_NEW while a previous lock is
            # being deleted. Retry within the same deadline, never change ACLs.
            access_error = exc
        if time.monotonic() >= deadline:
            if policy is None or access_error is not None:
                raise _wait_error(path, access_error)
            wait_attempt += 1
            await policy[1](wait_attempt, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 10.0)
            deadline = time.monotonic() + wait_timeout
            continue
        await asyncio.sleep(0.1)
    state = _owned.set({**ownership, project_key: (owner_id, 1)})
    try:
        yield
    finally:
        _owned.reset(state)
        _release(path, lock_token)


@contextmanager
def project_write_lock_sync(root: str | Path, *, timeout: float = _WAIT_SECONDS) -> Iterator[None]:
    project_key, path = _key(root)
    ownership = _owned.get()
    owner_id = _owner_id()
    if project_key in ownership and ownership[project_key][0] == owner_id:
        nested = dict(ownership)
        nested[project_key] = (owner_id, nested[project_key][1] + 1)
        state = _owned.set(nested)
        try:
            yield
        finally:
            _owned.reset(state)
        return

    lock_token = uuid4().hex
    deadline = time.monotonic() + timeout
    while True:
        access_error = None
        try:
            if _try_acquire(path, lock_token):
                break
        except PermissionError as exc:
            access_error = exc
        if time.monotonic() >= deadline:
            raise _wait_error(path, access_error)
        time.sleep(0.1)
    state = _owned.set({**ownership, project_key: (owner_id, 1)})
    try:
        yield
    finally:
        _owned.reset(state)
        _release(path, lock_token)


def project_mutation_locked(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    async def wrapper(self: Any, root: str | Path, *args: Any, **kwargs: Any) -> Any:
        async with project_write_lock(root):
            return await function(self, root, *args, **kwargs)

    return wrapper


def chapter_operation_locked(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    async def wrapper(self: Any, root: str | Path, chapter_no: int, *args: Any, **kwargs: Any) -> Any:
        async with chapter_operation_lock(root, chapter_no):
            return await function(self, root, chapter_no, *args, **kwargs)

    return wrapper


def project_mutation_locked_sync(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapper(self: Any, root: str | Path, *args: Any, **kwargs: Any) -> Any:
        with project_write_lock_sync(root):
            return function(self, root, *args, **kwargs)

    return wrapper
