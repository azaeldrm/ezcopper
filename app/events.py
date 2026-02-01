"""
SSE Event Broker for broadcasting events to connected clients.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, Any, List
from dataclasses import dataclass, field, asdict
from enum import Enum


class EventType(str, Enum):
    STEP = "step"
    MESSAGE = "message"
    URL_DETECTED = "url_detected"
    STATE_CHANGE = "state_change"
    ERROR = "error"
    ACTION_REQUIRED = "action_required"
    ORDER_PENDING = "order_pending"
    ORDER_PLACED = "order_placed"
    SCREENSHOT = "screenshot"


class BotState(str, Enum):
    IDLE = "idle"
    BOOTSTRAP = "bootstrap"
    DISCORD_MONITORING = "discord_monitoring"
    AMAZON_OPENING = "amazon_opening"
    AMAZON_ADD_TO_CART = "amazon_add_to_cart"
    AMAZON_PROCEED_CHECKOUT = "amazon_proceed_checkout"
    AMAZON_PLACE_ORDER_PENDING = "amazon_place_order_pending"
    AMAZON_ORDER_PLACED = "amazon_order_placed"
    ERROR = "error"
    PAUSED = "paused"


@dataclass
class Event:
    ts: str
    type: EventType
    step: str
    url: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        data = asdict(self)
        data["type"] = self.type.value
        return json.dumps(data)

    def to_log_line(self) -> str:
        return self.to_json()


class EventBroker:
    """Manages SSE subscriptions and event broadcasting."""

    def __init__(self, max_history: int = 100):
        self._subscribers: List[asyncio.Queue] = []
        self._history: List[Event] = []
        self._max_history = max_history
        self._lock = asyncio.Lock()

        # Current state tracking
        self._current_state: BotState = BotState.IDLE
        self._last_message: Dict[str, Any] = {}
        self._last_action: Dict[str, Any] = {}
        self._current_urls: List[str] = []
        self._start_time: datetime = datetime.now(timezone.utc)

    @property
    def current_state(self) -> BotState:
        return self._current_state

    @current_state.setter
    def current_state(self, value: BotState):
        self._current_state = value

    @property
    def last_message(self) -> Dict[str, Any]:
        return self._last_message

    @last_message.setter
    def last_message(self, value: Dict[str, Any]):
        self._last_message = value

    @property
    def last_action(self) -> Dict[str, Any]:
        return self._last_action

    @last_action.setter
    def last_action(self, value: Dict[str, Any]):
        self._last_action = value

    @property
    def current_urls(self) -> List[str]:
        return self._current_urls

    @current_urls.setter
    def current_urls(self, value: List[str]):
        self._current_urls = value

    @property
    def uptime_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self._start_time).total_seconds()

    def create_event(
        self,
        event_type: EventType,
        step: str,
        url: str = "",
        details: Dict[str, Any] = None
    ) -> Event:
        return Event(
            ts=datetime.now(timezone.utc).isoformat(),
            type=event_type,
            step=step,
            url=url,
            details=details or {}
        )

    async def publish(self, event: Event) -> None:
        """Publish an event to all subscribers and log it."""
        # Log to stdout as structured JSON
        print(event.to_log_line(), flush=True)

        async with self._lock:
            # Add to history
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

            # Broadcast to all subscribers
            dead_subscribers = []
            for queue in self._subscribers:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    dead_subscribers.append(queue)

            # Remove dead subscribers
            for queue in dead_subscribers:
                self._subscribers.remove(queue)

    async def subscribe(self) -> AsyncGenerator[Event, None]:
        """Subscribe to events. Returns an async generator."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)

        async with self._lock:
            self._subscribers.append(queue)

        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            async with self._lock:
                if queue in self._subscribers:
                    self._subscribers.remove(queue)

    async def get_history(self, limit: int = 50) -> List[Event]:
        """Get recent event history."""
        async with self._lock:
            return self._history[-limit:]

    def get_status(self) -> Dict[str, Any]:
        """Get current status for /status endpoint."""
        return {
            "state": self._current_state.value,
            "last_message": self._last_message,
            "last_action": self._last_action,
            "current_urls": self._current_urls,
            "uptime_seconds": self.uptime_seconds,
            "subscriber_count": len(self._subscribers)
        }


# Global event broker instance
event_broker = EventBroker()
