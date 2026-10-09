"""Manual offline check: competing sync writes must not stall an async app."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import json
from unittest.mock import patch

from inkflow.errors import ProjectBusyError
from inkflow.project_lock import project_write_lock, project_write_lock_sync, project_lock_wait_policy


async def check(root: Path) -> None:
    ticks = []
    async def competing_sync_write() -> float:
        started = time.monotonic()
        try:
            with project_write_lock_sync(root, timeout=1.0):
                raise AssertionError("A child task must not inherit another task's write ownership.")
        except ProjectBusyError:
            return time.monotonic() - started

    async def independent_read() -> None:
        await asyncio.sleep(0)
        ticks.append("read remains schedulable")

    async with project_write_lock(root):
        # Same-task local writes remain reentrant; competing child tasks do not.
        with project_write_lock_sync(root):
            assert (root / ".inkflow/project.write.lock").is_file()
        wait, _ = await asyncio.gather(competing_sync_write(), independent_read())
        assert wait < 0.25, "Synchronous contention blocked the event loop."
        assert ticks
    assert not (root / ".inkflow/project.write.lock").exists()
    # The busy result did not remove the original owner's lock or poison reuse.
    async with project_write_lock(root):
        assert (root / ".inkflow/project.write.lock").is_file()
    lock_path = root / ".inkflow/project.write.lock"
    lock_path.write_text(json.dumps({"pid": 99999999, "token": "dead-owner"}), encoding="utf-8")
    with patch("inkflow.project_lock._pid_alive", return_value=False):
        with project_write_lock_sync(root):
            assert "dead-owner" not in lock_path.read_text(encoding="utf-8")
    waits = []
    async def on_wait(attempt, delay):
        waits.append(attempt)
    started = time.monotonic()
    with patch("inkflow.project_lock._try_acquire", return_value=False), project_lock_wait_policy(on_wait=on_wait, probe_timeout=0.05):
        try:
            async with project_write_lock(root, timeout=0.3):
                raise AssertionError("Occupied writes must stop at their total deadline.")
        except ProjectBusyError:
            pass
    assert waits and time.monotonic() - started < 0.6


if __name__ == "__main__":
    with TemporaryDirectory(dir="D:/墨流/cache") as directory:
        asyncio.run(check(Path(directory)))
    print("Async contention and owner isolation check passed.")
