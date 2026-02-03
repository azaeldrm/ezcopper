"""
Persistent storage for detected items/activity feed.
"""

import json
import os
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime, timezone
import threading

ACTIVITY_FILE = Path("/data/activity.json")
MAX_ITEMS = int(os.getenv("MAX_ACTIVITY_ITEMS", "100"))

_lock = threading.Lock()


def load_activity() -> List[Dict[str, Any]]:
    """Load activity history from file."""
    if ACTIVITY_FILE.exists():
        try:
            with open(ACTIVITY_FILE, "r") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            pass
    return []


def save_activity(items: List[Dict[str, Any]]) -> None:
    """Save activity history to file."""
    ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ACTIVITY_FILE, "w") as f:
        json.dump(items[-MAX_ITEMS:], f, indent=2)


def add_activity_item(item: Dict[str, Any]) -> None:
    """Add a new item to the activity history."""
    with _lock:
        items = load_activity()
        items.append(item)
        # Keep only last MAX_ITEMS
        save_activity(items[-MAX_ITEMS:])


def create_activity_item(
    product: str,
    price: float,
    discount: float,
    savings: float,
    amazon_urls: List[str],
    triggered: bool,
    matched_rule: Dict[str, Any] = None,
    message_id: str = "",
    channel: str = ""
) -> Dict[str, Any]:
    """Create a standardized activity item."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "product": product,
        "price": price,
        "discount": discount,
        "savings": savings,
        "amazon_urls": amazon_urls,
        "triggered": triggered,
        "matched_rule": matched_rule,
        "message_id": message_id,
        "channel": channel
    }
