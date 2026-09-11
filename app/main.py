from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import Settings
from .database import Database
from .exchange import BybitClient, ExchangeError, PaperBroker
from .models import OrderIntent, Side
from .risk import RiskManager, RiskRejected
from .security import secure_equals
from .service import TradingService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def build_app(settings: Settings) -> Application:
    database = Database(settings.database_path)
    database.initialize()
    market = BybitClient(settings.bybit_api_key or "", settings.bybit_api_secret or "", settings.bybit_testnet)
    broker = PaperBroker(market) if settings.trading_mode == "paper" else market
    service = TradingService(database, RiskManager(settings, database), broker)
    app = Application.builder().token(settings.telegram_token).build()
    app.bot_data.update(settings=settings, database=database, service=service, market=market, live_armed=False)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("futures", futures))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("trade_buy", buy))
    app.add_handler(CommandHandler("trade_sell", sell))
    app.add_handler(CommandHandler("live_confirm", live_confirm))
    app.add_error_handler(error_handler)
    return app


def admin(update: Update, settings: Settings) -> bool:
    return bool(update.effective_user and update.effective_user.id == settings.admin_user_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    context.bot_data["database"].upsert_user(user.id, user.username, user.first_name)
    settings: Settings = context.bot_data["settings"]
    text = "Welcome. Connect Bybit and manage your paper-trading settings through the secure dashboard. Never send API keys in chat."
    if settings.mini_app_url:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Open trading dashboard", web_app=WebAppInfo(settings.mini_app_url))]])
        await update.effective_message.reply_text(text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text + " The dashboard URL has not been configured yet.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s: Settings = context.bot_data["settings"]
    armed = context.bot_data["live_armed"]
    await update.effective_message.reply_text(f"Mode: {s.trading_mode.upper()}\nLive armed: {'yes' if armed else 'no'}\nDaily trades: {s.max_daily_trades}\nMax order: {s.max_position_notional} USDT\nSymbols: {', '.join(sorted(s.allowed_symbols))}")


async def futures(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s: Settings = context.bot_data["settings"]
    if not s.mini_app_url:
        await update.effective_message.reply_text("The Mini App URL is not configured.")
        return
    await update.effective_message.reply_text("Open the Demo futures trade ticket. It checks allowed leverage only; it does not place an order.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open futures ticket", web_app=WebAppInfo(f"{s.mini_app_url.rstrip('/')}/futures"))]]))


async def _trade(update: Update, context: ContextTypes.DEFAULT_TYPE, side: Side) -> None:
    s: Settings = context.bot_data["settings"]
    if not admin(update, s):
        await update.effective_message.reply_text("This command is restricted to the administrator.")
        return
    if s.trading_mode == "live" and not context.bot_data["live_armed"]:
        await update.effective_message.reply_text("Live mode is not armed. Use /live_confirm after reviewing the configuration.")
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text("Usage: /trade_buy BTCUSDT 10")
        return
    try:
        intent = OrderIntent(update.effective_user.id, context.args[0].upper(), side, Decimal(context.args[1]), str(uuid.uuid4()))
        result = await context.bot_data["service"].execute(intent)
        await update.effective_message.reply_text(f"{result.status}: {side.value} {intent.symbol}\nOrder: {result.order_id}\nPrice: {result.price}\nQuantity: {result.quantity}")
    except (InvalidOperation, RiskRejected, ExchangeError, ValueError) as exc:
        await update.effective_message.reply_text(f"Order rejected: {exc}")


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _trade(update, context, Side.BUY)


async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _trade(update, context, Side.SELL)


async def live_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s: Settings = context.bot_data["settings"]
    if not admin(update, s) or s.trading_mode != "live" or len(context.args) != 1 or not s.live_confirmation_code or not secure_equals(context.args[0], s.live_confirmation_code):
        await update.effective_message.reply_text("Live confirmation was refused.")
        return
    context.bot_data["live_armed"] = True
    await update.effective_message.reply_text("Live mode armed for this process. Testnet is strongly recommended.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled Telegram update", exc_info=context.error)


def run() -> None:
    settings = Settings.from_env()
    app = build_app(settings)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run()
