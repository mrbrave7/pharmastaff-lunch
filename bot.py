"""Telegram bot: Uzbek messages, admin accept/reject of payments, daily report.

The Mini App is the main interface. The bot's jobs here are:
  * /start, /help — onboard and open the app
  * accept/reject buttons on forwarded payment screenshots
  * notify users when their order is accepted/rejected
  * post the Uzbek end-of-day report to admins at the cutoff hour
  * (fallback) accept a payment screenshot sent directly in chat
"""
import logging
import uuid
from datetime import time
from html import escape as esc
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db

logger = logging.getLogger(__name__)
HTML = ParseMode.HTML


# ─────────────────────────────────────────
#  FORMATTING (Uzbek)
# ─────────────────────────────────────────
def fmt_money(n: int) -> str:
    return f"{n:,}".replace(",", " ") + f" {config.CURRENCY}"


def _webapp_markup() -> InlineKeyboardMarkup | None:
    if config.WEBAPP_URL:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🍽 Menyuni ochish", web_app=WebAppInfo(url=config.WEBAPP_URL))]]
        )
    return None


async def order_caption(order: dict) -> str:
    """Admin-facing caption for a payment screenshot."""
    name = await db.get_name(order["user_id"])
    username = await db.get_username(order["user_id"])
    handle = f" (@{esc(username)})" if username else ""
    lines = [
        f"  • {esc(str(it['qty']))} × {esc(it['name'])} — {fmt_money(it['qty'] * it['unit_price'])}"
        for it in order["items"]
    ]
    return (
        f"🧾 <b>Yangi to'lov</b>  #{order['id']}\n"
        f"👤 {esc(name)}{handle}\n\n"
        f"🍽 <b>Buyurtma:</b>\n" + "\n".join(lines) + "\n\n"
        f"💰 <b>Jami to'lov: {fmt_money(order['total'])}</b>\n\n"
        f"Chek to'g'rimi? Tasdiqlang yoki rad eting:"
    )


def _review_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Qabul qilish", callback_data=f"acc_{order_id}"),
            InlineKeyboardButton("❌ Rad etish", callback_data=f"rej_{order_id}"),
        ]]
    )


# ─────────────────────────────────────────
#  NOTIFICATIONS (called from the web app and from handlers)
# ─────────────────────────────────────────
async def notify_admins_new_payment(bot, order: dict) -> None:
    """Send the screenshot + order summary to every admin with accept/reject buttons."""
    caption = await order_caption(order)
    kb = _review_keyboard(order["id"])
    path = order.get("screenshot_path")
    for admin_id in config.ADMIN_IDS:
        try:
            if path and Path(path).exists():
                await bot.send_photo(
                    chat_id=admin_id, photo=Path(path), caption=caption,
                    parse_mode=HTML, reply_markup=kb,
                )
            else:
                await bot.send_message(
                    chat_id=admin_id, text=caption + "\n\n⚠️ (chek rasmi topilmadi)",
                    parse_mode=HTML, reply_markup=kb,
                )
        except Exception as e:
            logger.error(f"Could not send payment to admin {admin_id}: {e}")


async def notify_user_reviewed(bot, user_id: int, accepted: bool) -> None:
    if accepted:
        text = "✅ <b>Buyurtmangiz tasdiqlandi!</b>\nYoqimli ishtaha! 🍽"
    else:
        text = (
            "❌ <b>To'lov cheki tasdiqlanmadi.</b>\n"
            "Iltimos, to'g'ri to'lov chekini ilova orqali qaytadan yuboring."
        )
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode=HTML)
    except Exception as e:
        logger.warning(f"Could not notify user {user_id}: {e}")


# ─────────────────────────────────────────
#  COMMANDS
# ─────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.add_subscriber(user.id, user.full_name or user.first_name, user.username)
    await update.message.reply_text(
        f"👋 Assalomu alaykum, {esc(user.first_name)}!\n\n"
        "Bu bot orqali har kuni tushlik buyurtma qilishingiz mumkin. 🍽\n"
        "Menyuni ochish uchun quyidagi tugmani yoki chap-pastdagi «🍽 Menyu» tugmasini bosing.",
        parse_mode=HTML,
        reply_markup=_webapp_markup(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ <b>Yordam</b>\n\n"
        "1. «Menyuni ochish» tugmasini bosing.\n"
        "2. Taomlarni tanlang va sonini belgilang.\n"
        "3. «Buyurtma berish» — karta raqami va to'lov summasi chiqadi.\n"
        "4. To'lovni amalga oshiring va chek rasmini ilovaga yuklang.\n"
        "5. Admin tasdiqlagach, buyurtmangiz qabul qilinadi. ✅\n\n"
        f"Buyurtmani soat <b>{config.ORDER_CUTOFF_HOUR}:00</b> gacha bekor qilish mumkin.",
        parse_mode=HTML,
        reply_markup=_webapp_markup(),
    )


# ─────────────────────────────────────────
#  ADMIN ACCEPT / REJECT
# ─────────────────────────────────────────
async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = query.from_user
    if admin.id not in config.ADMIN_IDS:
        await query.answer("Faqat admin uchun.", show_alert=True)
        return

    action, _, oid = query.data.partition("_")
    order_id = int(oid)
    accept = action == "acc"
    order = await db.review_order(order_id, admin.id, accept)

    if order is None:
        await query.answer("Bu buyurtma allaqachon ko'rib chiqilgan.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    decided = "✅ QABUL QILINDI" if accept else "❌ RAD ETILDI"
    by = esc(admin.full_name or admin.first_name)
    try:
        if query.message.caption is not None:
            base = query.message.caption_html or ""
            await query.edit_message_caption(
                caption=f"{base}\n\n<b>{decided}</b> — {by}",
                parse_mode=HTML, reply_markup=None,
            )
        else:
            base = query.message.text_html or ""
            await query.edit_message_text(
                f"{base}\n\n<b>{decided}</b> — {by}",
                parse_mode=HTML, reply_markup=None,
            )
    except Exception as e:
        logger.warning(f"Could not edit review message: {e}")

    await notify_user_reviewed(context.bot, order["user_id"], accept)

    # let the other admins' copies know
    for admin_id in config.ADMIN_IDS:
        if admin_id != admin.id:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"ℹ️ Buyurtma #{order_id}: {decided} ({by}).",
                    parse_mode=HTML,
                )
            except Exception:
                pass

    await query.answer("Bajarildi." if accept else "Rad etildi.")


# ─────────────────────────────────────────
#  FALLBACK: payment screenshot sent in chat
# ─────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    order = await db.get_active_order(user.id)
    if not order or order["status"] not in ("pending_payment", "awaiting_review"):
        return  # nothing to attach it to

    path = str(Path(config.SCREENSHOT_DIR) / f"{order['id']}_{uuid.uuid4().hex}.jpg")
    try:
        tg_file = await context.bot.get_file(update.message.photo[-1].file_id)
        await tg_file.download_to_drive(custom_path=path)
    except Exception as e:
        logger.error(f"Could not download chat screenshot: {e}")
        return

    if not await db.set_order_screenshot(order["id"], user.id, path):
        return
    await update.message.reply_text(
        "🧾 Chek qabul qilindi. Admin tasdig'ini kuting…", parse_mode=HTML
    )
    fresh = await db.get_order(order["id"])
    await notify_admins_new_payment(context.bot, fresh)


# ─────────────────────────────────────────
#  DAILY REPORT
# ─────────────────────────────────────────
def report_text(summary: dict) -> str:
    if not summary["dishes"]:
        body = "Bugun tasdiqlangan buyurtmalar yo'q."
    else:
        lines = [
            f"  • {esc(d['name'])} — <b>{d['qty']} ta</b> ({fmt_money(d['subtotal'])})"
            for d in summary["dishes"]
        ]
        body = "\n".join(lines)
    text = (
        "📊 <b>Bugungi buyurtmalar yakuni</b>\n\n"
        + body
        + f"\n\n✅ Jami: <b>{summary['order_count']} ta</b> buyurtma — "
        f"<b>{fmt_money(summary['money'])}</b>"
    )
    if summary["unpaid"]:
        text += f"\n🕓 To'lanmagan/kutilayotgan: {summary['unpaid']} ta"
    return text


async def job_daily_report(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running daily report job")
    await db.set_ordering_open(False)
    summary = await db.report_summary()
    text = report_text(summary)
    for admin_id in config.ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, parse_mode=HTML)
        except Exception as e:
            logger.error(f"Could not send report to admin {admin_id}: {e}")


# ─────────────────────────────────────────
#  STARTUP
# ─────────────────────────────────────────
async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Handler error", exc_info=context.error)


async def _post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Botni ishga tushirish / menyu"),
            BotCommand("help", "Yordam"),
        ]
    )
    if config.WEBAPP_URL:
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Buyurtma berish", web_app=WebAppInfo(url=config.WEBAPP_URL)
                )
            )
        except Exception as e:
            logger.warning(f"Could not set chat menu button: {e}")


def build_application() -> Application:
    app = Application.builder().token(config.BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(review_callback, pattern=r"^(acc|rej)_\d+$"))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))
    app.add_error_handler(_on_error)

    app.job_queue.run_daily(
        job_daily_report,
        time=time(hour=config.ORDER_CUTOFF_HOUR, minute=0, tzinfo=config.TZ),
        name="daily_report",
    )
    return app
