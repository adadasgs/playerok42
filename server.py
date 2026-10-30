
import os
import json
import hmac
import hashlib
import asyncio
import sqlite3
import time
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
DB_PATH = os.getenv("DB_PATH", "playerok.db")

bot = None
dp = Dispatcher()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER NOT NULL,
            buyer_id INTEGER,
            seller_id INTEGER,
            creator_role TEXT NOT NULL,
            item TEXT NOT NULL,
            description TEXT DEFAULT '',
            price REAL NOT NULL,
            currency TEXT DEFAULT 'TON',
            status TEXT DEFAULT 'waiting',
            created_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS support (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            reply TEXT,
            created_at INTEGER
        );
        """)


def verify_init_data(init_data: str):
    if not BOT_TOKEN or not init_data:
        raise HTTPException(401, "Telegram authorization required")

    values = dict(parse_qsl(init_data))
    received_hash = values.pop("hash", None)

    if not received_hash:
        raise HTTPException(401, "Invalid Telegram data")

    auth_date = int(values.get("auth_date", "0"))
    if time.time() - auth_date > 86400:
        raise HTTPException(401, "Telegram session expired")

    data_check = "\n".join(
        f"{k}={v}" for k, v in sorted(values.items())
    )

    secret = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256
    ).digest()

    calculated = hmac.new(
        secret,
        data_check.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Telegram authorization failed")

    try:
        return json.loads(values["user"])
    except Exception:
        raise HTTPException(401, "Invalid Telegram user")


def get_user(init_data: str):
    user = verify_init_data(init_data)
    uid = int(user["id"])

    with db() as c:
        c.execute("""
            INSERT INTO users (id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (uid, user.get("username"), user.get("first_name")))

    return user


async def notify(uid: int, text: str, keyboard=None):
    if not bot:
        return False
    try:
        await bot.send_message(uid, text, reply_markup=keyboard)
        return True
    except Exception:
        return False


@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id

    with db() as c:
        c.execute("""
            INSERT INTO users (id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (uid, message.from_user.username,
              message.from_user.first_name))

    await message.answer(
        "Добро пожаловать в Playerok!\n"
        "Теперь бот может отправлять тебе уведомления о сделках.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Открыть Playerok",
                web_app={"url": WEBAPP_URL}
            )
        ]]) if WEBAPP_URL.startswith("https://") else None
    )


@dp.callback_query()
async def deal_callback(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[0] != "invite":
        await callback.answer()
        return

    action, deal_id = parts[1], int(parts[2])
    uid = callback.from_user.id

    with db() as c:
        deal = c.execute(
            "SELECT * FROM deals WHERE id=?", (deal_id,)
        ).fetchone()

        if not deal:
            await callback.answer("Сделка не найдена", show_alert=True)
            return

        if deal["status"] != "waiting":
            await callback.answer("Приглашение уже неактивно", show_alert=True)
            return

        if uid == deal["creator_id"]:
            await callback.answer("Нельзя присоединиться к своей сделке", show_alert=True)
            return

        if action == "accept":
            role = "seller" if deal["creator_role"] == "buyer" else "buyer"
            column = "seller_id" if role == "seller" else "buyer_id"

            c.execute(
                f"UPDATE deals SET {column}=?, status='joined' WHERE id=?",
                (uid, deal_id)
            )
            await callback.message.edit_text(
                f"Ты присоединился к сделке №{deal_id} как {role}."
            )
            await notify(
                deal["creator_id"],
                f"Участник присоединился к сделке №{deal_id} как {role}."
            )
            await callback.answer("Ты присоединился!")
        elif action == "decline":
            c.execute(
                "UPDATE deals SET status='declined' WHERE id=?",
                (deal_id,)
            )
            await callback.message.edit_text("Приглашение отклонено.")
            await notify(
                deal["creator_id"],
                f"Приглашение в сделку №{deal_id} отклонено."
            )
            await callback.answer("Приглашение отклонено")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    init_db()

    if BOT_TOKEN:
        bot = Bot(BOT_TOKEN)
        asyncio.create_task(dp.start_polling(bot))

    yield

    if bot:
        await bot.session.close()


app = FastAPI(title="Playerok API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class DealCreate(BaseModel):
    item: str
    description: str = ""
    price: float
    currency: str = "TON"
    role: str


class DealJoin(BaseModel):
    deal_id: int


class SupportMessage(BaseModel):
    message: str


class SupportReply(BaseModel):
    support_id: int
    reply: str


@app.get("/")
async def home():
    return {"service": "Playerok", "status": "running"}


@app.get("/api/me")
async def me(x_telegram_init_data: str = Header(default="")):
    user = get_user(x_telegram_init_data)

    with db() as c:
        row = c.execute(
            "SELECT balance FROM users WHERE id=?",
            (user["id"],)
        ).fetchone()

    return {
        "id": user["id"],
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "balance": row["balance"] if row else 0
    }


@app.post("/api/deals")
async def create_deal(
    data: DealCreate,
    x_telegram_init_data: str = Header(default="")
):
    user = get_user(x_telegram_init_data)

    if data.role not in ("buyer", "seller"):
        raise HTTPException(400, "Role must be buyer or seller")

    if not data.item.strip() or data.price <= 0:
        raise HTTPException(400, "Invalid item or price")

    with db() as c:
        cur = c.execute("""
            INSERT INTO deals
            (creator_id, creator_role, item, description, price,
             currency, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user["id"], data.role, data.item.strip(),
            data.description, data.price,
            data.currency, int(time.time())
        ))
        deal_id = cur.lastrowid

    await notify(
        user["id"],
        f"Сделка №{deal_id} создана.\n"
        f"Товар: {data.item}\nЦена: {data.price} {data.currency}"
    )

    return {
        "success": True,
        "deal_id": deal_id,
        "status": "waiting",
        "share_code": str(deal_id)
    }


@app.get("/api/deals")
async def list_deals(
    x_telegram_init_data: str = Header(default="")
):
    user = get_user(x_telegram_init_data)

    with db() as c:
        rows = c.execute("""
            SELECT * FROM deals
            WHERE creator_id=? OR buyer_id=? OR seller_id=?
            ORDER BY id DESC
        """, (user["id"], user["id"], user["id"])).fetchall()

    return [dict(row) for row in rows]


@app.get("/api/deals/{deal_id}")
async def get_deal(
    deal_id: int,
    x_telegram_init_data: str = Header(default="")
):
    user = get_user(x_telegram_init_data)

    with db() as c:
        deal = c.execute(
            "SELECT * FROM deals WHERE id=?", (deal_id,)
        ).fetchone()

    if not deal:
        raise HTTPException(404, "Deal not found")

    if user["id"] not in (
        deal["creator_id"], deal["buyer_id"], deal["seller_id"]
    ):
        raise HTTPException(403, "Not a deal participant")

    return dict(deal)


@app.post("/api/deals/join")
async def join_deal(
    data: DealJoin,
    x_telegram_init_data: str = Header(default="")
):
    user = get_user(x_telegram_init_data)

    with db() as c:
        deal = c.execute(
            "SELECT * FROM deals WHERE id=?", (data.deal_id,)
        ).fetchone()

        if not deal:
            raise HTTPException(404, "Deal not found")

        if deal["creator_id"] == user["id"]:
            raise HTTPException(400, "You cannot join your own deal")

        if deal["status"] != "waiting":
            raise HTTPException(400, "Deal is not available")

        role = "seller" if deal["creator_role"] == "buyer" else "buyer"
        column = "seller_id" if role == "seller" else "buyer_id"

        c.execute(
            f"UPDATE deals SET {column}=?, status='joined' WHERE id=?",
            (user["id"], data.deal_id)
        )

    await notify(
        deal["creator_id"],
        f"Новый участник присоединился к сделке №{data.deal_id} как {role}."
    )

    return {"success": True, "role": role, "status": "joined"}


@app.post("/api/deals/{deal_id}/invite")
async def invite_deal(
    deal_id: int,
    x_telegram_init_data: str = Header(default="")
):
    user = get_user(x_telegram_init_data)

    with db() as c:
        deal = c.execute(
            "SELECT * FROM deals WHERE id=?", (deal_id,)
        ).fetchone()

    if not deal or deal["creator_id"] != user["id"]:
        raise HTTPException(404, "Deal not found")

    if not bot or not WEBAPP_URL.startswith("https://"):
        raise HTTPException(503, "Bot or Mini App URL is not configured")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Принять",
                callback_data=f"invite:accept:{deal_id}"
            ),
            InlineKeyboardButton(
                text="Отклонить",
                callback_data=f"invite:decline:{deal_id}"
            )
        ]
    ])

    return {
        "success": True,
        "share_text": (
            f"Приглашаю в сделку Playerok №{deal_id}\n"
            f"{deal['item']} — {deal['price']} {deal['currency']}\n"
            f"{WEBAPP_URL}?startapp=deal_{deal_id}"
        ),
        "message": "Скопируй ссылку и отправь участнику. "
                   "Для личного приглашения бот должен знать его Telegram ID."
    }


@app.post("/api/deals/{deal_id}/cancel")
async def cancel_deal(
    deal_id: int,
    x_telegram_init_data: str = Header(default="")
):
    user = get_user(x_telegram_init_data)

    with db() as c:
        deal = c.execute(
            "SELECT * FROM deals WHERE id=?", (deal_id,)
        ).fetchone()

        if not deal or deal["creator_id"] != user["id"]:
            raise HTTPException(404, "Deal not found")

        if deal["status"] in ("completed", "cancelled"):
            raise HTTPException(400, "Deal already closed")

        c.execute(
            "UPDATE deals SET status='cancelled' WHERE id=?",
            (deal_id,)
        )

    for uid in (deal["buyer_id"], deal["seller_id"]):
        if uid:
            await notify(uid, f"Сделка №{deal_id} отменена.")

    return {"success": True, "status": "cancelled"}


@app.post("/api/support")
async def send_support(
    data: SupportMessage,
    x_telegram_init_data: str = Header(default="")
):
    user = get_user(x_telegram_init_data)

    with db() as c:
        cur = c.execute("""
            INSERT INTO support (user_id, message, created_at)
            VALUES (?, ?, ?)
        """, (user["id"], data.message, int(time.time())))
        support_id = cur.lastrowid

    for admin_id in ADMIN_IDS:
        await notify(
            admin_id,
            f"Новое обращение №{support_id}\n"
            f"Пользователь: {user['id']}\n{data.message}"
        )

    return {"success": True, "support_id": support_id}


@app.post("/api/admin/support/reply")
async def reply_support(
    data: SupportReply,
    x_telegram_init_data: str = Header(default="")
):
    user = get_user(x_telegram_init_data)

    if user["id"] not in ADMIN_IDS:
        raise HTTPException(403, "Admin only")

    with db() as c:
        ticket = c.execute(
            "SELECT * FROM support WHERE id=?",
            (data.support_id,)
        ).fetchone()

        if not ticket:
            raise HTTPException(404, "Support message not found")

        c.execute(
            "UPDATE support SET reply=? WHERE id=?",
            (data.reply, data.support_id)
        )

    await notify(ticket["user_id"], f"Ответ поддержки:\n{data.reply}")

    return {"success": True}


@app.get("/api/admin/deals")
async def admin_deals(
    x_telegram_init_data: str = Header(default="")
):
    user = get_user(x_telegram_init_data)

    if user["id"] not in ADMIN_IDS:
        raise HTTPException(403, "Admin only")

    with db() as c:
        rows = c.execute(
            "SELECT * FROM deals ORDER BY id DESC"
        ).fetchall()

    return [dict(row) for row in rows]
