import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv
from telegram import Update
from urllib.parse import quote_plus
from telegram.constants import ParseMode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "users.sqlite3")
TARGET_SUBDOMAIN = os.getenv("TARGET_SUBDOMAIN", "kk")  # можно поменять, если надо

# Ловим "похожие на ссылки" куски, включая без схемы.
# Пример: instagram.com/reel/..., www.instagram.com/..., https://...
URL_RE = re.compile(r"((?:https?://)?[^\s]+)", re.IGNORECASE)

# Убираем типичный мусор вокруг ссылки
TRIM_CHARS = " \t\r\n()[]{}<>,.!\"'“”‘’"


def get_admin_ids() -> set[int]:
    raw = (os.getenv("ADMIN_IDS") or "").strip()
    if not raw:
        return set()
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


ADMIN_IDS = get_admin_ids()


def db_init() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen_ts TEXT NOT NULL,
                last_seen_ts  TEXT NOT NULL
            )
            """
        )
        con.commit()


def touch_user(user_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row:
            con.execute("UPDATE users SET last_seen_ts = ? WHERE user_id = ?", (now, user_id))
        else:
            con.execute(
                "INSERT INTO users (user_id, first_seen_ts, last_seen_ts) VALUES (?, ?, ?)",
                (user_id, now, now),
            )
        con.commit()


def stats_counts() -> dict[str, int]:
    now = datetime.now(timezone.utc)
    dau_from = (now - timedelta(days=1)).isoformat()
    wau_from = (now - timedelta(days=7)).isoformat()
    mau_from = (now - timedelta(days=30)).isoformat()

    with sqlite3.connect(DB_PATH) as con:
        (dau,) = con.execute("SELECT COUNT(*) FROM users WHERE last_seen_ts >= ?", (dau_from,)).fetchone()
        (wau,) = con.execute("SELECT COUNT(*) FROM users WHERE last_seen_ts >= ?", (wau_from,)).fetchone()
        (mau,) = con.execute("SELECT COUNT(*) FROM users WHERE last_seen_ts >= ?", (mau_from,)).fetchone()
        (total,) = con.execute("SELECT COUNT(*) FROM users").fetchone()

    return {"dau": dau, "wau": wau, "mau": mau, "total": total}


def normalize_url(raw: str) -> str:
    # убираем мусор вокруг (скобки, запятые, точки)
    u = raw.strip(TRIM_CHARS)
    if u.lower().startswith(("http://", "https://")):
        return u
    return "https://" + u


def convert_instagram_url(url: str) -> str | None:
    """
    instagram.com → kkinstagram.com
    www.instagram.com → kkinstagram.com
    любые поддомены → kkinstagram.com
    """
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return None

        host = (p.netloc or "").lower()
        host_no_port = host.split("@")[-1].split(":")[0]

        if not (
            host_no_port == "instagram.com"
            or host_no_port.endswith(".instagram.com")
        ):
            return None

        # сохраняем порт если есть
        port_part = ""
        if ":" in host.split("@")[-1]:
            port_part = ":" + host.split("@")[-1].split(":", 1)[1]

        new_host = f"kkinstagram.com{port_part}"

        new_p = p._replace(netloc=new_host)
        return urlunparse(new_p)

    except Exception:
        return None


def convert_text(text: str) -> tuple[str, bool]:
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        raw = m.group(1)

        # сохраняем “хвост” пунктуации (чтобы "...,)" осталось на месте)
        left = raw.lstrip(TRIM_CHARS)
        right_trimmed = raw.rstrip(TRIM_CHARS)
        prefix = raw[: len(raw) - len(left)]
        suffix = raw[len(right_trimmed) :]

        core = raw.strip(TRIM_CHARS)
        normalized = normalize_url(core)
        new_url = convert_instagram_url(normalized)
        if new_url:
            changed = True
            return f"{prefix}{new_url}{suffix}"

        return raw

    return URL_RE.sub(repl, text), changed


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        touch_user(update.effective_user.id)
    await update.message.reply_text("Скинь ссылку на Instagram видео 👇")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    if ADMIN_IDS and user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return

    touch_user(user.id)
    s = stats_counts()

    await update.message.reply_text(
        "📊 <b>Bot stats</b>\n"
        f"DAU (24h): <b>{s['dau']}</b>\n"
        f"WAU (7d): <b>{s['wau']}</b>\n"
        f"MAU (30d): <b>{s['mau']}</b>\n"
        f"Total: <b>{s['total']}</b>",
        parse_mode=ParseMode.HTML,
    )


def brand_kb(bot_username: str) -> InlineKeyboardMarkup:
    bot_link = f"https://t.me/{bot_username}"
    text = (
        "🔥 Удобный бот для Instagram видео.\n"
        "Кидаешь ссылку — получаешь видео в Telegram.\n\n"
        f"👉 {bot_link}"
    )
    share_url = "https://t.me/share/url?url=" + quote_plus(bot_link) + "&text=" + quote_plus(text)

    kb = [
        [InlineKeyboardButton("⭐️ Поделиться ботом", url=share_url)],
        [InlineKeyboardButton("📢 Реклама / Сотрудничество", url="https://t.me/atobones")],
    ]
    return InlineKeyboardMarkup(kb)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return

    user = update.effective_user
    if user:
        touch_user(user.id)

    text = msg.text or msg.caption or ""
    if not text:
        return

    new_text, changed = convert_text(text)

    # Если Telegram прислал ссылку как entity text_link (когда текст кликабельный, но url спрятан)
    # — просто добавим поддержку: если нет изменений через regex, проверим entities.
    if not changed and msg.entities:
        for e in msg.entities:
            if e.type == "text_link" and e.url:
                normalized = normalize_url(e.url)
                new_url = convert_instagram_url(normalized)
                if new_url:
                    changed = True
                    new_text = new_url
                    break

    if changed:
        # Отправляем "пустой" текст со СКРЫТОЙ ссылкой, чтобы была только карточка превью.
        # \u2060 = word joiner (невидимый символ)
        hidden = f'<a href="{new_text}">\u2060</a>'
        me = await context.bot.get_me()
        bot_username = me.username or "InstaCleanerBot"

        await msg.reply_text(
            hidden,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
            reply_markup=brand_kb(bot_username),  # важно: превью должно быть включено
        )
        return


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN env var is missing (проверьте .env и что вы в папке проекта)")

    db_init()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
