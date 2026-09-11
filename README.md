# Safe Trading Bot Foundation

This is a paper-first Telegram/Bybit bot foundation. It deliberately does **not** promise returns, invent signals, collect exchange secrets in Telegram, or enable live trading by default.

## Before starting

1. Revoke the Telegram token exposed in the old script with `@BotFather`, then create a replacement.
2. Create and activate a virtual environment.
3. Install `pip install -r requirements.txt`.
4. Copy `.env.example` to `.env`, fill in only the values you need, and keep `.env` private.
5. Run `python -m app.main`.

## Multi-user Telegram setup

The bot is now a Telegram front door with a Mini App dashboard. Users use `/start`, then **Open trading dashboard**. The Mini App authenticates a user using Telegram-signed init data; the server validates that signature before accessing their account.

Run the API separately behind HTTPS (for example, a reverse proxy with a fixed outbound IP):

```sh
uvicorn app.api:api --host 127.0.0.1 --port 8000
```

## Render deployment

`render.yaml` and `start.sh` deploy the Telegram poller and Mini App API together as one Render web service. User records, encrypted Bybit connections, settings, and trade history live in Supabase Postgres, so Render redeploys do not erase them.

1. Put this project in a **private GitHub repository**. Never commit `.env` or `trading_bot.db`.
2. In Render, choose **New → Blueprint**, select that repository, and deploy `render.yaml`.
3. In Supabase, open **Connect**, select **Session pooler**, and copy its connection string. In Render's Environment page, add it as the secret `DATABASE_URL`. Keep the password portion private. The app creates its tables automatically at startup—do not use the Supabase SQL editor for this project.
4. Enter the remaining secret values prompted by Render. Generate a new `CREDENTIAL_ENCRYPTION_KEY` for this deployment and keep it unchanged after launch; changing it would prevent decryption of previously saved user connections.
5. After Render provides `https://<service>.onrender.com`, set `MINI_APP_URL` to that exact address in the Render service environment variables, then redeploy.
6. In BotFather, configure that same HTTPS address as the Mini App URL. Stop the local polling bot before the Render service begins polling to prevent duplicate polling conflicts.

Before first deploy, rotate any bot token which appeared in a terminal or Render log. Only one copy of the bot may run `getUpdates`: stop your local bot and any duplicate Render service before testing the deployed one.

If you need a different permitted hosting region, Render requires a new service; it cannot move an existing one. Set `RUN_TELEGRAM_POLLER=false` on the new service while you verify its dashboard, then suspend the old service and set the new service to `true`. Reuse the same `DATABASE_URL` and `CREDENTIAL_ENCRYPTION_KEY` so existing encrypted user connections continue to work.

Set `MINI_APP_URL` to its public HTTPS URL and configure the same URL as the bot's Mini App in BotFather. The current connection flow permits **OKX Demo Trading only**—never mainnet. It validates the key, secret, and API passphrase before encrypting them in the database.

## Operation

`/status` shows mode and limits. In paper mode, `/trade_buy BTCUSDT 10` and `/trade_sell BTCUSDT 10` make simulated orders. All order creation is recorded using an idempotency key.

The original single-user live path is not a public multi-user feature and must remain disabled. We will add a per-user execution worker only after testnet onboarding, paper automation, cancellation, and reconciliation tests are complete.

## Demo automation

The dashboard provides one deliberately constrained experimental strategy, but it is paused for OKX until its futures execution adapter has completed validation. **It is not backtested evidence, investment advice, or a promise of any outcome.** `STOP ALL AUTOMATION` immediately disables a user's profile; it cannot cancel an order that an exchange already accepted.

The bot sends the user alerts when they start or pause automation, use the kill switch, submit a demo order, or a worker scan fails. Alerts are informational; the dashboard and Bybit remain the source of truth for order status.

No command accepts an API key or API secret. The bot only reads them from the server environment. The optional encrypted-credential store exists for a future secure web onboarding flow; do not put credentials in Telegram messages.

## What remains before production

Add a tested strategy with a documented backtest, exchange websocket order reconciliation, monitoring/alerting, key rotation, database backups, rate-limit retry policy, and independent security review. Paper-trade the exact strategy for a meaningful period first.
