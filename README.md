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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE).

Any modification distributed or used over a network must be released under the same license.
