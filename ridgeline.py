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
#   RIDGELINE AI TRADING AGENT v4.2
#   Elite Intelligence Edition
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
MOUNTAIN_OFFSET  = -6

HEADERS_ALPACA = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json"
}

# ── State ────────────────────────────────────────────────────────
performance_memory    = []
trade_diary           = []
trailing_stops        = {}
benchmark_start       = None
geopolitical_score    = 5
decision_log          = []   # Every decision with full context for learning
outcome_log           = []   # Results attributed to each decision
sector_performance    = {}   # Sector-level P&L tracking
regime_state          = "neutral"  # bullish / bearish / neutral / transitioning
last_vix              = 20
daily_summary = {
    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    "trades": [], "start_value": None, "cycles": 0,
    "wins": 0, "losses": 0, "trading_halted": False,
    "morning_brief_sent": False, "opening_hour_locked": False
}

# ── Macro Theme Watchlists ────────────────────────────────────────
MACRO_THEMES = {
    "ai_ipo":        ["NVDA", "MSFT", "GOOGL", "AMD", "META", "AVGO"],
    "gold_silver":   ["GLD", "SLV", "GDXJ", "AG", "PAAS"],
    "nuclear":       ["SMR", "OKLO", "LEU", "URG", "VST", "CCJ", "CEG", "NLR"],
    "defense":       ["KTOS", "AVAV", "RKLB", "PLTR", "AXON", "TDG", "HWM", "LMT", "RTX", "NOC"],
    "energy":        ["XOM", "OXY", "DVN", "COP", "LNG"],
    "inflation_hedge": ["GLD", "SLV", "BRK-B", "VPU", "XLE"],
    "short_plays":   ["SQQQ", "SH", "PSQ", "DOG"],
    "recession_safe": ["JNJ", "PG", "KO", "WMT", "VZ", "T"],
    "satellite_moonshots": [
        "URG", "UUUU", "NNE", "RCAT", "LUNR",
        "IMSR", "KTOS", "AVAV", "HIMS", "GEVO"
    ]
}

SECTOR_MAP = {
    # Defense
    "LMT": "defense", "RTX": "defense", "NOC": "defense", "KTOS": "defense",
    "AVAV": "defense", "RKLB": "defense", "PLTR": "defense", "AXON": "defense",
    "TDG": "defense", "HWM": "defense", "RCAT": "defense", "LUNR": "defense",
    # Nuclear
    "SMR": "nuclear", "OKLO": "nuclear", "LEU": "nuclear", "URG": "nuclear",
    "VST": "nuclear", "CCJ": "nuclear", "CEG": "nuclear", "NLR": "nuclear",
    "UUUU": "nuclear", "NNE": "nuclear", "IMSR": "nuclear",
    # Gold/Silver
    "GLD": "gold_silver", "SLV": "gold_silver", "GDXJ": "gold_silver",
    "GDX": "gold_silver", "AG": "gold_silver", "PAAS": "gold_silver",
    # AI/Tech
    "NVDA": "ai_tech", "MSFT": "ai_tech", "GOOGL": "ai_tech", "AMD": "ai_tech",
    "META": "ai_tech", "AVGO": "ai_tech",
    # Energy
    "XOM": "energy", "CVX": "energy", "OXY": "energy", "DVN": "energy",
    "COP": "energy", "LNG": "energy",
    # Hedge
    "SQQQ": "hedge", "SH": "hedge", "PSQ": "hedge", "DOG": "hedge",
}

# ── Portfolio Allocation Targets (v4.2) ──────────────────────────
ALLOCATION_TARGETS = {
    "core_macro":          0.70,
    "momentum_accelerator": 0.05,
    "satellite":           0.12,
    "hedge":               0.03,
    "cash_reserve":        0.10,
}

# ── Position Sizing Rules ────────────────────────────────────────
MAX_SINGLE_POSITION_PCT  = 0.20
MAX_SECTOR_PCT           = 0.25
MIN_POSITION_VALUE       = 500
SATELLITE_MAX_SINGLE_PCT = 0.03
MOMENTUM_ADD_THRESHOLD   = 0.10   # Add to winners up 10%+
TRAILING_STOP_PCT        = 0.07

# ── Multi-tier Profit Ladder ─────────────────────────────────────
PROFIT_TIERS = [
    (0.10, 0.15),  # At +10%, sell 15%
    (0.20, 0.25),  # At +20%, sell 25% more
    (0.35, 0.25),  # At +35%, sell 25% more
    # Remaining 35% runs freely
]

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
    payload = {"symbol": symbol, "qty": qty, "side": side, "type": "market", "time_in_force": "day"}
    r = requests.post(f"{ALPACA_BASE}/orders", headers=HEADERS_ALPACA, json=payload, timeout=10)
    if r.status_code in (200, 201):
        log.info(f"✅ ORDER: {side.upper()} {qty}x {symbol}")
        return r.json()
    log.warning(f"❌ ORDER FAILED: {symbol} {side} — {r.text}")
    return None

# ═══════════════════════════════════════════════════════════════
# TECHNICALS & PATTERNS
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

def detect_pattern(symbol, bars):
    """Quantified pattern recognition"""
    if len(bars) < 20:
        return {}
    closes = [b["c"] for b in bars]
    highs  = [b["h"] for b in bars]
    current = closes[-1]
    sma20 = calculate_sma(closes, 20)
    sma50 = calculate_sma(closes, 50) if len(closes) >= 50 else None
    recent_high = max(highs[-20:])
    pct_from_high = (current - recent_high) / recent_high * 100
    pct_above_sma20 = ((current - sma20) / sma20 * 100) if sma20 else 0

    patterns = {}
    if pct_above_sma20 > 5:
        patterns["overextended"] = f"{pct_above_sma20:.1f}% above 20-day SMA — mean reversion likely"
    if pct_above_sma20 < -5:
        patterns["oversold"] = f"{abs(pct_above_sma20):.1f}% below 20-day SMA — bounce candidate"
    if pct_from_high < -3 and pct_from_high > -8:
        patterns["pullback_buy"] = f"Healthy pullback {abs(pct_from_high):.1f}% from 20-day high"
    if pct_from_high > -1:
        patterns["near_highs"] = "Near recent highs — momentum or exhaustion watch"
    return patterns

def get_technicals(symbols):
    analysis = {}
    for symbol in symbols[:10]:
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
        patterns = detect_pattern(symbol, bars)
        analysis[symbol] = {
            "rsi": rsi, "sma10": sma10, "sma20": sma20, "sma50": sma50,
            "macd": macd, "macd_signal": signal,
            "volume_spike": vol_spike,
            "trend_short": trend_short, "trend_long": trend_long,
            "current_price": closes[-1] if closes else None,
            "patterns": patterns
        }
        pattern_str = ", ".join(patterns.keys()) or "none"
        log.info(f"📊 {symbol}: RSI={rsi} {trend_short}/{trend_long} MACD={macd} Patterns=[{pattern_str}]")
    return analysis

# ═══════════════════════════════════════════════════════════════
# VIX & MARKET REGIME
# ═══════════════════════════════════════════════════════════════

def get_vix_approx():
    """Approximate VIX via SPY volatility and fear/greed proxy"""
    bars = get_bars("SPY", limit=20)
    if not bars or len(bars) < 10:
        return 20  # Default
    closes = [b["c"] for b in bars]
    # Calculate rolling volatility
    returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
    if not returns:
        return 20
    avg = sum(returns) / len(returns)
    variance = sum((r - avg)**2 for r in returns) / len(returns)
    std_dev = variance ** 0.5
    # Annualize approximation
    vix_approx = std_dev * (252 ** 0.5) * 100
    return round(vix_approx, 1)

def detect_regime(benchmark, fear_greed, vix):
    """Identify current market regime"""
    if not benchmark:
        return "neutral", "No benchmark data"

    spy_change = benchmark["spy_change_pct"]
    fg_val = fear_greed["value"]

    if fg_val < 25 and vix > 25:
        return "fear_opportunity", "Extreme fear + elevated VIX — contrarian buy zone"
    elif fg_val > 75 and spy_change > 3:
        return "greed_caution", "Extreme greed + strong rally — take profits, add hedges"
    elif spy_change > 2 and vix < 18:
        return "risk_on", "Rally with low vol — position aggressively in winners"
    elif spy_change < -2 and vix > 22:
        return "risk_off", "Decline with rising vol — defensive rotation"
    else:
        return "neutral", "Mixed signals — selective positioning"

# ═══════════════════════════════════════════════════════════════
# PRE-MARKET & GAP AWARENESS
# ═══════════════════════════════════════════════════════════════

def check_premarket_gap():
    """Check if SPY is gapping significantly vs yesterday's close"""
    bars = get_bars("SPY", limit=3)
    if len(bars) < 2:
        return None
    yesterday_close = bars[-2]["c"]
    current = get_latest_price("SPY") or bars[-1]["c"]
    gap_pct = (current - yesterday_close) / yesterday_close * 100
    if abs(gap_pct) > 0.5:
        return {"gap_pct": round(gap_pct, 2), "direction": "up" if gap_pct > 0 else "down"}
    return None

# ═══════════════════════════════════════════════════════════════
# MARKET SENTIMENT & INTELLIGENCE
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

def get_full_intelligence(positions, regime_info):
    """v4.2: Comprehensive intelligence with Polymarket, X/FinTwit, institutional data"""
    held = [p["symbol"] for p in positions] if positions else []
    symbols_str = ", ".join(held) if held else "none currently"
    themes_str  = ", ".join([f"{k}: {v[:3]}" for k, v in MACRO_THEMES.items()])

    prompt = f"""You are Ridgeline v4.2's elite intelligence analyst. Search comprehensively across multiple sources:

HIGH TRUST SOURCES:
- ZeroHedge (contrarian macro, key perspective)
- Bloomberg, Reuters wire, WSJ, FT
- Fed statements, SEC EDGAR filings
- Polymarket (prediction probabilities)
- X/FinTwit via search (breaking signals, Elon tweets, Trump posts moving markets)
- Goldman/JPM/Morgan Stanley research (when reported)
- 13F filings (institutional positioning)
- Congressional trading disclosures (Pelosi tracker)
- Patent filings, job postings (expansion signals)

MEDIUM TRUST: CNBC data, MarketWatch, Barron's
LOW TRUST / IGNORE ANALYSIS: CNN, MSNBC, NYT, WaPo — use raw facts only

Current held positions: {symbols_str}
Theme watchlists: {themes_str}
Current regime: {regime_info}

Search for and return comprehensive intelligence on:

1. Top market-moving news last 24 hours (weight ZeroHedge and FinTwit)
2. Polymarket probabilities on: recession, Fed rate cuts, Iran ceasefire, major elections, AI IPOs
3. X/FinTwit breaking news — any significant posts from Elon, Trump, key financial accounts
4. Fed speaker calendar and rate expectations
5. Earnings reports next 5 days — with expected vs prior quarter
6. Options expiration (3rd Friday awareness)
7. Unusual options activity / dark pool prints (via public sources)
8. Analyst upgrades/downgrades for held + watchlist
9. Recent 13F filings — what institutions are buying/selling
10. Congressional trading alerts (Pelosi tracker, etc.)
11. SEC EDGAR filings — 8-K material events for held names
12. Defense restocking news
13. Gold/silver/nuclear macro
14. Inflation reality vs narrative
15. Insider buying (Form 4)
16. Short squeeze candidates
17. AI IPO news (Anthropic/OpenAI/SpaceX)
18. Pre-market movers and futures
19. Economic calendar (CPI, PPI, jobs, GDP releases this week)
20. Treasury auction results (demand for US debt)
21. Credit spreads widening/tightening (bond market signals)
22. Patent filings from NVDA/AMD/major tech revealing R&D direction
23. Job posting surges suggesting expansion (ex: defense contractors hiring)
24. ETF flows (money flooding in or fleeing from sectors)

Rate geopolitical risk 1-10 and identify regime shift signals.

Respond ONLY with valid JSON:
{{
  "headline_summary": "4-5 sentence macro overview",
  "market_mood": "risk-on|risk-off|neutral|transitioning",
  "narrative_vs_reality": "key gap today",
  "geopolitical_risk_score": <1-10>,
  "regime_shift_detected": <true|false>,
  "regime_shift_note": "what's changing",
  "key_events": ["event1", "event2"],
  "polymarket_probabilities": {{"topic": "probability"}},
  "fintwit_signals": ["notable X posts moving markets"],
  "fed_calendar": "next Fed meeting + speakers this week",
  "earnings_upcoming": {{"SYMBOL": "date + expected"}},
  "options_expiration": "next date",
  "unusual_options": ["notable flow"],
  "analyst_ratings": {{"SYMBOL": "action + target"}},
  "institutional_moves": ["13F highlights"],
  "congressional_trades": ["notable political trades"],
  "sec_filings_alerts": ["8-K material events for held names"],
  "defense_news": "",
  "gold_silver_nuclear": "",
  "inflation_reality": "",
  "insider_buying": ["Form 4 alerts"],
  "short_squeeze_candidates": ["high SI + catalyst"],
  "ai_ipo_news": "",
  "zerohedge_macro": "key contrarian thesis",
  "hot_sectors": ["sector1", "sector2"],
  "avoid_sectors": ["sector1"],
  "stock_news": {{"SYMBOL": "news"}},
  "premarket_movers": ["big moves"],
  "economic_calendar": ["CPI Thursday", "etc"],
  "credit_spreads": "tightening|widening|stable",
  "etf_flows": "where money is moving",
  "patent_signals": ["any meaningful patents"],
  "job_posting_signals": ["expansion signals"],
  "bunker_mode_trigger": false,
  "contrarian_opportunity": "any extreme sentiment creating opportunity"
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
                "max_tokens": 3000,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=90
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
        "market_mood": "neutral", "narrative_vs_reality": "",
        "geopolitical_risk_score": 5, "regime_shift_detected": False,
        "regime_shift_note": "", "key_events": [],
        "polymarket_probabilities": {}, "fintwit_signals": [],
        "fed_calendar": "", "earnings_upcoming": {},
        "options_expiration": "", "unusual_options": [],
        "analyst_ratings": {}, "institutional_moves": [],
        "congressional_trades": [], "sec_filings_alerts": [],
        "defense_news": "", "gold_silver_nuclear": "",
        "inflation_reality": "", "insider_buying": [],
        "short_squeeze_candidates": [], "ai_ipo_news": "",
        "zerohedge_macro": "", "hot_sectors": [], "avoid_sectors": [],
        "stock_news": {}, "premarket_movers": [],
        "economic_calendar": [], "credit_spreads": "stable",
        "etf_flows": "", "patent_signals": [],
        "job_posting_signals": [], "bunker_mode_trigger": False,
        "contrarian_opportunity": ""
    }

# ═══════════════════════════════════════════════════════════════
# SECTOR TRACKING
# ═══════════════════════════════════════════════════════════════

def get_sector_exposure(positions, portfolio_value):
    exposure = {}
    for p in positions:
        sector = SECTOR_MAP.get(p["symbol"], "other")
        mv = float(p.get("market_value", 0))
        exposure[sector] = exposure.get(sector, 0) + mv
    return {s: (v, v/portfolio_value*100) for s, v in exposure.items()}

def check_sector_limits(positions, portfolio_value):
    warnings = []
    exposure = get_sector_exposure(positions, portfolio_value)
    for sector, (value, pct) in exposure.items():
        if pct > MAX_SECTOR_PCT * 100:
            warnings.append(f"SECTOR {sector.upper()}: {pct:.1f}% of portfolio — trim")
    return warnings

# ═══════════════════════════════════════════════════════════════
# TRAILING STOPS & MULTI-TIER PROFIT LADDER
# ═══════════════════════════════════════════════════════════════

def update_trailing_stops_and_profits(positions):
    stops_triggered = []
    profit_ladder_sales = []

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
            log.warning(f"🛑 TRAILING STOP: {symbol} @ ${current:.2f}")
            stops_triggered.append({
                "symbol": symbol, "qty": qty, "price": current,
                "reason": f"7% trailing stop from high ${trailing_stops[symbol]:.2f}"
            })
            continue

        # Multi-tier profit ladder
        stored_tier = performance_memory[-1].get("profit_tiers_hit", {}).get(symbol, 0) if performance_memory else 0
        for tier_idx, (threshold, sell_pct) in enumerate(PROFIT_TIERS):
            if gain_pct >= threshold * 100 and tier_idx >= stored_tier:
                shares_to_sell = max(1, int(qty * sell_pct))
                if shares_to_sell >= qty:
                    continue  # Don't close entirely via ladder
                log.info(f"💰 PROFIT LADDER TIER {tier_idx+1}: {symbol} +{gain_pct:.1f}% — selling {shares_to_sell}")
                profit_ladder_sales.append({
                    "symbol": symbol, "qty": shares_to_sell, "price": current,
                    "tier": tier_idx + 1,
                    "reason": f"Profit ladder tier {tier_idx+1} at +{gain_pct:.1f}%"
                })
                break  # Only one tier per cycle

    return stops_triggered, profit_ladder_sales

# ═══════════════════════════════════════════════════════════════
# PERFORMANCE ATTRIBUTION & LEARNING
# ═══════════════════════════════════════════════════════════════

def log_decision(cycle_num, action, context):
    """Log every decision with full context for learning"""
    decision_log.append({
        "cycle": cycle_num,
        "time": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "context": context
    })
    # Keep last 200 decisions in memory
    if len(decision_log) > 200:
        decision_log.pop(0)

def attribute_outcome(symbol, entry_price, exit_price, reason_entered):
    """Attribute P&L to specific decision reasoning"""
    pnl_pct = (exit_price - entry_price) / entry_price * 100
    outcome = "win" if pnl_pct > 0 else "loss"
    attribution = {
        "symbol": symbol,
        "pnl_pct": round(pnl_pct, 2),
        "outcome": outcome,
        "reason_entered": reason_entered,
        "attribution": f"{outcome.upper()}: {abs(pnl_pct):.1f}% — thesis was: {reason_entered[:60]}"
    }
    outcome_log.append(attribution)
    if len(outcome_log) > 100:
        outcome_log.pop(0)
    return attribution

def get_learning_summary():
    """Generate learning insights from decision history"""
    if len(outcome_log) < 3:
        return "Insufficient history for learning insights yet."

    wins = [o for o in outcome_log if o["outcome"] == "win"]
    losses = [o for o in outcome_log if o["outcome"] == "loss"]
    win_rate = len(wins) / len(outcome_log) * 100 if outcome_log else 0
    avg_win = sum(o["pnl_pct"] for o in wins) / len(wins) if wins else 0
    avg_loss = sum(o["pnl_pct"] for o in losses) / len(losses) if losses else 0

    recent_wins = wins[-5:] if wins else []
    recent_losses = losses[-5:] if losses else []

    insights = f"Win rate: {win_rate:.0f}% ({len(wins)}W/{len(losses)}L). "
    insights += f"Avg win: +{avg_win:.1f}% | Avg loss: {avg_loss:.1f}%. "
    if recent_wins:
        insights += f"Recent wins: {'; '.join([f'{w['symbol']} +{w['pnl_pct']:.1f}%' for w in recent_wins[-3:]])}. "
    if recent_losses:
        insights += f"Recent losses: {'; '.join([f'{l['symbol']} {l['pnl_pct']:.1f}%' for l in recent_losses[-3:]])}."
    return insights

def get_memory_summary():
    if not performance_memory:
        return "No trading history yet."
    recent = performance_memory[-15:]
    pnls = [e["pnl"] for e in recent]
    trend = "improving" if len(pnls) > 1 and pnls[-1] > pnls[0] else "declining" if len(pnls) > 1 and pnls[-1] < pnls[0] else "flat"
    return f"P&L trend: {trend} over {len(recent)} cycles."

def get_win_loss():
    wins = daily_summary["wins"]
    losses = daily_summary["losses"]
    total = wins + losses
    if total == 0:
        return "No closed trades today"
    return f"{wins}W / {losses}L ({wins/total*100:.0f}% win rate today)"

# ═══════════════════════════════════════════════════════════════
# POSITION SIZING (v4.2 with volatility awareness)
# ═══════════════════════════════════════════════════════════════

def get_position_size(conviction, cash, price, geopolitical_risk, vix, is_satellite=False):
    if is_satellite:
        base_pct = SATELLITE_MAX_SINGLE_PCT
    else:
        base_pct = {
            "high":        0.25,
            "medium":      0.15,
            "low":         0.07,
            "speculative": 0.04
        }.get(conviction, 0.10)

    # Geopolitical risk adjustment
    geo_multiplier = max(0.5, 1.0 - (geopolitical_risk - 5) * 0.08)

    # VIX-based volatility adjustment
    # VIX 15 = full size, VIX 30+ = half size
    vix_multiplier = max(0.5, min(1.0, (30 - vix) / 15))

    adjusted_pct = base_pct * geo_multiplier * vix_multiplier

    max_spend = (cash - 1000) * adjusted_pct
    if price <= 0 or max_spend <= 0:
        return 0
    return max(1, int(max_spend / price))

def is_liquid_enough(symbol):
    bars = get_bars(symbol, limit=5)
    if not bars:
        return True
    avg_vol = sum(b["v"] for b in bars) / len(bars)
    return avg_vol > 50000

# ═══════════════════════════════════════════════════════════════
# RISK & BLACK SWAN
# ═══════════════════════════════════════════════════════════════

def check_risk(account, positions):
    warnings = []
    portfolio_value = float(account.get("portfolio_value", 100000))
    cash = float(account.get("cash", 0))
    for p in positions:
        mv  = float(p.get("market_value", 0))
        pct = mv / portfolio_value * 100
        if pct > MAX_SINGLE_POSITION_PCT * 100:
            warnings.append(f"CONCENTRATION: {p['symbol']} = {pct:.1f}% of portfolio — trim")
    unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    if unrealized < -(portfolio_value * 0.05):
        warnings.append(f"DRAWDOWN: Unrealized loss ${abs(unrealized):.2f} > 5%")
    if cash < 1000:
        warnings.append(f"CASH LOW: ${cash:.2f}")
    return warnings

def check_daily_loss(portfolio_value):
    if daily_summary["start_value"] is None:
        return False
    pct = (portfolio_value - daily_summary["start_value"]) / daily_summary["start_value"] * 100
    if pct < -MAX_DAILY_LOSS:
        log.warning(f"🚨 DAILY LOSS LIMIT: Down {abs(pct):.2f}%")
        daily_summary["trading_halted"] = True
        return True
    return False

def check_black_swan(positions, portfolio_value):
    warnings = []
    start = daily_summary.get("start_value", 100000)
    if start and (portfolio_value - start) / start * 100 < -10:
        warnings.append(f"BLACK SWAN: Portfolio down >10%")
    for p in positions:
        pl_pct = float(p.get("unrealized_plpc", 0)) * 100
        if pl_pct < -20:
            warnings.append(f"BLACK SWAN: {p['symbol']} down {abs(pl_pct):.1f}%")
    return warnings

# ═══════════════════════════════════════════════════════════════
# DYNAMIC HEDGE TRIGGER
# ═══════════════════════════════════════════════════════════════

def should_add_hedge(portfolio_value, benchmark, vix, fear_greed):
    """Determine if we should dynamically add inverse ETF hedge"""
    start = daily_summary.get("start_value", 100000)
    if not start:
        return False, ""
    daily_pnl_pct = (portfolio_value - start) / start * 100
    signals = []
    if daily_pnl_pct < -2:
        signals.append(f"Portfolio down {abs(daily_pnl_pct):.1f}% today")
    if vix > 25:
        signals.append(f"VIX elevated at {vix}")
    if benchmark and benchmark["spy_change_pct"] < -2:
        signals.append(f"SPY down {abs(benchmark['spy_change_pct']):.1f}%")
    if fear_greed["value"] < 25:
        signals.append(f"Extreme fear ({fear_greed['value']})")

    if len(signals) >= 2:
        return True, "; ".join(signals)
    return False, ""

# ═══════════════════════════════════════════════════════════════
# TIME AWARENESS
# ═══════════════════════════════════════════════════════════════

def get_time_context():
    now_mtn = datetime.now(timezone.utc) + timedelta(hours=MOUNTAIN_OFFSET)
    hour, minute = now_mtn.hour, now_mtn.minute
    weekday = now_mtn.weekday()  # 0=Mon, 4=Fri

    context = {"time_of_day": "normal", "is_friday_afternoon": False, "opening_hour": False, "closing_hour": False}

    if hour == 7 and minute < 30:
        context["time_of_day"] = "pre-market"
    elif hour == 7 and minute >= 30:
        context["time_of_day"] = "opening-bell"
        context["opening_hour"] = True
    elif hour == 8 and minute <= 30:
        context["opening_hour"] = True
        context["time_of_day"] = "opening-volatile"
    elif hour < 13:
        context["time_of_day"] = "morning-session"
    elif hour < 14:
        context["time_of_day"] = "midday-calm"
    elif hour >= 14 and hour <= 15:
        context["time_of_day"] = "afternoon-session"
    elif hour == 15 and minute >= 30:
        context["closing_hour"] = True
        context["time_of_day"] = "closing-institutional"

    if weekday == 4 and hour >= 13:
        context["is_friday_afternoon"] = True

    return context

def should_send_morning_brief():
    now_mtn = datetime.now(timezone.utc) + timedelta(hours=MOUNTAIN_OFFSET)
    return (now_mtn.hour == 7 and now_mtn.minute < 15
            and not daily_summary.get("morning_brief_sent", False))

# ═══════════════════════════════════════════════════════════════
# CLAUDE BRAIN v4.2
# ═══════════════════════════════════════════════════════════════

def ask_claude(account, positions, intel, technicals, fear_greed, vix,
               memory, risk_warns, sector_warns, black_swan, benchmark,
               win_loss, time_ctx, bunker_mode, regime_info, learning_summary,
               gap_info, sector_exposure, should_hedge):

    cash            = float(account.get("cash", 0))
    portfolio_value = float(account.get("portfolio_value", 0))
    pnl             = portfolio_value - 100000.0
    pnl_pct         = (pnl / 100000.0) * 100
    geo_risk        = intel.get("geopolitical_risk_score", geopolitical_score)

    bench_str = f"SPY: ${benchmark['spy_current']:.2f} ({'+' if benchmark['spy_change_pct']>=0 else ''}{benchmark['spy_change_pct']:.2f}%)" if benchmark else "unavailable"

    pos_str = "No open positions"
    if positions:
        pos_str = "\n".join([
            f"  {p['symbol']} [{SECTOR_MAP.get(p['symbol'], 'other')}]: {p['qty']} shares @ ${float(p['avg_entry_price']):.2f} avg, "
            f"now ${float(p['current_price']):.2f}, "
            f"P&L ${float(p['unrealized_pl']):.2f} ({float(p['unrealized_plpc'])*100:.1f}%), "
            f"high: ${trailing_stops.get(p['symbol'], float(p['current_price'])):.2f}"
            for p in positions
        ])

    tech_str = "\n".join([
        f"  {s}: RSI={d['rsi']} {d['trend_short']}/{d['trend_long']} Patterns=[{', '.join(d.get('patterns',{}).keys()) or 'none'}]"
        for s, d in technicals.items()
    ]) or "None"

    sector_str = "\n".join([
        f"  {sector}: ${value:.2f} ({pct:.1f}%)"
        for sector, (value, pct) in sector_exposure.items()
    ]) or "No positions"

    themes_str = json.dumps({k: v[:5] for k, v in MACRO_THEMES.items()})

    bunker_note = ""
    if bunker_mode:
        bunker_note = """
⚠️ BUNKER MODE ACTIVE: Prioritize GLD, SLV, defensive dividend stocks, cash.
Avoid high multiple growth, consumer discretionary, highly leveraged companies.
Consider SQQQ hedge, recession-safe positions, energy independence plays."""

    time_note = ""
    if time_ctx.get("opening_hour"):
        time_note = "⚠️ OPENING HOUR: Avoid large new positions unless high conviction. First 30 min volatility distorts prices."
    if time_ctx.get("closing_hour"):
        time_note = "⚠️ CLOSING HOUR: Watch unusual volume — often institutional repositioning. Good signal quality."
    if time_ctx.get("is_friday_afternoon"):
        time_note += "\n⚠️ FRIDAY PM: Raise cash slightly. Geopolitical events often erupt over weekends."

    hedge_note = ""
    if should_hedge[0]:
        hedge_note = f"🛡️ DYNAMIC HEDGE RECOMMENDED: {should_hedge[1]}. Consider allocating 3-5% to SQQQ/SH."

    gap_note = ""
    if gap_info:
        gap_note = f"🌅 PRE-MARKET GAP: SPY gapped {gap_info['direction']} {gap_info['gap_pct']}%. Adjust opening strategy."

    prompt = f"""You are RIDGELINE v4.2 — ELITE Intelligence Edition. You learn from past decisions and continuously evolve.

PHILOSOPHY:
- Think across 3 horizons: quick wins (days), medium term (weeks-months), long game (months-years)
- Trust contrarian macro over mainstream consensus
- The Fed is trapped between inflation and recession
- Iran war = multi-year defense/energy/nuclear catalyst
- Reported inflation understates real purchasing power erosion
- LEARN from your past trades — what's worked, what hasn't
- Personality: Calculated risk-taker between balanced and aggressive
{bunker_note}
{time_note}
{hedge_note}
{gap_note}

═══════════════════════════════════════
PORTFOLIO STATE
═══════════════════════════════════════
Cash: ${cash:.2f}
Portfolio Value: ${portfolio_value:.2f}
Total P&L: {"+" if pnl>=0 else ""}${pnl:.2f} ({("+" if pnl_pct>=0 else "")}{pnl_pct:.2f}%)
vs S&P 500: {bench_str}
VIX (approx): {vix}
Regime: {regime_info}
Geo Risk: {geo_risk}/10
Win/Loss today: {win_loss}
Time: {time_ctx['time_of_day']}

Open Positions:
{pos_str}

Sector Exposure:
{sector_str}

═══════════════════════════════════════
ELITE INTELLIGENCE (v4.2)
═══════════════════════════════════════
Headline: {intel.get("headline_summary","")}
Mood: {intel.get("market_mood","neutral")}
Regime Shift: {intel.get("regime_shift_detected", False)} — {intel.get("regime_shift_note","")}
Narrative vs Reality: {intel.get("narrative_vs_reality","")}
ZeroHedge Macro: {intel.get("zerohedge_macro","")}
Polymarket Probabilities: {json.dumps(intel.get("polymarket_probabilities",{}))}
FinTwit Signals: {intel.get("fintwit_signals",[])}
Institutional Moves (13F): {intel.get("institutional_moves",[])}
Congressional Trades: {intel.get("congressional_trades",[])}
SEC Filings (8-K): {intel.get("sec_filings_alerts",[])}
Fed Calendar: {intel.get("fed_calendar","")}
Earnings Upcoming: {json.dumps(intel.get("earnings_upcoming",{}))}
Economic Calendar: {intel.get("economic_calendar",[])}
Treasury/Credit: {intel.get("credit_spreads","")}
ETF Flows: {intel.get("etf_flows","")}
Patent Signals: {intel.get("patent_signals",[])}
Job Posting Signals: {intel.get("job_posting_signals",[])}
Options Flow: {intel.get("unusual_options",[])}
Analyst Ratings: {json.dumps(intel.get("analyst_ratings",{}))}
Defense: {intel.get("defense_news","")}
Gold/Silver/Nuclear: {intel.get("gold_silver_nuclear","")}
Inflation Reality: {intel.get("inflation_reality","")}
AI IPO: {intel.get("ai_ipo_news","")}
Insider Buying: {intel.get("insider_buying",[])}
Short Squeeze Candidates: {intel.get("short_squeeze_candidates",[])}
Hot Sectors: {intel.get("hot_sectors",[])}
Avoid Sectors: {intel.get("avoid_sectors",[])}
Pre-market: {intel.get("premarket_movers",[])}
Contrarian Opportunity: {intel.get("contrarian_opportunity","")}

Fear & Greed: {fear_greed["value"]}/100 ({fear_greed["label"]})

═══════════════════════════════════════
TECHNICAL ANALYSIS (with patterns)
═══════════════════════════════════════
{tech_str}

═══════════════════════════════════════
LEARNING FROM HISTORY
═══════════════════════════════════════
{learning_summary}

{memory}

═══════════════════════════════════════
RISK ALERTS
═══════════════════════════════════════
Position Risk: {chr(10).join(risk_warns) if risk_warns else "None"}
Sector Risk: {chr(10).join(sector_warns) if sector_warns else "None"}
Black Swan: {chr(10).join(black_swan) if black_swan else "None"}

═══════════════════════════════════════
TRADING RULES v4.2
═══════════════════════════════════════

ALLOCATION TARGETS (manage toward these):
- 70% core macro positions
- 5% momentum accelerator (add to winners up 10%+)
- 12% satellite moonshots (small caps with binary catalysts)
- 3-8% dynamic hedge (inverse ETFs when signals warrant)
- 5-10% cash reserve

POSITION MANAGEMENT:
- Max 20% in any single position
- Max 25% in any single sector
- Min $500 per position (exit smaller)
- No redundant correlated holdings (pick ONE of GDX/GDXJ, ONE of OKLO/SMR)
- 7% trailing stops auto-trigger
- Profit ladder: sell 15% at +10%, 25% at +20%, 25% at +35%, let rest run

SATELLITE RULES (12% allocation):
- Max 3% per individual satellite
- Candidates: URG, UUUU, NNE, RCAT, LUNR, IMSR, GEVO
- Moonshot bets — size small, never chase
- Take half off when satellite doubles

OFFENSIVE RULES:
- "Buy the Dip" — if core position drops 5-8% on NO fundamental news, ADD (not exit)
- "Sell the Rip" — if position spikes 20%+ on euphoria with no catalyst, TRIM
- Momentum accelerator — add 20-30% more to positions already up 10%+ with strong technicals
- Anti-herd — fade extreme F&G (<25 buy, >75 trim)

DEFENSIVE RULES:
- If 2+ hedge signals align, allocate 3-5% to SQQQ/SH
- Opening hour (first 30 min): no large new positions
- Closing hour (last 30 min): watch unusual volume for institutional signals
- Friday afternoon: raise cash 5%, weekend geo risk
- Pre-earnings: reduce exposure unless conviction extreme
- Post-earnings-miss: do NOT buy the dip (usually 2-3 week decline)
- Post-earnings-beat: consider adding on first pullback

LEARNING:
- Review learning summary — adjust based on what's worked
- If recent losses cluster in a sector/theme, reduce that exposure
- If recent wins cluster, continue that pattern with discipline
- Note patterns: momentum chases losing? Mean reversion working?

POSITION SIZING: Specify conviction. System calculates shares based on conviction + VIX + geo risk.

Respond ONLY with valid JSON:
{{
  "reasoning": "5-6 sentences covering: macro view, regime assessment, learning applied, technical setup, risk/reward",
  "outlook": "bullish|bearish|neutral",
  "confidence": "high|medium|low",
  "regime_assessment": "current market regime assessment",
  "time_horizon_mix": "quick X% / medium Y% / long Z%",
  "geopolitical_assessment": "brief geo assessment",
  "learning_applied": "how past lessons inform this cycle",
  "actions": [
    {{
      "type": "BUY|SELL|HOLD|ADD|TRIM",
      "symbol": "TICKER",
      "conviction": "high|medium|low|speculative",
      "time_horizon": "quick|medium|long|satellite",
      "bucket": "core|momentum|satellite|hedge",
      "price": <float>,
      "reason": "specific reason with data"
    }}
  ],
  "watchlist": ["SYM1", "SYM2", "SYM3"],
  "macro_themes_active": ["theme1", "theme2"],
  "risk_note": "key risk observation",
  "diary_note": "plain English summary of this cycle",
  "bunker_mode_recommend": false,
  "hedge_recommend": false
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
            "max_tokens": 3000,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=60
    )
    response.raise_for_status()
    text = response.json()["content"][0]["text"]
    return json.loads(text.replace("```json","").replace("```","").strip())

# ═══════════════════════════════════════════════════════════════
# EMAIL (kept from v4.1 — SendGrid upgrade coming separately)
# ═══════════════════════════════════════════════════════════════

def send_email(subject, html_body):
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD]):
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(EMAIL_FROM, EMAIL_PASSWORD)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info(f"📧 Email sent: {subject}")
    except Exception as e:
        log.warning(f"Email failed: {e}")

# ═══════════════════════════════════════════════════════════════
# MAIN CYCLE v4.2
# ═══════════════════════════════════════════════════════════════

def run_cycle(cycle_num):
    global daily_summary, geopolitical_score, regime_state, last_vix

    log.info(f"═══ CYCLE {cycle_num} — {datetime.now(timezone.utc).strftime('%H:%M UTC')} ═══")
    daily_summary["cycles"] += 1

    market_open, next_open, next_close = is_market_open()
    time_ctx = get_time_context()

    if not market_open:
        log.info(f"Market closed. Next open: {next_open}")
        return

    account         = get_account()
    positions       = get_positions()
    portfolio_value = float(account.get("portfolio_value", 100000))
    cash            = float(account.get("cash", 0))

    if daily_summary["start_value"] is None:
        daily_summary["start_value"] = portfolio_value

    log.info(f"💼 ${portfolio_value:.2f} | Cash: ${cash:.2f} | Positions: {len(positions)} | Time: {time_ctx['time_of_day']}")

    # Safety
    if daily_summary.get("trading_halted"):
        log.warning("⛔ Trading halted")
        return
    if check_daily_loss(portfolio_value):
        return

    # Stops + profit ladder
    stops, profit_sales = update_trailing_stops_and_profits(positions)
    for stop in stops:
        result = place_order(stop["symbol"], stop["qty"], "sell")
        if result:
            daily_summary["trades"].append(f"STOP {stop['qty']}x {stop['symbol']} — {stop['reason']}")
            trailing_stops.pop(stop["symbol"], None)
    for sale in profit_sales:
        result = place_order(sale["symbol"], sale["qty"], "sell")
        if result:
            daily_summary["trades"].append(f"LADDER T{sale['tier']} {sale['qty']}x {sale['symbol']}")
            daily_summary["wins"] += 1

    if stops or profit_sales:
        positions = get_positions()

    # Intelligence gathering
    log.info("🔍 Gathering elite intelligence v4.2...")
    fear_greed  = get_fear_greed()
    benchmark   = get_benchmark()
    vix         = get_vix_approx()
    last_vix    = vix
    held_syms   = [p["symbol"] for p in positions]
    technicals  = get_technicals(held_syms) if held_syms else {}
    risk_warns  = check_risk(account, positions)
    sector_warns = check_sector_limits(positions, portfolio_value)
    black_swan  = check_black_swan(positions, portfolio_value)
    memory      = get_memory_summary()
    learning    = get_learning_summary()
    win_loss    = get_win_loss()
    gap_info    = check_premarket_gap() if time_ctx.get("opening_hour") else None
    sector_exposure = get_sector_exposure(positions, portfolio_value)

    # Regime detection
    regime, regime_note = detect_regime(benchmark, fear_greed, vix)
    regime_state = regime
    regime_info = f"{regime}: {regime_note}"

    log.info(f"😨 F&G: {fear_greed['value']} | VIX: {vix} | Regime: {regime}")
    if benchmark:
        log.info(f"📈 SPY: ${benchmark['spy_current']:.2f} ({'+' if benchmark['spy_change_pct']>=0 else ''}{benchmark['spy_change_pct']:.2f}%)")

    # Hedge check
    should_hedge = should_add_hedge(portfolio_value, benchmark, vix, fear_greed)
    if should_hedge[0]:
        log.warning(f"🛡️ HEDGE SIGNAL: {should_hedge[1]}")

    log.info("📰 Fetching comprehensive intelligence (Polymarket, FinTwit, 13F, SEC, ZeroHedge)...")
    intel = get_full_intelligence(positions, regime_info)
    geopolitical_score = intel.get("geopolitical_risk_score", geopolitical_score)
    log.info(f"🌍 Geo: {geopolitical_score}/10 | Mood: {intel.get('market_mood')}")
    if intel.get("regime_shift_detected"):
        log.warning(f"🔄 REGIME SHIFT: {intel.get('regime_shift_note')}")
    if intel.get("zerohedge_macro"):
        log.info(f"🔴 ZH: {intel['zerohedge_macro'][:80]}...")
    if intel.get("polymarket_probabilities"):
        log.info(f"🎲 Polymarket: {intel['polymarket_probabilities']}")
    if intel.get("fintwit_signals"):
        log.info(f"🐦 FinTwit: {intel['fintwit_signals'][:2]}")

    # Bunker mode
    bunker_mode = (
        intel.get("bunker_mode_trigger", False) or
        geopolitical_score >= 9 or
        (portfolio_value < 95000 and fear_greed["value"] < 20)
    )
    if bunker_mode:
        log.warning("🏰 BUNKER MODE")

    # Claude decision
    log.info("🧠 Consulting Claude v4.2 with full intelligence...")
    try:
        decision = ask_claude(
            account, positions, intel, technicals, fear_greed, vix,
            memory, risk_warns, sector_warns, black_swan, benchmark,
            win_loss, time_ctx, bunker_mode, regime_info, learning,
            gap_info, sector_exposure, should_hedge
        )
    except Exception as e:
        log.error(f"Claude decision failed: {e}")
        return

    log.info(f"📊 {decision.get('outlook','?').upper()} | {decision.get('confidence','?')} | Regime: {decision.get('regime_assessment','?')}")
    log.info(f"💭 {decision.get('reasoning','')[:200]}")
    if decision.get("learning_applied"):
        log.info(f"🎓 LEARNING: {decision['learning_applied']}")
    if decision.get("macro_themes_active"):
        log.info(f"🎯 Themes: {', '.join(decision['macro_themes_active'])}")
    if decision.get("risk_note"):
        log.info(f"⚠️ {decision['risk_note']}")
    if decision.get("watchlist"):
        log.info(f"👀 {', '.join(decision['watchlist'])}")
    if decision.get("diary_note"):
        trade_diary.append({
            "time": datetime.now(timezone.utc).strftime("%H:%M UTC"),
            "diary_note": decision["diary_note"]
        })

    # Log decision for learning
    log_decision(cycle_num, decision, {
        "portfolio_value": portfolio_value,
        "regime": regime,
        "vix": vix,
        "fear_greed": fear_greed["value"],
        "geo_risk": geopolitical_score
    })

    # Execute
    position_map = {p["symbol"]: p for p in positions}
    cycle_actions = []

    for action in decision.get("actions", []):
        atype      = action.get("type","").upper()
        symbol     = action.get("symbol","")
        conviction = action.get("conviction","medium")
        price      = float(action.get("price", 0))
        reason     = action.get("reason","")
        horizon    = action.get("time_horizon","medium")
        bucket     = action.get("bucket","core")

        if not symbol or price <= 0:
            continue

        # Defensive: ensure numeric values are valid
        try:
            price = float(price) if price is not None else 0
            if price <= 0:
                log.warning(f"⛔ SKIP {symbol} — invalid price")
                continue
        except (ValueError, TypeError):
            log.warning(f"⛔ SKIP {symbol} — price conversion error")
            continue

        # Opening hour restraint
        if time_ctx.get("opening_hour") and conviction != "high" and atype in ("BUY","ADD"):
            log.info(f"⏸️ OPENING HOUR SKIP: {atype} {symbol} — only high conviction allowed first 30 min")
            continue

        if atype in ("BUY", "ADD"):
            if not is_liquid_enough(symbol):
                log.warning(f"⛔ SKIP {symbol} — low liquidity")
                continue
            is_satellite = (bucket == "satellite") or (symbol in MACRO_THEMES.get("satellite_moonshots", []))
            shares = get_position_size(conviction, cash, price, geopolitical_score, vix, is_satellite)
            if shares <= 0:
                log.warning(f"⛔ SKIP {atype} {symbol} — insufficient cash")
                continue
            cost = shares * price
            tag = f" [{bucket.upper()}]"
            log.info(f"🟢 {atype} {shares}x {symbol} @ ~${price:.2f} [{conviction}, {horizon}]{tag} | {reason[:80]}")
            result = place_order(symbol, shares, "buy")
            if result:
                cash -= cost
                trailing_stops[symbol] = price
                daily_summary["trades"].append(f"{atype} {shares}x {symbol} @ ${price:.2f}{tag}")
                cycle_actions.append(action)

        elif atype in ("SELL", "TRIM"):
            if symbol not in position_map:
                log.warning(f"⛔ SKIP {atype} {symbol} — no position")
                continue
            held_qty = int(float(position_map[symbol]["qty"]))
            sell_qty = held_qty if atype == "SELL" else max(1, held_qty // 3)  # TRIM = 1/3
            log.info(f"🔴 {atype} {sell_qty}x {symbol} @ ~${price:.2f} | {reason[:80]}")
            result = place_order(symbol, sell_qty, "sell")
            if result:
                entry = float(position_map[symbol]["avg_entry_price"])
                attribute_outcome(symbol, entry, price, reason)
                if atype == "SELL":
                    trailing_stops.pop(symbol, None)
                daily_summary["trades"].append(f"{atype} {sell_qty}x {symbol} @ ${price:.2f}")
                cycle_actions.append(action)

        elif atype == "HOLD":
            log.info(f"⏸️ HOLD {symbol} [{horizon}] — {reason[:80]}")
            cycle_actions.append(action)

    pnl = portfolio_value - 100000.0
    performance_memory.append({
        "cycle": cycle_num,
        "time": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "portfolio_value": round(portfolio_value, 2),
        "pnl": round(pnl, 2),
        "regime": regime,
        "vix": vix,
        "actions": cycle_actions,
        "profit_tiers_hit": {}
    })
    if len(performance_memory) > 100:
        performance_memory.pop(0)

    log.info(f"✅ CYCLE {cycle_num} | ${portfolio_value:.2f} | P&L: {'+'if pnl>=0 else ''}${pnl:.2f}\n")

# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    log.info("╔══════════════════════════════════════════════════════╗")
    log.info("║       RIDGELINE AI TRADING AGENT v4.2                ║")
    log.info("║       Elite Intelligence Edition                     ║")
    log.info("╠══════════════════════════════════════════════════════╣")
    log.info(f"║  Cycles: every {CYCLE_MINUTES} minutes                              ║")
    log.info("║                                                      ║")
    log.info("║  NEW IN v4.2:                                        ║")
    log.info("║  🎲 Polymarket prediction probabilities              ║")
    log.info("║  🐦 X/FinTwit breaking signals                       ║")
    log.info("║  🏦 13F institutional moves                          ║")
    log.info("║  🏛️ Congressional trades (Pelosi tracker)            ║")
    log.info("║  📄 SEC EDGAR real-time filings                      ║")
    log.info("║  💡 Patent filing signals                            ║")
    log.info("║  💼 Job posting expansion signals                    ║")
    log.info("║  💰 Multi-tier profit ladder (15/25/25)              ║")
    log.info("║  🛡️ Dynamic inverse ETF hedging                      ║")
    log.info("║  📉 VIX-scaled position sizing                       ║")
    log.info("║  🎯 Sector-wide stop loss logic                      ║")
    log.info("║  🌅 Pre-market gap awareness                         ║")
    log.info("║  🔄 Regime change detection                          ║")
    log.info("║  🚀 Momentum accelerator bucket (5%)                 ║")
    log.info("║  🎓 Performance attribution & learning                ║")
    log.info("║  📊 Quantified pattern recognition                    ║")
    log.info("║  ⏰ Opening/closing hour awareness                    ║")
    log.info("║  📅 Friday afternoon defensive shift                  ║")
    log.info("║  🎭 Buy-the-dip / Sell-the-rip logic                 ║")
    log.info("║  ⚡ Anti-herd contrarian triggers                     ║")
    log.info("║  📈 Earnings behavior discipline                      ║")
    log.info("║                                                      ║")
    log.info("║  CARRIED OVER FROM v4.1:                             ║")
    log.info("║  ✅ ZeroHedge + tiered news weighting                 ║")
    log.info("║  ✅ Macro theme watchlists                            ║")
    log.info("║  ✅ Conviction-based sizing                           ║")
    log.info("║  ✅ 7% trailing stops                                 ║")
    log.info("║  ✅ Bunker mode                                       ║")
    log.info("║  ✅ 12% satellite allocation                          ║")
    log.info("╚══════════════════════════════════════════════════════╝\n")

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
