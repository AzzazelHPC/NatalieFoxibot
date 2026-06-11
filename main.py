import asyncio
import os
import json
import random
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Any, Dict, Awaitable, Callable, Optional

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


class TaskDeleteState(StatesGroup):
    ids = State()
    day = State()


class KegDeleteState(StatesGroup):
    ids = State()
    day = State()


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


async def add_karma(user_id: int, delta: int, reason: str, created_by: Optional[int] = None, task_id: Optional[int] = None):
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
    kb.button(text="📊 Моя статистика")
    kb.button(text="🤖 ИИ управляющий")
    kb.button(text="🦊 Секретная кнопка")
    kb.button(text="🎰 Игра")
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
    kb.button(text="🗓 Задачи по дням", callback_data="task:days")
    kb.button(text="🗑 Удалить задачи по ID", callback_data="task:delete_ids")
    kb.button(text="🧨 Удалить задачи за день", callback_data="task:delete_day")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    return kb.as_markup()


def kegs_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="🍺 Открыть кегу", callback_data="keg:open")
    kb.button(text="📋 Открытые кеги", callback_data="keg:list_open")
    kb.button(text="⚠️ Скоро просрочка", callback_data="keg:priority")
    kb.button(text="🗓 Кеги по дням", callback_data="keg:days")
    kb.button(text="🗑 Удалить кеги по ID", callback_data="keg:delete_ids")
    kb.button(text="🧨 Удалить кеги за день", callback_data="keg:delete_day")
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
    kb.button(text="👤 Моя статистика", callback_data="penalty:my_stats")
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


async def notify_admins_about_request(user_id: int, full_name: str, username: Optional[str]):
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



async def render_personal_stats(user_id: int) -> str:
    async with db() as conn:
        cur = await conn.execute("SELECT COALESCE(full_name, user_id), role FROM users WHERE user_id=?", (user_id,))
        user_row = await cur.fetchone()

        cur = await conn.execute("SELECT COALESCE(SUM(delta),0) FROM karma_events WHERE user_id=?", (user_id,))
        karma = (await cur.fetchone())[0] or 0

        cur = await conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id=?", (user_id,))
        total_tasks = (await cur.fetchone())[0]
        cur = await conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id=? AND status='done'", (user_id,))
        done_tasks = (await cur.fetchone())[0]
        cur = await conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id=? AND status='process'", (user_id,))
        process_tasks = (await cur.fetchone())[0]
        cur = await conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id=? AND status='cant'", (user_id,))
        cant_tasks = (await cur.fetchone())[0]
        cur = await conn.execute("""
        SELECT COUNT(*) FROM tasks
        WHERE assignee_id=? AND status!='done' AND due_at IS NOT NULL AND due_at <= ?
        """, (user_id, datetime.now(TZ).isoformat()))
        overdue_tasks = (await cur.fetchone())[0]

        cur = await conn.execute("""
        SELECT delta, reason, created_at
        FROM karma_events
        WHERE user_id=?
        ORDER BY id DESC LIMIT 8
        """, (user_id,))
        karma_rows = await cur.fetchall()

        since = (date.today() - timedelta(days=30)).isoformat()
        cur = await conn.execute("""
        SELECT score, COUNT(*)
        FROM day_moods
        WHERE user_id=? AND day >= ?
        GROUP BY score
        """, (user_id, since))
        mood_rows = await cur.fetchall()

    name = user_row[0] if user_row else str(user_id)
    role = user_row[1] if user_row else "worker"

    if karma >= 500:
        level = "👑 Легенда"
    elif karma >= 300:
        level = "🏆 Отличник"
    elif karma >= 100:
        level = "⭐ Молодец"
    elif karma >= 0:
        level = "🙂 Нормально"
    else:
        level = "💀 Под подозрением"

    completed_percent = int(done_tasks * 100 / total_tasks) if total_tasks else 0

    mood_map = {3: "🔥 отличных", 2: "🙂 норм", 1: "😐 таких себе", 0: "💀 плохих"}
    mood_text = []
    total_moods = 0
    for score, count in sorted(mood_rows, reverse=True):
        total_moods += count
        mood_text.append(f"{mood_map.get(score, score)}: {count}")
    if not mood_text:
        mood_block = "Пока нет отметок настроения за 30 дней."
    else:
        mood_block = "\n".join(mood_text) + f"\nВсего отметок: {total_moods}"

    if not karma_rows:
        karma_block = "Штрафов/бонусов пока нет."
    else:
        karma_block = "\n".join([
            f"{created[:16]} | {delta:+} | {reason}"
            for delta, reason, created in karma_rows
        ])

    return (
        f"📊 Моя статистика\n\n"
        f"👤 {name}\n"
        f"Роль: {role}\n"
        f"Уровень: {level}\n"
        f"⭐ Карма: {karma}\n\n"
        f"📋 Задачи\n"
        f"Всего назначено: {total_tasks}\n"
        f"✅ Выполнено: {done_tasks}\n"
        f"⏳ В процессе: {process_tasks}\n"
        f"❌ Не могу: {cant_tasks}\n"
        f"⚠️ Просрочено сейчас: {overdue_tasks}\n"
        f"Процент выполнения: {completed_percent}%\n\n"
        f"🌙 Настроение за 30 дней\n{mood_block}\n\n"
        f"⚖️ Последние штрафы/бонусы\n{karma_block}"
    )


@dp.message(F.text == "📊 Моя статистика")
async def my_stats(message: Message):
    text = await render_personal_stats(message.from_user.id)
    await message.answer(text[:4000])


@dp.message(Command("stats"))
async def my_stats_command(message: Message):
    text = await render_personal_stats(message.from_user.id)
    await message.answer(text[:4000])


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
# Task history / delete tools
# =======================

def parse_id_list(text: str):
    ids = []
    for part in text.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return sorted(set(ids))


async def render_task_days(limit: int = 14):
    async with db() as conn:
        cur = await conn.execute("""
        SELECT substr(created_at,1,10) AS day,
               COUNT(*) AS total,
               SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done_count,
               SUM(CASE WHEN status!='done' THEN 1 ELSE 0 END) AS active_count
        FROM tasks
        GROUP BY substr(created_at,1,10)
        ORDER BY day DESC
        LIMIT ?
        """, (limit,))
        rows = await cur.fetchall()
    if not rows:
        return "📭 Задач по дням пока нет.", None
    kb = InlineKeyboardBuilder()
    lines = ["🗓 <b>Задачи по дням</b>\n"]
    for day, total, done_count, active_count in rows:
        done_count = done_count or 0
        active_count = active_count or 0
        lines.append(f"📅 <b>{day}</b> — всего: {total}, ✅ {done_count}, ⏳ {active_count}")
        kb.button(text=f"📅 {day} ({total})", callback_data=f"task:day:{day}")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


async def render_tasks_for_day(day: str):
    async with db() as conn:
        cur = await conn.execute("""
        SELECT t.id,t.title,t.status,t.assignee_id,t.due_at,COALESCE(u.full_name, t.assignee_id),t.created_at
        FROM tasks t
        LEFT JOIN users u ON u.user_id=t.assignee_id
        WHERE substr(t.created_at,1,10)=?
        ORDER BY t.id DESC
        """, (day,))
        rows = await cur.fetchall()
    if not rows:
        return f"📭 За {day} задач нет."
    status_map = {"new": "🆕", "process": "⏳", "done": "✅", "cant": "❌"}
    lines = [f"📅 <b>Задачи за {day}</b>\n"]
    for task_id, title, status, assignee_id, due_at, assignee_name, created_at in rows:
        created = created_at[11:16] if created_at and len(created_at) >= 16 else "--:--"
        due = "-"
        if due_at:
            try:
                due = datetime.fromisoformat(due_at).strftime("%d.%m %H:%M")
            except Exception:
                due = due_at
        lines.append(
            f"{status_map.get(status,'•')} <b>#{task_id}</b> {title}\n"
            f"👤 {assignee_name}\n"
            f"🕒 Создана: {created}\n"
            f"⏰ Дедлайн: {due}\n"
            f"📌 Статус: {status}\n"
        )
    return "\n".join(lines)


async def delete_tasks_by_ids(ids, deleted_by: int):
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    async with db() as conn:
        cur = await conn.execute(f"SELECT id,title FROM tasks WHERE id IN ({placeholders})", ids)
        rows = await cur.fetchall()
        found_ids = [row[0] for row in rows]
        if not found_ids:
            return 0
        ph = ",".join("?" for _ in found_ids)
        await conn.execute(f"DELETE FROM task_history WHERE task_id IN ({ph})", found_ids)
        await conn.execute(f"DELETE FROM karma_events WHERE task_id IN ({ph})", found_ids)
        await conn.execute(f"DELETE FROM tasks WHERE id IN ({ph})", found_ids)
        await conn.commit()
    await log_action(deleted_by, "tasks_deleted", ", ".join([f"#{i}" for i in found_ids]))
    return len(found_ids)


async def delete_tasks_by_day(day: str, deleted_by: int):
    async with db() as conn:
        cur = await conn.execute("SELECT id FROM tasks WHERE substr(created_at,1,10)=?", (day,))
        ids = [row[0] for row in await cur.fetchall()]
    return await delete_tasks_by_ids(ids, deleted_by)


@dp.callback_query(F.data == "task:days")
async def task_days(cb: CallbackQuery):
    text, markup = await render_task_days()
    await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("task:day:"))
async def task_day_detail(cb: CallbackQuery):
    day = cb.data.split(":", 2)[2]
    kb = InlineKeyboardBuilder()
    if await is_admin(cb.from_user.id):
        kb.button(text=f"🧨 Удалить все за {day}", callback_data=f"task:delete_day_confirm:{day}")
    kb.button(text="↩️ Назад", callback_data="task:days")
    kb.adjust(1)
    await cb.message.answer(await render_tasks_for_day(day), reply_markup=kb.as_markup(), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "task:delete_ids")
async def task_delete_ids_start(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Удалять задачи может только админ.", show_alert=True)
    await state.set_state(TaskDeleteState.ids)
    await cb.message.answer(
        "🗑 Напиши ID задач, которые удалить.\n\nМожно через запятую:\n<code>12, 15, 18</code>\n\n↩️ Назад — отменить.",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(TaskDeleteState.ids)
async def task_delete_ids_finish(message: Message, state: FSMContext):
    ids = parse_id_list(message.text or "")
    if not ids:
        return await message.answer("Не вижу ID. Пример: 12, 15, 18")
    deleted = await delete_tasks_by_ids(ids, message.from_user.id)
    await state.clear()
    await message.answer(f"✅ Удалено задач: {deleted}\nID: {', '.join(map(str, ids))}", reply_markup=main_menu(await is_admin(message.from_user.id)))


@dp.callback_query(F.data == "task:delete_day")
async def task_delete_day_start(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Удалять задачи может только админ.", show_alert=True)
    await state.set_state(TaskDeleteState.day)
    await cb.message.answer(
        "🧨 Напиши дату, за которую удалить все задачи.\n\nФормат: <code>2026-06-11</code>\n\n↩️ Назад — отменить.",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(TaskDeleteState.day)
async def task_delete_day_finish(message: Message, state: FSMContext):
    day = (message.text or "").strip()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except Exception:
        return await message.answer("Нужна дата в формате 2026-06-11")
    deleted = await delete_tasks_by_day(day, message.from_user.id)
    await state.clear()
    await message.answer(f"✅ За {day} удалено задач: {deleted}", reply_markup=main_menu(await is_admin(message.from_user.id)))


@dp.callback_query(F.data.startswith("task:delete_day_confirm:"))
async def task_delete_day_confirm(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Только админ.", show_alert=True)
    day = cb.data.rsplit(":", 1)[1]
    deleted = await delete_tasks_by_day(day, cb.from_user.id)
    await cb.message.edit_text(f"✅ За {day} удалено задач: {deleted}")
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
# Keg day list / delete tools
# =======================

async def render_keg_days(limit: int = 14):
    async with db() as conn:
        cur = await conn.execute("""
        SELECT substr(opened_at,1,10) AS day,
               COUNT(*) AS total,
               SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS opened,
               SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed
        FROM kegs
        GROUP BY substr(opened_at,1,10)
        ORDER BY day DESC
        LIMIT ?
        """, (limit,))
        rows = await cur.fetchall()
    if not rows:
        return "📭 Кег по дням пока нет.", None
    kb = InlineKeyboardBuilder()
    lines = ["🗓 <b>Кеги по дням</b>\n"]
    for day, total, opened, closed in rows:
        opened = opened or 0
        closed = closed or 0
        lines.append(f"📅 <b>{day}</b> — всего: {total}, 🍺 открыто: {opened}, ✅ закрыто: {closed}")
        kb.button(text=f"📅 {day} ({total})", callback_data=f"keg:day:{day}")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


async def render_kegs_for_day(day: str):
    async with db() as conn:
        cur = await conn.execute("""
        SELECT id,beer_name,opened_at,expires_at,status,closed_at
        FROM kegs
        WHERE substr(opened_at,1,10)=?
        ORDER BY id DESC
        """, (day,))
        rows = await cur.fetchall()
    if not rows:
        return f"📭 За {day} кег нет."
    lines = [f"📅 <b>Кеги за {day}</b>\n"]
    for keg_id, beer, opened_at, expires_at, status, closed_at in rows:
        st = "🍺 открыта" if status == "open" else "✅ закрыта"
        opened = opened_at[11:16] if opened_at and len(opened_at) >= 16 else "--:--"
        exp = "-"
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at).strftime("%d.%m %H:%M")
            except Exception:
                exp = expires_at
        lines.append(f"🍺 <b>#{keg_id}</b> {beer}\n🕒 Открыта: {opened}\n⏰ Годна до: {exp}\n📌 Статус: {st}\n")
    return "\n".join(lines)


async def delete_kegs_by_ids(ids, deleted_by: int):
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    async with db() as conn:
        cur = await conn.execute(f"SELECT id,beer_name FROM kegs WHERE id IN ({placeholders})", ids)
        rows = await cur.fetchall()
        found_ids = [row[0] for row in rows]
        if not found_ids:
            return 0
        ph = ",".join("?" for _ in found_ids)
        await conn.execute(f"DELETE FROM kegs WHERE id IN ({ph})", found_ids)
        await conn.commit()
    await log_action(deleted_by, "kegs_deleted", ", ".join([f"#{i}" for i in found_ids]))
    return len(found_ids)


async def delete_kegs_by_day(day: str, deleted_by: int):
    async with db() as conn:
        cur = await conn.execute("SELECT id FROM kegs WHERE substr(opened_at,1,10)=?", (day,))
        ids = [row[0] for row in await cur.fetchall()]
    return await delete_kegs_by_ids(ids, deleted_by)


@dp.callback_query(F.data == "keg:days")
async def keg_days_list(cb: CallbackQuery):
    text, markup = await render_keg_days()
    await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("keg:day:"))
async def keg_day_detail(cb: CallbackQuery):
    day = cb.data.split(":", 2)[2]
    kb = InlineKeyboardBuilder()
    if await is_admin(cb.from_user.id):
        kb.button(text=f"🧨 Удалить все за {day}", callback_data=f"keg:delete_day_confirm:{day}")
    kb.button(text="↩️ Назад", callback_data="keg:days")
    kb.adjust(1)
    await cb.message.answer(await render_kegs_for_day(day), reply_markup=kb.as_markup(), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "keg:delete_ids")
async def keg_delete_ids_start(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Удалять кеги может только админ.", show_alert=True)
    await state.set_state(KegDeleteState.ids)
    await cb.message.answer(
        "🗑 Напиши ID кег, которые удалить.\n\nМожно через запятую:\n<code>3, 4, 7</code>\n\n↩️ Назад — отменить.",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(KegDeleteState.ids)
async def keg_delete_ids_finish(message: Message, state: FSMContext):
    ids = parse_id_list(message.text or "")
    if not ids:
        return await message.answer("Не вижу ID. Пример: 3, 4, 7")
    deleted = await delete_kegs_by_ids(ids, message.from_user.id)
    await state.clear()
    await message.answer(f"✅ Удалено кег: {deleted}\nID: {', '.join(map(str, ids))}", reply_markup=main_menu(await is_admin(message.from_user.id)))


@dp.callback_query(F.data == "keg:delete_day")
async def keg_delete_day_start(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Удалять кеги может только админ.", show_alert=True)
    await state.set_state(KegDeleteState.day)
    await cb.message.answer(
        "🧨 Напиши дату, за которую удалить все кеги.\n\nФормат: <code>2026-06-11</code>\n\n↩️ Назад — отменить.",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(KegDeleteState.day)
async def keg_delete_day_finish(message: Message, state: FSMContext):
    day = (message.text or "").strip()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except Exception:
        return await message.answer("Нужна дата в формате 2026-06-11")
    deleted = await delete_kegs_by_day(day, message.from_user.id)
    await state.clear()
    await message.answer(f"✅ За {day} удалено кег: {deleted}", reply_markup=main_menu(await is_admin(message.from_user.id)))


@dp.callback_query(F.data.startswith("keg:delete_day_confirm:"))
async def keg_delete_day_confirm(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Только админ.", show_alert=True)
    day = cb.data.rsplit(":", 1)[1]
    deleted = await delete_kegs_by_day(day, cb.from_user.id)
    await cb.message.edit_text(f"✅ За {day} удалено кег: {deleted}")
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
# Mini game
# =======================

GAME_REELS = ["🍺", "🍟", "🦊", "👑", "⭐", "💀", "🔥"]


def game_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎰 Крутить ещё", callback_data="game:spin")
    kb.button(text="↩️ Назад", callback_data="back:main")
    kb.adjust(1)
    return kb.as_markup()


async def spin_game(user_id: int):
    reels = [random.choice(GAME_REELS) for _ in range(3)]
    unique = len(set(reels))
    points = 0
    if unique == 1:
        points = 25
        comment = "💎 ДЖЕКПОТ! Три одинаковых символа. Пивные боги аплодируют."
    elif unique == 2:
        points = 7
        comment = "😎 Два совпадения. Нормально так фартануло."
    elif "💀" in reels:
        points = -3
        comment = "💀 Череп выпал. Небольшой штраф от пивной вселенной."
    else:
        comment = "🎲 Мимо, но настроение засчитано."

    if points:
        await add_karma(user_id, points, f"Мини-игра: {' '.join(reels)}", user_id)
    await log_action(user_id, "game_spin", f"{' '.join(reels)}; karma={points}")

    sign = "+" if points > 0 else ""
    karma_text = f"\n⚖️ Карма: {sign}{points}" if points else "\n⚖️ Карма: без изменений"
    return f"🎰 <b>Пивной автомат</b>\n\n{'  '.join(reels)}\n\n{comment}{karma_text}"


@dp.message(F.text == "🎰 Игра")
async def game_message(message: Message):
    await message.answer(await spin_game(message.from_user.id), reply_markup=game_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data == "game:spin")
async def game_spin_cb(cb: CallbackQuery):
    await cb.message.answer(await spin_game(cb.from_user.id), reply_markup=game_keyboard(), parse_mode="HTML")
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
        SELECT l.user_id,COALESCE(u.full_name,l.user_id),l.action,l.details,l.created_at
        FROM action_log l
        LEFT JOIN users u ON u.user_id=l.user_id
        ORDER BY l.id DESC LIMIT 40
        """)
        rows = await cur.fetchall()
    if not rows:
        return await message.answer("История пустая.")

    emoji = {
        "task_created": "📋",
        "task_status": "📌",
        "tasks_deleted": "🗑",
        "keg_opened": "🍺",
        "keg_closed": "✅",
        "kegs_deleted": "🗑",
        "setting_changed": "⚙️",
        "karma": "⚖️",
        "game_spin": "🎰",
    }
    lines = ["🕘 <b>Красивая история действий</b>\n"]
    last_day = None
    for uid, name, action, details, created in rows:
        day = created[:10] if created else "----"
        time = created[11:16] if created and len(created) >= 16 else "--:--"
        if day != last_day:
            lines.append(f"\n📅 <b>{day}</b>")
            last_day = day
        icon = emoji.get(action, "•")
        lines.append(f"{icon} <b>{time}</b> — {name}\n   <code>{action}</code>: {details or '-'}")

    await message.answer("\n".join(lines)[:4000], parse_mode="HTML")


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



@dp.callback_query(F.data == "penalty:my_stats")
async def penalty_my_stats(cb: CallbackQuery):
    text = await render_personal_stats(cb.from_user.id)
    await cb.message.answer(text[:4000], reply_markup=penalty_menu() if await is_admin(cb.from_user.id) else back_inline())
    await cb.answer()


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

COMPLIMENTS = ['Доброе утро, Наталья 🌞 Сегодня ты как свежая кега — бодрая, ценная и всем нужна.',
 'Новый день, новая победа. Пусть сегодня всё идёт легко, а люди не тупят 😄',
 'Доброе утро ❤️ Желаю дня без нервов, без просрочек и с хорошей выручкой.',
 'Сегодня официальный прогноз: Наталья справится со всем, даже если всё опять через одно место.',
 'Просыпайся, легенда. Мир сам себя не организует 🦊',
 'Пусть сегодня задачи закрываются быстро, пиво продаётся бодро, а настроение держится выше 90%.',
 'Доброе утро 👑 Сегодня запрещено сомневаться в себе.',
 'Пусть клиенты будут адекватные, поставщики быстрые, а кофе вкусный.',
 'Сегодня день Натальи. Остальные просто присутствуют.',
 'Пусть всё, что должно получиться, получится без лишней драмы.',
 'Утренний отчёт: Наталья красивая, умная, опасная для хаоса.',
 'Пусть сегодня ни одна задача не посмеет тебя бесить.',
 'Сегодня ты главная героиня. Музыку включили, камера пошла.',
 'Пусть холодильники холодят, краны льют, а люди платят без вопросов.',
 'Доброе утро! День ещё ничего не успел испортить, значит шанс отличный.',
 'Сегодня твоя энергия нужна миру. Ну и пивнухе тоже.',
 'Пусть удача сегодня ходит за тобой как охранник.',
 'Наталья, сегодня ты не просто молодец, ты стратегический ресурс.',
 'Пусть настроение будет мягкое как Пломбір, а уверенность крепкая как Ципа-100.',
 'Система Foxi подтверждает: Наталья заряжена на победу.',
 'Доброе утро, Наталья ☀️ Всё получится. А если нет — бот будет морально поддерживать.',
 'Доброе утро, Наталья ☀️ Нервы будут целые. А если нет — бот будет морально поддерживать.',
 'Доброе утро, Наталья ☀️ Клиенты будут добрые. А если нет — бот будет морально поддерживать.',
 'Доброе утро, Наталья ☀️ Задачи закроются. А если нет — бот будет морально поддерживать.',
 'Доброе утро, Наталья ☀️ День будет вкусным. А если нет — бот будет морально поддерживать.',
 'Доброе утро, Наталья ☀️ Выручка порадует. А если нет — бот будет морально поддерживать.',
 'Доброе утро, Наталья ☀️ Хаос отступит. А если нет — бот будет морально поддерживать.',
 'Доброе утро, Наталья ☀️ Энергия появится. А если нет — бот будет морально поддерживать.',
 'Доброе утро, Наталья ☀️ Настроение выживет. А если нет — бот будет морально поддерживать.',
 'Доброе утро, Наталья ☀️ Удача зайдёт на смену. А если нет — бот будет морально поддерживать.',
 'Новый день, Наталья ☀️ Всё получится. А если нет — бот будет морально поддерживать.',
 'Новый день, Наталья ☀️ Нервы будут целые. А если нет — бот будет морально поддерживать.',
 'Новый день, Наталья ☀️ Клиенты будут добрые. А если нет — бот будет морально поддерживать.',
 'Новый день, Наталья ☀️ Задачи закроются. А если нет — бот будет морально поддерживать.',
 'Новый день, Наталья ☀️ День будет вкусным. А если нет — бот будет морально поддерживать.',
 'Новый день, Наталья ☀️ Выручка порадует. А если нет — бот будет морально поддерживать.',
 'Новый день, Наталья ☀️ Хаос отступит. А если нет — бот будет морально поддерживать.',
 'Новый день, Наталья ☀️ Энергия появится. А если нет — бот будет морально поддерживать.',
 'Новый день, Наталья ☀️ Настроение выживет. А если нет — бот будет морально поддерживать.',
 'Новый день, Наталья ☀️ Удача зайдёт на смену. А если нет — бот будет морально поддерживать.',
 'Сегодня, Наталья ☀️ Всё получится. А если нет — бот будет морально поддерживать.',
 'Сегодня, Наталья ☀️ Нервы будут целые. А если нет — бот будет морально поддерживать.',
 'Сегодня, Наталья ☀️ Клиенты будут добрые. А если нет — бот будет морально поддерживать.',
 'Сегодня, Наталья ☀️ Задачи закроются. А если нет — бот будет морально поддерживать.',
 'Сегодня, Наталья ☀️ День будет вкусным. А если нет — бот будет морально поддерживать.',
 'Сегодня, Наталья ☀️ Выручка порадует. А если нет — бот будет морально поддерживать.',
 'Сегодня, Наталья ☀️ Хаос отступит. А если нет — бот будет морально поддерживать.',
 'Сегодня, Наталья ☀️ Энергия появится. А если нет — бот будет морально поддерживать.',
 'Сегодня, Наталья ☀️ Настроение выживет. А если нет — бот будет морально поддерживать.',
 'Сегодня, Наталья ☀️ Удача зайдёт на смену. А если нет — бот будет морально поддерживать.',
 'Утренний прогноз, Наталья ☀️ Всё получится. А если нет — бот будет морально поддерживать.',
 'Утренний прогноз, Наталья ☀️ Нервы будут целые. А если нет — бот будет морально поддерживать.',
 'Утренний прогноз, Наталья ☀️ Клиенты будут добрые. А если нет — бот будет морально поддерживать.',
 'Утренний прогноз, Наталья ☀️ Задачи закроются. А если нет — бот будет морально поддерживать.',
 'Утренний прогноз, Наталья ☀️ День будет вкусным. А если нет — бот будет морально поддерживать.',
 'Утренний прогноз, Наталья ☀️ Выручка порадует. А если нет — бот будет морально поддерживать.',
 'Утренний прогноз, Наталья ☀️ Хаос отступит. А если нет — бот будет морально поддерживать.',
 'Утренний прогноз, Наталья ☀️ Энергия появится. А если нет — бот будет морально поддерживать.',
 'Утренний прогноз, Наталья ☀️ Настроение выживет. А если нет — бот будет морально поддерживать.',
 'Утренний прогноз, Наталья ☀️ Удача зайдёт на смену. А если нет — бот будет морально поддерживать.',
 'Foxi докладывает, Наталья ☀️ Всё получится. А если нет — бот будет морально поддерживать.',
 'Foxi докладывает, Наталья ☀️ Нервы будут целые. А если нет — бот будет морально поддерживать.',
 'Foxi докладывает, Наталья ☀️ Клиенты будут добрые. А если нет — бот будет морально поддерживать.',
 'Foxi докладывает, Наталья ☀️ Задачи закроются. А если нет — бот будет морально поддерживать.',
 'Foxi докладывает, Наталья ☀️ День будет вкусным. А если нет — бот будет морально поддерживать.',
 'Foxi докладывает, Наталья ☀️ Выручка порадует. А если нет — бот будет морально поддерживать.',
 'Foxi докладывает, Наталья ☀️ Хаос отступит. А если нет — бот будет морально поддерживать.',
 'Foxi докладывает, Наталья ☀️ Энергия появится. А если нет — бот будет морально поддерживать.',
 'Foxi докладывает, Наталья ☀️ Настроение выживет. А если нет — бот будет морально поддерживать.',
 'Foxi докладывает, Наталья ☀️ Удача зайдёт на смену. А если нет — бот будет морально поддерживать.',
 'Система подтверждает, Наталья ☀️ Всё получится. А если нет — бот будет морально поддерживать.',
 'Система подтверждает, Наталья ☀️ Нервы будут целые. А если нет — бот будет морально поддерживать.',
 'Система подтверждает, Наталья ☀️ Клиенты будут добрые. А если нет — бот будет морально поддерживать.',
 'Система подтверждает, Наталья ☀️ Задачи закроются. А если нет — бот будет морально поддерживать.',
 'Система подтверждает, Наталья ☀️ День будет вкусным. А если нет — бот будет морально поддерживать.',
 'Система подтверждает, Наталья ☀️ Выручка порадует. А если нет — бот будет морально поддерживать.',
 'Система подтверждает, Наталья ☀️ Хаос отступит. А если нет — бот будет морально поддерживать.',
 'Система подтверждает, Наталья ☀️ Энергия появится. А если нет — бот будет морально поддерживать.',
 'Система подтверждает, Наталья ☀️ Настроение выживет. А если нет — бот будет морально поддерживать.',
 'Система подтверждает, Наталья ☀️ Удача зайдёт на смену. А если нет — бот будет морально поддерживать.',
 'Официально, Наталья ☀️ Всё получится. А если нет — бот будет морально поддерживать.',
 'Официально, Наталья ☀️ Нервы будут целые. А если нет — бот будет морально поддерживать.',
 'Официально, Наталья ☀️ Клиенты будут добрые. А если нет — бот будет морально поддерживать.',
 'Официально, Наталья ☀️ Задачи закроются. А если нет — бот будет морально поддерживать.',
 'Официально, Наталья ☀️ День будет вкусным. А если нет — бот будет морально поддерживать.',
 'Официально, Наталья ☀️ Выручка порадует. А если нет — бот будет морально поддерживать.',
 'Официально, Наталья ☀️ Хаос отступит. А если нет — бот будет морально поддерживать.',
 'Официально, Наталья ☀️ Энергия появится. А если нет — бот будет морально поддерживать.',
 'Официально, Наталья ☀️ Настроение выживет. А если нет — бот будет морально поддерживать.',
 'Официально, Наталья ☀️ Удача зайдёт на смену. А если нет — бот будет морально поддерживать.',
 'План на день, Наталья ☀️ Всё получится. А если нет — бот будет морально поддерживать.',
 'План на день, Наталья ☀️ Нервы будут целые. А если нет — бот будет морально поддерживать.',
 'План на день, Наталья ☀️ Клиенты будут добрые. А если нет — бот будет морально поддерживать.',
 'План на день, Наталья ☀️ Задачи закроются. А если нет — бот будет морально поддерживать.',
 'План на день, Наталья ☀️ День будет вкусным. А если нет — бот будет морально поддерживать.',
 'План на день, Наталья ☀️ Выручка порадует. А если нет — бот будет морально поддерживать.',
 'План на день, Наталья ☀️ Хаос отступит. А если нет — бот будет морально поддерживать.',
 'План на день, Наталья ☀️ Энергия появится. А если нет — бот будет морально поддерживать.',
 'План на день, Наталья ☀️ Настроение выживет. А если нет — бот будет морально поддерживать.',
 'План на день, Наталья ☀️ Удача зайдёт на смену. А если нет — бот будет морально поддерживать.']

SECRET_PHRASES = ['🍺 Пивной оракул говорит: сегодня всё получится.',
 '👑 Наталья снова молодец. Проверено наукой.',
 '🚨 Уровень крутости сегодня: 97%.',
 '🐸 Где-то сейчас грустит непроданный Флинт.',
 '☕ Рекомендуется срочно выпить что-нибудь вкусное.',
 '🍺 Пиво верит в тебя.',
 '📈 Шансы на хороший день выросли на 15%.',
 '🧠 ИИ проверил обстановку. Паниковать рано.',
 '💎 Сегодня ты главный персонаж.',
 '🚀 Производительность повышена.',
 '🍟 Чипсы наблюдают за тобой.',
 '👀 Кто-то сейчас вспоминает о тебе.',
 '🌞 День официально одобрен.',
 '⚠️ Сегодня запрещено грустить без разрешения.',
 '🍺 Пивные боги довольны.',
 '🦊 Склад пока не загорелся. Всё хорошо.',
 '🎯 Удача рядом.',
 '🧸 Сегодня можно быть добрее к себе.',
 '📦 Коробка счастья уже в пути.',
 '🎉 Вселенная поставила лайк.',
 '🍕 Возможно сегодня будет вкусняшка.',
 '🧠 Мозг работает. Иногда даже слишком.',
 '💰 Есть шанс найти деньги в кармане.',
 '👑 Совет директоров признал тебя молодцом.',
 '🍺 Ципа одобряет твои решения.',
 '🚨 Срочно улыбнись.',
 '😎 Сегодня стиль на максимуме.',
 '📊 График настроения идёт вверх.',
 '🐈 Где-то мурчит котик специально для тебя.',
 '⭐ Рейтинг дня: 9 из 10.',
 '🍀 Немного удачи уже выдано.',
 '🎲 Судьба подмигнула.',
 '🥳 Всё будет хорошо. И это приказ.',
 '🍺 Сегодняшняя кега в тебя верит.',
 '🧃 Не забудь попить воды.',
 '📈 Карма слегка выросла просто так.',
 '🛸 Инопланетяне довольны твоей работой.',
 '🌈 Сегодня хороший день для хорошего дня.',
 '🎵 Фоновая музыка стала лучше.',
 '⚡ Энергия +10.',
 '🦄 Немного магии добавлено.',
 '🍔 Вероятность перекуса: 83%.',
 '🧠 Уровень гениальности: достаточный.',
 '🐸 Пивной дух машет лапкой.',
 '🚀 Ты уже молодец.',
 '🍺 Всё идёт по плану. Даже если плана нет.',
 '👀 Кто прочитал это — красавчик.',
 '📦 Все проблемы временно отложены.',
 '🌟 Наталья одобрила этот день.',
 '🦊 Секрет Foxi: кега верит в тебя.',
 '🦊 Секрет Foxi: кега подозрительно молчит.',
 '🦊 Секрет Foxi: кега ждёт твоего решения.',
 '🦊 Секрет Foxi: кега готовится к великому дню.',
 '🦊 Секрет Foxi: кега просит не нервничать.',
 '🦊 Секрет Foxi: кега официально одобряет.',
 '🦊 Секрет Foxi: кега шепчет: ты справишься.',
 '🦊 Секрет Foxi: кега ушёл в режим уважения.',
 '🦊 Секрет Foxi: кега поставил лайк.',
 '🦊 Секрет Foxi: кега сохраняет интригу.',
 '🦊 Секрет Foxi: накладная верит в тебя.',
 '🦊 Секрет Foxi: накладная подозрительно молчит.',
 '🦊 Секрет Foxi: накладная ждёт твоего решения.',
 '🦊 Секрет Foxi: накладная готовится к великому дню.',
 '🦊 Секрет Foxi: накладная просит не нервничать.',
 '🦊 Секрет Foxi: накладная официально одобряет.',
 '🦊 Секрет Foxi: накладная шепчет: ты справишься.',
 '🦊 Секрет Foxi: накладная ушёл в режим уважения.',
 '🦊 Секрет Foxi: накладная поставил лайк.',
 '🦊 Секрет Foxi: накладная сохраняет интригу.',
 '🦊 Секрет Foxi: ценник верит в тебя.',
 '🦊 Секрет Foxi: ценник подозрительно молчит.',
 '🦊 Секрет Foxi: ценник ждёт твоего решения.',
 '🦊 Секрет Foxi: ценник готовится к великому дню.',
 '🦊 Секрет Foxi: ценник просит не нервничать.',
 '🦊 Секрет Foxi: ценник официально одобряет.',
 '🦊 Секрет Foxi: ценник шепчет: ты справишься.',
 '🦊 Секрет Foxi: ценник ушёл в режим уважения.',
 '🦊 Секрет Foxi: ценник поставил лайк.',
 '🦊 Секрет Foxi: ценник сохраняет интригу.',
 '🦊 Секрет Foxi: холодильник верит в тебя.',
 '🦊 Секрет Foxi: холодильник подозрительно молчит.',
 '🦊 Секрет Foxi: холодильник ждёт твоего решения.',
 '🦊 Секрет Foxi: холодильник готовится к великому дню.',
 '🦊 Секрет Foxi: холодильник просит не нервничать.',
 '🦊 Секрет Foxi: холодильник официально одобряет.',
 '🦊 Секрет Foxi: холодильник шепчет: ты справишься.',
 '🦊 Секрет Foxi: холодильник ушёл в режим уважения.',
 '🦊 Секрет Foxi: холодильник поставил лайк.',
 '🦊 Секрет Foxi: холодильник сохраняет интригу.',
 '🦊 Секрет Foxi: краник верит в тебя.',
 '🦊 Секрет Foxi: краник подозрительно молчит.',
 '🦊 Секрет Foxi: краник ждёт твоего решения.',
 '🦊 Секрет Foxi: краник готовится к великому дню.',
 '🦊 Секрет Foxi: краник просит не нервничать.',
 '🦊 Секрет Foxi: краник официально одобряет.',
 '🦊 Секрет Foxi: краник шепчет: ты справишься.',
 '🦊 Секрет Foxi: краник ушёл в режим уважения.',
 '🦊 Секрет Foxi: краник поставил лайк.',
 '🦊 Секрет Foxi: краник сохраняет интригу.',
 '🦊 Секрет Foxi: Флинт верит в тебя.',
 '🦊 Секрет Foxi: Флинт подозрительно молчит.',
 '🦊 Секрет Foxi: Флинт ждёт твоего решения.',
 '🦊 Секрет Foxi: Флинт готовится к великому дню.',
 '🦊 Секрет Foxi: Флинт просит не нервничать.',
 '🦊 Секрет Foxi: Флинт официально одобряет.',
 '🦊 Секрет Foxi: Флинт шепчет: ты справишься.',
 '🦊 Секрет Foxi: Флинт ушёл в режим уважения.',
 '🦊 Секрет Foxi: Флинт поставил лайк.',
 '🦊 Секрет Foxi: Флинт сохраняет интригу.',
 '🦊 Секрет Foxi: Лейс верит в тебя.',
 '🦊 Секрет Foxi: Лейс подозрительно молчит.',
 '🦊 Секрет Foxi: Лейс ждёт твоего решения.',
 '🦊 Секрет Foxi: Лейс готовится к великому дню.',
 '🦊 Секрет Foxi: Лейс просит не нервничать.',
 '🦊 Секрет Foxi: Лейс официально одобряет.',
 '🦊 Секрет Foxi: Лейс шепчет: ты справишься.',
 '🦊 Секрет Foxi: Лейс ушёл в режим уважения.',
 '🦊 Секрет Foxi: Лейс поставил лайк.',
 '🦊 Секрет Foxi: Лейс сохраняет интригу.',
 '🦊 Секрет Foxi: Чипстерс верит в тебя.',
 '🦊 Секрет Foxi: Чипстерс подозрительно молчит.',
 '🦊 Секрет Foxi: Чипстерс ждёт твоего решения.',
 '🦊 Секрет Foxi: Чипстерс готовится к великому дню.',
 '🦊 Секрет Foxi: Чипстерс просит не нервничать.',
 '🦊 Секрет Foxi: Чипстерс официально одобряет.',
 '🦊 Секрет Foxi: Чипстерс шепчет: ты справишься.',
 '🦊 Секрет Foxi: Чипстерс ушёл в режим уважения.',
 '🦊 Секрет Foxi: Чипстерс поставил лайк.',
 '🦊 Секрет Foxi: Чипстерс сохраняет интригу.',
 '🦊 Секрет Foxi: Говерла верит в тебя.',
 '🦊 Секрет Foxi: Говерла подозрительно молчит.',
 '🦊 Секрет Foxi: Говерла ждёт твоего решения.',
 '🦊 Секрет Foxi: Говерла готовится к великому дню.',
 '🦊 Секрет Foxi: Говерла просит не нервничать.',
 '🦊 Секрет Foxi: Говерла официально одобряет.',
 '🦊 Секрет Foxi: Говерла шепчет: ты справишься.',
 '🦊 Секрет Foxi: Говерла ушёл в режим уважения.',
 '🦊 Секрет Foxi: Говерла поставил лайк.',
 '🦊 Секрет Foxi: Говерла сохраняет интригу.',
 '🦊 Секрет Foxi: Петрос верит в тебя.',
 '🦊 Секрет Foxi: Петрос подозрительно молчит.',
 '🦊 Секрет Foxi: Петрос ждёт твоего решения.',
 '🦊 Секрет Foxi: Петрос готовится к великому дню.',
 '🦊 Секрет Foxi: Петрос просит не нервничать.',
 '🦊 Секрет Foxi: Петрос официально одобряет.',
 '🦊 Секрет Foxi: Петрос шепчет: ты справишься.',
 '🦊 Секрет Foxi: Петрос ушёл в режим уважения.',
 '🦊 Секрет Foxi: Петрос поставил лайк.',
 '🦊 Секрет Foxi: Петрос сохраняет интригу.',
 '🦊 Секрет Foxi: терминал верит в тебя.']

NATALIA_PHRASES = ['👑 Наталья всегда права.',
 '👑 Если Наталья не права — смотри пункт выше.',
 '👑 Спорить с Натальей запрещено техникой безопасности.',
 '👑 Сегодня Наталья официально молодец.',
 '👑 По данным ИИ Наталья великолепна.',
 '👑 Совет пивных мудрецов доволен Натальей.',
 '👑 Уровень легендарности: максимальный.',
 '👑 Даже Ципа-100 уважает Наталью.',
 '👑 Наталья одобрила этот день.',
 '👑 Склад работает потому что Наталья разрешила.',
 '👑 Наталья не опаздывает — это время пришло раньше.',
 '👑 Если день сложный, значит он не готов к Наталье.',
 '👑 Наталья — причина, по которой хаос держится в рамках.',
 '👑 У Натальи не настроение, а стратегическая позиция.',
 '👑 Наталья может одним взглядом закрыть просроченную задачу.',
 '👑 Пивные духи внесли Наталью в белый список.',
 '👑 Сегодня Наталья в режиме королевского контроля.',
 '👑 Реальность получила замечание от Натальи.',
 '👑 Наталья — это когда красиво и страшно спорить.',
 '👑 Уровень Натальи: недоступно для обычных смертных.',
 '👑 Наталья одобрила этот день. Возражения не принимаются.',
 '👑 Наталья одобрила весь хаос. Возражения не принимаются.',
 '👑 Наталья одобрила пивных духов. Возражения не принимаются.',
 '👑 Наталья одобрила таблички. Возражения не принимаются.',
 '👑 Наталья одобрила задачи. Возражения не принимаются.',
 '👑 Наталья одобрила склад. Возражения не принимаются.',
 '👑 Наталья одобрила клиентов. Возражения не принимаются.',
 '👑 Наталья одобрила просрок. Возражения не принимаются.',
 '👑 Наталья одобрила планы. Возражения не принимаются.',
 '👑 Наталья одобрила вселенную. Возражения не принимаются.',
 '👑 Наталья победила этот день. Возражения не принимаются.',
 '👑 Наталья победила весь хаос. Возражения не принимаются.',
 '👑 Наталья победила пивных духов. Возражения не принимаются.',
 '👑 Наталья победила таблички. Возражения не принимаются.',
 '👑 Наталья победила задачи. Возражения не принимаются.',
 '👑 Наталья победила склад. Возражения не принимаются.',
 '👑 Наталья победила клиентов. Возражения не принимаются.',
 '👑 Наталья победила просрок. Возражения не принимаются.',
 '👑 Наталья победила планы. Возражения не принимаются.',
 '👑 Наталья победила вселенную. Возражения не принимаются.',
 '👑 Наталья пережила этот день. Возражения не принимаются.',
 '👑 Наталья пережила весь хаос. Возражения не принимаются.',
 '👑 Наталья пережила пивных духов. Возражения не принимаются.',
 '👑 Наталья пережила таблички. Возражения не принимаются.',
 '👑 Наталья пережила задачи. Возражения не принимаются.',
 '👑 Наталья пережила склад. Возражения не принимаются.',
 '👑 Наталья пережила клиентов. Возражения не принимаются.',
 '👑 Наталья пережила просрок. Возражения не принимаются.',
 '👑 Наталья пережила планы. Возражения не принимаются.',
 '👑 Наталья пережила вселенную. Возражения не принимаются.',
 '👑 Наталья укротила этот день. Возражения не принимаются.',
 '👑 Наталья укротила весь хаос. Возражения не принимаются.',
 '👑 Наталья укротила пивных духов. Возражения не принимаются.',
 '👑 Наталья укротила таблички. Возражения не принимаются.',
 '👑 Наталья укротила задачи. Возражения не принимаются.',
 '👑 Наталья укротила склад. Возражения не принимаются.',
 '👑 Наталья укротила клиентов. Возражения не принимаются.',
 '👑 Наталья укротила просрок. Возражения не принимаются.',
 '👑 Наталья укротила планы. Возражения не принимаются.',
 '👑 Наталья укротила вселенную. Возражения не принимаются.',
 '👑 Наталья переиграла этот день. Возражения не принимаются.',
 '👑 Наталья переиграла весь хаос. Возражения не принимаются.',
 '👑 Наталья переиграла пивных духов. Возражения не принимаются.',
 '👑 Наталья переиграла таблички. Возражения не принимаются.',
 '👑 Наталья переиграла задачи. Возражения не принимаются.',
 '👑 Наталья переиграла склад. Возражения не принимаются.',
 '👑 Наталья переиграла клиентов. Возражения не принимаются.',
 '👑 Наталья переиграла просрок. Возражения не принимаются.',
 '👑 Наталья переиграла планы. Возражения не принимаются.',
 '👑 Наталья переиграла вселенную. Возражения не принимаются.',
 '👑 Наталья пересчитала этот день. Возражения не принимаются.',
 '👑 Наталья пересчитала весь хаос. Возражения не принимаются.',
 '👑 Наталья пересчитала пивных духов. Возражения не принимаются.',
 '👑 Наталья пересчитала таблички. Возражения не принимаются.',
 '👑 Наталья пересчитала задачи. Возражения не принимаются.',
 '👑 Наталья пересчитала склад. Возражения не принимаются.',
 '👑 Наталья пересчитала клиентов. Возражения не принимаются.',
 '👑 Наталья пересчитала просрок. Возражения не принимаются.',
 '👑 Наталья пересчитала планы. Возражения не принимаются.',
 '👑 Наталья пересчитала вселенную. Возражения не принимаются.',
 '👑 Наталья освятила этот день. Возражения не принимаются.',
 '👑 Наталья освятила весь хаос. Возражения не принимаются.',
 '👑 Наталья освятила пивных духов. Возражения не принимаются.',
 '👑 Наталья освятила таблички. Возражения не принимаются.',
 '👑 Наталья освятила задачи. Возражения не принимаются.',
 '👑 Наталья освятила склад. Возражения не принимаются.',
 '👑 Наталья освятила клиентов. Возражения не принимаются.',
 '👑 Наталья освятила просрок. Возражения не принимаются.',
 '👑 Наталья освятила планы. Возражения не принимаются.',
 '👑 Наталья освятила вселенную. Возражения не принимаются.',
 '👑 Наталья запустила этот день. Возражения не принимаются.',
 '👑 Наталья запустила весь хаос. Возражения не принимаются.',
 '👑 Наталья запустила пивных духов. Возражения не принимаются.',
 '👑 Наталья запустила таблички. Возражения не принимаются.',
 '👑 Наталья запустила задачи. Возражения не принимаются.',
 '👑 Наталья запустила склад. Возражения не принимаются.',
 '👑 Наталья запустила клиентов. Возражения не принимаются.',
 '👑 Наталья запустила просрок. Возражения не принимаются.',
 '👑 Наталья запустила планы. Возражения не принимаются.',
 '👑 Наталья запустила вселенную. Возражения не принимаются.',
 '👑 Наталья проконтролировала этот день. Возражения не принимаются.',
 '👑 Наталья проконтролировала весь хаос. Возражения не принимаются.',
 '👑 Наталья проконтролировала пивных духов. Возражения не принимаются.',
 '👑 Наталья проконтролировала таблички. Возражения не принимаются.',
 '👑 Наталья проконтролировала задачи. Возражения не принимаются.',
 '👑 Наталья проконтролировала склад. Возражения не принимаются.',
 '👑 Наталья проконтролировала клиентов. Возражения не принимаются.',
 '👑 Наталья проконтролировала просрок. Возражения не принимаются.',
 '👑 Наталья проконтролировала планы. Возражения не принимаются.',
 '👑 Наталья проконтролировала вселенную. Возражения не принимаются.',
 '👑 Наталья нейтрализовала этот день. Возражения не принимаются.',
 '👑 Наталья нейтрализовала весь хаос. Возражения не принимаются.',
 '👑 Наталья нейтрализовала пивных духов. Возражения не принимаются.',
 '👑 Наталья нейтрализовала таблички. Возражения не принимаются.',
 '👑 Наталья нейтрализовала задачи. Возражения не принимаются.',
 '👑 Наталья нейтрализовала склад. Возражения не принимаются.',
 '👑 Наталья нейтрализовала клиентов. Возражения не принимаются.',
 '👑 Наталья нейтрализовала просрок. Возражения не принимаются.',
 '👑 Наталья нейтрализовала планы. Возражения не принимаются.',
 '👑 Наталья нейтрализовала вселенную. Возражения не принимаются.']

BEER_ORACLE = ['🔮 Сегодня твой сорт: Говерла. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Говерла. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Говерла. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Говерла. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Говерла. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Говерла. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Говерла. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Говерла. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Говерла. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Говерла. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: Петрос. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Петрос. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Петрос. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Петрос. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Петрос. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Петрос. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Петрос. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Петрос. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Петрос. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Петрос. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: Менчул. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Менчул. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Менчул. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Менчул. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Менчул. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Менчул. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Менчул. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Менчул. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Менчул. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Менчул. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: Гуцул IPA. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Гуцул IPA. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Гуцул IPA. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Гуцул IPA. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Гуцул IPA. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Гуцул IPA. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Гуцул IPA. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Гуцул IPA. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Гуцул IPA. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Гуцул IPA. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: Квітка. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Квітка. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Квітка. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Квітка. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Квітка. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Квітка. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Квітка. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Квітка. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Квітка. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Квітка. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: Золота. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Золота. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Золота. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Золота. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Золота. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Золота. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Золота. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Золота. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Золота. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Золота. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: Пломбір. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Пломбір. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Пломбір. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Пломбір. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Пломбір. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Пломбір. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Пломбір. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Пломбір. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Пломбір. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Пломбір. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: На молоці. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: На молоці. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: На молоці. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: На молоці. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: На молоці. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: На молоці. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: На молоці. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: На молоці. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: На молоці. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: На молоці. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: Ципа-100. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Ципа-100. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Ципа-100. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Ципа-100. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Ципа-100. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Ципа-100. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Ципа-100. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Ципа-100. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Ципа-100. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Ципа-100. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: Blanche. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Blanche. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Blanche. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Blanche. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Blanche. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Blanche. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Blanche. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Blanche. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Blanche. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Blanche. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: IPA. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: IPA. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: IPA. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: IPA. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: IPA. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: IPA. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: IPA. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: IPA. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: IPA. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: IPA. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: Milk Stout. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Milk Stout. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Milk Stout. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Milk Stout. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Milk Stout. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Milk Stout. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Milk Stout. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Milk Stout. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Milk Stout. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Milk Stout. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: Golden Ale. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Golden Ale. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Golden Ale. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Golden Ale. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Golden Ale. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Golden Ale. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Golden Ale. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Golden Ale. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Golden Ale. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Golden Ale. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: Pilsner. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Pilsner. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Pilsner. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Pilsner. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Pilsner. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Pilsner. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Pilsner. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Pilsner. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Pilsner. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Pilsner. Прогноз: режим королевы смены.',
 '🔮 Сегодня твой сорт: Helles. Прогноз: спокойствие и контроль.',
 '🔮 Сегодня твой сорт: Helles. Прогноз: движ и неожиданные повороты.',
 '🔮 Сегодня твой сорт: Helles. Прогноз: красота и характер.',
 '🔮 Сегодня твой сорт: Helles. Прогноз: мягкость без слабости.',
 '🔮 Сегодня твой сорт: Helles. Прогноз: сила и уверенность.',
 '🔮 Сегодня твой сорт: Helles. Прогноз: уют и хитрый план.',
 '🔮 Сегодня твой сорт: Helles. Прогноз: энергия на максимум.',
 '🔮 Сегодня твой сорт: Helles. Прогноз: день без лишней паники.',
 '🔮 Сегодня твой сорт: Helles. Прогноз: продажи и удача.',
 '🔮 Сегодня твой сорт: Helles. Прогноз: режим королевы смены.',
 '🔮 Оракул шепчет: не открывай новую кегу на эмоциях.',
 '🔮 Сегодня лучше сначала кофе, потом решения.',
 '🔮 Если клиент странный — это проверка терпения.',
 '🔮 Пивные духи советуют проверить сроки.',
 '🔮 День хорош для мелких побед и крупных перекусов.',
 '🔮 Сегодня задача сама себя не закроет, но может испугаться.',
 '🔮 Оракул видит выручку. Она хочет быть больше.',
 '🔮 Сегодня нельзя спорить с Натальей. Хотя это вообще никогда нельзя.',
 '🔮 Проверь кеги: одна из них что-то скрывает.',
 '🔮 Если всё бесит — значит пора 5 минут тишины.']

RANDOM_ACTIONS = ['☕ Выпить кофе и не трогать людей 10 минут.',
 '🍺 Проверить, какая кега ближе к сроку.',
 '📋 Закрыть одну мелкую задачу, чтобы почувствовать власть.',
 '🧹 Посмотреть на склад и сделать вид, что всё под контролем.',
 '💬 Написать Мише: «бот живой, я богиня».',
 '😌 Пять минут ничего не делать. Это не лень, это техобслуживание.',
 '🎲 Судьба сказала: проверить задачи. Но красиво.',
 '🎲 Судьба сказала: проверить кеги. Но красиво.',
 '🎲 Судьба сказала: проверить снеки. Но красиво.',
 '🎲 Судьба сказала: проверить ценники. Но красиво.',
 '🎲 Судьба сказала: проверить витрину. Но красиво.',
 '🎲 Судьба сказала: проверить склад. Но красиво.',
 '🎲 Судьба сказала: проверить настроение. Но красиво.',
 '🎲 Судьба сказала: проверить планы. Но красиво.',
 '🎲 Судьба сказала: проверить остатки. Но красиво.',
 '🎲 Судьба сказала: проверить кофе. Но красиво.',
 '🎲 Судьба сказала: посмотреть задачи. Но красиво.',
 '🎲 Судьба сказала: посмотреть кеги. Но красиво.',
 '🎲 Судьба сказала: посмотреть снеки. Но красиво.',
 '🎲 Судьба сказала: посмотреть ценники. Но красиво.',
 '🎲 Судьба сказала: посмотреть витрину. Но красиво.',
 '🎲 Судьба сказала: посмотреть склад. Но красиво.',
 '🎲 Судьба сказала: посмотреть настроение. Но красиво.',
 '🎲 Судьба сказала: посмотреть планы. Но красиво.',
 '🎲 Судьба сказала: посмотреть остатки. Но красиво.',
 '🎲 Судьба сказала: посмотреть кофе. Но красиво.',
 '🎲 Судьба сказала: пересчитать задачи. Но красиво.',
 '🎲 Судьба сказала: пересчитать кеги. Но красиво.',
 '🎲 Судьба сказала: пересчитать снеки. Но красиво.',
 '🎲 Судьба сказала: пересчитать ценники. Но красиво.',
 '🎲 Судьба сказала: пересчитать витрину. Но красиво.',
 '🎲 Судьба сказала: пересчитать склад. Но красиво.',
 '🎲 Судьба сказала: пересчитать настроение. Но красиво.',
 '🎲 Судьба сказала: пересчитать планы. Но красиво.',
 '🎲 Судьба сказала: пересчитать остатки. Но красиво.',
 '🎲 Судьба сказала: пересчитать кофе. Но красиво.',
 '🎲 Судьба сказала: похвалить задачи. Но красиво.',
 '🎲 Судьба сказала: похвалить кеги. Но красиво.',
 '🎲 Судьба сказала: похвалить снеки. Но красиво.',
 '🎲 Судьба сказала: похвалить ценники. Но красиво.',
 '🎲 Судьба сказала: похвалить витрину. Но красиво.',
 '🎲 Судьба сказала: похвалить склад. Но красиво.',
 '🎲 Судьба сказала: похвалить настроение. Но красиво.',
 '🎲 Судьба сказала: похвалить планы. Но красиво.',
 '🎲 Судьба сказала: похвалить остатки. Но красиво.',
 '🎲 Судьба сказала: похвалить кофе. Но красиво.',
 '🎲 Судьба сказала: потрогать взглядом задачи. Но красиво.',
 '🎲 Судьба сказала: потрогать взглядом кеги. Но красиво.',
 '🎲 Судьба сказала: потрогать взглядом снеки. Но красиво.',
 '🎲 Судьба сказала: потрогать взглядом ценники. Но красиво.',
 '🎲 Судьба сказала: потрогать взглядом витрину. Но красиво.',
 '🎲 Судьба сказала: потрогать взглядом склад. Но красиво.',
 '🎲 Судьба сказала: потрогать взглядом настроение. Но красиво.',
 '🎲 Судьба сказала: потрогать взглядом планы. Но красиво.',
 '🎲 Судьба сказала: потрогать взглядом остатки. Но красиво.',
 '🎲 Судьба сказала: потрогать взглядом кофе. Но красиво.',
 '🎲 Судьба сказала: обновить задачи. Но красиво.',
 '🎲 Судьба сказала: обновить кеги. Но красиво.',
 '🎲 Судьба сказала: обновить снеки. Но красиво.',
 '🎲 Судьба сказала: обновить ценники. Но красиво.',
 '🎲 Судьба сказала: обновить витрину. Но красиво.',
 '🎲 Судьба сказала: обновить склад. Но красиво.',
 '🎲 Судьба сказала: обновить настроение. Но красиво.',
 '🎲 Судьба сказала: обновить планы. Но красиво.',
 '🎲 Судьба сказала: обновить остатки. Но красиво.',
 '🎲 Судьба сказала: обновить кофе. Но красиво.',
 '🎲 Судьба сказала: отложить задачи. Но красиво.',
 '🎲 Судьба сказала: отложить кеги. Но красиво.',
 '🎲 Судьба сказала: отложить снеки. Но красиво.',
 '🎲 Судьба сказала: отложить ценники. Но красиво.',
 '🎲 Судьба сказала: отложить витрину. Но красиво.',
 '🎲 Судьба сказала: отложить склад. Но красиво.',
 '🎲 Судьба сказала: отложить настроение. Но красиво.',
 '🎲 Судьба сказала: отложить планы. Но красиво.',
 '🎲 Судьба сказала: отложить остатки. Но красиво.',
 '🎲 Судьба сказала: отложить кофе. Но красиво.',
 '🎲 Судьба сказала: закрыть задачи. Но красиво.',
 '🎲 Судьба сказала: закрыть кеги. Но красиво.',
 '🎲 Судьба сказала: закрыть снеки. Но красиво.',
 '🎲 Судьба сказала: закрыть ценники. Но красиво.',
 '🎲 Судьба сказала: закрыть витрину. Но красиво.',
 '🎲 Судьба сказала: закрыть склад. Но красиво.',
 '🎲 Судьба сказала: закрыть настроение. Но красиво.',
 '🎲 Судьба сказала: закрыть планы. Но красиво.',
 '🎲 Судьба сказала: закрыть остатки. Но красиво.',
 '🎲 Судьба сказала: закрыть кофе. Но красиво.',
 '🎲 Судьба сказала: вспомнить задачи. Но красиво.',
 '🎲 Судьба сказала: вспомнить кеги. Но красиво.',
 '🎲 Судьба сказала: вспомнить снеки. Но красиво.',
 '🎲 Судьба сказала: вспомнить ценники. Но красиво.',
 '🎲 Судьба сказала: вспомнить витрину. Но красиво.',
 '🎲 Судьба сказала: вспомнить склад. Но красиво.',
 '🎲 Судьба сказала: вспомнить настроение. Но красиво.',
 '🎲 Судьба сказала: вспомнить планы. Но красиво.',
 '🎲 Судьба сказала: вспомнить остатки. Но красиво.',
 '🎲 Судьба сказала: вспомнить кофе. Но красиво.',
 '🎲 Судьба сказала: организовать задачи. Но красиво.',
 '🎲 Судьба сказала: организовать кеги. Но красиво.',
 '🎲 Судьба сказала: организовать снеки. Но красиво.',
 '🎲 Судьба сказала: организовать ценники. Но красиво.',
 '🎲 Судьба сказала: организовать витрину. Но красиво.',
 '🎲 Судьба сказала: организовать склад. Но красиво.',
 '🎲 Судьба сказала: организовать настроение. Но красиво.',
 '🎲 Судьба сказала: организовать планы. Но красиво.',
 '🎲 Судьба сказала: организовать остатки. Но красиво.',
 '🎲 Судьба сказала: организовать кофе. Но красиво.']

JACKPOT_PHRASES = ['💎 ДЖЕКПОТ: Наталья получает бессрочное право быть молодцом.',
 '💎 Редкая пасхалка: пивные боги официально хлопают стоя.',
 '💎 Джекпот дня: все проблемы назначены на завтра.',
 '💎 Ультра-редкий прогноз: сегодня Наталья легенда без обсуждений.',
 '💎 Секретный бонус: +100 к настроению и +50 к королевской власти.']

def phrase_with_jackpot(phrases):
    # 1% шанс редкой пасхалки
    if random.randint(1, 100) == 1:
        return random.choice(JACKPOT_PHRASES)
    return random.choice(phrases)


@dp.callback_query(F.data.startswith("secret:"))
async def secret_actions(cb: CallbackQuery):
    if await get_setting("secret_button_enabled") != "1":
        return await cb.answer("Секретная кнопка выключена.", show_alert=True)

    action = cb.data.split(":")[1]
    if action == "compliment":
        text = phrase_with_jackpot(COMPLIMENTS)
    elif action == "beer_oracle":
        text = phrase_with_jackpot(BEER_ORACLE)
    elif action == "natalie":
        text = phrase_with_jackpot(NATALIA_PHRASES)
    elif action == "random_task":
        text = phrase_with_jackpot(RANDOM_ACTIONS)
    else:
        text = "🦊 Foxi ничего не понял, но сделал вид, что так и надо."
    await cb.message.answer(text, reply_markup=secret_menu())
    await cb.answer()


@dp.message(Command("natalie"))
@dp.message(F.text.lower().in_({"наталья", "/наташа", "наташа", "natalie", "/natalie"}))
async def natalie_joke(message: Message):
    await message.answer(phrase_with_jackpot(NATALIA_PHRASES))


@dp.message(Command("oracle"))
@dp.message(F.text.lower().in_({"оракул", "пивной оракул", "/beer", "/oracle"}))
async def beer_oracle_command(message: Message):
    await message.answer(phrase_with_jackpot(BEER_ORACLE))


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
