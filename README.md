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


Both are **required**. If `ALLOW_LIVE_TRADING=true` is set without a
confirmation code, the app refuses to boot. This is a hard gate —
there is no way to accidentally enable live trading without a
confirmation code.

### 2. User creates a live OKX API key

In OKX, create a **trade-only** API key. Requirements:

- **Trade** permissions (derivatives is what the bot uses)
- **Read** permissions (for account/positions info)
- **No withdrawal permissions** — the bot never needs them, and
  trade-only keys are a critical safety layer
- **No transfer permissions**
- **IP whitelist** (optional but recommended) — restrict to your server's IP

### 3. User connects through the Mini App

Account tab → **Live** toggle → paste credentials → type the confirmation
code → tick the acceptance checkbox → Connect.

The backend validates the credentials against OKX's `/account/config`
endpoint **using the live header** (not demo). If OKX rejects the keys,
the connection is refused. Only after OKX confirms the credentials does
the bot store them (encrypted at rest).

### 4. User starts a live session

Automate tab → pick strategy, duration, target, risk → **Start session**.

Before the session starts, the user sees a final confirmation dialog
that specifically warns about real-money trading. Only after tapping
through does the session begin.

### 5. Session runs

The worker trades the user's live account the same way it trades demo.
Log lines are tagged `[LIVE]` so you can distinguish them from demo at
a glance.

### 6. User stops the session

Click **Stop session** in the Mini App. The worker closes all open
positions at market, reconciles P&L, and ends the session.

---

