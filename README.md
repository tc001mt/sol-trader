# SOL Trader — AI-Powered Solana Trading Bot

An autonomous trading bot for Solana-based tokens, powered by Claude AI and deployed on-chain via Jupiter DEX.

## What it does

- **AI decisions**: Claude (via OpenRouter) analyzes market data every 20 minutes and decides to buy, sell, or wait
- **Safety layer**: A faster Gemini model runs a parallel safety check each cycle — classifies market as GREEN / YELLOW / RED
- **Multi-token**: Trades SOL, BTC (wrapped), ETH (wrapped), JUP, BONK, WIF, PENGU on Solana via Jupiter
- **Flash crash protection**: Distinguishes real crashes (1h + 24h both negative) from short spikes before selling
- **Futures signals**: Reads Binance funding rate, open interest, and long/short ratio to detect overleveraged markets
- **Technical indicators**: RSI 14, MACD, Bollinger Bands % from CoinGecko data
- **Dashboard**: Real-time web UI showing prices, wallet, trades, indicators, and fear/greed index
- **Learning rules**: Claude proposes weekly rule updates based on trade history — requires human approval before activation

## Shitcoin Hunter (separate, experimental bot)

`shitcoin_hunter.py` is a second, fully independent bot included in this repo:
a 5-minute momentum scanner that dynamically discovers the top 100 Solana
tokens by volume, looks for volume spikes plus positive short-term momentum,
and buys whatever a Claude/Gemini check flags as the strongest candidate —
with automated take-profit/stop-loss exits.

It has its own wallet, its own `.env.hunter` config, and its own systemd
service — it shares only the Jupiter swap pattern with the main bot. It is
the least mature part of this repo. **Read the Disclaimer below before even
running it in `DRY_RUN`, let alone with real funds.**

## Architecture

```
scheduler.py       ← APScheduler: runs trading cycle every 20 min
  └─ data_collector.py  ← Fetches prices, indicators, news, futures
  └─ claude_brain.py    ← Builds AI prompt, calls OpenRouter, parses decision
  └─ trader.py          ← Executes swaps on Jupiter DEX
  └─ database.py        ← SQLite/PostgreSQL trade history

app.py             ← Flask dashboard (web UI)
templates/
  index.html       ← Real-time dashboard
```

## Requirements

- Python 3.11+
- A Solana wallet with private key (base58 or JSON array)
- OpenRouter API key (for Claude + Gemini access)
- CoinGecko API key (optional, increases rate limits)
- Helius or other Solana RPC endpoint (optional, improves reliability)

## Setup

```bash
git clone https://github.com/tc001mt/sol-trader.git
cd sol-trader
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your keys
python database.py   # Initialize DB
python scheduler.py  # Start trading
python app.py        # Start dashboard (port 5000)
```

## Environment variables

```
SOLANA_PRIVATE_KEY=      # Base58 or JSON array private key
SOLANA_RPC=              # RPC endpoint (default: mainnet-beta)
OPENROUTER_API_KEY=      # Required for AI decisions
OPENROUTER_MODEL_MAIN=   # Default: anthropic/claude-sonnet-4-5
OPENROUTER_MODEL_FAST=   # Default: google/gemini-2.5-flash-lite
COINGECKO_API_KEY=       # Optional
NEWSAPI_KEY=             # Optional, for additional news sources
TELEGRAM_BOT_TOKEN=      # Optional, for trade notifications
TELEGRAM_CHAT_ID=        # Optional
DRY_RUN=true             # Set to false to enable real trades
```

## Safety features

- `DRY_RUN=true` by default — no real trades until explicitly disabled
- Minimum SOL reserve (0.009 SOL) always maintained for transaction fees
- MACD bearish block: prevents buys when momentum is negative (unless confidence > 85%)
- Flash crash guard: holds position during short spikes, only sells on sustained crashes
- Max consecutive buy limit: prevents runaway accumulation without any sells
- Manual USDC refuge button in dashboard

## Dashboard

Run `python app.py` and open `http://localhost:5000`. Shows:
- Live prices (SOL, BTC, ETH) with 1h/24h change
- Fear & Greed index
- Wallet breakdown with USD values
- Market alert level (GREEN / YELLOW / RED) with AI reasoning
- Technical indicators per token (RSI, MACD, BB%, Funding rate, Long/Short ratio)
- Recent trade history with P&L
- 30-day performance stats
- Pending AI rule proposals (approve/reject)

## Running as a persistent service (Linux / systemd)

To keep the bot and dashboard running 24/7, create two systemd service files.

**`/etc/systemd/system/sol-trader-scheduler.service`**
```ini
[Unit]
Description=SOL Trader — Trading Scheduler
After=network.target

[Service]
User=YOUR_USER
WorkingDirectory=/path/to/sol-trader
EnvironmentFile=/path/to/sol-trader/.env
ExecStart=/path/to/sol-trader/venv/bin/python scheduler.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/sol-trader-dashboard.service`**
```ini
[Unit]
Description=SOL Trader — Web Dashboard
After=network.target

[Service]
User=YOUR_USER
WorkingDirectory=/path/to/sol-trader
EnvironmentFile=/path/to/sol-trader/.env
ExecStart=/path/to/sol-trader/venv/bin/python app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start both:
```bash
sudo systemctl daemon-reload
sudo systemctl enable sol-trader-scheduler sol-trader-dashboard
sudo systemctl start sol-trader-scheduler sol-trader-dashboard

# Check status
sudo systemctl status sol-trader-scheduler
sudo systemctl status sol-trader-dashboard

# View live logs
journalctl -u sol-trader-scheduler -f
```

The services will restart automatically on crash and start on server reboot.

## Keeping up to date

This project is actively developed. Fixes, improvements, and new features are pushed frequently — sometimes multiple times per day — as we observe the bot's behavior in live trading conditions.

**Manual update:**
```bash
cd sol-trader
git pull
sudo systemctl restart sol-trader-scheduler sol-trader-dashboard
```

**Automatic update (optional):**

Add a cron job to pull and restart every hour:
```bash
crontab -e
```
```
0 * * * * cd /path/to/sol-trader && git pull --ff-only && sudo systemctl restart sol-trader-scheduler sol-trader-dashboard
```

> Before updating in production, check the [commit history](https://github.com/tc001mt/sol-trader/commits/main) for any breaking changes or new required environment variables.

---

## Disclaimer

**Use at your own risk. We accept no responsibility whatsoever — for either bot in this repo.**

This project — the main SOL Trader bot **and** Shitcoin Hunter — is published purely
so other people can read the code, run it, comment on it, and suggest improvements.
It is **not published as a recommendation to use it**, and especially not with real money.

- **No financial advice.** Nothing in this project constitutes financial, investment, or trading advice.
- **Never "done," and possibly never will be.** Both bots are an ongoing experiment, not a finished or validated product. Nobody can predict what the market will do next, no matter how many rules or AI models sit on top of the decision. Treat everything here as a work in progress, indefinitely.
- **Test in `DRY_RUN=true`, and strongly consider staying there.** This isn't a formality — it's the intended way to run this project. Live trading is offered as an option for people who fully understand and accept the risk, not as the goal.
- **Risk of total loss.** If you set `DRY_RUN=false`, you may lose part or all of the funds in the connected wallet(s) — through bad trades, bugs, API failures, or on-chain errors. Never use money you cannot afford to lose.
- **No liability, no exceptions.** The authors and contributors accept no responsibility for any financial losses, missed trades, technical malfunctions, API outages, on-chain errors, or any other damage arising from running this software, in `DRY_RUN` or live, on either bot.
- **No uptime guarantee.** Either bot may stop working at any time due to changes in external APIs (Jupiter, CoinGecko, OpenRouter, Binance), network issues, or software bugs.

If you choose to run either bot with real funds, that is entirely your own decision — you are solely responsible for it and for any funds you put at risk.

---

## Support

If this project is useful to you, consider donating — it helps cover server and API costs.

**Solana:** `5qAqB9T1e51fRNG3MY5tYqAZXFVriTiKDQTBkGvx81dL`

## Referral Program

SOL Trader uses the [Jupiter Referral Program](https://referral.jup.ag) to support ongoing development.

By default, a small fee (0.5%) is applied to every swap and sent to the project's referral account. This does not affect your trading results — Jupiter absorbs the cost from routing fees.

**To use your own referral account instead:**
1. Visit [referral.jup.ag](https://referral.jup.ag) and connect your wallet
2. Create a referral account (one-time on-chain transaction)
3. Copy the referral account address
4. Add it to your `.env`:
   ```
   JUPITER_REFERRAL_ACCOUNT=<your_referral_account_address>
   JUPITER_REFERRAL_FEE_BPS=50
   ```

**To disable referral entirely:** set `JUPITER_REFERRAL_ACCOUNT=` (empty).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE).

Any modification distributed or used over a network must be released under the same license.
