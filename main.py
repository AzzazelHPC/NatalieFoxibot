import asyncio
import os
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import aiosqlite
import json
from openai import AsyncOpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
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


class TaskState(StatesGroup):
    title = State()
    assignee = State()
    due_hours = State()


class KegState(StatesGroup):
    beer_name = State()
    shelf_days = State()


class SettingsState(StatesGroup):
    value = State()


class BroadcastState(StatesGroup):
    text = State()


class AIState(StatesGroup):
    chat = State()


def db():
    return aiosqlite.connect(DB_PATH)


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
        """)
        defaults = {
            "daily_task_check_time": "19:00",
            "evening_keg_question_time": "20:00",
            "morning_priority_time": "09:00",
            "task_remind_minutes": "60",
            "keg_warning_days": "2",
            "notify_admins": "1"
        }
        for k, v in defaults.items():
            await conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        await conn.commit()


def now_iso():
    return datetime.now(TZ).replace(microsecond=0).isoformat()


def main_menu(is_admin=False):
    kb = ReplyKeyboardBuilder()
    kb.button(text="📋 Задачи")
    kb.button(text="🍺 Кеги")
    kb.button(text="📊 Отчёты")
    kb.button(text="🕘 История")
    kb.button(text="🤖 ИИ управляющий")
    if is_admin:
        kb.button(text="⚙️ Настройки")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


def tasks_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить задачу", callback_data="task:add")
    kb.button(text="📋 Активные", callback_data="task:list_active")
    kb.button(text="✅ Выполненные", callback_data="task:list_done")
    kb.button(text="👤 Мои задачи", callback_data="task:my")
    kb.adjust(1)
    return kb.as_markup()


def kegs_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="🍺 Открыть кегу", callback_data="keg:open")
    kb.button(text="📋 Открытые кеги", callback_data="keg:list_open")
    kb.button(text="⚠️ Скоро просрочка", callback_data="keg:priority")
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
    return kb.as_markup()


def settings_menu():
    kb = InlineKeyboardBuilder()
    items = [
        ("Время утреннего приоритета", "daily_task_check_time"),
        ("Вечерний вопрос по кегам", "evening_keg_question_time"),
        ("Утренний список пива", "morning_priority_time"),
        ("Повтор задачи, минут", "task_remind_minutes"),
        ("За сколько дней пиво в приоритет", "keg_warning_days"),
    ]
    for title, key in items:
        kb.button(text=title, callback_data=f"set:{key}")
    kb.adjust(1)
    return kb.as_markup()


def ai_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Начать разговор с ИИ", callback_data="ai:start")
    kb.button(text="📊 Анализ магазина", callback_data="ai:analyze")
    kb.button(text="🧹 Очистить память ИИ", callback_data="ai:clear")
    kb.adjust(1)
    return kb.as_markup()


def ai_chat_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text="❌ Закончить разговор с ИИ")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True)


def ai_action_buttons(action_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выполнить", callback_data=f"ai_action:{action_id}:yes")
    kb.button(text="❌ Отмена", callback_data=f"ai_action:{action_id}:no")
    kb.adjust(2)
    return kb.as_markup()


async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def log_action(user_id: int, action: str, details: str = ""):
    async with db() as conn:
        await conn.execute(
            "INSERT INTO action_log(user_id,action,details,created_at) VALUES(?,?,?,?)",
            (user_id, action, details, now_iso())
        )
        await conn.commit()


async def get_setting(key: str) -> str:
    async with db() as conn:
        cur = await conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else ""



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
        cur = await conn.execute("SELECT user_id,full_name,role FROM users ORDER BY role='admin' DESC, full_name LIMIT 50")
        users = await cur.fetchall()
        cur = await conn.execute("SELECT user_id,action,details,created_at FROM action_log ORDER BY id DESC LIMIT 20")
        logs = await cur.fetchall()

    text = ["КОНТЕКСТ МАГАЗИНА"]
    text.append("\nСотрудники:")
    if users:
        for uid, name, role in users:
            text.append(f"- {name or uid} | id={uid} | роль={role}")
    else:
        text.append("- пока нет")

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
Ты ИИ-управляющий маленькой пивнухи. Отвечай по-русски, коротко и по делу.
Ты видишь данные бота: задачи, кеги, сотрудников и историю.

ВАЖНО:
1. Не говори, что ты не видишь базу — база ниже в контексте.
2. Если пользователь просит создать задачу, НЕ создавай её сам, а верни action create_task.
3. Для create_task нужен title, assignee_id если понятно кому, due_hours если понятно через сколько часов.
4. Если сотрудник указан именем, найди его id в контексте.
5. Если данных не хватает, спроси уточнение.
6. Всегда возвращай СТРОГО JSON без markdown.

Формат ответа:
{{
  "reply": "текст ответа пользователю",
  "actions": [
    {{"type":"create_task","title":"...","assignee_id":123,"due_hours":1}}
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
        due_hours = payload.get("due_hours") or 1
        if not title:
            return "Не хватает названия задачи."
        try:
            assignee_id = int(assignee_id) if assignee_id else user_id
            due_hours = int(due_hours)
        except Exception:
            assignee_id = user_id
            due_hours = 1
        due_at = datetime.now(TZ) + timedelta(hours=due_hours)
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


async def save_user(message: Message):
    u = message.from_user
    role = "admin" if await is_admin(u.id) else "worker"
    async with db() as conn:
        await conn.execute("""
        INSERT INTO users(user_id, username, full_name, role, created_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name, role=excluded.role
        """, (u.id, u.username, u.full_name, role, now_iso()))
        await conn.commit()


@dp.message(CommandStart())
async def start(message: Message):
    await save_user(message)
    await message.answer(
        "Готово. Я менеджер задач и кег 🍺\nВыбери раздел в меню.",
        reply_markup=main_menu(await is_admin(message.from_user.id))
    )


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
        cur = await conn.execute("SELECT key,value FROM settings")
        rows = await cur.fetchall()
    text = "⚙️ Настройки:\n" + "\n".join([f"• {k}: {v}" for k, v in rows])
    await message.answer(text, reply_markup=settings_menu())


@dp.callback_query(F.data == "task:add")
async def add_task(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TaskState.title)
    await cb.message.answer("Напиши задачу. Например: поменять ценники на чипсы")
    await cb.answer()


@dp.message(TaskState.title)
async def task_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    async with db() as conn:
        cur = await conn.execute("SELECT user_id, full_name FROM users ORDER BY role='admin' DESC, full_name")
        users = await cur.fetchall()
    kb = InlineKeyboardBuilder()
    for uid, name in users:
        kb.button(text=name or str(uid), callback_data=f"assign:{uid}")
    kb.adjust(1)
    await state.set_state(TaskState.assignee)
    await message.answer("Кому назначить?", reply_markup=kb.as_markup())


@dp.callback_query(TaskState.assignee, F.data.startswith("assign:"))
async def task_assignee(cb: CallbackQuery, state: FSMContext):
    assignee_id = int(cb.data.split(":")[1])
    await state.update_data(assignee_id=assignee_id)
    await state.set_state(TaskState.due_hours)
    await cb.message.answer("Через сколько часов напомнить, если не сделано? Напиши число. Например: 1")
    await cb.answer()


@dp.message(TaskState.due_hours)
async def task_due(message: Message, state: FSMContext):
    try:
        hours = int(message.text.strip())
    except ValueError:
        return await message.answer("Напиши число часов, например 1")
    data = await state.get_data()
    due_at = datetime.now(TZ) + timedelta(hours=hours)
    async with db() as conn:
        cur = await conn.execute("""
        INSERT INTO tasks(title,assignee_id,created_by,created_at,updated_at,due_at)
        VALUES(?,?,?,?,?,?)
        """, (data["title"], data["assignee_id"], message.from_user.id, now_iso(), now_iso(), due_at.isoformat()))
        task_id = cur.lastrowid
        await conn.execute("INSERT INTO task_history(task_id,user_id,action,comment,created_at) VALUES(?,?,?,?,?)",
                           (task_id, message.from_user.id, "created", data["title"], now_iso()))
        await conn.commit()
    await log_action(message.from_user.id, "task_created", f"#{task_id} {data['title']}")
    await state.clear()
    await message.answer(f"✅ Задача #{task_id} создана.")
    try:
        await bot.send_message(data["assignee_id"], f"⚠️ Новая задача #{task_id}:\n{data['title']}", reply_markup=task_buttons(task_id))
    except Exception:
        await message.answer("Не смог написать сотруднику. Пусть он сначала нажмёт /start в боте.")


async def render_tasks(where: str, params=()):
    async with db() as conn:
        cur = await conn.execute(f"SELECT id,title,status,assignee_id,due_at FROM tasks WHERE {where} ORDER BY id DESC LIMIT 30", params)
        rows = await cur.fetchall()
    if not rows:
        return "Пусто."
    status_map = {"new": "🆕", "process": "⏳", "done": "✅", "cant": "❌"}
    return "\n\n".join([f"{status_map.get(s,'•')} #{i} {t}\nСтатус: {s}\nДедлайн: {d or '-'}" for i,t,s,a,d in rows])


@dp.callback_query(F.data == "task:list_active")
async def list_active(cb: CallbackQuery):
    await cb.message.answer(await render_tasks("status != 'done'"))
    await cb.answer()


@dp.callback_query(F.data == "task:list_done")
async def list_done(cb: CallbackQuery):
    await cb.message.answer(await render_tasks("status = 'done'"))
    await cb.answer()


@dp.callback_query(F.data == "task:my")
async def list_my(cb: CallbackQuery):
    await cb.message.answer(await render_tasks("assignee_id=? AND status!='done'", (cb.from_user.id,)))
    await cb.answer()


@dp.callback_query(F.data.startswith("taskstatus:"))
async def change_task_status(cb: CallbackQuery):
    _, task_id, status = cb.data.split(":")
    async with db() as conn:
        await conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, now_iso(), task_id))
        await conn.execute("INSERT INTO task_history(task_id,user_id,action,created_at) VALUES(?,?,?,?)",
                           (task_id, cb.from_user.id, f"status_{status}", now_iso()))
        await conn.commit()
    await log_action(cb.from_user.id, "task_status", f"#{task_id} -> {status}")
    await cb.message.edit_text(f"Задача #{task_id}: статус обновлён на {status}")
    for admin in ADMIN_IDS:
        if admin != cb.from_user.id:
            try:
                await bot.send_message(admin, f"📌 Задача #{task_id}: статус {status} от {cb.from_user.full_name}")
            except Exception:
                pass
    await cb.answer()


@dp.callback_query(F.data == "keg:open")
async def keg_open(cb: CallbackQuery, state: FSMContext):
    await state.set_state(KegState.beer_name)
    await cb.message.answer("Какое пиво открыли? Напиши название.")
    await cb.answer()


@dp.message(KegState.beer_name)
async def keg_name(message: Message, state: FSMContext):
    await state.update_data(beer_name=message.text.strip())
    await state.set_state(KegState.shelf_days)
    await message.answer("Сколько дней годна после открытия? Например: 5")


@dp.message(KegState.shelf_days)
async def keg_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
    except ValueError:
        return await message.answer("Напиши число дней, например 5")
    data = await state.get_data()
    opened = datetime.now(TZ)
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
    await message.answer(f"🍺 Кега #{keg_id} открыта: {data['beer_name']}\nГодна до: {expires.strftime('%d.%m.%Y %H:%M')}")


async def render_kegs(priority_only=False):
    warning = int(await get_setting("keg_warning_days"))
    limit = datetime.now(TZ) + timedelta(days=warning)
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
        left = exp - datetime.now(TZ)
        text.append(f"🍺 #{i} {name}\nОткрыта: {opened[:10]}\nГодна до: {exp.strftime('%d.%m.%Y %H:%M')}\nОсталось примерно: {max(left.days,0)} дн.")
    return "\n\n".join(text)


@dp.callback_query(F.data == "keg:list_open")
async def list_kegs(cb: CallbackQuery):
    await cb.message.answer(await render_kegs(False))
    await cb.answer()


@dp.callback_query(F.data == "keg:priority")
async def priority_kegs(cb: CallbackQuery):
    await cb.message.answer(await render_kegs(True))
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



@dp.message(F.text == "🤖 ИИ управляющий")
async def show_ai_manager(message: Message):
    await message.answer(
        "🤖 ИИ управляющий\n\nОн видит активные задачи, открытые кеги, сотрудников и историю действий.",
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
    data = await ask_ai(cb.from_user.id, "Сделай анализ магазина на сегодня: что важно, какие задачи горят, какие кеги в приоритете.")
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


@dp.message(AIState.chat, F.text == "❌ Закончить разговор с ИИ")
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
            due_hours = action.get("due_hours") or 1
            await message.answer(
                f"ИИ предлагает создать задачу:\n\n📋 {title}\n👤 Кому: {assignee_id}\n⏰ Напомнить через: {due_hours} ч.",
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


@dp.message(F.text == "📊 Отчёты")
async def reports(message: Message):
    active = await render_tasks("status != 'done'")
    priority = await render_kegs(True)
    await message.answer(f"📊 Отчёт\n\n📋 Активные задачи:\n{active}\n\n⚠️ Пиво в приоритете:\n{priority}")


@dp.message(F.text == "🕘 История")
async def history(message: Message):
    async with db() as conn:
        cur = await conn.execute("SELECT user_id,action,details,created_at FROM action_log ORDER BY id DESC LIMIT 30")
        rows = await cur.fetchall()
    if not rows:
        return await message.answer("История пустая.")
    text = "🕘 Последние действия:\n" + "\n".join([f"{created[:16]} | {uid} | {act} | {det}" for uid,act,det,created in rows])
    await message.answer(text[:4000])


@dp.callback_query(F.data.startswith("set:"))
async def set_start(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Только админ", show_alert=True)
    key = cb.data.split(":", 1)[1]
    await state.update_data(setting_key=key)
    await state.set_state(SettingsState.value)
    await cb.message.answer(f"Новое значение для {key}:\nНапример время 09:00 или число 60")
    await cb.answer()


@dp.message(SettingsState.value)
async def set_value(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data["setting_key"]
    value = message.text.strip()
    async with db() as conn:
        await conn.execute("UPDATE settings SET value=? WHERE key=?", (value, key))
        await conn.commit()
    await log_action(message.from_user.id, "setting_changed", f"{key}={value}")
    await state.clear()
    reschedule_jobs()
    await message.answer(f"✅ Настройка обновлена: {key} = {value}")


async def remind_unfinished_tasks():
    minutes = int(await get_setting("task_remind_minutes"))
    threshold = datetime.now(TZ) - timedelta(minutes=minutes)
    async with db() as conn:
        cur = await conn.execute("""
        SELECT id,title,assignee_id FROM tasks
        WHERE status!='done'
        AND (last_remind_at IS NULL OR last_remind_at <= ?)
        AND due_at <= ?
        """, (threshold.isoformat(), datetime.now(TZ).isoformat()))
        rows = await cur.fetchall()
        for task_id, title, assignee_id in rows:
            await conn.execute("UPDATE tasks SET last_remind_at=? WHERE id=?", (now_iso(), task_id))
            try:
                await bot.send_message(assignee_id, f"🔔 Напоминание по задаче #{task_id}:\n{title}\nУже сделали?", reply_markup=task_buttons(task_id))
            except Exception:
                pass
        await conn.commit()


async def evening_keg_question():
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, открывали", callback_data="keg:open")
    kb.button(text="Нет", callback_data="noop")
    kb.adjust(1)
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
    active = await render_tasks("status != 'done'")
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, "📋 Вечерняя проверка задач:\n\n" + active)
        except Exception:
            pass


@dp.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.message.edit_text("Ок, записал: новых кег сегодня не открывали.")
    await log_action(cb.from_user.id, "keg_evening_answer", "no")
    await cb.answer()


def parse_time(value: str):
    h, m = value.split(":")
    return int(h), int(m)


def reschedule_jobs():
    scheduler.remove_all_jobs()
    scheduler.add_job(remind_unfinished_tasks, "interval", minutes=5, id="remind_tasks")

    async def add_time_jobs():
        pass


async def setup_scheduled_jobs():
    scheduler.remove_all_jobs()
    scheduler.add_job(remind_unfinished_tasks, "interval", minutes=5, id="remind_tasks")
    for key, func, job_id in [
        ("evening_keg_question_time", evening_keg_question, "evening_keg"),
        ("morning_priority_time", morning_priority, "morning_priority"),
        ("daily_task_check_time", daily_task_check, "daily_task_check"),
    ]:
        h, m = parse_time(await get_setting(key))
        scheduler.add_job(func, "cron", hour=h, minute=m, id=job_id)
    scheduler.start()


@dp.message(Command("id"))
async def my_id(message: Message):
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")


async def main():
    await init_db()
    await setup_scheduled_jobs()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
