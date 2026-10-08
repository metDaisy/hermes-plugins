"""Request-owned execution leases for Main/Auxiliary role transitions."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import anyio


class ExecutionLeaseCoordinator:
    """Own queue accounting, role serialization, cancellation, and Main restoration."""

    def __init__(
        self,
        *,
        ensure_role: Callable[[str], Awaitable[None]],
        role_configured: Callable[[str], bool],
        abort_transition: Callable[[str, BaseException], Awaitable[None]],
        publish_counts: Callable[[int, int], Awaitable[None]],
        state_changed: Callable[[int, int], None] | None = None,
        refresh_state: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self.ensure_role = ensure_role
        self.role_configured = role_configured
        self.abort_transition = abort_transition
        self.publish_counts = publish_counts
        self.state_changed = state_changed or (lambda _queued, _active: None)
        self.refresh_state = refresh_state
        self.transition_lock = asyncio.Lock()
        self.counter_lock = asyncio.Lock()
        self.main_idle_event = asyncio.Event()
        self.main_idle_event.set()
        self.queued_main_requests = 0
        self.active_main_requests = 0

    def _refresh(self) -> None:
        if self.refresh_state is not None:
            self.queued_main_requests, self.active_main_requests = self.refresh_state()

    async def _publish(self) -> None:
        self.state_changed(self.queued_main_requests, self.active_main_requests)
        await self.publish_counts(self.queued_main_requests, self.active_main_requests)

    async def begin(self, role: str | None) -> Callable[[], Awaitable[None]] | None:
        self._refresh()
        if role is None:
            return None
        if role == "main":
            return await self._begin_main()
        return await self._begin_compression()

    async def _begin_main(self) -> Callable[[], Awaitable[None]]:
        queued = active = acquired = False
        try:
            async with self.counter_lock:
                self.queued_main_requests += 1
                queued = True
                await self._publish()
            await self.transition_lock.acquire()
            acquired = True
            await self.ensure_role("main")
            async with self.counter_lock:
                self.queued_main_requests -= 1
                queued = False
                self.active_main_requests += 1
                self.main_idle_event.clear()
                active = True
                await self._publish()
        except BaseException:
            async with self.counter_lock:
                changed = False
                if queued:
                    self.queued_main_requests = max(0, self.queued_main_requests - 1)
                    changed = True
                if active:
                    self.active_main_requests = max(0, self.active_main_requests - 1)
                    if self.active_main_requests == 0:
                        self.main_idle_event.set()
                    changed = True
                if changed:
                    try:
                        with anyio.CancelScope(shield=True):
                            await self._publish()
                    except BaseException:  # noqa: BLE001
                        pass
            if acquired:
                self.transition_lock.release()
            raise
        self.transition_lock.release()

        async def finish_main() -> None:
            async with self.counter_lock:
                self.active_main_requests = max(0, self.active_main_requests - 1)
                if self.active_main_requests == 0:
                    self.main_idle_event.set()
                await self._publish()

        return finish_main

    async def _begin_compression(self) -> Callable[[], Awaitable[None]]:
        restore_main = self.role_configured("main")
        await self.transition_lock.acquire()
        try:
            while True:
                async with self.counter_lock:
                    if self.active_main_requests == 0:
                        break
                await self.main_idle_event.wait()
                self._refresh()
            await self.ensure_role("compression")
        except BaseException as original_error:
            try:
                if restore_main:
                    await self.ensure_role("main")
            except asyncio.CancelledError:
                pass
            except BaseException as restore_error:
                await self.abort_transition("compression", restore_error)
                original_error.add_note(f"Main restoration also failed: {restore_error}")
            finally:
                self.transition_lock.release()
            raise

        async def finish_compression() -> None:
            try:
                if restore_main:
                    try:
                        await self.ensure_role("main")
                    except asyncio.CancelledError:
                        raise
                    except BaseException as restore_error:
                        await self.abort_transition("compression", restore_error)
                        raise
            finally:
                self.transition_lock.release()

        return finish_compression
