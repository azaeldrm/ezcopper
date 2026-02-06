# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EZCopper is an automated Amazon checkout bot that monitors Discord channels for product drops and executes purchases. Built with Python, FastAPI, and Playwright for browser automation.

## Development Commands

**Docker is managed from `/srv/homelab/docker/` using `manage-service.sh`:**

```bash
# Rebuild and restart (after code changes)
# IMPORTANT: Commit and push your branch first - Docker builds from git
git add -A && git commit -m "your message" && git push
/srv/homelab/docker/manage-service.sh ezcopper d && /srv/homelab/docker/manage-service.sh ezcopper u b

# Just restart (no rebuild, e.g., for .env changes)
/srv/homelab/docker/manage-service.sh ezcopper r

# View logs
docker logs -f ezcopper

# Manual API testing
curl http://localhost:8000/status
curl -N http://localhost:8000/events  # SSE stream
curl -X POST http://localhost:8000/actions/trigger -H "Content-Type: application/json" -d '{"url": "https://www.amazon.com/dp/B0XXXXXXXXX"}'

# Access points
# - noVNC (visual browser): http://localhost:6080
# - REST API: http://localhost:8000
# - Rules UI: http://localhost:8001
```

**Testing workflow:** No test suite exists. To test code changes:
1. Commit and push to your branch
2. Rebuild the Docker image (pulls from git): `manage-service.sh ezcopper d && manage-service.sh ezcopper u b`
3. Test manually via `/actions/trigger` endpoint or monitor the event stream

## Architecture

```
app/
├── main.py           # FastAPI server + orchestrator (port 8000, 8001)
├── amazon_flow.py    # Purchase state machine (2000+ lines)
│                     # FlowState enum: OPENING_PRODUCT → ADDING_TO_CART → PROCEEDING_TO_CHECKOUT → PLACING_ORDER
├── discord_watcher.py # Multi-channel Discord monitoring via web scraping
├── browser.py        # Playwright browser lifecycle (persistent profiles)
├── events.py         # Global EventBroker for SSE streaming
├── rules_ui.py       # Web UI for purchase rules management
├── message_parser.py # Discord message parsing (Inventory Bot format)
└── activity_store.py # JSON-based activity history
```

**Key patterns:**
- Event-driven with global `EventBroker` singleton for real-time SSE updates
- Producer-consumer: Discord watcher produces URLs → async queue → Amazon worker consumes
- Persistent Chromium browser profiles at `/data/profile/` (login once in bootstrap mode)
- State machines: `BotState` (overall) and `FlowState` (per-purchase)

**Data paths:**
- `/data/profile/` - Browser profile
- `/data/rules.json` - Purchase rules
- `/data/activity.json` - Activity feed
- `/data/artifacts/` - Screenshots/traces on failure

## Modes

- **bootstrap**: Displays login pages for Discord/Amazon authentication via noVNC
- **run**: Starts Discord watcher + Amazon worker for monitoring

Set via `MODE` environment variable. Bootstrap first, then switch to run.

## Key Environment Variables

| Variable | Purpose |
|----------|---------|
| `MODE` | bootstrap or run |
| `DISCORD_CHANNEL_URLS` | Comma-separated Discord channel URLs |
| `CONFIRM_FINAL_ORDER` | Pause before purchase for manual confirmation |
| `DRY_RUN` | Test mode without actual purchases |
| `POLL_INTERVAL_SECONDS` | Discord polling frequency |
| `FAST_CHECKOUT` | Skip cart confirmation, navigate directly to checkout (default: false) |
| `FAST_CHECKOUT_DELAY_MS` | Delay after add-to-cart before checkout navigation (default: 300) |
| `TIMEOUT_MS_*` | Various timeout settings (see README.md) |

## Working with amazon_flow.py

This is the most complex module (~2000+ lines). Key classes:
- `AmazonFlow`: Single purchase flow executor with retry logic
- `AmazonWorker`: Queue processor with pause/resume
- `SellerInfo`/`PriceInfo`: Data classes for extracted product info

### AOD (Amazon Offers Dialog) Traversal Logic

The `_find_valid_amazon_offer_in_aod()` method handles offer selection:

1. **Pinned Offer Check** (featured offer at top)
   - Clicks to expand pinned offer (reveals seller info)
   - Extracts price, ships_from, sold_by using **scoped selectors** within `#aod-pinned-offer`
   - Validates: price must match expected, must ship from Amazon.com
   - If valid, selects pinned offer

2. **Offer List Traversal** (if pinned fails)
   - Iterates through `#aod-offer` elements (sorted by price ascending)
   - **Early termination**: Stops when offer price > expected price (no point checking higher prices)
   - For each offer at/below expected price, checks if ships from Amazon.com
   - First valid match is selected

**Critical implementation details:**
- All pinned offer selectors must be scoped to `pinned_offer.locator()` not `page.locator()` (multiple `#aod-offer-shipsFrom` elements exist on page)
- Price validation happens during traversal, so the separate `_step_validate_price` is skipped for AOD offers
- The `expected_price` parameter flows from the trigger request through to offer selection

### Checkout Flow

**Standard flow:**
```
Add to Cart → Wait for side panel (~2s) → Click "Proceed to Checkout" → Redirect → Checkout page
```

**Fast checkout flow** (`FAST_CHECKOUT=true`):
```
Add to Cart → Brief delay (300-500ms) → Navigate to /checkout/entry/cart?proceedToCheckout=1 → Checkout page
```

Fast checkout saves ~3 seconds by bypassing the cart confirmation side panel entirely. The checkout entry URL triggers Amazon's redirect to the actual checkout session.

### Debug Logging

Extensive `_log_step()` calls with prefixes like `debug_pinned_*`, `debug_offer_*`, `aod_*` help trace offer selection. Check docker logs when troubleshooting.

## Discord Watcher Notes

Scrapes Discord web UI via Playwright (no API). Selector changes in Discord may break parsing. The watcher supports whitelist/blacklist channel filtering and rules-based URL filtering (keywords + max price).

## Test Scenarios

**Success criteria during development:** With `CONFIRM_FINAL_ORDER=true`, success = reaching checkout screen (bot waits for manual confirmation).

**Activity data:** `/srv/homelab/docker/ezcopper/data/activity.json` contains historical runs for reference.

**Test URLs:**

| Type | ASIN | Description | Expected Behavior |
|------|------|-------------|-------------------|
| AOD (pinned offer) | `B083NBZTTN` | Funko Pop | Should succeed - pinned offer is Amazon.com |
| AOD (offer list) | `B0DGJ736JM` | Apple Watch | Pinned price mismatch → finds offer in list |
| AOD (Amazon Resale) | `B08164VTWH` | AMD Ryzen | Should succeed - Amazon Resale seller |
| AOD (price exceeded) | `B0DM6YWF6W` | NITECORE Backpack | Early termination - all offers > expected |
| Standard (3rd party) | `B09SBKXX1S` | Funko Pop Munchlax | Should fail - "Invalid shipper" |

**Trigger test (requires price for validation):**
```bash
# AOD - Pinned offer from Amazon (should succeed, selects pinned)
curl -X POST http://localhost:8000/actions/trigger -H "Content-Type: application/json" \
  -d '{"url": "https://www.amazon.com/dp/B083NBZTTN?smid=ATVPDKIKX0DER&aod=1", "price": 29.74, "product": "Funko Pop"}'

# AOD - Offer list selection (pinned price mismatch, finds match in offer list)
curl -X POST http://localhost:8000/actions/trigger -H "Content-Type: application/json" \
  -d '{"url": "https://www.amazon.com/dp/B0DGJ736JM?smid=A2L77EE7U53NWQ&aod=1", "price": 160.06, "product": "Apple Watch"}'

# AOD - Amazon Resale seller (should succeed)
curl -X POST http://localhost:8000/actions/trigger -H "Content-Type: application/json" \
  -d '{"url": "https://www.amazon.com/dp/B08164VTWH?smid=A2L77EE7U53NWQ&aod=1", "price": 257.45, "product": "AMD Ryzen"}'

# AOD - Price exceeded (early termination, no valid offer at expected price)
curl -X POST http://localhost:8000/actions/trigger -H "Content-Type: application/json" \
  -d '{"url": "https://www.amazon.com/dp/B0DM6YWF6W?smid=ATVPDKIKX0DER&aod=1", "price": 125.23, "product": "NITECORE Backpack"}'
```

**TriggerRequest fields:** `url` (required), `price` (for validation), `product` (display name), `message_id` (optional)

**URL types:**
- **AOD URLs** (`&aod=1` or `?aod=1`): Opens Amazon Offers Dialog directly
  - First checks pinned offer (price + Amazon shipper)
  - If pinned fails, traverses offer list (stops when price > expected)
  - `smid` parameter indicates seller but actual validation is done via page scraping
- **Standard URLs**: Regular product page, checks seller info from buybox directly
