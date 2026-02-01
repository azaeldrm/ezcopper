"""
Playwright browser lifecycle management with persistent profile support.
"""

import asyncio
import os
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from app.events import event_broker, EventType, BotState


class BrowserManager:
    """Manages Playwright browser lifecycle with persistent profile."""

    # Paths
    PROFILE_DIR = Path("/data/profile")
    ARTIFACTS_DIR = Path("/data/artifacts")
    STATE_FILE = Path("/data/state.json")

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._discord_pages: dict[str, Page] = {}  # channel_url -> Page
        self._amazon_page: Optional[Page] = None
        self._is_running = False

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def context(self) -> Optional[BrowserContext]:
        return self._context

    @property
    def discord_pages(self) -> dict[str, Page]:
        return self._discord_pages

    @property
    def amazon_page(self) -> Optional[Page]:
        return self._amazon_page

    async def initialize(self) -> None:
        """Initialize Playwright and browser with persistent context."""
        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "browser_init",
                details={"message": "Initializing Playwright browser"}
            )
        )

        # Ensure directories exist
        self.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()

        # Launch browser with persistent context
        # Basic fingerprint reduction to appear as normal Chrome
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.PROFILE_DIR),
            headless=False,  # Need headed mode for noVNC viewing
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--disable-gpu",
                "--window-size=1920,1080",
                "--start-maximized",
                # Basic fingerprint reduction
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
            accept_downloads=True,
        )

        # Remove navigator.webdriver flag
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        self._is_running = True

        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "browser_ready",
                details={"message": "Browser initialized successfully"}
            )
        )

    async def get_or_create_discord_page(self, channel_url: str = None) -> Page:
        """Get existing Discord page for a channel or create a new one."""
        # Legacy single-page mode (for bootstrap)
        if channel_url is None:
            if not self._discord_pages:
                pages = self._context.pages
                if pages:
                    page = pages[0]
                else:
                    page = await self._context.new_page()
                self._discord_pages["_default"] = page
            return self._discord_pages.get("_default") or list(self._discord_pages.values())[0]

        # Multi-channel mode
        if channel_url not in self._discord_pages or self._discord_pages[channel_url].is_closed():
            # Reuse first available page if this is the first channel
            if not self._discord_pages:
                pages = self._context.pages
                if pages:
                    self._discord_pages[channel_url] = pages[0]
                else:
                    self._discord_pages[channel_url] = await self._context.new_page()
            else:
                self._discord_pages[channel_url] = await self._context.new_page()
        return self._discord_pages[channel_url]

    async def get_or_create_amazon_page(self) -> Page:
        """Get existing Amazon page or create a new one."""
        if self._amazon_page is None or self._amazon_page.is_closed():
            self._amazon_page = await self._context.new_page()
        return self._amazon_page

    async def close_amazon_page(self) -> None:
        """Close the Amazon page if it exists."""
        if self._amazon_page and not self._amazon_page.is_closed():
            await self._amazon_page.close()
            self._amazon_page = None

    async def take_screenshot(self, stage: str) -> str:
        """Take a screenshot and save to artifacts directory."""
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{stage}.png"
        filepath = self.ARTIFACTS_DIR / filename

        # Try to screenshot the active page
        page = self._amazon_page
        if not page:
            # Get first discord page if no amazon page
            for p in self._discord_pages.values():
                if not p.is_closed():
                    page = p
                    break
        if page and not page.is_closed():
            await page.screenshot(path=str(filepath), full_page=False)

            await event_broker.publish(
                event_broker.create_event(
                    EventType.SCREENSHOT,
                    "screenshot_saved",
                    details={"path": str(filepath), "stage": stage}
                )
            )
            return str(filepath)
        return ""

    async def save_trace(self, stage: str) -> str:
        """Save Playwright trace to artifacts directory."""
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{stage}.zip"
        filepath = self.ARTIFACTS_DIR / filename

        if self._context:
            try:
                await self._context.tracing.stop(path=str(filepath))
                return str(filepath)
            except Exception:
                pass
        return ""

    async def start_tracing(self) -> None:
        """Start tracing for debugging."""
        if self._context:
            try:
                await self._context.tracing.start(
                    screenshots=True,
                    snapshots=True,
                    sources=True
                )
            except Exception:
                pass  # Tracing might already be started

    async def stop_tracing(self, stage: str) -> str:
        """Stop tracing and save."""
        return await self.save_trace(stage)

    async def shutdown(self) -> None:
        """Gracefully shutdown browser and Playwright."""
        self._is_running = False

        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "browser_shutdown",
                details={"message": "Shutting down browser"}
            )
        )

        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        self._discord_pages = {}
        self._amazon_page = None


# Global browser manager instance
browser_manager = BrowserManager()
