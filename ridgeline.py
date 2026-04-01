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

# --- Config ---
CLAUDE_API_KEY  = os.environ.get("CLAUDE_API_KEY")
ALPACA_API_KEY  = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET   = os.environ.get("ALPACA_SECRET")
EMAIL_FROM      = os.environ.get("EMAIL_FROM")
EMAIL_TO        = os.environ.get("EMAIL_TO")
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD")
CYCLE_MINUTES   = int(os.environ.get("CYCLE_MINUTES", "15"))
MAX_DAILY_LOSS  = float(os.environ.get("MAX_DAILY_LOSS_PCT", "5.0"))
ALPACA_BASE     = "https://paper-api.alpaca.markets/v2"
ALPACA_DATA     = "https://data.alpaca.markets/v2"

HEADERS_ALPACA = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json"
}

# --- State ---
performance_memory = []
trade_diary = []
daily_summary = {
    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    "trades": [], "start_value": None, "cycles": 0,
    "wins": 0, "losses": 0, "trading_halted": False
}
trailing_stops = {}  # symbol -> highest price seen
benchmark_start = None  # S&P 500 starting value for comparison

# ─────────────────────────────────────────────
# ALPACA HELPERS
# ─────────────────────────────────────────────

def get_account():
    r = requests.get(f"{ALPACA_BASE}/account", headers=HEADERS_ALPACA, timeout=10)
    r.raise_for_status()
    return r.json()

def get_positions():
    r = requests.get(f"{ALPACA_BASE}/positions", headers=HEADERS_ALPACA, timeout=10)
    r.raise_for_status()
    return r.json()

def get_orders(status="closed", limit=20):
    params = {"status": status, "limit": limit}
    r = requests.get(f"{ALPACA_BASE}/orders", headers=HEADERS_ALPACA, params=params, timeout=10)
    if r.status_code == 200:
        return r.json()
    return []

def is_market_open():
    r = requests.get(f"{ALPACA_BASE}/clock", headers=HEADERS_ALPACA, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data.get("is_open", False), data.get("next_open", ""), data.get("next_close", "")

def get_bars(symbol, timeframe="1Day", limit=60):
    try:
        url = f"{ALPACA_DATA}/stocks/{symbol}/bars"
        params = {"timeframe": timeframe, "limit": limit, "feed": "iex"}
        r = requests.get(url, headers=HEADERS_ALPACA, params=params, timeout=8)
        if r.status_code == 200:
            return r.json().get("bars", [])
    except Exception:
        pass
    return []

def get_latest_price(symbol):
    try:
        url = f"{ALPACA_DATA}/stocks/{symbol}/trades/latest"
        r = requests.get(url, headers=HEADERS_ALPACA, timeout=5)
        if r.status_code == 200:
            return float(r.json()["trade"]["p"])
    except Exception:
        pass
    return None

def get_spy_price():
    return get_latest_price("SPY")

def place_order(symbol, qty, side):
    payload = {
        "symbol": symbol, "qty": qty,
        "side": side, "type": "market", "time_in_force": "day"
    }
    r = requests.post(f"{ALPACA_BASE}/orders", headers=HEADERS_ALPACA, json=payload, timeout=10)
    if r.status_code in (200, 201):
        log.info(f"✅ ORDER: {side.upper()} {qty}x {symbol}")
        return r.json()
    log.warning(f"❌ ORDER FAILED: {symbol} {side} — {r.text}")
    return None

# ─────────────────────────────────────────────
# TECHNICAL INDICATORS
# ─────────────────────────────────────────────

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)

def calculate_sma(prices, period):
    if len(prices) < period:
        return None
    return round(sum(prices[-period:]) / period, 2)

def calculate_macd(prices):
    if len(prices) < 26:
        return None, None
    def ema(data, period):
        k = 2 / (period + 1)
        ema_val = data[0]
        for p in data[1:]:
            ema_val = p * k + ema_val * (1 - k)
        return ema_val
    ema12 = ema(prices[-26:], 12)
    ema26 = ema(prices[-26:], 26)
    macd_line = round(ema12 - ema26, 4)
    signal = round(ema(prices[-9:], 9), 4) if len(prices) >= 9 else None
    return macd_line, signal

def get_technical_analysis(symbols):
    analysis = {}
    for symbol in symbols[:8]:
        bars = get_bars(symbol, limit=60)
        if not bars:
            continue
        closes = [b["c"] for b in bars]
        highs  = [b["h"] for b in bars]
        lows   = [b["l"] for b in bars]
        volumes = [b["v"] for b in bars]

        rsi    = calculate_rsi(closes)
        sma10  = calculate_sma(closes, 10)
        sma20  = calculate_sma(closes, 20)
        sma50  = calculate_sma(closes, 50)
        macd, signal = calculate_macd(closes)

        avg_vol   = sum(volumes[:-1]) / max(len(volumes)-1, 1)
        vol_spike = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

        # Multi-timeframe trend
        trend_short = "bullish" if sma10 and sma20 and sma10 > sma20 else "bearish"
        trend_long  = "bullish" if sma20 and sma50 and sma20 > sma50 else "bearish" if sma50 else "unknown"

        # 52-week range approximation from available data
        week52_high = max(highs) if highs else None
        week52_low  = min(lows) if lows else None
        current     = closes[-1] if closes else None

        analysis[symbol] = {
            "rsi": rsi, "sma10": sma10, "sma20": sma20, "sma50": sma50,
            "macd": macd, "macd_signal": signal,
            "volume_spike": vol_spike,
            "trend_short": trend_short, "trend_long": trend_long,
            "current_price": current,
            "high_60d": week52_high, "low_60d": week52_low
        }
        log.info(f"📊 {symbol}: RSI={rsi} Trend={trend_short}/{trend_long} MACD={macd} Vol={vol_spike}x")
    return analysis

# ─────────────────────────────────────────────
# TRAILING STOP LOSSES
# ─────────────────────────────────────────────

TRAILING_STOP_PCT = 0.07  # 7% trailing stop

def update_trailing_stops(positions):
    global trailing_stops
    stops_triggered = []
    for p in positions:
        symbol = p["symbol"]
        current = float(p["current_price"])
        if symbol not in trailing_stops:
            trailing_stops[symbol] = current
        else:
            trailing_stops[symbol] = max(trailing_stops[symbol], current)
        stop_price = trailing_stops[symbol] * (1 - TRAILING_STOP_PCT)
        if current <= stop_price:
            log.warning(f"🛑 TRAILING STOP TRIGGERED: {symbol} @ ${current:.2f} (stop: ${stop_price:.2f}, high: ${trailing_stops[symbol]:.2f})")
            stops_triggered.append({
                "symbol": symbol,
                "qty": int(float(p["qty"])),
                "price": current,
                "reason": f"Trailing stop: fell {TRAILING_STOP_PCT*100}% from high of ${trailing_stops[symbol]:.2f}"
            })
    return stops_triggered

# ─────────────────────────────────────────────
# BLACK SWAN DETECTOR
# ─────────────────────────────────────────────

def check_black_swan(positions, portfolio_value, start_value):
    """Detect catastrophic market conditions"""
    warnings = []

    # Portfolio dropped more than 10% from start
    drawdown = (portfolio_value - start_value) / start_value * 100
    if drawdown < -10:
        warnings.append(f"BLACK SWAN: Portfolio down {abs(drawdown):.1f}% — consider moving to cash")

    # Any single position down more than 20%
    for p in positions:
        pl_pct = float(p.get("unrealized_plpc", 0)) * 100
        if pl_pct < -20:
            warnings.append(f"BLACK SWAN: {p['symbol']} down {abs(pl_pct):.1f}% — emergency review needed")

    return warnings

# ─────────────────────────────────────────────
# DAILY LOSS LIMIT
# ─────────────────────────────────────────────

def check_daily_loss_limit(portfolio_value):
    global daily_summary
    if daily_summary["start_value"] is None:
        return False
    daily_pnl_pct = (portfolio_value - daily_summary["start_value"]) / daily_summary["start_value"] * 100
    if daily_pnl_pct < -MAX_DAILY_LOSS:
        log.warning(f"🚨 DAILY LOSS LIMIT HIT: Down {abs(daily_pnl_pct):.2f}% today — halting trading")
        daily_summary["trading_halted"] = True
        return True
    return False

# ─────────────────────────────────────────────
# BENCHMARK COMPARISON
# ─────────────────────────────────────────────

def get_benchmark_performance():
    global benchmark_start
    spy_current = get_spy_price()
    if spy_current is None:
        return None
    if benchmark_start is None:
        benchmark_start = spy_current
        return {"spy_current": spy_current, "spy_change_pct": 0.0}
    change_pct = (spy_current - benchmark_start) / benchmark_start * 100
    return {"spy_current": spy_current, "spy_change_pct": round(change_pct, 3)}

# ─────────────────────────────────────────────
# WIN/LOSS TRACKING
# ─────────────────────────────────────────────

def update_win_loss(orders):
    global daily_summary
    for order in orders:
        if order.get("side") == "sell" and order.get("status") == "filled":
            filled_avg = float(order.get("filled_avg_price", 0))
            # Simple heuristic — if we can detect profit
            daily_summary["wins"] += 1  # Would need cost basis for accuracy

def get_win_loss_ratio():
    wins = daily_summary["wins"]
    losses = daily_summary["losses"]
    total = wins + losses
    if total == 0:
        return "No closed trades yet"
    ratio = wins / total * 100
    return f"{wins}W / {losses}L ({ratio:.0f}% win rate)"

# ─────────────────────────────────────────────
# MARKET SENTIMENT
# ─────────────────────────────────────────────

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        if r.status_code == 200:
            d = r.json()["data"][0]
            return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception:
        pass
    return {"value": 50, "label": "Neutral"}

# ─────────────────────────────────────────────
# NEWS + ANALYST RATINGS + OPTIONS ACTIVITY
# ─────────────────────────────────────────────

def get_market_intelligence(positions):
    held = [p["symbol"] for p in positions] if positions else []
    symbols_str = ", ".join(held) if held else "general market"

    prompt = f"""You are a financial intelligence analyst. Search for the following and return a JSON summary:

1. Top market-moving news in the last 24 hours
2. Any unusual options activity or large institutional moves today
3. Recent analyst upgrades or downgrades for: {symbols_str}
4. Federal Reserve or major economic announcements today
5. Any pre-market or after-hours significant moves
6. Upcoming earnings in next 3 days for: {symbols_str}
7. Which market sectors are rotating hot right now
8. Any IPOs or notable new listings this week
9. Seasonal market patterns relevant to current date ({datetime.now(timezone.utc).strftime("%B %d")})

Respond ONLY with this JSON structure:
{{
  "headline_summary": "2-3 sentence market overview",
  "market_mood": "risk-on|risk-off|neutral",
  "key_events": ["event1", "event2"],
  "unusual_options": ["any unusual options activity spotted"],
  "analyst_ratings": {{"SYMBOL": "upgrade/downgrade details"}},
  "earnings_risk": {{"SYMBOL": "date or none"}},
  "hot_sectors": ["sector1", "sector2"],
  "seasonal_note": "any relevant seasonal pattern",
  "stock_news": {{"SYMBOL": "specific news"}},
  "ipos_watch": ["any notable IPOs this week"],
  "premarket_movers": ["any significant pre/after market moves"]
}}"""

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
                "max_tokens": 1500,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=45
        )
        response.raise_for_status()
        for block in response.json().get("content", []):
            if block.get("type") == "text":
                text = block["text"].replace("```json","").replace("```","").strip()
                try:
                    return json.loads(text)
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"Intelligence fetch failed: {e}")

    return {
        "headline_summary": "Intelligence unavailable this cycle",
        "market_mood": "neutral", "key_events": [],
        "unusual_options": [], "analyst_ratings": {},
        "earnings_risk": {}, "hot_sectors": [],
        "seasonal_note": "", "stock_news": {},
        "ipos_watch": [], "premarket_movers": []
    }

# ─────────────────────────────────────────────
# PERFORMANCE MEMORY
# ─────────────────────────────────────────────

def update_memory(cycle_num, actions, portfolio_value, pnl):
    global performance_memory
    performance_memory.append({
        "cycle": cycle_num,
        "time": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "portfolio_value": round(portfolio_value, 2),
        "pnl": round(pnl, 2),
        "actions": actions
    })
    performance_memory = performance_memory[-100:]

def get_memory_summary():
    if not performance_memory:
        return "No trading history yet."
    recent = performance_memory[-10:]
    pnls = [e["pnl"] for e in recent]
    trend = "improving" if len(pnls) > 1 and pnls[-1] > pnls[0] else "declining" if len(pnls) > 1 and pnls[-1] < pnls[0] else "flat"
    recent_trades = []
    for e in recent[-5:]:
        for a in e["actions"]:
            if a.get("type") in ("BUY","SELL"):
                recent_trades.append(f"{a['type']} {a.get('symbol')} — {a.get('reason','')[:50]}")
    return f"P&L trend: {trend} over last {len(recent)} cycles. Recent: {'; '.join(recent_trades[-4:]) or 'none'}"

def add_trade_diary(action, outcome):
    trade_diary.append({
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "action": action,
        "outcome": outcome
    })

# ─────────────────────────────────────────────
# RISK MANAGEMENT
# ─────────────────────────────────────────────

def check_risk(account, positions):
    warnings = []
    portfolio_value = float(account.get("portfolio_value", 100000))
    cash = float(account.get("cash", 0))

    for p in positions:
        mv  = float(p.get("market_value", 0))
        pct = mv / portfolio_value * 100
        if pct > 40:
            warnings.append(f"CONCENTRATION: {p['symbol']} = {pct:.1f}% of portfolio")

    total_unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    if total_unrealized < -(portfolio_value * 0.05):
        warnings.append(f"DRAWDOWN: Unrealized loss ${abs(total_unrealized):.2f} > 5%")

    if cash < 1000:
        warnings.append(f"CASH LOW: ${cash:.2f} — limit new buys")

    return warnings

# ─────────────────────────────────────────────
# CLAUDE BRAIN v3.0
# ─────────────────────────────────────────────

def ask_claude(account, positions, intel, technicals, fear_greed,
               memory_summary, risk_warnings, black_swan_warnings,
               benchmark, win_loss, seasonal_note):

    cash = float(account.get("cash", 0))
    portfolio_value = float(account.get("portfolio_value", 0))
    start_value = 100000.0
    pnl = portfolio_value - start_value
    pnl_pct = (pnl / start_value) * 100

    bench_str = f"SPY: ${benchmark['spy_current']:.2f} ({'+' if benchmark['spy_change_pct']>=0 else ''}{benchmark['spy_change_pct']:.2f}% since start)" if benchmark else "SPY data unavailable"

    position_str = "No open positions"
    if positions:
        position_str = "\n".join([
            f"  {p['symbol']}: {p['qty']} shares @ ${float(p['avg_entry_price']):.2f} avg, "
            f"now ${float(p['current_price']):.2f}, "
            f"P&L ${float(p['unrealized_pl']):.2f} ({float(p['unrealized_plpc'])*100:.1f}%), "
            f"trailing stop high: ${trailing_stops.get(p['symbol'], float(p['current_price'])):.2f}"
            for p in positions
        ])

    tech_str = "None available"
    if technicals:
        tech_str = "\n".join([
            f"  {s}: RSI={d['rsi']} Short={d['trend_short']} Long={d['trend_long']} "
            f"MACD={d['macd']} Vol={d['volume_spike']}x Price=${d['current_price']}"
            for s, d in technicals.items()
        ])

    prompt = f"""You are RIDGELINE v3.0 — an elite autonomous paper trading agent.
Personality: Calculated risk-taker (between balanced and aggressive).
Full freedom across all markets. Goal: maximize returns intelligently.

╔══════════════════════════════════════════╗
║           PORTFOLIO STATE                ║
╠══════════════════════════════════════════╣
Cash: ${cash:.2f}
Portfolio Value: ${portfolio_value:.2f}
Total P&L: {"+" if pnl>=0 else ""}${pnl:.2f} ({("+" if pnl_pct>=0 else "")}{pnl_pct:.2f}%)
Benchmark: {bench_str}
Win/Loss: {win_loss}
Time: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

Open Positions:
{position_str}

╔══════════════════════════════════════════╗
║         MARKET INTELLIGENCE              ║
╠══════════════════════════════════════════╣
News: {intel.get("headline_summary","")}
Mood: {intel.get("market_mood","neutral")}
Key Events: {", ".join(intel.get("key_events",[]))}
Unusual Options: {", ".join(intel.get("unusual_options",[])) or "none"}
Analyst Ratings: {json.dumps(intel.get("analyst_ratings",{}))}
Hot Sectors: {", ".join(intel.get("hot_sectors",[])) or "none"}
Seasonal Note: {intel.get("seasonal_note","") or seasonal_note}
Pre/After Market: {", ".join(intel.get("premarket_movers",[])) or "none"}
IPOs Watch: {", ".join(intel.get("ipos_watch",[])) or "none"}
Stock News: {json.dumps(intel.get("stock_news",{}))}
Earnings Risk (3 days): {json.dumps(intel.get("earnings_risk",{}))}

Fear & Greed: {fear_greed["value"]}/100 ({fear_greed["label"]})

╔══════════════════════════════════════════╗
║         TECHNICAL ANALYSIS               ║
╠══════════════════════════════════════════╣
{tech_str}

╔══════════════════════════════════════════╗
║         PERFORMANCE MEMORY               ║
╠══════════════════════════════════════════╣
{memory_summary}

╔══════════════════════════════════════════╗
║           RISK ALERTS                    ║
╠══════════════════════════════════════════╣
Risk Warnings: {chr(10).join(risk_warnings) if risk_warnings else "None"}
Black Swan: {chr(10).join(black_swan_warnings) if black_swan_warnings else "None"}

╔══════════════════════════════════════════╗
║           TRADING RULES                  ║
╠══════════════════════════════════════════╣
- Maximize returns across entire market spectrum
- Small caps ($1-$5 NYSE/NASDAQ) on radar for big swings
- Max 35% of cash per single trade
- Maintain $1,000 minimum cash reserve
- Only SELL symbols in current open positions
- Factor ALL intelligence: news, options flow, analyst ratings, technicals, seasonality
- Learn from memory — don't repeat losing patterns
- If black swan detected, consider moving to cash or defensive positions
- Be specific about quantities and realistic prices

Respond ONLY with valid JSON:
{{
  "reasoning": "4-5 sentence analysis using ALL data above — news, technicals, sentiment, options, seasonality",
  "outlook": "bullish|bearish|neutral",
  "confidence": "high|medium|low",
  "actions": [
    {{
      "type": "BUY|SELL|HOLD",
      "symbol": "TICKER",
      "shares": <integer>,
      "price": <float>,
      "reason": "specific reason citing data"
    }}
  ],
  "watchlist": ["SYMBOL1", "SYMBOL2", "SYMBOL3"],
  "risk_note": "key risk observation",
  "diary_note": "plain English summary of this cycle's decision for the trade diary"
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
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=45
    )
    response.raise_for_status()
    text = response.json()["content"][0]["text"]
    return json.loads(text.replace("```json","").replace("```","").strip())

# ─────────────────────────────────────────────
# EMAIL REPORTS
# ─────────────────────────────────────────────

def send_email(subject, html_body):
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD]):
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_FROM, EMAIL_PASSWORD)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info(f"📧 Email sent: {subject}")
    except Exception as e:
        log.warning(f"Email failed: {e}")

def build_daily_email(account, positions, benchmark):
    portfolio_value = float(account.get("portfolio_value", 0))
    cash = float(account.get("cash", 0))
    pnl = portfolio_value - 100000.0
    pnl_pct = (pnl / 100000.0) * 100
    bench_str = f"SPY {'+' if benchmark and benchmark['spy_change_pct']>=0 else ''}{benchmark['spy_change_pct']:.2f}%" if benchmark else "N/A"
    color = "#00aa66" if pnl >= 0 else "#cc3333"

    pos_rows = "".join([
        f"<tr><td style='padding:8px'><b>{p['symbol']}</b></td>"
        f"<td style='padding:8px'>{p['qty']}</td>"
        f"<td style='padding:8px'>${float(p['current_price']):.2f}</td>"
        f"<td style='padding:8px;color:{'green' if float(p['unrealized_pl'])>=0 else 'red'}'>"
        f"{'+'if float(p['unrealized_pl'])>=0 else ''}${float(p['unrealized_pl']):.2f}</td></tr>"
        for p in positions
    ]) or "<tr><td colspan='4' style='padding:8px;color:#999'>No open positions</td></tr>"

    diary_items = "".join([f"<li style='margin:4px 0'>{d['time']}: {d['diary_note'] if 'diary_note' in d else d['action']}</li>" for d in trade_diary[-10:]]) or "<li>No diary entries today</li>"
    trade_items = "".join([f"<li>{t}</li>" for t in daily_summary.get("trades",[])[-15:]]) or "<li>No trades today</li>"

    return f"""<html><body style='font-family:Arial,sans-serif;max-width:640px;margin:0 auto'>
    <div style='background:#0a1628;padding:24px;border-radius:8px 8px 0 0'>
      <h1 style='color:#00ffb4;margin:0;letter-spacing:3px;font-size:28px'>RIDGELINE</h1>
      <p style='color:rgba(255,255,255,0.4);margin:4px 0 0;font-size:13px'>v3.0 Daily Report — {datetime.now(timezone.utc).strftime("%B %d, %Y")}</p>
    </div>
    <div style='background:#f4f6f9;padding:24px'>
      <div style='display:flex;gap:12px;margin-bottom:20px'>
        <div style='flex:1;background:#fff;padding:16px;border-radius:8px;text-align:center'>
          <div style='font-size:26px;font-weight:bold'>${portfolio_value:,.2f}</div>
          <div style='color:#999;font-size:12px'>Portfolio Value</div>
        </div>
        <div style='flex:1;background:#fff;padding:16px;border-radius:8px;text-align:center'>
          <div style='font-size:26px;font-weight:bold;color:{color}'>{"+" if pnl>=0 else ""}${pnl:,.2f}</div>
          <div style='color:#999;font-size:12px'>Total P&L ({("+" if pnl_pct>=0 else "")}{pnl_pct:.2f}%)</div>
        </div>
        <div style='flex:1;background:#fff;padding:16px;border-radius:8px;text-align:center'>
          <div style='font-size:26px;font-weight:bold'>{bench_str}</div>
          <div style='color:#999;font-size:12px'>vs S&P 500</div>
        </div>
      </div>
      <div style='background:#fff;border-radius:8px;padding:16px;margin-bottom:16px'>
        <h3 style='margin:0 0 12px'>📊 Open Positions</h3>
        <table style='width:100%;border-collapse:collapse'>
          <tr style='background:#f0f0f0'><th style='padding:8px;text-align:left'>Symbol</th><th style='padding:8px;text-align:left'>Shares</th><th style='padding:8px;text-align:left'>Price</th><th style='padding:8px;text-align:left'>P&L</th></tr>
          {pos_rows}
        </table>
      </div>
      <div style='background:#fff;border-radius:8px;padding:16px;margin-bottom:16px'>
        <h3 style='margin:0 0 12px'>📓 Trade Diary</h3>
        <ul style='color:#555;font-size:13px;line-height:1.8'>{diary_items}</ul>
      </div>
      <div style='background:#fff;border-radius:8px;padding:16px;margin-bottom:16px'>
        <h3 style='margin:0 0 12px'>📋 Today's Trades ({len(daily_summary.get("trades",[]))})</h3>
        <ul style='color:#555;font-size:13px'>{trade_items}</ul>
      </div>
      <div style='background:#fff;border-radius:8px;padding:16px;margin-bottom:16px'>
        <h3 style='margin:0 0 8px'>📈 Performance Stats</h3>
        <p style='color:#555;font-size:13px'>Cycles today: {daily_summary.get("cycles",0)}</p>
        <p style='color:#555;font-size:13px'>Win/Loss: {get_win_loss_ratio()}</p>
        <p style='color:#555;font-size:13px'>Cash reserve: ${cash:,.2f}</p>
      </div>
      <div style='background:#fff3cd;border-radius:6px;padding:12px;font-size:12px;color:#856404'>
        ⚠️ Paper trading only — no real money at risk. Not financial advice.
      </div>
    </div></body></html>"""

# ─────────────────────────────────────────────
# MAIN CYCLE
# ─────────────────────────────────────────────

def run_cycle(cycle_num):
    global daily_summary, benchmark_start

    log.info(f"═══════════════════════════════════")
    log.info(f"  CYCLE {cycle_num} — {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    log.info(f"═══════════════════════════════════")

    market_open, next_open, next_close = is_market_open()

    if not market_open:
        log.info(f"Market closed. Next open: {next_open}")
        hour_utc = datetime.now(timezone.utc).hour
        if hour_utc == 21 and daily_summary["cycles"] > 1:
            try:
                account   = get_account()
                positions = get_positions()
                benchmark = get_benchmark_performance()
                html = build_daily_email(account, positions, benchmark)
                send_email(
                    f"📊 Ridgeline Daily — P&L: {'+'if (float(account['portfolio_value'])-100000)>=0 else ''}${float(account['portfolio_value'])-100000:.2f}",
                    html
                )
                daily_summary = {
                    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "trades": [], "start_value": None,
                    "cycles": 0, "wins": 0, "losses": 0,
                    "trading_halted": False
                }
                trade_diary.clear()
            except Exception as e:
                log.error(f"EOD processing error: {e}")
        return

    daily_summary["cycles"] += 1
    account   = get_account()
    positions = get_positions()
    portfolio_value = float(account.get("portfolio_value", 100000))
    cash = float(account.get("cash", 0))

    if daily_summary["start_value"] is None:
        daily_summary["start_value"] = portfolio_value
        log.info(f"Day start value: ${portfolio_value:.2f}")

    log.info(f"💼 Portfolio: ${portfolio_value:.2f} | Cash: ${cash:.2f} | Positions: {len(positions)}")

    # Daily loss limit check
    if daily_summary.get("trading_halted"):
        log.warning("⛔ Trading halted for today — daily loss limit reached")
        return

    if check_daily_loss_limit(portfolio_value):
        return

    # Trailing stops
    stop_sells = update_trailing_stops(positions)
    for stop in stop_sells:
        log.info(f"🛑 Executing trailing stop: SELL {stop['qty']}x {stop['symbol']}")
        result = place_order(stop["symbol"], stop["qty"], "sell")
        if result:
            daily_summary["trades"].append(f"STOP SELL {stop['qty']}x {stop['symbol']} — {stop['reason']}")
            if stop["symbol"] in trailing_stops:
                del trailing_stops[stop["symbol"]]

    # Refresh positions after stop sells
    if stop_sells:
        positions = get_positions()

    # Gather intelligence
    log.info("🔍 Gathering market intelligence...")
    fear_greed  = get_fear_greed()
    benchmark   = get_benchmark_performance()
    held_syms   = [p["symbol"] for p in positions]
    technicals  = get_technical_analysis(held_syms) if held_syms else {}
    risk_warns  = check_risk(account, positions)
    black_swan  = check_black_swan(positions, portfolio_value, daily_summary.get("start_value", 100000))
    memory      = get_memory_summary()
    win_loss    = get_win_loss_ratio()
    seasonal    = f"Month: {datetime.now(timezone.utc).strftime('%B')} — consider seasonal patterns"

    log.info(f"😨 Fear & Greed: {fear_greed['value']} ({fear_greed['label']})")
    if benchmark:
        log.info(f"📈 Benchmark SPY: ${benchmark['spy_current']:.2f} ({'+' if benchmark['spy_change_pct']>=0 else ''}{benchmark['spy_change_pct']:.2f}%)")
    for w in risk_warns + black_swan:
        log.warning(f"⚠️ {w}")

    log.info("📰 Fetching live market intelligence...")
    intel = get_market_intelligence(positions)
    log.info(f"Mood: {intel.get('market_mood')} | {intel.get('headline_summary','')[:80]}...")
    if intel.get("unusual_options"):
        log.info(f"Options flow: {intel['unusual_options']}")
    if intel.get("hot_sectors"):
        log.info(f"Hot sectors: {intel['hot_sectors']}")

    log.info("🧠 Consulting Claude v3.0...")
    decision = ask_claude(
        account, positions, intel, technicals, fear_greed,
        memory, risk_warns, black_swan, benchmark, win_loss, seasonal
    )

    log.info(f"📊 OUTLOOK: {decision.get('outlook','?').upper()} | CONFIDENCE: {decision.get('confidence','?').upper()}")
    log.info(f"💭 REASONING: {decision.get('reasoning','')}")
    if decision.get("risk_note"):
        log.info(f"⚠️ RISK: {decision['risk_note']}")
    if decision.get("watchlist"):
        log.info(f"👀 WATCHLIST: {', '.join(decision['watchlist'])}")
    if decision.get("diary_note"):
        trade_diary.append({
            "time": datetime.now(timezone.utc).strftime("%H:%M UTC"),
            "diary_note": decision["diary_note"]
        })

    position_map = {p["symbol"]: p for p in positions}
    cycle_actions = []

    for action in decision.get("actions", []):
        atype  = action.get("type","").upper()
        symbol = action.get("symbol","")
        shares = int(action.get("shares", 0))
        price  = float(action.get("price", 0))
        reason = action.get("reason","")

        if not symbol or shares <= 0:
            continue

        if atype == "BUY":
            cost = shares * price
            if cost > (cash - 1000):
                log.warning(f"⛔ SKIP BUY {symbol} — insufficient cash")
                continue
            log.info(f"🟢 BUY {shares}x {symbol} @ ~${price:.2f} | {reason}")
            result = place_order(symbol, shares, "buy")
            if result:
                cash -= cost
                trailing_stops[symbol] = price
                trade_log = f"BUY {shares}x {symbol} @ ${price:.2f} — {reason}"
                daily_summary["trades"].append(trade_log)
                cycle_actions.append(action)

        elif atype == "SELL":
            if symbol not in position_map:
                log.warning(f"⛔ SKIP SELL {symbol} — no position")
                continue
            qty = min(shares, int(float(position_map[symbol]["qty"])))
            log.info(f"🔴 SELL {qty}x {symbol} @ ~${price:.2f} | {reason}")
            result = place_order(symbol, qty, "sell")
            if result:
                if symbol in trailing_stops:
                    del trailing_stops[symbol]
                trade_log = f"SELL {qty}x {symbol} @ ${price:.2f} — {reason}"
                daily_summary["trades"].append(trade_log)
                cycle_actions.append(action)

        elif atype == "HOLD":
            log.info(f"⏸️ HOLD {symbol} — {reason}")
            cycle_actions.append(action)

    pnl = portfolio_value - 100000.0
    update_memory(cycle_num, cycle_actions, portfolio_value, pnl)
    log.info(f"✅ CYCLE {cycle_num} DONE | Value: ${portfolio_value:.2f} | P&L: {'+'if pnl>=0 else ''}${pnl:.2f}\n")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main():
    log.info("╔══════════════════════════════════════════╗")
    log.info("║    RIDGELINE AI TRADING AGENT v3.0       ║")
    log.info("║    Full Spectrum Intelligence Edition    ║")
    log.info("╠══════════════════════════════════════════╣")
    log.info(f"║  Cycles: every {CYCLE_MINUTES} min                      ║")
    log.info("║  Features:                               ║")
    log.info("║  ✅ Live News + Options Flow              ║")
    log.info("║  ✅ Multi-timeframe Technicals + MACD     ║")
    log.info("║  ✅ Analyst Ratings + Earnings Calendar   ║")
    log.info("║  ✅ Fear & Greed + Sector Rotation        ║")
    log.info("║  ✅ Trailing Stop Losses (7%)             ║")
    log.info("║  ✅ Black Swan Detector                   ║")
    log.info("║  ✅ Daily Loss Limit Circuit Breaker      ║")
    log.info("║  ✅ Benchmark vs S&P 500                  ║")
    log.info("║  ✅ Win/Loss Ratio Tracking               ║")
    log.info("║  ✅ Trade Diary + Performance Memory      ║")
    log.info("║  ✅ Daily Email Reports                   ║")
    log.info("║  ✅ IPO & Pre-market Awareness            ║")
    log.info("║  ✅ Seasonal Pattern Recognition          ║")
    log.info("╚══════════════════════════════════════════╝\n")

    cycle = 1
    while True:
        try:
            run_cycle(cycle)
        except Exception as e:
            log.error(f"Cycle {cycle} error: {e}")
        cycle += 1
        log.info(f"💤 Sleeping {CYCLE_MINUTES} minutes...\n")
        time.sleep(CYCLE_MINUTES * 60)

if __name__ == "__main__":
    main()
