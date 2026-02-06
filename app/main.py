"""
Main application: FastAPI server + orchestrator for Discord monitoring and Amazon purchases.
"""

import asyncio
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Optional
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.events import event_broker, EventType, BotState
from app.browser import browser_manager
from app.discord_watcher import DiscordWatcher
from app.amazon_flow import AmazonWorker, AmazonFlow
from app.rules_ui import rules_app


# Configuration from environment
MODE = os.getenv("MODE", "bootstrap")
# Support both DISCORD_CHANNEL_URLS (new, comma-separated) and DISCORD_CHANNEL_URL (legacy, single)
_channel_urls_str = os.getenv("DISCORD_CHANNEL_URLS", "") or os.getenv("DISCORD_CHANNEL_URL", "")
DISCORD_CHANNEL_URLS = [url.strip() for url in _channel_urls_str.split(",") if url.strip()]

# Whitelist and Blacklist channel URLs
WHITELIST_CHANNEL_URLS = [url.strip() for url in os.getenv("WHITELIST_CHANNEL_URLS", "").split(",") if url.strip()]
BLACKLIST_CHANNEL_URLS = [url.strip() for url in os.getenv("BLACKLIST_CHANNEL_URLS", "").split(",") if url.strip()]

# Backward compatibility: treat legacy DISCORD_CHANNEL_URLS as blacklist if no explicit blacklist configured
if DISCORD_CHANNEL_URLS and not BLACKLIST_CHANNEL_URLS and not WHITELIST_CHANNEL_URLS:
    BLACKLIST_CHANNEL_URLS = DISCORD_CHANNEL_URLS

# Create channel configuration map
CHANNEL_CONFIG = {}
for url in WHITELIST_CHANNEL_URLS:
    CHANNEL_CONFIG[url] = "whitelist"
for url in BLACKLIST_CHANNEL_URLS:
    CHANNEL_CONFIG[url] = "blacklist"

# All monitored channels
ALL_CHANNEL_URLS = WHITELIST_CHANNEL_URLS + BLACKLIST_CHANNEL_URLS

KEYWORDS = [k.strip() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]
URL_REGEX = os.getenv("URL_REGEX", "")
CONFIRM_FINAL_ORDER = os.getenv("CONFIRM_FINAL_ORDER", "true").lower() == "true"
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "5.0"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Global instances
url_queue: asyncio.Queue = asyncio.Queue()
discord_watcher: Optional[DiscordWatcher] = None
amazon_worker: Optional[AmazonWorker] = None
shutdown_event: asyncio.Event = asyncio.Event()


class TriggerRequest(BaseModel):
    """Request model for manual URL trigger."""
    url: str
    price: Optional[float] = None
    message_id: Optional[str] = None
    product: Optional[str] = None


class HealthResponse(BaseModel):
    """Response model for health check."""
    status: str
    mode: str
    timestamp: str


class StatusResponse(BaseModel):
    """Response model for status endpoint."""
    state: str
    last_message: dict
    last_action: dict
    current_urls: list
    uptime_seconds: float
    mode: str
    discord_channel_urls: list
    confirm_final_order: bool


async def run_bootstrap_mode():
    """
    Bootstrap mode: Launch browser for operator to log into Discord and Amazon.
    """
    await event_broker.publish(
        event_broker.create_event(
            EventType.STATE_CHANGE,
            "bootstrap_mode_start",
            details={
                "message": "Bootstrap mode - please log in to Discord and Amazon via noVNC",
                "novnc_url": "http://localhost:6080"
            }
        )
    )

    event_broker.current_state = BotState.BOOTSTRAP

    # Initialize browser
    await browser_manager.initialize()

    # Open Discord
    discord_page = await browser_manager.get_or_create_discord_page()
    await discord_page.goto("https://discord.com/login", wait_until="networkidle")

    await event_broker.publish(
        event_broker.create_event(
            EventType.STEP,
            "bootstrap_discord",
            url="https://discord.com/login",
            details={"message": "Please log in to Discord"}
        )
    )

    # Open Amazon in another tab
    amazon_page = await browser_manager.get_or_create_amazon_page()
    await amazon_page.goto("https://www.amazon.com/ap/signin", wait_until="networkidle")

    await event_broker.publish(
        event_broker.create_event(
            EventType.STEP,
            "bootstrap_amazon",
            url="https://www.amazon.com/ap/signin",
            details={"message": "Please log in to Amazon"}
        )
    )

    await event_broker.publish(
        event_broker.create_event(
            EventType.ACTION_REQUIRED,
            "bootstrap_login_required",
            details={
                "message": "Log in to both Discord and Amazon, then stop the container.",
                "discord_tab": "Tab 1",
                "amazon_tab": "Tab 2",
                "novnc_url": "http://localhost:6080"
            }
        )
    )

    # Keep running until shutdown
    while not shutdown_event.is_set():
        await asyncio.sleep(1)


async def run_normal_mode():
    """
    Normal run mode: Monitor Discord and process Amazon purchases.
    """
    global discord_watcher, amazon_worker

    if not ALL_CHANNEL_URLS:
        await event_broker.publish(
            event_broker.create_event(
                EventType.ERROR,
                "config_error",
                details={"message": "No channel URLs configured. Set WHITELIST_CHANNEL_URLS or BLACKLIST_CHANNEL_URLS"}
            )
        )
        return

    await event_broker.publish(
        event_broker.create_event(
            EventType.STATE_CHANGE,
            "run_mode_start",
            details={
                "all_channel_urls": ALL_CHANNEL_URLS,
                "whitelist_channels": WHITELIST_CHANNEL_URLS,
                "blacklist_channels": BLACKLIST_CHANNEL_URLS,
                "total_channels": len(ALL_CHANNEL_URLS),
                "keywords": KEYWORDS,
                "url_regex": URL_REGEX,
                "confirm_final_order": CONFIRM_FINAL_ORDER
            }
        )
    )

    # Initialize browser
    await browser_manager.initialize()

    # Create Discord watcher (supports multiple channels)
    discord_watcher = DiscordWatcher(
        channel_urls=ALL_CHANNEL_URLS,
        channel_config=CHANNEL_CONFIG,
        keywords=KEYWORDS if KEYWORDS else None,
        url_regex=URL_REGEX if URL_REGEX else None,
        poll_interval=POLL_INTERVAL_SECONDS,
        dry_run=DRY_RUN
    )
    discord_watcher.set_url_queue(url_queue)

    # Create Amazon worker
    amazon_worker = AmazonWorker(
        url_queue=url_queue,
        confirm_final_order=CONFIRM_FINAL_ORDER
    )

    # Start both tasks concurrently
    await asyncio.gather(
        discord_watcher.start_watching(),
        amazon_worker.start(),
        return_exceptions=True
    )


async def startup():
    """Application startup."""
    await event_broker.publish(
        event_broker.create_event(
            EventType.STEP,
            "application_startup",
            details={
                "mode": MODE,
                "discord_channel_urls": DISCORD_CHANNEL_URLS,
                "channel_count": len(DISCORD_CHANNEL_URLS),
                "keywords": KEYWORDS,
                "confirm_final_order": CONFIRM_FINAL_ORDER
            }
        )
    )

    # Start the appropriate mode as a background task
    if MODE == "bootstrap":
        asyncio.create_task(run_bootstrap_mode())
    else:
        asyncio.create_task(run_normal_mode())


async def shutdown():
    """Graceful shutdown."""
    await event_broker.publish(
        event_broker.create_event(
            EventType.STEP,
            "application_shutdown",
            details={"message": "Graceful shutdown initiated"}
        )
    )

    shutdown_event.set()

    # Stop workers
    if discord_watcher:
        discord_watcher.stop()
    if amazon_worker:
        amazon_worker.stop()

    # Close browser
    await browser_manager.shutdown()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager."""
    await startup()
    yield
    await shutdown()


# Create FastAPI app
app = FastAPI(
    title="EZCopper",
    description="Automated Amazon checkout bot that monitors Discord channels for product drops",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware to allow UI on port 8001 to connect to SSE on port 8000
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    return HealthResponse(
        status="healthy" if browser_manager.is_running else "initializing",
        mode=MODE,
        timestamp=datetime.now(timezone.utc).isoformat()
    )


@app.get("/status", response_model=StatusResponse)
async def get_status():
    """Get current bot status."""
    status = event_broker.get_status()
    return StatusResponse(
        state=status["state"],
        last_message=status["last_message"],
        last_action=status["last_action"],
        current_urls=status["current_urls"],
        uptime_seconds=status["uptime_seconds"],
        mode=MODE,
        discord_channel_urls=DISCORD_CHANNEL_URLS,
        confirm_final_order=CONFIRM_FINAL_ORDER
    )


@app.get("/events")
async def events_stream():
    """SSE stream of structured JSON events."""
    async def event_generator():
        async for event in event_broker.subscribe():
            yield {
                "event": event.type.value,
                "data": event.to_json()
            }

    return EventSourceResponse(event_generator())


@app.post("/actions/trigger")
async def trigger_url(request: TriggerRequest, background_tasks: BackgroundTasks):
    """Manually trigger an Amazon URL for testing."""
    if MODE == "bootstrap":
        raise HTTPException(status_code=400, detail="Cannot trigger in bootstrap mode")

    if not browser_manager.is_running:
        raise HTTPException(status_code=503, detail="Browser not initialized")

    # Generate message_id if not provided
    message_id = request.message_id or f"manual-trigger-{datetime.now(timezone.utc).timestamp()}"

    # Queue the URL for processing
    await url_queue.put({
        "url": request.url,
        "message": {
            "source": "manual_trigger",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message_id": message_id,
            "product": request.product or "Manual trigger"
        },
        "parsed": {
            "price": request.price
        }
    })

    await event_broker.publish(
        event_broker.create_event(
            EventType.STEP,
            "manual_trigger",
            url=request.url,
            details={
                "message": "URL queued for manual testing",
                "message_id": message_id,
                "product": request.product,
                "price": request.price
            }
        )
    )

    return JSONResponse(
        content={"status": "queued", "url": request.url},
        status_code=202
    )


@app.post("/actions/pause")
async def pause_worker():
    """Pause the Amazon worker."""
    if amazon_worker:
        amazon_worker.pause()
        await event_broker.publish(
            event_broker.create_event(
                EventType.STATE_CHANGE,
                "worker_paused",
                details={"message": "Amazon worker paused"}
            )
        )
        return {"status": "paused"}
    raise HTTPException(status_code=400, detail="Worker not initialized")


@app.post("/actions/resume")
async def resume_worker():
    """Resume the Amazon worker."""
    if amazon_worker:
        amazon_worker.resume()
        event_broker.current_state = BotState.DISCORD_MONITORING
        await event_broker.publish(
            event_broker.create_event(
                EventType.STATE_CHANGE,
                "worker_resumed",
                details={"message": "Amazon worker resumed"}
            )
        )
        return {"status": "resumed"}
    raise HTTPException(status_code=400, detail="Worker not initialized")


@app.get("/history")
async def get_event_history(limit: int = 50):
    """Get recent event history."""
    events = await event_broker.get_history(limit)
    return [
        {
            "ts": e.ts,
            "type": e.type.value,
            "step": e.step,
            "url": e.url,
            "details": e.details
        }
        for e in events
    ]


def handle_signal(signum, frame):
    """Handle shutdown signals."""
    print(f"Received signal {signum}, initiating shutdown...")
    shutdown_event.set()


async def run_servers():
    """Run both the main API server and the rules UI server."""
    config_main = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        access_log=True
    )
    config_rules = uvicorn.Config(
        rules_app,
        host="0.0.0.0",
        port=8001,
        log_level="info",
        access_log=True
    )

    server_main = uvicorn.Server(config_main)
    server_rules = uvicorn.Server(config_rules)

    await asyncio.gather(
        server_main.serve(),
        server_rules.serve()
    )


if __name__ == "__main__":
    # Setup signal handlers
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Run both servers
    asyncio.run(run_servers())
