import asyncio
import os
import json
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, Awaitable, Callable

import aiosqlite
from openai import AsyncOpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
DB_PATH = "manager_bot.db"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")
ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

if not BOT_TOKEN:
    raise RuntimeError("В .env не указан BOT_TOKEN")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=str(TZ))


# =======================
# FSM
# =======================

class TaskState(StatesGroup):
    title = State()
    assignee = State()
    due_minutes = State()


class KegState(StatesGroup):
    beer_name = State()
    shelf_days = State()


class SettingsState(StatesGroup):
    value = State()


class AIState(StatesGroup):
    chat = State()


class PenaltyState(StatesGroup):
    user = State()
    points = State()
    reason = State()


# =======================
# DB / helpers
# =======================

def db():
    return aiosqlite.connect(DB_PATH)


def now_dt() -> datetime:
    return datetime.now(TZ).replace(microsecond=0)


def now_iso() -> str:
    return now_dt().isoformat()


def today_str() -> str:
    return now_dt().date().isoformat()


async def column_exists(conn, table: str, column: str) -> bool:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    return any(row[1] == column for row in rows)


async def add_column_if_missing(conn, table: str, column: str, ddl: str):
    if not await column_exists(conn, table, column):
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


async def init_db():
    async with db() as conn:
        await conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            role TEXT DEFAULT 'worker',
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            assignee_id INTEGER,
            status TEXT DEFAULT 'new',
            created_by INTEGER,
            created_at TEXT,
            updated_at TEXT,
            due_at TEXT,
            last_remind_at TEXT
        );

        CREATE TABLE IF NOT EXISTS task_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            user_id INTEGER,
            action TEXT,
            comment TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS kegs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            beer_name TEXT NOT NULL,
            opened_at TEXT,
            shelf_days INTEGER,
            expires_at TEXT,
            status TEXT DEFAULT 'open',
            closed_at TEXT,
            created_by INTEGER
        );

        CREATE TABLE IF NOT EXISTS action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            details TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS ai_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS ai_pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS karma_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_by INTEGER,
            task_id INTEGER,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS day_moods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            mood TEXT NOT NULL,
            score INTEGER NOT NULL,
            comment TEXT,
            day TEXT NOT NULL,
            created_at TEXT
        );
        """)

        # Миграции для старой базы
        await add_column_if_missing(conn, "users", "approved", "INTEGER DEFAULT 0")
        await add_column_if_missing(conn, "users", "banned", "INTEGER DEFAULT 0")
        await add_column_if_missing(conn, "users", "approved_at", "TEXT")
        await add_column_if_missing(conn, "users", "approved_by", "INTEGER")
        await add_column_if_missing(conn, "tasks", "penalty_applied", "INTEGER DEFAULT 0")

        defaults = {
            "daily_task_check_time": "19:00",
            "evening_keg_question_time": "20:00",
            "morning_priority_time": "09:00",
            "task_remind_minutes": "60",
            "keg_warning_days": "2",
            "notify_admins": "1",

            # Новое
            "morning_compliments_enabled": "1",
            "morning_compliments_time": "07:00",
            "mood_enabled": "1",
            "mood_question_time": "22:00",
            "penalties_enabled": "1",
            "auto_overdue_penalty": "5",
            "secret_button_enabled": "1",
        }
        for k, v in defaults.items():
            await conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

        # Админы всегда одобрены
        for admin_id in ADMIN_IDS:
            await conn.execute("""
            INSERT INTO users(user_id, username, full_name, role, created_at, approved, banned, approved_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET role='admin', approved=1, banned=0
            """, (admin_id, "", f"Admin {admin_id}", "admin", now_iso(), 1, 0, now_iso()))

        await conn.commit()


async def get_setting(key: str) -> str:
    async with db() as conn:
        cur = await conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else ""


async def set_setting_value(key: str, value: str):
    async with db() as conn:
        await conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
        await conn.commit()


async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def is_approved(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    async with db() as conn:
        cur = await conn.execute("SELECT approved,banned FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    return bool(row and row[0] == 1 and row[1] == 0)


async def log_action(user_id: int, action: str, details: str = ""):
    async with db() as conn:
        await conn.execute(
            "INSERT INTO action_log(user_id,action,details,created_at) VALUES(?,?,?,?)",
            (user_id, action, details, now_iso())
        )
        await conn.commit()


async def add_karma(user_id: int, delta: int, reason: str, created_by: int | None = None, task_id: int | None = None):
    if str(await get_setting("penalties_enabled")) != "1":
        return
    async with db() as conn:
        await conn.execute("""
        INSERT INTO karma_events(user_id,delta,reason,created_by,task_id,created_at)
        VALUES(?,?,?,?,?,?)
        """, (user_id, delta, reason, created_by, task_id, now_iso()))
        await conn.commit()


def parse_time(value: str):
    h, m = value.split(":")
    return int(h), int(m)


def bool_text(value: str) -> str:
    return "вкл" if str(value) == "1" else "выкл"


# =======================
# Keyboards
# =======================

def main_menu(is_admin_user=False):
    kb = ReplyKeyboardBuilder()
    kb.button(text="📋 Задачи")
    kb.button(text="🍺 Кеги")
    kb.button(text="📊 Отчёты")
    kb.button(text="🕘 История")
    kb.button(text="🤖 ИИ управляющий")
    kb.button(text="🦊 Секретная кнопка")
    if is_admin_user:
        kb.button(text="👥 Пользователи")
        kb.button(text="⚖️ Штрафы")
        kb.button(text="⚙️ Настройки")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


def cancel_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text="↩️ Назад")
    kb.button(text="❌ Отмена")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


def back_inline(callback_data="back:main"):
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Назад", callback_data=callback_data)
    kb.adjust(1)
    return kb.as_markup()


def tasks_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить задачу", callback_data="task:add")
    kb.button(text="📋 Активные", callback_data="task:list_active")
    kb.button(text="✅ Выполненные", callback_data="task:list_done")
    kb.button(text="👤 Мои задачи", callback_data="task:my")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    return kb.as_markup()


def kegs_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="🍺 Открыть кегу", callback_data="keg:open")
    kb.button(text="📋 Открытые кеги", callback_data="keg:list_open")
    kb.button(text="⚠️ Скоро просрочка", callback_data="keg:priority")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    return kb.as_markup()


def task_buttons(task_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сделано", callback_data=f"taskstatus:{task_id}:done")
    kb.button(text="⏳ В процессе", callback_data=f"taskstatus:{task_id}:process")
    kb.button(text="❌ Не могу", callback_data=f"taskstatus:{task_id}:cant")
    kb.adjust(1)
    return kb.as_markup()


def keg_buttons(keg_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Кега закончилась/закрыть", callback_data=f"kegclose:{keg_id}")
    kb.button(text="↩️ Назад", callback_data="keg:list_open")
    kb.adjust(1)
    return kb.as_markup()


def settings_menu():
    kb = InlineKeyboardBuilder()
    items = [
        ("Вечерняя проверка задач", "daily_task_check_time"),
        ("Вечерний вопрос по кегам", "evening_keg_question_time"),
        ("Утренний список пива", "morning_priority_time"),
        ("Повтор задачи, минут", "task_remind_minutes"),
        ("За сколько дней пиво в приоритет", "keg_warning_days"),
        ("Комплименты утром: вкл/выкл", "morning_compliments_enabled"),
        ("Время комплиментов", "morning_compliments_time"),
        ("Оценка дня: вкл/выкл", "mood_enabled"),
        ("Время оценки дня", "mood_question_time"),
        ("Штрафы: вкл/выкл", "penalties_enabled"),
        ("Автоштраф за просрочку", "auto_overdue_penalty"),
        ("Секретная кнопка: вкл/выкл", "secret_button_enabled"),
    ]
    for title, key in items:
        kb.button(text=title, callback_data=f"set:{key}")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    return kb.as_markup()


def ai_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Начать разговор с ИИ", callback_data="ai:start")
    kb.button(text="📊 Анализ магазина", callback_data="ai:analyze")
    kb.button(text="🧹 Очистить память ИИ", callback_data="ai:clear")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    return kb.as_markup()


def ai_chat_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text="❌ Закончить разговор с ИИ")
    kb.button(text="↩️ Назад")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True)


def ai_action_buttons(action_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выполнить", callback_data=f"ai_action:{action_id}:yes")
    kb.button(text="❌ Отмена", callback_data=f"ai_action:{action_id}:no")
    kb.adjust(2)
    return kb.as_markup()


def users_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="⏳ Заявки на доступ", callback_data="users:pending")
    kb.button(text="✅ Одобренные", callback_data="users:approved")
    kb.button(text="🚫 Забаненные", callback_data="users:banned")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    return kb.as_markup()


def user_manage_buttons(user_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"user:approve:{user_id}")
    kb.button(text="🚫 Удалить/забанить", callback_data=f"user:ban:{user_id}")
    kb.button(text="↩️ Назад", callback_data="users:approved")
    kb.adjust(1)
    return kb.as_markup()


def penalty_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="➖ Выдать штраф", callback_data="penalty:add")
    kb.button(text="🏆 Рейтинг кармы", callback_data="penalty:rating")
    kb.button(text="📜 История штрафов", callback_data="penalty:history")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    return kb.as_markup()


def mood_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔥 Отличный", callback_data="mood:great:3")
    kb.button(text="🙂 Норм", callback_data="mood:good:2")
    kb.button(text="😐 Такое", callback_data="mood:ok:1")
    kb.button(text="💀 Ужас", callback_data="mood:bad:0")
    kb.adjust(2)
    return kb.as_markup()


def secret_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="❤️ Поднять настроение", callback_data="secret:compliment")
    kb.button(text="🔮 Пивной оракул", callback_data="secret:beer_oracle")
    kb.button(text="👑 Наталья всегда права", callback_data="secret:natalie")
    kb.button(text="🎲 Что делать?", callback_data="secret:random_task")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    return kb.as_markup()


# =======================
# Middleware auth
# =======================

class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        # Админам всегда можно
        if user.id in ADMIN_IDS:
            return await handler(event, data)

        # /start и /id разрешаем, чтобы отправить заявку и узнать id
        if isinstance(event, Message):
            text = event.text or ""
            if text.startswith("/start") or text.startswith("/id"):
                return await handler(event, data)

        approved = await is_approved(user.id)
        if approved:
            return await handler(event, data)

        # Неодобренным блокируем все кроме /start
        if isinstance(event, Message):
            await event.answer("⏳ Доступ ещё не одобрен админом. Попроси админа подтвердить регистрацию.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Доступ ещё не одобрен.", show_alert=True)
        return None


dp.message.middleware(AccessMiddleware())
dp.callback_query.middleware(AccessMiddleware())


# =======================
# User registration
# =======================

async def save_user(message: Message):
    u = message.from_user
    admin = await is_admin(u.id)
    role = "admin" if admin else "worker"
    approved = 1 if admin else 0
    async with db() as conn:
        await conn.execute("""
        INSERT INTO users(user_id, username, full_name, role, created_at, approved, banned, approved_at)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name,
            role=CASE WHEN excluded.role='admin' THEN 'admin' ELSE users.role END,
            banned=CASE WHEN users.banned=1 THEN 1 ELSE 0 END,
            approved=CASE WHEN excluded.approved=1 THEN 1 ELSE users.approved END
        """, (u.id, u.username or "", u.full_name, role, now_iso(), approved, 0, now_iso() if admin else None))
        await conn.commit()


async def notify_admins_about_request(user_id: int, full_name: str, username: str | None):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"user:approve:{user_id}")
    kb.button(text="🚫 Отклонить", callback_data=f"user:ban:{user_id}")
    kb.adjust(2)
    text = f"👤 Новая заявка на доступ:\n\n{full_name}\nID: {user_id}"
    if username:
        text += f"\n@{username}"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=kb.as_markup())
        except Exception:
            pass


@dp.message(CommandStart())
async def start(message: Message):
    await save_user(message)
    admin = await is_admin(message.from_user.id)
    if admin:
        return await message.answer(
            "Готово. Я менеджер задач, кег и настроения 🍺🦊\nВыбери раздел в меню.",
            reply_markup=main_menu(True)
        )

    if await is_approved(message.from_user.id):
        return await message.answer(
            "Доступ одобрен ✅\nВыбери раздел в меню.",
            reply_markup=main_menu(False)
        )

    await notify_admins_about_request(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await message.answer("⏳ Заявка отправлена админу. После одобрения бот откроет меню.")


@dp.message(Command("id"))
async def my_id(message: Message):
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")


# =======================
# Back / cancel
# =======================

@dp.message(F.text.in_({"↩️ Назад", "❌ Отмена"}))
async def cancel_or_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, вернулись в главное меню.", reply_markup=main_menu(await is_admin(message.from_user.id)))


@dp.callback_query(F.data == "back:main")
async def back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Главное меню:", reply_markup=main_menu(await is_admin(cb.from_user.id)))
    await cb.answer()


# =======================
# Main menus
# =======================

@dp.message(F.text == "📋 Задачи")
async def show_tasks(message: Message):
    await message.answer("Раздел задач:", reply_markup=tasks_menu())


@dp.message(F.text == "🍺 Кеги")
async def show_kegs(message: Message):
    await message.answer("Раздел кег:", reply_markup=kegs_menu())


@dp.message(F.text == "⚙️ Настройки")
async def show_settings(message: Message):
    if not await is_admin(message.from_user.id):
        return await message.answer("Настройки доступны только админу.")
    async with db() as conn:
        cur = await conn.execute("SELECT key,value FROM settings ORDER BY key")
        rows = await cur.fetchall()
    text = "⚙️ Настройки:\n" + "\n".join([f"• {k}: {v}" for k, v in rows])
    await message.answer(text[:4000], reply_markup=settings_menu())


@dp.message(F.text == "👥 Пользователи")
async def show_users(message: Message):
    if not await is_admin(message.from_user.id):
        return await message.answer("Только админ.")
    await message.answer("👥 Управление пользователями:", reply_markup=users_menu())


@dp.message(F.text == "⚖️ Штрафы")
async def show_penalties(message: Message):
    if not await is_admin(message.from_user.id):
        return await message.answer("Только админ.")
    await message.answer("⚖️ Система штрафов и кармы:", reply_markup=penalty_menu())


@dp.message(F.text == "🦊 Секретная кнопка")
async def show_secret(message: Message):
    if await get_setting("secret_button_enabled") != "1":
        return await message.answer("Секретная кнопка сейчас выключена 😶")
    await message.answer("🦊 Секретный раздел Foxi активирован.", reply_markup=secret_menu())


# =======================
# Tasks
# =======================

@dp.callback_query(F.data == "task:add")
async def add_task(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TaskState.title)
    await cb.message.answer(
        "Напиши задачу.\n\nНапример: поменять ценники на чипсы\n\nМожно нажать ↩️ Назад, если передумал.",
        reply_markup=cancel_keyboard()
    )
    await cb.answer()


@dp.message(TaskState.title)
async def task_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    async with db() as conn:
        cur = await conn.execute("""
        SELECT user_id, full_name FROM users
        WHERE approved=1 AND banned=0
        ORDER BY role='admin' DESC, full_name
        """)
        users = await cur.fetchall()
    kb = InlineKeyboardBuilder()
    for uid, name in users:
        kb.button(text=name or str(uid), callback_data=f"assign:{uid}")
    kb.button(text="↩️ Назад", callback_data="task:add")
    kb.adjust(1)
    await state.set_state(TaskState.assignee)
    await message.answer("Кому назначить?", reply_markup=kb.as_markup())


@dp.callback_query(TaskState.assignee, F.data.startswith("assign:"))
async def task_assignee(cb: CallbackQuery, state: FSMContext):
    assignee_id = int(cb.data.split(":")[1])
    await state.update_data(assignee_id=assignee_id)
    await state.set_state(TaskState.due_minutes)
    await cb.message.answer(
        "Через сколько минут напомнить, если не сделано?\n\nНапример: 10, 20, 60, 120",
        reply_markup=cancel_keyboard()
    )
    await cb.answer()


@dp.message(TaskState.due_minutes)
async def task_due(message: Message, state: FSMContext):
    try:
        minutes = int(message.text.strip())
        if minutes < 1:
            raise ValueError
    except ValueError:
        return await message.answer("Напиши число минут, например 10 или 20")

    data = await state.get_data()
    due_at = now_dt() + timedelta(minutes=minutes)
    async with db() as conn:
        cur = await conn.execute("""
        INSERT INTO tasks(title,assignee_id,created_by,created_at,updated_at,due_at)
        VALUES(?,?,?,?,?,?)
        """, (data["title"], data["assignee_id"], message.from_user.id, now_iso(), now_iso(), due_at.isoformat()))
        task_id = cur.lastrowid
        await conn.execute("""
        INSERT INTO task_history(task_id,user_id,action,comment,created_at)
        VALUES(?,?,?,?,?)
        """, (task_id, message.from_user.id, "created", data["title"], now_iso()))
        await conn.commit()

    await log_action(message.from_user.id, "task_created", f"#{task_id} {data['title']}; remind={minutes}min")
    await state.clear()
    await message.answer(f"✅ Задача #{task_id} создана.\nНапоминание через {minutes} мин.", reply_markup=main_menu(await is_admin(message.from_user.id)))
    try:
        await bot.send_message(data["assignee_id"], f"⚠️ Новая задача #{task_id}:\n{data['title']}", reply_markup=task_buttons(task_id))
    except Exception:
        await message.answer("Не смог написать сотруднику. Пусть он сначала нажмёт /start в боте.")


async def render_tasks(where: str, params=()):
    async with db() as conn:
        cur = await conn.execute(f"""
        SELECT t.id,t.title,t.status,t.assignee_id,t.due_at,COALESCE(u.full_name, t.assignee_id)
        FROM tasks t
        LEFT JOIN users u ON u.user_id=t.assignee_id
        WHERE {where}
        ORDER BY t.id DESC LIMIT 30
        """, params)
        rows = await cur.fetchall()
    if not rows:
        return "Пусто."
    status_map = {"new": "🆕", "process": "⏳", "done": "✅", "cant": "❌"}
    result = []
    for i, title, status, assignee_id, due_at, assignee_name in rows:
        due_text = "-"
        if due_at:
            try:
                due_text = datetime.fromisoformat(due_at).strftime("%d.%m %H:%M")
            except Exception:
                due_text = due_at
        result.append(f"{status_map.get(status,'•')} #{i} {title}\nКому: {assignee_name}\nСтатус: {status}\nДедлайн: {due_text}")
    return "\n\n".join(result)


@dp.callback_query(F.data == "task:list_active")
async def list_active(cb: CallbackQuery):
    await cb.message.answer(await render_tasks("t.status != 'done'"), reply_markup=back_inline("back:main"))
    await cb.answer()


@dp.callback_query(F.data == "task:list_done")
async def list_done(cb: CallbackQuery):
    await cb.message.answer(await render_tasks("t.status = 'done'"), reply_markup=back_inline("back:main"))
    await cb.answer()


@dp.callback_query(F.data == "task:my")
async def list_my(cb: CallbackQuery):
    await cb.message.answer(await render_tasks("t.assignee_id=? AND t.status!='done'", (cb.from_user.id,)), reply_markup=back_inline("back:main"))
    await cb.answer()


@dp.callback_query(F.data.startswith("taskstatus:"))
async def change_task_status(cb: CallbackQuery):
    _, task_id, status = cb.data.split(":")
    task_id_int = int(task_id)

    async with db() as conn:
        cur = await conn.execute("SELECT assignee_id,title FROM tasks WHERE id=?", (task_id_int,))
        row = await cur.fetchone()
        assignee_id = row[0] if row else cb.from_user.id
        title = row[1] if row else ""
        await conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, now_iso(), task_id_int))
        await conn.execute("INSERT INTO task_history(task_id,user_id,action,created_at) VALUES(?,?,?,?)",
                           (task_id_int, cb.from_user.id, f"status_{status}", now_iso()))
        await conn.commit()

    if status == "done":
        await add_karma(assignee_id, +10, f"Задача выполнена: {title}", cb.from_user.id, task_id_int)
    elif status == "process":
        await add_karma(assignee_id, +2, f"Взял(а) в процесс: {title}", cb.from_user.id, task_id_int)
    elif status == "cant":
        await add_karma(assignee_id, -5, f"Не смог(ла) выполнить: {title}", cb.from_user.id, task_id_int)

    await log_action(cb.from_user.id, "task_status", f"#{task_id} -> {status}")
    await cb.message.edit_text(f"Задача #{task_id}: статус обновлён на {status}")

    for admin in ADMIN_IDS:
        if admin != cb.from_user.id:
            try:
                await bot.send_message(admin, f"📌 Задача #{task_id}: статус {status} от {cb.from_user.full_name}")
            except Exception:
                pass
    await cb.answer()


# =======================
# Kegs
# =======================

@dp.callback_query(F.data == "keg:open")
async def keg_open(cb: CallbackQuery, state: FSMContext):
    await state.set_state(KegState.beer_name)
    await cb.message.answer(
        "Какое пиво открыли? Напиши название.\n\n↩️ Назад — отменить.",
        reply_markup=cancel_keyboard()
    )
    await cb.answer()


@dp.message(KegState.beer_name)
async def keg_name(message: Message, state: FSMContext):
    await state.update_data(beer_name=message.text.strip())
    await state.set_state(KegState.shelf_days)
    await message.answer("Сколько дней годна после открытия? Например: 5", reply_markup=cancel_keyboard())


@dp.message(KegState.shelf_days)
async def keg_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
        if days < 1:
            raise ValueError
    except ValueError:
        return await message.answer("Напиши число дней, например 5")

    data = await state.get_data()
    opened = now_dt()
    expires = opened + timedelta(days=days)
    async with db() as conn:
        cur = await conn.execute("""
        INSERT INTO kegs(beer_name,opened_at,shelf_days,expires_at,created_by)
        VALUES(?,?,?,?,?)
        """, (data["beer_name"], opened.isoformat(), days, expires.isoformat(), message.from_user.id))
        keg_id = cur.lastrowid
        await conn.commit()

    await log_action(message.from_user.id, "keg_opened", f"#{keg_id} {data['beer_name']}")
    await state.clear()
    await message.answer(
        f"🍺 Кега #{keg_id} открыта: {data['beer_name']}\nГодна до: {expires.strftime('%d.%m.%Y %H:%M')}",
        reply_markup=main_menu(await is_admin(message.from_user.id))
    )


async def render_kegs(priority_only=False):
    warning = int(await get_setting("keg_warning_days"))
    limit = now_dt() + timedelta(days=warning)
    query = "SELECT id,beer_name,opened_at,expires_at FROM kegs WHERE status='open'"
    params = []
    if priority_only:
        query += " AND expires_at <= ?"
        params.append(limit.isoformat())
    query += " ORDER BY expires_at ASC"
    async with db() as conn:
        cur = await conn.execute(query, params)
        rows = await cur.fetchall()
    if not rows:
        return "Открытых кег нет." if not priority_only else "Приоритетных кег сейчас нет."

    text = []
    for i, name, opened, expires in rows:
        exp = datetime.fromisoformat(expires)
        left = exp - now_dt()
        hours_left = max(int(left.total_seconds() // 3600), 0)
        text.append(
            f"🍺 #{i} {name}\n"
            f"Открыта: {opened[:10]}\n"
            f"Годна до: {exp.strftime('%d.%m.%Y %H:%M')}\n"
            f"Осталось примерно: {hours_left // 24} дн. {hours_left % 24} ч."
        )
    return "\n\n".join(text)


@dp.callback_query(F.data == "keg:list_open")
async def list_kegs(cb: CallbackQuery):
    await cb.message.answer(await render_kegs(False), reply_markup=back_inline("back:main"))
    await cb.answer()


@dp.callback_query(F.data == "keg:priority")
async def priority_kegs(cb: CallbackQuery):
    await cb.message.answer(await render_kegs(True), reply_markup=back_inline("back:main"))
    await cb.answer()


@dp.callback_query(F.data.startswith("kegclose:"))
async def close_keg(cb: CallbackQuery):
    keg_id = int(cb.data.split(":")[1])
    async with db() as conn:
        await conn.execute("UPDATE kegs SET status='closed', closed_at=? WHERE id=?", (now_iso(), keg_id))
        await conn.commit()
    await log_action(cb.from_user.id, "keg_closed", f"#{keg_id}")
    await cb.message.edit_text(f"✅ Кега #{keg_id} закрыта.")
    await cb.answer()


# =======================
# AI
# =======================

async def get_shop_context() -> str:
    async with db() as conn:
        cur = await conn.execute("""
        SELECT t.id,t.title,t.status,t.due_at,COALESCE(u.full_name, t.assignee_id)
        FROM tasks t
        LEFT JOIN users u ON u.user_id=t.assignee_id
        WHERE t.status!='done'
        ORDER BY t.id DESC LIMIT 30
        """)
        tasks = await cur.fetchall()
        cur = await conn.execute("""
        SELECT id,beer_name,opened_at,expires_at
        FROM kegs
        WHERE status='open'
        ORDER BY expires_at ASC LIMIT 30
        """)
        kegs = await cur.fetchall()
        cur = await conn.execute("SELECT user_id,full_name,role,approved,banned FROM users ORDER BY role='admin' DESC, full_name LIMIT 50")
        users = await cur.fetchall()
        cur = await conn.execute("SELECT user_id,action,details,created_at FROM action_log ORDER BY id DESC LIMIT 20")
        logs = await cur.fetchall()
        cur = await conn.execute("""
        SELECT u.full_name, COALESCE(SUM(k.delta),0)
        FROM users u
        LEFT JOIN karma_events k ON k.user_id=u.user_id
        WHERE u.approved=1 AND u.banned=0
        GROUP BY u.user_id
        ORDER BY COALESCE(SUM(k.delta),0) DESC
        LIMIT 20
        """)
        karma = await cur.fetchall()

    text = ["КОНТЕКСТ МАГАЗИНА"]
    text.append("\nСотрудники:")
    for uid, name, role, approved, banned in users:
        text.append(f"- {name or uid} | id={uid} | роль={role} | approved={approved} | banned={banned}")

    text.append("\nАктивные задачи:")
    if tasks:
        for tid, title, status, due_at, assignee in tasks:
            text.append(f"- #{tid}: {title}; статус={status}; кому={assignee}; дедлайн={due_at or '-'}")
    else:
        text.append("- нет активных задач")

    text.append("\nОткрытые кеги:")
    if kegs:
        for kid, beer, opened, expires in kegs:
            text.append(f"- #{kid}: {beer}; открыта={opened}; годна до={expires}")
    else:
        text.append("- нет открытых кег")

    text.append("\nКарма:")
    for name, score in karma:
        text.append(f"- {name}: {score}")

    text.append("\nПоследние действия:")
    if logs:
        for uid, action, details, created in logs:
            text.append(f"- {created}: user={uid}; {action}; {details}")
    else:
        text.append("- пусто")
    return "\n".join(text)


async def get_user_ai_history(user_id: int, limit: int = 12):
    async with db() as conn:
        cur = await conn.execute(
            "SELECT role,content FROM ai_messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        )
        rows = await cur.fetchall()
    return list(reversed(rows))


async def save_ai_message(user_id: int, role: str, content: str):
    async with db() as conn:
        await conn.execute(
            "INSERT INTO ai_messages(user_id,role,content,created_at) VALUES(?,?,?,?)",
            (user_id, role, content, now_iso())
        )
        await conn.commit()


def extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                return None
    return None


async def ask_ai(user_id: int, user_text: str) -> dict:
    if not ai_client:
        return {"reply": "OPENAI_API_KEY не указан в .env. Добавь ключ и перезапусти бота.", "actions": []}

    context = await get_shop_context()
    history = await get_user_ai_history(user_id)
    history_text = "\n".join([f"{role}: {content}" for role, content in history[-8:]])

    prompt = f"""
Ты ИИ-управляющий маленькой пивнухи и личный помощник Натальи. Отвечай по-русски, коротко и по делу.
У тебя есть данные бота: задачи, кеги, сотрудники, карма и история.

ВАЖНО:
1. Не говори, что ты не видишь базу — база ниже в контексте.
2. Если пользователь просит создать задачу, НЕ создавай её сам, а верни action create_task.
3. Для create_task нужен title, assignee_id если понятно кому, due_minutes если понятно через сколько минут.
4. Если сотрудник указан именем, найди его id в контексте.
5. Если данных не хватает, спроси уточнение.
6. Всегда возвращай СТРОГО JSON без markdown.

Формат ответа:
{{
  "reply": "текст ответа пользователю",
  "actions": [
    {{"type":"create_task","title":"...","assignee_id":123,"due_minutes":10}}
  ]
}}

Если действий нет: "actions": []

{context}

История разговора:
{history_text}

Сообщение пользователя: {user_text}
"""
    try:
        response = await ai_client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
        )
        raw = response.output_text
    except Exception as e:
        return {"reply": f"Ошибка OpenAI API: {e}", "actions": []}

    data = extract_json(raw)
    if not isinstance(data, dict) or "reply" not in data:
        data = {"reply": raw, "actions": []}
    if "actions" not in data or not isinstance(data["actions"], list):
        data["actions"] = []
    return data


async def create_pending_action(user_id: int, action: dict) -> int:
    async with db() as conn:
        cur = await conn.execute(
            "INSERT INTO ai_pending_actions(user_id,action_type,payload_json,created_at) VALUES(?,?,?,?)",
            (user_id, action.get("type", "unknown"), json.dumps(action, ensure_ascii=False), now_iso())
        )
        await conn.commit()
        return cur.lastrowid


async def execute_ai_action(action_id: int, user_id: int) -> str:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT action_type,payload_json FROM ai_pending_actions WHERE id=? AND user_id=?",
            (action_id, user_id)
        )
        row = await cur.fetchone()
        if not row:
            return "Действие не найдено или уже удалено."
        action_type, payload_json = row
        payload = json.loads(payload_json)
        await conn.execute("DELETE FROM ai_pending_actions WHERE id=?", (action_id,))
        await conn.commit()

    if action_type == "create_task":
        title = str(payload.get("title", "")).strip()
        assignee_id = payload.get("assignee_id")
        due_minutes = payload.get("due_minutes") or payload.get("due_hours", 1) * 60
        if not title:
            return "Не хватает названия задачи."
        try:
            assignee_id = int(assignee_id) if assignee_id else user_id
            due_minutes = int(due_minutes)
        except Exception:
            assignee_id = user_id
            due_minutes = 60

        due_at = now_dt() + timedelta(minutes=due_minutes)
        async with db() as conn:
            cur = await conn.execute("""
            INSERT INTO tasks(title,assignee_id,created_by,created_at,updated_at,due_at)
            VALUES(?,?,?,?,?,?)
            """, (title, assignee_id, user_id, now_iso(), now_iso(), due_at.isoformat()))
            task_id = cur.lastrowid
            await conn.execute(
                "INSERT INTO task_history(task_id,user_id,action,comment,created_at) VALUES(?,?,?,?,?)",
                (task_id, user_id, "ai_created", title, now_iso())
            )
            await conn.commit()
        await log_action(user_id, "ai_task_created", f"#{task_id} {title}")
        try:
            await bot.send_message(assignee_id, f"🤖 Новая задача от ИИ #{task_id}:\n{title}", reply_markup=task_buttons(task_id))
        except Exception:
            pass
        return f"✅ Создал задачу #{task_id}: {title}"

    return "Я пока умею выполнять только создание задач."


@dp.message(F.text == "🤖 ИИ управляющий")
async def show_ai_manager(message: Message):
    await message.answer(
        "🤖 ИИ управляющий\n\nОн видит активные задачи, открытые кеги, сотрудников, карму и историю.",
        reply_markup=ai_menu()
    )


@dp.callback_query(F.data == "ai:start")
async def ai_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AIState.chat)
    await cb.message.answer(
        "🤖 Режим ИИ включён.\n\nПиши обычным языком: что проверить, что создать, что сегодня важно.\nЧтобы выйти — нажми кнопку ниже.",
        reply_markup=ai_chat_keyboard()
    )
    await cb.answer()


@dp.callback_query(F.data == "ai:analyze")
async def ai_analyze(cb: CallbackQuery):
    await cb.message.answer("Думаю по магазину…")
    data = await ask_ai(cb.from_user.id, "Сделай анализ магазина на сегодня: что важно, какие задачи горят, какие кеги в приоритете, у кого какая карма.")
    await save_ai_message(cb.from_user.id, "user", "Анализ магазина")
    await save_ai_message(cb.from_user.id, "assistant", data["reply"])
    await cb.message.answer(data["reply"][:4000])
    await cb.answer()


@dp.callback_query(F.data == "ai:clear")
async def ai_clear(cb: CallbackQuery):
    async with db() as conn:
        await conn.execute("DELETE FROM ai_messages WHERE user_id=?", (cb.from_user.id,))
        await conn.execute("DELETE FROM ai_pending_actions WHERE user_id=?", (cb.from_user.id,))
        await conn.commit()
    await cb.message.answer("🧹 Память разговора с ИИ очищена.")
    await cb.answer()


@dp.message(AIState.chat, F.text.in_({"❌ Закончить разговор с ИИ", "↩️ Назад"}))
async def ai_stop(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Ок, режим ИИ выключен.",
        reply_markup=main_menu(await is_admin(message.from_user.id))
    )


@dp.message(AIState.chat)
async def ai_chat(message: Message):
    user_text = message.text or ""
    await save_ai_message(message.from_user.id, "user", user_text)
    await message.answer("🤖 Думаю…")
    data = await ask_ai(message.from_user.id, user_text)
    reply = data.get("reply", "")
    await save_ai_message(message.from_user.id, "assistant", reply)
    await message.answer(reply[:4000])

    for action in data.get("actions", []):
        if action.get("type") == "create_task":
            action_id = await create_pending_action(message.from_user.id, action)
            title = action.get("title", "без названия")
            assignee_id = action.get("assignee_id") or "ты"
            due_minutes = action.get("due_minutes") or action.get("due_hours", 1) * 60
            await message.answer(
                f"ИИ предлагает создать задачу:\n\n📋 {title}\n👤 Кому: {assignee_id}\n⏰ Напомнить через: {due_minutes} мин.",
                reply_markup=ai_action_buttons(action_id)
            )


@dp.callback_query(F.data.startswith("ai_action:"))
async def ai_action_confirm(cb: CallbackQuery):
    _, action_id, answer = cb.data.split(":")
    action_id = int(action_id)
    if answer == "no":
        async with db() as conn:
            await conn.execute("DELETE FROM ai_pending_actions WHERE id=? AND user_id=?", (action_id, cb.from_user.id))
            await conn.commit()
        await cb.message.edit_text("❌ Действие отменено.")
        return await cb.answer()
    result = await execute_ai_action(action_id, cb.from_user.id)
    await cb.message.edit_text(result)
    await cb.answer()


# =======================
# Reports / History
# =======================

@dp.message(F.text == "📊 Отчёты")
async def reports(message: Message):
    active = await render_tasks("t.status != 'done'")
    priority = await render_kegs(True)
    rating = await render_karma_rating(limit=5)
    mood = await render_mood_stats(days=30)
    await message.answer(
        f"📊 Отчёт\n\n"
        f"📋 Активные задачи:\n{active}\n\n"
        f"⚠️ Пиво в приоритете:\n{priority}\n\n"
        f"🏆 Карма:\n{rating}\n\n"
        f"🌙 Настроение за 30 дней:\n{mood}"
    )


@dp.message(F.text == "🕘 История")
async def history(message: Message):
    async with db() as conn:
        cur = await conn.execute("""
        SELECT user_id,action,details,created_at FROM action_log
        ORDER BY id DESC LIMIT 30
        """)
        rows = await cur.fetchall()
    if not rows:
        return await message.answer("История пустая.")
    text = "🕘 Последние действия:\n" + "\n".join([f"{created[:16]} | {uid} | {act} | {det}" for uid, act, det, created in rows])
    await message.answer(text[:4000])


# =======================
# Users admin
# =======================

async def render_users(status: str):
    where = {
        "pending": "approved=0 AND banned=0",
        "approved": "approved=1 AND banned=0",
        "banned": "banned=1",
    }[status]
    async with db() as conn:
        cur = await conn.execute(f"""
        SELECT user_id, username, full_name, role, approved, banned
        FROM users WHERE {where}
        ORDER BY created_at DESC LIMIT 50
        """)
        rows = await cur.fetchall()

    if not rows:
        return "Пусто.", None

    kb = InlineKeyboardBuilder()
    lines = []
    for uid, username, full_name, role, approved, banned in rows:
        uname = f"@{username}" if username else "-"
        lines.append(f"👤 {full_name or uid}\nID: {uid}\n{uname}\nРоль: {role}")
        if uid not in ADMIN_IDS:
            if status == "pending":
                kb.button(text=f"✅ Одобрить {full_name or uid}", callback_data=f"user:approve:{uid}")
                kb.button(text=f"🚫 Отклонить {full_name or uid}", callback_data=f"user:ban:{uid}")
            elif status == "approved":
                kb.button(text=f"🚫 Удалить {full_name or uid}", callback_data=f"user:ban:{uid}")
            elif status == "banned":
                kb.button(text=f"✅ Вернуть {full_name or uid}", callback_data=f"user:approve:{uid}")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    return "\n\n".join(lines), kb.as_markup()


@dp.callback_query(F.data.startswith("users:"))
async def users_list(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Только админ", show_alert=True)
    status = cb.data.split(":")[1]
    text, markup = await render_users(status)
    await cb.message.answer(text[:4000], reply_markup=markup or users_menu())
    await cb.answer()


@dp.callback_query(F.data.startswith("user:approve:"))
async def approve_user(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Только админ", show_alert=True)
    user_id = int(cb.data.split(":")[2])
    async with db() as conn:
        await conn.execute("""
        UPDATE users SET approved=1,banned=0,approved_at=?,approved_by=? WHERE user_id=?
        """, (now_iso(), cb.from_user.id, user_id))
        await conn.commit()
    await log_action(cb.from_user.id, "user_approved", str(user_id))
    try:
        await bot.send_message(user_id, "✅ Доступ одобрен. Теперь можно пользоваться ботом.", reply_markup=main_menu(False))
    except Exception:
        pass
    await cb.message.answer(f"✅ Пользователь {user_id} одобрен.")
    await cb.answer()


@dp.callback_query(F.data.startswith("user:ban:"))
async def ban_user(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Только админ", show_alert=True)
    user_id = int(cb.data.split(":")[2])
    if user_id in ADMIN_IDS:
        return await cb.answer("Админа из .env нельзя удалить через бота.", show_alert=True)
    async with db() as conn:
        await conn.execute("UPDATE users SET approved=0,banned=1 WHERE user_id=?", (user_id,))
        await conn.commit()
    await log_action(cb.from_user.id, "user_banned", str(user_id))
    try:
        await bot.send_message(user_id, "🚫 Доступ к боту закрыт администратором.")
    except Exception:
        pass
    await cb.message.answer(f"🚫 Пользователь {user_id} удалён/забанен.")
    await cb.answer()


# =======================
# Penalties / karma
# =======================

async def render_karma_rating(limit=20):
    async with db() as conn:
        cur = await conn.execute("""
        SELECT u.user_id, COALESCE(u.full_name, u.user_id), COALESCE(SUM(k.delta),0) AS score
        FROM users u
        LEFT JOIN karma_events k ON k.user_id=u.user_id
        WHERE u.approved=1 AND u.banned=0
        GROUP BY u.user_id
        ORDER BY score DESC
        LIMIT ?
        """, (limit,))
        rows = await cur.fetchall()
    if not rows:
        return "Пока пусто."
    lines = []
    for idx, (uid, name, score) in enumerate(rows, start=1):
        emoji = "🏆" if idx == 1 else "⭐"
        lines.append(f"{emoji} {idx}. {name}: {score}")
    return "\n".join(lines)


@dp.callback_query(F.data == "penalty:rating")
async def penalty_rating(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Только админ", show_alert=True)
    await cb.message.answer("🏆 Рейтинг кармы:\n\n" + await render_karma_rating(), reply_markup=penalty_menu())
    await cb.answer()


@dp.callback_query(F.data == "penalty:history")
async def penalty_history(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Только админ", show_alert=True)
    async with db() as conn:
        cur = await conn.execute("""
        SELECT k.delta,k.reason,k.created_at,COALESCE(u.full_name,k.user_id)
        FROM karma_events k
        LEFT JOIN users u ON u.user_id=k.user_id
        ORDER BY k.id DESC LIMIT 30
        """)
        rows = await cur.fetchall()
    if not rows:
        text = "История пустая."
    else:
        text = "\n".join([f"{created[:16]} | {name} | {delta:+} | {reason}" for delta, reason, created, name in rows])
    await cb.message.answer("📜 История кармы:\n\n" + text[:3800], reply_markup=penalty_menu())
    await cb.answer()


@dp.callback_query(F.data == "penalty:add")
async def penalty_add(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Только админ", show_alert=True)
    async with db() as conn:
        cur = await conn.execute("""
        SELECT user_id, full_name FROM users WHERE approved=1 AND banned=0 ORDER BY full_name
        """)
        users = await cur.fetchall()
    kb = InlineKeyboardBuilder()
    for uid, name in users:
        if uid not in ADMIN_IDS:
            kb.button(text=name or str(uid), callback_data=f"penuser:{uid}")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    await state.set_state(PenaltyState.user)
    await cb.message.answer("Кому выдать штраф?", reply_markup=kb.as_markup())
    await cb.answer()


@dp.callback_query(PenaltyState.user, F.data.startswith("penuser:"))
async def penalty_user(cb: CallbackQuery, state: FSMContext):
    user_id = int(cb.data.split(":")[1])
    await state.update_data(user_id=user_id)
    await state.set_state(PenaltyState.points)
    await cb.message.answer(
        "Сколько баллов снять?\n\nНапример: 5 или 10\nПиши положительное число, бот сам сделает минус.",
        reply_markup=cancel_keyboard()
    )
    await cb.answer()


@dp.message(PenaltyState.points)
async def penalty_points(message: Message, state: FSMContext):
    try:
        points = int(message.text.strip())
        if points < 1:
            raise ValueError
    except ValueError:
        return await message.answer("Напиши число, например 5")
    await state.update_data(points=points)
    await state.set_state(PenaltyState.reason)
    await message.answer("За что штраф? Например: не закрыла сменную задачу", reply_markup=cancel_keyboard())


@dp.message(PenaltyState.reason)
async def penalty_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data["user_id"]
    points = data["points"]
    reason = message.text.strip()
    await add_karma(user_id, -points, "Штраф: " + reason, message.from_user.id)
    await log_action(message.from_user.id, "manual_penalty", f"{user_id}: -{points}; {reason}")
    await state.clear()
    await message.answer(f"✅ Штраф выдан: -{points}\nПричина: {reason}", reply_markup=main_menu(await is_admin(message.from_user.id)))
    try:
        await bot.send_message(user_id, f"⚖️ Тебе выдали штраф: -{points} кармы\nПричина: {reason}")
    except Exception:
        pass


# =======================
# Mood
# =======================

async def render_mood_stats(days=30):
    since = (now_dt() - timedelta(days=days)).date().isoformat()
    async with db() as conn:
        cur = await conn.execute("""
        SELECT score, COUNT(*) FROM day_moods
        WHERE day >= ?
        GROUP BY score
        """, (since,))
        rows = await cur.fetchall()
        cur = await conn.execute("SELECT COUNT(*) FROM day_moods WHERE day >= ?", (since,))
        total = (await cur.fetchone())[0]
    if total == 0:
        return "Пока нет оценок."
    counts = {score: count for score, count in rows}
    good = counts.get(2, 0) + counts.get(3, 0)
    bad = counts.get(0, 0)
    ok = counts.get(1, 0)
    return f"Хороших дней: {good}/{total}\nНейтральных: {ok}\nПлохих: {bad}"


@dp.callback_query(F.data.startswith("mood:"))
async def mood_answer(cb: CallbackQuery):
    _, mood, score = cb.data.split(":")
    score = int(score)
    async with db() as conn:
        await conn.execute("""
        DELETE FROM day_moods WHERE user_id=? AND day=?
        """, (cb.from_user.id, today_str()))
        await conn.execute("""
        INSERT INTO day_moods(user_id,mood,score,day,created_at)
        VALUES(?,?,?,?,?)
        """, (cb.from_user.id, mood, score, today_str(), now_iso()))
        await conn.commit()
    await log_action(cb.from_user.id, "mood_answer", f"{mood}:{score}")

    phrase = {
        "great": "🔥 Отлично! День засчитан как мощный.",
        "good": "🙂 Хороший день, забираем в статистику.",
        "ok": "😐 Бывает. Завтра добьём.",
        "bad": "💀 Понял. День был не подарок, но ты всё равно вывезла.",
    }.get(mood, "Записал.")
    await cb.message.edit_text(phrase)
    await cb.answer()


# =======================
# Secret / jokes
# =======================

COMPLIMENTS = [
    "Доброе утро, Наталья 🌞 Сегодня ты как свежая кега — бодрая, ценная и всем нужна.",
    "Новый день, новая победа. Пусть сегодня всё идёт легко, а люди не тупят 😄",
    "Доброе утро ❤️ Желаю дня без нервов, без просрочек и с хорошей выручкой.",
    "Сегодня официальный прогноз: Наталья справится со всем, даже если всё опять через одно место.",
    "Просыпайся, легенда. Мир сам себя не организует 🦊",
    "Пусть сегодня задачи закрываются быстро, пиво продаётся бодро, а настроение держится выше 90%.",
]

SECRET_PHRASES = [
    "👑 Наталья всегда права. Даже когда не права — значит, реальность ошиблась.",
    "⚠️ Спорить с Натальей запрещено техникой безопасности.",
    "🍺 Наталья одобрила этот день. День может продолжаться.",
    "🦊 Foxi докладывает: уровень крутости Натальи критически высокий.",
    "💅 Если Наталья молчит — система анализирует, кому сейчас прилетит.",
    "🔥 Наталья не опаздывает. Это время приходит раньше неё.",
]

BEER_ORACLE = [
    "🔮 Сегодня твой сорт: Ципа-100. День будет крепкий.",
    "🔮 Сегодня твой сорт: Квітка. Нужно красиво, но с характером.",
    "🔮 Сегодня твой сорт: Говерла. Спокойно, уверенно, по классике.",
    "🔮 Сегодня твой сорт: Пломбір. День требует мягкости, но не слабости.",
    "🔮 Сегодня твой сорт: Гуцул IPA. Кто-то будет спорить, но ты победишь.",
]

RANDOM_ACTIONS = [
    "☕ Выпить кофе и не трогать людей 10 минут.",
    "🍺 Проверить, какая кега ближе к сроку.",
    "📋 Закрыть одну мелкую задачу, чтобы почувствовать власть.",
    "🧹 Посмотреть на склад и сделать вид, что всё под контролем.",
    "💬 Написать Мише: «бот живой, я богиня».",
    "😌 Пять минут ничего не делать. Это не лень, это техобслуживание.",
]


@dp.callback_query(F.data.startswith("secret:"))
async def secret_actions(cb: CallbackQuery):
    if await get_setting("secret_button_enabled") != "1":
        return await cb.answer("Секретная кнопка выключена.", show_alert=True)

    action = cb.data.split(":")[1]
    if action == "compliment":
        text = random.choice(COMPLIMENTS)
    elif action == "beer_oracle":
        text = random.choice(BEER_ORACLE)
    elif action == "natalie":
        text = random.choice(SECRET_PHRASES)
    elif action == "random_task":
        text = random.choice(RANDOM_ACTIONS)
    else:
        text = "🦊 Foxi ничего не понял, но сделал вид, что так и надо."
    await cb.message.answer(text, reply_markup=secret_menu())
    await cb.answer()


@dp.message(Command("natalie"))
@dp.message(F.text.lower().in_({"наталья", "/наташа", "наташа", "natalie"}))
async def natalie_joke(message: Message):
    await message.answer(random.choice(SECRET_PHRASES))


# =======================
# Settings
# =======================

@dp.callback_query(F.data.startswith("set:"))
async def set_start(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Только админ", show_alert=True)
    key = cb.data.split(":", 1)[1]
    await state.update_data(setting_key=key)
    await state.set_state(SettingsState.value)

    hint = "Например: 09:00 или число 60"
    if key.endswith("_enabled") or key in {"penalties_enabled", "secret_button_enabled"}:
        hint = "Напиши 1 чтобы включить, или 0 чтобы выключить"
    await cb.message.answer(f"Новое значение для {key}:\n{hint}", reply_markup=cancel_keyboard())
    await cb.answer()


@dp.message(SettingsState.value)
async def set_value(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data["setting_key"]
    value = message.text.strip()

    # Мини-валидация
    if key.endswith("_time") or key in {"daily_task_check_time", "evening_keg_question_time", "morning_priority_time", "mood_question_time", "morning_compliments_time"}:
        try:
            parse_time(value)
        except Exception:
            return await message.answer("Время нужно в формате 09:00")
    if key.endswith("_enabled") or key in {"penalties_enabled", "secret_button_enabled"}:
        if value not in {"0", "1"}:
            return await message.answer("Нужно 1 или 0")

    await set_setting_value(key, value)
    await log_action(message.from_user.id, "setting_changed", f"{key}={value}")
    await state.clear()
    await setup_scheduled_jobs(restart=True)
    await message.answer(f"✅ Настройка обновлена: {key} = {value}", reply_markup=main_menu(await is_admin(message.from_user.id)))


# =======================
# Scheduled jobs
# =======================

async def get_approved_users():
    async with db() as conn:
        cur = await conn.execute("""
        SELECT user_id FROM users WHERE approved=1 AND banned=0
        """)
        rows = await cur.fetchall()
    ids = {row[0] for row in rows}
    ids.update(ADMIN_IDS)
    return list(ids)


async def remind_unfinished_tasks():
    minutes = int(await get_setting("task_remind_minutes"))
    threshold = now_dt() - timedelta(minutes=minutes)
    async with db() as conn:
        cur = await conn.execute("""
        SELECT id,title,assignee_id,penalty_applied FROM tasks
        WHERE status!='done'
        AND (last_remind_at IS NULL OR last_remind_at <= ?)
        AND due_at <= ?
        """, (threshold.isoformat(), now_dt().isoformat()))
        rows = await cur.fetchall()

        for task_id, title, assignee_id, penalty_applied in rows:
            await conn.execute("UPDATE tasks SET last_remind_at=? WHERE id=?", (now_iso(), task_id))
            try:
                await bot.send_message(assignee_id, f"🔔 Напоминание по задаче #{task_id}:\n{title}\nУже сделали?", reply_markup=task_buttons(task_id))
            except Exception:
                pass

            if int(await get_setting("penalties_enabled")) == 1 and not penalty_applied:
                points = int(await get_setting("auto_overdue_penalty"))
                if points > 0:
                    await conn.execute("UPDATE tasks SET penalty_applied=1 WHERE id=?", (task_id,))
                    await conn.execute("""
                    INSERT INTO karma_events(user_id,delta,reason,created_by,task_id,created_at)
                    VALUES(?,?,?,?,?,?)
                    """, (assignee_id, -points, f"Автоштраф за просрочку задачи: {title}", None, task_id, now_iso()))
                    try:
                        await bot.send_message(assignee_id, f"⚖️ Автоштраф: -{points} кармы\nЗадача просрочена: {title}")
                    except Exception:
                        pass

        await conn.commit()


async def evening_keg_question():
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, открывали", callback_data="keg:open")
    kb.button(text="Нет", callback_data="noop")
    kb.adjust(1)

    # Вопрос по кегам шлём админам, чтобы не спамить всех
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, "🍺 Сегодня открывали новую кегу?", reply_markup=kb.as_markup())
        except Exception:
            pass


async def morning_priority():
    text = await render_kegs(True)
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, "🌅 Приоритет на сегодня по пиву:\n\n" + text)
        except Exception:
            pass


async def daily_task_check():
    active = await render_tasks("t.status != 'done'")
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, "📋 Вечерняя проверка задач:\n\n" + active)
        except Exception:
            pass


async def morning_compliments():
    if await get_setting("morning_compliments_enabled") != "1":
        return
    users = await get_approved_users()
    text = random.choice(COMPLIMENTS)
    for user_id in users:
        try:
            await bot.send_message(user_id, text)
        except Exception:
            pass


async def mood_question():
    if await get_setting("mood_enabled") != "1":
        return
    users = await get_approved_users()
    for user_id in users:
        try:
            await bot.send_message(user_id, "🌙 Как прошёл день?", reply_markup=mood_keyboard())
        except Exception:
            pass


@dp.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.message.edit_text("Ок, записал: новых кег сегодня не открывали.")
    await log_action(cb.from_user.id, "keg_evening_answer", "no")
    await cb.answer()


async def setup_scheduled_jobs(restart=False):
    if scheduler.running:
        scheduler.remove_all_jobs()
    else:
        scheduler.remove_all_jobs()

    scheduler.add_job(remind_unfinished_tasks, "interval", minutes=5, id="remind_tasks")

    jobs = [
        ("evening_keg_question_time", evening_keg_question, "evening_keg"),
        ("morning_priority_time", morning_priority, "morning_priority"),
        ("daily_task_check_time", daily_task_check, "daily_task_check"),
        ("morning_compliments_time", morning_compliments, "morning_compliments"),
        ("mood_question_time", mood_question, "mood_question"),
    ]

    for key, func, job_id in jobs:
        try:
            h, m = parse_time(await get_setting(key))
            scheduler.add_job(func, "cron", hour=h, minute=m, id=job_id)
        except Exception as e:
            print(f"Не смог запланировать {job_id}: {e}")

    if not scheduler.running:
        scheduler.start()


# =======================
# Fallback
# =======================

@dp.message()
async def fallback(message: Message):
    text = (message.text or "").strip().lower()
    if "устала" in text or "заеб" in text:
        return await message.answer("🚨 Обнаружена уставшая Наталья. Рекомендация: чай, 10 минут тишины и запрет людям тупить.")
    await message.answer("Не понял команду. Выбери раздел в меню.", reply_markup=main_menu(await is_admin(message.from_user.id)))


async def main():
    await init_db()
    await setup_scheduled_jobs()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
