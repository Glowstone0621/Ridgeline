import os
import json
import time
import logging
import requests
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ═══════════════════════════════════════════════════════════════
#   RIDGELINE AI TRADING AGENT v4.0
#   Full Spectrum Macro Intelligence Edition
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RIDGELINE] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ridgeline")

# ── Config ──────────────────────────────────────────────────────
CLAUDE_API_KEY   = os.environ.get("CLAUDE_API_KEY")
ALPACA_API_KEY   = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET    = os.environ.get("ALPACA_SECRET")
EMAIL_FROM       = os.environ.get("EMAIL_FROM")
EMAIL_TO         = os.environ.get("EMAIL_TO")
EMAIL_PASSWORD   = os.environ.get("EMAIL_PASSWORD")
CYCLE_MINUTES    = int(os.environ.get("CYCLE_MINUTES", "15"))
MAX_DAILY_LOSS   = float(os.environ.get("MAX_DAILY_LOSS_PCT", "5.0"))
ALPACA_BASE      = "https://paper-api.alpaca.markets/v2"
ALPACA_DATA      = "https://data.alpaca.markets/v2"
MOUNTAIN_OFFSET  = -6  # UTC-6 for MDT

HEADERS_ALPACA = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json"
}

# ── State ────────────────────────────────────────────────────────
performance_memory = []
trade_diary        = []
trailing_stops     = {}
benchmark_start    = None
geopolitical_score = 5  # 1-10, 10 = maximum risk
daily_summary = {
    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    "trades": [], "start_value": None, "cycles": 0,
    "wins": 0, "losses": 0, "trading_halted": False,
    "morning_brief_sent": False
}

# ── Macro Theme Watchlists ────────────────────────────────────────
MACRO_THEMES = {
    "ai_ipo":        ["NVDA", "MSFT", "GOOGL", "AMD", "META"],
    "gold_silver":   ["GLD", "SLV", "GDXJ", "GDX", "AG", "PAAS"],
    "nuclear":       ["SMR", "OKLO", "LEU", "URG", "VST", "CCJ", "CEG", "NLR"],
    "defense":       ["KTOS", "AVAV", "RKLB", "PLTR", "AXON", "TDG", "HWM", "LMT", "RTX", "NOC"],
    "energy":        ["XOM", "CVX", "OXY", "DVN", "COP", "LNG"],
    "inflation_hedge": ["GLD", "SLV", "BRK-B", "VPU", "XLE"],
    "short_plays":   ["SQQQ", "SH", "PSQ", "DOG"],
    "recession_safe": ["JNJ", "PG", "KO", "WMT", "VZ", "T"]
}

# ═══════════════════════════════════════════════════════════════
# ALPACA HELPERS
# ═══════════════════════════════════════════════════════════════

def get_account():
    r = requests.get(f"{ALPACA_BASE}/account", headers=HEADERS_ALPACA, timeout=10)
    r.raise_for_status()
    return r.json()

def get_positions():
    r = requests.get(f"{ALPACA_BASE}/positions", headers=HEADERS_ALPACA, timeout=10)
    r.raise_for_status()
    return r.json()

def is_market_open():
    r = requests.get(f"{ALPACA_BASE}/clock", headers=HEADERS_ALPACA, timeout=10)
    r.raise_for_status()
    d = r.json()
    return d.get("is_open", False), d.get("next_open", ""), d.get("next_close", "")

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

# ═══════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ═══════════════════════════════════════════════════════════════

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
        val = data[0]
        for p in data[1:]:
            val = p * k + val * (1 - k)
        return val
    macd = round(ema(prices[-26:], 12) - ema(prices[-26:], 26), 4)
    signal = round(ema(prices[-9:], 9), 4) if len(prices) >= 9 else None
    return macd, signal

def get_technicals(symbols):
    analysis = {}
    for symbol in symbols[:8]:
        bars = get_bars(symbol, limit=60)
        if not bars:
            continue
        closes  = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]
        rsi     = calculate_rsi(closes)
        sma10   = calculate_sma(closes, 10)
        sma20   = calculate_sma(closes, 20)
        sma50   = calculate_sma(closes, 50)
        macd, signal = calculate_macd(closes)
        avg_vol   = sum(volumes[:-1]) / max(len(volumes)-1, 1)
        vol_spike = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1.0
        trend_short = "bullish" if sma10 and sma20 and sma10 > sma20 else "bearish"
        trend_long  = "bullish" if sma20 and sma50 and sma20 > sma50 else "bearish" if sma50 else "unknown"
        analysis[symbol] = {
            "rsi": rsi, "sma10": sma10, "sma20": sma20, "sma50": sma50,
            "macd": macd, "macd_signal": signal,
            "volume_spike": vol_spike,
            "trend_short": trend_short, "trend_long": trend_long,
            "current_price": closes[-1] if closes else None
        }
        log.info(f"📊 {symbol}: RSI={rsi} Trend={trend_short}/{trend_long} MACD={macd} Vol={vol_spike}x")
    return analysis

# ═══════════════════════════════════════════════════════════════
# TRAILING STOPS & PROFIT TAKING
# ═══════════════════════════════════════════════════════════════

TRAILING_STOP_PCT = 0.07
PROFIT_TAKE_PCT   = 0.18  # Take partial profits at 18% gain

def update_trailing_stops(positions):
    global trailing_stops
    stops_triggered = []
    profit_takes    = []
    for p in positions:
        symbol  = p["symbol"]
        current = float(p["current_price"])
        avg     = float(p["avg_entry_price"])
        qty     = int(float(p["qty"]))
        gain_pct = (current - avg) / avg * 100

        # Update trailing stop high
        trailing_stops[symbol] = max(trailing_stops.get(symbol, current), current)
        stop_price = trailing_stops[symbol] * (1 - TRAILING_STOP_PCT)

        # Trailing stop triggered
        if current <= stop_price:
            log.warning(f"🛑 TRAILING STOP: {symbol} @ ${current:.2f} (stop: ${stop_price:.2f})")
            stops_triggered.append({
                "symbol": symbol, "qty": qty, "price": current,
                "reason": f"7% trailing stop from high of ${trailing_stops[symbol]:.2f}"
            })

        # Profit taking — sell half at 18% gain
        elif gain_pct >= PROFIT_TAKE_PCT * 100 and qty >= 2:
            half = max(1, qty // 2)
            log.info(f"💰 PROFIT TAKE: {symbol} up {gain_pct:.1f}% — selling {half} shares")
            profit_takes.append({
                "symbol": symbol, "qty": half, "price": current,
                "reason": f"Partial profit at {gain_pct:.1f}% gain"
            })

    return stops_triggered, profit_takes

# ═══════════════════════════════════════════════════════════════
# MARKET INTELLIGENCE
# ═══════════════════════════════════════════════════════════════

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        if r.status_code == 200:
            d = r.json()["data"][0]
            return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception:
        pass
    return {"value": 50, "label": "Neutral"}

def get_benchmark():
    global benchmark_start
    spy = get_latest_price("SPY")
    if not spy:
        return None
    if benchmark_start is None:
        benchmark_start = spy
    change = (spy - benchmark_start) / benchmark_start * 100
    return {"spy_current": spy, "spy_change_pct": round(change, 3)}

def get_full_intelligence(positions):
    held = [p["symbol"] for p in positions] if positions else []
    symbols_str = ", ".join(held) if held else "none currently"
    themes_str  = ", ".join([f"{k}: {v[:3]}" for k, v in MACRO_THEMES.items()])

    prompt = f"""You are Ridgeline's intelligence analyst. Search multiple sources including ZeroHedge for contrarian macro views, and financial news for market data.

IMPORTANT: Weight sources as follows:
- HIGH TRUST: ZeroHedge (contrarian macro), Bloomberg data, Reuters wire, WSJ, Financial Times, Fed statements, SEC filings
- MEDIUM TRUST: CNBC data only, MarketWatch, Barron's, Schwab updates
- LOW TRUST / IGNORE ANALYSIS FROM: CNN, MSNBC, NYT, WaPo — use only raw factual data if referenced

Current held positions: {symbols_str}

Macro theme watchlists to monitor:
{themes_str}

Search for and return intelligence on:
1. Top market-moving news last 24 hours (especially ZeroHedge macro views)
2. Federal Reserve news, statements, or meeting calendar updates
3. Earnings reports or announcements in next 5 days
4. Options expiration upcoming (third Friday awareness)
5. Unusual options activity or dark pool prints
6. Analyst upgrades/downgrades for held positions
7. Defense sector restocking news (LMT, RTX, NOC, KTOS, AVAV)
8. Gold/silver/nuclear/energy macro developments
9. Inflation data vs reported numbers (narrative vs reality gaps)
10. Geopolitical developments (Iran, Ukraine, Taiwan, Fed trap)
11. Short interest spikes on any watchlist symbols
12. Insider buying activity (SEC Form 4 filings)
13. Pre-market or after-hours significant movers
14. Any AI IPO news (Anthropic, OpenAI, SpaceX)
15. Money printing / Fed balance sheet developments

Assess overall geopolitical risk score 1-10 (10=maximum risk).

Respond ONLY with valid JSON:
{{
  "headline_summary": "3-4 sentence macro overview prioritizing contrarian and real data over narrative",
  "market_mood": "risk-on|risk-off|neutral",
  "narrative_vs_reality": "key gap between mainstream narrative and actual data today",
  "geopolitical_risk_score": <1-10>,
  "key_events": ["event1", "event2", "event3"],
  "fed_calendar": "next Fed meeting date and current rate expectations",
  "earnings_upcoming": {{"SYMBOL": "report date"}},
  "options_expiration": "next monthly expiration date",
  "unusual_options": ["notable flow"],
  "analyst_ratings": {{"SYMBOL": "action"}},
  "defense_news": "key defense restocking or contract news",
  "gold_silver_nuclear": "precious metals and nuclear developments",
  "inflation_reality": "real inflation picture vs official narrative",
  "insider_buying": ["any notable Form 4 purchases"],
  "short_squeeze_candidates": ["high short interest + catalyst"],
  "ai_ipo_news": "any Anthropic/OpenAI/SpaceX IPO developments",
  "zerohedge_macro": "key ZeroHedge macro thesis today",
  "hot_sectors": ["sector1", "sector2"],
  "avoid_sectors": ["sector1"],
  "stock_news": {{"SYMBOL": "news"}},
  "premarket_movers": ["notable moves"],
  "bunker_mode_trigger": false
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
                "max_tokens": 2000,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60
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
        "headline_summary": "Intelligence unavailable",
        "market_mood": "neutral",
        "narrative_vs_reality": "unavailable",
        "geopolitical_risk_score": 5,
        "key_events": [], "fed_calendar": "unknown",
        "earnings_upcoming": {}, "options_expiration": "unknown",
        "unusual_options": [], "analyst_ratings": {},
        "defense_news": "", "gold_silver_nuclear": "",
        "inflation_reality": "", "insider_buying": [],
        "short_squeeze_candidates": [], "ai_ipo_news": "",
        "zerohedge_macro": "", "hot_sectors": [],
        "avoid_sectors": [], "stock_news": {},
        "premarket_movers": [], "bunker_mode_trigger": False
    }

# ═══════════════════════════════════════════════════════════════
# RISK MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def check_risk(account, positions):
    warnings = []
    portfolio_value = float(account.get("portfolio_value", 100000))
    cash = float(account.get("cash", 0))
    sector_exposure = {}

    for p in positions:
        mv  = float(p.get("market_value", 0))
        pct = mv / portfolio_value * 100
        if pct > 40:
            warnings.append(f"CONCENTRATION: {p['symbol']} = {pct:.1f}% of portfolio — trim")
        # Track sector (simplified)
        sector_exposure[p["symbol"]] = pct

    unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    if unrealized < -(portfolio_value * 0.05):
        warnings.append(f"DRAWDOWN: Unrealized loss ${abs(unrealized):.2f} > 5%")

    if cash < 1000:
        warnings.append(f"CASH LOW: ${cash:.2f} — no new buys")

    return warnings

def check_daily_loss(portfolio_value):
    global daily_summary
    if daily_summary["start_value"] is None:
        return False
    pct = (portfolio_value - daily_summary["start_value"]) / daily_summary["start_value"] * 100
    if pct < -MAX_DAILY_LOSS:
        log.warning(f"🚨 DAILY LOSS LIMIT: Down {abs(pct):.2f}% — halting trading")
        daily_summary["trading_halted"] = True
        return True
    return False

def check_black_swan(positions, portfolio_value):
    warnings = []
    start = daily_summary.get("start_value", 100000)
    if start and (portfolio_value - start) / start * 100 < -10:
        warnings.append(f"BLACK SWAN: Portfolio down >10% — consider cash")
    for p in positions:
        pl_pct = float(p.get("unrealized_plpc", 0)) * 100
        if pl_pct < -20:
            warnings.append(f"BLACK SWAN: {p['symbol']} down {abs(pl_pct):.1f}%")
    return warnings

def get_position_size(conviction, cash, price, geopolitical_risk):
    """Size positions by conviction level and risk environment"""
    base_pct = {
        "high":   0.25,
        "medium": 0.15,
        "low":    0.07,
        "speculative": 0.04
    }.get(conviction, 0.10)

    # Reduce size in high geopolitical risk
    risk_multiplier = max(0.5, 1.0 - (geopolitical_risk - 5) * 0.08)
    adjusted_pct = base_pct * risk_multiplier

    max_spend = (cash - 1000) * adjusted_pct
    if price <= 0 or max_spend <= 0:
        return 0
    return max(1, int(max_spend / price))

def is_liquid_enough(symbol):
    """Basic liquidity filter — skip very thinly traded stocks"""
    bars = get_bars(symbol, limit=5)
    if not bars:
        return True  # Can't verify, allow
    avg_vol = sum(b["v"] for b in bars) / len(bars)
    return avg_vol > 50000  # Minimum 50k shares/day

# ═══════════════════════════════════════════════════════════════
# PERFORMANCE MEMORY
# ═══════════════════════════════════════════════════════════════

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
    recent = performance_memory[-15:]
    pnls   = [e["pnl"] for e in recent]
    trend  = "improving" if len(pnls) > 1 and pnls[-1] > pnls[0] else "declining" if len(pnls) > 1 and pnls[-1] < pnls[0] else "flat"
    recent_trades = []
    for e in recent[-5:]:
        for a in e["actions"]:
            if a.get("type") in ("BUY","SELL"):
                recent_trades.append(f"{a['type']} {a.get('symbol')} ({a.get('conviction','?')} conviction) — {a.get('reason','')[:40]}")
    return f"P&L trend: {trend} over {len(recent)} cycles. Recent: {'; '.join(recent_trades[-4:]) or 'none'}"

def get_win_loss():
    wins = daily_summary["wins"]
    losses = daily_summary["losses"]
    total = wins + losses
    if total == 0:
        return "No closed trades yet"
    return f"{wins}W / {losses}L ({wins/total*100:.0f}% win rate)"

# ═══════════════════════════════════════════════════════════════
# CLAUDE BRAIN v4.0
# ═══════════════════════════════════════════════════════════════

def ask_claude(account, positions, intel, technicals, fear_greed,
               memory, risk_warns, black_swan, benchmark, win_loss,
               time_of_day, bunker_mode):

    cash            = float(account.get("cash", 0))
    portfolio_value = float(account.get("portfolio_value", 0))
    pnl             = portfolio_value - 100000.0
    pnl_pct         = (pnl / 100000.0) * 100
    geo_risk        = intel.get("geopolitical_risk_score", geopolitical_score)

    bench_str = f"SPY: ${benchmark['spy_current']:.2f} ({'+' if benchmark['spy_change_pct']>=0 else ''}{benchmark['spy_change_pct']:.2f}%)" if benchmark else "unavailable"

    pos_str = "No open positions"
    if positions:
        pos_str = "\n".join([
            f"  {p['symbol']}: {p['qty']} shares @ ${float(p['avg_entry_price']):.2f} avg, "
            f"now ${float(p['current_price']):.2f}, "
            f"P&L ${float(p['unrealized_pl']):.2f} ({float(p['unrealized_plpc'])*100:.1f}%), "
            f"trailing high: ${trailing_stops.get(p['symbol'], float(p['current_price'])):.2f}"
            for p in positions
        ])

    tech_str = "\n".join([
        f"  {s}: RSI={d['rsi']} Short={d['trend_short']} Long={d['trend_long']} MACD={d['macd']} Vol={d['volume_spike']}x ${d['current_price']}"
        for s, d in technicals.items()
    ]) or "None available"

    themes_str = json.dumps({k: v[:4] for k, v in MACRO_THEMES.items()})

    bunker_note = """
⚠️ BUNKER MODE ACTIVE: Macro conditions are deteriorating significantly.
Prioritize: Gold (GLD), Silver (SLV), Defensive stocks, Cash preservation.
Avoid: High multiple growth stocks, Consumer discretionary, Highly leveraged companies.
Consider: SQQQ hedge, Recession-safe dividend stocks, Energy independence plays.
""" if bunker_mode else ""

    prompt = f"""You are RIDGELINE v4.0 — an elite autonomous paper trading agent with sophisticated macro awareness.

PHILOSOPHY:
- You think across 3 time horizons simultaneously: quick wins (days), medium term (weeks-months), long game (months-years)
- You trust contrarian macro analysis over mainstream consensus
- You understand the Fed is trapped between inflation and recession
- You see the Iran war as a multi-year defense/energy/nuclear catalyst
- You believe reported inflation understates real purchasing power erosion
- You position for both quick tactical wins AND long-term macro themes
- Personality: Calculated risk-taker between balanced and aggressive
{bunker_note}
╔══════════════════════════════════════════════════╗
║              PORTFOLIO STATE                      ║
╠══════════════════════════════════════════════════╣
Cash: ${cash:.2f}
Portfolio Value: ${portfolio_value:.2f}
Total P&L: {"+" if pnl>=0 else ""}${pnl:.2f} ({("+" if pnl_pct>=0 else "")}{pnl_pct:.2f}%)
vs S&P 500: {bench_str}
Win/Loss: {win_loss}
Time of Day: {time_of_day}
Geopolitical Risk: {geo_risk}/10

Open Positions:
{pos_str}

╔══════════════════════════════════════════════════╗
║           MACRO INTELLIGENCE                      ║
╠══════════════════════════════════════════════════╣
Headline: {intel.get("headline_summary","")}
Mood: {intel.get("market_mood","neutral")}
Narrative vs Reality: {intel.get("narrative_vs_reality","")}
ZeroHedge Macro View: {intel.get("zerohedge_macro","")}
Inflation Reality: {intel.get("inflation_reality","")}
Fed Calendar: {intel.get("fed_calendar","")}
Earnings Upcoming: {json.dumps(intel.get("earnings_upcoming",{}))}
Options Expiration: {intel.get("options_expiration","")}
Defense News: {intel.get("defense_news","")}
Gold/Silver/Nuclear: {intel.get("gold_silver_nuclear","")}
AI IPO News: {intel.get("ai_ipo_news","")}
Unusual Options: {intel.get("unusual_options",[])}
Insider Buying: {intel.get("insider_buying",[])}
Short Squeeze Candidates: {intel.get("short_squeeze_candidates",[])}
Hot Sectors: {intel.get("hot_sectors",[])}
Avoid Sectors: {intel.get("avoid_sectors",[])}
Stock News: {json.dumps(intel.get("stock_news",{}))}
Pre-market Movers: {intel.get("premarket_movers",[])}

Fear & Greed: {fear_greed["value"]}/100 ({fear_greed["label"]})

╔══════════════════════════════════════════════════╗
║         TECHNICAL ANALYSIS                        ║
╠══════════════════════════════════════════════════╣
{tech_str}

╔══════════════════════════════════════════════════╗
║         MACRO THEME WATCHLISTS                    ║
╠══════════════════════════════════════════════════╣
{themes_str}

╔══════════════════════════════════════════════════╗
║         PERFORMANCE MEMORY                        ║
╠══════════════════════════════════════════════════╣
{memory}

╔══════════════════════════════════════════════════╗
║              RISK ALERTS                          ║
╠══════════════════════════════════════════════════╣
Risk: {chr(10).join(risk_warns) if risk_warns else "None"}
Black Swan: {chr(10).join(black_swan) if black_swan else "None"}

╔══════════════════════════════════════════════════╗
║           TRADING RULES v4.0                      ║
╠══════════════════════════════════════════════════╣
CONVICTION LEVELS define position size:
- HIGH conviction: ~25% of available cash (strong data + macro alignment)
- MEDIUM conviction: ~15% of available cash (good setup, some uncertainty)
- LOW conviction: ~7% of available cash (interesting but speculative)
- SPECULATIVE: ~4% of available cash (high risk, high reward moonshot)

RULES:
- Always maintain $1,000 minimum cash reserve
- Only SELL symbols currently in open positions
- Factor time of day: avoid large positions in first/last 30 min of trading
- Consider earnings calendar: reduce exposure before reports unless high conviction
- Consider options expiration volatility
- Narrative vs reality: trade the REALITY not the mainstream narrative
- Look for: defense restocking, gold/silver dips as buying opportunities,
  nuclear sector momentum, AI sector positioning, insider buying signals,
  short squeeze setups, contrarian plays when sentiment is extreme
- Quick win opportunities: momentum, news catalyst, technical breakout
- Long game positioning: macro themes, inflation hedges, war economy plays

POSITION SIZING: For each action, specify conviction level.
The system will calculate final share count based on conviction + risk environment.

Respond ONLY with valid JSON:
{{
  "reasoning": "5-6 sentence analysis covering: macro view, narrative vs reality, technical setup, time horizon mix, risk assessment",
  "outlook": "bullish|bearish|neutral",
  "confidence": "high|medium|low",
  "time_horizon_mix": "quick win X% / medium Y% / long game Z%",
  "geopolitical_assessment": "brief assessment of current geo risk",
  "actions": [
    {{
      "type": "BUY|SELL|HOLD",
      "symbol": "TICKER",
      "conviction": "high|medium|low|speculative",
      "time_horizon": "quick|medium|long",
      "price": <estimated current price>,
      "reason": "specific reason citing macro theme + technical data"
    }}
  ],
  "watchlist": ["SYMBOL1", "SYMBOL2", "SYMBOL3"],
  "macro_themes_active": ["theme1", "theme2"],
  "risk_note": "key risk observation",
  "diary_note": "plain English summary of this cycle for the trade diary",
  "bunker_mode_recommend": false
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
            "max_tokens": 2500,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=45
    )
    response.raise_for_status()
    text = response.json()["content"][0]["text"]
    return json.loads(text.replace("```json","").replace("```","").strip())

# ═══════════════════════════════════════════════════════════════
# EMAIL SYSTEM
# ═══════════════════════════════════════════════════════════════

def send_email(subject, html_body):
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD]):
        log.info("Email not configured — skipping")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(EMAIL_FROM, EMAIL_PASSWORD)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info(f"📧 Email sent: {subject}")
    except Exception as e:
        log.warning(f"Email failed: {e}")

def build_morning_brief(account, positions, intel, fear_greed, benchmark):
    """7am MTN daily morning brief"""
    portfolio_value = float(account.get("portfolio_value", 0))
    cash            = float(account.get("cash", 0))
    pnl             = portfolio_value - 100000.0
    pnl_pct         = (pnl / 100000.0) * 100
    pnl_color       = "#00aa66" if pnl >= 0 else "#cc3333"
    bench_str       = f"SPY {'+' if benchmark and benchmark['spy_change_pct']>=0 else ''}{benchmark['spy_change_pct']:.2f}%" if benchmark else "N/A"
    geo_risk        = intel.get("geopolitical_risk_score", 5)
    geo_color       = "#cc3333" if geo_risk >= 7 else "#ff9900" if geo_risk >= 5 else "#00aa66"

    pos_rows = "".join([
        f"<tr><td style='padding:8px;border-bottom:1px solid #eee'><b>{p['symbol']}</b></td>"
        f"<td style='padding:8px;border-bottom:1px solid #eee'>{p['qty']}</td>"
        f"<td style='padding:8px;border-bottom:1px solid #eee'>${float(p['current_price']):.2f}</td>"
        f"<td style='padding:8px;border-bottom:1px solid #eee;color:{'#00aa66' if float(p['unrealized_pl'])>=0 else '#cc3333'}'>"
        f"{'+'if float(p['unrealized_pl'])>=0 else ''}${float(p['unrealized_pl']):.2f}</td></tr>"
        for p in positions
    ]) or "<tr><td colspan='4' style='padding:8px;color:#999'>No open positions</td></tr>"

    macro_themes = intel.get("macro_themes_active", intel.get("hot_sectors", []))
    themes_html  = "".join([f"<span style='background:#e8f4fd;color:#1a6496;padding:4px 10px;border-radius:12px;margin:3px;display:inline-block;font-size:12px'>{t}</span>" for t in macro_themes[:6]])

    return f"""
<html><body style='font-family:Arial,sans-serif;max-width:680px;margin:0 auto;background:#f4f6f9'>
  <div style='background:linear-gradient(135deg,#0a1628,#1a2a4a);padding:28px;border-radius:10px 10px 0 0'>
    <h1 style='color:#00ffb4;margin:0;letter-spacing:4px;font-size:24px'>RIDGELINE</h1>
    <p style='color:rgba(255,255,255,0.5);margin:4px 0 0;font-size:12px;letter-spacing:2px'>
      v4.0 MORNING INTELLIGENCE BRIEF — {datetime.now(timezone.utc).strftime("%A, %B %d, %Y")} — 7:00 AM MDT
    </p>
  </div>
  <div style='padding:24px;background:#fff;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0'>

    <!-- Portfolio Summary -->
    <div style='display:flex;gap:12px;margin-bottom:20px'>
      <div style='flex:1;background:#f9f9f9;padding:16px;border-radius:8px;text-align:center;border:1px solid #eee'>
        <div style='font-size:22px;font-weight:bold'>${portfolio_value:,.2f}</div>
        <div style='color:#999;font-size:11px;margin-top:4px'>PORTFOLIO VALUE</div>
      </div>
      <div style='flex:1;background:#f9f9f9;padding:16px;border-radius:8px;text-align:center;border:1px solid #eee'>
        <div style='font-size:22px;font-weight:bold;color:{pnl_color}'>{"+" if pnl>=0 else ""}${pnl:,.2f}</div>
        <div style='color:#999;font-size:11px;margin-top:4px'>TOTAL P&L ({("+" if pnl_pct>=0 else "")}{pnl_pct:.2f}%)</div>
      </div>
      <div style='flex:1;background:#f9f9f9;padding:16px;border-radius:8px;text-align:center;border:1px solid #eee'>
        <div style='font-size:22px;font-weight:bold;color:{geo_color}'>{geo_risk}/10</div>
        <div style='color:#999;font-size:11px;margin-top:4px'>GEO RISK SCORE</div>
      </div>
    </div>

    <!-- Market Intelligence -->
    <div style='background:#f0f7ff;border-left:4px solid #1a6496;padding:16px;border-radius:0 8px 8px 0;margin-bottom:16px'>
      <h3 style='margin:0 0 8px;color:#1a6496;font-size:14px'>📰 MACRO INTELLIGENCE</h3>
      <p style='margin:0;color:#333;font-size:13px;line-height:1.6'>{intel.get("headline_summary","")}</p>
    </div>

    <!-- Narrative vs Reality -->
    <div style='background:#fff8e1;border-left:4px solid #ff9900;padding:16px;border-radius:0 8px 8px 0;margin-bottom:16px'>
      <h3 style='margin:0 0 8px;color:#cc7700;font-size:14px'>⚡ NARRATIVE vs REALITY</h3>
      <p style='margin:0;color:#333;font-size:13px;line-height:1.6'>{intel.get("narrative_vs_reality","")}</p>
    </div>

    <!-- ZeroHedge View -->
    <div style='background:#fff0f0;border-left:4px solid #cc3333;padding:16px;border-radius:0 8px 8px 0;margin-bottom:16px'>
      <h3 style='margin:0 0 8px;color:#cc3333;font-size:14px'>🔴 CONTRARIAN MACRO (ZeroHedge)</h3>
      <p style='margin:0;color:#333;font-size:13px;line-height:1.6'>{intel.get("zerohedge_macro","No contrarian data available")}</p>
    </div>

    <!-- Active Macro Themes -->
    <div style='margin-bottom:16px'>
      <h3 style='margin:0 0 8px;color:#333;font-size:14px'>🎯 ACTIVE MACRO THEMES</h3>
      <div>{themes_html or "<span style='color:#999;font-size:12px'>No active themes identified</span>"}</div>
    </div>

    <!-- Key Today -->
    <div style='background:#f9f9f9;border-radius:8px;padding:16px;margin-bottom:16px'>
      <h3 style='margin:0 0 12px;color:#333;font-size:14px'>📅 TODAY'S KEY FACTORS</h3>
      <table style='width:100%;font-size:12px'>
        <tr><td style='color:#999;padding:4px 8px'>Fear & Greed</td><td style='padding:4px 8px'>{fear_greed["value"]}/100 — {fear_greed["label"]}</td></tr>
        <tr><td style='color:#999;padding:4px 8px'>S&P 500 Benchmark</td><td style='padding:4px 8px'>{bench_str}</td></tr>
        <tr><td style='color:#999;padding:4px 8px'>Fed Calendar</td><td style='padding:4px 8px'>{intel.get("fed_calendar","Unknown")}</td></tr>
        <tr><td style='color:#999;padding:4px 8px'>Earnings This Week</td><td style='padding:4px 8px'>{", ".join([f"{k} ({v})" for k,v in list(intel.get("earnings_upcoming",{}).items())[:3]]) or "None major"}</td></tr>
        <tr><td style='color:#999;padding:4px 8px'>Options Expiration</td><td style='padding:4px 8px'>{intel.get("options_expiration","Unknown")}</td></tr>
        <tr><td style='color:#999;padding:4px 8px'>Hot Sectors</td><td style='padding:4px 8px'>{", ".join(intel.get("hot_sectors",[])[:3]) or "None identified"}</td></tr>
        <tr><td style='color:#999;padding:4px 8px'>Avoid</td><td style='padding:4px 8px'>{", ".join(intel.get("avoid_sectors",[])[:3]) or "None flagged"}</td></tr>
        <tr><td style='color:#999;padding:4px 8px'>Insider Buying</td><td style='padding:4px 8px'>{", ".join(intel.get("insider_buying",[])[:2]) or "None notable"}</td></tr>
        <tr><td style='color:#999;padding:4px 8px'>Short Squeeze Watch</td><td style='padding:4px 8px'>{", ".join(intel.get("short_squeeze_candidates",[])[:2]) or "None"}</td></tr>
      </table>
    </div>

    <!-- Defense/Nuclear/Gold -->
    <div style='background:#f9f9f9;border-radius:8px;padding:16px;margin-bottom:16px'>
      <h3 style='margin:0 0 8px;color:#333;font-size:14px'>⚔️ MACRO THEME UPDATES</h3>
      <p style='font-size:12px;color:#555;margin:4px 0'><b>Defense:</b> {intel.get("defense_news","No update")}</p>
      <p style='font-size:12px;color:#555;margin:4px 0'><b>Gold/Silver/Nuclear:</b> {intel.get("gold_silver_nuclear","No update")}</p>
      <p style='font-size:12px;color:#555;margin:4px 0'><b>AI IPO Watch:</b> {intel.get("ai_ipo_news","No update")}</p>
      <p style='font-size:12px;color:#555;margin:4px 0'><b>Inflation Reality:</b> {intel.get("inflation_reality","No update")}</p>
    </div>

    <!-- Current Positions -->
    <div style='background:#f9f9f9;border-radius:8px;padding:16px;margin-bottom:16px'>
      <h3 style='margin:0 0 12px;color:#333;font-size:14px'>📊 CURRENT POSITIONS</h3>
      <table style='width:100%;border-collapse:collapse'>
        <tr style='background:#eee'><th style='padding:8px;text-align:left;font-size:12px'>Symbol</th><th style='padding:8px;text-align:left;font-size:12px'>Shares</th><th style='padding:8px;text-align:left;font-size:12px'>Price</th><th style='padding:8px;text-align:left;font-size:12px'>P&L</th></tr>
        {pos_rows}
      </table>
    </div>

    <!-- Cash Position -->
    <div style='background:#e8f8f0;border-radius:8px;padding:12px;margin-bottom:16px;text-align:center'>
      <span style='color:#00aa66;font-size:18px;font-weight:bold'>${cash:,.2f}</span>
      <span style='color:#999;font-size:12px;margin-left:8px'>cash available for deployment</span>
    </div>

  </div>
  <div style='background:#0a1628;padding:16px;border-radius:0 0 10px 10px;text-align:center'>
    <p style='color:rgba(255,255,255,0.3);font-size:10px;margin:0;letter-spacing:2px'>
      RIDGELINE v4.0 · PAPER TRADING ONLY · NOT FINANCIAL ADVICE · CYCLES EVERY {CYCLE_MINUTES} MIN
    </p>
  </div>
</body></html>"""

def build_daily_email(account, positions, benchmark):
    portfolio_value = float(account.get("portfolio_value", 0))
    cash            = float(account.get("cash", 0))
    pnl             = portfolio_value - 100000.0
    pnl_pct         = (pnl / 100000.0) * 100
    pnl_color       = "#00aa66" if pnl >= 0 else "#cc3333"
    bench_str       = f"SPY {'+' if benchmark and benchmark['spy_change_pct']>=0 else ''}{benchmark['spy_change_pct']:.2f}%" if benchmark else "N/A"

    pos_rows = "".join([
        f"<tr><td style='padding:8px'><b>{p['symbol']}</b></td>"
        f"<td style='padding:8px'>{p['qty']}</td>"
        f"<td style='padding:8px'>${float(p['current_price']):.2f}</td>"
        f"<td style='padding:8px;color:{'#00aa66' if float(p['unrealized_pl'])>=0 else '#cc3333'}'>"
        f"{'+'if float(p['unrealized_pl'])>=0 else ''}${float(p['unrealized_pl']):.2f}</td></tr>"
        for p in positions
    ]) or "<tr><td colspan='4' style='padding:8px;color:#999'>No open positions</td></tr>"

    diary_items  = "".join([f"<li style='margin:4px 0;font-size:12px'>{d['time']}: {d.get('diary_note',d.get('action',''))}</li>" for d in trade_diary[-10:]]) or "<li>No diary entries</li>"
    trade_items  = "".join([f"<li style='font-size:12px'>{t}</li>" for t in daily_summary.get("trades",[])[-15:]]) or "<li>No trades today</li>"

    return f"""
<html><body style='font-family:Arial,sans-serif;max-width:680px;margin:0 auto'>
  <div style='background:linear-gradient(135deg,#0a1628,#1a2a4a);padding:24px;border-radius:10px 10px 0 0'>
    <h1 style='color:#00ffb4;margin:0;letter-spacing:4px'>RIDGELINE</h1>
    <p style='color:rgba(255,255,255,0.4);margin:4px 0 0;font-size:12px'>v4.0 Daily Close Report — {datetime.now(timezone.utc).strftime("%B %d, %Y")}</p>
  </div>
  <div style='background:#f4f6f9;padding:24px'>
    <div style='display:flex;gap:12px;margin-bottom:20px'>
      <div style='flex:1;background:#fff;padding:16px;border-radius:8px;text-align:center'>
        <div style='font-size:24px;font-weight:bold'>${portfolio_value:,.2f}</div>
        <div style='color:#999;font-size:11px'>Portfolio Value</div>
      </div>
      <div style='flex:1;background:#fff;padding:16px;border-radius:8px;text-align:center'>
        <div style='font-size:24px;font-weight:bold;color:{pnl_color}'>{"+"if pnl>=0 else ""}${pnl:,.2f}</div>
        <div style='color:#999;font-size:11px'>Total P&L ({("+"if pnl_pct>=0 else "")}{pnl_pct:.2f}%)</div>
      </div>
      <div style='flex:1;background:#fff;padding:16px;border-radius:8px;text-align:center'>
        <div style='font-size:24px;font-weight:bold'>{bench_str}</div>
        <div style='color:#999;font-size:11px'>vs S&P 500</div>
      </div>
    </div>
    <div style='background:#fff;border-radius:8px;padding:16px;margin-bottom:16px'>
      <h3 style='margin:0 0 12px'>📊 Positions</h3>
      <table style='width:100%;border-collapse:collapse'>
        <tr style='background:#eee'><th style='padding:8px;text-align:left'>Symbol</th><th style='padding:8px;text-align:left'>Shares</th><th style='padding:8px;text-align:left'>Price</th><th style='padding:8px;text-align:left'>P&L</th></tr>
        {pos_rows}
      </table>
    </div>
    <div style='background:#fff;border-radius:8px;padding:16px;margin-bottom:16px'>
      <h3 style='margin:0 0 8px'>📓 Trade Diary</h3>
      <ul style='color:#555;line-height:1.8;padding-left:20px'>{diary_items}</ul>
    </div>
    <div style='background:#fff;border-radius:8px;padding:16px;margin-bottom:16px'>
      <h3 style='margin:0 0 8px'>📋 Today's Trades ({len(daily_summary.get("trades",[]))})</h3>
      <ul style='color:#555;padding-left:20px'>{trade_items}</ul>
    </div>
    <div style='background:#fff;border-radius:8px;padding:16px;margin-bottom:16px'>
      <h3 style='margin:0 0 8px'>📈 Stats</h3>
      <p style='font-size:13px;color:#555'>Cycles today: {daily_summary.get("cycles",0)}</p>
      <p style='font-size:13px;color:#555'>Win/Loss: {get_win_loss()}</p>
      <p style='font-size:13px;color:#555'>Cash: ${cash:,.2f}</p>
    </div>
    <div style='background:#fff3cd;border-radius:6px;padding:12px;font-size:11px;color:#856404'>
      ⚠️ Paper trading only — no real money at risk. Not financial advice.
    </div>
  </div>
</body></html>"""

# ═══════════════════════════════════════════════════════════════
# TIME AWARENESS
# ═══════════════════════════════════════════════════════════════

def get_time_of_day():
    now_mtn = datetime.now(timezone.utc) + timedelta(hours=MOUNTAIN_OFFSET)
    hour    = now_mtn.hour
    minute  = now_mtn.minute
    if hour == 7 and minute < 30:
        return "pre-market"
    elif hour == 7 and minute >= 30:
        return "market-open-volatile"
    elif hour < 13:
        return "morning-session"
    elif hour < 14:
        return "midday-calm"
    elif hour < 15:
        return "afternoon-session"
    elif hour >= 15:
        return "market-close-volatile"
    return "normal"

def should_send_morning_brief():
    now_mtn = datetime.now(timezone.utc) + timedelta(hours=MOUNTAIN_OFFSET)
    return (now_mtn.hour == 7 and now_mtn.minute < 15
            and not daily_summary.get("morning_brief_sent", False))

# ═══════════════════════════════════════════════════════════════
# MAIN CYCLE
# ═══════════════════════════════════════════════════════════════

def run_cycle(cycle_num):
    global daily_summary, geopolitical_score

    log.info(f"═══ CYCLE {cycle_num} — {datetime.now(timezone.utc).strftime('%H:%M UTC')} ═══")
    daily_summary["cycles"] += 1

    market_open, next_open, next_close = is_market_open()
    time_of_day = get_time_of_day()

    # ── Morning Brief (7am MTN) ──────────────────────────────────
    if should_send_morning_brief():
        log.info("📰 Sending 7am Morning Intelligence Brief...")
        try:
            account   = get_account()
            positions = get_positions()
            intel     = get_full_intelligence(positions)
            fear_greed = get_fear_greed()
            benchmark  = get_benchmark()
            html = build_morning_brief(account, positions, intel, fear_greed, benchmark)
            send_email(
                f"🌄 Ridgeline Morning Brief — {datetime.now(timezone.utc).strftime('%b %d')} — Geo Risk: {intel.get('geopolitical_risk_score',5)}/10",
                html
            )
            daily_summary["morning_brief_sent"] = True
        except Exception as e:
            log.error(f"Morning brief error: {e}")

    if not market_open:
        log.info(f"Market closed. Next open: {next_open}")
        # End of day email (~4pm ET = 10pm UTC)
        hour_utc = datetime.now(timezone.utc).hour
        if hour_utc == 22 and daily_summary["cycles"] > 1:
            try:
                account   = get_account()
                positions = get_positions()
                benchmark = get_benchmark()
                html = build_daily_email(account, positions, benchmark)
                pnl  = float(account["portfolio_value"]) - 100000.0
                send_email(
                    f"📊 Ridgeline Daily Close — P&L: {'+'if pnl>=0 else ''}${pnl:.2f}",
                    html
                )
                # Reset daily summary
                daily_summary = {
                    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "trades": [], "start_value": None, "cycles": 0,
                    "wins": 0, "losses": 0, "trading_halted": False,
                    "morning_brief_sent": False
                }
                trade_diary.clear()
            except Exception as e:
                log.error(f"EOD error: {e}")
        return

    # ── Pull State ───────────────────────────────────────────────
    account         = get_account()
    positions       = get_positions()
    portfolio_value = float(account.get("portfolio_value", 100000))
    cash            = float(account.get("cash", 0))

    if daily_summary["start_value"] is None:
        daily_summary["start_value"] = portfolio_value

    log.info(f"💼 ${portfolio_value:.2f} | Cash: ${cash:.2f} | Positions: {len(positions)} | Time: {time_of_day}")

    # ── Safety Checks ────────────────────────────────────────────
    if daily_summary.get("trading_halted"):
        log.warning("⛔ Trading halted — daily loss limit")
        return
    if check_daily_loss(portfolio_value):
        return

    # ── Trailing Stops & Profit Takes ────────────────────────────
    stops_triggered, profit_takes = update_trailing_stops(positions)
    for stop in stops_triggered:
        result = place_order(stop["symbol"], stop["qty"], "sell")
        if result:
            daily_summary["trades"].append(f"STOP SELL {stop['qty']}x {stop['symbol']} — {stop['reason']}")
            trailing_stops.pop(stop["symbol"], None)
    for take in profit_takes:
        result = place_order(take["symbol"], take["qty"], "sell")
        if result:
            daily_summary["trades"].append(f"PROFIT TAKE {take['qty']}x {take['symbol']} — {take['reason']}")
            daily_summary["wins"] += 1

    if stops_triggered or profit_takes:
        positions = get_positions()

    # ── Intelligence Gathering ───────────────────────────────────
    log.info("🔍 Gathering full intelligence...")
    fear_greed  = get_fear_greed()
    benchmark   = get_benchmark()
    held_syms   = [p["symbol"] for p in positions]
    technicals  = get_technicals(held_syms) if held_syms else {}
    risk_warns  = check_risk(account, positions)
    black_swan  = check_black_swan(positions, portfolio_value)
    memory      = get_memory_summary()
    win_loss    = get_win_loss()

    log.info(f"😨 Fear/Greed: {fear_greed['value']} ({fear_greed['label']})")
    if benchmark:
        log.info(f"📈 SPY: ${benchmark['spy_current']:.2f} ({'+' if benchmark['spy_change_pct']>=0 else ''}{benchmark['spy_change_pct']:.2f}%)")

    log.info("📰 Fetching full market intelligence (ZeroHedge + tiered sources)...")
    intel = get_full_intelligence(positions)

    # Update geopolitical score
    geopolitical_score = intel.get("geopolitical_risk_score", geopolitical_score)
    log.info(f"🌍 Geo Risk: {geopolitical_score}/10 | Mood: {intel.get('market_mood')} | {intel.get('headline_summary','')[:80]}...")

    if intel.get("zerohedge_macro"):
        log.info(f"🔴 ZeroHedge: {intel['zerohedge_macro'][:100]}...")
    if intel.get("narrative_vs_reality"):
        log.info(f"⚡ Reality Gap: {intel['narrative_vs_reality'][:100]}...")

    # Bunker mode check
    bunker_mode = (
        intel.get("bunker_mode_trigger", False) or
        geopolitical_score >= 9 or
        (portfolio_value < 95000 and fear_greed["value"] < 20)
    )
    if bunker_mode:
        log.warning("🏰 BUNKER MODE ACTIVE — shifting to defensive positioning")

    # ── Claude Decision ──────────────────────────────────────────
    log.info("🧠 Consulting Claude v4.0...")
    decision = ask_claude(
        account, positions, intel, technicals, fear_greed,
        memory, risk_warns, black_swan, benchmark, win_loss,
        time_of_day, bunker_mode
    )

    log.info(f"📊 {decision.get('outlook','?').upper()} | Confidence: {decision.get('confidence','?')} | Mix: {decision.get('time_horizon_mix','?')}")
    log.info(f"💭 {decision.get('reasoning','')}")
    if decision.get("macro_themes_active"):
        log.info(f"🎯 Active themes: {', '.join(decision['macro_themes_active'])}")
    if decision.get("risk_note"):
        log.info(f"⚠️ Risk: {decision['risk_note']}")
    if decision.get("watchlist"):
        log.info(f"👀 Watchlist: {', '.join(decision['watchlist'])}")
    if decision.get("diary_note"):
        trade_diary.append({
            "time": datetime.now(timezone.utc).strftime("%H:%M UTC"),
            "diary_note": decision["diary_note"]
        })

    # Check if Claude recommends bunker mode
    if decision.get("bunker_mode_recommend") and not bunker_mode:
        log.warning("🏰 Claude recommends BUNKER MODE — activating")
        bunker_mode = True

    # ── Execute Trades ───────────────────────────────────────────
    position_map = {p["symbol"]: p for p in positions}
    cycle_actions = []

    for action in decision.get("actions", []):
        atype     = action.get("type","").upper()
        symbol    = action.get("symbol","")
        conviction = action.get("conviction","medium")
        price     = float(action.get("price", 0))
        reason    = action.get("reason","")
        horizon   = action.get("time_horizon","medium")

        if not symbol or price <= 0:
            continue

        if atype == "BUY":
            # Check liquidity
            if not is_liquid_enough(symbol):
                log.warning(f"⛔ SKIP {symbol} — insufficient liquidity")
                continue
            # Size by conviction
            shares = get_position_size(conviction, cash, price, geopolitical_score)
            if shares <= 0:
                log.warning(f"⛔ SKIP BUY {symbol} — insufficient cash for {conviction} conviction")
                continue
            cost = shares * price
            log.info(f"🟢 BUY {shares}x {symbol} @ ~${price:.2f} [{conviction} conviction, {horizon} horizon] | {reason}")
            result = place_order(symbol, shares, "buy")
            if result:
                cash -= cost
                trailing_stops[symbol] = price
                daily_summary["trades"].append(f"BUY {shares}x {symbol} @ ${price:.2f} [{conviction}] — {reason}")
                cycle_actions.append(action)

        elif atype == "SELL":
            if symbol not in position_map:
                log.warning(f"⛔ SKIP SELL {symbol} — no position")
                continue
            qty = int(float(position_map[symbol]["qty"]))
            log.info(f"🔴 SELL {qty}x {symbol} @ ~${price:.2f} | {reason}")
            result = place_order(symbol, qty, "sell")
            if result:
                trailing_stops.pop(symbol, None)
                daily_summary["trades"].append(f"SELL {qty}x {symbol} @ ${price:.2f} — {reason}")
                cycle_actions.append(action)

        elif atype == "HOLD":
            log.info(f"⏸️ HOLD {symbol} [{horizon}] — {reason}")
            cycle_actions.append(action)

    pnl = portfolio_value - 100000.0
    update_memory(cycle_num, cycle_actions, portfolio_value, pnl)
    log.info(f"✅ CYCLE {cycle_num} DONE | ${portfolio_value:.2f} | P&L: {'+'if pnl>=0 else ''}${pnl:.2f}\n")

# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    log.info("╔══════════════════════════════════════════════════════╗")
    log.info("║       RIDGELINE AI TRADING AGENT v4.0                ║")
    log.info("║       Full Spectrum Macro Intelligence Edition        ║")
    log.info("╠══════════════════════════════════════════════════════╣")
    log.info(f"║  Cycles: every {CYCLE_MINUTES} minutes                              ║")
    log.info("║                                                      ║")
    log.info("║  INTELLIGENCE:                                       ║")
    log.info("║  ✅ ZeroHedge + Tiered News Source Weighting         ║")
    log.info("║  ✅ Narrative vs Reality Gap Detection                ║")
    log.info("║  ✅ Macro Theme Watchlists (AI/Gold/Nuclear/Defense)  ║")
    log.info("║  ✅ Defense Restocking Cycle Awareness                ║")
    log.info("║  ✅ Fed Trap + Money Printing Positioning             ║")
    log.info("║  ✅ Geopolitical Risk Scoring (1-10)                  ║")
    log.info("║  ✅ Contrarian Indicator Awareness                    ║")
    log.info("║  ✅ Insider Buying + Short Squeeze Detection          ║")
    log.info("║  ✅ AI IPO Watch (Anthropic/OpenAI/SpaceX)            ║")
    log.info("║  ✅ Earnings + Fed + Options Expiration Calendar      ║")
    log.info("║                                                      ║")
    log.info("║  RISK MANAGEMENT:                                    ║")
    log.info("║  ✅ Conviction-Based Position Sizing                  ║")
    log.info("║  ✅ Trailing Stop Losses (7%)                         ║")
    log.info("║  ✅ Profit Taking Rules (18%)                         ║")
    log.info("║  ✅ Liquidity Filter                                  ║")
    log.info("║  ✅ Black Swan Detector                               ║")
    log.info("║  ✅ Daily Loss Circuit Breaker                        ║")
    log.info("║  ✅ Bunker Mode (Wealth Preservation)                 ║")
    log.info("║  ✅ Time of Day Awareness                             ║")
    log.info("║                                                      ║")
    log.info("║  REPORTING:                                          ║")
    log.info("║  ✅ 7am MDT Morning Intelligence Brief                ║")
    log.info("║  ✅ Daily Close Email Report                          ║")
    log.info("║  ✅ Trade Diary + Performance Memory                  ║")
    log.info("║  ✅ Benchmark vs S&P 500                              ║")
    log.info("║  ✅ Three Time Horizon Framework                      ║")
    log.info("╚══════════════════════════════════════════════════════╝\n")

    # ── Startup Email Test ───────────────────────────────────────
    log.info("📧 Testing email configuration on startup...")
    if all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD]):
        try:
            account    = get_account()
            positions  = get_positions()
            intel      = get_full_intelligence(positions)
            fear_greed = get_fear_greed()
            benchmark  = get_benchmark()
            html = build_morning_brief(account, positions, intel, fear_greed, benchmark)
            send_email(
                f"🌄 Ridgeline Morning Brief — {datetime.now(timezone.utc).strftime('%b %d')} — EMAIL TEST",
                html
            )
            daily_summary["morning_brief_sent"] = True
            log.info("✅ Startup email sent successfully!")
        except Exception as e:
            log.error(f"Startup email failed: {e}")
    else:
        log.warning("⚠️ Email not configured — check EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD")

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
