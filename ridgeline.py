import os
import json
import time
import logging
import requests
from datetime import datetime, timezone

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RIDGELINE] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ridgeline")

# --- Config from environment ---
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET  = os.environ.get("ALPACA_SECRET")
ALPACA_BASE    = "https://paper-api.alpaca.markets/v2"
CYCLE_MINUTES  = int(os.environ.get("CYCLE_MINUTES", "15"))

HEADERS_ALPACA = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json"
}

# --- Alpaca Helpers ---
def get_account():
    r = requests.get(f"{ALPACA_BASE}/account", headers=HEADERS_ALPACA)
    r.raise_for_status()
    return r.json()

def get_positions():
    r = requests.get(f"{ALPACA_BASE}/positions", headers=HEADERS_ALPACA)
    r.raise_for_status()
    return r.json()

def get_latest_price(symbol):
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest"
    r = requests.get(url, headers=HEADERS_ALPACA)
    if r.status_code == 200:
        return float(r.json()["trade"]["p"])
    return None

def place_order(symbol, qty, side):
    payload = {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "type": "market",
        "time_in_force": "day"
    }
    r = requests.post(f"{ALPACA_BASE}/orders", headers=HEADERS_ALPACA, json=payload)
    if r.status_code in (200, 201):
        log.info(f"ORDER PLACED: {side.upper()} {qty}x {symbol}")
        return r.json()
    else:
        log.warning(f"ORDER FAILED: {symbol} {side} — {r.text}")
        return None

def is_market_open():
    r = requests.get(f"{ALPACA_BASE}/clock", headers=HEADERS_ALPACA)
    r.raise_for_status()
    return r.json().get("is_open", False)

# --- Claude Brain ---
def ask_claude(account, positions):
    cash = float(account.get("cash", 0))
    portfolio_value = float(account.get("portfolio_value", 0))
    start_value = 100000.0
    pnl = portfolio_value - start_value
    pnl_pct = (pnl / start_value) * 100

    position_summary = "No open positions"
    if positions:
        position_summary = "; ".join([
            f"{p['symbol']}: {p['qty']} shares @ avg ${float(p['avg_entry_price']):.2f}, current ${float(p['current_price']):.2f}"
            for p in positions
        ])

    prompt = f"""You are RIDGELINE, an autonomous paper trading agent with a calculated-risk personality (between balanced and aggressive).

Current portfolio state:
- Cash available: ${cash:.2f}
- Total portfolio value: ${portfolio_value:.2f}
- P&L vs start: {"+" if pnl >= 0 else ""}${pnl:.2f} ({"+" if pnl_pct >= 0 else ""}{pnl_pct:.2f}%)
- Open positions: {position_summary}
- Time: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

Goal: Grow a $250 seed investment by making smart trading decisions over time.

You have FULL FREEDOM to reason across the entire investing spectrum — large caps, mid caps, small caps, ETFs, sector plays, momentum trades, value plays, or anything else you believe offers the best risk-adjusted returns.

Small cap stocks ($1-$5 range on NYSE/NASDAQ) are worth keeping on your radar for potential high-percentage swings, but never force them — only act when conviction is high.

Rules:
- Only trade real US stocks or ETFs listed on NYSE/NASDAQ (no OTC or pink sheets)
- Never spend more than 35% of available cash on a single trade
- Always leave at least $1000 cash reserve
- Only SELL symbols that are in open positions
- Be specific about quantities and use realistic current market prices
- Reason freely — your job is best returns, not following a rigid strategy

Respond ONLY with valid JSON, no other text:
{{
  "reasoning": "2-3 sentence market analysis and strategy rationale",
  "outlook": "bullish|bearish|neutral",
  "actions": [
    {{
      "type": "BUY|SELL|HOLD",
      "symbol": "TICKER",
      "shares": <integer>,
      "price": <estimated current price as float>,
      "reason": "brief reason"
    }}
  ]
}}"""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}]
        }
    )
    response.raise_for_status()
    text = response.json()["content"][0]["text"]
    clean = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

# --- Main Cycle ---
def run_cycle(cycle_num):
    log.info(f"=== CYCLE {cycle_num} STARTED ===")

    if not is_market_open():
        log.info("Market is closed. Skipping trading cycle.")
        return

    account   = get_account()
    positions = get_positions()

    log.info(f"Portfolio: ${float(account['portfolio_value']):.2f} | Cash: ${float(account['cash']):.2f} | Positions: {len(positions)}")

    decision = ask_claude(account, positions)

    log.info(f"OUTLOOK: {decision['outlook'].upper()}")
    log.info(f"REASONING: {decision['reasoning']}")

    position_map = {p["symbol"]: p for p in positions}

    for action in decision.get("actions", []):
        atype  = action.get("type", "").upper()
        symbol = action.get("symbol", "")
        shares = int(action.get("shares", 0))
        price  = float(action.get("price", 0))
        reason = action.get("reason", "")

        if not symbol or shares <= 0:
            continue

        if atype == "BUY":
            cash = float(account["cash"])
            cost = shares * price
            reserve = 1000
            if cost > (cash - reserve):
                log.warning(f"SKIPPING BUY {symbol} — insufficient cash (need ${cost:.2f}, have ${cash - reserve:.2f} after reserve)")
                continue
            log.info(f"BUY {shares}x {symbol} @ ~${price:.2f} | Reason: {reason}")
            place_order(symbol, shares, "buy")

        elif atype == "SELL":
            if symbol not in position_map:
                log.warning(f"SKIPPING SELL {symbol} — no open position")
                continue
            held = int(float(position_map[symbol]["qty"]))
            qty  = min(shares, held)
            log.info(f"SELL {qty}x {symbol} @ ~${price:.2f} | Reason: {reason}")
            place_order(symbol, qty, "sell")

        elif atype == "HOLD":
            log.info(f"HOLD {symbol} — {reason}")

    log.info(f"=== CYCLE {cycle_num} COMPLETE ===\n")

# --- Entry Point ---
def main():
    log.info("RIDGELINE AI TRADING AGENT — STARTING UP")
    log.info(f"Cycle interval: every {CYCLE_MINUTES} minutes")
    log.info("Paper trading mode — no real money at risk\n")

    cycle = 1
    while True:
        try:
            run_cycle(cycle)
        except Exception as e:
            log.error(f"Cycle {cycle} error: {e}")
        cycle += 1
        log.info(f"Sleeping {CYCLE_MINUTES} minutes until next cycle...")
        time.sleep(CYCLE_MINUTES * 60)

if __name__ == "__main__":
    main()
