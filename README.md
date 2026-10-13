# Relay Trade

Automated crypto perpetuals trading bot. Connects to OKX (demo or live),
runs one or two strategies on 9 markets, manages every position with
automatic stop-loss, breakeven, and trailing logic.

Users interact through a Telegram Mini App (WebApp). The server exposes
a FastAPI backend and runs the auto-trading worker in the background.

---

## What the bot does

Given a user-configured session (target profit, max loss, risk per trade,
duration, markets), the worker:

1. Scans all selected markets every 60 seconds.
2. Runs one or two strategies on each market, in priority order.
3. Places a market order when a strategy produces a valid entry signal.
4. Attaches a stop-loss and take-profit to every entry via OKX's algo orders.
5. Manages the position on each scan: moves the SL to breakeven after
   +1.5R, trails the SL after +2R, tightens it after +3R.
6. Records closed positions, reconciles P&L with OKX, and updates
   session statistics.
7. Ends the session when target profit, max loss, safety floor, or
   duration is reached.

Every session uses isolated margin and never widens a stop-loss.

---

## Recommended onboarding

**Run demo first.** Every user gets the same code path regardless of
environment; only the OKX endpoint differs. Two weeks of demo is enough
to observe the bot's behavior in real market conditions before risking
real funds.

Recommended demo setup:

- Environment: **Demo**
- Strategy: **Daily breakout** only
- Duration: **7 days**
- Target: **+$50** · Max loss: **-$20**
- Risk per trade: **Auto** (bot reads your balance and picks)
- Leverage ceiling: **20×**
- Markets: **Auto-scan** (all 9 default markets)

After 2 weeks, review session history. If net positive and the trade
behavior matches expectations, consider going live with a small balance
($100–$200).

---

## Going live — full workflow

### 1. Operator enables live trading

Two environment variables on the server must be set:
