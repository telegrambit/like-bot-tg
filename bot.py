import asyncio
import html
import logging
import sqlite3
import time
from pathlib import Path

import aiohttp
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config import (
    ADMIN_IDS,
    BOT_TOKEN,
    DAILY_LIKE_LIMIT,
    INFO_API_URL,
    LIKE_API_KEY,
    LIKE_API_URL,
    REQUEST_TIMEOUT,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("free-fire-like-bot")

DB_PATH = Path(__file__).with_name("bot.db")


# -------------------------
# Database
# -------------------------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS like_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_like_usage_user_time
        ON like_usage(user_id, created_at)
        """
    )
    conn.commit()
    return conn


def cleanup_old_usage(conn, now=None):
    now = now or int(time.time())
    cutoff = now - 86400
    conn.execute("DELETE FROM like_usage WHERE created_at < ?", (cutoff,))
    conn.commit()


def get_usage(user_id: int):
    now = int(time.time())
    conn = db_connect()
    try:
        cleanup_old_usage(conn, now)
        row = conn.execute(
            "SELECT COUNT(*) FROM like_usage WHERE user_id = ? AND created_at >= ?",
            (user_id, now - 86400),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def add_usage(user_id: int):
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO like_usage(user_id, created_at) VALUES(?, ?)",
            (user_id, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def remaining_seconds(user_id: int):
    conn = db_connect()
    try:
        row = conn.execute(
            """
            SELECT MIN(created_at)
            FROM like_usage
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if not row or row[0] is None:
            return 0
        return max(0, int(row[0]) + 86400 - int(time.time()))
    finally:
        conn.close()


def fmt_wait(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "less than 1m"


# -------------------------
# Helpers
# -------------------------

def args_or_usage(update: Update, usage: str):
    args = update.effective_message.text.split()
    if len(args) != 3:
        return None, None, usage
    return args[1].upper(), args[2], None


def safe(value, default="N/A"):
    if value is None or value == "":
        return default
    return html.escape(str(value))


def get_nested(data, *keys, default="N/A"):
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None or cur == "" else cur


async def fetch_json(url: str, params: dict):
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            url,
            params=params,
            headers={"User-Agent": "FreeFireTelegramBot/1.0"},
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"API HTTP {resp.status}: {text[:300]}")
            try:
                return await resp.json(content_type=None)
            except Exception as exc:
                raise RuntimeError(f"API returned invalid JSON: {exc}") from exc


def parse_like_response(data: dict, fallback_uid: str):
    # This supports the field names shown in the API response you shared.
    nickname = (
        data.get("PlayerNickname")
        or data.get("PlayerName")
        or data.get("Name")
        or get_nested(data, "BasicInformation", "Name")
        or "Unknown"
    )

    uid = (
        data.get("UID")
        or data.get("uid")
        or get_nested(data, "BasicInformation", "UID")
        or fallback_uid
    )

    before = (
        data.get("LikesbeforeCommand")
        or data.get("LikesBeforeCommand")
        or data.get("LikesBefore")
        or get_nested(data, "BasicInformation", "Likes", default=0)
    )

    after = (
        data.get("LikesafterCommand")
        or data.get("LikesAfterCommand")
        or data.get("LikesAfter")
        or before
    )

    added = (
        data.get("LikesGivenByAPI")
        if data.get("LikesGivenByAPI") is not None
        else data.get("LikesAdded", data.get("AddedLikes", 0))
    )

    return str(nickname), str(uid), before, after, added


def like_message(nickname, uid, server, before, after, added, requester, status, used):
    limit_text = "Unlimited" if status == "👑 Admin" else f"{used}/{DAILY_LIKE_LIMIT} used"

    return (
        "╔═════════════════════════╗\n"
        "       ✅ <b>LIKE SENT SUCCESSFULLY</b> ✅\n"
        "╚═════════════════════════╝\n\n"
        f"👤 <b>Name</b>      : {safe(nickname)}\n"
        f"🆔 <b>UID</b>       : {safe(uid)}\n"
        f"🌍 <b>Region</b>    : {safe(server)}\n\n"
        "📊 <b>Like Details</b>\n"
        f"├ 💙 <b>Before</b>  : {safe(before)}\n"
        f"├ 💚 <b>After</b>   : {safe(after)}\n"
        f"└ ❤️ <b>Added</b>   : {safe(added)}\n\n"
        "👑 <b>Requested By</b>\n"
        f"├ 👤 <b>Name</b>    : {safe(requester)}\n"
        f"├ ⚡️ <b>Status</b>  : {safe(status)}\n"
        f"└ ♾️ <b>Limit</b>   : {safe(limit_text)}"
    )


def info_message(data: dict, requested_server: str):
    basic = data.get("BasicInformation", {}) or {}
    activity = data.get("ActivityInformation", {}) or {}
    guild = data.get("GuildInformation", {}) or {}
    leader = data.get("LeaderInformation", {}) or {}
    pet = data.get("PetDetails", {}) or {}

    api_server = data.get("server_used") or requested_server
    status = data.get("status", "unknown")

    return (
        "╔════════════════════════════╗\n"
        "        📋 <b>FREE FIRE PLAYER INFO</b>\n"
        "╚════════════════════════════╝\n\n"
        "👤 <b>Basic Information</b>\n"
        f"├ 🏷️ Name      : {safe(basic.get('Name'))}\n"
        f"├ 🆔 UID       : {safe(basic.get('UID'))}\n"
        f"├ 🌍 Region    : {safe(basic.get('Region'))}\n"
        f"├ ❤️ Likes     : {safe(basic.get('Likes'))}\n"
        f"├ ⭐ Level     : {safe(basic.get('Level'))}\n"
        f"├ ✨ EXP       : {safe(basic.get('Exp'))}\n"
        f"├ 🛡️ Honor     : {safe(basic.get('HonorScore'))}\n"
        f"├ 👑 Prime     : {safe(basic.get('PrimeLevel'))}\n"
        f"└ 🚫 Ban       : {safe(data.get('BanStatus'))}\n\n"

        "🏆 <b>Activity Information</b>\n"
        f"├ 🎯 BR Points : {safe(activity.get('BRPoints'))}\n"
        f"├ 🏅 BR Rank   : {safe(activity.get('BRRank'))}\n"
        f"├ 🎫 Booyah    : {safe(activity.get('BooyahPass'))}\n"
        f"├ 🎖️ BP Badges : {safe(activity.get('CurrentBpBadges'))}\n"
        f"├ 🆕 OB        : {safe(activity.get('MostRecentOB'))}\n"
        f"├ 📅 Created   : {safe(activity.get('CreatedAt'))}\n"
        f"└ 🕘 Last Login: {safe(activity.get('LastLogin'))}\n\n"

        "🏰 <b>Guild Information</b>\n"
        f"├ 🆔 Guild ID  : {safe(guild.get('GuildID'))}\n"
        f"├ 🏷️ Name      : {safe(guild.get('GuildName'))}\n"
        f"├ ⭐ Level     : {safe(guild.get('GuildLevel'))}\n"
        f"├ 👥 Members   : {safe(guild.get('LiveMembers'))}/{safe(guild.get('MaxMembers'))}\n"
        "└\n\n"

        "👑 <b>Leader Information</b>\n"
        f"├ 👤 Name      : {safe(leader.get('Name'))}\n"
        f"├ 🆔 UID       : {safe(leader.get('UID'))}\n"
        f"├ 🌍 Region    : {safe(leader.get('Region'))}\n"
        f"├ 🏅 BR Rank   : {safe(leader.get('BRRank'))}\n"
        f"└ 🎯 BR Points : {safe(leader.get('BRPoints'))}\n\n"

        "🐾 <b>Pet Details</b>\n"
        f"├ 🐶 Pet       : {safe(pet.get('PetNick'))}\n"
        f"├ ⭐ Level     : {safe(pet.get('PetLevel'))}\n"
        f"├ 📈 EXP       : {safe(pet.get('PetExp'))}\n"
        f"└ 🎒 Equipped  : {safe(pet.get('Equipped'))}\n\n"

        f"🌐 <b>Server Used</b> : {safe(api_server)}\n"
        f"📡 <b>API Status</b> : {safe(status)}"
    )


# -------------------------
# Commands
# -------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🔥 <b>Free Fire Like & Info Bot</b>\n\n"
        "❤️ <b>Like:</b> /like ind 12345678\n"
        "📋 <b>Info:</b> /info ind 12345678\n\n"
        "Normal users get 2 /like requests per 24 hours.\n"
        "👑 Admin users have unlimited /like requests.",
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "⚠️ <b>Usage</b>\n\n"
        "<code>/like ind 12345678</code>\n"
        "<code>/info ind 12345678</code>",
        parse_mode=ParseMode.HTML,
    )


async def like_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user

    server, uid, error = args_or_usage(update, "⚠️ Usage: /like ind 12345678")
    if error:
        await message.reply_text(error)
        return

    if not server.isalpha() or not uid.isdigit():
        await message.reply_text("⚠️ Usage: /like ind 12345678")
        return

    admin = is_admin(user.id)
    used = get_usage(user.id)

    if not admin and used >= DAILY_LIKE_LIMIT:
        await message.reply_text(
            "❌ <b>Daily Limit Exceeded!</b>\n\n"
            "You can use 5 likes per 24 hours.\n"
            "⏳ Please wait 24 hours from your last like.\n"
            "👑 Admin users have unlimited likes.",
            parse_mode=ParseMode.HTML,
        )
        return

    loading = await message.reply_text("⏳ <b>Sending Likes...</b>", parse_mode=ParseMode.HTML)

    try:
        data = await fetch_json(
            LIKE_API_URL,
            {
                "uid": uid,
                "server_name": server,
            },
        )

        nickname, real_uid, before, after, added = parse_like_response(data, uid)

        # Count the request after the API has responded successfully.
        if not admin:
            add_usage(user.id)
            used += 1

        requester = user.first_name or user.username or str(user.id)
        if user.last_name:
            requester += f" {user.last_name}"

        status = "👑 Admin" if admin else "👤 User"

        await loading.edit_text(
            like_message(
                nickname,
                real_uid,
                server,
                before,
                after,
                added,
                requester,
                status,
                used,
            ),
            parse_mode=ParseMode.HTML,
        )

    except asyncio.TimeoutError:
        await loading.edit_text(
            "❌ <b>API Timeout</b>\nPlease try again later.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        log.exception("Like API error")
        await loading.edit_text(
            "❌ <b>Like request failed.</b>\n\n"
            f"<code>{safe(str(exc)[:500])}</code>",
            parse_mode=ParseMode.HTML,
        )


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    server, uid, error = args_or_usage(update, "⚠️ Usage: /info ind 12345678")
    if error:
        await message.reply_text(error)
        return

    if not server.isalpha() or not uid.isdigit():
        await message.reply_text("⚠️ Usage: /info ind 12345678")
        return

    loading = await message.reply_text("⏳ <b>Fetching player info...</b>", parse_mode=ParseMode.HTML)

    try:
        # The supplied info API accepts uid. We keep the requested server
        # for display while preserving the API's own server_used/Region values.
        data = await fetch_json(INFO_API_URL, {"uid": uid})

        if isinstance(data, dict) and data.get("status") not in (None, "success"):
            raise RuntimeError(f"API status: {data.get('status')}")

        await loading.edit_text(
            info_message(data, server),
            parse_mode=ParseMode.HTML,
        )

    except asyncio.TimeoutError:
        await loading.edit_text(
            "❌ <b>Info API Timeout</b>\nPlease try again later.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        log.exception("Info API error")
        await loading.edit_text(
            "❌ <b>Could not fetch player info.</b>\n\n"
            f"<code>{safe(str(exc)[:500])}</code>",
            parse_mode=ParseMode.HTML,
        )


async def post_init(application: Application):
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start the bot"),
            BotCommand("like", "Send likes: /like ind UID"),
            BotCommand("info", "Player info: /info ind UID"),
            BotCommand("help", "Show usage"),
        ]
    )


def main():
    if not BOT_TOKEN or "PASTE_YOUR" in BOT_TOKEN:
        raise SystemExit("Please put your Telegram bot token in config.py")

    # Make sure the database exists before polling starts.
    conn = db_connect()
    conn.close()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("like", like_command))
    app.add_handler(CommandHandler("info", info_command))

    print("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
