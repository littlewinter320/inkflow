"""Cross-entry project mutation lock used by desktop, MCP, and batch flows."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import os
from pathlib import Path
import time
import threading
from typing import Any, Callable, Iterator
from uuid import uuid4

from .errors import ProjectError


_owned: ContextVar[dict[str, tuple[str, int]]] = ContextVar("inkflow_owned_project_locks", default={})
_WAIT_SECONDS = 30.0
_STALE_SECONDS = 6 * 60 * 60


def _owner_id() -> str:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return f"task:{id(task)}" if task is not None else f"thread:{threading.get_ident()}"


def _key(root: str | Path) -> tuple[str, Path]:
    resolved = Path(root).expanduser().resolve()
    return os.path.normcase(str(resolved)), resolved / ".inkflow" / "project.write.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _is_stale(path: Path) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0))
        return not _pid_alive(pid) or age > _STALE_SECONDS
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        try:
            return time.time() - path.stat().st_mtime > _STALE_SECONDS
        except OSError:
            return False


def _try_acquire(path: Path, lock_token: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"pid": os.getpid(), "token": lock_token, "created_at": time.time()},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _is_stale(path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return False
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    return True


def _release(path: Path, lock_token: str) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("token") == lock_token:
            path.unlink()
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError):
        # Never remove a lock whose ownership cannot be proven.
        return


@asynccontextmanager
async def project_write_lock(root: str | Path, *, timeout: float = _WAIT_SECONDS):
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
    while not _try_acquire(path, lock_token):
        if time.monotonic() >= deadline:
            raise ProjectError("当前项目正在执行另一项写作、审查或正史操作，请等待它完成后重试。")
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
    while not _try_acquire(path, lock_token):
        if time.monotonic() >= deadline:
            raise ProjectError("当前项目正在执行另一项写作、审查或正史操作，请等待它完成后重试。")
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


def project_mutation_locked_sync(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapper(self: Any, root: str | Path, *args: Any, **kwargs: Any) -> Any:
        with project_write_lock_sync(root):
            return function(self, root, *args, **kwargs)

    return wrapper
