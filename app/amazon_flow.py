"""
Amazon purchase flow state machine.
Handles: Open Product → Add to Cart → Proceed to Checkout → Place Order
"""

import asyncio
import os
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any
from dataclasses import dataclass

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout
from typing import List

from app.events import event_broker, EventType, BotState
from app.browser import browser_manager


@dataclass
class SellerInfo:
    """Information about the seller/shipper for a product."""
    ships_from: Optional[str] = None
    sold_by: Optional[str] = None
    raw_text: str = ""

    def is_amazon_shipper(self) -> bool:
        """Check if ships from Amazon.com (exact match only)."""
        if not self.ships_from:
            return False
        return self.ships_from.strip().lower() == "amazon.com"

    def is_valid_seller(self) -> bool:
        """Check if sold by Amazon (matches Amazon.com, Amazon Resale, Amazon Warehouse, etc.)."""
        if not self.sold_by:
            return False
        return "amazon" in self.sold_by.lower()


@dataclass
class PriceInfo:
    """Price information extracted from page."""
    displayed_price: Optional[float] = None
    raw_text: str = ""

# =============================================================================
# CONFIGURABLE TIMING PARAMETERS (via environment variables)
# =============================================================================

# TIMEOUT_* = max wait, proceeds immediately when ready
# WAIT_* = fixed sleep, always waits the full duration

# Timeouts in milliseconds (proceed as soon as condition met)
TIMEOUT_MS_PAGE_LOAD = int(os.getenv("TIMEOUT_MS_PAGE_LOAD", "30000"))
TIMEOUT_MS_ELEMENT_VISIBLE = int(os.getenv("TIMEOUT_MS_ELEMENT_VISIBLE", "10000"))
TIMEOUT_MS_SELECTOR_CHECK = int(os.getenv("TIMEOUT_MS_SELECTOR_CHECK", "150"))
TIMEOUT_MS_AOD_PANEL = int(os.getenv("TIMEOUT_MS_AOD_PANEL", "10000"))
TIMEOUT_MS_CHECKOUT_LOAD = int(os.getenv("TIMEOUT_MS_CHECKOUT_LOAD", "30000"))

# Timeout in seconds for order confirmation
TIMEOUT_SECONDS_ORDER_CONFIRM = float(os.getenv("TIMEOUT_SECONDS_ORDER_CONFIRM", "300"))

# Fixed waits in seconds (DEPRECATED - prefer event-driven waits below)
# These are only used as fallbacks when element detection fails
WAIT_SECONDS_DYNAMIC_CONTENT = float(os.getenv("WAIT_SECONDS_DYNAMIC_CONTENT", "2.0"))
WAIT_SECONDS_CART_UPDATE = float(os.getenv("WAIT_SECONDS_CART_UPDATE", "2.0"))
WAIT_SECONDS_CHECKOUT_TRANSITION = float(os.getenv("WAIT_SECONDS_CHECKOUT_TRANSITION", "3.0"))

# Event-driven wait timeouts (proceed immediately when element appears)
TIMEOUT_MS_BUYBOX_READY = int(os.getenv("TIMEOUT_MS_BUYBOX_READY", "10000"))  # Wait for buybox after page load
TIMEOUT_MS_CART_CONFIRM = int(os.getenv("TIMEOUT_MS_CART_CONFIRM", "10000"))  # Wait for cart confirmation
TIMEOUT_MS_CHECKOUT_READY = int(os.getenv("TIMEOUT_MS_CHECKOUT_READY", "15000"))  # Wait for checkout page elements

# Retry settings
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
DELAY_SECONDS_RETRY = float(os.getenv("DELAY_SECONDS_RETRY", "0.5"))


class FlowState(str, Enum):
    """States in the Amazon purchase flow."""
    IDLE = "idle"
    OPENING_PRODUCT = "opening_product"
    ADDING_TO_CART = "adding_to_cart"
    WAITING_CART_CONFIRMATION = "waiting_cart_confirmation"
    PROCEEDING_TO_CHECKOUT = "proceeding_to_checkout"
    ON_CHECKOUT_PAGE = "on_checkout_page"
    PLACING_ORDER = "placing_order"
    ORDER_PENDING_CONFIRMATION = "order_pending_confirmation"
    ORDER_PLACED = "order_placed"
    ERROR = "error"
    COMPLETE = "complete"


@dataclass
class FlowResult:
    """Result of a flow execution."""
    success: bool
    state: FlowState
    message: str
    details: Dict[str, Any]


class AmazonFlow:
    """
    State machine for Amazon purchase flow.

    Flow:
    1. Open product page
    2. Click "Add to Cart"
    3. Wait for side panel/drawer or cart confirmation
    4. Click "Proceed to checkout" (from panel or cart page)
    5. On review page, click "Place your order" (with optional confirmation gate)
    """

    # Selectors for Amazon web pages (may need updates as Amazon changes)
    SELECTORS = {
        # Add to Cart buttons - AOD (All Offers Display) selectors FIRST
        # since many products show the AOD panel instead of main Add to Cart
        "add_to_cart": [
            # AOD overlay panel - these work on product pages with multiple sellers
            ".aod-clear-float input[name='submit.addToCart']",
            "#aod-pinned-offer input[name='submit.addToCart']",
            "#aod-offer-list input[name='submit.addToCart']",
            ".aod-clear-float .a-button-input",
            "#aod-pinned-offer .a-button-input",
            # Main Add to Cart button (standard product pages)
            "#add-to-cart-button",
            "input[name='submit.add-to-cart']",
            # Buy Now button as alternative
            "#buy-now-button",
            "input[name='submit.buy-now']",
            # Other AOD patterns
            ".asin-container-padding input[name='submit.addToCart']",
            "#aod-pinned-offer input.a-button-input",
            "#aod-offer-list .a-button-input",
            "#aod-offer input[name='submit.addToCart']",
            "[data-aod-atc-action] input",
            ".aod-atc-button input",
            "#aod-offer .a-button-input",
            "#all-offers-display .a-button-input",
            "#aod-container .a-button-input",
            # Desktop offers pane and generic patterns
            "[data-feature-id='addToCart'] button",
            "#submit.add-to-cart",
            "#desktop_qualifiedBuyBox input[name='submit.add-to-cart']",
            "#qualifiedBuybox input[name='submit.add-to-cart']",
            "input[data-action='add-to-cart']",
            "[data-feature-id='desktop-action-panel'] input[name='submit.add-to-cart']",
        ],
        # Side panel / drawer that appears after adding to cart
        "side_panel": [
            "#attach-sidesheet",
            "#attach-accessory-pane",
            "[data-feature-id='attach-accessory-pane']",
            "#huc-v2-order-row-container",
            "#sw-atc-details-single-container",
        ],
        # Cart confirmation elements
        "cart_confirmation": [
            "#huc-v2-order-row-confirm-text",
            "#hlb-view-cart-announce",
            "[data-feature-id='huc-v2-order-row']",
            "#NATC_SMART_WAGON_CONF_MSG_SUCCESS",
        ],
        # Proceed to checkout from side panel
        "side_panel_checkout": [
            "#attach-sidesheet-checkout-button",
            "#hlb-ptc-btn-native",
            "input[name='proceedToRetailCheckout']",
            "#sc-buy-box-ptc-button input",
            "[data-feature-id='proceed-to-checkout-action'] input",
        ],
        # Go to Cart button
        "go_to_cart": [
            "#hlb-view-cart",
            "#nav-cart",
            "a[href*='/cart']",
            "#sw-gtc",
        ],
        # Cart page proceed to checkout
        "cart_checkout": [
            "input[name='proceedToRetailCheckout']",
            "#sc-buy-box-ptc-button input",
            "[data-feature-id='proceed-to-checkout-action'] input",
            "#sc-buy-box-ptc-button",
        ],
        # Place order button on final page
        "place_order": [
            "input[name='placeYourOrder1']",
            "#submitOrderButtonId input",
            "#bottomSubmitOrderButtonId input",
            "[name='placeYourOrder1']",
            "#turbo-checkout-pyo-button",
        ],
        # Order confirmation
        "order_confirmation": [
            "#checkoutThankYouHeader",
            "[data-testid='order-confirmation']",
            ".a-box-inner h1:has-text('Order placed')",
            "#widget-purchaseSummary",
        ],
        # Product availability
        "currently_unavailable": [
            "#availability span:has-text('Currently unavailable')",
            ".a-size-medium:has-text('Currently unavailable')",
            "text='Currently unavailable'",
        ],
        "see_all_buying_options": [
            "#buybox-see-all-buying-choices",
            "a:has-text('See All Buying Options')",
            "#desktop_buybox_content a:has-text('See All Buying Options')",
        ],
        # AOD Panel - No offers detection
        "aod_no_offers": [
            "text='No featured offers available'",
            "#aod-pinned-offer-show-more-link-announcement",
        ],
        # AOD Panel - Offer cards
        "aod_offer_cards": [
            "#aod-offer",
            ".aod-offer-container",
        ],
        # AOD Panel - See more expansion
        "aod_see_more": [
            "#aod-pinned-offer-show-more-link",
            ".aod-see-more-link",
        ],
        # AOD Panel - Seller info
        "aod_ships_from": [
            "#aod-offer-shipsFrom .a-row .a-size-small:last-child",
            "#aod-offer-shipsFrom span:last-child",
            "#aod-pinned-offer .aod-ship-from span.a-size-small",
            "div:has-text('Ships from') + div",
            "[id*='shipFrom'] span",
        ],
        "aod_sold_by": [
            "#aod-offer-soldBy .a-row a",
            "#aod-offer-soldBy a",
            "#aod-pinned-offer .aod-sold-by a",
            "div:has-text('Sold by') + div a",
            "div:has-text('Sold by') + div",
            "[id*='soldBy'] a",
        ],
        # AOD Panel - Price
        "aod_price": [
            "#aod-pinned-offer .a-price .a-offscreen",
            ".aod-pinned-offer-price .a-offscreen",
        ],
        # Standard page - Seller info
        "standard_merchant_info": [
            "#merchant-info",
            "#tabular-buybox",
        ],
        "standard_ships_sold_combined": [
            "#merchant-info",
        ],
        # Standard page - Price
        "standard_price": [
            "#corePrice_feature_div .a-price .a-offscreen",
            "#apex_desktop .a-price .a-offscreen",
            ".a-price.aok-align-center .a-offscreen",
        ],
        # Event-driven wait selectors - elements indicating page is ready
        "buybox_ready": [
            "#add-to-cart-button",
            "#buy-now-button",
            "#buybox-see-all-buying-choices",
            "#desktop_buybox",
            "#aod-pinned-offer",
        ],
        "cart_confirm_ready": [
            "#attach-sidesheet",  # Side panel
            "#sw-atc-details-single-container",  # Cart added confirmation
            "#huc-v2-order-row-confirm-text",  # "Added to Cart" text
            "#NATC_SMART_WAGON_CONF_MSG_SUCCESS",  # Success message
            "#hlb-view-cart-announce",  # View cart button appeared
        ],
        "checkout_ready": [
            "input[name='placeYourOrder1']",  # Place order button
            "#submitOrderButtonId",  # Submit order
            "#turbo-checkout-pyo-button",  # Turbo checkout
            "#checkout-main",  # Checkout container
            "[data-feature-id='checkout']",  # Checkout feature
        ],
    }

    # Timeouts in milliseconds (using env var values)
    TIMEOUTS = {
        "page_load": TIMEOUT_MS_PAGE_LOAD,
        "element_visible": TIMEOUT_MS_ELEMENT_VISIBLE,
        "side_panel": TIMEOUT_MS_AOD_PANEL,
        "checkout_load": TIMEOUT_MS_CHECKOUT_LOAD,
        "order_confirmation": TIMEOUT_MS_CHECKOUT_LOAD,
    }

    def __init__(self, confirm_final_order: bool = True):
        self.confirm_final_order = confirm_final_order
        self._current_state = FlowState.IDLE
        self._current_url: Optional[str] = None
        self._message_id: Optional[str] = None
        self._seller_info: Optional[SellerInfo] = None
        self._price_info: Optional[PriceInfo] = None

    @property
    def current_state(self) -> FlowState:
        return self._current_state

    def _is_aod_url(self, url: str) -> bool:
        """Check if URL contains aod=1 parameter."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        return params.get('aod', [''])[0] == '1'

    def _update_state(self, state: FlowState) -> None:
        """Update flow state and sync with event broker."""
        self._current_state = state

        # Map flow state to bot state
        state_mapping = {
            FlowState.IDLE: BotState.IDLE,
            FlowState.OPENING_PRODUCT: BotState.AMAZON_OPENING,
            FlowState.ADDING_TO_CART: BotState.AMAZON_ADD_TO_CART,
            FlowState.WAITING_CART_CONFIRMATION: BotState.AMAZON_ADD_TO_CART,
            FlowState.PROCEEDING_TO_CHECKOUT: BotState.AMAZON_PROCEED_CHECKOUT,
            FlowState.ON_CHECKOUT_PAGE: BotState.AMAZON_PROCEED_CHECKOUT,
            FlowState.PLACING_ORDER: BotState.AMAZON_PLACE_ORDER_PENDING,
            FlowState.ORDER_PENDING_CONFIRMATION: BotState.AMAZON_PLACE_ORDER_PENDING,
            FlowState.ORDER_PLACED: BotState.AMAZON_ORDER_PLACED,
            FlowState.ERROR: BotState.ERROR,
            FlowState.COMPLETE: BotState.DISCORD_MONITORING,
        }
        event_broker.current_state = state_mapping.get(state, BotState.IDLE)

    async def _find_and_click(
        self,
        page: Page,
        selectors: list,
        step_name: str,
        timeout: int = 10000
    ) -> bool:
        """Try multiple selectors and click the first visible element."""
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                # Use short timeout per selector to quickly skip non-existent ones
                if await locator.is_visible(timeout=TIMEOUT_MS_SELECTOR_CHECK):
                    await locator.click(timeout=timeout)
                    await event_broker.publish(
                        event_broker.create_event(
                            EventType.STEP,
                            step_name,
                            url=page.url,
                            details={"selector": selector, "action": "clicked"}
                        )
                    )
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_any(
        self,
        page: Page,
        selectors: list,
        timeout: int = 10000
    ) -> Optional[str]:
        """Wait for any of the selectors to be visible (legacy polling method)."""
        end_time = asyncio.get_event_loop().time() + (timeout / 1000)

        while asyncio.get_event_loop().time() < end_time:
            for selector in selectors:
                try:
                    locator = page.locator(selector).first
                    if await locator.is_visible(timeout=500):
                        return selector
                except Exception:
                    continue
            await asyncio.sleep(0.5)
        return None

    async def _wait_for_element(
        self,
        page: Page,
        selector_key: str,
        timeout: int = 10000,
        state: str = "visible"
    ) -> Optional[str]:
        """
        Wait for any of the selectors to become visible.
        Simple polling approach - checks each selector in sequence.

        Returns:
            The selector that matched, or None if timeout
        """
        selectors = self.SELECTORS.get(selector_key, [])
        if not selectors:
            return None

        end_time = asyncio.get_event_loop().time() + (timeout / 1000)

        while asyncio.get_event_loop().time() < end_time:
            for selector in selectors:
                try:
                    locator = page.locator(selector).first
                    if await locator.is_visible(timeout=200):
                        return selector
                except:
                    continue
            await asyncio.sleep(0.3)

        return None

    async def _extract_seller_info_aod(self, page: Page) -> SellerInfo:
        """Extract seller info from AOD panel."""
        info = SellerInfo()

        # Check for "No featured offers"
        try:
            no_offers = page.locator("text='No featured offers available'")
            if await no_offers.is_visible(timeout=1000):
                return SellerInfo(raw_text="No featured offers available")
        except:
            pass

        # Try to click "See more" to expand offers
        try:
            see_more = page.locator("#aod-pinned-offer-show-more-link").first
            if await see_more.is_visible(timeout=500):
                await see_more.click()
                # Event-driven wait: Wait for expanded content (offer cards)
                try:
                    await page.locator("#aod-offer").first.wait_for(state="visible", timeout=2000)
                except:
                    pass  # Continue even if timeout
        except:
            pass

        # Extract ships from / sold by
        for selector in self.SELECTORS.get("aod_ships_from", []):
            try:
                elem = page.locator(selector).first
                if await elem.is_visible(timeout=500):
                    info.ships_from = (await elem.inner_text()).strip()
                    await self._log_step("debug_ships_from", f"Found ships_from: '{info.ships_from}' using selector: {selector}")
                    break
            except:
                continue

        for selector in self.SELECTORS.get("aod_sold_by", []):
            try:
                elem = page.locator(selector).first
                if await elem.is_visible(timeout=500):
                    info.sold_by = (await elem.inner_text()).strip()
                    await self._log_step("debug_sold_by", f"Found sold_by: '{info.sold_by}' using selector: {selector}")
                    break
            except:
                continue

        # If we found ships_from but not sold_by, check if they might be combined
        # or if just "Amazon.com" means both
        if info.ships_from and not info.sold_by:
            if 'amazon' in info.ships_from.lower():
                info.sold_by = info.ships_from
        elif info.sold_by and not info.ships_from:
            if 'amazon' in info.sold_by.lower():
                info.ships_from = info.sold_by

        # Try to get combined seller info from AOD panel
        if not info.ships_from and not info.sold_by:
            try:
                # Look for combined seller info in AOD panel
                seller_elem = page.locator("#aod-pinned-offer #aod-offer-seller, #aod-pinned-offer .a-popover-trigger").first
                if await seller_elem.is_visible(timeout=500):
                    text = (await seller_elem.inner_text()).strip()
                    if 'amazon' in text.lower():
                        info.ships_from = "Amazon.com"
                        info.sold_by = "Amazon.com"
                        info.raw_text = text
            except:
                pass

        # Debug log final extraction
        await self._log_step("debug_aod_final", f"AOD extraction complete", {
            "ships_from": info.ships_from,
            "sold_by": info.sold_by,
            "raw_text": info.raw_text
        })

        return info

    async def _extract_seller_info_standard(self, page: Page) -> SellerInfo:
        """Extract seller info from standard product page."""
        info = SellerInfo()

        # =================================================================
        # PRIORITY 1: Try specific seller element selectors first
        # =================================================================
        # Look for seller link directly (most reliable when present)
        seller_link_selectors = [
            "#sellerProfileTriggerId",  # Seller profile link
            "a[href*='/seller/']",  # Any seller link
            "#tabular-buybox a[href*='/seller/']",  # Tabular buybox seller link
            "#desktop_buybox a[href*='/seller/']",  # Desktop buybox seller link
        ]

        for selector in seller_link_selectors:
            try:
                elem = page.locator(selector).first
                if await elem.is_visible(timeout=300):
                    seller_name = (await elem.inner_text()).strip()
                    if seller_name and len(seller_name) > 1:
                        await self._log_step("debug_seller_link_found", f"Found seller via link: {seller_name}", {"selector": selector})
                        # If we found seller via link, assume ships_from is same unless we find otherwise
                        info.sold_by = seller_name
                        info.ships_from = seller_name
                        if 'amazon' in seller_name.lower():
                            info.ships_from = "Amazon.com"
                            info.sold_by = "Amazon.com"
                        info.raw_text = f"Seller link: {seller_name}"
                        return info
            except:
                continue

        # =================================================================
        # PRIORITY 2: Try buybox text parsing as fallback
        # =================================================================
        buybox_selectors = [
            "#merchant-info",
            "#desktop_buybox",
            "#buybox",
            "#apex_desktop",
            ".celwidget[data-feature-name='desktop-buybox']"
        ]

        buybox_text = ""
        for selector in buybox_selectors:
            try:
                element = page.locator(selector).first
                if await element.is_visible(timeout=500):
                    buybox_text = (await element.inner_text()).strip()
                    if buybox_text:
                        await self._log_step("debug_buybox_found", f"Found buybox with selector: {selector}", {"preview": buybox_text[:200]})
                        break
                    else:
                        await self._log_step("debug_buybox_empty", f"Selector {selector} found but text empty")
                else:
                    await self._log_step("debug_buybox_not_visible", f"Selector {selector} not visible")
            except Exception as e:
                await self._log_step("debug_buybox_error", f"Selector {selector} error: {str(e)}")

        if buybox_text:
            info.raw_text = buybox_text
            text_lower = buybox_text.lower()

            # Pattern 1: "Ships from and sold by Amazon.com"
            if "ships from and sold by amazon" in text_lower:
                info.ships_from = "Amazon.com"
                info.sold_by = "Amazon.com"
                await self._log_step("debug_pattern_match", "Matched pattern: 'Ships from and sold by Amazon'")
                return info

            # Pattern 2: "Shipper / Seller\nAmazon.com" or similar label+value formats
            lines = [line.strip() for line in buybox_text.split('\n') if line.strip()]

            # Find lines that are just seller names (not labels)
            # Labels to ignore: Shipper, Seller, Ships from, Sold by, Returns, etc.
            label_keywords = ['shipper', 'seller', 'ships from', 'sold by', 'returns',
                            'delivery', 'quantity', 'add to cart', 'buy now', 'customer',
                            'service', 'see more', 'free', 'prime', 'deliver to', 'available',
                            'ship', 'payment', 'secure', 'transaction', 'protection', 'plan']

            # Additional non-seller keywords to filter out
            non_seller_keywords = ['in stock', 'out of stock', 'only', 'left', 'order soon',
                                   'refund', 'replacement', 'add to list', 'gift', 'qty', 'details']

            data_lines = []
            for line in lines:
                line_lower = line.lower()
                # Skip if it's a label or contains special chars suggesting it's a label
                is_label = any(label in line_lower for label in label_keywords)
                is_non_seller = any(kw in line_lower for kw in non_seller_keywords)
                is_price = '$' in line or any(c.isdigit() for c in line) and '.' in line
                is_short_price = line_lower in ['.', '..', '...']
                is_pure_number = line.isdigit()  # Skip quantity numbers like "1", "2", etc.

                if not is_label and not is_short_price and not is_non_seller and not is_pure_number:
                    # Keep this line if it looks like actual data
                    # But skip pure price lines like "$967.64"
                    if is_price and len(line.replace('$', '').replace('.', '').replace(',', '').strip()) > 0:
                        # It's a price line, skip it
                        continue
                    elif not is_price:
                        data_lines.append(line)

            await self._log_step("debug_data_lines", f"Extracted data lines: {data_lines}")

            # Look for seller name in data lines
            # The pattern is: "Shipper / Seller" label followed by the seller name
            # Data lines should contain the seller name after filtering out labels
            if len(data_lines) >= 1:
                # Prioritize finding Amazon or seller-like names
                seller_name = None

                # First pass: Look for Amazon
                for line in data_lines:
                    if line.isdigit() or len(line) < 3:
                        continue
                    if 'amazon' in line.lower():
                        seller_name = line
                        break

                # Second pass: Look for other seller names if Amazon not found
                if not seller_name:
                    for line in data_lines:
                        # Skip numeric lines, very short lines, and common non-seller text
                        if line.isdigit() or len(line) < 3:
                            continue
                        if any(word in line.lower() for word in ['refund', 'replacement', 'add to list', 'payment', 'return']):
                            continue
                        # This looks like a seller name
                        seller_name = line
                        break

                if seller_name:
                    await self._log_step("debug_seller_name_found", f"Found seller name: {seller_name}")
                    # Check if it's Amazon
                    if 'amazon' in seller_name.lower():
                        info.ships_from = "Amazon.com"
                        info.sold_by = "Amazon.com"
                        await self._log_step("debug_pattern_match", f"Matched pattern: Amazon seller found: {seller_name}")
                    else:
                        # It's a third-party seller
                        info.ships_from = seller_name
                        info.sold_by = seller_name
                        await self._log_step("debug_pattern_match", f"Found third-party seller: {seller_name}")
                    return info

        # Try tabular buybox format
        try:
            ships_row = page.locator("#tabular-buybox .tabular-buybox-text:has-text('Ships from')").first
            sold_row = page.locator("#tabular-buybox .tabular-buybox-text:has-text('Sold by')").first

            if await ships_row.is_visible(timeout=500):
                info.ships_from = (await ships_row.locator("span").last.inner_text()).strip()
                await self._log_step("debug_ships_from", f"Found ships_from: '{info.ships_from}' using tabular buybox")
            if await sold_row.is_visible(timeout=500):
                info.sold_by = (await sold_row.locator("span, a").last.inner_text()).strip()
                await self._log_step("debug_sold_by", f"Found sold_by: '{info.sold_by}' using tabular buybox")
        except:
            pass

        # Aggressive fallback: Search entire page for "Ships from" / "Sold by" text
        if not info.ships_from or not info.sold_by:
            try:
                # Look for ANY element containing "Ships from" or "Sold by"
                page_text = await page.content()
                await self._log_step("debug_page_search", "Searching entire page for seller info")

                # Try to find ships_from
                if not info.ships_from:
                    ships_elem = page.locator("text=/Ships from/i").first
                    if await ships_elem.is_visible(timeout=500):
                        # Get parent container and extract text
                        parent = ships_elem.locator("xpath=ancestor::div[1]")
                        text = await parent.inner_text()
                        # Parse out the shipper name (usually on next line or after "Ships from")
                        lines = [l.strip() for l in text.split('\n') if l.strip()]
                        for i, line in enumerate(lines):
                            if 'ships from' in line.lower() and i + 1 < len(lines):
                                info.ships_from = lines[i + 1]
                                await self._log_step("debug_ships_from", f"Found ships_from via page search: '{info.ships_from}'")
                                break

                # Try to find sold_by
                if not info.sold_by:
                    sold_elem = page.locator("text=/Sold by/i").first
                    if await sold_elem.is_visible(timeout=500):
                        parent = sold_elem.locator("xpath=ancestor::div[1]")
                        text = await parent.inner_text()
                        lines = [l.strip() for l in text.split('\n') if l.strip()]
                        for i, line in enumerate(lines):
                            if 'sold by' in line.lower() and i + 1 < len(lines):
                                info.sold_by = lines[i + 1]
                                await self._log_step("debug_sold_by", f"Found sold_by via page search: '{info.sold_by}'")
                                break
            except:
                pass

        # Debug log final extraction
        await self._log_step("debug_standard_final", f"Standard extraction complete", {
            "ships_from": info.ships_from,
            "sold_by": info.sold_by,
            "raw_text": info.raw_text
        })

        # If extraction failed, capture page state for debugging
        if not info.ships_from and not info.sold_by:
            try:
                # Take screenshot
                screenshot_path = await browser_manager.take_screenshot("seller_extraction_failed")

                # Get visible text from buybox area
                buybox_text = ""
                try:
                    buybox = page.locator("#desktop_buybox, #buybox, #apex_desktop").first
                    if await buybox.is_visible(timeout=1000):
                        buybox_text = await buybox.inner_text()
                except:
                    pass

                await self._log_step("debug_extraction_failed", "Seller extraction failed - captured page state", {
                    "screenshot": screenshot_path,
                    "buybox_text_preview": buybox_text[:500] if buybox_text else "No buybox found",
                    "page_url": page.url
                })
            except:
                pass

        return info

    async def _extract_price(self, page: Page, is_aod: bool) -> PriceInfo:
        """Extract displayed price from page."""
        import re
        selectors = self.SELECTORS.get("aod_price" if is_aod else "standard_price", [])

        for selector in selectors:
            try:
                elem = page.locator(selector).first
                if await elem.is_visible(timeout=500):
                    text = (await elem.inner_text()).strip()
                    # Parse "$123.45" or "123.45" format
                    price_match = re.search(r'\$?([\d,]+\.?\d*)', text)
                    if price_match:
                        return PriceInfo(
                            displayed_price=float(price_match.group(1).replace(',', '')),
                            raw_text=text
                        )
            except:
                continue

        return PriceInfo(raw_text="Price not found")

    async def _check_currently_unavailable(self, page: Page) -> bool:
        """Check if product is currently unavailable."""
        for selector in self.SELECTORS.get("currently_unavailable", []):
            try:
                elem = page.locator(selector).first
                if await elem.is_visible(timeout=500):
                    return True
            except:
                continue
        return False

    async def _check_and_click_see_all_options(self, page: Page) -> bool:
        """Check for 'See All Buying Options' and click it. Returns True if clicked."""
        for selector in self.SELECTORS.get("see_all_buying_options", []):
            try:
                elem = page.locator(selector).first
                if await elem.is_visible(timeout=1000):
                    await self._log_step("clicking_see_all_options", "Clicking 'See All Buying Options'")
                    await elem.click()

                    # Event-driven wait: Wait for AOD panel to appear
                    aod_ready = await self._wait_for_element(
                        page, "aod_offer_cards", timeout=TIMEOUT_MS_AOD_PANEL
                    )
                    if aod_ready:
                        await self._log_step("aod_panel_ready", "AOD panel loaded", {"selector": aod_ready})
                    else:
                        await asyncio.sleep(1.0)  # Fallback
                    return True
            except:
                continue
        return False

    async def _extract_aod_offer_info(self, offer_element, offer_name: str) -> tuple[Optional[str], Optional[str]]:
        """Extract ships_from and sold_by from an AOD offer element."""
        ships_from = None
        sold_by = None

        # Try to get ships from
        try:
            # Look for ships-from container within this offer
            ships_container = offer_element.locator("[id*='shipsFrom'], .aod-ship-from").first
            if await ships_container.is_visible(timeout=300):
                text = (await ships_container.inner_text()).strip()
                lines = [l.strip() for l in text.split('\n') if l.strip()]
                for line in lines:
                    if 'ships from' in line.lower():
                        continue
                    ships_from = line
                    break
        except:
            pass

        # Try to get sold by
        try:
            # First try to find seller link
            sold_link = offer_element.locator("[id*='soldBy'] a, .aod-sold-by a").first
            if await sold_link.is_visible(timeout=300):
                sold_by = (await sold_link.inner_text()).strip()
            else:
                # Fallback: get text container
                sold_text_elem = offer_element.locator("[id*='soldBy'], .aod-sold-by").first
                if await sold_text_elem.is_visible(timeout=300):
                    text = (await sold_text_elem.inner_text()).strip()
                    lines = [line.strip() for line in text.split('\n') if line.strip()]
                    for line in lines:
                        if 'sold by' in line.lower() or 'rating' in line.lower() or '%' in line:
                            continue
                        sold_by = line
                        break
        except:
            pass

        return ships_from, sold_by

    def _is_valid_amazon_offer(self, ships_from: Optional[str], sold_by: Optional[str]) -> bool:
        """Check if offer is from valid Amazon seller."""
        is_valid_shipper = ships_from and ships_from.strip().lower() == "amazon.com"
        is_valid_seller = sold_by and any(
            keyword in sold_by.lower()
            for keyword in ["amazon.com", "amazon resale", "amazon warehouse"]
        )
        return is_valid_shipper and is_valid_seller

    async def _find_valid_amazon_offer_in_aod(self, page: Page) -> Optional[Dict[str, Any]]:
        """
        Traverse AOD offers to find first valid Amazon offer.
        Valid = Ships from Amazon.com AND (Sold by Amazon.com OR Amazon Resale OR Amazon Warehouse)

        Checks in order:
        1. Pinned offer (featured offer at top)
        2. Offer list (additional offers below)

        Returns dict with offer info if found, None otherwise.
        """
        await self._log_step("aod_traversing", "Searching AOD offers for valid Amazon seller...")

        # Check for no offers message
        try:
            no_offers = page.locator("text='No featured offers available'").first
            if await no_offers.is_visible(timeout=1000):
                await self._log_step("aod_no_offers", "No featured offers available")
                return None
        except:
            pass

        # =================================================================
        # STEP 1: Check the PINNED OFFER first (featured offer at top)
        # =================================================================
        try:
            pinned_offer = page.locator("#aod-pinned-offer").first
            if await pinned_offer.is_visible(timeout=1000):
                await self._log_step("aod_checking_pinned", "Checking pinned offer...")

                ships_from, sold_by = await self._extract_aod_offer_info(pinned_offer, "pinned")

                await self._log_step("aod_pinned_checked", f"Pinned offer: Ships from '{ships_from}', Sold by '{sold_by}'", {
                    "offer_type": "pinned",
                    "ships_from": ships_from,
                    "sold_by": sold_by
                })

                if self._is_valid_amazon_offer(ships_from, sold_by):
                    await self._log_step("aod_valid_offer_found", "Valid Amazon offer found in pinned offer", {
                        "ships_from": ships_from,
                        "sold_by": sold_by,
                        "offer_type": "pinned"
                    })

                    # Find Add to Cart button in pinned offer
                    add_button = pinned_offer.locator("input[name='submit.addToCart'], .a-button-input").first
                    if await add_button.is_visible(timeout=500):
                        await self._log_step("aod_selecting_offer", "Selecting pinned offer")
                        self._seller_info = SellerInfo(
                            ships_from=ships_from,
                            sold_by=sold_by,
                            raw_text=f"Ships from {ships_from}, Sold by {sold_by}"
                        )
                        return {
                            "offer_index": "pinned",
                            "ships_from": ships_from,
                            "sold_by": sold_by,
                            "add_button": add_button
                        }
                else:
                    await self._log_step("aod_pinned_invalid", f"Pinned offer not valid Amazon seller, checking offer list...")
        except Exception as e:
            await self._log_step("aod_pinned_error", f"Error checking pinned offer: {str(e)}")

        # =================================================================
        # STEP 2: Check the OFFER LIST (additional offers below)
        # =================================================================
        # Try to expand "See more" if available
        try:
            see_more = page.locator("#aod-pinned-offer-show-more-link").first
            if await see_more.is_visible(timeout=500):
                await see_more.click()
                try:
                    await page.locator("#aod-offer").first.wait_for(state="visible", timeout=2000)
                except:
                    pass
        except:
            pass

        # Get all offer cards in the list
        offer_cards = page.locator("#aod-offer")
        count = await offer_cards.count()
        await self._log_step("aod_offers_found", f"Found {count} offers in offer list", {"count": count})

        # Traverse each offer in the list
        for i in range(count):
            offer = offer_cards.nth(i)

            ships_from, sold_by = await self._extract_aod_offer_info(offer, f"offer_{i}")

            await self._log_step("aod_offer_checked", f"Offer {i+1}: Ships from '{ships_from}', Sold by '{sold_by}'", {
                "offer_index": i,
                "ships_from": ships_from,
                "sold_by": sold_by
            })

            if self._is_valid_amazon_offer(ships_from, sold_by):
                await self._log_step("aod_valid_offer_found", f"Valid Amazon offer found at index {i}", {
                    "ships_from": ships_from,
                    "sold_by": sold_by,
                    "offer_index": i
                })

                # Try to click "Add to Cart" button for this offer
                try:
                    add_button = offer.locator("input[name='submit.addToCart'], .a-button-input").first
                    if await add_button.is_visible(timeout=500):
                        await self._log_step("aod_selecting_offer", f"Selecting offer {i}")
                        self._seller_info = SellerInfo(
                            ships_from=ships_from,
                            sold_by=sold_by,
                            raw_text=f"Ships from {ships_from}, Sold by {sold_by}"
                        )
                        return {
                            "offer_index": i,
                            "ships_from": ships_from,
                            "sold_by": sold_by,
                            "add_button": add_button
                        }
                except Exception as e:
                    await self._log_step("aod_select_error", f"Error selecting offer {i}: {str(e)}")

        await self._log_step("aod_no_valid_offer", "No valid Amazon offer found in AOD")
        return None

    async def _log_step(self, step: str, message: str, details: Dict[str, Any] = None) -> None:
        """Publish event and append to activity item steps."""
        await event_broker.publish(event_broker.create_event(
            EventType.STEP, step,
            url=self._current_url or "",
            details={"message_id": self._message_id, "message": message, **(details or {})}
        ))

        if self._message_id:
            from app.activity_store import append_activity_step
            append_activity_step(self._message_id, step, message, details)

    async def _step_validate_seller(self, page: Page, is_aod: bool) -> FlowResult:
        """Validate seller before adding to cart."""
        await self._log_step("seller_validating", "Checking seller information...")

        if is_aod:
            seller_info = await self._extract_seller_info_aod(page)
        else:
            seller_info = await self._extract_seller_info_standard(page)

        self._seller_info = seller_info  # Store for result tracking

        # Check for "No featured offers"
        if "no featured offers" in seller_info.raw_text.lower():
            await self._log_step("seller_failed", "No featured offers available", {"raw_text": seller_info.raw_text})
            return FlowResult(
                success=False, state=FlowState.ERROR,
                message="No featured offers available",
                details={"seller_info": {"raw_text": seller_info.raw_text}}
            )

        # Validate shipper
        if not seller_info.is_amazon_shipper():
            msg = f"Invalid shipper: Ships from '{seller_info.ships_from or 'Unknown'}'"
            await self._log_step("seller_failed", msg, {"ships_from": seller_info.ships_from, "sold_by": seller_info.sold_by})
            return FlowResult(
                success=False, state=FlowState.ERROR,
                message=msg,
                details={"ships_from": seller_info.ships_from, "sold_by": seller_info.sold_by}
            )

        # Validate seller
        if not seller_info.is_valid_seller():
            msg = f"Invalid seller: Sold by '{seller_info.sold_by or 'Unknown'}'"
            await self._log_step("seller_failed", msg, {"ships_from": seller_info.ships_from, "sold_by": seller_info.sold_by})
            return FlowResult(
                success=False, state=FlowState.ERROR,
                message=msg,
                details={"ships_from": seller_info.ships_from, "sold_by": seller_info.sold_by}
            )

        await self._log_step("seller_validated", "Seller is Amazon.com", {"ships_from": seller_info.ships_from, "sold_by": seller_info.sold_by})

        return FlowResult(success=True, state=FlowState.IDLE, message="Seller validated", details={})

    async def _step_validate_price(self, page: Page, expected_price: float, is_aod: bool) -> FlowResult:
        """Validate price exactly matches expected."""
        await self._log_step("price_validating", f"Checking price matches ${expected_price:.2f}...")

        price_info = await self._extract_price(page, is_aod)
        self._price_info = price_info  # Store for result tracking

        if price_info.displayed_price is None:
            await self._log_step("price_failed", "Could not extract price from page", {"raw_text": price_info.raw_text})
            return FlowResult(
                success=False, state=FlowState.ERROR,
                message="Could not extract price from page",
                details={"raw_text": price_info.raw_text}
            )

        # EXACT match required
        if price_info.displayed_price != expected_price:
            msg = f"Price mismatch: ${price_info.displayed_price:.2f} vs expected ${expected_price:.2f}"
            await self._log_step("price_failed", msg, {"displayed": price_info.displayed_price, "expected": expected_price})
            return FlowResult(
                success=False, state=FlowState.ERROR,
                message=msg,
                details={"displayed": price_info.displayed_price, "expected": expected_price}
            )

        await self._log_step("price_validated", f"Price matches ${price_info.displayed_price:.2f}", {"price": price_info.displayed_price})

        return FlowResult(success=True, state=FlowState.IDLE, message="Price validated", details={})

    async def _handle_error(
        self,
        page: Page,
        stage: str,
        error: str
    ) -> None:
        """Handle an error by taking screenshot and saving trace."""
        self._update_state(FlowState.ERROR)

        # Take screenshot
        screenshot_path = await browser_manager.take_screenshot(stage)

        # Stop and save trace
        trace_path = await browser_manager.stop_tracing(stage)

        await event_broker.publish(
            event_broker.create_event(
                EventType.ERROR,
                f"amazon_{stage}_error",
                url=page.url if page else "",
                details={
                    "error": error,
                    "screenshot": screenshot_path,
                    "trace": trace_path
                }
            )
        )

    async def execute(self, url: str, message_info: Dict[str, Any] = None, expected_price: Optional[float] = None) -> FlowResult:
        """
        Execute the full Amazon purchase flow for a given URL.

        Returns FlowResult with success status and details.
        """
        self._current_url = url
        self._message_id = message_info.get("message_id", "") if message_info else ""
        page = None

        event_broker.current_urls = [url]
        event_broker.last_action = {
            "url": url,
            "started_at": datetime.now().isoformat(),
            "message_info": message_info
        }

        await self._log_step("flow_started", "Starting Amazon purchase flow", {"url": url})

        try:
            # Start tracing for this flow
            await browser_manager.start_tracing()

            # Get Amazon page
            page = await browser_manager.get_or_create_amazon_page()

            # Detect flow type
            is_aod = self._is_aod_url(url)
            await self._log_step("url_detected", f"URL type: {'AOD' if is_aod else 'Standard'}", {"is_aod": is_aod})

            # Step 1: Open product page
            result = await self._step_open_product(page, url)
            if not result.success:
                return result

            # Step 2: Check product availability
            if await self._check_currently_unavailable(page):
                await self._log_step("product_unavailable", "Product is currently unavailable")
                return FlowResult(
                    success=False,
                    state=FlowState.ERROR,
                    message="Product is currently unavailable",
                    details={}
                )

            # Step 3: Handle "See All Buying Options" for standard pages
            if not is_aod:
                clicked = await self._check_and_click_see_all_options(page)
                if clicked:
                    is_aod = True  # Now we're on AOD page
                    await self._log_step("navigated_to_aod", "Navigated to AOD page from 'See All Buying Options'")

            # Step 4: Handle AOD offer selection OR standard seller validation
            aod_offer = None
            if is_aod:
                # Find and select valid Amazon offer
                aod_offer = await self._find_valid_amazon_offer_in_aod(page)
                if not aod_offer:
                    return FlowResult(
                        success=False,
                        state=FlowState.ERROR,
                        message="No valid Amazon offer found in AOD",
                        details={}
                    )
                # Seller validation already done in AOD traversal, skip separate validation
            else:
                # Standard page - validate seller
                result = await self._step_validate_seller(page, is_aod)
                if not result.success:
                    return result

            # Step 5: Validate price
            if expected_price is not None:
                result = await self._step_validate_price(page, expected_price, is_aod)
                if not result.success:
                    return result

            # Step 6: Add to cart or Buy Now
            used_buy_now = False
            if aod_offer and aod_offer.get("add_button"):
                # Use the specific offer's add button from AOD
                await self._log_step("adding_to_cart", "Clicking Add to Cart for selected AOD offer...")
                try:
                    await aod_offer["add_button"].click()

                    # Event-driven wait: Wait for cart confirmation elements
                    cart_confirm = await self._wait_for_element(
                        page, "cart_confirm_ready", timeout=TIMEOUT_MS_CART_CONFIRM
                    )
                    if cart_confirm:
                        await self._log_step("cart_confirm_detected", f"Cart confirmation appeared", {"selector": cart_confirm})
                    else:
                        await asyncio.sleep(1.0)  # Fallback

                    self._update_state(FlowState.WAITING_CART_CONFIRMATION)
                except Exception as e:
                    return FlowResult(
                        success=False,
                        state=FlowState.ERROR,
                        message=f"Failed to click Add to Cart for selected offer: {str(e)}",
                        details={"error": str(e)}
                    )
            else:
                # Standard page - try Buy Now first (goes directly to checkout)
                buy_now_clicked = await self._try_buy_now(page)
                if buy_now_clicked:
                    used_buy_now = True
                    await self._log_step("used_buy_now", "Clicked Buy Now - going directly to checkout")
                else:
                    # Fall back to Add to Cart
                    await self._log_step("adding_to_cart", "Clicking Add to Cart...")
                    result = await self._step_add_to_cart(page)
                    if not result.success:
                        return result
                    await self._log_step("added_to_cart", "Item added to cart")

            # Step 7: Cart confirmation (skip if Buy Now was used)
            if not used_buy_now:
                result = await self._step_wait_cart_confirmation(page)
                if not result.success:
                    return result
                await self._log_step("cart_confirmed", "Cart confirmation received")

                # Step 8: Proceed to checkout
                await self._log_step("proceeding_to_checkout", "Proceeding to checkout...")
                result = await self._step_proceed_to_checkout(page)
                if not result.success:
                    return result

            await self._log_step("on_checkout_page", "On checkout page")

            # Step 7: Place order
            await self._log_step("placing_order", "Placing order...")
            result = await self._step_place_order(page)
            if result.success:
                await self._log_step("order_placed", "Order placed successfully")
            return result

        except Exception as e:
            if page:
                await self._handle_error(page, "flow_exception", str(e))
            await self._log_step("flow_error", f"Flow exception: {str(e)}", {"error": str(e)})
            return FlowResult(
                success=False,
                state=FlowState.ERROR,
                message=f"Flow exception: {str(e)}",
                details={"error": str(e)}
            )

        finally:
            # Clean up
            event_broker.current_urls = []
            try:
                await browser_manager.stop_tracing("flow_complete")
            except Exception:
                pass

    async def _step_open_product(self, page: Page, url: str) -> FlowResult:
        """Step 1: Open the Amazon product page."""
        self._update_state(FlowState.OPENING_PRODUCT)

        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "amazon_open_product",
                url=url
            )
        )

        for attempt in range(MAX_RETRIES):
            try:
                # Navigate to URL - don't wait for full load, just DOM ready
                # This prevents timeout on slow-loading images/resources
                await page.goto(url, wait_until="domcontentloaded", timeout=self.TIMEOUTS["page_load"])

                # Wait for buybox elements to appear (this is the real check)
                ready_selector = await self._wait_for_element(
                    page, "buybox_ready", timeout=TIMEOUT_MS_BUYBOX_READY
                )

                if ready_selector:
                    await event_broker.publish(
                        event_broker.create_event(
                            EventType.STEP,
                            "amazon_buybox_ready",
                            url=page.url,
                            details={"ready_selector": ready_selector}
                        )
                    )
                # Brief wait for JS to settle
                await asyncio.sleep(0.3)

                # Check if we landed on a product page
                if "amazon.com" in page.url or "amzn" in page.url:
                    await event_broker.publish(
                        event_broker.create_event(
                            EventType.STEP,
                            "amazon_product_loaded",
                            url=page.url
                        )
                    )
                    return FlowResult(
                        success=True,
                        state=FlowState.OPENING_PRODUCT,
                        message="Product page loaded",
                        details={"url": page.url}
                    )

            except PlaywrightTimeout:
                await event_broker.publish(
                    event_broker.create_event(
                        EventType.STEP,
                        "amazon_open_retry",
                        url=url,
                        details={"attempt": attempt + 1}
                    )
                )
                await asyncio.sleep(DELAY_SECONDS_RETRY)

        await self._handle_error(page, "open_product", "Failed to load product page")
        return FlowResult(
            success=False,
            state=FlowState.ERROR,
            message="Failed to load product page",
            details={"url": url}
        )

    async def _try_buy_now(self, page: Page) -> bool:
        """Try to click Buy Now button (goes directly to checkout). Returns True if clicked."""
        buy_now_selectors = [
            "#buy-now-button",
            "input[name='submit.buy-now']",
            "[data-feature-id='buy-now'] input",
            ".a-button-input[aria-labelledby='buy-now-button-announce']",
        ]

        for selector in buy_now_selectors:
            try:
                elem = page.locator(selector).first
                if await elem.is_visible(timeout=1000):
                    await elem.click()

                    # Event-driven wait: Wait for checkout page elements to appear
                    checkout_ready = await self._wait_for_element(
                        page, "checkout_ready", timeout=TIMEOUT_MS_CHECKOUT_READY
                    )

                    if checkout_ready:
                        await self._log_step("checkout_ready", f"Checkout page ready", {"selector": checkout_ready})
                    else:
                        # Fallback short wait if checkout elements not detected
                        await asyncio.sleep(1.0)

                    self._update_state(FlowState.ON_CHECKOUT_PAGE)
                    return True
            except:
                continue

        return False

    async def _step_add_to_cart(self, page: Page) -> FlowResult:
        """Step 2: Click Add to Cart button."""
        self._update_state(FlowState.ADDING_TO_CART)

        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "amazon_add_to_cart_start",
                url=page.url
            )
        )

        # First, wait for either the AOD panel or main Add to Cart button to be ready
        # The AOD panel loads asynchronously after the main page - prioritize it
        aod_ready_selectors = [
            ".aod-clear-float",     # AOD panel container (most common for multi-seller)
            "#aod-pinned-offer",    # AOD panel pinned offer
            "#aod-offer-list",      # AOD offer list
            "#add-to-cart-button",  # Main Add to Cart (fallback)
        ]

        # Event-driven wait: Wait for any cart-related element to appear
        panel_found = await self._wait_for_element(page, "buybox_ready", timeout=TIMEOUT_MS_AOD_PANEL)
        if panel_found:
            await event_broker.publish(
                event_broker.create_event(
                    EventType.STEP,
                    "amazon_cart_panel_ready",
                    url=page.url,
                    details={"selector": panel_found}
                )
            )
            # No fixed delay - element is already visible

        for attempt in range(MAX_RETRIES):
            if await self._find_and_click(
                page,
                self.SELECTORS["add_to_cart"],
                "amazon_add_to_cart_click",
                timeout=self.TIMEOUTS["element_visible"]
            ):
                # Event-driven wait: Wait for cart confirmation elements
                cart_confirm = await self._wait_for_element(
                    page, "cart_confirm_ready", timeout=TIMEOUT_MS_CART_CONFIRM
                )

                if cart_confirm:
                    await self._log_step("cart_confirm_detected", f"Cart confirmation appeared", {"selector": cart_confirm})
                else:
                    # Fallback short wait if confirmation not detected
                    await asyncio.sleep(1.0)

                return FlowResult(
                    success=True,
                    state=FlowState.ADDING_TO_CART,
                    message="Add to cart clicked",
                    details={}
                )

            await event_broker.publish(
                event_broker.create_event(
                    EventType.STEP,
                    "amazon_add_to_cart_retry",
                    url=page.url,
                    details={"attempt": attempt + 1}
                )
            )
            await asyncio.sleep(DELAY_SECONDS_RETRY)

        await self._handle_error(page, "add_to_cart", "Add to Cart button not found")
        return FlowResult(
            success=False,
            state=FlowState.ERROR,
            message="Add to Cart button not found",
            details={}
        )

    async def _step_wait_cart_confirmation(self, page: Page) -> FlowResult:
        """Step 3: Wait for side panel/drawer or cart confirmation."""
        self._update_state(FlowState.WAITING_CART_CONFIRMATION)

        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "amazon_wait_cart_confirmation",
                url=page.url
            )
        )

        # Wait for either side panel or cart confirmation
        all_selectors = self.SELECTORS["side_panel"] + self.SELECTORS["cart_confirmation"]
        found_selector = await self._wait_for_any(
            page,
            all_selectors,
            timeout=self.TIMEOUTS["side_panel"]
        )

        if found_selector:
            await event_broker.publish(
                event_broker.create_event(
                    EventType.STEP,
                    "amazon_cart_confirmed",
                    url=page.url,
                    details={"selector": found_selector}
                )
            )
            return FlowResult(
                success=True,
                state=FlowState.WAITING_CART_CONFIRMATION,
                message="Cart confirmation received",
                details={"selector": found_selector}
            )

        # If no confirmation, check if item is in cart anyway
        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "amazon_cart_confirmation_timeout",
                url=page.url,
                details={"message": "No confirmation panel, proceeding anyway"}
            )
        )

        return FlowResult(
            success=True,
            state=FlowState.WAITING_CART_CONFIRMATION,
            message="Proceeding without explicit confirmation",
            details={}
        )

    async def _step_proceed_to_checkout(self, page: Page) -> FlowResult:
        """Step 4: Proceed to checkout (from side panel or cart page)."""
        self._update_state(FlowState.PROCEEDING_TO_CHECKOUT)

        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "amazon_proceed_to_checkout_start",
                url=page.url
            )
        )

        # First, try checkout from side panel
        if await self._find_and_click(
            page,
            self.SELECTORS["side_panel_checkout"],
            "amazon_side_panel_checkout_click",
            timeout=5000
        ):
            # Event-driven wait: Wait for checkout page elements
            checkout_ready = await self._wait_for_element(
                page, "checkout_ready", timeout=TIMEOUT_MS_CHECKOUT_READY
            )
            if checkout_ready:
                await self._log_step("checkout_ready", "Checkout page ready from side panel", {"selector": checkout_ready})
            else:
                await asyncio.sleep(1.0)  # Fallback

            self._update_state(FlowState.ON_CHECKOUT_PAGE)
            return FlowResult(
                success=True,
                state=FlowState.ON_CHECKOUT_PAGE,
                message="Proceeded to checkout from side panel",
                details={}
            )

        # If side panel checkout not found, go to cart
        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "amazon_going_to_cart",
                url=page.url
            )
        )

        if await self._find_and_click(
            page,
            self.SELECTORS["go_to_cart"],
            "amazon_go_to_cart_click",
            timeout=5000
        ):
            # Event-driven wait: Wait for cart page to load (cart checkout button)
            cart_ready = await self._wait_for_element(
                page, "cart_checkout", timeout=TIMEOUT_MS_BUYBOX_READY
            )
            if not cart_ready:
                await asyncio.sleep(1.0)  # Fallback

        # Now try to proceed to checkout from cart page
        for attempt in range(MAX_RETRIES):
            if await self._find_and_click(
                page,
                self.SELECTORS["cart_checkout"],
                "amazon_cart_checkout_click",
                timeout=self.TIMEOUTS["element_visible"]
            ):
                # Event-driven wait: Wait for checkout page elements
                checkout_ready = await self._wait_for_element(
                    page, "checkout_ready", timeout=TIMEOUT_MS_CHECKOUT_READY
                )
                if checkout_ready:
                    await self._log_step("checkout_ready", "Checkout page ready from cart", {"selector": checkout_ready})
                else:
                    await asyncio.sleep(1.0)  # Fallback

                self._update_state(FlowState.ON_CHECKOUT_PAGE)
                return FlowResult(
                    success=True,
                    state=FlowState.ON_CHECKOUT_PAGE,
                    message="Proceeded to checkout from cart",
                    details={}
                )

            # Navigate to cart if not there
            try:
                await page.goto("https://www.amazon.com/gp/cart/view.html", timeout=self.TIMEOUTS["page_load"])
                # Event-driven wait for cart page
                await self._wait_for_element(page, "cart_checkout", timeout=TIMEOUT_MS_BUYBOX_READY)
            except Exception:
                pass

            await asyncio.sleep(DELAY_SECONDS_RETRY)

        await self._handle_error(page, "proceed_to_checkout", "Could not proceed to checkout")
        return FlowResult(
            success=False,
            state=FlowState.ERROR,
            message="Could not proceed to checkout",
            details={}
        )

    async def _step_place_order(self, page: Page) -> FlowResult:
        """Step 5: Place the order (with optional confirmation gate)."""
        self._update_state(FlowState.PLACING_ORDER)

        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "amazon_place_order_start",
                url=page.url
            )
        )

        # Event-driven wait: Wait for checkout page to be ready (Place Order button visible)
        place_order_found = await self._wait_for_element(
            page, "place_order", timeout=self.TIMEOUTS["checkout_load"]
        )

        if not place_order_found:
            await self._handle_error(page, "place_order", "Place Order button not found")
            return FlowResult(
                success=False,
                state=FlowState.ERROR,
                message="Place Order button not found",
                details={}
            )

        # Safety switch: if CONFIRM_FINAL_ORDER is true, stop and wait for operator
        if self.confirm_final_order:
            self._update_state(FlowState.ORDER_PENDING_CONFIRMATION)

            await event_broker.publish(
                event_broker.create_event(
                    EventType.ORDER_PENDING,
                    "amazon_order_pending_confirmation",
                    url=page.url,
                    details={
                        "message": "Order ready - waiting for operator confirmation via noVNC",
                        "action_required": "Click 'Place your order' button in noVNC viewer"
                    }
                )
            )

            await event_broker.publish(
                event_broker.create_event(
                    EventType.ACTION_REQUIRED,
                    "operator_action_required",
                    url=page.url,
                    details={
                        "action": "Click 'Place your order' button",
                        "reason": "CONFIRM_FINAL_ORDER=true"
                    }
                )
            )

            # Wait for order confirmation page (operator will click)
            confirmation_found = await self._wait_for_any(
                page,
                self.SELECTORS["order_confirmation"],
                timeout=int(TIMEOUT_SECONDS_ORDER_CONFIRM * 1000)  # Convert to ms
            )

            if confirmation_found:
                self._update_state(FlowState.ORDER_PLACED)
                await event_broker.publish(
                    event_broker.create_event(
                        EventType.ORDER_PLACED,
                        "amazon_order_placed",
                        url=page.url,
                        details={"confirmation_selector": confirmation_found}
                    )
                )
                return FlowResult(
                    success=True,
                    state=FlowState.ORDER_PLACED,
                    message="Order placed successfully (operator confirmed)",
                    details={}
                )
            else:
                return FlowResult(
                    success=False,
                    state=FlowState.ORDER_PENDING_CONFIRMATION,
                    message="Timeout waiting for operator confirmation",
                    details={}
                )

        # Automatic order placement (CONFIRM_FINAL_ORDER=false)
        if await self._find_and_click(
            page,
            self.SELECTORS["place_order"],
            "amazon_place_order_click",
            timeout=5000
        ):
            # Event-driven wait: Wait for order confirmation page
            confirmation_found = await self._wait_for_any(
                page,
                self.SELECTORS["order_confirmation"],
                timeout=self.TIMEOUTS["order_confirmation"]
            )

            if confirmation_found:
                self._update_state(FlowState.ORDER_PLACED)
                await event_broker.publish(
                    event_broker.create_event(
                        EventType.ORDER_PLACED,
                        "amazon_order_placed",
                        url=page.url,
                        details={"automatic": True}
                    )
                )
                return FlowResult(
                    success=True,
                    state=FlowState.ORDER_PLACED,
                    message="Order placed successfully",
                    details={}
                )

        await self._handle_error(page, "place_order", "Failed to place order")
        return FlowResult(
            success=False,
            state=FlowState.ERROR,
            message="Failed to place order",
            details={}
        )


class AmazonWorker:
    """Worker that processes Amazon URLs from a queue."""

    def __init__(self, url_queue: asyncio.Queue, confirm_final_order: bool = True):
        self.url_queue = url_queue
        self.confirm_final_order = confirm_final_order
        self._is_running = False
        self._is_paused = False

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    def pause(self) -> None:
        self._is_paused = True
        event_broker.current_state = BotState.PAUSED

    def resume(self) -> None:
        self._is_paused = False

    async def start(self) -> None:
        """Start processing URLs from the queue."""
        self._is_running = True

        await event_broker.publish(
            event_broker.create_event(
                EventType.STEP,
                "amazon_worker_started",
                details={"confirm_final_order": self.confirm_final_order}
            )
        )

        while self._is_running and browser_manager.is_running:
            # Check if paused
            while self._is_paused and self._is_running:
                await asyncio.sleep(1)

            try:
                # Get next URL from queue (with timeout to check running status)
                try:
                    item = await asyncio.wait_for(
                        self.url_queue.get(),
                        timeout=5.0
                    )
                except asyncio.TimeoutError:
                    continue

                url = item.get("url")
                message_info = item.get("message")
                parsed_data = item.get("parsed", {})
                expected_price = parsed_data.get("price")

                if url:
                    flow = AmazonFlow(confirm_final_order=self.confirm_final_order)
                    result = await flow.execute(url, message_info, expected_price=expected_price)

                    # Update activity item with result
                    message_id = message_info.get("message_id", "") if message_info else item.get("message", {}).get("message_id", "")
                    if message_id:
                        from app.activity_store import update_activity_result
                        update_activity_result(
                            message_id=message_id,
                            result_status="success" if result.success else "failure",
                            result_message=result.message,
                            result_details=result.details
                        )

                    # Log result
                    await event_broker.publish(
                        event_broker.create_event(
                            EventType.STEP,
                            "amazon_flow_complete",
                            url=url,
                            details={
                                "success": result.success,
                                "state": result.state.value,
                                "message": result.message,
                                "message_id": message_id
                            }
                        )
                    )

                    # Clean up Amazon page after flow
                    await browser_manager.close_amazon_page()

                    # Return to monitoring state
                    if result.success or result.state != FlowState.ERROR:
                        event_broker.current_state = BotState.DISCORD_MONITORING

            except Exception as e:
                await event_broker.publish(
                    event_broker.create_event(
                        EventType.ERROR,
                        "amazon_worker_error",
                        details={"error": str(e)}
                    )
                )

    def stop(self) -> None:
        """Stop the worker."""
        self._is_running = False
