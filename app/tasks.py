"""Task lifecycle helpers shared by the service's components."""

from __future__ import annotations

import asyncio
from typing import Any


async def cancel_and_wait(task: asyncio.Task[Any] | None) -> None:
    """Cancel ``task`` and wait for it to end.

    The task's own cancellation, or whatever it unwinds with, is expected and swallowed.
    A cancellation aimed at our caller while it waits reaches the caller, as a
    CancelledError even when the task unwound with something else.
    """
    if task is None:
        return
    # cancelling() counts every cancel ever requested of the caller, including ones it has
    # already handled, so only a rise from here on is aimed at this wait (the idiom
    # asyncio.timeout uses, as does HAConnection._stop_locked).
    caller = asyncio.current_task()
    cancelling_at_entry = caller.cancelling() if caller is not None else 0
    task.cancel()
    try:
        await task
    except BaseException as exc:
        if caller is not None and caller.cancelling() > cancelling_at_entry:
            raise asyncio.CancelledError() from exc
