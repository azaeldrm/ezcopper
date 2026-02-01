"""
Discord channel watcher that monitors for new messages containing Amazon URLs.
"""

import asyncio
import json
import re
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Set
from dataclasses import dataclass, asdict

from playwright.async_api import Page, Locator

from app.events import event_broker, EventType, BotState
from app.browser import browser_manager
from app.message_parser import MessageParser, ParsedMessage
from app.rules_ui import load_rules, Rule
from app.activity_store import add_activity_item, create_activity_item


@dataclass
class DiscordMessage:
    """Represents a Discord message."""
    message_id: str
    timestamp: str
    author: str
    text: str
    urls: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DiscordWatcher:
    """Watches one or more Discord channels for new messages via the web UI."""

    # Selectors for Discord web UI (these may need updates as Discord changes)
    SELECTORS = {
        # Message list container
        "message_list": "[class*='scrollerInner']",
        # Individual message containers
        "message_item": "[id^='chat-messages-']",
        # Message content
        "message_content": "[class*='messageContent']",
        # Message timestamp
        "message_timestamp": "time",
        # Message author
        "message_author": "[class*='username']",
        # Links in messages
        "message_links": "a[href]",
        # Channel name header
        "channel_name": "[class*='title-'] h1, [class*='channelName']",
    }

    # Amazon URL patterns
    AMAZON_URL_PATTERNS = [
        r"https?://(?:www\.)?amazon\.com[^\s]*",
        r"https?://(?:www\.)?amzn\.to[^\s]*",
        r"https?://(?:www\.)?amazon\.co\.[a-z]{2}[^\s]*",
        r"https?://a\.co[^\s]*",
    ]

    STATE_FILE = Path("/data/state.json")

    def __init__(
        self,
        channel_urls: List[str],
        channel_config: Dict[str, str] = None,
        keywords: Optional[List[str]] = None,
        url_regex: Optional[str] = None,
        poll_interval: float = 5.0,
        dry_run: bool = False
    ):
        # Support both single URL (legacy) and multiple URLs
        if isinstance(channel_urls, str):
            channel_urls = [channel_urls]
        self.channel_urls = [url.strip() for url in channel_urls if url.strip()]
        self.channel_config = channel_config or {}  # channel_url -> "whitelist" or "blacklist"
        self.keywords = keywords or []
        self.url_regex = re.compile(url_regex) if url_regex else None
        self.poll_interval = poll_interval
        self.dry_run = dry_run
        self._amazon_url_pattern = re.compile("|".join(self.AMAZON_URL_PATTERNS), re.IGNORECASE)
        self._seen_message_ids: Set[str] = set()
        self._last_seen_id: Optional[str] = None
        self._is_running = False
        self._url_queue: Optional[asyncio.Queue] = None
        self._channel_names: Dict[str, str] = {}  # channel_url -> friendly name

    def set_url_queue(self, queue: asyncio.Queue) -> None:
        """Set the queue for detected Amazon URLs."""
        self._url_queue = queue

    def _extract_channel_id(self, url: str) -> str:
        """Extract channel ID from Discord URL for use as identifier."""
        # URL format: https://discord.com/channels/SERVER_ID/CHANNEL_ID
        parts = url.rstrip('/').split('/')
        if len(parts) >= 2:
            return parts[-1]  # Last part is channel ID
        return url

    async def _get_channel_name(self, page: Page, channel_url: str) -> str:
        """Get the channel name from the page or extract from URL."""
        # Try to get from page header
        try:
            name_elem = page.locator(self.SELECTORS["channel_name"]).first
            name = await name_elem.inner_text(timeout=2000)
            if name:
                return name.strip()
        except Exception:
            pass

        # Fallback: use channel ID
        return f"#{self._extract_channel_id(channel_url)}"

    def get_channel_name(self, channel_url: str) -> str:
        """Get cached channel name or fallback."""
        return self._channel_names.get(channel_url, f"#{self._extract_channel_id(channel_url)}")

    def _load_state(self) -> None:
        """Load persisted state from file."""
        if self.STATE_FILE.exists():
            try:
                with open(self.STATE_FILE, "r") as f:
                    state = json.load(f)
                    self._last_seen_id = state.get("last_seen_id")
                    self._seen_message_ids = set(state.get("seen_message_ids", []))
            except Exception as e:
                print(f"Error loading state: {e}")

    def _save_state(self) -> None:
        """Persist state to file."""
        try:
            state = {
                "last_seen_id": self._last_seen_id,
                "seen_message_ids": list(self._seen_message_ids)[-1000:],  # Keep last 1000
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            with open(self.STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            print(f"Error saving state: {e}")

    def _generate_message_id(self, text: str, author: str) -> str:
        """Generate a unique ID for a message if none available."""
        content = f"{author}:{text}"
        return hashlib.md5(content.encode()).hexdigest()[:16]

    def _extract_amazon_urls(self, text: str, links: List[str]) -> List[str]:
        """Extract Amazon URLs from text and links."""
        amazon_urls = []

        # Check in text content
        matches = self._amazon_url_pattern.findall(text)
        amazon_urls.extend(matches)

        # Check in extracted links
        for link in links:
            if self._amazon_url_pattern.match(link):
                amazon_urls.append(link)

        # Remove duplicates while preserving order
        seen = set()
        unique_urls = []
        for url in amazon_urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)

        return unique_urls

    def _matches_keywords(self, text: str) -> bool:
        """Check if text matches any configured keywords."""
        if not self.keywords:
            return True  # No keywords configured means match all

        text_lower = text.lower()
        for keyword in self.keywords:
            if keyword.lower() in text_lower:
                return True
        return False

    def _matches_url_regex(self, urls: List[str]) -> bool:
        """Check if any URL matches the configured regex."""
        if not self.url_regex:
            return True  # No regex configured means match all

        for url in urls:
            if self.url_regex.search(url):
                return True
        return False

    async def _parse_message_element(self, page: Page, element: Locator) -> Optional[DiscordMessage]:
        """Parse a Discord message element into a DiscordMessage object."""
        try:
            # Get message ID from element
            element_id = await element.get_attribute("id")
            message_id = element_id or ""

            # Get message content
            content_elem = element.locator(self.SELECTORS["message_content"]).first
            text = ""
            try:
                text = await content_elem.inner_text(timeout=1000)
            except Exception:
                pass

            # Get timestamp
            timestamp = ""
            try:
                time_elem = element.locator(self.SELECTORS["message_timestamp"]).first
                timestamp = await time_elem.get_attribute("datetime") or ""
            except Exception:
                timestamp = datetime.now(timezone.utc).isoformat()

            # Get author
            author = ""
            try:
                author_elem = element.locator(self.SELECTORS["message_author"]).first
                author = await author_elem.inner_text(timeout=1000)
            except Exception:
                pass

            # Get all links
            links = []
            try:
                link_elements = element.locator(self.SELECTORS["message_links"])
                count = await link_elements.count()
                for i in range(count):
                    href = await link_elements.nth(i).get_attribute("href")
                    if href:
                        links.append(href)
            except Exception:
                pass

            # Generate ID if not available
            if not message_id:
                message_id = self._generate_message_id(text, author)

            return DiscordMessage(
                message_id=message_id,
                timestamp=timestamp,
                author=author,
                text=text,
                urls=links
            )

        except Exception as e:
            await event_broker.publish(
                event_broker.create_event(
                    EventType.ERROR,
                    "parse_message_error",
                    details={"error": str(e)}
                )
            )
            return None

    def _check_rules(self, parsed: ParsedMessage, channel_url: str = "") -> tuple:
        """
        Check if message matches rules based on channel type.

        Returns:
            (matched_rule, should_trigger) where:
            - matched_rule: The rule that matched (or None)
            - should_trigger: Whether to trigger action
        """
        from app.rules_ui import get_whitelist_rules, get_blacklist_rules

        channel_type = self.channel_config.get(channel_url, "blacklist")

        if channel_type == "whitelist":
            # Whitelist channel: Auto-click everything EXCEPT items matching whitelist rules
            whitelist_rules = get_whitelist_rules()
            for rule in whitelist_rules:
                if parsed.matches_rule(rule.keywords, rule.max_price):
                    # Match found = DON'T trigger (exclusion rule)
                    return (rule, False)
            # No match = DO trigger (auto-click everything else)
            return (None, True)

        else:  # blacklist (default)
            # Blacklist channel: Auto-click ONLY items matching blacklist rules
            blacklist_rules = get_blacklist_rules()
            for rule in blacklist_rules:
                if parsed.matches_rule(rule.keywords, rule.max_price):
                    # Match found = DO trigger (inclusion rule)
                    return (rule, True)
            # No match = DON'T trigger
            return (None, False)

    async def _process_new_message(self, message: DiscordMessage, channel_url: str = "") -> None:
        """Process a new Discord message."""
        channel_name = self.get_channel_name(channel_url) if channel_url else ""

        # Check if already seen
        if message.message_id in self._seen_message_ids:
            return

        # Mark as seen
        self._seen_message_ids.add(message.message_id)
        self._last_seen_id = message.message_id
        self._save_state()

        # Update event broker state
        msg_dict = message.to_dict()
        msg_dict["channel"] = channel_name
        event_broker.last_message = msg_dict

        # Emit message event
        await event_broker.publish(
            event_broker.create_event(
                EventType.MESSAGE,
                "discord_message",
                details=msg_dict
            )
        )

        # Try to parse the message using the Inventory Bot format
        parsed = MessageParser.parse(message.text, message.urls)

        if parsed and parsed.amazon_urls:
            # Check against rules based on channel type
            matched_rule, should_trigger = self._check_rules(parsed, channel_url)
            channel_type = self.channel_config.get(channel_url, "blacklist")

            if should_trigger:
                # Should trigger action
                if self.dry_run:
                    # DRY RUN: Log what would happen without taking action
                    await event_broker.publish(
                        event_broker.create_event(
                            EventType.STEP,
                            "dry_run_would_trigger",
                            details={
                                "message_id": message.message_id,
                                "product": parsed.product_name,
                                "price": parsed.price,
                                "discount": parsed.discount_percent,
                                "savings": parsed.savings,
                                "matched_keywords": matched_rule.keywords if matched_rule else [],
                                "max_price": matched_rule.max_price if matched_rule else 0,
                                "amazon_urls": parsed.amazon_urls,
                                "verdict": f"WOULD TRIGGER - {'no whitelist rule blocked' if channel_type == 'whitelist' else 'blacklist rule matched'}",
                                "dry_run": True,
                                "channel": channel_name,
                                "channel_type": channel_type
                            }
                        )
                    )
                    # Save to activity history
                    add_activity_item(create_activity_item(
                        product=parsed.product_name,
                        price=parsed.price,
                        discount=parsed.discount_percent,
                        savings=parsed.savings,
                        amazon_urls=parsed.amazon_urls,
                        triggered=True,
                        matched_rule={"keywords": matched_rule.keywords, "max_price": matched_rule.max_price} if matched_rule else None,
                        message_id=message.message_id,
                        channel=channel_name
                    ))
                else:
                    # LIVE MODE: Actually queue for processing
                    await event_broker.publish(
                        event_broker.create_event(
                            EventType.STEP,
                            "rule_matched",
                            details={
                                "message_id": message.message_id,
                                "product": parsed.product_name,
                                "price": parsed.price,
                                "discount": parsed.discount_percent,
                                "savings": parsed.savings,
                                "matched_keywords": matched_rule.keywords if matched_rule else [],
                                "max_price": matched_rule.max_price if matched_rule else 0,
                                "amazon_urls": parsed.amazon_urls,
                                "channel": channel_name,
                                "channel_type": channel_type
                            }
                        )
                    )
                    # Save to activity history
                    add_activity_item(create_activity_item(
                        product=parsed.product_name,
                        price=parsed.price,
                        discount=parsed.discount_percent,
                        savings=parsed.savings,
                        amazon_urls=parsed.amazon_urls,
                        triggered=True,
                        matched_rule={"keywords": matched_rule.keywords, "max_price": matched_rule.max_price} if matched_rule else None,
                        message_id=message.message_id,
                        channel=channel_name
                    ))

                    # Queue URLs for Amazon worker
                    for url in parsed.amazon_urls:
                        await event_broker.publish(
                            event_broker.create_event(
                                EventType.URL_DETECTED,
                                "amazon_url_detected",
                                url=url,
                                details={
                                    "message_id": message.message_id,
                                    "author": message.author,
                                    "product": parsed.product_name,
                                    "price": parsed.price
                                }
                            )
                        )

                        if self._url_queue:
                            await self._url_queue.put({
                                "url": url,
                                "message": message.to_dict(),
                                "parsed": {
                                    "price": parsed.price,
                                    "discount": parsed.discount_percent,
                                    "product": parsed.product_name
                                }
                            })
            else:
                # Should NOT trigger
                await event_broker.publish(
                    event_broker.create_event(
                        EventType.STEP,
                        "dry_run_no_match" if self.dry_run else "no_rule_matched",
                        details={
                            "message_id": message.message_id,
                            "product": parsed.product_name,
                            "price": parsed.price,
                            "discount": parsed.discount_percent,
                            "savings": parsed.savings,
                            "amazon_urls": parsed.amazon_urls,
                            "verdict": f"WOULD NOT TRIGGER - {'whitelist rule blocked' if matched_rule else 'no blacklist rule matched'}",
                            "dry_run": self.dry_run,
                            "channel": channel_name,
                            "channel_type": channel_type
                        }
                    )
                )
                # Save to activity history
                add_activity_item(create_activity_item(
                    product=parsed.product_name,
                    price=parsed.price,
                    discount=parsed.discount_percent,
                    savings=parsed.savings,
                    amazon_urls=parsed.amazon_urls,
                    triggered=False,
                    matched_rule={"keywords": matched_rule.keywords, "max_price": matched_rule.max_price} if matched_rule else None,
                    message_id=message.message_id,
                    channel=channel_name
                ))

        # Fallback: if no parsed format, use legacy keyword/URL matching
        elif not parsed:
            amazon_urls = self._extract_amazon_urls(message.text, message.urls)

            if amazon_urls:
                # Check keyword filter (legacy mode)
                if not self._matches_keywords(message.text):
                    await event_broker.publish(
                        event_broker.create_event(
                            EventType.STEP,
                            "keyword_filter_skip",
                            details={"message_id": message.message_id, "keywords": self.keywords}
                        )
                    )
                    return

                # Check URL regex filter
                if not self._matches_url_regex(amazon_urls):
                    await event_broker.publish(
                        event_broker.create_event(
                            EventType.STEP,
                            "url_regex_filter_skip",
                            details={"message_id": message.message_id}
                        )
                    )
                    return

                if self.dry_run:
                    # DRY RUN: Log what would happen (legacy mode)
                    await event_broker.publish(
                        event_broker.create_event(
                            EventType.STEP,
                            "dry_run_would_trigger_legacy",
                            details={
                                "message_id": message.message_id,
                                "author": message.author,
                                "amazon_urls": amazon_urls,
                                "text_preview": message.text[:200],
                                "verdict": "WOULD TRIGGER (legacy mode) - keywords matched",
                                "matched_keywords": self.keywords,
                                "dry_run": True
                            }
                        )
                    )
                else:
                    # LIVE MODE: Queue for processing
                    for url in amazon_urls:
                        await event_broker.publish(
                            event_broker.create_event(
                                EventType.URL_DETECTED,
                                "amazon_url_detected",
                                url=url,
                                details={
                                    "message_id": message.message_id,
                                    "author": message.author,
                                    "mode": "legacy"
                                }
                            )
                        )

                        if self._url_queue:
                            await self._url_queue.put({
                                "url": url,
                                "message": message.to_dict()
                            })

    async def _seed_existing_messages(self, page, channel_url: str) -> None:
        """
        On startup: mark ALL existing messages as seen (including the last one).
        This allows all channels to load quickly before any processing begins.
        New messages arriving after startup will be processed normally.
        """
        channel_name = self.get_channel_name(channel_url)
        try:
            message_elements = page.locator(self.SELECTORS["message_item"])
            count = await message_elements.count()

            if count == 0:
                return

            await event_broker.publish(
                event_broker.create_event(
                    EventType.STEP,
                    "seeding_messages",
                    details={
                        "total_visible": count,
                        "channel": channel_name,
                        "message": f"[{channel_name}] Marking {count} existing messages as seen"
                    }
                )
            )

            # Mark ALL messages as seen (skip analysis to allow fast startup)
            for i in range(count):
                element = message_elements.nth(i)
                message = await self._parse_message_element(page, element)
                if message and message.message_id:
                    self._seen_message_ids.add(message.message_id)

            # Save the seeded state
            self._save_state()

        except Exception as e:
            await event_broker.publish(
                event_broker.create_event(
                    EventType.ERROR,
                    "seeding_error",
                    details={"error": str(e), "channel": channel_name}
                )
            )

    async def navigate_to_channel(self, channel_url: str) -> bool:
        """Navigate to a specific Discord channel."""
        page = await browser_manager.get_or_create_discord_page(channel_url)

        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "discord_navigate",
                url=channel_url,
                details={"message": "Navigating to Discord channel"}
            )
        )

        try:
            await page.goto(channel_url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(3)  # Wait for Discord to fully load

            # Check if we're logged in by looking for the message list
            try:
                await page.wait_for_selector(
                    self.SELECTORS["message_list"],
                    timeout=30000
                )
                # Cache the channel name
                self._channel_names[channel_url] = await self._get_channel_name(page, channel_url)
                return True
            except Exception:
                await event_broker.publish(
                    event_broker.create_event(
                        EventType.ERROR,
                        "discord_not_logged_in",
                        details={"message": "Discord login required - please use noVNC"}
                    )
                )
                return False

        except Exception as e:
            await event_broker.publish(
                event_broker.create_event(
                    EventType.ERROR,
                    "discord_navigate_error",
                    url=channel_url,
                    details={"error": str(e)}
                )
            )
            return False

    async def _poll_channel(self, channel_url: str) -> None:
        """Poll a single channel for new messages."""
        page = await browser_manager.get_or_create_discord_page(channel_url)
        channel_name = self.get_channel_name(channel_url)

        try:
            # Find all message elements
            message_elements = page.locator(self.SELECTORS["message_item"])
            count = await message_elements.count()

            # Only check the last few messages for new ones
            for i in range(max(0, count - 5), count):
                element = message_elements.nth(i)
                message = await self._parse_message_element(page, element)

                if message:
                    await self._process_new_message(message, channel_url)

        except Exception as e:
            await event_broker.publish(
                event_broker.create_event(
                    EventType.ERROR,
                    "discord_poll_error",
                    details={"error": str(e), "channel": channel_name}
                )
            )

            # Try to recover by reloading
            try:
                await page.reload(wait_until="networkidle", timeout=30000)
            except Exception:
                pass

    async def start_watching(self) -> None:
        """Start watching Discord channels for new messages."""
        self._load_state()
        self._is_running = True

        event_broker.current_state = BotState.DISCORD_MONITORING

        await event_broker.publish(
            event_broker.create_event(
                EventType.STATE_CHANGE,
                "discord_watch_start",
                details={
                    "channel_urls": self.channel_urls,
                    "channel_count": len(self.channel_urls)
                }
            )
        )

        # Navigate to all channels and seed messages
        for channel_url in self.channel_urls:
            if not await self.navigate_to_channel(channel_url):
                await event_broker.publish(
                    event_broker.create_event(
                        EventType.ERROR,
                        "channel_init_failed",
                        details={"channel_url": channel_url}
                    )
                )
                continue

            page = await browser_manager.get_or_create_discord_page(channel_url)
            await self._seed_existing_messages(page, channel_url)

        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "all_channels_ready",
                details={
                    "channels": [self.get_channel_name(url) for url in self.channel_urls],
                    "message": f"Monitoring {len(self.channel_urls)} channel(s)"
                }
            )
        )

        # Polling loop - check all channels in parallel
        while self._is_running and browser_manager.is_running:
            # Poll all channels concurrently
            await asyncio.gather(
                *[self._poll_channel(url) for url in self.channel_urls],
                return_exceptions=True
            )

            # Wait before next poll
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        """Stop watching."""
        self._is_running = False
        self._save_state()
