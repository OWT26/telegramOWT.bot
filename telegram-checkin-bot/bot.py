"""
Telegram Check-In/Out Bot for 3 drivers (dispatcher workflow)

Stack: Python 3.10+, python-telegram-bot==21.*, SQLite (built-in), asyncio

Features
- /start: registers driver (restricted by a whitelist of phone numbers or secret PINs)
- Two big buttons: ✅ Check In and 🏁 Check Out
- Guided flow asking for: load ID, trailer #, location (GPS), odometer (optional), reefer setpoint/actual (optional), photos (POPs/BoLs), freeform notes
- Automatic timestamps in Europe/Chisinau
- Saves to SQLite (events table)
- Sends a formatted summary to a dispatcher chat (group/channel or your DM)
- /exportcsv to export last N days
- /drivers to see who is registered
- Admin-only setup: /setdispatch <chat_id>

Before you run
1) pip install python-telegram-bot==21.4 aiosqlite python-dotenv tzdata
2) Create .env next to this file with:
   BOT_TOKEN=123456:ABC... (from BotFather)
   ADMIN_IDS=111111111,222222222      # Telegram user IDs allowed as admins
   DRIVER_PINS=V1:1111,V2:2222,V3:3333  # map of “alias:pin” for your 3 drivers
   DISPATCH_CHAT_ID=0  # you can later set via /setdispatch
3) Start the bot: python bot.py
4) Add the bot to your dispatcher group (if you use a group) and promote it to see messages.

Notes
- Location is requested using Telegram’s native location attachment.
- Photos: accept 0..3 pictures; they’re saved as file_ids in DB; the dispatcher summary includes thumbnails.
- To change fields, edit PROMPTS and build_event_dict().

"""
import asyncio
import csv
import io
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import aiosqlite
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    AIORateLimiter,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

# -------------------- Config & Constants --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DISPATCH_CHAT_ID_ENV = int(os.getenv("DISPATCH_CHAT_ID", "0") or 0)

# Expected format: "Alias:PIN,Alias2:PIN2"
_driver_pins_raw = os.getenv("DRIVER_PINS", "").strip()
DRIVER_PINS: Dict[str, str] = {}
if _driver_pins_raw:
    for pair in _driver_pins_raw.split(","):
        if ":" in pair:
            alias, pin = pair.split( ":", 1 )
            DRIVER_PINS[alias.strip()] = pin.strip()

TZ = ZoneInfo("Europe/Chisinau")
DB_PATH = os.getenv("DB_PATH", "checkbot.sqlite3")

# Conversation states
(ASK_MODE, ASK_PIN, ASK_LOAD, ASK_TRAILER, ASK_LOCATION, ASK_ODOMETER,
 ASK_TEMP, ASK_PHOTOS, ASK_NOTES, CONFIRM) = range(10)

PROMPTS = {
    "welcome": "Привет! Выберите действие:",
    "ask_pin": "Введите ваш PIN, пожалуйста (4 цифры).",
    "bad_pin": "Неверный PIN. Попробуйте ещё раз.",
    "ask_load": "Номер загрузки / PO / BOL?",
    "ask_trailer": "Номер трейлера?",
    "ask_location": "Отправьте геолокацию кнопкой 📍 или отправьте текстом адрес",
    "ask_odometer": "Одометр (км), можно пропустить — отправьте 'пропуск'.",
    "ask_temp": "Температура рефера (set/actual), например 35F/36F. Можно 'пропуск'.",
    "ask_photos": "Фото (до 3 шт.). Когда достаточно, отправьте 'готово' или 'пропуск'.",
    "ask_notes": "Комментарии/заметки? Можно 'пропуск'.",
    "confirm": "Проверьте данные ниже. Отправить?",
    "saved": "✅ Сохранено и отправлено диспетчеру. Хорошей дороги!",
}

MAIN_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("✅ Check In"), KeyboardButton("🏁 Check Out")]],
    resize_keyboard=True,
)

LOCATION_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# -------------------- Data Models --------------------
@dataclass
class Driver:
    user_id: int
    alias: str

@dataclass
class Event:
    driver_alias: str
    user_id: int
    mode: str  # "IN" or "OUT"
    ts_local: str
    load_id: str
    trailer: str
    location: str  # either lat,lon or free text
    odometer: Optional[str]
    temp: Optional[str]
    photos: list[str]
    notes: Optional[str]

# -------------------- DB Helpers --------------------
INIT_SQL = """
CREATE TABLE IF NOT EXISTS drivers(
  user_id INTEGER PRIMARY KEY,
  alias TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_utc TEXT NOT NULL,
  ts_local TEXT NOT NULL,
  mode TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  driver_alias TEXT NOT NULL,
  load_id TEXT NOT NULL,
  trailer TEXT NOT NULL,
  location TEXT NOT NULL,
  odometer TEXT,
  temp TEXT,
  photos_json TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(INIT_SQL)
        await db.commit()

async def get_dispatch_chat_id() -> int:
    # ENV overrides DB default if set
    if DISPATCH_CHAT_ID_ENV:
        return DISPATCH_CHAT_ID_ENV
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key='dispatch_chat_id'") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

async def set_dispatch_chat_id(value: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES('dispatch_chat_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(value),),
        )
        await db.commit()

async def register_driver(user_id: int, alias: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO drivers(user_id, alias) VALUES(?, ?)",
            (user_id, alias),
        )
        await db.commit()

async def get_driver(user_id: int) -> Optional[Driver]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, alias FROM drivers WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            if row:
                return Driver(user_id=row[0], alias=row[1])
            return None

async def insert_event(ev: Event) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO events(
              created_utc, ts_local, mode, user_id, driver_alias, load_id, trailer,
              location, odometer, temp, photos_json, notes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                ev.ts_local,
                ev.mode,
                ev.user_id,
                ev.driver_alias,
                ev.load_id,
                ev.trailer,
                ev.location,
                ev.odometer,
                ev.temp,
                json.dumps(ev.photos),
                ev.notes,
            ),
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            row = await cur.fetchone()
            return int(row[0])

# -------------------- Utilities --------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def ensure_registered(update: Update, context: CallbackContext) -> Driver:
    driver = await get_driver(update.effective_user.id)
    if driver:
        return driver
    # ask for PIN
    await update.effective_chat.send_message(PROMPTS["ask_pin"], reply_markup=ReplyKeyboardRemove())
    context.user_data["awaiting_pin"] = True
    raise RuntimeError("PIN_REQUIRED")

async def check_pin_and_register(update: Update, context: CallbackContext) -> Optional[Driver]:
    text = (update.message.text or "").strip()
    for alias, pin in DRIVER_PINS.items():
        if text == pin:
            await register_driver(update.effective_user.id, alias)
            await update.message.reply_text(f"Принято, {alias}!", reply_markup=MAIN_KB)
            return Driver(user_id=update.effective_user.id, alias=alias)
    await update.message.reply_text(PROMPTS["bad_pin"])
    return None

# Build a human-readable location string
def format_location(update: Update) -> Tuple[str, Optional[Tuple[float, float]]]:
    if update.message.location:
        lat = update.message.location.latitude
        lon = update.message.location.longitude
        return (f"{lat:.6f},{lon:.6f}", (lat, lon))
    return (update.message.text.strip() if update.message and update.message.text else "", None)

# -------------------- Handlers --------------------
async def cmd_start(update: Update, context: CallbackContext):
    user = update.effective_user
    driver = await get_driver(user.id)
    if driver:
        await update.message.reply_text(
            f"Здравствуйте, {driver.alias}!", reply_markup=MAIN_KB
        )
        return ConversationHandler.END
    else:
        await update.message.reply_text(PROMPTS["ask_pin"], reply_markup=ReplyKeyboardRemove())
        context.user_data["awaiting_pin"] = True
        return ASK_PIN

async def pin_entry(update: Update, context: CallbackContext):
    driver = await check_pin_and_register(update, context)
    if driver:
        await update.message.reply_text(PROMPTS["welcome"], reply_markup=MAIN_KB)
        return ASK_MODE
    return ASK_PIN

async def choose_mode(update: Update, context: CallbackContext):
    text = (update.message.text or "").lower()
    if "check in" in text:
        context.user_data["mode"] = "IN"
    elif "check out" in text:
        context.user_data["mode"] = "OUT"
    else:
        await update.message.reply_text("Нажмите одну из кнопок ниже.", reply_markup=MAIN_KB)
        return ASK_MODE

    await update.message.reply_text(PROMPTS["ask_load"], reply_markup=ReplyKeyboardRemove())
    return ASK_LOAD

async def ask_load(update: Update, context: CallbackContext):
    context.user_data["load_id"] = (update.message.text or "").strip()
    await update.message.reply_text(PROMPTS["ask_trailer"])
    return ASK_TRAILER

async def ask_trailer(update: Update, context: CallbackContext):
    context.user_data["trailer"] = (update.message.text or "").strip()
    await update.message.reply_text(PROMPTS["ask_location"], reply_markup=LOCATION_KB)
    return ASK_LOCATION

async def ask_location(update: Update, context: CallbackContext):
    loc_str, coords = format_location(update)
    context.user_data["location"] = loc_str
    context.user_data["coords"] = coords
    await update.message.reply_text(PROMPTS["ask_odometer"], reply_markup=ReplyKeyboardRemove())
    return ASK_ODOMETER

async def ask_odometer(update: Update, context: CallbackContext):
    text = (update.message.text or "").strip()
    context.user_data["odometer"] = None if text.lower() == "пропуск" else text
    await update.message.reply_text(PROMPTS["ask_temp"])
    return ASK_TEMP

async def ask_temp(update: Update, context: CallbackContext):
    text = (update.message.text or "").strip()
    context.user_data["temp"] = None if text.lower() == "пропуск" else text
    context.user_data["photos"] = []
    await update.message.reply_text(PROMPTS["ask_photos"])
    return ASK_PHOTOS

async def ask_photos_photo(update: Update, context: CallbackContext):
    # get best resolution file id
    largest = update.message.photo[-1]
    file_id = largest.file_id
    photos = context.user_data.get("photos", [])
    if len(photos) < 3:
        photos.append(file_id)
        context.user_data["photos"] = photos
        await update.message.reply_text(
            f"Фото сохранено ({len(photos)}/3). Добавьте ещё или напишите 'готово'."
        )
    else:
        await update.message.reply_text("Уже 3 фото. Напишите 'готово' или 'пропуск'.")
    return ASK_PHOTOS

async def ask_photos_done(update: Update, context: CallbackContext):
    await update.message.reply_text(PROMPTS["ask_notes"])
    return ASK_NOTES

async def ask_notes(update: Update, context: CallbackContext):
    text = (update.message.text or "").strip()
    context.user_data["notes"] = None if text.lower() == "пропуск" else text

    # Build summary for confirmation
    driver = await get_driver(update.effective_user.id)
    mode = context.user_data["mode"]
    ts_local = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

    summary = (
        f"<b>{'CHECK IN' if mode=='IN' else 'CHECK OUT'}</b>\n"
        f"Время: <code>{ts_local}</code>\n"
        f"Водитель: <b>{driver.alias}</b>\n"
        f"Load: <code>{context.user_data['load_id']}</code>\n"
        f"Trailer: <code>{context.user_data['trailer']}</code>\n"
        f"Location: <code>{context.user_data['location']}</code>\n"
        f"Odometer: <code>{context.user_data.get('odometer') or '-'}"
        f"</code>\nTemp: <code>{context.user_data.get('temp') or '-'}"
        f"</code>\nNotes: <i>{context.user_data.get('notes') or '-'}</i>"
    )

    kb = ReplyKeyboardMarkup([["Отправить", "Отмена"]], resize_keyboard=True)
    await update.message.reply_html(summary, reply_markup=kb)
    context.user_data["ts_local"] = ts_local
    return CONFIRM

async def confirm(update: Update, context: CallbackContext):
    text = (update.message.text or "").lower()
    if "отмена" in text:
        await update.message.reply_text("Отменено.", reply_markup=MAIN_KB)
        return ConversationHandler.END

    if "отправить" not in text:
        await update.message.reply_text("Выберите: Отправить / Отмена")
        return CONFIRM

    driver = await get_driver(update.effective_user.id)
    mode = context.user_data["mode"]
    ev = Event(
        driver_alias=driver.alias,
        user_id=driver.user_id,
        mode=mode,
        ts_local=context.user_data["ts_local"],
        load_id=context.user_data["load_id"],
        trailer=context.user_data["trailer"],
        location=context.user_data["location"],
        odometer=context.user_data.get("odometer"),
        temp=context.user_data.get("temp"),
        photos=context.user_data.get("photos", []),
        notes=context.user_data.get("notes"),
    )
    event_id = await insert_event(ev)

    # Notify dispatcher chat
    dispatch_chat_id = await get_dispatch_chat_id()
    summary = (
        f"<b>#{event_id}</b> — <b>{'CHECK IN' if mode=='IN' else 'CHECK OUT'}</b>\n"
        f"⏰ <code>{ev.ts_local}</code>\n"
        f"👤 <b>{ev.driver_alias}</b> (id {ev.user_id})\n"
        f"📦 Load: <code>{ev.load_id}</code>\n"
        f"🚛 Trailer: <code>{ev.trailer}</code>\n"
        f"📍 Location: <code>{ev.location}</code>\n"
        f"📈 Odometer: <code>{ev.odometer or '-'}"
        f"</code>\n🌡️ Reefer: <code>{ev.temp or '-'}"
        f"</code>\n📝 Notes: <i>{ev.notes or '-'}</i>"
    )

    # Send to driver (confirmation) and dispatcher (summary + photos)
    await update.message.reply_html(PROMPTS["saved"], reply_markup=MAIN_KB)

    if dispatch_chat_id:
        await context.bot.send_message(dispatch_chat_id, summary, parse_mode=ParseMode.HTML)
        if ev.photos:
            media = [InputMediaPhoto(p) for p in ev.photos[:10]]
            # send as album
            await context.bot.send_media_group(dispatch_chat_id, media)

    context.user_data.clear()
    return ConversationHandler.END

# -------------------- Admin commands --------------------
async def cmd_setdispatch(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setdispatch <chat_id>")
        return
    chat_id = int(context.args[0])
    await set_dispatch_chat_id(chat_id)
    await update.message.reply_text(f"Dispatch chat set to {chat_id}")

async def cmd_drivers(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, alias FROM drivers ORDER BY alias") as cur:
            rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text("No drivers yet.")
        return
    text = "\n".join([f"{alias}: {uid}" for uid, alias in rows])
    await update.message.reply_text(text)

async def cmd_exportcsv(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        return
    days = int(context.args[0]) if context.args else 14
    cutoff_utc = datetime.now(timezone.utc).timestamp() - days * 86400

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, created_utc, ts_local, mode, user_id, driver_alias, load_id, trailer, location, odometer, temp, photos_json, notes FROM events"
        ) as cur:
            rows = await cur.fetchall()

    # Filter by created_utc cutoff
    filtered = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r[1]).timestamp()
        except Exception:
            t = 0
        if t >= cutoff_utc:
            filtered.append(r)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id","created_utc","ts_local","mode","user_id","driver_alias","load_id",
        "trailer","location","odometer","temp","photos_json","notes"
    ])
    for r in filtered:
        writer.writerow(r)

    output.seek(0)
    await update.message.reply_document(
        document=io.BytesIO(output.getvalue().encode("utf-8")),
        filename=f"events_last_{days}d.csv",
        caption=f"Exported {len(filtered)} events"
    )

# -------------------- Fallbacks --------------------
async def fallback_text(update: Update, context: CallbackContext):
    # If awaiting PIN
    if context.user_data.get("awaiting_pin"):
        driver = await check_pin_and_register(update, context)
        if driver:
            context.user_data.pop("awaiting_pin", None)
            await update.message.reply_text(PROMPTS["welcome"], reply_markup=MAIN_KB)
            return ASK_MODE
        return ASK_PIN
    await update.message.reply_text("Не понял. Нажмите кнопку или используйте команды.")
    return ConversationHandler.END

async def fallback_location(update: Update, context: CallbackContext):
    # handle location outside flow
    await update.message.reply_text("Локация принята.")

# -------------------- Main --------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing in environment")

    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, pin_entry)],
            ASK_MODE: [MessageHandler(filters.Regex("^(✅ Check In|🏁 Check Out)$"), choose_mode)],
            ASK_LOAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_load)],
            ASK_TRAILER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_trailer)],
            ASK_LOCATION: [
                MessageHandler(filters.LOCATION, ask_location),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_location),
            ],
            ASK_ODOMETER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_odometer)],
            ASK_TEMP: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_temp)],
            ASK_PHOTOS: [
                MessageHandler(filters.PHOTO, ask_photos_photo),
                MessageHandler(filters.Regex(r"(?i)^(готово|пропуск)$") | (filters.TEXT & ~filters.COMMAND), ask_photos_done),
            ],
            ASK_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_notes)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
        },
        fallbacks=[MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text), MessageHandler(filters.LOCATION, fallback_location)],
        allow_reentry=True,
    )

    # Admin commands
    app.add_handler(CommandHandler("setdispatch", cmd_setdispatch))
    app.add_handler(CommandHandler("drivers", cmd_drivers))
    app.add_handler(CommandHandler("exportcsv", cmd_exportcsv))

    app.add_handler(conv)

    await app.initialize()
    await app.start()
    print("Bot started. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await app.stop()
        await app.shutdown()

# Small regex import used in ConversationHandler
import re

if __name__ == "__main__":
    asyncio.run(main())
