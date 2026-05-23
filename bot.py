# ============================================================
# IvaSms Number Bot - Telegram OTP Number Service
# ============================================================

import asyncio
import html as html_module
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from uuid import uuid4

from telegram import (
    Bot,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    MessageHandler, filters,
    ContextTypes,
)

_RUNTIME_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "IvaSms-api")
if _RUNTIME_CONFIG_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_CONFIG_DIR)

from runtime_config import (
    BOT_TOKEN,
    ADMIN_IDS,
    COUNTRY_FLAGS,
    OTP_CHANNEL_ID,
    OTP_CHANNEL_URL,
    BOT_PUBLIC_URL,
    OTP_POLL_SECONDS,
    IVASMS_SESSION_RESTORE_COOLDOWN,
    BOT_RUN_MODE,
    BOT_WEBHOOK_URL,
    BOT_WEBHOOK_SECRET,
    BOT_LISTEN_HOST,
    BOT_LISTEN_PORT,
)
from gacha_engine import Database
from iva_client import IVASSMSClient
from number_service import init_stock, load_numbers_from_excel, normalize_country_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ────────── Globals ──────────
db = Database()
iva_client = IVASSMSClient()
session_lost_notified = False
session_failure_count = 0
manual_login_in_progress = False
last_restore_attempt_at = 0.0
last_session_skip_log_at = 0.0
last_poll_status_log_at = 0.0
last_poll_status_count = -1
next_otp_poll_not_before_at = 0.0
polling_conflict_shutdown = False
session_restore_task = None
REFRESH_SMS_JOB_NAME = "refresh-sms-monitor"
WITHDRAW_MIN_USD = 1.0
WITHDRAW_MIN_DANA = 10000
ADDNUM_STATE_KEY = "addnum_setup"

MOJIBAKE_REPLACEMENTS = {
    "\u00e2\u20ac\u00a2": "•",
    "\u00e2\u0153\u2026": "✅",
    "\u00e2\u009d\u0152": "❌",
    "\u00e2\u008f\u00b3": "⏳",
    "\u00e2\u008f\u00b0": "⏰",
    "\u00e2\u00ad\u0090": "⭐",
    "\u00e2\u20ac\u201d": "—",
    "\u00e2\u201d\u0081": "━",
    "\u00e2\u201d\u20ac": "─",
    "\u00e2\u2022\u201d": "╔",
    "\u00e2\u2022\u0161": "╚",
    "\u00e2\u2022\u0090": "═",
    "\u00e2\u2022\u2018": "║",
    "\u00e2\u2022\u2014": "╗",
    "\u00e2\u2022\u009d": "╝",
    "\u00e2\u0161\u00a0\u00ef\u00b8\u008f": "⚠️",
    "\u00e2\u201e\u00b9\u00ef\u00b8\u008f": "ℹ️",
    "\u00f0\u0178\u2018\u2039": "👋",
    "\u00f0\u0178\u2018\u00a4": "👤",
    "\u00f0\u0178\u2019\u00ac": "💬",
    "\u00f0\u0178\u2019\u00b3": "💳",
    "\u00f0\u0178\u2019\u00b8": "💸",
    "\u00f0\u0178\u2019\u00be": "💾",
    "\u00f0\u0178\u2019\u017d": "💎",
    "\u00f0\u0178\u201c\u00a1": "📡",
    "\u00f0\u0178\u201c\u00a5": "📥",
    "\u00f0\u0178\u201c\u00a6": "📦",
    "\u00f0\u0178\u201c\u00a9": "📩",
    "\u00f0\u0178\u201c\u00b1": "📱",
    "\u00f0\u0178\u201c\u00b2": "📲",
    "\u00f0\u0178\u201c\u017d": "📎",
    "\u00f0\u0178\u201c\u02c6": "📈",
    "\u00f0\u0178\u201c\u02dc": "📘",
    "\u00f0\u0178\u201d\u008d": "🔍",
    "\u00f0\u0178\u201d\u0090": "🔐",
    "\u00f0\u0178\u201d\u017d": "🔎",
    "\u00f0\u0178\u201d\u201e": "🔄",
    "\u00f0\u0178\u201d\u2122": "🔙",
    "\u00f0\u0178\u201d\u00bd": "🔽",
    "\u00f0\u0178\u0152\u008d": "🌍",
    "\u00f0\u0178\u0152\u00b0": "🌰",
    "\u00f0\u0178\u008f\u00a0": "🏠",
    "\u00f0\u0178\u008f\u00a7": "🏧",
    "\u00f0\u0178\u008f\u2020": "🏆",
    "\u00f0\u0178\u008f\u00b3\u00ef\u00b8\u008f": "🏳️",
    "\u00f0\u0178\xa7\xb9": "🧹",
    "\u00f0\u0178\u0178\u00a1": "🟡",
}


def _repair_cp1252_utf8_runs(value: str) -> str:
    # Repair remaining mojibake runs such as "━━━" -> "━━━━━".
    def replace_match(match: re.Match) -> str:
        chunk = match.group(0)
        try:
            return chunk.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return chunk

    pattern = r"[\u0080-\u024f\u2018-\u201f\u20ac\u2122\u0152\u0153\u0178]+"
    repaired = value
    for _ in range(2):
        updated = re.sub(pattern, replace_match, repaired)
        if updated == repaired:
            break
        repaired = updated
    return repaired


def clean_text(value: str) -> str:
    if not isinstance(value, str):
        return value

    cleaned = value
    for bad, good in MOJIBAKE_REPLACEMENTS.items():
        cleaned = cleaned.replace(bad, good)
    return _repair_cp1252_utf8_runs(cleaned)


def _install_telegram_text_sanitizer():
    original_reply_text = Message.reply_text
    original_edit_text = Message.edit_text
    original_send_message = Bot.send_message
    original_edit_message_text = CallbackQuery.edit_message_text

    async def patched_reply_text(self, text, *args, **kwargs):
        return await original_reply_text(self, clean_text(text), *args, **kwargs)

    async def patched_edit_text(self, text, *args, **kwargs):
        return await original_edit_text(self, clean_text(text), *args, **kwargs)

    async def patched_send_message(self, chat_id, text, *args, **kwargs):
        return await original_send_message(
            self,
            chat_id,
            clean_text(text),
            *args,
            **kwargs,
        )

    async def patched_edit_message_text(self, text, *args, **kwargs):
        return await original_edit_message_text(
            self,
            clean_text(text),
            *args,
            **kwargs,
        )

    Message.reply_text = patched_reply_text
    Message.edit_text = patched_edit_text
    Bot.send_message = patched_send_message
    CallbackQuery.edit_message_text = patched_edit_message_text


_install_telegram_text_sanitizer()


def get_flag(country: str) -> str:
    return COUNTRY_FLAGS.get(country.upper(), "🏳️")


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def get_flag(country: str) -> str:
    normalized = normalize_country_name(country)
    return COUNTRY_FLAGS.get(normalized.upper(), "🏳️")


def get_country_display(country: str) -> str:
    normalized = normalize_country_name(country)
    return normalized.title() if normalized else str(country or "").strip()


def get_country_hashtag(country: str) -> str:
    normalized = normalize_country_name(country)
    slug = re.sub(r"[^A-Z0-9]+", "_", normalized.upper()).strip("_")
    return f"#{slug}" if slug else "#UNKNOWN"


def get_display_name(user_row: dict) -> str:
    return html_module.escape(
        user_row.get("first_name") or user_row.get("username") or str(user_row.get("telegram_id"))
    )


def format_usd(amount: float) -> str:
    return f"${amount:.2f}"


def format_idr(amount: int) -> str:
    return f"Rp{int(amount):,}".replace(",", ".")


def mask_account_number(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 4:
        return text
    return f"{text[:2]}••••••{text[-2:]}"


def build_profile_text(profile: dict, user) -> str:
    method = profile.get("withdraw_method") or "-"
    withdraw_name = profile.get("withdraw_name") or "-"
    withdraw_account = profile.get("withdraw_account") or "-"
    pending_withdraw = profile.get("withdraw_pending_id") or "-"
    return "\n".join([
        f"👤 <b>Profile {html_module.escape(user.first_name or 'User')}</b>",
        "",
        f"• ID: <code>{user.id}</code>",
        f"• Username: <code>{html_module.escape(user.username or '-')}</code>",
        f"• Total nomor diambil: <b>{profile.get('total_numbers', 0)}</b>",
        f"• Nomor aktif: <b>{profile.get('active_numbers', 0)}</b>",
        f"• Kode referral: <code>{html_module.escape(profile.get('ref_code') or '-')}</code>",
        f"• Total referral: <b>{profile.get('referral_count', 0)}</b>",
        f"• Bonus referral: <b>{profile.get('referral_bonus', 0)}</b>",
        f"• Saldo USD: <b>{format_usd(float(profile.get('balance_usd', 0) or 0))}</b>",
        f"• Saldo DANA: <b>{format_idr(int(profile.get('balance_dana', 0) or 0))}</b>",
        f"• Metode WD: <b>{html_module.escape(str(method))}</b>",
        f"• Nama WD: <code>{html_module.escape(str(withdraw_name))}</code>",
        f"• Tujuan WD: <code>{html_module.escape(str(withdraw_account))}</code>",
        f"• Pending WD: <code>{html_module.escape(str(pending_withdraw))}</code>",
    ])


def build_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Set Withdraw", callback_data="profile_setwithdraw")],
        [InlineKeyboardButton("🏧 Tarik USD", callback_data="withdraw_usd"),
         InlineKeyboardButton("💳 Tarik DANA", callback_data="withdraw_dana")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
        except Exception as exc:
            logger.error(f"Failed to notify admin {admin_id}: {exc}")


def build_restock_notification(report: dict) -> str | None:
    country_changes = report.get("country_changes") or {}
    summary_lines = []
    for country, changes in sorted(country_changes.items()):
        total_added = int(changes.get("added", 0) or 0) + int(changes.get("reactivated", 0) or 0)
        if total_added <= 0:
            continue
        summary_lines.append(
            f"{html_module.escape(country.title())} {get_flag(country)}💬 added: <b>{total_added}</b>"
        )

    if not summary_lines:
        return None

    return "\n".join([
        "📦 <b>Stock Update</b>",
        "",
        *summary_lines,
    ])


async def broadcast_restock_notification(context: ContextTypes.DEFAULT_TYPE, report: dict):
    text = build_restock_notification(report)
    if not text:
        return

    user_ids = db.get_all_user_ids()
    delivered = 0
    for telegram_id in user_ids:
        try:
            await context.bot.send_message(chat_id=telegram_id, text=text, parse_mode="HTML")
            delivered += 1
        except Exception as exc:
            logger.warning(f"Restock notice skipped for user {telegram_id}: {exc}")

    try:
        await context.bot.send_message(chat_id=OTP_CHANNEL_ID, text=text, parse_mode="HTML")
    except Exception as exc:
        logger.error(f"Failed to send restock notice to channel {OTP_CHANNEL_ID}: {exc}")


def build_restock_notification(report: dict) -> str | None:
    country_changes = report.get("country_changes") or {}
    summary_lines = []
    for country, changes in sorted(country_changes.items()):
        total_added = int(changes.get("added", 0) or 0) + int(changes.get("reactivated", 0) or 0)
        if total_added <= 0:
            continue
        summary_lines.append(
            f"{html_module.escape(get_country_display(country))} {get_flag(country)} added: <b>{total_added}</b>"
        )

    if not summary_lines:
        return None

    return "\n".join([
        "📦 <b>Stock Update</b>",
        "",
        *summary_lines,
    ])

    logger.info("Restock notice delivered to %s user(s)", delivered)


async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def safe_answer_callback(query: CallbackQuery, *args, **kwargs):
    try:
        return await query.answer(*args, **kwargs)
    except BadRequest as exc:
        message = str(exc).lower()
        if "query is too old" in message or "query id is invalid" in message or "response timeout expired" in message:
            logger.info(f"Callback answer skipped: {exc}")
            return None
        raise


async def safe_delete_message(message: Message | None):
    if not message:
        return
    try:
        await message.delete()
    except Exception:
        pass


async def cleanup_message_ids(bot: Bot, chat_id: int, message_ids: list[int] | None):
    if not message_ids:
        return
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass


def parse_numbers_from_text(raw_text: str) -> list[str]:
    seen = set()
    parsed = []
    text = str(raw_text or "")
    for match in re.finditer(r"\d{7,20}", text):
        phone_number = re.sub(r"\D", "", match.group(0))
        if len(phone_number) < 7:
            continue
        if phone_number in seen:
            continue
        seen.add(phone_number)
        parsed.append(phone_number)
    return parsed


async def load_numbers_from_uploaded_text(document) -> str:
    tg_file = await document.get_file()
    payload = await tg_file.download_as_bytearray()
    raw_bytes = bytes(payload)
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="ignore")


async def process_addnum_document(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_document, title: str | None = None):
    user = update.effective_user
    file_name = str(reply_document.file_name or "").lower()
    processing = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="📥 <b>Processing addnum document...</b>",
        parse_mode="HTML",
    )
    filepath = ""
    try:
        if file_name.endswith(".xlsx"):
            filepath = os.path.join(
                tempfile.gettempdir(),
                f"ivasms-restock-{user.id}-{uuid4().hex}.xlsx",
            )
            tg_file = await reply_document.get_file()
            await tg_file.download_to_drive(filepath)
            numbers = load_numbers_from_excel(filepath)
        elif file_name.endswith(".txt") or file_name.endswith(".csv"):
            raw_text = await load_numbers_from_uploaded_text(reply_document)
            effective_title = str(title or "").strip()
            if not effective_title:
                base_name = os.path.splitext(os.path.basename(file_name))[0].replace("_", " ").replace("-", " ").strip()
                effective_title = base_name or "MANUAL IMPORT"
            numbers = build_manual_number_payload(effective_title, parse_numbers_from_text(raw_text))
        else:
            await processing.edit_text(
                "❌ File harus `.xlsx`, `.txt`, atau `.csv`.",
                parse_mode="HTML",
            )
            return

        if not numbers:
            await processing.edit_text("❌ Tidak ada nomor valid yang terbaca.", parse_mode="HTML")
            return

        report = db.add_numbers_report(numbers)
        stock = db.get_stock_by_country()
        lines = [
            "✅ <b>Restock selesai!</b>",
            "",
            f"• Sumber file: <code>{html_module.escape(reply_document.file_name or 'upload')}</code>",
            f"• Baris valid terbaca: <b>{report['total']}</b>",
            f"• Nomor baru masuk: <b>{report['added']}</b>",
            f"• Nomor diaktifkan lagi: <b>{report['reactivated']}</b>",
            f"• Duplikat / terlewati: <b>{report['duplicates']}</b>",
            "",
        ]
        if report["duplicate_samples"]:
            lines.append(
                f"• Sampel duplikat: <code>{html_module.escape(', '.join(report['duplicate_samples']))}</code>"
            )
            lines.append("")
        for country, count in sorted(stock.items()):
            lines.append(f"  {get_flag(country)} {country}: <b>{count}</b>")

        await processing.edit_text("\n".join(lines), parse_mode="HTML")
        await broadcast_restock_notification(context, report)
    except Exception as exc:
        await processing.edit_text(f"❌ Error addnum: {html_module.escape(str(exc))}", parse_mode="HTML")
    finally:
        if filepath:
            try:
                os.remove(filepath)
            except Exception:
                pass


def build_manual_number_payload(range_name: str, phone_numbers: list[str]) -> list[dict]:
    title = str(range_name or "").strip()
    country = normalize_country_name(title)
    return [
        {
            "phone_number": phone_number,
            "country": country,
            "range_name": title,
            "rate": 0.0,
        }
        for phone_number in phone_numbers
    ]


def parse_test_date_input(raw_value: str | None) -> str:
    value = str(raw_value or "").strip()
    now = datetime.now()
    if not value:
        return now.strftime("%Y-%m-%d")

    if re.fullmatch(r"\d{1,2}", value):
        parsed = datetime(now.year, now.month, int(value))
        return parsed.strftime("%Y-%m-%d")

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue

    raise ValueError("Format tanggal tidak valid. Gunakan `21`, `2026-05-21`, atau `21/05/2026`.")


def build_delnum_scope_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Hapus Semua Stok", callback_data="delnum_scope_all")],
        [InlineKeyboardButton("🌍 Pilih Negara", callback_data="delnum_scope_country")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])


def build_delnum_country_keyboard(stock: dict) -> InlineKeyboardMarkup:
    rows = []
    for country, count in sorted(stock.items()):
        rows.append([
            InlineKeyboardButton(
                f"{get_flag(country)} {country} ({count})",
                callback_data=f"delnum_country_{country}",
            )
        ])
    rows.append([InlineKeyboardButton("🔙 Kembali", callback_data="delnum_back_scope")])
    return InlineKeyboardMarkup(rows)


def build_delnum_confirm_keyboard(target: str, is_all: bool) -> InlineKeyboardMarkup:
    confirm_data = "delnum_confirm_all" if is_all else f"delnum_confirm_country_{target}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ya, Hapus", callback_data=confirm_data)],
        [InlineKeyboardButton("🔙 Batal", callback_data="delnum_back_scope")],
    ])


def get_refreshsms_job(context: ContextTypes.DEFAULT_TYPE):
    jobs = context.job_queue.get_jobs_by_name(REFRESH_SMS_JOB_NAME)
    return jobs[0] if jobs else None


# ══════════════════════════════════════════════════════════
# COMMANDS
# ══════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.get_or_create_user(user.id, user.username or "", user.first_name or "")
    stock = db.get_total_stock()
    keyboard = [
        [InlineKeyboardButton("📱 Get Number", callback_data="get_number")],
        [InlineKeyboardButton("👤 Profile", callback_data="profile_view"),
         InlineKeyboardButton("?? Help User", callback_data="help_user")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")],
    ]
    msg = (
        f"👋 <b>Welcome, {html_module.escape(user.first_name)}!</b>\n\n"
        f"📡 <b>IvaSms Number Bot</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Total Stock: <b>{stock}</b> numbers\n\n"
        f"🔽 Select a button from below:\n"
        f"  📱 <b>Get Number</b>\n"
        f"  🏆 <b>Leaderboard</b>"
    )
    await update.message.reply_text(msg, parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock = db.get_stock_by_country()
    total = sum(stock.values())
    lines = [f"📦 <b>Stock: {total} numbers</b>\n"]
    for country, count in sorted(stock.items()):
        lines.append(f"  {get_flag(country)} {country}: <b>{count}</b>")
    if not stock:
        lines.append("  <i>No numbers.</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_setlogin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global session_lost_notified
    global session_failure_count
    global manual_login_in_progress
    global last_restore_attempt_at
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /setlogin email password")
        return

    email, password = args[0], args[1]
    msg = await update.message.reply_text(
        "🔐 <b>Logging in to IvaSms...</b>\n"
        "⏳ Bypass Cloudflare (max 2 menit)...",
        parse_mode="HTML"
    )
    try:
        await update.message.delete()
    except Exception:
        pass

    manual_login_in_progress = True
    try:
        # Playwright async - tidak perlu to_thread lagi
        success = await iva_client.login_with_credentials(email, password)
        last_restore_attempt_at = asyncio.get_running_loop().time()

        if success:
            session_lost_notified = False
            session_failure_count = 0
            iva_client.save_credentials(email, password)
            await msg.edit_text(
                "✅ <b>Login Berhasil!</b>\n\n"
                "📡 IvaSms: Online ✅\n"
                "💾 Credential tersimpan untuk auto-login mandiri.\n"
                "📦 OTP akan dipantau otomatis.",
                parse_mode="HTML"
            )
        else:
            await msg.edit_text(
                "❌ <b>Login Gagal!</b>\n\n"
                "Kemungkinan:\n"
                "• Cloudflare masih blocking\n"
                "• Email/password salah\n\n"
                "Coba lagi: /setlogin email pass",
                parse_mode="HTML"
            )
    finally:
        manual_login_in_progress = False

async def cmd_addnum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text(
            "📎 <b>Cara restock:</b>\n\n"
            "1. Kirim file .xlsx ke chat\n"
            "2. Reply file dengan /addnum",
            parse_mode="HTML"
        )
        return

    if not reply.document.file_name.endswith('.xlsx'):
        await update.message.reply_text("❌ File harus .xlsx")
        return

    msg = await update.message.reply_text("📥 <b>Processing...</b>", parse_mode="HTML")
    filepath = os.path.join(
        tempfile.gettempdir(),
        f"ivasms-restock-{user.id}-{uuid4().hex}.xlsx",
    )
    try:
        file = await reply.document.get_file()
        await file.download_to_drive(filepath)

        numbers = load_numbers_from_excel(filepath)
        if not numbers:
            await msg.edit_text("❌ Tidak ada nomor di file.")
            return

        report = db.add_numbers_report(numbers)
        stock = db.get_stock_by_country()
        lines = [
            f"✅ <b>Restock selesai!</b>",
            "",
            f"• Baris valid terbaca: <b>{report['total']}</b>",
            f"• Nomor baru masuk: <b>{report['added']}</b>",
            f"• Nomor diaktifkan lagi: <b>{report['reactivated']}</b>",
            f"• Duplikat / terlewati: <b>{report['duplicates']}</b>",
            "",
        ]
        if report["duplicate_samples"]:
            lines.append(
                f"• Sampel duplikat: <code>{html_module.escape(', '.join(report['duplicate_samples']))}</code>"
            )
            lines.append("")
        if report["added"] == 0 and report["reactivated"] == 0 and report["total"] > 0:
            lines.append("ℹ️ Semua nomor yang terbaca sudah ada di database dan masih aktif, atau format file masih bentrok dengan data lama.")
            lines.append("")
        for country, count in sorted(stock.items()):
            lines.append(f"  {get_flag(country)} {country}: <b>{count}</b>")
        await msg.edit_text("\n".join(lines), parse_mode="HTML")
        await broadcast_restock_notification(context, report)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")
    finally:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass


async def cmd_delnum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    stock = db.get_stock_by_country()
    total = sum(stock.values())
    assigned = db.get_assigned_count()
    if total <= 0:
        await update.message.reply_text(
            "ℹ️ Tidak ada stok available untuk dihapus.\n"
            f"Assigned aktif saat ini: <b>{assigned}</b>",
            parse_mode="HTML",
        )
        return

    lines = [
        "🗑 <b>Hapus Stok Nomor</b>",
        "",
        f"• Stok available saat ini: <b>{total}</b>",
        f"• Assigned aktif tidak akan dihapus: <b>{assigned}</b>",
        "",
        "Pilih aksi yang ingin dijalankan:",
    ]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=build_delnum_scope_keyboard(),
    )


async def cmd_statuslogin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    credentials_saved = iva_client.has_saved_credentials()
    cookies_saved = iva_client.has_cookies_file()
    page_url = iva_client._page.url if iva_client._page else "-"
    http_status = await iva_client.get_session_status_http()
    csrf_ready = bool(iva_client.csrf_token or http_status.get("token_ready"))
    portal_page_ready = "/portal/sms/received" in page_url
    session_ready = bool(
        http_status.get("active")
        or (iva_client.logged_in and iva_client._page and portal_page_ready and csrf_ready)
    )

    lines = [
        "🔎 <b>Status Login IvaSms</b>",
        "",
        f"• Session aktif: {'✅' if session_ready else '❌'}",
        f"• Manual login berjalan: {'✅' if manual_login_in_progress else '❌'}",
        f"• Cookies tersimpan: {'✅' if cookies_saved else '❌'}",
        f"• Credential tersimpan: {'✅' if credentials_saved else '❌'}",
        f"• CSRF siap: {'✅' if csrf_ready else '❌'}",
        f"• URL aktif: <code>{html_module.escape(page_url)}</code>",
        f"• Session failure count: <code>{session_failure_count}</code>",
        f"• Cookies path: <code>{html_module.escape(iva_client._cookies_file)}</code>",
        f"• Credentials path: <code>{html_module.escape(str(iva_client._credentials_file))}</code>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_clearlogin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global session_failure_count
    global session_lost_notified

    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    await iva_client.close()
    cleared = iva_client.clear_saved_session()
    session_failure_count = 0
    session_lost_notified = False
    await update.message.reply_text(
        "🧹 <b>Login session dibersihkan</b>\n\n"
        f"• Cookies dihapus: {'✅' if cleared['cookies'] else '❌'}\n"
        f"• Credential dihapus: {'✅' if cleared['credentials'] else '❌'}",
        parse_mode="HTML",
    )


async def cmd_relogin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global manual_login_in_progress
    global session_failure_count
    global session_lost_notified
    global last_restore_attempt_at

    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    credentials = iva_client.load_credentials()
    if not credentials:
        await update.message.reply_text("❌ Credential tersimpan tidak ditemukan. Gunakan /setlogin dulu.")
        return

    msg = await update.message.reply_text("🔄 <b>Memulai relogin dari credential tersimpan...</b>", parse_mode="HTML")
    manual_login_in_progress = True
    try:
        success = await iva_client.login_with_credentials(credentials["email"], credentials["password"])
        last_restore_attempt_at = asyncio.get_running_loop().time()
        if success:
            session_failure_count = 0
            session_lost_notified = False
            await msg.edit_text("✅ <b>Relogin berhasil.</b>", parse_mode="HTML")
        else:
            await msg.edit_text("❌ <b>Relogin gagal.</b>", parse_mode="HTML")
    finally:
        manual_login_in_progress = False


async def cmd_refreshsession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    msg = await update.message.reply_text("🔄 <b>Merefresh session IvaSms...</b>", parse_mode="HTML")
    refreshed = await iva_client.refresh_session()
    if not refreshed:
        refreshed = await iva_client.ensure_logged_in(startup_mode=True)
    if refreshed:
        await msg.edit_text("✅ <b>Session aktif dan sudah direfresh.</b>", parse_mode="HTML")
    else:
        await msg.edit_text("❌ <b>Session belum aktif.</b>\nGunakan /setlogin atau /relogin.", parse_mode="HTML")


async def refresh_sms_job(context: ContextTypes.DEFAULT_TYPE):
    if manual_login_in_progress:
        return

    if not iva_client.logged_in:
        await iva_client.ensure_logged_in(startup_mode=True)
        if not iva_client.logged_in:
            return

    refreshed = await iva_client.refresh_session()
    if not refreshed:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    result = await iva_client.check_otps(from_date=today, to_date=today)
    current_count = int((result or {}).get("count_sms", 0) or 0)

    job = get_refreshsms_job(context)
    if not job:
        return

    previous_count = job.data.get("last_count")
    job.data["last_count"] = current_count

    if previous_count is None or current_count == previous_count:
        return

    trend = "bertambah" if current_count > previous_count else "berkurang"
    lines = [
        "🔄 <b>Refresh SMS Update</b>",
        "",
        f"• OTP hari ini sekarang: <b>{current_count}</b>",
        f"• Perubahan dari refresh sebelumnya: <b>{trend}</b>",
    ]
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text="\n".join(lines), parse_mode="HTML")
        except Exception as exc:
            logger.warning(f"Failed to send refresh SMS update to admin {admin_id}: {exc}")


async def cmd_refreshsms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    action = (context.args[0].strip().lower() if context.args else "on")
    current_job = get_refreshsms_job(context)

    if action in {"off", "stop"}:
        if current_job:
            current_job.schedule_removal()
            await update.message.reply_text("✅ Auto refresh SMS per detik dimatikan.", parse_mode="HTML")
        else:
            await update.message.reply_text("ℹ️ Auto refresh SMS belum aktif.", parse_mode="HTML")
        return

    if action in {"status", "cek"}:
        status_text = "aktif" if current_job else "mati"
        await update.message.reply_text(
            f"🔎 Auto refresh SMS saat ini: <b>{status_text}</b>",
            parse_mode="HTML",
        )
        return

    if current_job:
        await update.message.reply_text(
            "ℹ️ Auto refresh SMS sudah aktif. Gunakan /refreshsms off untuk mematikan.",
            parse_mode="HTML",
        )
        return

    context.job_queue.run_repeating(
        refresh_sms_job,
        interval=1,
        first=0,
        name=REFRESH_SMS_JOB_NAME,
        data={"last_count": None, "started_by": user.id},
    )
    await update.message.reply_text(
        "✅ Auto refresh SMS aktif. Bot akan refresh halaman SMS tiap 1 detik.\n"
        "Gunakan /refreshsms off untuk mematikan atau /refreshsms status untuk cek status.",
        parse_mode="HTML",
    )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.get_or_create_user(user.id, user.username or "", user.first_name or "")
    profile = db.get_user_profile(user.id)
    await update.message.reply_text(
        build_profile_text(profile, user),
        parse_mode="HTML",
        reply_markup=build_profile_keyboard(),
    )


async def cmd_refstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    profile = db.get_or_create_user(user.id, user.username or "", user.first_name or "")
    lines = [
        "?? <b>Referral Stats</b>",
        "",
        f"• Kode referral: <code>{html_module.escape(profile.get('ref_code') or '-')}</code>",
        f"• Total referral masuk: <b>{profile.get('referral_count', 0)}</b>",
        f"• Bonus referral: <b>{profile.get('referral_bonus', 0)}</b>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_topref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = db.get_referral_leaderboard(10)
    lines = ["🏆 <b>Top Referral</b>", ""]
    if not top:
        lines.append("Belum ada data referral.")
    else:
        for index, row in enumerate(top, start=1):
            lines.append(
                f"{index}. <b>{get_display_name(row)}</b> — {row.get('referral_count', 0)} ref, bonus {row.get('referral_bonus', 0)}"
            )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_bonusref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    profile = db.get_or_create_user(user.id, user.username or "", user.first_name or "")
    await update.message.reply_text(
        "🎁 <b>Bonus Referral</b>\n\n"
        f"• Kode referral kamu: <code>{html_module.escape(profile.get('ref_code') or '-')}</code>\n"
        f"• Bonus terkumpul: <b>{profile.get('referral_bonus', 0)}</b>\n"
        f"• Total referral aktif: <b>{profile.get('referral_count', 0)}</b>",
        parse_mode="HTML",
    )


async def cmd_helpuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "?? <b>Help User</b>",
        "",
        "• /start - buka menu utama",
        "• /stock - lihat stok negara",
        "• /profile - lihat profil dan referral",
        "• /refstats - statistik referral kamu",
        "• /topref - leaderboard referral",
        "• /bonusref - ringkasan bonus referral",
        "• /helpuser - daftar command user",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    user = update.effective_user
    setup = context.user_data.get("withdraw_setup")
    if not setup:
        return

    text = message.text.strip()
    if not text:
        await message.reply_text("❌ Input kosong.")
        return

    if setup["step"] == "name":
        db.update_profile_meta(user.id, withdraw_method=setup["method"], withdraw_name=text)
        setup["step"] = "account"
        context.user_data["withdraw_setup"] = setup
        await message.reply_text(
            f"✅ Nama pemilik tersimpan untuk {setup['method']}.\n\nSekarang kirim nomor rekening / ID wallet tujuan.",
            parse_mode="HTML",
        )
        return

    if setup["step"] == "account":
        db.update_profile_meta(user.id, withdraw_account=text)
        context.user_data.pop("withdraw_setup", None)
        profile = db.get_user_profile(user.id)
        await message.reply_text(
            "✅ Metode withdraw berhasil disimpan.",
            parse_mode="HTML",
        )
        await message.reply_text(
            build_profile_text(profile, user),
            parse_mode="HTML",
            reply_markup=build_profile_keyboard(),
        )


async def finalize_manual_addnum(update: Update, context: ContextTypes.DEFAULT_TYPE, setup: dict, title: str, raw_numbers_text: str):
    phone_numbers = parse_numbers_from_text(raw_numbers_text)
    if not phone_numbers:
        warning = await update.message.reply_text(
            "❌ Tidak ada nomor valid yang terbaca.\nKirim ulang daftar nomor, satu nomor per baris.",
            parse_mode="HTML",
        )
        cleanup_ids = setup.setdefault("cleanup_message_ids", [])
        cleanup_ids.append(warning.message_id)
        context.user_data[ADDNUM_STATE_KEY] = setup
        return

    numbers = build_manual_number_payload(title, phone_numbers)
    report = db.add_numbers_report(numbers)
    stock = db.get_stock_by_country()
    lines = [
        "✅ <b>Restock manual selesai!</b>",
        "",
        f"• Title / CTAX: <code>{html_module.escape(title)}</code>",
        f"• Nomor valid terbaca: <b>{len(phone_numbers)}</b>",
        f"• Nomor baru masuk: <b>{report['added']}</b>",
        f"• Nomor diaktifkan lagi: <b>{report['reactivated']}</b>",
        f"• Duplikat / terlewati: <b>{report['duplicates']}</b>",
        "",
    ]
    if report["duplicate_samples"]:
        lines.append(
            f"• Sampel duplikat: <code>{html_module.escape(', '.join(report['duplicate_samples']))}</code>"
        )
        lines.append("")
    for country, count in sorted(stock.items()):
        lines.append(f"  {get_flag(country)} {country}: <b>{count}</b>")

    cleanup_ids = list(set(setup.get("cleanup_message_ids", []) + [update.message.message_id]))
    context.user_data.pop(ADDNUM_STATE_KEY, None)
    await cleanup_message_ids(context.bot, update.effective_chat.id, cleanup_ids)
    summary = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="\n".join(lines),
        parse_mode="HTML",
    )
    await safe_delete_message(update.message)
    await broadcast_restock_notification(context, report)
    logger.info("Manual addnum completed by admin %s with summary message %s", update.effective_user.id, summary.message_id)


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    user = update.effective_user
    addnum_setup = context.user_data.get(ADDNUM_STATE_KEY)
    if addnum_setup and is_admin(user.id):
        cleanup_ids = addnum_setup.setdefault("cleanup_message_ids", [])
        cleanup_ids.append(message.message_id)
        text = message.text.strip()
        if not text:
            warning = await message.reply_text("❌ Input kosong.")
            cleanup_ids.append(warning.message_id)
            context.user_data[ADDNUM_STATE_KEY] = addnum_setup
            return

        if addnum_setup.get("step") == "title":
            addnum_setup["title"] = text
            addnum_setup["step"] = "numbers"
            prompt = await message.reply_text(
                "📲 <b>Kirim daftar nomor</b>\n\n"
                "Bisa teks panjang, satu nomor per baris, atau file `.txt/.csv/.xlsx`.\n"
                "Contoh:\n"
                "<code>233268500105\n233268500213\n233268500102</code>",
                parse_mode="HTML",
            )
            cleanup_ids.append(prompt.message_id)
            context.user_data[ADDNUM_STATE_KEY] = addnum_setup
            return

        if addnum_setup.get("step") == "numbers":
            await finalize_manual_addnum(
                update,
                context,
                addnum_setup,
                addnum_setup.get("title", ""),
                text,
            )
            return

    setup = context.user_data.get("withdraw_setup")
    if not setup:
        return

    text = message.text.strip()
    if not text:
        await message.reply_text("âŒ Input kosong.")
        return

    if setup["step"] == "name":
        db.update_profile_meta(user.id, withdraw_method=setup["method"], withdraw_name=text)
        setup["step"] = "account"
        context.user_data["withdraw_setup"] = setup
        await message.reply_text(
            f"âœ… Nama pemilik tersimpan untuk {setup['method']}.\n\nSekarang kirim nomor rekening / ID wallet tujuan.",
            parse_mode="HTML",
        )
        return

    if setup["step"] == "account":
        db.update_profile_meta(user.id, withdraw_account=text)
        context.user_data.pop("withdraw_setup", None)
        profile = db.get_user_profile(user.id)
        await message.reply_text(
            "âœ… Metode withdraw berhasil disimpan.",
            parse_mode="HTML",
        )
        await message.reply_text(
            build_profile_text(profile, user),
            parse_mode="HTML",
            reply_markup=build_profile_keyboard(),
        )


async def handle_document_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.document:
        return

    user = update.effective_user
    addnum_setup = context.user_data.get(ADDNUM_STATE_KEY)
    if addnum_setup and is_admin(user.id):
        cleanup_ids = addnum_setup.setdefault("cleanup_message_ids", [])
        cleanup_ids.append(message.message_id)
        if addnum_setup.get("step") != "numbers":
            warning = await message.reply_text(
                "❌ Kirim title/ctax dulu sebelum upload file nomor.",
                parse_mode="HTML",
            )
            cleanup_ids.append(warning.message_id)
            context.user_data[ADDNUM_STATE_KEY] = addnum_setup
            return

        await process_addnum_document(
            update,
            context,
            message.document,
            title=addnum_setup.get("title", ""),
        )
        context.user_data.pop(ADDNUM_STATE_KEY, None)
        await safe_delete_message(message)
        return


async def cmd_addnum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("âŒ Admin only.")
        return

    reply = update.message.reply_to_message
    if reply and reply.document:
        await process_addnum_document(update, context, reply.document)
        return

    await safe_delete_message(update.message)
    context.user_data[ADDNUM_STATE_KEY] = {
        "step": "title",
        "cleanup_message_ids": [],
    }
    prompt = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "📥 <b>Add Number Manual</b>\n\n"
            "Kirim <b>ctax / title</b> dulu.\n"
            "Setelah itu kirim daftar nomor sebagai teks panjang atau file `.txt/.csv/.xlsx`.\n"
            "Contoh: <code>CTAX CAMEROON</code>"
        ),
        parse_mode="HTML",
    )
    context.user_data[ADDNUM_STATE_KEY]["cleanup_message_ids"].append(prompt.message_id)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("âŒ Admin only.")
        return

    otp_text = "Kode WhatsApp: 313-561 Jangan bagikan kode ini dengan orang lain"
    if context.args:
        otp_text = " ".join(context.args).strip() or otp_text

    await send_to_otp_channel(
        context,
        "WhatsApp",
        "TEST",
        "233268500105",
        otp_text,
    )
    await update.message.reply_text(
        "✅ Test OTP berhasil dikirim ke channel.",
        parse_mode="HTML",
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    try:
        target_date = parse_test_date_input(context.args[0] if context.args else "")
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}", parse_mode="HTML")
        return

    status = await update.message.reply_text(
        f"🔎 <b>Testing OTP real</b>\n\n"
        f"• From: <code>{target_date}</code>\n"
        f"• To: <code>{target_date}</code>\n"
        f"• Source: <code>IvaSms received</code>",
        parse_mode="HTML",
    )

    if not iva_client.logged_in:
        await iva_client.ensure_logged_in(startup_mode=True)

    result = await iva_client.check_otps(from_date=target_date, to_date=target_date)
    if not result:
        await status.edit_text(
            "❌ Gagal mengambil data OTP dari IvaSms.\nPastikan session login masih aktif.",
            parse_mode="HTML",
        )
        return

    otp_messages = await iva_client.get_all_otp_messages(
        result.get("sms_details", []),
        from_date=target_date,
        to_date=target_date,
        limit=200,
    )

    if not otp_messages:
        await status.edit_text(
            f"ℹ️ Tidak ada OTP ditemukan untuk tanggal <code>{target_date}</code>.",
            parse_mode="HTML",
        )
        return

    sent = 0
    seen = set()
    for item in otp_messages:
        phone = str(item.get("phone_number", "") or "").strip()
        otp_msg = str(item.get("otp_message", "") or "").strip()
        if not phone or not otp_msg:
            continue
        dedupe_key = (phone, otp_msg)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        await send_to_otp_channel(
            context,
            str(item.get("sender", "") or "WhatsApp"),
            str(item.get("range", "") or "UNKNOWN"),
            phone,
            otp_msg,
        )
        sent += 1

    await status.edit_text(
        "\n".join(
            [
                "✅ <b>Test OTP real selesai</b>",
                "",
                f"• Tanggal: <code>{target_date}</code>",
                f"• Range terbaca: <b>{len(result.get('sms_details', []))}</b>",
                f"• OTP ditemukan: <b>{len(otp_messages)}</b>",
                f"• OTP terkirim ke channel: <b>{sent}</b>",
            ]
        ),
        parse_mode="HTML",
    )


async def render_profile(query_or_message, user):
    db.get_or_create_user(user.id, user.username or "", user.first_name or "")
    profile = db.get_user_profile(user.id)
    text = build_profile_text(profile, user)
    if hasattr(query_or_message, "edit_message_text"):
        await safe_edit(query_or_message, text, reply_markup=build_profile_keyboard())
    else:
        await query_or_message.reply_text(text, parse_mode="HTML", reply_markup=build_profile_keyboard())


async def start_withdraw_setup(query, user, context: ContextTypes.DEFAULT_TYPE, method: str):
    db.get_user_profile(user.id) or db.get_or_create_user(user.id, user.username or "", user.first_name or "")
    db.update_profile_meta(
        user.id,
        withdraw_method=method,
        withdraw_name="",
        withdraw_account="",
    )
    context.user_data["withdraw_setup"] = {
        "step": "name",
        "method": method,
    }
    await safe_edit(
        query,
        f"💸 <b>Set Withdraw {html_module.escape(method)}</b>\n\nKirim <b>nama pemilik</b> untuk metode {html_module.escape(method)}.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Profile", callback_data="profile_view")]]),
    )


async def submit_withdraw_request(user, context: ContextTypes.DEFAULT_TYPE, withdraw_type: str):
    profile = db.get_user_profile(user.id)
    if not profile:
        return False, "Profile tidak ditemukan."

    method = (profile.get("withdraw_method") or "").lower()
    owner_name = (profile.get("withdraw_name") or "").strip()
    account = (profile.get("withdraw_account") or "").strip()
    pending_id = profile.get("withdraw_pending_id")
    if not method or not owner_name or not account:
        return False, "Set withdraw dulu di Profile."
    if pending_id:
        return False, f"Masih ada withdraw pending: {pending_id}"

    if withdraw_type == "usd":
        balance = float(profile.get("balance_usd", 0) or 0)
        if balance < WITHDRAW_MIN_USD:
            return False, f"Minimal withdraw USD adalah {format_usd(WITHDRAW_MIN_USD)}."
        amount = balance
        destination_label = f"{method.upper()} | {owner_name} | {account}"
    else:
        balance = int(profile.get("balance_dana", 0) or 0)
        if balance < WITHDRAW_MIN_DANA:
            return False, f"Minimal withdraw DANA adalah {format_idr(WITHDRAW_MIN_DANA)}."
        amount = balance
        destination_label = f"{method.upper()} | {owner_name} | {account}"

    request = db.create_withdraw_request(user.id, withdraw_type, amount, destination_label)
    admin_text = "\n".join([
        "💸 <b>Request Withdraw Baru</b>",
        "",
        f"• Request ID: <code>{request['request_id']}</code>",
        f"• User: <b>{html_module.escape(user.first_name or user.username or str(user.id))}</b>",
        f"• User ID: <code>{user.id}</code>",
        f"• Type: <b>{html_module.escape(withdraw_type.upper())}</b>",
        f"• Method: <b>{html_module.escape(method.upper())}</b>",
        f"• Nama: <code>{html_module.escape(owner_name)}</code>",
        f"• Tujuan: <code>{html_module.escape(account)}</code>",
        f"• Amount: <b>{format_usd(amount) if withdraw_type == 'usd' else format_idr(amount)}</b>",
    ])
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ ACC", callback_data=f"wd_approve_{request['request_id']}"),
            InlineKeyboardButton("❌ Tolak", callback_data=f"wd_reject_{request['request_id']}"),
        ]
    ])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, admin_text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as exc:
            logger.error(f"Failed to send withdraw request to admin {admin_id}: {exc}")
    return True, request["request_id"]


# ══════════════════════════════════════════════════════════
# CALLBACKS
# ══════════════════════════════════════════════════════════

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer_callback(query)
    user = query.from_user
    db.get_or_create_user(user.id, user.username or "", user.first_name or "")
    data = query.data

    try:
        if data == "get_number":
            await show_services(query)
        elif data == "profile_view":
            await render_profile(query, user)
        elif data == "help_user":
            await safe_edit(
                query,
                "\n".join([
                    "?? <b>Help User</b>",
                    "",
                    "• /start - buka menu utama",
                    "• /stock - lihat stok negara",
                    "• /profile - lihat profil dan referral",
                    "• /refstats - statistik referral kamu",
                    "• /topref - leaderboard referral",
                    "• /bonusref - ringkasan bonus referral",
                    "• /helpuser - daftar command user",
                ]),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]),
            )
        elif data == "profile_setwithdraw":
            await safe_edit(
                query,
                "💸 <b>Pilih metode withdraw</b>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 DANA", callback_data="setwd_dana"),
                     InlineKeyboardButton("🟡 Binance", callback_data="setwd_binance")],
                    [InlineKeyboardButton("🔙 Profile", callback_data="profile_view")],
                ]),
            )
        elif data == "setwd_dana":
            await start_withdraw_setup(query, user, context, "DANA")
        elif data == "setwd_binance":
            await start_withdraw_setup(query, user, context, "Binance")
        elif data == "withdraw_usd":
            ok, result = await submit_withdraw_request(user, context, "usd")
            if ok:
                await safe_edit(
                    query,
                    f"✅ <b>Withdraw USD dibuat</b>\n\nRequest ID: <code>{result}</code>\nMenunggu ACC admin.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Profile", callback_data="profile_view")]]),
                )
            else:
                await safe_edit(
                    query,
                    f"❌ <b>Withdraw USD gagal</b>\n\n{html_module.escape(result)}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Profile", callback_data="profile_view")]]),
                )
        elif data == "withdraw_dana":
            ok, result = await submit_withdraw_request(user, context, "dana")
            if ok:
                await safe_edit(
                    query,
                    f"✅ <b>Withdraw DANA dibuat</b>\n\nRequest ID: <code>{result}</code>\nMenunggu ACC admin.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Profile", callback_data="profile_view")]]),
                )
            else:
                await safe_edit(
                    query,
                    f"❌ <b>Withdraw DANA gagal</b>\n\n{html_module.escape(result)}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Profile", callback_data="profile_view")]]),
                )
        elif data.startswith("wd_approve_"):
            if not is_admin(user.id):
                await safe_answer_callback(query, "Admin only", show_alert=True)
                return
            request_id = data[len("wd_approve_"):]
            request = db.get_withdraw_request(request_id)
            if not request:
                await safe_answer_callback(query, "Request tidak ditemukan", show_alert=True)
                return
            if request.get("status") != "pending":
                await safe_answer_callback(query, "Request sudah diproses", show_alert=True)
                return
            request = db.update_withdraw_request(
                request_id,
                status="approved",
                decided_by=user.id,
                decided_at=datetime.now().isoformat(),
            )
            db.clear_withdraw_pending(request["telegram_id"], request_id)
            profile = db.get_user_profile(request["telegram_id"])
            if request["method"] == "usd":
                db.update_profile_meta(request["telegram_id"], balance_usd=0.0)
                amount_text = format_usd(float(request["amount"]))
            else:
                db.update_profile_meta(request["telegram_id"], balance_dana=0)
                amount_text = format_idr(int(request["amount"]))
            await context.bot.send_message(
                request["telegram_id"],
                f"✅ <b>Withdraw di-ACC admin</b>\n\n• Request ID: <code>{request_id}</code>\n• Amount: <b>{amount_text}</b>",
                parse_mode="HTML",
            )
            await context.bot.send_message(
                OTP_CHANNEL_ID,
                "\n".join([
                    "✅ <b>Withdraw Approved</b>",
                    f"• Request ID: <code>{request_id}</code>",
                    f"• User ID: <code>{request['telegram_id']}</code>",
                    f"• Type: <b>{html_module.escape(request['method'].upper())}</b>",
                    f"• Amount: <b>{amount_text}</b>",
                    f"• Rekening/Wallet: <code>{html_module.escape(mask_account_number(profile.get('withdraw_account') or ''))}</code>",
                ]),
                parse_mode="HTML",
            )
            await safe_edit(query, f"✅ Withdraw <code>{request_id}</code> di-ACC.", reply_markup=None)
        elif data.startswith("wd_reject_"):
            if not is_admin(user.id):
                await safe_answer_callback(query, "Admin only", show_alert=True)
                return
            request_id = data[len("wd_reject_"):]
            request = db.get_withdraw_request(request_id)
            if not request:
                await safe_answer_callback(query, "Request tidak ditemukan", show_alert=True)
                return
            if request.get("status") != "pending":
                await safe_answer_callback(query, "Request sudah diproses", show_alert=True)
                return
            db.update_withdraw_request(
                request_id,
                status="rejected",
                decided_by=user.id,
                decided_at=datetime.now().isoformat(),
            )
            db.clear_withdraw_pending(request["telegram_id"], request_id)
            await context.bot.send_message(
                request["telegram_id"],
                f"❌ <b>Withdraw ditolak admin</b>\n\n• Request ID: <code>{request_id}</code>",
                parse_mode="HTML",
            )
            await safe_edit(query, f"❌ Withdraw <code>{request_id}</code> ditolak.", reply_markup=None)
        elif data == "service_whatsapp":
            await show_countries(query)
        elif data.startswith("country_"):
            country = data[len("country_"):]
            await assign_and_show(query, user, country)
        elif data.startswith("change_"):
            country = data[len("change_"):]
            await change_number(query, user, country)
        elif data.startswith("wscheck_"):
            number_id = int(data[len("wscheck_"):])
            await ws_check(query, user, number_id, context)
        elif data == "change_country":
            await show_countries(query)
        elif data == "back_services":
            await show_services(query)
        elif data == "main_menu":
            await show_main_menu(query)
        elif data == "leaderboard":
            await show_leaderboard(query)
        elif data == "delnum_scope_all":
            if not is_admin(user.id):
                await safe_answer_callback(query, "Admin only", show_alert=True)
                return
            total = db.get_total_stock()
            assigned = db.get_assigned_count()
            await safe_edit(
                query,
                "\n".join([
                    "⚠️ <b>Konfirmasi Hapus Semua Stok</b>",
                    "",
                    f"• Semua nomor <b>available</b> akan dihapus: <b>{total}</b>",
                    f"• Nomor assigned tetap aman: <b>{assigned}</b>",
                    "",
                    "Lanjutkan hapus semua stok available?",
                ]),
                reply_markup=build_delnum_confirm_keyboard("all", is_all=True),
            )
        elif data == "delnum_scope_country":
            if not is_admin(user.id):
                await safe_answer_callback(query, "Admin only", show_alert=True)
                return
            stock = db.get_stock_by_country()
            if not stock:
                await safe_edit(query, "ℹ️ Tidak ada stok available untuk dihapus.")
                return
            await safe_edit(
                query,
                "🌍 <b>Pilih negara yang ingin dihapus stoknya</b>",
                reply_markup=build_delnum_country_keyboard(stock),
            )
        elif data == "delnum_back_scope":
            if not is_admin(user.id):
                await safe_answer_callback(query, "Admin only", show_alert=True)
                return
            total = db.get_total_stock()
            assigned = db.get_assigned_count()
            await safe_edit(
                query,
                "\n".join([
                    "🗑 <b>Hapus Stok Nomor</b>",
                    "",
                    f"• Stok available saat ini: <b>{total}</b>",
                    f"• Assigned aktif tidak akan dihapus: <b>{assigned}</b>",
                    "",
                    "Pilih aksi yang ingin dijalankan:",
                ]),
                reply_markup=build_delnum_scope_keyboard(),
            )
        elif data.startswith("delnum_country_"):
            if not is_admin(user.id):
                await safe_answer_callback(query, "Admin only", show_alert=True)
                return
            country = data[len("delnum_country_"):]
            stock = db.get_stock_by_country()
            count = int(stock.get(country, 0) or 0)
            if count <= 0:
                await safe_answer_callback(query, "Stok negara ini sudah kosong.", show_alert=True)
                return
            await safe_edit(
                query,
                "\n".join([
                    f"⚠️ <b>Konfirmasi Hapus Stok {html_module.escape(country)}</b>",
                    "",
                    f"• Nomor available yang akan dihapus: <b>{count}</b>",
                    "• Nomor assigned tidak akan dihapus",
                    "",
                    "Lanjutkan?",
                ]),
                reply_markup=build_delnum_confirm_keyboard(country, is_all=False),
            )
        elif data == "delnum_confirm_all":
            if not is_admin(user.id):
                await safe_answer_callback(query, "Admin only", show_alert=True)
                return
            deleted = db.delete_available_numbers()
            assigned = db.get_assigned_count()
            await safe_edit(
                query,
                "\n".join([
                    "✅ <b>Semua stok available berhasil dihapus</b>",
                    "",
                    f"• Total dihapus: <b>{deleted}</b>",
                    f"• Assigned yang tetap aman: <b>{assigned}</b>",
                ]),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑 Buka Lagi", callback_data="delnum_back_scope")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
                ]),
            )
        elif data.startswith("delnum_confirm_country_"):
            if not is_admin(user.id):
                await safe_answer_callback(query, "Admin only", show_alert=True)
                return
            country = data[len("delnum_confirm_country_"):]
            deleted = db.delete_available_numbers(country=country)
            total = db.get_total_stock()
            await safe_edit(
                query,
                "\n".join([
                    f"✅ <b>Stok {html_module.escape(country)} berhasil dihapus</b>",
                    "",
                    f"• Total dihapus: <b>{deleted}</b>",
                    f"• Sisa stok available semua negara: <b>{total}</b>",
                ]),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌍 Hapus Negara Lain", callback_data="delnum_scope_country")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
                ]),
            )
    except Exception as e:
        logger.error(f"Callback error [{data}]: {e}")
        try:
            await query.edit_message_text(f"❌ Error: {e}")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════
# FLOW
# ══════════════════════════════════════════════════════════

async def show_main_menu(query):
    stock = db.get_total_stock()
    keyboard = [
        [InlineKeyboardButton("📱 Get Number", callback_data="get_number")],
        [InlineKeyboardButton("👤 Profile", callback_data="profile_view"),
         InlineKeyboardButton("?? Help User", callback_data="help_user")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")],
    ]
    await safe_edit(query,
        f"📡 <b>IvaSms Number Bot</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Stock: <b>{stock}</b>\n\n"
        f"Select a button from below:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_services(query):
    stock = db.get_total_stock()
    keyboard = [
        [InlineKeyboardButton(f"💬 WhatsApp ({stock})", callback_data="service_whatsapp")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ]
    await safe_edit(query, "📲 <b>Please select a service:</b>",
                    reply_markup=InlineKeyboardMarkup(keyboard))


async def show_countries(query):
    stock = db.get_stock_by_country()
    keyboard = []
    for country, count in sorted(stock.items()):
        keyboard.append([InlineKeyboardButton(
            f"💎 {get_flag(country)} {country} ({count})",
            callback_data=f"country_{country}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_services")])
    await safe_edit(query, "🌍 <b>Select Country for WhatsApp</b> 💬",
                    reply_markup=InlineKeyboardMarkup(keyboard))


async def assign_and_show(query, user, country):
    db.get_or_create_user(user.id, user.username or "", user.first_name or "")
    stock = db.get_stock_by_country()
    if stock.get(country, 0) <= 0:
        await safe_edit(query,
            f"❌ <b>No numbers available.</b>\n\n"
            f"{get_flag(country)} {country} — Stock: 0",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌍 Change Country", callback_data="change_country")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ])
        )
        return

    # Release existing assigned number if any
    current = db.get_user_active_number(user.id)
    if current:
        db.release_number(current['id'])

    number = db.assign_number(user.id, country)
    if not number:
        await safe_edit(query, "❌ Error assigning. Try again.")
        return

    db.increment_user_numbers(user.id)
    await show_number_card(query, number, country)


async def change_number(query, user, country):
    db.get_or_create_user(user.id, user.username or "", user.first_name or "")
    number = db.change_number(user.id, country)
    if not number:
        await safe_edit(query,
            f"❌ <b>No more numbers available.</b>\n"
            f"{get_flag(country)} {country} — Stock habis!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌍 Change Country", callback_data="change_country")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ])
        )
        return
    db.increment_user_numbers(user.id)
    await show_number_card(query, number, country)


async def show_number_card(query, number, country):
    flag = get_flag(country)
    phone = number['phone_number']
    display = f"+{phone}" if not phone.startswith("+") else phone
    nid = number['id']
    now = datetime.now().strftime("%H:%M:%S")

    keyboard = [
        [InlineKeyboardButton("📲 Change Number", callback_data=f"change_{country}")],
        [InlineKeyboardButton("🔍 WS Check", callback_data=f"wscheck_{nid}")],
        [InlineKeyboardButton("🌍 Change Country", callback_data="change_country")],
    ]
    msg = (
        f"✅ <b>New Number For You</b> 📱\n\n"
        f"  Service  :  WhatsApp\n"
        f"  Number(s) :\n"
        f"  <code>{display}</code>\n\n"
        f"  Country  :  {flag} {country} 💎\n"
        f"  OTP Status  :  ⏳ Waiting for OTP\n"
        f"  Monitor  :  Auto check setiap {OTP_POLL_SECONDS} detik\n"
        f"  ⏰ {now}"
    )
    await safe_edit(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))


def extract_otp_code(text: str) -> str:
    # Match 6 consecutive digits
    match = re.search(r'\b(\d{6})\b', text)
    if match:
        return match.group(1)
    
    # Match 3-3 digits separated by hyphen
    match = re.search(r'\b(\d{3}-\d{3})\b', text)
    if match:
        return match.group(1).replace('-', '')

    # Match 5 consecutive digits
    match = re.search(r'\b(\d{5})\b', text)
    if match:
        return match.group(1)

    # Match first 4 to 8 digit number
    match = re.search(r'\b(\d{4,8})\b', text)
    if match:
        return match.group(1)

    return text


def mask_phone_number(phone: str) -> str:
    clean_phone = "".join(c for c in str(phone) if c.isdigit())
    formatted_phone = f"+{clean_phone}" if not str(phone).startswith("+") else str(phone)
    if len(clean_phone) >= 9:
        return f"{formatted_phone[:6]}••••{formatted_phone[-3:]}"
    if len(formatted_phone) >= 5:
        return f"{formatted_phone[:3]}••••{formatted_phone[-1:]}"
    return formatted_phone


async def send_otp_to_user(context: ContextTypes.DEFAULT_TYPE, number: dict, otp_msg: str):
    phone = number["phone_number"]
    display = f"+{phone}" if not str(phone).startswith("+") else str(phone)
    country = get_country_display(number["country"])
    flag = get_flag(country)
    otp_code = extract_otp_code(otp_msg)

    msg = (
        f"📩 <b>OTP Received!</b>\n\n"
        f"  📱 <code>{html_module.escape(display)}</code>\n"
        f"  {flag} {country}\n\n"
        f"  ⭐ <b>Code:</b> <code>{html_module.escape(otp_code)}</code>\n"
        f"  💬 <b>Message:</b>\n"
        f"  <code>{html_module.escape(otp_msg)}</code>"
    )
    await context.bot.send_message(
        chat_id=number["assigned_to"],
        text=msg,
        parse_mode="HTML",
    )


async def send_to_otp_channel(context: ContextTypes.DEFAULT_TYPE, service: str, country: str, phone: str, otp_msg: str):
    """Send the successful activation message to the configured Telegram channel."""
    try:
        otp_code = extract_otp_code(otp_msg)
        masked_phone = mask_phone_number(phone)
        flag = get_flag(country)
        now_str = datetime.now().strftime("%H:%M")

        msg = (
            f"<b>Bot</b> [Admin]\n"
            f"#VE {flag} {service} <code>{masked_phone}</code> {now_str}\n"
            f"---------------------\n"
            f"<code>{html_module.escape(otp_code)}</code> (Tap to copy)\n"
            f"---------------------"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Main Channel", url=OTP_CHANNEL_URL),
                InlineKeyboardButton("Get Number", url=BOT_PUBLIC_URL),
            ]
        ])

        await context.bot.send_message(
            chat_id=OTP_CHANNEL_ID,
            text=msg,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.info(f"Broadcasted successful OTP for {masked_phone} to channel {OTP_CHANNEL_ID}")
    except Exception as e:
        logger.error(f"Failed to broadcast to channel: {e}")


async def send_to_otp_channel(context: ContextTypes.DEFAULT_TYPE, service: str, country: str, phone: str, otp_msg: str):
    """Send the successful activation message to the configured Telegram channel."""
    try:
        otp_code = extract_otp_code(otp_msg)
        masked_phone = mask_phone_number(phone)
        flag = get_flag(country)
        hashtag = get_country_hashtag(country)
        country_display = get_country_display(country)
        now_str = datetime.now().strftime("%H:%M")

        msg = (
            f"<b>Bot</b> [Admin]\n"
            f"{hashtag} {flag} {html_module.escape(country_display)} <code>{masked_phone}</code> {now_str}\n"
            f"---------------------\n"
            f"<code>{html_module.escape(otp_code)}</code> (Tap to copy)\n"
            f"---------------------"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Main Channel", url=OTP_CHANNEL_URL),
                InlineKeyboardButton("Get Number", url=BOT_PUBLIC_URL),
            ]
        ])

        await context.bot.send_message(
            chat_id=OTP_CHANNEL_ID,
            text=msg,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.info("Broadcasted successful OTP for %s to channel %s", masked_phone, OTP_CHANNEL_ID)
    except Exception as e:
        logger.error(f"Failed to broadcast to channel: {e}")


async def process_number_otp(context: ContextTypes.DEFAULT_TYPE, number: dict):
    """Check one assigned number, persist OTP, notify owner, and broadcast masked copy to channel."""
    if number["status"] == "used" and number.get("otp_message"):
        return number["otp_message"]

    otp_msg = await iva_client.get_otp_for_number(number["phone_number"])
    if not otp_msg:
        return None
    return await persist_and_send_otp(context, number, otp_msg)


async def persist_and_send_otp(context: ContextTypes.DEFAULT_TYPE, number: dict, otp_msg: str):
    """Persist a resolved OTP and notify the owner/channel once."""
    if not otp_msg:
        return None
    db.mark_number_used(number["id"], otp_msg)

    try:
        await send_otp_to_user(context, number, otp_msg)
    except Exception as exc:
        logger.error(f"❌ Failed to send OTP to user {number['assigned_to']}: {exc}")

    await send_to_otp_channel(
        context,
        "WhatsApp",
        number["country"],
        number["phone_number"],
        otp_msg,
    )
    return otp_msg


def build_phone_otp_lookup(messages):
    """Collapse scraped OTP rows into a phone -> latest message mapping."""
    lookup = {}
    for item in messages or []:
        phone = str(item.get("phone_number", "") or "").strip()
        message = str(item.get("otp_message", "") or "").strip()
        if phone and message:
            lookup[phone] = message
    return lookup


async def restore_ivasms_session_background():
    """Restore the IvaSms session without blocking the scheduler job."""
    global session_failure_count
    global session_lost_notified

    try:
        restore_ok = False

        if iva_client.has_cookies_file():
            logger.info("OTP poller restoring background IvaSms session from cookies")
            restore_ok = await iva_client.login_with_cookies(startup_mode=True)
        else:
            logger.info("OTP poll skipped: no cookie session available for background restore")
            return False

        if restore_ok and iva_client.logged_in:
            session_failure_count = 0
            return True

        session_failure_count += 1
        return False
    except Exception as exc:
        logger.error(f"Background IvaSms session restore failed: {exc}")
        session_failure_count += 1
        return False


async def poll_pending_otps_job(context: ContextTypes.DEFAULT_TYPE):
    """Background job: poll pending assigned numbers and deliver OTPs automatically."""
    global session_lost_notified
    global session_failure_count
    global manual_login_in_progress
    global last_restore_attempt_at
    global last_session_skip_log_at
    global last_poll_status_log_at
    global last_poll_status_count
    global next_otp_poll_not_before_at
    global session_restore_task

    loop_time = asyncio.get_running_loop().time()

    def should_log_skip():
        return loop_time - last_session_skip_log_at >= 30

    if manual_login_in_progress:
        if should_log_skip():
            logger.info("OTP poll skipped: manual /setlogin sedang berjalan")
            last_session_skip_log_at = loop_time
        return

    pending_numbers = db.get_pending_assigned_numbers()
    if not pending_numbers and not iva_client.logged_in:
        return

    if not iva_client.logged_in:
        since_last_attempt = loop_time - last_restore_attempt_at

        if session_restore_task and not session_restore_task.done():
            if should_log_skip():
                logger.info("OTP poll skipped: background session restore still running")
                last_session_skip_log_at = loop_time
            return

        if session_restore_task and session_restore_task.done():
            try:
                restore_ok = session_restore_task.result()
            except Exception:
                restore_ok = False
            session_restore_task = None

            if restore_ok and session_lost_notified and iva_client.logged_in:
                await notify_admins(
                    context,
                    "<b>Info</b>\nSession IvaSms berhasil dipulihkan. Monitoring OTP kembali aktif.",
                )
                session_lost_notified = False
            elif not restore_ok and session_failure_count >= 5 and not session_lost_notified:
                await notify_admins(
                    context,
                    "<b>Alert</b>\nSession IvaSms terputus. Bot mencoba login mandiri dari credential tersimpan.",
                )
                session_lost_notified = True

        if not iva_client.has_cookies_file():
            if not iva_client.has_saved_credentials():
                if should_log_skip():
                    logger.info("OTP poll skipped: no cookies and no stored credentials")
                    last_session_skip_log_at = loop_time
                return

            if since_last_attempt < IVASMS_SESSION_RESTORE_COOLDOWN:
                if should_log_skip():
                    logger.info(
                        "OTP poll skipped: waiting %.0fs before next autonomous login attempt",
                        IVASMS_SESSION_RESTORE_COOLDOWN - since_last_attempt,
                    )
                    last_session_skip_log_at = loop_time
                return

            creds = iva_client.load_credentials()
            if not creds:
                logger.info("OTP poll skipped: stored credentials unavailable")
                return

            last_restore_attempt_at = loop_time
            session_restore_task = asyncio.create_task(restore_ivasms_session_background())
            logger.info("OTP poll skipped: autonomous session restore started in background")
            return
        else:
            if since_last_attempt < IVASMS_SESSION_RESTORE_COOLDOWN:
                if should_log_skip():
                    logger.info(
                        "OTP poll skipped: waiting %.0fs before next session restore attempt",
                        IVASMS_SESSION_RESTORE_COOLDOWN - since_last_attempt,
                    )
                    last_session_skip_log_at = loop_time
                return

            last_restore_attempt_at = loop_time
            session_restore_task = asyncio.create_task(restore_ivasms_session_background())
            logger.info("OTP poll skipped: cookie session restore started in background")
            return

    if iva_client.logged_in:
        session_failure_count = 0
        last_restore_attempt_at = asyncio.get_running_loop().time()

    if session_lost_notified and iva_client.logged_in:
        await notify_admins(
            context,
            "<b>Info</b>\nSession IvaSms berhasil dipulihkan. Monitoring OTP kembali aktif.",
        )
        session_lost_notified = False

    if not pending_numbers:
        return

    if loop_time < next_otp_poll_not_before_at:
        return

    if (
        len(pending_numbers) != last_poll_status_count
        or loop_time - last_poll_status_log_at >= 30
    ):
        logger.info("Polling OTPs for %s assigned number(s)", len(pending_numbers))
        last_poll_status_log_at = loop_time
        last_poll_status_count = len(pending_numbers)

    from_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    to_date = datetime.now().strftime("%Y-%m-%d")
    otp_lookup = {}

    try:
        summary = await iva_client.check_otps(
            from_date=from_date,
            to_date=to_date,
            allow_browser_refresh=False,
        )
        if not summary:
            next_otp_poll_not_before_at = loop_time + 5
            return

        summary_source = str(summary.get("source", "") or "")
        summary_count = int(summary.get("count_sms", 0) or 0)

        if summary_source == "browser":
            next_otp_poll_not_before_at = loop_time + 3

        if summary_count > 0:
            messages = await iva_client.get_all_otp_messages(
                summary.get("sms_details", []),
                from_date=from_date,
                to_date=to_date,
                limit=max(300, len(pending_numbers) * 50),
                allow_browser_refresh=False,
            )
            otp_lookup = build_phone_otp_lookup(messages)
    except Exception as exc:
        logger.error(f"? Background OTP batch poll failed: {exc}")
        next_otp_poll_not_before_at = loop_time + 5
        return

    if not otp_lookup:
        return

    for number in pending_numbers:
        otp_msg = otp_lookup.get(str(number["phone_number"]))
        if not otp_msg:
            continue
        try:
            await persist_and_send_otp(context, number, otp_msg)
        except Exception as exc:
            logger.error(f"? Background OTP delivery failed for number {number['id']}: {exc}")


async def ws_check(query, user, number_id, context: ContextTypes.DEFAULT_TYPE):
    number = db.check_number_otp(number_id)
    if not number:
        await safe_edit(query, "❌ Number tidak ditemukan.")
        return

    phone = number['phone_number']
    display = f"+{phone}" if not phone.startswith("+") else phone
    country = number['country']
    flag = get_flag(country)
    now = datetime.now().strftime("%H:%M:%S")

    # Sudah punya OTP?
    if number['status'] == 'used' and number.get('otp_message'):
        await safe_edit(query,
            f"📩 <b>OTP Received!</b> ✅\n\n"
            f"  📱 <code>{display}</code>\n"
            f"  {flag} {country}\n\n"
            f"  💬 <b>OTP:</b>\n"
            f"  <code>{html_module.escape(number['otp_message'])}</code>\n\n"
            f"  ⚠️ Nomor sudah digunakan.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📱 Get New Number", callback_data="get_number")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ])
        )
        return

    # Cek dari IvaSms (async call langsung)
    if iva_client.logged_in:
        otp_msg = await process_number_otp(context, number)
        if otp_msg:
            await safe_edit(query,
                f"📩 <b>OTP Received!</b> ✅\n\n"
                f"  📱 <code>{display}</code>\n"
                f"  {flag} {country}\n\n"
                f"  💬 <b>OTP:</b>\n"
                f"  <code>{html_module.escape(otp_msg)}</code>\n\n"
                f"  ⚠️ Nomor sudah digunakan.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📱 Get New Number", callback_data="get_number")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
                ])
            )
            return

    # Belum ada OTP
    await safe_edit(query,
        f"⏳ <b>No OTP yet</b>\n\n"
        f"  📱 <code>{display}</code>\n"
        f"  {flag} {country}\n"
        f"  Status: ⏳ Waiting for OTP\n\n"
        f"  🔄 Checked: {now}\n"
        f"  <i>Tap 🔍 WS Check lagi untuk refresh.</i>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📲 Change Number", callback_data=f"change_{country}")],
            [InlineKeyboardButton("🔍 WS Check", callback_data=f"wscheck_{number_id}")],
            [InlineKeyboardButton("🌍 Change Country", callback_data="change_country")],
        ])
    )


async def show_leaderboard(query):
    top = db.get_leaderboard(10)
    medals = ["🥇", "??", "🥉"]
    lines = ["🏆 <b>Leaderboard</b>\n", "━" * 20]
    for i, u in enumerate(top):
        m = medals[i] if i < 3 else f"{i+1}."
        name = html_module.escape(u.get('first_name') or u.get('username') or str(u['telegram_id']))
        lines.append(f"  {m} <b>{name}</b> — {u['total_numbers']} numbers")
    if not top:
        lines.append("  <i>Belum ada data.</i>")
    await safe_edit(query, "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
                    ]))


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════

async def post_init(application):
    """Run after application starts."""
    init_stock(db)
    application.job_queue.run_repeating(
        poll_pending_otps_job,
        interval=OTP_POLL_SECONDS,
        first=10,
        name="otp-poller",
    )

    stock = db.get_stock_by_country()
    for country, count in sorted(stock.items()):
        logger.info(f"  {get_flag(country)} {country}: {count}")


async def handle_application_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    global polling_conflict_shutdown

    if isinstance(context.error, Conflict):
        if polling_conflict_shutdown:
            return
        polling_conflict_shutdown = True
        logger.error("Telegram polling conflict detected. Another bot instance is already running for this token. Stopping current instance.")
        try:
            if context.application.updater:
                await context.application.updater.stop()
        except Exception as exc:
            logger.error(f"Failed to stop updater after conflict: {exc}")
        try:
            await context.application.stop()
        except Exception as exc:
            logger.error(f"Failed to stop application after conflict: {exc}")
        return

    if context.error:
        logger.error(
            "Unhandled application error: %s",
            context.error,
            exc_info=(type(context.error), context.error, context.error.__traceback__),
        )
    else:
        logger.error("Unhandled application error without exception context.")


def main():
    asyncio.set_event_loop(asyncio.new_event_loop())
    stock = db.get_total_stock()
    banner_lines = [
        '',
        '    +-------------------------------------------+',
        '    |   IvaSms Number Bot                      |',
        f"    |   Stock: {stock} numbers".ljust(44) + '|',
        '    +-------------------------------------------+',
        '',
    ]
    print("\n".join(banner_lines))

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    if app.job_queue is None:
        raise RuntimeError(
            'JobQueue is required. Install dependency with `python-telegram-bot[job-queue]==21.6`.'
        )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("helpuser", cmd_helpuser))
    app.add_handler(CommandHandler("setlogin", cmd_setlogin))
    app.add_handler(CommandHandler("statuslogin", cmd_statuslogin))
    app.add_handler(CommandHandler("statussession", cmd_statuslogin))
    app.add_handler(CommandHandler("clearlogin", cmd_clearlogin))
    app.add_handler(CommandHandler("relogin", cmd_relogin))
    app.add_handler(CommandHandler("refreshsession", cmd_refreshsession))
    app.add_handler(CommandHandler("refreshsms", cmd_refreshsms))
    app.add_handler(CommandHandler("refstats", cmd_refstats))
    app.add_handler(CommandHandler("topref", cmd_topref))
    app.add_handler(CommandHandler("bonusref", cmd_bonusref))
    app.add_handler(CommandHandler("addnum", cmd_addnum))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("delnum", cmd_delnum))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document_input))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(handle_application_error)

    logger.info("🤖 Bot started!")
    if BOT_RUN_MODE == "webhook":
        logger.warning("BOT_RUN_MODE=webhook ignored; this bot is forced to polling mode")
    logger.info("Starting bot in polling mode")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
