# RIDGELINE — AI Trading Agent

An autonomous paper trading agent powered by Claude AI and Alpaca Markets.
Runs every 30 minutes, analyzes your portfolio, and executes trades automatically.

---

## Deployment to Railway (Step by Step)

### Step 1: Push to GitHub
1. Create a new repository on GitHub called `ridgeline`
2. Upload these 4 files to it:
   - ridgeline.py
   - requirements.txt
   - Procfile
   - README.md

### Step 2: Create Railway account
1. Go to https://railway.app
2. Sign up with your GitHub account
3. Click "New Project"
4. Select "Deploy from GitHub repo"
5. Choose your `ridgeline` repository

### Step 3: Set Environment Variables
In Railway, go to your project → Variables tab and add:

| Variable | Value |
|----------|-------|
| CLAUDE_API_KEY | sk-ant-... |
| ALPACA_API_KEY | PK... |
| ALPACA_SECRET | your secret key |
| CYCLE_MINUTES | 30 |

### Step 4: Deploy
Railway will automatically build and deploy Ridgeline.
It will start running immediately and cycle every 30 minutes.

### Step 5: Monitor
- Click "Logs" in Railway to watch Ridgeline make decisions in real time
- Log into Alpaca paper trading to see your positions and P&L

---

## How It Works

1. Every 30 minutes, Ridgeline wakes up
2. Checks if the stock market is open (skips weekends/after hours)
3. Pulls your current portfolio from Alpaca
4. Sends everything to Claude AI for analysis
5. Claude decides what to buy, sell, or hold
6. Ridgeline executes the trades via Alpaca
7. Goes back to sleep for 30 minutes

---

## Cost
- Railway: ~$5/month
- Claude API: ~$1-3/month at this usage level
- Alpaca paper trading: Free

---

## Safety
- Paper trading only — no real money
- $1,000 cash reserve always maintained
- Max 35% of cash per single trade
- Only trades during market hours

---

*Not financial advice. For educational and experimental purposes only.*
