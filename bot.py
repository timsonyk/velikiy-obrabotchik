import os
import logging
import sqlite3
from io import BytesIO
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from dotenv import load_dotenv
import asyncio

from image_processor import process_image_stream
from PIL import Image
from io import BytesIO

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота с увеличенным таймаутом для больших файлов
BOT_TOKEN = os.getenv("BOTKEY")
if not BOT_TOKEN:
    raise ValueError("BOTKEY не найден в .env файле!")

# Пароль для авторизации (из config)
from config import AUTH_PASSWORD, PROMPTS, DITE_PROMPTS

# Путь к базе данных
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")

# === ФУНКЦИИ ДЛЯ РАБОТЫ С БД ===

def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            status TEXT DEFAULT 'authorized',
            login_type TEXT DEFAULT 'veryberrylab',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            variants_count INTEGER DEFAULT 5,
            use_custom_prompt INTEGER DEFAULT 0,
            custom_prompt TEXT,
            aspect_ratio TEXT DEFAULT '1:1',
            dite_prompt_id TEXT DEFAULT 'shorten',
            saved_prompt_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    ''')
    # Таблица сохранённых промптов для каждого профиля
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS saved_prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login_type TEXT NOT NULL,
            prompt_name TEXT NOT NULL,
            prompt_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Миграции
    cursor.execute("PRAGMA table_info(user_settings)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'aspect_ratio' not in columns:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN aspect_ratio TEXT DEFAULT '1:1'")
    if 'dite_prompt_id' not in columns:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN dite_prompt_id TEXT DEFAULT 'shorten'")
    if 'saved_prompt_id' not in columns:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN saved_prompt_id INTEGER")
    # Миграция: добавляем login_type в users
    cursor.execute("PRAGMA table_info(users)")
    user_columns = [col[1] for col in cursor.fetchall()]
    if 'login_type' not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN login_type TEXT DEFAULT 'veryberrylab'")
    conn.commit()
    conn.close()
    logger.info(f"База данных инициализирована: {DB_PATH}")

def load_users_from_db():
    """Загрузить пользователей из БД"""
    authorized = set()
    blocked = set()
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, status FROM users")
    for row in cursor.fetchall():
        user_id, status = row
        if status == 'authorized':
            authorized.add(user_id)
        elif status == 'blocked':
            blocked.add(user_id)
    conn.close()
    
    logger.info(f"Загружено из БД: {len(authorized)} авторизованных, {len(blocked)} заблокированных")
    return authorized, blocked

def save_user_to_db(user_id: int, username: str, first_name: str, status: str, login_type: str = None):
    """Сохранить пользователя в БД"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if login_type:
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, first_name, status, login_type)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, username, first_name, status, login_type))
    else:
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, first_name, status)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, first_name, status))
    conn.commit()
    conn.close()
    logger.info(f"Пользователь {user_id} ({first_name}) сохранён: статус={status}, логин={login_type}")

def get_user_login_type(user_id: int) -> str:
    """Получить login_type пользователя"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT login_type FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] else "veryberrylab"

def load_user_settings_from_db(user_id: int) -> dict:
    """Загрузить настройки пользователя из БД"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT variants_count, use_custom_prompt, custom_prompt, aspect_ratio, dite_prompt_id, saved_prompt_id FROM user_settings WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return {
            'variants_count': row[0],
            'use_custom_prompt': bool(row[1]),
            'custom_prompt': row[2],
            'aspect_ratio': row[3] or '1:1',
            'dite_prompt_id': row[4] or 'shorten',
            'saved_prompt_id': row[5]
        }
    return None

def save_user_settings_to_db(user_id: int, settings: dict):
    """Сохранить настройки пользователя в БД"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO user_settings (user_id, variants_count, use_custom_prompt, custom_prompt, aspect_ratio, dite_prompt_id, saved_prompt_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, settings['variants_count'], int(settings['use_custom_prompt']), settings['custom_prompt'], settings.get('aspect_ratio', '1:1'), settings.get('dite_prompt_id', 'shorten'), settings.get('saved_prompt_id')))
    conn.commit()
    conn.close()

# === ФУНКЦИИ ДЛЯ СОХРАНЁННЫХ ПРОМПТОВ ===

def get_saved_prompts(login_type: str) -> list:
    """Получить все сохранённые промпты для профиля"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, prompt_name, prompt_text FROM saved_prompts WHERE login_type = ? ORDER BY created_at DESC",
        (login_type,)
    )
    prompts = [{"id": row[0], "name": row[1], "text": row[2]} for row in cursor.fetchall()]
    conn.close()
    return prompts

def save_prompt_to_db(login_type: str, prompt_name: str, prompt_text: str) -> int:
    """Сохранить промпт в БД и вернуть его ID"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO saved_prompts (login_type, prompt_name, prompt_text) VALUES (?, ?, ?)",
        (login_type, prompt_name, prompt_text)
    )
    prompt_id = cursor.lastrowid
    conn.commit()
    conn.close()
    logger.info(f"Сохранён промпт '{prompt_name}' для {login_type} (id={prompt_id})")
    return prompt_id

def get_saved_prompt_by_id(prompt_id: int) -> dict | None:
    """Получить промпт по ID"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, login_type, prompt_name, prompt_text FROM saved_prompts WHERE id = ?", (prompt_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "login_type": row[1], "name": row[2], "text": row[3]}
    return None

def delete_saved_prompt(prompt_id: int):
    """Удалить сохранённый промпт"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM saved_prompts WHERE id = ?", (prompt_id,))
    conn.commit()
    conn.close()
    logger.info(f"Удалён промпт id={prompt_id}")

# Инициализация БД
init_db()

# Таймаут 5 минут для отправки больших файлов (~17 МБ)
session = AiohttpSession(timeout=300)
bot = Bot(token=BOT_TOKEN, session=session)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Состояния
class UserStates(StatesGroup):
    waiting_for_login_choice = State()  # Выбор логина
    waiting_for_password = State()
    waiting_for_photo = State()
    waiting_for_custom_prompt = State()
    waiting_for_reference_photo = State()
    waiting_for_photo_with_reference = State()
    waiting_for_prompt_name = State()  # Ввод имени для сохранения промпта
    waiting_for_prompt_text = State()  # Ввод текста нового промпта

# Хранилище настроек пользователей (кэш в памяти)
user_settings = {}

# Загружаем пользователей из БД при старте
authorized_users, blocked_users = load_users_from_db()

# Очередь обработки (только 1 обработка одновременно)
processing_semaphore = asyncio.Semaphore(1)
queue_count = 0  # Счетчик ожидающих в очереди

def get_user_settings(user_id: int) -> dict:
    """Получить настройки пользователя"""
    if user_id not in user_settings:
        # Пробуем загрузить из БД
        db_settings = load_user_settings_from_db(user_id)
        if db_settings:
            user_settings[user_id] = db_settings
        else:
            user_settings[user_id] = {
                'variants_count': 5,  # По умолчанию 5 вариантов
                'use_custom_prompt': False,  # Использовать кастомный промпт
                'custom_prompt': None,  # Кастомный промпт
                'aspect_ratio': '1:1',  # Соотношение сторон
                'dite_prompt_id': 'shorten',  # Промпт для dite
                'saved_prompt_id': None  # ID сохранённого промпта
            }
    return user_settings[user_id]

def save_settings(user_id: int):
    """Сохранить настройки пользователя в БД"""
    if user_id in user_settings:
        save_user_settings_to_db(user_id, user_settings[user_id])

def get_persistent_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура внизу экрана"""
    keyboard = [[KeyboardButton(text="🏠 Главное меню")]]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_login_choice_menu() -> InlineKeyboardMarkup:
    """Меню выбора логина"""
    keyboard = [
        [InlineKeyboardButton(text="🍓 VeryBerryLab", callback_data="login_veryberrylab")],
        [InlineKeyboardButton(text="👗 Dite", callback_data="login_dite")],
        [InlineKeyboardButton(text="🥖 Bageatteria", callback_data="login_bageatteria")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_main_menu() -> InlineKeyboardMarkup:
    """Главное меню"""
    keyboard = [
        [InlineKeyboardButton(text="📸 Отправить фото", callback_data="send_photo")],
        [InlineKeyboardButton(text="🖼 Отправить с референсом", callback_data="send_with_reference")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_settings_menu(user_id: int) -> InlineKeyboardMarkup:
    """Меню настроек"""
    settings = get_user_settings(user_id)
    login_type = get_user_login_type(user_id)

    # Определяем статус промпта
    saved_prompt_id = settings.get('saved_prompt_id')
    if saved_prompt_id and not settings['use_custom_prompt']:
        saved_prompt = get_saved_prompt_by_id(saved_prompt_id)
        if saved_prompt:
            prompt_status = f"💾 {saved_prompt['name'][:15]}"
        else:
            prompt_status = "Не выбран"
    elif login_type == "dite" and not settings['use_custom_prompt']:
        dite_id = settings.get('dite_prompt_id', 'shorten')
        prompt_status = DITE_PROMPTS.get(dite_id, {}).get('name', 'Стандартный')
    elif login_type == "veryberrylab" and not settings['use_custom_prompt']:
        prompt_status = "Стандартный"
    else:
        prompt_status = "Не выбран"

    aspect_ratio = settings.get('aspect_ratio', '1:1')

    keyboard = [
        [InlineKeyboardButton(
            text=f"Количество фото: {settings['variants_count']}",
            callback_data="change_variants"
        )],
        [InlineKeyboardButton(
            text=f"Соотношение сторон: {aspect_ratio}",
            callback_data="change_ratio"
        )],
        [InlineKeyboardButton(
            text=f"Промпт: {prompt_status}",
            callback_data="change_prompt_mode"
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_variants_menu() -> InlineKeyboardMarkup:
    """Меню выбора количества вариантов"""
    keyboard = [
        [InlineKeyboardButton(text="1", callback_data="variants_1"),
         InlineKeyboardButton(text="3", callback_data="variants_3"),
         InlineKeyboardButton(text="5", callback_data="variants_5")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_ratio_menu() -> InlineKeyboardMarkup:
    """Меню выбора соотношения сторон"""
    keyboard = [
        [InlineKeyboardButton(text="1:1 (квадрат)", callback_data="ratio_1:1"),
         InlineKeyboardButton(text="3:4 (портрет)", callback_data="ratio_3:4")],
        [InlineKeyboardButton(text="16:9 (широкий)", callback_data="ratio_16:9"),
         InlineKeyboardButton(text="9:16 (вертикальный)", callback_data="ratio_9:16")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_prompt_menu(user_id: int) -> InlineKeyboardMarkup:
    """Меню выбора типа промпта"""
    settings = get_user_settings(user_id)
    login_type = get_user_login_type(user_id)
    current_saved_id = settings.get('saved_prompt_id')
    is_using_saved = current_saved_id is not None and not settings['use_custom_prompt']

    keyboard = []

    if login_type == "dite":
        # Для dite - выбор из нескольких встроенных промптов
        current_dite_prompt = settings.get('dite_prompt_id', 'shorten')
        for prompt_id, prompt_data in DITE_PROMPTS.items():
            is_current = prompt_id == current_dite_prompt and not settings['use_custom_prompt'] and not is_using_saved
            mark = "✓ " if is_current else ""
            keyboard.append([InlineKeyboardButton(
                text=f"{mark}{prompt_data['name']}",
                callback_data=f"dite_prompt_{prompt_id}"
            )])
    elif login_type == "veryberrylab":
        # Для veryberrylab - стандартный промпт
        is_default = not settings['use_custom_prompt'] and not is_using_saved
        mark = "✓ " if is_default else ""
        keyboard.append([InlineKeyboardButton(text=f"{mark}📝 Стандартный промпт", callback_data="prompt_default")])
    # Для bageatteria - нет стандартного промпта

    # Показываем сохранённые промпты для этого профиля
    saved_prompts = get_saved_prompts(login_type)
    if saved_prompts:
        keyboard.append([InlineKeyboardButton(text="── Сохранённые ──", callback_data="noop")])
        for sp in saved_prompts:
            mark = "✓ " if sp['id'] == current_saved_id and is_using_saved else ""
            keyboard.append([InlineKeyboardButton(
                text=f"{mark}💾 {sp['name'][:20]}",
                callback_data=f"use_saved_{sp['id']}"
            )])

    # Кнопка для создания нового сохраняемого промпта
    keyboard.append([InlineKeyboardButton(text="➕ Создать промпт", callback_data="create_saved_prompt")])

    # Управление сохранёнными промптами
    if saved_prompts:
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить промпт", callback_data="delete_prompt_menu")])

    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обработка команды /start"""
    user_id = message.from_user.id

    # Проверяем, заблокирован ли пользователь
    if user_id in blocked_users:
        await message.answer("🚫 Доступ запрещен.")
        return

    # Показываем выбор профиля (для всех - новых и авторизованных)
    user_name = message.from_user.first_name
    if user_id in authorized_users:
        current_login = get_user_login_type(user_id)
        await message.answer(
            f"👋 С возвращением, {user_name}!\n"
            f"Текущий профиль: {current_login}\n\n"
            "Выберите профиль:",
            reply_markup=get_login_choice_menu()
        )
    else:
        await message.answer(
            "👋 Добро пожаловать!\n\n"
            "Выберите ваш профиль:",
            reply_markup=get_login_choice_menu()
        )
    await state.set_state(UserStates.waiting_for_login_choice)

@dp.callback_query(lambda c: c.data.startswith("login_"))
async def handle_login_choice(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора логина"""
    user_id = callback.from_user.id
    login_type = callback.data.replace("login_", "")

    # Если пользователь уже авторизован - просто меняем профиль
    if user_id in authorized_users:
        username = callback.from_user.username
        first_name = callback.from_user.first_name
        save_user_to_db(user_id, username, first_name, 'authorized', login_type)
        await state.clear()

        await callback.message.answer(
            f"✅ Профиль изменён на: {login_type}\n\n"
            "Выбери действие:",
            reply_markup=get_main_menu()
        )
        await callback.message.answer("⬇️", reply_markup=get_persistent_keyboard())
        await callback.answer()
        return

    # Новый пользователь - запрашиваем пароль
    await state.update_data(chosen_login=login_type)
    await callback.message.answer("🔐 Введите пароль:")
    await state.set_state(UserStates.waiting_for_password)
    await callback.answer()

@dp.message(UserStates.waiting_for_password)
async def check_password(message: types.Message, state: FSMContext):
    """Проверка пароля"""
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name

    # Получаем выбранный логин из состояния
    state_data = await state.get_data()
    login_type = state_data.get('chosen_login', 'veryberrylab')

    if message.text == AUTH_PASSWORD:
        # Пароль верный - авторизуем
        authorized_users.add(user_id)
        blocked_users.discard(user_id)  # Убираем из заблокированных, если был там
        save_user_to_db(user_id, username, first_name, 'authorized', login_type)
        await state.clear()

        await message.answer(
            f"✅ Авторизация успешна!\n\n"
            f"👋 Привет, {first_name}!\n"
            f"Ваш профиль: {login_type}\n\n"
            "Отправь мне фото, и я создам несколько улучшенных вариантов!\n\n"
            "Выбери действие:",
            reply_markup=get_main_menu()
        )
        await message.answer("⬇️", reply_markup=get_persistent_keyboard())
    else:
        # Пароль неверный - блокируем
        blocked_users.add(user_id)
        save_user_to_db(user_id, username, first_name, 'blocked')
        await state.clear()
        await message.answer("🚫 Неверный пароль. Доступ запрещен.")

@dp.message(lambda m: m.text == "🏠 Главное меню")
async def handle_main_menu_button(message: types.Message, state: FSMContext):
    """Обработка кнопки Главное меню"""
    user_id = message.from_user.id

    if user_id in blocked_users:
        await message.answer("🚫 Доступ запрещен.")
        return

    if user_id not in authorized_users:
        await message.answer("🔐 Для доступа к боту введите /start")
        return

    await state.clear()
    await message.answer(
        "Выбери действие:",
        reply_markup=get_main_menu()
    )

async def check_auth(user_id: int) -> bool:
    """Проверка авторизации пользователя"""
    return user_id in authorized_users and user_id not in blocked_users

@dp.callback_query(lambda c: c.data == "main_menu")
async def show_main_menu(callback: types.CallbackQuery):
    """Показать главное меню"""
    if not await check_auth(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    await callback.message.edit_text(
        "Выбери действие:",
        reply_markup=get_main_menu()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "send_photo")
async def request_photo(callback: types.CallbackQuery, state: FSMContext):
    """Запросить фото у пользователя"""
    if not await check_auth(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    await callback.message.answer(
        "📸 Отправь мне фотографию, которую нужно обработать.\n"
        "Пожалуйста, отправь фото как документ (без сжатия) для лучшего качества."
    )
    await state.set_state(UserStates.waiting_for_photo)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "send_with_reference")
async def request_reference_photo(callback: types.CallbackQuery, state: FSMContext):
    """Запросить референсное фото"""
    if not await check_auth(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    await callback.message.answer(
        "🖼 Режим с референсом\n\n"
        "Шаг 1 из 2: Отправь РЕФЕРЕНСНОЕ изображение.\n"
        "Это фото, на элементы которого ты будешь ссылаться в промпте.\n\n"
        "Для отмены нажми 🏠 Главное меню"
    )
    await state.set_state(UserStates.waiting_for_reference_photo)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "settings")
async def show_settings(callback: types.CallbackQuery):
    """Показать настройки"""
    user_id = callback.from_user.id
    if not await check_auth(user_id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    await callback.message.edit_text(
        "⚙️ Настройки:",
        reply_markup=get_settings_menu(user_id)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "change_variants")
async def change_variants(callback: types.CallbackQuery):
    """Изменить количество вариантов"""
    if not await check_auth(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    await callback.message.edit_text(
        "Выбери количество генерируемых фотографий:",
        reply_markup=get_variants_menu()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("variants_"))
async def set_variants(callback: types.CallbackQuery):
    """Установить количество вариантов"""
    user_id = callback.from_user.id
    if not await check_auth(user_id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    count = int(callback.data.split("_")[1])

    settings = get_user_settings(user_id)
    settings['variants_count'] = count
    save_settings(user_id)

    await callback.message.edit_text(
        f"✅ Количество фото изменено на {count}",
        reply_markup=get_settings_menu(user_id)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "change_ratio")
async def change_ratio(callback: types.CallbackQuery):
    """Изменить соотношение сторон"""
    if not await check_auth(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    await callback.message.edit_text(
        "Выбери соотношение сторон для генерируемых фотографий:",
        reply_markup=get_ratio_menu()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("ratio_"))
async def set_ratio(callback: types.CallbackQuery):
    """Установить соотношение сторон"""
    user_id = callback.from_user.id
    if not await check_auth(user_id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    ratio = callback.data.replace("ratio_", "")

    settings = get_user_settings(user_id)
    settings['aspect_ratio'] = ratio
    save_settings(user_id)

    await callback.message.edit_text(
        f"✅ Соотношение сторон изменено на {ratio}",
        reply_markup=get_settings_menu(user_id)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "change_prompt_mode")
async def change_prompt_mode(callback: types.CallbackQuery):
    """Открыть меню выбора промпта"""
    user_id = callback.from_user.id
    if not await check_auth(user_id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    await callback.message.edit_text(
        "Выбери тип промпта для обработки изображений:",
        reply_markup=get_prompt_menu(user_id)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "prompt_default")
async def set_default_prompt(callback: types.CallbackQuery):
    """Установить стандартный промпт"""
    user_id = callback.from_user.id
    if not await check_auth(user_id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    settings = get_user_settings(user_id)
    settings['use_custom_prompt'] = False
    settings['saved_prompt_id'] = None  # Сбрасываем сохранённый промпт
    save_settings(user_id)

    await callback.message.edit_text(
        "✅ Установлен стандартный промпт",
        reply_markup=get_settings_menu(user_id)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("dite_prompt_"))
async def set_dite_prompt(callback: types.CallbackQuery):
    """Выбор промпта для dite"""
    user_id = callback.from_user.id
    if not await check_auth(user_id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    prompt_id = callback.data.replace("dite_prompt_", "")
    if prompt_id not in DITE_PROMPTS:
        await callback.answer("Неверный промпт", show_alert=True)
        return

    settings = get_user_settings(user_id)
    settings['dite_prompt_id'] = prompt_id
    settings['use_custom_prompt'] = False  # Отключаем кастомный промпт
    settings['saved_prompt_id'] = None  # Сбрасываем сохранённый промпт
    save_settings(user_id)

    prompt_name = DITE_PROMPTS[prompt_id]['name']
    await callback.message.edit_text(
        f"✅ Выбран промпт: {prompt_name}",
        reply_markup=get_settings_menu(user_id)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "prompt_custom")
async def set_custom_prompt(callback: types.CallbackQuery):
    """Использовать кастомный промпт"""
    user_id = callback.from_user.id
    if not await check_auth(user_id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    settings = get_user_settings(user_id)

    if settings['custom_prompt'] is None:
        await callback.answer("Сначала добавьте свой промпт!", show_alert=True)
        return

    settings['use_custom_prompt'] = True
    save_settings(user_id)

    await callback.message.edit_text(
        "✅ Установлен ваш кастомный промпт",
        reply_markup=get_settings_menu(user_id)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "prompt_new")
async def request_new_prompt(callback: types.CallbackQuery, state: FSMContext):
    """Запросить новый промпт"""
    if not await check_auth(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    await callback.message.answer(
        "Отправь свой промпт для обработки изображений.\n\n"
        "Промпт должен описывать, как нужно обработать изображение.\n"
        "Например: 'Создай реалистичное фото с мягким освещением и белым фоном'\n\n"
        "Для отмены отправь /cancel"
    )
    await state.set_state(UserStates.waiting_for_custom_prompt)
    await callback.answer()

@dp.message(UserStates.waiting_for_custom_prompt)
async def save_custom_prompt(message: types.Message, state: FSMContext):
    """Сохранить кастомный промпт"""
    user_id = message.from_user.id
    if not await check_auth(user_id):
        await message.answer("🚫 Доступ запрещен.")
        await state.clear()
        return

    # Проверяем, что это текстовое сообщение
    if not message.text:
        await message.answer(
            "⚠️ Пожалуйста, отправьте текст промпта.",
            reply_markup=get_main_menu()
        )
        return

    if message.text == "/cancel":
        await message.answer(
            "Отменено",
            reply_markup=get_main_menu()
        )
        await state.clear()
        return

    settings = get_user_settings(user_id)
    settings['custom_prompt'] = message.text
    settings['use_custom_prompt'] = True
    save_settings(user_id)

    prompt_preview = message.text[:100] + ('...' if len(message.text) > 100 else '')
    await message.answer(
        f"✅ Промпт сохранен!\n\n"
        f"Ваш промпт:\n{prompt_preview}",
        reply_markup=get_settings_menu(user_id)
    )
    await state.clear()

# === ОБРАБОТЧИКИ ДЛЯ СОХРАНЁННЫХ ПРОМПТОВ ===

@dp.callback_query(lambda c: c.data == "noop")
async def handle_noop(callback: types.CallbackQuery):
    """Ничего не делать (для разделителей)"""
    await callback.answer()

@dp.callback_query(lambda c: c.data == "create_saved_prompt")
async def create_saved_prompt(callback: types.CallbackQuery, state: FSMContext):
    """Начать создание сохраняемого промпта"""
    if not await check_auth(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    await callback.message.answer(
        "📝 Создание нового промпта\n\n"
        "Шаг 1/2: Введите НАЗВАНИЕ для промпта.\n"
        "Например: 'Светлый фон', 'Ретро стиль', 'Каталожное фото'\n\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(UserStates.waiting_for_prompt_name)
    await callback.answer()

@dp.message(UserStates.waiting_for_prompt_name)
async def handle_prompt_name(message: types.Message, state: FSMContext):
    """Получить имя промпта"""
    user_id = message.from_user.id
    if not await check_auth(user_id):
        await message.answer("🚫 Доступ запрещен.")
        await state.clear()
        return

    if message.text == "/cancel":
        await message.answer("Отменено", reply_markup=get_main_menu())
        await state.clear()
        return

    prompt_name = message.text.strip()[:50]  # Ограничиваем длину имени
    await state.update_data(prompt_name=prompt_name)

    await message.answer(
        f"✅ Название: {prompt_name}\n\n"
        "Шаг 2/2: Теперь введите ТЕКСТ промпта.\n"
        "Опишите, как нужно обработать изображение.\n\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(UserStates.waiting_for_prompt_text)

@dp.message(UserStates.waiting_for_prompt_text)
async def handle_prompt_text(message: types.Message, state: FSMContext):
    """Получить текст промпта и сохранить"""
    user_id = message.from_user.id
    if not await check_auth(user_id):
        await message.answer("🚫 Доступ запрещен.")
        await state.clear()
        return

    if message.text == "/cancel":
        await message.answer("Отменено", reply_markup=get_main_menu())
        await state.clear()
        return

    state_data = await state.get_data()
    prompt_name = state_data.get('prompt_name', 'Без названия')
    prompt_text = message.text

    login_type = get_user_login_type(user_id)
    prompt_id = save_prompt_to_db(login_type, prompt_name, prompt_text)

    # Автоматически выбираем созданный промпт
    settings = get_user_settings(user_id)
    settings['saved_prompt_id'] = prompt_id
    settings['use_custom_prompt'] = False
    save_settings(user_id)

    await message.answer(
        f"✅ Промпт '{prompt_name}' сохранён!\n\n"
        f"Текст:\n{prompt_text[:100]}{'...' if len(prompt_text) > 100 else ''}\n\n"
        f"Этот промпт теперь доступен всем пользователям профиля {login_type}.",
        reply_markup=get_settings_menu(user_id)
    )
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("use_saved_"))
async def use_saved_prompt(callback: types.CallbackQuery):
    """Использовать сохранённый промпт"""
    user_id = callback.from_user.id
    if not await check_auth(user_id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    prompt_id = int(callback.data.replace("use_saved_", ""))
    saved_prompt = get_saved_prompt_by_id(prompt_id)

    if not saved_prompt:
        await callback.answer("Промпт не найден", show_alert=True)
        return

    settings = get_user_settings(user_id)
    settings['saved_prompt_id'] = prompt_id
    settings['use_custom_prompt'] = False
    save_settings(user_id)

    await callback.message.edit_text(
        f"✅ Выбран промпт: {saved_prompt['name']}",
        reply_markup=get_settings_menu(user_id)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "delete_prompt_menu")
async def show_delete_prompt_menu(callback: types.CallbackQuery):
    """Показать меню удаления промптов"""
    user_id = callback.from_user.id
    if not await check_auth(user_id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    login_type = get_user_login_type(user_id)
    saved_prompts = get_saved_prompts(login_type)

    if not saved_prompts:
        await callback.answer("Нет сохранённых промптов", show_alert=True)
        return

    keyboard = []
    for sp in saved_prompts:
        keyboard.append([InlineKeyboardButton(
            text=f"🗑 {sp['name'][:25]}",
            callback_data=f"delete_saved_{sp['id']}"
        )])
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="change_prompt_mode")])

    await callback.message.edit_text(
        "Выберите промпт для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("delete_saved_"))
async def delete_saved_prompt_handler(callback: types.CallbackQuery):
    """Удалить сохранённый промпт"""
    user_id = callback.from_user.id
    if not await check_auth(user_id):
        await callback.answer("🚫 Доступ запрещен", show_alert=True)
        return

    prompt_id = int(callback.data.replace("delete_saved_", ""))
    saved_prompt = get_saved_prompt_by_id(prompt_id)

    if not saved_prompt:
        await callback.answer("Промпт не найден", show_alert=True)
        return

    prompt_name = saved_prompt['name']
    delete_saved_prompt(prompt_id)

    # Если удалённый промпт был выбран, сбрасываем настройку
    settings = get_user_settings(user_id)
    if settings.get('saved_prompt_id') == prompt_id:
        settings['saved_prompt_id'] = None
        save_settings(user_id)

    await callback.message.edit_text(
        f"✅ Промпт '{prompt_name}' удалён",
        reply_markup=get_settings_menu(user_id)
    )
    await callback.answer()

@dp.message(UserStates.waiting_for_reference_photo, lambda m: m.photo or (m.document and m.document.mime_type and m.document.mime_type.startswith('image/')))
async def handle_reference_photo(message: types.Message, state: FSMContext):
    """Обработка референсного фото"""
    user_id = message.from_user.id
    if not await check_auth(user_id):
        await message.answer("🚫 Доступ запрещен.")
        await state.clear()
        return

    if message.photo:
        file_id = message.photo[-1].file_id
    else:
        file_id = message.document.file_id

    try:
        file = await bot.get_file(file_id)
        file_bytes = await bot.download_file(file.file_path)
        reference_bytes = file_bytes.read()
        await state.update_data(reference_image=reference_bytes)

        await message.answer(
            "✅ Референс получен!\n\n"
            "Шаг 2 из 2: Теперь отправь ОСНОВНОЕ изображение для обработки."
        )
        await state.set_state(UserStates.waiting_for_photo_with_reference)
    except Exception as e:
        logger.error(f"Ошибка при получении референса: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}", reply_markup=get_main_menu())
        await state.clear()

@dp.message(UserStates.waiting_for_photo_with_reference, lambda m: m.photo or (m.document and m.document.mime_type and m.document.mime_type.startswith('image/')))
async def handle_photo_with_reference(message: types.Message, state: FSMContext):
    """Обработка фото с референсом"""
    global queue_count
    user_id = message.from_user.id
    if not await check_auth(user_id):
        await message.answer("🚫 Доступ запрещен.")
        await state.clear()
        return

    state_data = await state.get_data()
    reference_bytes = state_data.get('reference_image')
    if not reference_bytes:
        await message.answer("❌ Референс не найден. Начните сначала.", reply_markup=get_main_menu())
        await state.clear()
        return

    if message.photo:
        file_id = message.photo[-1].file_id
    else:
        file_id = message.document.file_id

    if processing_semaphore.locked():
        queue_count += 1
        processing_msg = await message.answer(f"⏳ Вы {queue_count}-й в очереди...")
    else:
        processing_msg = await message.answer("⏳ Обрабатываю с референсом...")

    await state.clear()

    async with processing_semaphore:
        if queue_count > 0:
            queue_count -= 1
        try:
            await processing_msg.edit_text("⏳ Скачиваю фото...")
            file = await bot.get_file(file_id)
            file_bytes = await bot.download_file(file.file_path)
            main_bytes = file_bytes.read()

            settings = get_user_settings(user_id)
            variants_count = settings['variants_count']
            aspect_ratio = settings.get('aspect_ratio', '1:1')

            # Определяем промпт
            login_type = get_user_login_type(user_id)
            saved_prompt_id = settings.get('saved_prompt_id')

            if saved_prompt_id and not settings['use_custom_prompt']:
                # Используем сохранённый промпт
                saved_prompt = get_saved_prompt_by_id(saved_prompt_id)
                custom_prompt = saved_prompt['text'] if saved_prompt else None
            elif settings['use_custom_prompt'] and settings['custom_prompt']:
                custom_prompt = settings['custom_prompt']
            elif login_type == "dite":
                dite_prompt_id = settings.get('dite_prompt_id', 'shorten')
                custom_prompt = DITE_PROMPTS.get(dite_prompt_id, {}).get('text')
            else:
                custom_prompt = PROMPTS.get(login_type)

            await processing_msg.edit_text(f"⏳ Генерирую {variants_count} вариантов с референсом...")

            from image_processor import process_image_with_reference_stream
            sent_count = 0
            async for idx, img_bytes in process_image_with_reference_stream(main_bytes, reference_bytes, variants_count, custom_prompt, aspect_ratio):
                sent_count += 1
                img = Image.open(BytesIO(img_bytes))
                img.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                preview_buffer = BytesIO()
                img.save(preview_buffer, format='JPEG', quality=85)
                await message.answer_photo(BufferedInputFile(preview_buffer.getvalue(), f"preview_{idx}.jpg"), caption=f"Вариант {idx} (превью)")
                await message.answer_document(BufferedInputFile(img_bytes, f"result_{idx}.png"), caption=f"Вариант {idx} (полное качество)")

            if sent_count == 0:
                await processing_msg.edit_text("❌ Ошибка обработки.", reply_markup=get_main_menu())
            else:
                await processing_msg.edit_text(f"✅ Готово! {sent_count} вариантов с референсом.", reply_markup=get_main_menu())
        except Exception as e:
            logger.error(f"Ошибка: {e}")
            await message.answer(f"❌ Ошибка: {str(e)}", reply_markup=get_main_menu())

@dp.message(lambda m: m.photo or (m.document and m.document.mime_type and m.document.mime_type.startswith('image/')))
async def handle_any_photo(message: types.Message, state: FSMContext):
    """Обработка любого фото сразу при отправке"""
    global queue_count
    user_id = message.from_user.id

    # Проверяем авторизацию
    if not await check_auth(user_id):
        await message.answer("🚫 Доступ запрещен. Введите /start для авторизации.")
        return

    # Сбрасываем состояние если было
    await state.clear()

    # Получаем file_id
    if message.photo:
        file_id = message.photo[-1].file_id
    else:
        file_id = message.document.file_id

    # Проверяем очередь
    if processing_semaphore.locked():
        queue_count += 1
        queue_pos = queue_count
        processing_msg = await message.answer(f"⏳ Сервер занят. Вы {queue_pos}-й в очереди, подождите...")
    else:
        processing_msg = await message.answer("⏳ Обрабатываю фото...")

    # Ждем своей очереди
    async with processing_semaphore:
        if queue_count > 0:
            queue_count -= 1

        try:
            await processing_msg.edit_text("⏳ Скачиваю фото...")

            file = await bot.get_file(file_id)
            file_bytes = await bot.download_file(file.file_path)

            # Получаем настройки пользователя
            settings = get_user_settings(user_id)
            variants_count = settings['variants_count']
            aspect_ratio = settings.get('aspect_ratio', '1:1')

            # Определяем какой промпт использовать
            login_type = get_user_login_type(user_id)
            saved_prompt_id = settings.get('saved_prompt_id')

            if saved_prompt_id and not settings['use_custom_prompt']:
                # Используем сохранённый промпт
                saved_prompt = get_saved_prompt_by_id(saved_prompt_id)
                custom_prompt = saved_prompt['text'] if saved_prompt else None
            elif settings['use_custom_prompt'] and settings['custom_prompt']:
                custom_prompt = settings['custom_prompt']
            elif login_type == "dite":
                dite_prompt_id = settings.get('dite_prompt_id', 'shorten')
                custom_prompt = DITE_PROMPTS.get(dite_prompt_id, {}).get('text')
            else:
                custom_prompt = PROMPTS.get(login_type)  # None для bageatteria

            await processing_msg.edit_text(
                f"⏳ Генерирую {variants_count} вариантов ({aspect_ratio})...\n"
                "Отправляю каждый по мере готовности!"
            )

            # Обрабатываем и отправляем по мере готовности
            sent_count = 0
            async for idx, img_bytes in process_image_stream(file_bytes, variants_count, custom_prompt, aspect_ratio):
                sent_count += 1

                # Создаем сжатое превью (макс 1280px, JPEG качество 85)
                img = Image.open(BytesIO(img_bytes))
                img.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                preview_buffer = BytesIO()
                img.save(preview_buffer, format='JPEG', quality=85)
                preview_bytes = preview_buffer.getvalue()

                # Отправляем превью (сжатое)
                await message.answer_photo(
                    BufferedInputFile(preview_bytes, filename=f"preview_{idx}.jpg"),
                    caption=f"Вариант {idx} (превью)"
                )

                # Отправляем оригинал (без сжатия)
                await message.answer_document(
                    BufferedInputFile(img_bytes, filename=f"result_{idx}.png"),
                    caption=f"Вариант {idx} (полное качество)"
                )

            if sent_count == 0:
                await processing_msg.edit_text(
                    "❌ Ошибка при обработке изображения. Попробуйте еще раз.",
                    reply_markup=get_main_menu()
                )
                return

            await processing_msg.edit_text(
                f"✅ Готово! Отправлено {sent_count} вариантов.",
                reply_markup=get_main_menu()
            )

        except Exception as e:
            logger.error(f"Ошибка при обработке фото: {e}")
            await message.answer(
                f"❌ Произошла ошибка: {str(e)}",
                reply_markup=get_main_menu()
            )

async def main():
    """Запуск бота"""
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
