from __future__ import annotations

import asyncio
import copy
import html
import io
import json
import logging
import os
import re
import threading
from datetime import datetime, time, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.enums import ButtonStyle, ChatMemberStatus
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import (
    ChatJoinRequest,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from tracker import OsintBot
from flask import Flask

# Keep literal placeholders such as ${NUMBER} in endpoint templates. Without
# this, python-dotenv expands them while reading .env and sends an empty value.
load_dotenv(interpolate=False)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


web_app = Flask(__name__)

@web_app.get("/")
def home():
    return "Telegram bot is running"


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing {name}. Add it to your .env file.")
    return value


osint_bot = OsintBot(
    mongo_uri=required("MONGO_URI"),
    database_name=os.getenv("MONGO_DATABASE", "osint_bot"),
)

def chat_id_from_env(name: str) -> int | str:
    value = required(name)
    return int(value) if value.lstrip("-").isdigit() else value


REQUIRED_CHANNELS = (
    (chat_id_from_env("CHANNEL_1_ID"), required("CHANNEL_1_URL"), "Channel 1"),
    (chat_id_from_env("CHANNEL_2_ID"), required("CHANNEL_2_URL"), "Channel 2"),
    (chat_id_from_env("CHANNEL_3_ID"), required("CHANNEL_3_URL"), "Channel 3"),
)
PENDING_JOIN_REQUESTS: dict[int | str, set[int]] = {}
PENDING_LOOKUPS: dict[int, str] = {}


def admin_user_ids() -> frozenset[int]:
    """Read one or more comma-separated Telegram administrator IDs."""
    raw_ids = os.getenv("ADMIN_USER_ID", "")
    ids: set[int] = set()
    for raw_id in raw_ids.split(","):
        raw_id = raw_id.strip()
        if not raw_id or raw_id == "0":
            continue
        if raw_id.isdigit():
            ids.add(int(raw_id))
        else:
            LOGGER.warning("Ignoring invalid ADMIN_USER_ID value")
    return frozenset(ids)


ADMIN_USER_IDS = admin_user_ids()
BOT_USERNAME = os.getenv("BOT_USERNAME", "OsintYutabot").lstrip("@")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").strip().lstrip("@")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
PREMIUM_STANDARD_LOOKUP_SETTINGS = frozenset(
    {"ADV_NUM_TO_INFO", "PAN_TO_INFO", "AADHAR_TO_FAMILY", "IFSC_TO_INFO"}
)

app = Client(
    "osint_bot",
    api_id=int(required("API_ID")),
    api_hash=required("API_HASH"),
    bot_token=required("BOT_TOKEN"),
)

WELCOME_TEXT = (
    "<b>🕵️‍♂️ Yuta OSINT Bot</b>\n\n"
    "I can help you find information using OSINT commands.\n\n"
    "<b>💳 Credit:</b> {credits}{premium_expiry}\n\n"
    "<b><i>FEATURES</i></b>\n"
    "• Number Info ── (all users)\n"
    "• Aadhaar Info ── (all users)\n"
    "• Advanced Number Info ── (premium only)\n"
    "• Aadhaar To Family ── (premium only)\n"
    "• Vehicle To Info ── (premium only)\n"
    "• Vehicle To Mobile No. ── (premium only)\n"
    "• IFSC To Bank Info ── (premium only)\n\n"
    "── ── ── ── ── ── ── ── ── ── ── ── ── ── ──\n\n"
    "<b>⚠️ Important:</b>\n"
    "• This bot is for educational purposes only\n"
    "• Do not use for illegal activities\n"
    "• Users are responsible for their actions\n"
    "• Unauthorized use is strictly prohibited\n"
    "• Lookup messages will be auto-deleted after 300 seconds"
)

PREMIUM_REQUIRED_TEXT = (
    "<b>🔒 Premium Required</b>\n\n"
    "<i>This lookup is available to premium users only.</i>\n\n"
    "<b>Please contact the admin to buy Premium for your account.</b>\n\n"
    f"<b>Admin Contact:</b> @{OWNER_USERNAME} "
)
PREMIUM_REQUIRED_ALERT = (
    f"Premium required. Contact @{OWNER_USERNAME} to buy Premium."
)

JOIN_TEXT = (
    "<b>OSINT BOT</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>Must Join all channels then You can use this bot</b>"
)


def join_keyboard(missing_channels: tuple[tuple[int | str, str, str], ...]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(name, url=url, style=ButtonStyle.PRIMARY)]
            for _, url, name in missing_channels
        ]
        + [[
            InlineKeyboardButton(
                "✅ Joined",
                callback_data="verify_subscription",
                style=ButtonStyle.PRIMARY,
            )
        ]]
    )


def start_menu_keyboard() -> InlineKeyboardMarkup:
    """Show lookup buttons only when their API endpoint is configured."""
    lookup_options = (
        ("NUM_TO_INFO", "📞 Number to Info", "lookup_number"),
        ("AADHAR_TO_INFO", "🪪 Aadhar to Info", "lookup_aadhar"),
        ("ADV_NUM_TO_INFO", "🔎 Adv. Number Info", "lookup_advanced_number"),
        ("PAN_TO_INFO", "🪪 PAN to Info", "lookup_pan"),
        ("AADHAR_TO_FAMILY", "👨‍👩‍👧 Aadhaar Family", "lookup_aadhar_family"),
        ("IFSC_TO_INFO", "🏦 IFSC to Info", "lookup_ifsc"),
        ("TG_TO_INFO", "👤 TG to Info", "lookup_tg_info"),
        ("VEHICLE_TO_INFO", "🚗 Vehicle to Info", "lookup_vehicle_info"),
        ("VEHICLE_TO_NUMBER", "📱 Vehicle to Number", "lookup_vehicle_number"),
    )
    buttons = [
        InlineKeyboardButton(label, callback_data=callback, style=ButtonStyle.PRIMARY)
        for setting, label, callback in lookup_options
        if os.getenv(setting, "").strip()
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    rows.append([
        InlineKeyboardButton(
            "🤝 Refer & Earn",
            callback_data="refer_earn",
            style=ButtonStyle.PRIMARY,
        )
    ])
    return InlineKeyboardMarkup(rows)


def referral_keyboard() -> InlineKeyboardMarkup:
    """Buttons for the referral view."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📋 Copy Referral Link",
                    callback_data="copy_referral_link",
                    style=ButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back", callback_data="refer_back", style=ButtonStyle.PRIMARY
                )
            ],
        ]
    )


async def missing_required_channels(user_id: int) -> tuple[tuple[int | str, str, str], ...]:
    """Return channels where the user is neither joined nor awaiting approval."""
    inactive_statuses = {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
    missing: list[tuple[int | str, str, str]] = []
    for channel in REQUIRED_CHANNELS:
        chat_id, _, _ = channel
        try:
            member = await app.get_chat_member(chat_id, user_id)
            if member.status not in inactive_statuses:
                PENDING_JOIN_REQUESTS.get(chat_id, set()).discard(user_id)
                await asyncio.to_thread(osint_bot.clear_pending_join_request, chat_id, user_id)
            else:
                if not await has_pending_join_request(chat_id, user_id):
                    missing.append(channel)
        except RPCError:
            # A non-member raises an RPC error, so check the pending-request list too.
            if not await has_pending_join_request(chat_id, user_id):
                missing.append(channel)
    return tuple(missing)


async def has_pending_join_request(chat_id: int | str, user_id: int) -> bool:
    """Check a pending request received by this bot, including before a restart."""
    return (
        user_id in PENDING_JOIN_REQUESTS.get(chat_id, set())
        or await asyncio.to_thread(osint_bot.has_pending_join_request, chat_id, user_id)
    )


@app.on_chat_join_request()
async def process_join_request(_: Client, request: ChatJoinRequest) -> None:
    """Record pending access only for invite links that require approval."""
    invite_link = request.invite_link
    if not invite_link or not invite_link.creates_join_request:
        return

    for chat_id, _, _ in REQUIRED_CHANNELS:
        if request.chat.id == chat_id:
            PENDING_JOIN_REQUESTS.setdefault(chat_id, set()).add(request.from_user.id)
            await asyncio.to_thread(osint_bot.record_pending_join_request, chat_id, request.from_user.id)
            LOGGER.info("Granted bot access for pending request from user %s in chat %s", request.from_user.id, chat_id)
            return


async def welcome_text(user_id: int) -> str:
    """Build the welcome message, including premium expiry when applicable."""
    credits = await asyncio.to_thread(
        osint_bot.get_credits, user_id
    )
    is_premium = await asyncio.to_thread(
        osint_bot.is_premium, user_id
    )
    premium_expiry = ""
    if is_premium:
        expiry = await asyncio.to_thread(osint_bot.premium_expiry, user_id)
        if expiry:
            premium_expiry = f"\n<b>📅 Premium expires:</b> {expiry:%d-%m-%Y}"
    return WELCOME_TEXT.format(
        credits="Unlimited" if is_premium else credits,
        premium_expiry=premium_expiry,
    )


async def send_start_message(message: Message, user_id: int | None = None) -> None:
    if user_id is None and not message.from_user:
        return
    target_user_id = user_id if user_id is not None else message.from_user.id
    await message.reply_text(
        await welcome_text(target_user_id),
        reply_markup=start_menu_keyboard(),
    )


async def referral_view_text(user_id: int) -> str:
    """Build the referral screen shown from the inline menu."""
    referrals, earned_credits = await asyncio.to_thread(osint_bot.referral_stats, user_id)
    progress = referrals % 2
    percent = progress * 50
    progress_bar = "■" * (progress * 5) + "▱" * (10 - progress * 5)
    remaining = 2 - progress
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    return (
        "<b>🤝 REFER &amp; EARN</b>\n\n"
        f"🔗 <b>Your Referral Link:</b>\n<code>{referral_link}</code>\n\n"
        "📊 <b>Your Stats:</b>\n"
        f"• Total Successful Referrals: <b>{referrals}</b>\n"
        f"• Credits Earned from Referrals: <b>{earned_credits}</b>\n\n"
        "🎯 <b>Progress to Next Credit:</b>\n"
        f"{progress_bar} {percent}%\n"
        f"┗ ➤ {remaining} more referral(s) needed\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>How it works:</b>\n"
        "• Share your referral link with friends.\n"
        "• When they start the bot using your link and join all required channels, they become your referral.\n"
        "• For every 2 successful referrals, you earn <b>1 FREE Credit</b>!\n"
        "• Referrals are counted only after channel membership is verified.\n\n"
        "🚀 <b>Start sharing now and earn free credits!</b>"
    )


async def remove_loading_message(message: Message) -> None:
    """Delete a temporary loading reply without hiding the final result."""
    try:
        await message.delete()
    except RPCError:
        LOGGER.debug("Could not delete loading message %s", message.id)


async def notify_referrer(referrer_id: int | None, referred_user: Any) -> None:
    """Tell the referrer when their referral completes channel verification."""
    if referrer_id is None:
        return
    name = html.escape(referred_user.first_name or referred_user.username or "A user")
    try:
        await app.send_message(
            referrer_id,
            f"{name} successfully joined the bot with your referral link.",
        )
    except RPCError:
        LOGGER.warning("Could not notify referrer %s", referrer_id)


Aadhaar_RE = re.compile(r"(?<!\d)(\d{4})[ -]?(\d{4})[ -]?(\d{4})(?!\d)")
SENSITIVE_KEYS = {"aadhar", "aadhaar", "aadharno", "aadhaarno", "uid", "uidai"}
REMOVED_KEYS = {"owner", "metadata"}


def sanitize(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            str(item): sanitize(child, str(item))
            for item, child in value.items()
            if re.sub(r"[^a-z0-9]", "", str(item).lower()) not in REMOVED_KEYS
        }

    if isinstance(value, list):
        return [sanitize(item, key) for item in value]

    return value


def format_result(data: Any) -> str:
    safe_data = sanitize(data)
    return json.dumps(safe_data, indent=2, ensure_ascii=True, default=str)


def result_file(data: Any, filename: str) -> io.BytesIO:
    document = io.BytesIO(format_result(data).encode("utf-8"))
    document.name = filename
    return document


def json_message(data: Any) -> str:
    return "```json\n" + format_result(data) + "\n```"


RESULT_ALIASES = {
    "name": {"name", "fullname", "personname"},
    "father": {"father", "fathername", "fname", "fathersname", "father_name"},
    "address": {"address", "fulladdress", "completeaddress"},
    "circle": {"circle", "telecomcircle", "operatorcircle"},
    "email": {"email", "emailaddress", "mail"},
    "aadhar": {"aadhar", "aadhaar", "aadharno", "aadhaarno", "uid", "uidai"},
    "alternate": {"alt", "alternate", "alternatenumber", "alternatenumbers"},
    "number": {
        "num", "number", "mobile", "mobileno", "mobilenumber", "phone", "phoneno", "phonenumber"
    },
}


def normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def scalar_value(value: Any) -> str | None:
    if isinstance(value, (str, int, float)) and str(value).strip():
        return str(value).strip()
    return None


def field_value(record: Any, field: str) -> str | None:
    aliases = {normalized_key(alias) for alias in RESULT_ALIASES[field]}
    if isinstance(record, dict):
        for key, value in record.items():
            if normalized_key(key) in aliases:
                found = scalar_value(value)
                if found:
                    return found
        for value in record.values():
            found = field_value(value, field)
            if found:
                return found
    elif isinstance(record, list):
        for value in record:
            found = field_value(value, field)
            if found:
                return found
    return None


def looks_like_result(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    keys = {normalized_key(key) for key in value}
    result_keys = {
        normalized_key(alias)
        for aliases in RESULT_ALIASES.values()
        for alias in aliases
    }
    return bool(keys & result_keys)


def result_records(value: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if looks_like_result(value):
            records.append(value)
        else:
            for child in value.values():
                records.extend(result_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(result_records(child))
    return records


def format_lookup_record(record: dict[str, Any], query: str, command: str) -> str:
    number = field_value(record, "number") or (query if command == "num" else "Not found")

    def text(field: str, fallback: str = "Not found") -> str:
        return html.escape(field_value(record, field) or fallback)

    lines = [
        f"👤 Name: {text('name')}",
        f"👨‍👦 Father: {text('father')}",
        f"📍 Address: {text('address')}",
        f"📡 Circle: {text('circle')}",
    ]
    email = field_value(record, "email")
    aadhar = field_value(record, "aadhar")
    alternate = field_value(record, "alternate")
    if email:
        lines.append(f"📧 Email: {html.escape(email)}")
    if aadhar:
        lines.append(f"🆔 Aadhar: {html.escape(aadhar)}")
    if alternate:
        lines.append(f"📞 Alternate: {html.escape(alternate)}")
    lines.append(f"📱 Number: {html.escape(number)}")
    return "\n".join(lines)


def lookup_message_chunks(query: str, data: Any, command: str) -> list[str]:
    records = result_records(data)
    if not records and isinstance(data, dict):
        records = [data]
    title = "📱 NUMBER SEARCH" if command == "num" else "🆔 AADHAR SEARCH"
    chunks: list[str] = []
    current: list[str] = []
    for index, record in enumerate(records, start=1):
        block = f"📌 Record #{index}\n{format_lookup_record(record, query, command)}"
        if current and (len(current) >= 5 or len("\n───────────────────────────────────\n".join(current + [block])) + len(title) + 20 > 4096):
            chunks.append(f"🔍 {title}\n═══════════════════════════════════\n\n" + "\n───────────────────────────────────\n".join(current))
            current = []
        current.append(block)
    if current:
        chunks.append(f"🔍 {title}\n═══════════════════════════════════\n\n" + "\n───────────────────────────────────\n".join(current))
    return chunks


def normalise_aadhaar(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    return digits if len(digits) == 12 else None


def normalise_phone_number(value: str) -> str | None:
    """Return the 10-digit Indian mobile number expected by the configured API."""
    digits = re.sub(r"[\s-]", "", value)
    if digits.startswith("+"):
        digits = digits[1:]
    if not digits.isdigit():
        return None
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if len(digits) == 10 else None


def normalise_vehicle_number(value: str) -> str | None:
    """Normalise a vehicle registration number such as GJ23CH0081."""
    vehicle = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return vehicle if re.fullmatch(r"[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{1,4}", vehicle) else None


def normalise_telegram_user_id(value: str) -> str | None:
    """Accept a positive numeric Telegram user ID."""
    user_id = value.strip()
    return user_id if user_id.isdigit() and int(user_id) > 0 else None


def normalise_pan(value: str) -> str | None:
    """Validate an Indian PAN, e.g. ABCDE1234F."""
    pan = value.strip().upper()
    return pan if re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", pan) else None


def normalise_ifsc(value: str) -> str | None:
    """Validate an IFSC code, e.g. SBIN0001234."""
    ifsc = value.strip().upper()
    return ifsc if re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc) else None


def text_result_chunks(data: Any, title: str) -> list[str]:
    """Render a nested API response as readable Telegram text rather than JSON."""
    lines: list[str] = []
    hidden_metadata = {
        "success",
        "credit",
        "usedtoday",
        "dailylimit",
        "validdays",
        "expireson",
        "query",
    }

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if normalized_key(key) in hidden_metadata:
                    continue
                walk(child, f"{path} {key}".strip())
        elif isinstance(value, list):
            for index, child in enumerate(value, start=1):
                walk(child, f"{path} {index}".strip())
        elif value is not None:
            # Many APIs wrap their fields in a `result` object. It is only a
            # transport wrapper, so do not show that word in user-facing labels.
            label_path = re.sub(r"(?<![A-Za-z0-9])result(?![A-Za-z0-9])", "", path, flags=re.IGNORECASE)
            label = re.sub(r"\s+", " ", label_path.replace("_", " ")).strip().title() or "Value"
            lines.append(f"<b>{html.escape(label)}:</b> {html.escape(str(value))}")

    walk(sanitize(data))
    if not lines:
        return []

    header = f"<b>{html.escape(title)}</b>\n━━━━━━━━━━━━━━\n"
    chunks: list[str] = []
    current = header
    for line in lines:
        if len(current) + len(line) + 1 > 4096:
            chunks.append(current.rstrip())
            current = header
        current += line + "\n"
    if current != header:
        chunks.append(current.rstrip())
    return chunks


def api_error_message(data: Any) -> str | None:
    """Return a safe, user-facing message from a failed JSON API response."""
    if not isinstance(data, dict) or data.get("status") is not False:
        return None
    message = data.get("message")
    return message if isinstance(message, str) and message else "No data found."


def combine_lookup_results(number_data: Any, aadhar_data: Any) -> Any:
    if aadhar_data:
        return {"number_info": number_data, "aadhaar_info": aadhar_data}
    return number_data


def is_no_result_payload(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    status = data.get("status")
    message = data.get("message")
    if status is False and isinstance(message, str):
        normalized = re.sub(r"[^a-z0-9]", "", message.lower())
        if "nonumberdatafound" in normalized or "numberdatafound" in normalized and "no" in normalized:
            return True
    # Some number APIs return a successful top-level response while putting the
    # actual no-result state in an item under ``result``.
    result = data.get("result")
    if isinstance(result, list):
        for item in result:
            if not isinstance(item, dict):
                continue
            item_status = item.get("status")
            if isinstance(item_status, str):
                normalized = re.sub(r"[^a-z0-9]", "", item_status.lower())
                if normalized == "nodatafound":
                    return True
    return False


def build_api_url(template: str, placeholder: str, value: str, parameter: str) -> str:
    encoded_value = quote(value, safe="")
    expanded = template.replace("${" + placeholder + "}", encoded_value)
    if "${" + placeholder + "}" in expanded:
        raise ValueError(f"Unresolved API placeholder: {placeholder}")

    parts = urlsplit(expanded)
    if parameter not in parts.query:
        separator = "&" if parts.query else ""
        expanded = urlunsplit(parts._replace(query=parts.query + separator + f"{parameter}={encoded_value}"))
    return expanded


def start_referrer_id(message: Message) -> int | None:
    """Extract a numeric referrer ID from a /start deep-link command."""
    parts = message.text.split(maxsplit=1) if message.text else []
    if len(parts) != 2:
        return None
    value = parts[1].strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


async def lookup_number(message: Message, number: str) -> None:
    if not message.from_user:
        return

    if await asyncio.to_thread(osint_bot.is_protected_number, number):
        await message.reply_text("This number was protected. Try another number.")
        return

    missing_channels = await missing_required_channels(message.from_user.id)

    if missing_channels:
        await message.reply_text(JOIN_TEXT, reply_markup=join_keyboard(missing_channels))
        return

    await asyncio.to_thread(osint_bot.register_user, message.from_user)

    api_template = os.getenv("NUM_TO_INFO", "").strip()
    if not api_template:
        await message.reply_text("The number lookup API is not configured.")
        return

    has_credit = await asyncio.to_thread(osint_bot.consume_credit, message.from_user.id)
    if not has_credit:
        await message.reply_text(f"<b><i>Contact the admin to buy credits and premium to your account before using this command.\n\n Admin Contact: @{OWNER_USERNAME}</b></i>")
        return

    loading_message = await message.reply_text("Fetching the number information…")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            url = build_api_url(api_template, "NUMBER", number, "number")
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            error_message = api_error_message(data)
            if error_message:
                await remove_loading_message(loading_message)
                await message.reply_text(error_message)
                return
    except httpx.HTTPStatusError as error:
        LOGGER.warning("Number API returned HTTP %s", error.response.status_code)
        await remove_loading_message(loading_message)
        await message.reply_text("The lookup service rejected the request. Please try again later.")
        return
    except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as error:
        LOGGER.warning("Lookup failed: %s", type(error).__name__)
        await remove_loading_message(loading_message)
        await message.reply_text("Lookup failed. Please try again later.")
        return

    if is_no_result_payload(data):
        await remove_loading_message(loading_message)
        await message.reply_text("❌ No data found")
        return
    chunks = lookup_message_chunks(number, data, "num")
    if not chunks:
        await remove_loading_message(loading_message)
        await message.reply_text("❌ No data found")
        return

    await remove_loading_message(loading_message)
    for chunk in chunks:
        await message.reply_text(chunk)


async def lookup_aadhar(message: Message, aadhar: str) -> None:
    if not message.from_user:
        return

    missing_channels = await missing_required_channels(message.from_user.id)
    if missing_channels:
        await message.reply_text(JOIN_TEXT, reply_markup=join_keyboard(missing_channels))
        return

    await asyncio.to_thread(osint_bot.register_user, message.from_user)
    api_template = os.getenv("AADHAR_TO_INFO", "").strip()
    if not api_template:
        await message.reply_text("The Aadhaar lookup API is not configured.")
        return

    has_credit = await asyncio.to_thread(osint_bot.consume_credit, message.from_user.id)
    if not has_credit:
        await message.reply_text(f"<b><i>Contact the admin to buy credits and premium to your account before using this command.\n\n Admin Contact: @{OWNER_USERNAME}</b></i>")
        return

    loading_message = await message.reply_text("Fetching the Aadhaar information…")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            url = build_api_url(api_template, "AADHAR", aadhar, "aadhar")
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            error_message = api_error_message(data)
            if error_message:
                await remove_loading_message(loading_message)
                await message.reply_text(error_message)
                return
    except httpx.HTTPStatusError as error:
        LOGGER.warning("Aadhaar API returned HTTP %s", error.response.status_code)
        await remove_loading_message(loading_message)
        await message.reply_text("The lookup service rejected the request. Please try again later.")
        return
    except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as error:
        LOGGER.warning("Aadhaar lookup failed: %s", type(error).__name__)
        await remove_loading_message(loading_message)
        await message.reply_text("Lookup failed. Please try again later.")
        return

    chunks = lookup_message_chunks(aadhar, data, "aadhar")
    await remove_loading_message(loading_message)
    if not chunks:
        await message.reply_text("**No data found**")
        return
    for chunk in chunks:
        await message.reply_text(chunk)


async def lookup_standard_api(message: Message, value: str, *, setting: str,
                              placeholder: str, parameter: str, title: str) -> None:
    """Run a configured, credit-based lookup and render its JSON response."""
    if not message.from_user:
        return
    if setting == "ADV_NUM_TO_INFO" and await asyncio.to_thread(
        osint_bot.is_protected_number, value
    ):
        await message.reply_text("This number was protected. Try another number.")
        return
    missing_channels = await missing_required_channels(message.from_user.id)
    if missing_channels:
        await message.reply_text(JOIN_TEXT, reply_markup=join_keyboard(missing_channels))
        return
    await asyncio.to_thread(osint_bot.register_user, message.from_user)
    if setting in PREMIUM_STANDARD_LOOKUP_SETTINGS and not await asyncio.to_thread(
        osint_bot.is_premium, message.from_user.id
    ):
        await message.reply_text(PREMIUM_REQUIRED_TEXT)
        return
    api_template = os.getenv(setting, "").strip()
    if not api_template:
        await message.reply_text(f"The {title.lower()} API is not configured.")
        return
    if not await asyncio.to_thread(osint_bot.consume_credit, message.from_user.id):
        await message.reply_text(
            "<b><i>Contact the admin to buy credits and premium to your account before using this command."
            f"\n\nAdmin Contact: @{OWNER_USERNAME}</i></b>"
        )
        return

    loading_message = await message.reply_text(f"Fetching {title.lower()}…")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            url = build_api_url(api_template, placeholder, value, parameter)
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as error:
        LOGGER.warning("%s API returned HTTP %s", setting, error.response.status_code)
        await remove_loading_message(loading_message)
        await message.reply_text("The lookup service rejected the request. Please try again later.")
        return
    except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as error:
        LOGGER.warning("%s lookup failed: %s", setting, type(error).__name__)
        await remove_loading_message(loading_message)
        await message.reply_text("Lookup failed. Please try again later.")
        return

    await remove_loading_message(loading_message)
    error_message = api_error_message(data)
    if error_message or is_no_result_payload(data):
        await message.reply_text(error_message or "No data found.")
        return
    if setting == "AADHAR_TO_FAMILY":
        formatted = json_message(data)
        if len(formatted) <= 4096:
            await message.reply_text(formatted)
        else:
            await message.reply_document(
                result_file(data, "aadhaar_family_info.json"),
                caption="Aadhaar Family Info (JSON)",
            )
        return
    chunks = text_result_chunks(data, title.upper())
    if not chunks:
        await message.reply_text("No data found.")
        return
    for chunk in chunks:
        await message.reply_text(chunk)


async def fetch_vehicle_data(message: Message, vehicle: str, setting: str) -> Any | None:
    """Fetch a vehicle API response after applying the normal lookup checks."""
    if not message.from_user:
        return None

    missing_channels = await missing_required_channels(message.from_user.id)
    if missing_channels:
        await message.reply_text(JOIN_TEXT, reply_markup=join_keyboard(missing_channels))
        return None

    await asyncio.to_thread(osint_bot.register_user, message.from_user)
    if not await asyncio.to_thread(
        osint_bot.is_premium, message.from_user.id
    ):
        await message.reply_text(PREMIUM_REQUIRED_TEXT)
        return None

    api_template = os.getenv(setting, "").strip()
    if not api_template:
        await message.reply_text("The vehicle lookup API is not configured.")
        return None

    loading_message = await message.reply_text("Fetching vehicle information…")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            url = build_api_url(api_template, "VEHICLE", vehicle, "rc")
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as error:
        LOGGER.warning("Vehicle API returned HTTP %s", error.response.status_code)
        await remove_loading_message(loading_message)
        await message.reply_text("Not Founded")
        return None
    except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as error:
        LOGGER.warning("Vehicle lookup failed: %s", type(error).__name__)
        await remove_loading_message(loading_message)
        await message.reply_text("Lookup failed. Please try again later.")
        return None

    await remove_loading_message(loading_message)
    error_message = api_error_message(data)
    if error_message or is_no_result_payload(data):
        await message.reply_text(error_message or "No data found.")
        return None
    return data


async def lookup_vehicle_info(message: Message, vehicle: str) -> None:
    data = await fetch_vehicle_data(message, vehicle, "VEHICLE_TO_INFO")
    if data is None:
        return

    formatted = json_message(data)
    if len(formatted) <= 4096:
        await message.reply_text(formatted)
        return

    await message.reply_document(
        result_file(data, f"{vehicle}_vehicle_info.json"),
        caption="Vehicle lookup data (JSON)",
    )


async def lookup_vehicle_number(message: Message, vehicle: str) -> None:
    data = await fetch_vehicle_data(message, vehicle, "VEHICLE_TO_NUMBER")
    if data is None:
        return

    number = field_value(data, "number")
    if not number:
        await message.reply_text("No mobile number found for this vehicle.")
        return

    number = normalise_phone_number(number) or number
    await message.reply_text(
        "✅ <b>Lookup Complete</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"📝 Input: <code>{html.escape(vehicle)}</code>\n\n"
        "<b>📱 SEARCHED NUMBER</b>\n"
        f"┗ ➤ <code>{html.escape(number)}</code>"
    )


async def lookup_tg_info(message: Message, telegram_user_id: str) -> None:
    """Fetch and render configured Telegram-account information for premium users."""
    if not message.from_user:
        return

    missing_channels = await missing_required_channels(message.from_user.id)
    if missing_channels:
        await message.reply_text(JOIN_TEXT, reply_markup=join_keyboard(missing_channels))
        return

    await asyncio.to_thread(osint_bot.register_user, message.from_user)
    if not await asyncio.to_thread(osint_bot.is_premium, message.from_user.id):
        await message.reply_text(PREMIUM_REQUIRED_TEXT)
        return

    api_template = os.getenv("TG_TO_INFO", "").strip()
    if not api_template:
        await message.reply_text("The TG to Info API is not configured.")
        return

    loading_message = await message.reply_text("Fetching Telegram account information…")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            url = build_api_url(api_template, "TG_ID", telegram_user_id, "id")
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as error:
        LOGGER.warning("TG info API returned HTTP %s", error.response.status_code)
        await remove_loading_message(loading_message)
        await message.reply_text("The lookup service rejected the request. Please try again later.")
        return
    except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as error:
        LOGGER.warning("TG info lookup failed: %s", type(error).__name__)
        await remove_loading_message(loading_message)
        await message.reply_text("Lookup failed. Please try again later.")
        return

    await remove_loading_message(loading_message)
    error_message = api_error_message(data)
    if error_message or is_no_result_payload(data):
        await message.reply_text(error_message or "No data found.")
        return

    chunks = text_result_chunks(data, "TG INFO")
    if not chunks:
        await message.reply_text("No data found.")
        return
    for chunk in chunks:
        await message.reply_text(chunk)


@app.on_message(filters.private & filters.text & ~filters.regex(r"^/"))
async def lookup_keyboard_input(_: Client, message: Message) -> None:
    """Consume a lookup value after an inline action is selected."""
    if not message.from_user or not message.text:
        return

    user_id = message.from_user.id
    selected_tool = PENDING_LOOKUPS.get(user_id)
    if not selected_tool:
        return

    if selected_tool == "number":
        number = normalise_phone_number(message.text)
        if not number:
            await message.reply_text("Please send a valid 10-digit mobile number.")
            return
        PENDING_LOOKUPS.pop(user_id, None)
        await lookup_number(message, number)
        return

    if selected_tool == "advanced_number":
        number = normalise_phone_number(message.text)
        if not number:
            await message.reply_text("Please send a valid 10-digit mobile number.")
            return
        PENDING_LOOKUPS.pop(user_id, None)
        await lookup_standard_api(message, number, setting="ADV_NUM_TO_INFO", placeholder="NUMBER", parameter="number", title="Advanced Number Info")
        return

    if selected_tool == "pan":
        pan = normalise_pan(message.text)
        if not pan:
            await message.reply_text("Please send a valid PAN (for example, ABCDE1234F).")
            return
        PENDING_LOOKUPS.pop(user_id, None)
        await lookup_standard_api(message, pan, setting="PAN_TO_INFO", placeholder="PAN", parameter="pan", title="PAN Info")
        return

    if selected_tool == "ifsc":
        ifsc = normalise_ifsc(message.text)
        if not ifsc:
            await message.reply_text("Please send a valid IFSC code (for example, SBIN0001234).")
            return
        PENDING_LOOKUPS.pop(user_id, None)
        await lookup_standard_api(message, ifsc, setting="IFSC_TO_INFO", placeholder="IFSC", parameter="ifsc", title="IFSC Info")
        return

    if selected_tool == "tg_info":
        telegram_user_id = normalise_telegram_user_id(message.text)
        if not telegram_user_id:
            await message.reply_text("Please send a valid numeric Telegram user ID.")
            return
        PENDING_LOOKUPS.pop(user_id, None)
        await lookup_tg_info(message, telegram_user_id)
        return

    if selected_tool in {"vehicle_info", "vehicle_number"}:
        vehicle = normalise_vehicle_number(message.text)
        if not vehicle:
            await message.reply_text(
                "Please send a valid vehicle registration number (for example, GJ23CH0081)."
            )
            return
        PENDING_LOOKUPS.pop(user_id, None)
        if selected_tool == "vehicle_info":
            await lookup_vehicle_info(message, vehicle)
        else:
            await lookup_vehicle_number(message, vehicle)
        return

    aadhar = normalise_aadhaar(message.text)
    if not aadhar:
        await message.reply_text("Please send a valid 12-digit Aadhaar number.")
        return
    PENDING_LOOKUPS.pop(user_id, None)
    if selected_tool == "aadhar_family":
        await lookup_standard_api(message, aadhar, setting="AADHAR_TO_FAMILY", placeholder="AADHAR", parameter="aadhar", title="Aadhaar Family Info")
    else:
        await lookup_aadhar(message, aadhar)


@app.on_message(filters.command("refer") & filters.private)
async def refer_command(_: Client, message: Message) -> None:
    """Show a user's referral link and completed-referral progress."""
    if not message.from_user:
        return
    missing_channels = await missing_required_channels(message.from_user.id)
    if missing_channels:
        await message.reply_text(JOIN_TEXT, reply_markup=join_keyboard(missing_channels))
        return

    await asyncio.to_thread(osint_bot.register_user, message.from_user)
    referrals, earned_credits = await asyncio.to_thread(
        osint_bot.referral_stats, message.from_user.id
    )
    credits = await asyncio.to_thread(osint_bot.get_credits, message.from_user.id)
    is_premium = await asyncio.to_thread(osint_bot.is_premium, message.from_user.id)
    progress = referrals % 2
    percent = progress * 50
    progress_bar = "▰" * (progress * 5) + "▱" * (10 - progress * 5)
    remaining = 2 - progress
    referral_link = f"https://t.me/{BOT_USERNAME}?start={message.from_user.id}"
    await message.reply_text(
        "<b>🤝 REFER &amp; EARN</b>\n\n"
        f"🔗 <b>Your Referral Link:</b>\n<code>{referral_link}</code>\n\n"
        "📊 <b>Your Stats:</b>\n"
        f"• Total Successful Referrals: <b>{referrals}</b>\n"
        f"• Credits Earned from Referrals: <b>{earned_credits}</b>\n\n"
        "🎯 <b>Progress to Next Credit:</b>\n"
        f"{progress_bar} {percent}%\n"
        f"┗ ➤ {remaining} more referral(s) needed\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>How it works:</b>\n"
        "• Share your referral link with friends.\n"
        "• When they start the bot using your link and join all required channels, they become your referral.\n"
        "• For every 2 successful referrals, you earn <b>1 FREE Credit</b>!\n"
        "• Referrals are counted only after channel membership is verified.\n\n"
        "🚀 <b>Start sharing now and earn free credits!</b>"
        ,
        reply_markup=referral_keyboard(),
    )


@app.on_message(filters.command("givecredit") & filters.private)
async def give_credit_command(_: Client, message: Message) -> None:
    """Allow the configured administrator to add credits to registered users."""
    if not message.from_user or message.from_user.id not in ADMIN_USER_IDS:
        await message.reply_text("You are not authorized to use this command.")
        return

    parts = message.text.split(maxsplit=2) if message.text else []
    if len(parts) != 3:
        await message.reply_text("Usage: /givecredit <amount> <all|userid>")
        return

    try:
        amount = int(parts[1])
    except ValueError:
        amount = 0
    if amount <= 0:
        await message.reply_text("Credit amount must be a positive whole number.")
        return

    recipient = parts[2].strip().lower()
    if recipient == "all":
        count = await asyncio.to_thread(osint_bot.add_credits_to_all, amount)
        await message.reply_text(f"Added {amount} credit(s) to {count} user(s).")
        return

    if not recipient.isdigit():
        await message.reply_text("Usage: /givecredit <amount> <all|userid>")
        return

    updated = await asyncio.to_thread(osint_bot.add_credits, int(recipient), amount)
    if not updated:
        await message.reply_text("That user has not started the bot yet.")
        return
    await message.reply_text(f"Added {amount} credit(s) to user {recipient}.")


@app.on_message(filters.command("protect") & filters.private)
async def protect_number_command(_: Client, message: Message) -> None:
    """Allow an administrator to prevent a number from being looked up."""
    if not message.from_user or message.from_user.id not in ADMIN_USER_IDS:
        await message.reply_text("You are not authorized to use this command.")
        return

    parts = message.text.split(maxsplit=1) if message.text else []
    number = normalise_phone_number(parts[1]) if len(parts) == 2 else None
    if not number:
        await message.reply_text("Usage: /protect <10-digit mobile number>")
        return

    added = await asyncio.to_thread(osint_bot.protect_number, number, message.from_user.id)
    if added:
        await message.reply_text(f"Number <code>{number}</code> is now protected.")
    else:
        await message.reply_text(f"Number <code>{number}</code> is already protected.")


@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_command(_: Client, message: Message) -> None:
    """Send an admin message to every registered bot user."""
    if not message.from_user or message.from_user.id not in ADMIN_USER_IDS:
        await message.reply_text("You are not authorized to use this command.")
        return

    reply = message.reply_to_message
    text = ""
    entities = None
    if not reply:
        command_match = re.match(r"^/broadcast(?:@\w+)?\s+", message.text or "")
        if not command_match:
            await message.reply_text(
                "Usage: /broadcast <message>\n\n"
                "To preserve any message type and formatting exactly, reply to it with /broadcast."
            )
            return
        body_offset = command_match.end()
        text = (message.text or "")[body_offset:]
        if not text:
            await message.reply_text("Please provide a message to broadcast.")
            return

        entities = []
        for entity in message.entities or []:
            entity_end = entity.offset + entity.length
            if entity.offset >= body_offset:
                adjusted = copy.copy(entity)
                adjusted.offset -= body_offset
                entities.append(adjusted)
            elif entity_end > body_offset:
                # A format spanning the command boundary cannot be sent safely.
                LOGGER.warning("Ignoring broadcast entity spanning the command prefix")

    user_ids = await asyncio.to_thread(osint_bot.user_ids)
    sent = 0
    failed = 0
    await message.reply_text(f"Broadcast started for {len(user_ids)} user(s).")

    for user_id in user_ids:
        try:
            if reply:
                await app.copy_message(user_id, reply.chat.id, reply.id)
            else:
                await app.send_message(user_id, text, entities=entities)
            sent += 1
        except FloodWait as error:
            await asyncio.sleep(error.value)
            try:
                if reply:
                    await app.copy_message(user_id, reply.chat.id, reply.id)
                else:
                    await app.send_message(user_id, text, entities=entities)
                sent += 1
            except RPCError:
                failed += 1
        except RPCError:
            failed += 1

    await message.reply_text(f"Broadcast complete. Sent: {sent}; failed: {failed}.")


@app.on_message(filters.command("premium") & filters.private)
async def premium_command(_: Client, message: Message) -> None:
    """Grant a user unlimited access through the supplied expiry date."""
    if not message.from_user or message.from_user.id not in ADMIN_USER_IDS:
        await message.reply_text("You are not authorized to use this command.")
        return

    parts = message.text.split(maxsplit=2) if message.text else []
    if len(parts) != 3 or not parts[1].isdigit():
        await message.reply_text("Usage: /premium <userid> <dd-mm-yyyy>")
        return

    try:
        expiry_date = datetime.strptime(parts[2], "%d-%m-%Y").date()
    except ValueError:
        await message.reply_text("Use the expiry format dd-mm-yyyy, for example 31-12-2026.")
        return

    expires_at = datetime.combine(expiry_date, time.max, tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        await message.reply_text("The premium expiry date must be in the future.")
        return

    user_id = int(parts[1])
    await asyncio.to_thread(osint_bot.set_premium, user_id, expires_at)
    await message.reply_text(
        f"Premium enabled for <code>{user_id}</code> until <b>{expiry_date:%d-%m-%Y}</b>."
    )


def find_aadhaar(value: Any) -> str | None:
    """Find an Aadhaar field internally; it is never included in bot output."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {"aadhar", "aadhaar", "aadharno", "aadhaarno", "uid"}:
                digits = re.sub(r"\D", "", str(child))
                if len(digits) == 12:
                    return digits
            found = find_aadhaar(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_aadhaar(child)
            if found:
                return found
    return None


@app.on_message(filters.command("start") & filters.private)
async def start_command(_: Client, message: Message) -> None:
    if not message.from_user:
        return
    referrer_id = start_referrer_id(message)
    await asyncio.to_thread(osint_bot.register_user, message.from_user, referrer_id)
    missing_channels = await missing_required_channels(message.from_user.id)
    if missing_channels:
        await message.reply_text(JOIN_TEXT, reply_markup=join_keyboard(missing_channels))
        return
    referrer_id = await asyncio.to_thread(
        osint_bot.complete_referral, message.from_user.id
    )
    await notify_referrer(referrer_id, message.from_user)
    await send_start_message(message)


@app.on_callback_query(filters.regex("^verify_subscription$"))
async def verify_subscription(_: Client, callback_query: CallbackQuery) -> None:
    user = callback_query.from_user
    missing_channels = await missing_required_channels(user.id)
    if missing_channels:
        await callback_query.answer(
            "Please join every required channel first.",
            show_alert=True,
        )
        return

    await asyncio.to_thread(osint_bot.register_user, user)
    referrer_id = await asyncio.to_thread(osint_bot.complete_referral, user.id)
    await notify_referrer(referrer_id, user)
    await callback_query.answer("Membership verified!")
    await callback_query.message.delete()
    await send_start_message(callback_query.message, user.id)


@app.on_callback_query(
    filters.regex(
        r"^lookup_(number|aadhar|advanced_number|pan|aadhar_family|ifsc|tg_info|vehicle_info|vehicle_number)$"
    )
)
async def choose_lookup(_: Client, callback_query: CallbackQuery) -> None:
    """Prompt for lookup input after the user chooses a blue inline action."""
    user = callback_query.from_user
    selected_tool = callback_query.data.removeprefix("lookup_")
    missing_channels = await missing_required_channels(user.id)
    if missing_channels:
        await callback_query.answer("Please join every required channel first.", show_alert=True)
        return

    if selected_tool in {
        "advanced_number", "pan", "aadhar_family", "ifsc", "tg_info",
        "vehicle_info", "vehicle_number",
    } and not await asyncio.to_thread(osint_bot.is_premium, user.id):
        await callback_query.answer(
            PREMIUM_REQUIRED_ALERT, show_alert=True
        )
        return

    await asyncio.to_thread(osint_bot.register_user, user)
    PENDING_LOOKUPS[user.id] = selected_tool
    prompt = (
        "Send the 10-digit mobile number."
        if selected_tool == "number"
        else "Send the 10-digit mobile number for the advanced lookup."
        if selected_tool == "advanced_number"
        else "Send the 12-digit Aadhaar number."
        if selected_tool in {"aadhar", "aadhar_family"}
        else "Send the 10-character PAN (for example, ABCDE1234F)."
        if selected_tool == "pan"
        else "Send the 11-character IFSC code (for example, SBIN0001234)."
        if selected_tool == "ifsc"
        else "Send the Telegram account user ID."
        if selected_tool == "tg_info"
        else "Send the vehicle registration number (for example, GJ23CH0081)."
    )
    await callback_query.message.reply_text(prompt)
    await callback_query.answer()


@app.on_callback_query(filters.regex("^account_status$"))
async def account_status(_: Client, callback_query: CallbackQuery) -> None:
    """Show the current balance and premium state without changing the menu."""
    user = callback_query.from_user
    credits = await asyncio.to_thread(osint_bot.get_credits, user.id)
    is_premium = await asyncio.to_thread(osint_bot.is_premium, user.id)
    status = "Unlocked (unlimited usage)" if is_premium else "Locked"
    balance = "Unlimited" if is_premium else str(credits)
    await callback_query.answer(
        f"Balance: {balance}\nPremium: {status}", show_alert=True
    )


@app.on_callback_query(filters.regex("^refer_earn$"))
async def show_referral_view(_: Client, callback_query: CallbackQuery) -> None:
    user = callback_query.from_user
    missing_channels = await missing_required_channels(user.id)
    if missing_channels:
        await callback_query.answer("Please join every required channel first.", show_alert=True)
        return

    await asyncio.to_thread(osint_bot.register_user, user)
    await callback_query.message.edit_text(
        await referral_view_text(user.id), reply_markup=referral_keyboard()
    )
    await callback_query.answer()


@app.on_callback_query(filters.regex("^copy_referral_link$"))
async def copy_referral_link(_: Client, callback_query: CallbackQuery) -> None:
    referral_link = f"https://t.me/{BOT_USERNAME}?start={callback_query.from_user.id}"
    await callback_query.answer(f"Copy this referral link:\n{referral_link}", show_alert=True)


@app.on_callback_query(filters.regex("^refer_back$"))
async def show_start_view(_: Client, callback_query: CallbackQuery) -> None:
    user = callback_query.from_user
    await callback_query.message.edit_text(
        await welcome_text(user.id),
        reply_markup=start_menu_keyboard(),
    )
    await callback_query.answer()



if __name__ == "__main__":
    LOGGER.info("Starting bot")
    threading.Thread(
        target=lambda: web_app.run(
            host="0.0.0.0",
            port=int(os.getenv("PORT", "5000")),
            use_reloader=False,
        ),
        daemon=True,
    ).start()
    app.run()
