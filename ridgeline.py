import os
import json
import time
import logging
import requests
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RIDGELINE] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ridgeline")

# --- Config from environment ---
CLAUDE_API_KEY  = os.environ.get("CLAUDE_API_KEY")
ALPACA_API_KEY  = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET   = os.environ.get("ALPACA_SECRET")
EMAIL_FROM      = os.environ.get("EMAIL_FROM")
EMAIL_TO        = os.environ.get("EMAIL_TO")
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD")
CYCLE_MINUTES   = int(os.environ.get("CYCLE_MINUTES", "15"))
ALPACA_BASE     = "https://paper-api.alpaca.markets/v2"
ALPACA_DATA     = "https://data.alpaca.markets/v2"

HEADERS_ALPACA  = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json"
}

# --- Performance Memory (in-memory log, persists while running) ---
performance_memory = []
daily_summary = {
    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    "trades": [],
    "start_value": None,
    "cycles": 0
}

# ─────────────────────────────────────────────
# ALPACA HELPERS
# ─────────────────────────────────────────────

def get_account():
    r = requests.get(f"{ALPACA_BASE}/account", headers=HEADERS_ALPACA)
    r.raise_for_status()
    return r.json()

def get_positions():
    r = requests.get(f"{ALPACA_BASE}/positions", headers=HEADERS_ALPACA)
    r.raise_for_status()
    return r.json()

def get_latest_price(symbol):
    try:
        url = f"{ALPACA_DATA}/stocks/{symbol}/trades/latest"
        r = requests.get(url, headers=HEADERS_ALPACA, timeout=5)
        if r.status_code == 200:
            return float(r.json()["trade"]["p"])
    except Exception:
        pass
    return None

def get_bars(symbol, timeframe="1Day", limit=20):
    try:
        url = f"{ALPACA_DATA}/stocks/{symbol}/bars"
        params = {"timeframe": timeframe, "limit": limit, "feed": "iex"}
        r = requests.get(url, headers=HEADERS_ALPACA, params=params, timeout=5)
        if r.status_code == 200:
            return r.json().get("bars", [])
    except Exception:
        pass
    return []

def is_market_open():
    r = requests.get(f"{ALPACA_BASE}/clock", headers=HEADERS_ALPACA)
    r.raise_for_status()
    return r.json().get("is_open", False)

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

# ─────────────────────────────────────────────
# TECHNICAL INDICATORS
# ─────────────────────────────────────────────

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_sma(prices, period):
    if len(prices) < period:
        return None
    return round(sum(prices[-period:]) / period, 2)

def get_technical_analysis(symbols):
    analysis = {}
    for symbol in symbols[:5]:  # Limit to avoid rate limiting
        bars = get_bars(symbol, limit=30)
        if not bars:
            continue
        closes = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]
        rsi = calculate_rsi(closes)
        sma10 = calculate_sma(closes, 10)
        sma20 = calculate_sma(closes, 20)
        avg_vol = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
        vol_spike = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1.0
        current = closes[-1] if closes else None
        trend = "neutral"
        if sma10 and sma20:
            trend = "bullish" if sma10 > sma20 else "bearish"
        analysis[symbol] = {
            "rsi": rsi,
            "sma10": sma10,
            "sma20": sma20,
            "volume_spike": vol_spike,
            "trend": trend,
            "current_price": current
        }
        log.info(f"TECHNICALS {symbol}: RSI={rsi} SMA10={sma10} SMA20={sma20} Vol={vol_spike}x Trend={trend}")
    return analysis

# ─────────────────────────────────────────────
# MARKET SENTIMENT
# ─────────────────────────────────────────────

def get_fear_greed_index():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        if r.status_code == 200:
            data = r.json()["data"][0]
            return {
                "value": int(data["value"]),
                "label": data["value_classification"]
            }
    except Exception:
        pass
    return {"value": 50, "label": "Neutral"}

def get_vix():
    # Approximated via market data — VIX not directly available on free tier
    # We'll ask Claude to factor in general volatility awareness
    return None

# ─────────────────────────────────────────────
# NEWS & WEB SEARCH
# ─────────────────────────────────────────────

def get_market_news(positions):
    """Use Claude with web search to get relevant market news"""
    held_symbols = [p["symbol"] for p in positions] if positions else []
    symbols_str = ", ".join(held_symbols) if held_symbols else "general market"

    prompt = f"""Search for the latest financial market news relevant to trading decisions today.
Focus on:
1. Major market-moving headlines from the last 24 hours
2. News about these specific stocks if held: {symbols_str}
3. Federal Reserve or economic data announcements
4. Any sector-wide events (tech, energy, healthcare, financials)

Return a concise JSON summary:
{{
  "headline_summary": "2-3 sentence overview of most important market news today",
  "market_mood": "risk-on|risk-off|neutral",
  "key_events": ["event 1", "event 2", "event 3"],
  "stock_news": {{"SYMBOL": "brief news summary"}}
}}
Respond with JSON only, no other text."""

    try:
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
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        response.raise_for_status()
        content = response.json().get("content", [])
        for block in content:
            if block.get("type") == "text":
                text = block["text"].replace("```json", "").replace("```", "").strip()
                try:
                    return json.loads(text)
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"News fetch failed: {e}")

    return {
        "headline_summary": "News unavailable this cycle",
        "market_mood": "neutral",
        "key_events": [],
        "stock_news": {}
    }

# ─────────────────────────────────────────────
# EARNINGS CALENDAR
# ─────────────────────────────────────────────

def get_earnings_context(positions):
    """Ask Claude to flag any earnings risk for held positions"""
    if not positions:
        return "No positions to check for earnings risk."
    symbols = [p["symbol"] for p in positions]
    prompt = f"""Quickly check: do any of these stocks have earnings announcements in the next 3 days? {', '.join(symbols)}
Respond with JSON only:
{{"earnings_risk": {{"SYMBOL": "earnings date or none"}}}}"""
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=20
        )
        content = response.json().get("content", [])
        for block in content:
            if block.get("type") == "text":
                text = block["text"].replace("```json", "").replace("```", "").strip()
                try:
                    return json.loads(text).get("earnings_risk", {})
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"Earnings check failed: {e}")
    return {}

# ─────────────────────────────────────────────
# PERFORMANCE MEMORY
# ─────────────────────────────────────────────

def update_memory(cycle_num, actions, portfolio_value, pnl):
    global performance_memory
    entry = {
        "cycle": cycle_num,
        "time": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "portfolio_value": round(portfolio_value, 2),
        "pnl": round(pnl, 2),
        "actions": actions
    }
    performance_memory.append(entry)
    # Keep last 50 cycles in memory
    performance_memory = performance_memory[-50:]

def get_memory_summary():
    if not performance_memory:
        return "No trading history yet."
    recent = performance_memory[-10:]
    total_trades = sum(len(e["actions"]) for e in recent)
    pnl_trend = [e["pnl"] for e in recent]
    trend = "improving" if len(pnl_trend) > 1 and pnl_trend[-1] > pnl_trend[0] else "declining" if len(pnl_trend) > 1 and pnl_trend[-1] < pnl_trend[0] else "flat"
    last_actions = []
    for e in recent[-3:]:
        for a in e["actions"]:
            last_actions.append(f"Cycle {e['cycle']}: {a.get('type')} {a.get('symbol')} — {a.get('reason', '')}")
    return f"P&L trend over last {len(recent)} cycles: {trend}. Recent trades: {'; '.join(last_actions[-5:]) if last_actions else 'none'}"

# ─────────────────────────────────────────────
# RISK MANAGEMENT
# ─────────────────────────────────────────────

def check_risk_limits(account, positions):
    warnings = []
    portfolio_value = float(account.get("portfolio_value", 100000))
    cash = float(account.get("cash", 0))

    # Check if any single position is more than 40% of portfolio
    for p in positions:
        market_value = float(p.get("market_value", 0))
        pct = (market_value / portfolio_value) * 100
        if pct > 40:
            warnings.append(f"CONCENTRATION RISK: {p['symbol']} is {pct:.1f}% of portfolio — consider trimming")

    # Check if unrealized losses exceed 5% of portfolio
    total_unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    if total_unrealized < -(portfolio_value * 0.05):
        warnings.append(f"DRAWDOWN ALERT: Unrealized losses of ${abs(total_unrealized):.2f} exceed 5% threshold")

    # Check cash reserve
    if cash < 1000:
        warnings.append(f"CASH RESERVE LOW: Only ${cash:.2f} available — limit new buys")

    return warnings

# ─────────────────────────────────────────────
# CLAUDE BRAIN (FULL ENHANCED)
# ─────────────────────────────────────────────

def ask_claude(account, positions, news, technicals, fear_greed, earnings, risk_warnings, memory_summary):
    cash = float(account.get("cash", 0))
    portfolio_value = float(account.get("portfolio_value", 0))
    start_value = 100000.0
    pnl = portfolio_value - start_value
    pnl_pct = (pnl / start_value) * 100

    position_summary = "No open positions"
    if positions:
        position_summary = "\n".join([
            f"  {p['symbol']}: {p['qty']} shares @ avg ${float(p['avg_entry_price']):.2f}, "
            f"current ${float(p['current_price']):.2f}, "
            f"P&L: ${float(p['unrealized_pl']):.2f} ({float(p['unrealized_plpc'])*100:.1f}%)"
            for p in positions
        ])

    tech_summary = "No technical data available"
    if technicals:
        tech_summary = "\n".join([
            f"  {sym}: RSI={d['rsi']} Trend={d['trend']} Vol={d['volume_spike']}x current=${d['current_price']}"
            for sym, d in technicals.items()
        ])

    risk_text = "\n".join(risk_warnings) if risk_warnings else "No risk warnings"
    earnings_text = json.dumps(earnings) if earnings else "None identified"

    prompt = f"""You are RIDGELINE, an elite autonomous paper trading agent with a calculated-risk personality (between balanced and aggressive). You have full freedom to trade across the entire market spectrum.

═══════════════════════════════════════
PORTFOLIO STATE
═══════════════════════════════════════
Cash available: ${cash:.2f}
Total portfolio value: ${portfolio_value:.2f}
P&L vs start: {"+" if pnl >= 0 else ""}${pnl:.2f} ({"+" if pnl_pct >= 0 else ""}{pnl_pct:.2f}%)
Time: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

Open positions:
{position_summary}

═══════════════════════════════════════
MARKET INTELLIGENCE
═══════════════════════════════════════
NEWS: {news.get("headline_summary", "N/A")}
Market mood: {news.get("market_mood", "neutral")}
Key events: {", ".join(news.get("key_events", [])) or "none"}
Stock-specific news: {json.dumps(news.get("stock_news", {}))}

Fear & Greed Index: {fear_greed["value"]}/100 ({fear_greed["label"]})

Technical Analysis:
{tech_summary}

Earnings Risk (next 3 days):
{earnings_text}

═══════════════════════════════════════
PERFORMANCE MEMORY
═══════════════════════════════════════
{memory_summary}

═══════════════════════════════════════
RISK WARNINGS
═══════════════════════════════════════
{risk_text}

═══════════════════════════════════════
TRADING RULES
═══════════════════════════════════════
- Goal: Grow $250 seed investment — maximize returns intelligently
- Full freedom: large caps, small caps, ETFs, sector plays, momentum, value
- Keep small caps ($1-$5 NYSE/NASDAQ) on radar for high-percentage swings
- Never spend more than 35% of cash on a single trade
- Always maintain $1,000 cash reserve minimum
- Only SELL symbols currently in open positions
- Factor in all intelligence above — news, technicals, sentiment, earnings risk
- Learn from performance memory — don't repeat losing patterns
- Respect risk warnings

Respond ONLY with valid JSON:
{{
  "reasoning": "3-4 sentence analysis incorporating news, technicals, sentiment and memory",
  "outlook": "bullish|bearish|neutral",
  "confidence": "high|medium|low",
  "actions": [
    {{
      "type": "BUY|SELL|HOLD",
      "symbol": "TICKER",
      "shares": <integer>,
      "price": <float>,
      "reason": "specific reason citing data above"
    }}
  ],
  "watchlist": ["SYMBOL1", "SYMBOL2"],
  "risk_note": "any concerns or risk observations"
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
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=30
    )
    response.raise_for_status()
    text = response.json()["content"][0]["text"]
    clean = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

# ─────────────────────────────────────────────
# DAILY EMAIL REPORT
# ─────────────────────────────────────────────

def send_daily_email(account, positions, daily_summary):
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD]):
        log.info("Email not configured — skipping daily report")
        return

    portfolio_value = float(account.get("portfolio_value", 0))
    cash = float(account.get("cash", 0))
    pnl = portfolio_value - 100000.0
    pnl_pct = (pnl / 100000.0) * 100

    position_lines = ""
    for p in positions:
        position_lines += f"""
        <tr>
          <td style='padding:8px;border-bottom:1px solid #eee'><b>{p['symbol']}</b></td>
          <td style='padding:8px;border-bottom:1px solid #eee'>{p['qty']} shares</td>
          <td style='padding:8px;border-bottom:1px solid #eee'>${float(p['current_price']):.2f}</td>
          <td style='padding:8px;border-bottom:1px solid #eee;color:{"green" if float(p['unrealized_pl']) >= 0 else "red"}'>
            {"+" if float(p['unrealized_pl']) >= 0 else ""}${float(p['unrealized_pl']):.2f}
          </td>
        </tr>"""

    trade_lines = ""
    for t in daily_summary.get("trades", [])[-10:]:
        trade_lines += f"<li>{t}</li>"

    html = f"""
    <html><body style='font-family:Arial,sans-serif;max-width:600px;margin:0 auto'>
      <div style='background:#0a1628;padding:20px;border-radius:8px 8px 0 0'>
        <h1 style='color:#00ffb4;margin:0;letter-spacing:3px'>RIDGELINE</h1>
        <p style='color:rgba(255,255,255,0.5);margin:4px 0 0'>Daily Trading Report — {datetime.now(timezone.utc).strftime("%B %d, %Y")}</p>
      </div>
      <div style='background:#f9f9f9;padding:20px'>
        <h2 style='color:#333'>Portfolio Summary</h2>
        <table style='width:100%'>
          <tr>
            <td style='padding:12px;background:#fff;border-radius:6px;text-align:center;width:33%'>
              <div style='font-size:24px;font-weight:bold;color:#333'>${portfolio_value:,.2f}</div>
              <div style='color:#999;font-size:12px'>Portfolio Value</div>
            </td>
            <td style='width:10px'></td>
            <td style='padding:12px;background:#fff;border-radius:6px;text-align:center;width:33%'>
              <div style='font-size:24px;font-weight:bold;color:{"#00aa66" if pnl >= 0 else "#cc3333"}'>{"+" if pnl >= 0 else ""}${pnl:,.2f}</div>
              <div style='color:#999;font-size:12px'>Total P&L ({("+" if pnl_pct >= 0 else "")}{pnl_pct:.2f}%)</div>
            </td>
            <td style='width:10px'></td>
            <td style='padding:12px;background:#fff;border-radius:6px;text-align:center;width:33%'>
              <div style='font-size:24px;font-weight:bold;color:#333'>${cash:,.2f}</div>
              <div style='color:#999;font-size:12px'>Cash Available</div>
            </td>
          </tr>
        </table>

        <h2 style='color:#333;margin-top:24px'>Open Positions</h2>
        {"<p style='color:#999'>No open positions</p>" if not positions else f"<table style='width:100%;border-collapse:collapse'><tr style='background:#eee'><th style='padding:8px;text-align:left'>Symbol</th><th style='padding:8px;text-align:left'>Shares</th><th style='padding:8px;text-align:left'>Price</th><th style='padding:8px;text-align:left'>P&L</th></tr>{position_lines}</table>"}

        <h2 style='color:#333;margin-top:24px'>Today's Trades ({len(daily_summary.get("trades", []))})</h2>
        {"<p style='color:#999'>No trades today</p>" if not daily_summary.get("trades") else f"<ul style='color:#555'>{trade_lines}</ul>"}

        <h2 style='color:#333;margin-top:24px'>Cycles Run Today</h2>
        <p style='color:#555'>{daily_summary.get("cycles", 0)} cycles completed</p>

        <div style='margin-top:24px;padding:16px;background:#fff3cd;border-radius:6px;font-size:12px;color:#856404'>
          ⚠️ Paper trading only — no real money at risk. Not financial advice.
        </div>
      </div>
    </body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Ridgeline Daily Report — P&L: {'+'if pnl>=0 else ''}${pnl:.2f} ({('+'if pnl_pct>=0 else '')}{pnl_pct:.2f}%)"
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info(f"Daily email report sent to {EMAIL_TO}")
    except Exception as e:
        log.warning(f"Email failed: {e}")

# ─────────────────────────────────────────────
# MAIN CYCLE
# ─────────────────────────────────────────────

def run_cycle(cycle_num):
    global daily_summary
    log.info(f"=== CYCLE {cycle_num} STARTED ===")
    daily_summary["cycles"] += 1

    if not is_market_open():
        log.info("Market is closed. Sleeping until next cycle.")
        # Send daily email at market close if it's after 4pm ET
        hour_utc = datetime.now(timezone.utc).hour
        if hour_utc == 21 and daily_summary["cycles"] > 1:  # ~4pm ET
            try:
                account = get_account()
                positions = get_positions()
                send_daily_email(account, positions, daily_summary)
                # Reset daily summary
                daily_summary = {
                    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "trades": [],
                    "start_value": None,
                    "cycles": 0
                }
            except Exception as e:
                log.warning(f"End of day processing failed: {e}")
        return

    # Pull portfolio state
    account   = get_account()
    positions = get_positions()
    portfolio_value = float(account.get("portfolio_value", 100000))
    cash = float(account.get("cash", 0))

    if daily_summary["start_value"] is None:
        daily_summary["start_value"] = portfolio_value

    log.info(f"Portfolio: ${portfolio_value:.2f} | Cash: ${cash:.2f} | Positions: {len(positions)}")

    # Gather all intelligence in parallel
    log.info("Gathering market intelligence...")

    # 1. Fear & Greed
    fear_greed = get_fear_greed_index()
    log.info(f"Fear & Greed Index: {fear_greed['value']} ({fear_greed['label']})")

    # 2. Technical analysis on held positions + watchlist
    held_symbols = [p["symbol"] for p in positions]
    technicals = get_technical_analysis(held_symbols) if held_symbols else {}

    # 3. Risk check
    risk_warnings = check_risk_limits(account, positions)
    for w in risk_warnings:
        log.warning(w)

    # 4. Performance memory
    memory_summary = get_memory_summary()
    log.info(f"Memory: {memory_summary[:100]}...")

    # 5. News (uses web search)
    log.info("Fetching live market news...")
    news = get_market_news(positions)
    log.info(f"News mood: {news.get('market_mood')} | {news.get('headline_summary','')[:80]}...")

    # 6. Earnings calendar
    earnings = get_earnings_context(positions)
    if earnings:
        log.info(f"Earnings risk: {earnings}")

    # Ask Claude for decision
    log.info("Consulting Claude AI for trading decision...")
    decision = ask_claude(account, positions, news, technicals, fear_greed, earnings, risk_warnings, memory_summary)

    log.info(f"OUTLOOK: {decision.get('outlook','?').upper()} | CONFIDENCE: {decision.get('confidence','?').upper()}")
    log.info(f"REASONING: {decision.get('reasoning','')}")
    if decision.get("risk_note"):
        log.info(f"RISK NOTE: {decision['risk_note']}")
    if decision.get("watchlist"):
        log.info(f"WATCHLIST: {', '.join(decision['watchlist'])}")

    # Execute trades
    position_map = {p["symbol"]: p for p in positions}
    cycle_actions = []

    for action in decision.get("actions", []):
        atype  = action.get("type", "").upper()
        symbol = action.get("symbol", "")
        shares = int(action.get("shares", 0))
        price  = float(action.get("price", 0))
        reason = action.get("reason", "")

        if not symbol or shares <= 0:
            continue

        if atype == "BUY":
            cost = shares * price
            reserve = 1000
            if cost > (cash - reserve):
                log.warning(f"SKIPPING BUY {symbol} — insufficient cash")
                continue
            log.info(f"BUY {shares}x {symbol} @ ~${price:.2f} | {reason}")
            result = place_order(symbol, shares, "buy")
            if result:
                cash -= cost
                trade_log = f"BUY {shares}x {symbol} @ ${price:.2f} — {reason}"
                daily_summary["trades"].append(trade_log)
                cycle_actions.append(action)

        elif atype == "SELL":
            if symbol not in position_map:
                log.warning(f"SKIPPING SELL {symbol} — no position")
                continue
            held = int(float(position_map[symbol]["qty"]))
            qty  = min(shares, held)
            log.info(f"SELL {qty}x {symbol} @ ~${price:.2f} | {reason}")
            result = place_order(symbol, qty, "sell")
            if result:
                trade_log = f"SELL {qty}x {symbol} @ ${price:.2f} — {reason}"
                daily_summary["trades"].append(trade_log)
                cycle_actions.append(action)

        elif atype == "HOLD":
            log.info(f"HOLD {symbol} — {reason}")
            cycle_actions.append(action)

    # Update performance memory
    pnl = portfolio_value - 100000.0
    update_memory(cycle_num, cycle_actions, portfolio_value, pnl)

    log.info(f"=== CYCLE {cycle_num} COMPLETE | Portfolio: ${portfolio_value:.2f} | P&L: {'+'if pnl>=0 else ''}${pnl:.2f} ===\n")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main():
    log.info("╔══════════════════════════════════════╗")
    log.info("║   RIDGELINE AI TRADING AGENT v2.0    ║")
    log.info("║   Enhanced Intelligence Edition      ║")
    log.info("╚══════════════════════════════════════╝")
    log.info(f"Cycle interval: every {CYCLE_MINUTES} minutes")
    log.info(f"Features: Live News | Technicals | Sentiment | Memory | Risk Mgmt | Email Reports")
    log.info("Paper trading mode — no real money at risk\n")

    cycle = 1
    while True:
        try:
            run_cycle(cycle)
        except Exception as e:
            log.error(f"Cycle {cycle} error: {e}")
        cycle += 1
        log.info(f"Sleeping {CYCLE_MINUTES} minutes...\n")
        time.sleep(CYCLE_MINUTES * 60)

if __name__ == "__main__":
    main()
