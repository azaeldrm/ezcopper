# EZCopper

**Automated Amazon checkout bot that monitors Discord channels for product drops.**

Lightning-fast automated purchasing when those limited-edition items drop in your Discord channels. EZCopper watches, clicks, and checks out before you can blink.

## What It Does

1. **Monitors Discord** - Watches your Discord channels for Amazon links
2. **Filters Smart** - Optional keyword/regex filtering for specific drops
3. **Buys Instantly** - Full automated flow: Add to Cart → Checkout → Place Order
4. **Safety First** - Optional confirmation gate before final purchase
5. **Always Ready** - Persistent browser sessions (login once, run forever)

## Quick Start

```bash
# 1. Configure
cat > .env << 'EOF'
MODE=bootstrap
DISCORD_CHANNEL_URLS=https://discord.com/channels/SERVER_ID/CHANNEL_ID
# Optional: Add multiple channels separated by commas
# DISCORD_CHANNEL_URLS=https://discord.com/channels/.../CH1,https://discord.com/channels/.../CH2
CONFIRM_FINAL_ORDER=true
POLL_INTERVAL_SECONDS=5.0
DRY_RUN=false
EOF

# 2. Bootstrap (login once)
docker compose up -d
# Open http://localhost:6080 and login to Discord + Amazon
docker compose down

# 3. Run
sed -i 's/MODE=bootstrap/MODE=run/' .env
docker compose up -d
```

## Features

- **Multi-Channel** - Monitor multiple Discord channels simultaneously
- **Rules Engine** - Web UI at `http://localhost:8001` for keyword + price filtering and visual event monitoring
- **Visual Access** - noVNC viewer at `http://localhost:6080`
- **REST API** - Status, control, and real-time events at `http://localhost:8000`
- **Live Activity Feed** - Real-time monitoring of detected products and matches
- **Safety Switch** - Pause before final order for manual confirmation (can be disabled)
- **Observability** - JSON logs, screenshots on failure, Playwright traces
- **Dockerized** - Fully containerized with persistent storage

## API Highlights

```bash
# Status
curl http://localhost:8000/status

# Live event stream (SSE)
curl -N http://localhost:8000/events

# Manual trigger (testing)
curl -X POST http://localhost:8000/actions/trigger \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.amazon.com/dp/B0XXXXXXXXX"}'

# Pause/Resume
curl -X POST http://localhost:8000/actions/pause
curl -X POST http://localhost:8000/actions/resume
```

## Architecture

```
┌─────────────────────────────────────────┐
│         Docker Container                │
│  ┌───────────────────────────────────┐  │
│  │  noVNC (port 6080) + Xvfb + x11vnc│  │
│  └───────────────────────────────────┘  │
│  ┌───────────────────────────────────┐  │
│  │  FastAPI (8000) + Rules UI (8001) │  │
│  │  Discord Watcher → Amazon Worker  │  │
│  └───────────────────────────────────┘  │
│  ┌───────────────────────────────────┐  │
│  │  Playwright Chromium Browser      │  │
│  │  Tab 1: Discord | Tab 2: Amazon   │  │
│  └───────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

## Configuration

### Basic Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `MODE` | `bootstrap` for setup, `run` for monitoring | `bootstrap` |
| `DISCORD_CHANNEL_URLS` | Comma-separated Discord channel URLs | - |
| `CONFIRM_FINAL_ORDER` | Pause before placing order | `true` |
| `POLL_INTERVAL_SECONDS` | Discord polling frequency | `5.0` |
| `DRY_RUN` | Test mode (no actual purchases) | `false` |

### Advanced Timing (milliseconds)

Optimize for speed vs. reliability. Lower values = faster but less stable.

| Variable | Description | Default |
|----------|-------------|---------|
| `TIMEOUT_MS_PAGE_LOAD` | Max wait for page load | `30000` |
| `TIMEOUT_MS_ELEMENT_VISIBLE` | Max wait for elements | `10000` |
| `TIMEOUT_MS_SELECTOR_CHECK` | Selector availability check | `150` |
| `TIMEOUT_MS_AOD_PANEL` | Amazon offers panel timeout | `10000` |
| `TIMEOUT_MS_CHECKOUT_LOAD` | Checkout page load timeout | `30000` |
| `TIMEOUT_SECONDS_ORDER_CONFIRM` | Manual order confirmation wait | `300` |
| `WAIT_SECONDS_DYNAMIC_CONTENT` | Fixed wait for dynamic content | `2.0` |
| `WAIT_SECONDS_CART_UPDATE` | Fixed wait after cart update | `2.0` |
| `WAIT_SECONDS_CHECKOUT_TRANSITION` | Fixed wait at checkout | `3.0` |
| `MAX_RETRIES` | Retry attempts on failure | `3` |
| `DELAY_SECONDS_RETRY` | Delay between retries | `0.5` |

**Example aggressive config** (faster but less reliable):
```bash
POLL_INTERVAL_SECONDS=1
TIMEOUT_MS_PAGE_LOAD=8000
TIMEOUT_MS_ELEMENT_VISIBLE=5000
WAIT_SECONDS_DYNAMIC_CONTENT=1.0
CONFIRM_FINAL_ORDER=false  # ⚠️ Auto-purchase without confirmation
```

## How It Works

**Note:** Filtering rules (keywords + max price) are configured via the Rules UI at `http://localhost:8001`, not environment variables.

**Discord Watcher** continuously polls your Discord channels for new messages. When it finds a product with an Amazon link, it checks against your rules. If a rule matches (keywords + price limit), it queues the URL.

**Amazon Worker** picks up queued URLs and executes the purchase flow through a real Chromium browser using Playwright. With persistent browser profiles, you stay logged in across restarts.

**Safety Gate**: When `CONFIRM_FINAL_ORDER=true`, the bot stops at checkout and waits for you to manually click "Place your order" via noVNC.

**Configure Rules**: Open `http://localhost:8001` to add purchase rules with keywords and max price limits. The Live Activity Feed shows all detected products and whether they matched your rules.

## Disclaimer

Educational purposes only. Use responsibly and in compliance with Discord's and Amazon's Terms of Service. Automated purchasing may violate platform terms.

## Tech Stack

Python · FastAPI · Playwright · Docker · noVNC · Xvfb

---
