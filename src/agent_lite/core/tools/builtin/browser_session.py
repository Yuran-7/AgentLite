from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright


log = logging.getLogger(__name__)


@dataclass
class BrowserSessionState:
    playwright: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    validated_origins: set[tuple[str, str, int]] = field(default_factory=set)
    known_refs: set[str] = field(default_factory=set)
    waiting_for_user: bool = False
    user_controlled: bool = False
    headless: bool | None = None
    last_used: float = field(default_factory=time.monotonic)


async def close_browser_state(state: BrowserSessionState) -> None:
    state.known_refs.clear()
    state.validated_origins.clear()
    page, context, browser, playwright = (
        state.page,
        state.context,
        state.browser,
        state.playwright,
    )
    state.page = None
    state.context = None
    state.browser = None
    state.playwright = None
    if page is not None and not page.is_closed():
        with contextlib.suppress(Exception):
            await page.close()
    if context is not None:
        with contextlib.suppress(Exception):
            await context.close()
    if browser is not None:
        with contextlib.suppress(Exception):
            await browser.close()
    if playwright is not None:
        with contextlib.suppress(Exception):
            await playwright.stop()


class BrowserSessionManager:
    """Own browser runtimes that may survive one agent run."""

    def __init__(self, idle_timeout_s: float = 600.0) -> None:
        self._idle_timeout_s = idle_timeout_s
        self._states: dict[str, BrowserSessionState] = {}
        self._reaper: asyncio.Task[None] | None = None

    def state_for(self, session_id: str) -> BrowserSessionState:
        state = self._states.get(session_id)
        if state is None:
            state = BrowserSessionState()
            self._states[session_id] = state
        self.touch(state)
        return state

    @staticmethod
    def touch(state: BrowserSessionState) -> None:
        state.last_used = time.monotonic()

    def retain_for_user(self, state: BrowserSessionState) -> None:
        state.waiting_for_user = True
        state.user_controlled = True
        self.touch(state)

    def clear_waiting(self, state: BrowserSessionState) -> None:
        state.waiting_for_user = False
        state.user_controlled = False
        self.touch(state)

    async def release(self, session_id: str) -> None:
        state = self._states.get(session_id)
        if state is None or state.waiting_for_user:
            return
        self._states.pop(session_id, None)
        await close_browser_state(state)

    async def close_session(self, session_id: str) -> None:
        state = self._states.pop(session_id, None)
        if state is not None:
            await close_browser_state(state)

    async def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(
                self._reap_loop(), name="browser-session-reaper"
            )

    async def stop(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        states = list(self._states.values())
        self._states.clear()
        await asyncio.gather(
            *(close_browser_state(state) for state in states),
            return_exceptions=True,
        )

    async def _reap_loop(self) -> None:
        interval = max(5.0, min(30.0, self._idle_timeout_s / 2))
        while True:
            await asyncio.sleep(interval)
            cutoff = time.monotonic() - self._idle_timeout_s
            expired = [
                session_id
                for session_id, state in self._states.items()
                if state.waiting_for_user and state.last_used <= cutoff
            ]
            for session_id in expired:
                log.info("closing idle browser login session session_id=%s", session_id)
                await self.close_session(session_id)
