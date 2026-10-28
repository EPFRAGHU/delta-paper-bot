# Delta Exchange India bot (ETHUSD perpetual, 4h trend breakout)

No strategy guarantees profit. Backtests are not future results. Only trade money you can afford to lose.

## Strategy
- Market: ETHUSD perpetual (contract = 0.01 ETH, about $23). BTC (0.001 BTC, about $80) and SOL (1 SOL) contracts are too coarse for a ~$55 account.
- Timeframe: 4h candles, decided only on closed candles.
- Long: close breaks the 40-bar high, price above a rising 200 EMA, ADX >= 20. Short is the mirror.
- Stop: 2.5 x ATR from entry, placed on the exchange as a reduce-only stop order (triggered on mark price).
- Exit: trailing stop = tighter of the 20-bar low/high and 3 x ATR from the best price. It only ever moves in your favour.

## Risk rules
- 1.5% of equity risked per trade. If one contract would risk more than 3%, the trade is skipped.
- Notional is capped at 10x equity, and exchange leverage is set to 10x. Real usage is usually below 1x.
- Daily loss limit of 5%: no new entries for the rest of the UTC day.
- Max drawdown of 25% from peak: the bot halts and waits for you.
- One position at a time. Create a file named `STOP` in this folder to block new entries.

## Backtest (real Delta India candles, fees + 18% GST + slippage, $55 start)
| Period | ETH bot | ETH buy & hold |
|---|---|---|
| In-sample: Feb 2024 to ~Sep 2025 (params chosen here) | +46.6%, PF 2.91, max DD 6.5% | +78.9% |
| Out-of-sample: last 40%, to Oct 2026 | +9.7%, PF 1.56, max DD 8.5%, 23 trades | -44.8% |
| Full | +67.6%, 62 trades, win rate 47%, max DD 6.5% | -1.3% |

Re-run with: `python backtest.py --symbols ETHUSD,BTCUSD --resolution 4h --days 1460`
(This overwrites `params.json`. Add `--no-search` to only evaluate the current params.)

## Extra data tested and rejected (Oct 2026)
Every filter was tested as an entry veto on the same 4h ETH backtest. It had to help out-of-sample, not only in-sample. Scripts: `funding_test.py`, `signals_test.py`.

| Filter (free source) | In-sample | Out-of-sample | Verdict |
|---|---|---|---|
| none (baseline) | +46.6% | +9.7% | kept |
| Funding rate crowding (Binance) | about the same | +11–13% | changes only 3–6 of 62 trades, too few to trust |
| Breakout volume > 1.3x avg (Binance) | +16% | +10% | worse |
| Taker buy/sell order flow (Binance) | +6% | +17% | unstable, flips between periods |
| BTC trend must agree (Binance) | +46% | +9% | no gain |
| Fear & Greed extremes (alternative.me) | +46% | -0.4% to -4% | worse |

Other GitHub strategies, same ETH data, fees and sizing (`strategy_compare.py`, 1h bars, one trade per UTC day):

| Strategy | In-sample | Out-of-sample |
|---|---|---|
| Larry Williams volatility breakout, k=0.4 to 0.8 | -66% to -9% | -40% to +7% |
| Dual Thrust (quant-trading repo), k=0.3 to 0.7 | -51% to +9% | -12% to +4% |

### Timeframe x strategy study (`research/multi_tf.py`, Oct 2026)
This study used 2 years of Delta 1m ETH candles resampled to 17 timeframes (1m to 4h), plus Binance taker flow for order-flow delta. It ran 7 textbook strategies with no per-timeframe tuning, and identical fees, GST, slippage, 1.5% risk and 10x cap. "Early" is the first 60% of the period and "late" the last 40%.
- **Every strategy blows up the account (-91%) on 1m, 2m, 3m and 5m.** Fees and noise win at that speed. Nearly everything below 30m loses.
- Profitable in both periods, most robust first:
  - Donchian breakout (this bot) on 4h: +37% early, +11% late, max DD 8%.
  - EMA 9/21 + EMA200 on 90m: +15% early, +49% late, DD 19%.
  - EMA 9/21 on 4h: +21% / +12%.
  - Donchian on 90m and 1h: about +7–11% in each period.
- RSI and Bollinger mean reversion lose on almost every timeframe. VWAP cross loses below 3h. Order-flow delta is weakly positive only between 45m and 3h, then fades in the late period.
- Volume profile was not tested: it needs tick or footprint history, which isn't freely available.

Frameworks: freqtrade and jesse don't support Delta Exchange. CCXT's `delta` class points at Delta Global (`api.delta.exchange`), which Delta says India accounts can't use, so this bot talks to the India API directly.

Paid APIs (CoinGlass, CoinMarketCap, CoinAPI, from $29/mo) mostly sell the same funding, OI and price data. That costs about half this account per month for no tested edge.

## Run it
1. Paper (no keys, live prices, simulated fills): `python bot.py`
2. Testnet (fake money, real order flow): make a Delta India demo account, create testnet API keys, put them in `.env` (copy `.env.example`), then run `python bot.py --mode testnet`
3. Live: create keys at delta.exchange > API Management with **Read + Trading only, no withdrawal**, and whitelist your IP. Put them in `.env`, set `LIVE_CONFIRM=YES`, then run `python bot.py --mode live`

The bot must stay running (PC on, internet up). If it's off, the exchange stop still protects an open position, but the stop won't trail. On restart it re-syncs with the exchange.
Logs: `bot_<mode>.log`. Trades: `trades.csv`.

## Cloud paper trading (GitHub Actions)
`.github/workflows/paper-bot.yml` runs `python bot.py --mode paper --once` about every 15 minutes, free, with no keys. Each run:
- replays 5-minute candles since the last run, so a stop touched in between is still caught
- decides on any newly closed 4h candle
- commits `state_paper.json`, `trades.csv` and `signals.csv`
- refreshes the dashboard data in `docs/data/`

Online dashboard: https://delta-paper-bot.vercel.app. It's hosted on Vercel and reads `docs/data/*.json` straight from GitHub, so it never needs redeploying. Data can lag by up to about 5 minutes because of GitHub's cache.

Limits: GitHub can delay scheduled runs by 5–20 minutes, so paper entries can be a little later than the candle close. Only paper mode runs this way. Live trading needs an always-on machine with a whitelisted static IP.

To pause the cloud bot, open the repo's Actions tab, choose paper-bot, then "Disable workflow".

## Telegram alerts
The bots message you on every entry, stop move, exit and drawdown halt. Each message is tagged with the mode and strategy. They stay silent unless both repository secrets are set: `TELEGRAM_BOT_TOKEN` (from @BotFather) and `TELEGRAM_CHAT_ID` (your chat id). To test, open Actions, choose paper-bot, click "Run workflow" and tick "Send a Telegram test message". For local or VPS runs, put the same two values in `.env`.

## Dashboard
Run `python dashboard.py` in a second terminal, then open http://127.0.0.1:8765

Tabs: Paper / Testnet / Live / Backtest. It shows equity, realized and unrealized P&L, win rate, profit factor, drawdown, the open position with its trailing stop, a candle chart with entry and exit markers, the equity curve, trade history and the 4h signal log. It refreshes every 10s, only reads files, and never places orders.

Tests: `python test_bot.py`
