"""
Simple Web UI for managing purchase rules.
Runs on port 8001.
"""

import json
from pathlib import Path
from typing import List
from dataclasses import dataclass, asdict

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.activity_store import load_activity


RULES_FILE = Path("/data/rules.json")


@dataclass
class Rule:
    """A purchase rule with keywords and max price."""
    keywords: List[str]
    max_price: float
    rule_type: str = "blacklist"  # "whitelist" or "blacklist"

    def to_dict(self):
        return asdict(self)


class RuleCreate(BaseModel):
    """Request model for creating a rule."""
    keywords: str  # Comma-separated
    max_price: float
    rule_type: str = "blacklist"


def load_rules() -> List[Rule]:
    """Load rules from file."""
    if RULES_FILE.exists():
        try:
            with open(RULES_FILE, "r") as f:
                data = json.load(f)
                rules = []
                for r in data:
                    # Migration: convert old 'enabled' field to new 'rule_type' field
                    if "enabled" in r and "rule_type" not in r:
                        r["rule_type"] = "blacklist"
                    r.pop("enabled", None)  # Remove old field
                    rules.append(Rule(**r))
                return rules
        except Exception:
            pass
    return []


def save_rules(rules: List[Rule]) -> None:
    """Save rules to file."""
    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RULES_FILE, "w") as f:
        json.dump([r.to_dict() for r in rules], f, indent=2)


def get_whitelist_rules() -> List[Rule]:
    """Get only whitelist rules."""
    return [r for r in load_rules() if r.rule_type == "whitelist"]


def get_blacklist_rules() -> List[Rule]:
    """Get only blacklist rules."""
    return [r for r in load_rules() if r.rule_type == "blacklist"]


# Create FastAPI app for rules UI
rules_app = FastAPI(title="Purchase Rules Manager")


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Purchase Rules Manager</title>
    <style>
        * {
            box-sizing: border-box;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }
        body {
            margin: 0;
            padding: 20px;
            background: #1a1a2e;
            color: #eee;
            min-height: 100vh;
        }
        .container {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            max-width: 1400px;
            margin: 0 auto;
        }
        @media (max-width: 900px) {
            .container {
                grid-template-columns: 1fr;
            }
        }
        .panel {
            background: #16213e;
            border-radius: 8px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            max-height: calc(100vh - 40px);
        }
        .panel-content {
            flex: 1;
            overflow-y: auto;
            min-height: 0;
        }
        h2 {
            color: #00d4ff;
            margin-top: 0;
            border-bottom: 2px solid #00d4ff;
            padding-bottom: 10px;
            font-size: 1.3em;
        }
        .rule-card {
            background: #0f0f23;
            border-radius: 8px;
            padding: 8px 12px;
            margin-bottom: 8px;
            border-left: 4px solid #00d4ff;
        }
        .rule-content {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
        }
        .rule-info {
            flex: 1;
            min-width: 0;
            display: flex;
            align-items: center;
            gap: 15px;
        }
        .rule-actions {
            display: flex;
            gap: 8px;
            align-items: center;
            flex-shrink: 0;
        }
        label {
            display: block;
            margin-bottom: 5px;
            color: #aaa;
            font-size: 0.9em;
        }
        input[type="text"], input[type="number"] {
            width: 100%;
            padding: 10px;
            border: 1px solid #333;
            border-radius: 4px;
            background: #0f0f23;
            color: #eee;
            margin-bottom: 10px;
        }
        input[type="text"]:focus, input[type="number"]:focus {
            outline: none;
            border-color: #00d4ff;
        }
        .input-row {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 15px;
        }
        button {
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.9em;
            transition: background 0.2s;
        }
        .btn-primary {
            background: #00d4ff;
            color: #000;
        }
        .btn-primary:hover {
            background: #00a8cc;
        }
        .btn-danger {
            background: #ff4757;
            color: #fff;
        }
        .btn-danger:hover {
            background: #cc3a47;
        }
        .btn-small {
            padding: 5px 10px;
            font-size: 0.8em;
        }
        .add-form {
            background: #0f0f23;
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 15px;
            border: 2px dashed #333;
            flex-shrink: 0;
        }
        .add-form h3 {
            margin-top: 0;
            color: #00d4ff;
            font-size: 1em;
        }
        .empty-state {
            text-align: center;
            padding: 30px;
            color: #666;
        }
        .status {
            padding: 10px;
            border-radius: 4px;
            margin-bottom: 15px;
            display: none;
        }
        .status.success {
            background: #00d4ff22;
            border: 1px solid #00d4ff;
            color: #00d4ff;
            display: block;
        }
        .status.error {
            background: #ff475722;
            border: 1px solid #ff4757;
            color: #ff4757;
            display: block;
        }
        .keywords-display {
            display: flex;
            flex-wrap: wrap;
            gap: 5px;
            margin-bottom: 0;
            flex: 1;
        }
        .keyword-tag {
            background: #00d4ff33;
            color: #00d4ff;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.8em;
        }
        .price-display {
            font-size: 0.95em;
            color: #4cd137;
            min-width: 80px;
            text-align: right;
        }
        /* Modal Styles */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.7);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        .modal-overlay.active {
            display: flex;
        }
        .modal-content {
            background: #16213e;
            border-radius: 8px;
            padding: 25px;
            max-width: 500px;
            width: 90%;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            border-bottom: 2px solid #00d4ff;
            padding-bottom: 10px;
        }
        .modal-header h3 {
            margin: 0;
            color: #00d4ff;
        }
        .btn-close {
            background: transparent;
            border: none;
            color: #aaa;
            font-size: 1.5em;
            cursor: pointer;
            padding: 0;
            width: 30px;
            height: 30px;
        }
        .btn-close:hover {
            color: #fff;
        }
        /* Section Headers */
        .rules-section {
            margin-bottom: 20px;
        }
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .section-title {
            color: #00d4ff;
            font-size: 1em;
            font-weight: 600;
            margin: 0;
        }
        .btn-add {
            background: transparent;
            border: 1px solid #00d4ff;
            color: #00d4ff;
            width: 28px;
            height: 28px;
            padding: 0;
            border-radius: 4px;
            font-size: 1.2em;
            line-height: 1;
        }
        .btn-add:hover {
            background: #00d4ff;
            color: #000;
        }
        /* X Delete Button */
        .btn-delete-x {
            background: transparent;
            border: 1px solid #ff4757;
            color: #ff4757;
            width: 28px;
            height: 28px;
            padding: 0;
            border-radius: 4px;
            font-size: 1.1em;
            line-height: 1;
        }
        .btn-delete-x:hover {
            background: #ff4757;
            color: #fff;
        }
        /* Activity Feed Styles */
        .activity-feed {
            flex: 1;
            overflow-y: auto;
            min-height: 0;
        }
        .feed-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        .connection-status {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.85em;
        }
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #666;
        }
        .status-dot.connected {
            background: #4cd137;
            box-shadow: 0 0 8px #4cd137;
        }
        .status-dot.disconnected {
            background: #ff4757;
        }
        .feed-item {
            background: #0f0f23;
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 10px;
            border-left: 4px solid #666;
            animation: slideIn 0.3s ease;
        }
        @keyframes slideIn {
            from {
                opacity: 0;
                transform: translateX(20px);
            }
            to {
                opacity: 1;
                transform: translateX(0);
            }
        }
        .feed-item.triggered {
            border-left-color: #4cd137;
            background: #4cd13711;
        }
        .feed-item.not-triggered {
            border-left-color: #ffa502;
        }
        .feed-item.error {
            border-left-color: #ff4757;
        }
        .feed-item-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 8px;
        }
        .feed-item-meta {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .feed-item-time {
            font-size: 0.75em;
            color: #666;
            white-space: nowrap;
        }
        .feed-channel {
            font-size: 0.7em;
            color: #7289da;
            background: #7289da22;
            padding: 2px 6px;
            border-radius: 3px;
            white-space: nowrap;
        }
        .feed-item-product {
            font-weight: 500;
            color: #eee;
            font-size: 0.95em;
            line-height: 1.3;
            margin-bottom: 8px;
            word-break: break-word;
        }
        .feed-item-product a:hover {
            color: #00d4ff !important;
            text-decoration: underline !important;
        }
        .feed-item-details {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            font-size: 0.85em;
        }
        .feed-price {
            color: #4cd137;
            font-weight: 600;
        }
        .feed-discount {
            color: #ffa502;
        }
        .feed-verdict {
            padding: 2px 8px;
            border-radius: 3px;
            font-size: 0.8em;
            font-weight: 500;
        }
        .feed-verdict.trigger {
            background: #4cd13733;
            color: #4cd137;
        }
        .feed-verdict.no-trigger {
            background: #ffa50233;
            color: #ffa502;
        }
        .clear-feed {
            background: transparent;
            border: 1px solid #444;
            color: #888;
            padding: 5px 10px;
            font-size: 0.8em;
        }
        .clear-feed:hover {
            border-color: #666;
            color: #aaa;
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Left Panel: Rules -->
        <div class="panel">
            <h2>Purchase Rules</h2>
            <div id="status" class="status"></div>

            <div class="panel-content" id="rules-container">
                <!-- Whitelist Rules Section -->
                <div class="rules-section">
                    <div class="section-header">
                        <h3 class="section-title">Whitelist Purchase Rules</h3>
                        <button class="btn-add" onclick="openAddModal('whitelist')" title="Add Whitelist Rule">+</button>
                    </div>
                    <div id="whitelist-rules-list"></div>
                </div>

                <!-- Blacklist Rules Section -->
                <div class="rules-section">
                    <div class="section-header">
                        <h3 class="section-title">Blacklist Purchase Rules</h3>
                        <button class="btn-add" onclick="openAddModal('blacklist')" title="Add Blacklist Rule">+</button>
                    </div>
                    <div id="blacklist-rules-list"></div>
                </div>
            </div>
        </div>

        <!-- Modal for Adding Rules -->
        <div class="modal-overlay" id="add-rule-modal">
            <div class="modal-content">
                <div class="modal-header">
                    <h3>Add New Rule</h3>
                    <button class="btn-close" onclick="closeAddModal()">&times;</button>
                </div>
                <div class="input-row">
                    <div>
                        <label for="modal-keywords">Keywords (comma-separated)</label>
                        <input type="text" id="modal-keywords" placeholder="e.g., 5080, rtx">
                    </div>
                    <div>
                        <label for="modal-max-price">Max Price ($)</label>
                        <input type="number" id="modal-max-price" step="0.01" min="0" placeholder="1000.00">
                    </div>
                </div>
                <input type="hidden" id="modal-rule-type" value="blacklist">
                <button class="btn-primary" onclick="addRuleFromModal()">Add Rule</button>
            </div>
        </div>

        <!-- Right Panel: Activity Feed -->
        <div class="panel">
            <div class="feed-header">
                <h2>Live Activity Feed</h2>
                <div style="display: flex; align-items: center; gap: 15px;">
                    <div class="connection-status">
                        <span class="status-dot" id="connection-dot"></span>
                        <span id="connection-text">Connecting...</span>
                    </div>
                    <button class="clear-feed" onclick="clearFeed()">Clear</button>
                </div>
            </div>
            <div class="activity-feed" id="activity-feed">
                <div class="empty-state" id="feed-empty">Waiting for activity...</div>
            </div>
        </div>
    </div>

    <script>
        const API_BASE = '';
        const EVENTS_URL = window.location.protocol + '//' + window.location.hostname + ':8000/events';
        let eventSource = null;
        const MAX_FEED_ITEMS = 50;

        // Rules Management
        let currentRuleType = 'blacklist';

        function showStatus(message, isError = false) {
            const status = document.getElementById('status');
            status.textContent = message;
            status.className = 'status ' + (isError ? 'error' : 'success');
            setTimeout(() => { status.style.display = 'none'; }, 3000);
        }

        function openAddModal(ruleType) {
            currentRuleType = ruleType;
            document.getElementById('modal-rule-type').value = ruleType;
            document.getElementById('modal-keywords').value = '';
            document.getElementById('modal-max-price').value = '';
            document.getElementById('add-rule-modal').classList.add('active');
        }

        function closeAddModal() {
            document.getElementById('add-rule-modal').classList.remove('active');
        }

        async function loadRules() {
            try {
                const response = await fetch(API_BASE + '/api/rules');
                const rules = await response.json();
                const whitelistRules = rules.filter(r => r.rule_type === 'whitelist');
                const blacklistRules = rules.filter(r => r.rule_type === 'blacklist');
                renderRuleList('whitelist-rules-list', whitelistRules, rules);
                renderRuleList('blacklist-rules-list', blacklistRules, rules);
            } catch (error) {
                showStatus('Failed to load rules: ' + error.message, true);
            }
        }

        function renderRuleList(containerId, rules, allRules) {
            const container = document.getElementById(containerId);
            if (rules.length === 0) {
                container.innerHTML = '<div class="empty-state" style="padding: 15px;">No rules configured.</div>';
                return;
            }

            container.innerHTML = rules.map(rule => {
                const globalIndex = allRules.indexOf(rule);
                return `
                    <div class="rule-card">
                        <div class="rule-content">
                            <div class="rule-info">
                                <div class="keywords-display">
                                    ${rule.keywords.map(k => `<span class="keyword-tag">${k}</span>`).join('')}
                                </div>
                                <div class="price-display">$${rule.max_price.toFixed(2)}</div>
                            </div>
                            <div class="rule-actions">
                                <button class="btn-delete-x" onclick="deleteRuleQuick(${globalIndex})" title="Delete">&times;</button>
                            </div>
                        </div>
                    </div>
                `;
            }).join('');
        }

        async function addRuleFromModal() {
            const keywords = document.getElementById('modal-keywords').value.trim();
            const maxPrice = parseFloat(document.getElementById('modal-max-price').value);
            const ruleType = document.getElementById('modal-rule-type').value;

            if (!keywords) { showStatus('Please enter keywords', true); return; }
            if (isNaN(maxPrice) || maxPrice <= 0) { showStatus('Please enter valid price', true); return; }

            try {
                const response = await fetch(API_BASE + '/api/rules', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ keywords, max_price: maxPrice, rule_type: ruleType })
                });
                if (!response.ok) throw new Error('Failed');

                closeAddModal();
                showStatus('Rule added');
                loadRules();
            } catch (error) {
                showStatus('Failed to add rule', true);
            }
        }

        async function deleteRuleQuick(index) {
            try {
                await fetch(API_BASE + '/api/rules/' + index, { method: 'DELETE' });
                showStatus('Rule deleted');
                loadRules();
            } catch (error) {
                showStatus('Failed to delete', true);
            }
        }

        // Close modal on background click
        document.addEventListener('click', (e) => {
            if (e.target.id === 'add-rule-modal') {
                closeAddModal();
            }
        });

        // Activity Feed
        function setConnectionStatus(connected) {
            const dot = document.getElementById('connection-dot');
            const text = document.getElementById('connection-text');
            if (connected) {
                dot.className = 'status-dot connected';
                text.textContent = 'Connected';
            } else {
                dot.className = 'status-dot disconnected';
                text.textContent = 'Disconnected';
            }
        }

        function formatTime(isoString) {
            const date = new Date(isoString);
            return date.toLocaleTimeString();
        }

        function addFeedItem(data) {
            const feed = document.getElementById('activity-feed');
            const empty = document.getElementById('feed-empty');
            if (empty) empty.remove();

            const details = data.details || {};
            const product = details.product || details.text || 'Unknown item';
            const channel = details.channel || '';
            const amazonUrls = details.amazon_urls || [];
            const productUrl = amazonUrls.length > 0 ? amazonUrls[0] : '';

            // Debug logging
            console.log('Feed item data:', {
                step: data.step,
                product: product,
                amazonUrls: amazonUrls,
                productUrl: productUrl,
                fullDetails: details
            });

            // Check for duplicate (same product AND same channel as first item)
            const firstItem = feed.querySelector('.feed-item');
            if (firstItem && firstItem.dataset.product) {
                const firstProduct = firstItem.dataset.product;
                const firstChannel = firstItem.dataset.channel || '';
                if (product.substring(0, 100) === firstProduct && channel === firstChannel) {
                    return; // Skip duplicate
                }
            }

            // Check for NO MATCH first to avoid false positives (e.g., "no_rule_matched" contains "rule_matched")
            const isNoMatch = data.step && (data.step.includes('no_match') || data.step.includes('no_rule'));
            const isTriggered = !isNoMatch && data.step && (data.step.includes('would_trigger') || data.step.includes('rule_matched'));

            let itemClass = '';
            let verdictClass = '';
            let verdictText = '';

            if (isTriggered) {
                itemClass = 'triggered';
                verdictClass = 'trigger';
                // Show "MATCHED" for live mode, "WOULD TRIGGER" for dry run
                verdictText = data.step && data.step.includes('rule_matched') ? 'MATCHED' : 'WOULD TRIGGER';
            } else if (isNoMatch) {
                itemClass = 'not-triggered';
                verdictClass = 'no-trigger';
                verdictText = 'NO MATCH';
            }

            const price = details.price !== undefined ? '$' + details.price.toFixed(2) : '';
            const discount = details.discount !== undefined ? details.discount + '% off' : '';

            const productText = escapeHtml(product.substring(0, 150)) + (product.length > 150 ? '...' : '');
            const productDisplay = productUrl
                ? `<a href="${escapeHtml(productUrl)}" target="_blank" rel="noopener noreferrer" style="color: inherit; text-decoration: none; cursor: pointer;">${productText}</a>`
                : productText;

            const item = document.createElement('div');
            item.className = 'feed-item ' + itemClass;
            item.dataset.product = product.substring(0, 100); // Store for dedup
            item.dataset.channel = channel; // Store for per-channel dedup
            item.innerHTML = `
                <div class="feed-item-header">
                    <span class="feed-verdict ${verdictClass}">${verdictText}</span>
                    <div class="feed-item-meta">
                        ${channel ? `<span class="feed-channel">${escapeHtml(channel)}</span>` : ''}
                        <span class="feed-item-time">${formatTime(data.ts)}</span>
                    </div>
                </div>
                <div class="feed-item-product">${productDisplay}</div>
                <div class="feed-item-details">
                    ${price ? `<span class="feed-price">${price}</span>` : ''}
                    ${discount ? `<span class="feed-discount">${discount}</span>` : ''}
                </div>
            `;

            feed.insertBefore(item, feed.firstChild);

            // Limit feed items
            while (feed.children.length > MAX_FEED_ITEMS) {
                feed.removeChild(feed.lastChild);
            }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function clearFeed() {
            const feed = document.getElementById('activity-feed');
            feed.innerHTML = '<div class="empty-state" id="feed-empty">Waiting for activity...</div>';
        }

        function connectSSE() {
            if (eventSource) {
                eventSource.close();
            }

            eventSource = new EventSource(EVENTS_URL);

            eventSource.onopen = () => {
                setConnectionStatus(true);
            };

            eventSource.onerror = () => {
                setConnectionStatus(false);
                // Reconnect after 5 seconds
                setTimeout(connectSSE, 5000);
            };

            eventSource.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    // Only show product-related events
                    if (data.step && (
                        data.step.includes('dry_run') ||
                        data.step.includes('rule_matched') ||
                        data.step.includes('no_rule') ||
                        data.step === 'discord_message'
                    )) {
                        // Skip raw discord_message if we'll get a dry_run event
                        if (data.step === 'discord_message') return;
                        addFeedItem(data);
                    }
                } catch (e) {
                    console.error('Failed to parse event:', e);
                }
            };

            // Handle specific event types
            eventSource.addEventListener('step', (event) => {
                try {
                    const data = JSON.parse(event.data);
                    // Check for rule-related events (dry_run, matched, or no match)
                    if (data.step && (
                        data.step.includes('dry_run') ||
                        data.step.includes('no_rule') ||
                        data.step === 'rule_matched'  // Exact match to avoid matching "no_rule_matched"
                    )) {
                        addFeedItem(data);
                    }
                } catch (e) {}
            });
        }

        // Load activity history from storage
        async function loadActivityHistory() {
            try {
                const response = await fetch(API_BASE + '/api/activity');
                const items = await response.json();
                // Sort by timestamp descending (newest first) and add to feed
                items.sort((a, b) => new Date(b.ts) - new Date(a.ts));
                items.forEach(item => {
                    addHistoryItem(item);
                });
            } catch (error) {
                console.error('Failed to load activity history:', error);
            }
        }

        function addHistoryItem(item) {
            const feed = document.getElementById('activity-feed');
            const empty = document.getElementById('feed-empty');
            if (empty) empty.remove();

            const thisProduct = (item.product || '').substring(0, 100);
            const thisChannel = item.channel || '';
            const amazonUrls = item.amazon_urls || [];
            const productUrl = amazonUrls.length > 0 ? amazonUrls[0] : '';

            // Debug logging
            console.log('History item data:', {
                product: thisProduct,
                amazonUrls: amazonUrls,
                productUrl: productUrl,
                fullItem: item
            });

            // Check for duplicate (same product AND same channel as first item)
            const firstItem = feed.querySelector('.feed-item');
            if (firstItem && firstItem.dataset.product) {
                const firstChannel = firstItem.dataset.channel || '';
                if (thisProduct === firstItem.dataset.product && thisChannel === firstChannel) {
                    return; // Skip duplicate
                }
            }

            const isTriggered = item.triggered;

            let itemClass = isTriggered ? 'triggered' : 'not-triggered';
            let verdictClass = isTriggered ? 'trigger' : 'no-trigger';
            let verdictText = isTriggered ? 'WOULD TRIGGER' : 'NO MATCH';

            const price = item.price !== undefined ? '$' + item.price.toFixed(2) : '';
            const discount = item.discount !== undefined && item.discount > 0 ? item.discount + '% off' : '';

            const productText = escapeHtml((item.product || '').substring(0, 150)) + ((item.product || '').length > 150 ? '...' : '');
            const productDisplay = productUrl
                ? `<a href="${escapeHtml(productUrl)}" target="_blank" rel="noopener noreferrer" style="color: inherit; text-decoration: none; cursor: pointer;">${productText}</a>`
                : productText;

            const elem = document.createElement('div');
            elem.className = 'feed-item ' + itemClass;
            elem.style.animation = 'none'; // No animation for history items
            elem.dataset.product = thisProduct; // Store for dedup
            elem.dataset.channel = thisChannel; // Store for per-channel dedup
            elem.innerHTML = `
                <div class="feed-item-header">
                    <span class="feed-verdict ${verdictClass}">${verdictText}</span>
                    <div class="feed-item-meta">
                        ${thisChannel ? `<span class="feed-channel">${escapeHtml(thisChannel)}</span>` : ''}
                        <span class="feed-item-time">${formatTime(item.ts)}</span>
                    </div>
                </div>
                <div class="feed-item-product">${productDisplay}</div>
                <div class="feed-item-details">
                    ${price ? `<span class="feed-price">${price}</span>` : ''}
                    ${discount ? `<span class="feed-discount">${discount}</span>` : ''}
                </div>
            `;

            // Add to end (items already sorted newest first)
            feed.appendChild(elem);
        }

        // Initialize
        loadRules();
        loadActivityHistory();
        connectSSE();
    </script>
</body>
</html>
"""


@rules_app.get("/", response_class=HTMLResponse)
async def rules_ui():
    """Serve the rules management UI."""
    return HTML_TEMPLATE


@rules_app.get("/api/rules")
async def get_rules():
    """Get all rules."""
    rules = load_rules()
    return [r.to_dict() for r in rules]


@rules_app.post("/api/rules")
async def create_rule(rule: RuleCreate):
    """Create a new rule."""
    rules = load_rules()

    # Parse keywords from comma-separated string
    keywords = [k.strip() for k in rule.keywords.split(",") if k.strip()]

    if not keywords:
        raise HTTPException(status_code=400, detail="At least one keyword is required")

    if rule.max_price <= 0:
        raise HTTPException(status_code=400, detail="Max price must be greater than 0")

    if rule.rule_type not in ["whitelist", "blacklist"]:
        raise HTTPException(status_code=400, detail="Invalid rule_type")

    new_rule = Rule(
        keywords=keywords,
        max_price=rule.max_price,
        rule_type=rule.rule_type
    )
    rules.append(new_rule)
    save_rules(rules)

    return {"status": "created", "rule": new_rule.to_dict()}


@rules_app.delete("/api/rules/{index}")
async def delete_rule(index: int):
    """Delete a rule by index."""
    rules = load_rules()

    if index < 0 or index >= len(rules):
        raise HTTPException(status_code=404, detail="Rule not found")

    deleted = rules.pop(index)
    save_rules(rules)

    return {"status": "deleted", "rule": deleted.to_dict()}


@rules_app.get("/api/rules/whitelist")
async def get_whitelist_rules_api():
    """Get whitelist rules only."""
    return [r.to_dict() for r in get_whitelist_rules()]


@rules_app.get("/api/rules/blacklist")
async def get_blacklist_rules_api():
    """Get blacklist rules only."""
    return [r.to_dict() for r in get_blacklist_rules()]


@rules_app.get("/api/activity")
async def get_activity():
    """Get activity history."""
    return load_activity()
