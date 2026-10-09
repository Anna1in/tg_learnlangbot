"""Шар роботи з PostgreSQL (asyncpg): схема, стартові слова, запити."""
from datetime import datetime, timedelta, timezone

import asyncpg

LANG_CODES = ("uk", "ru", "en")   # мови перекладу
LEARN_CODES = ("en", "it")        # мови, які можна вивчати
CATEGORIES = ["greetings", "polite", "basic", "food", "custom"]
MAX_CUSTOM_WORDS = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id         BIGINT PRIMARY KEY,
    lang            TEXT,                              -- мова перекладу (uk/ru/en)
    learn_lang      TEXT NOT NULL DEFAULT 'en',        -- мова, яку вивчають (en/it)
    ai_requests     INT NOT NULL DEFAULT 0,            -- лічильник AI-запитів
    ai_window_start TIMESTAMPTZ,                       -- початок поточного 7-денного вікна
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS words (
    id               SERIAL PRIMARY KEY,
    original         TEXT NOT NULL,
    translation      TEXT NOT NULL,
    translation_lang TEXT NOT NULL,
    learn_lang       TEXT NOT NULL DEFAULT 'en',
    category         TEXT NOT NULL DEFAULT 'custom',
    owner_id         BIGINT REFERENCES users(user_id) ON DELETE CASCADE  -- NULL = системне слово
);
CREATE INDEX IF NOT EXISTS ix_words_owner ON words (owner_id);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id          BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    mode             TEXT NOT NULL DEFAULT 'interval',  -- interval | twice_daily | off
    interval_minutes INT NOT NULL DEFAULT 1440,
    category         TEXT,                              -- NULL = усі слова
    last_sent_at     TIMESTAMPTZ
);

-- Міграція для баз, створених до появи італійської
ALTER TABLE users ADD COLUMN IF NOT EXISTS learn_lang TEXT NOT NULL DEFAULT 'en';
ALTER TABLE words ADD COLUMN IF NOT EXISTS learn_lang TEXT NOT NULL DEFAULT 'en';
DROP INDEX IF EXISTS ux_system_words;
CREATE UNIQUE INDEX IF NOT EXISTS ux_system_words_v2
    ON words (learn_lang, original, translation_lang) WHERE owner_id IS NULL;
"""

# 10 стартових слів: (оригінал, {мова: переклад}, категорія).
# Для мови "en" переклад — коротке англійське пояснення.
STARTER_EN = [
    ("Hello", {"uk": "Привіт", "ru": "Привет", "en": "A common greeting"}, "greetings"),
    ("Goodbye", {"uk": "До побачення", "ru": "До свидания", "en": "Said when leaving"}, "greetings"),
    ("Thank you", {"uk": "Дякую", "ru": "Спасибо", "en": "Said to show gratitude"}, "polite"),
    ("Please", {"uk": "Будь ласка", "ru": "Пожалуйста", "en": "Polite word used when asking"}, "polite"),
    ("Sorry", {"uk": "Вибач", "ru": "Извини", "en": "Said when you apologise"}, "polite"),
    ("Yes", {"uk": "Так", "ru": "Да", "en": "The opposite of no"}, "basic"),
    ("No", {"uk": "Ні", "ru": "Нет", "en": "The opposite of yes"}, "basic"),
    ("Help", {"uk": "Допомога", "ru": "Помощь", "en": "To assist someone"}, "basic"),
    ("Water", {"uk": "Вода", "ru": "Вода", "en": "A clear liquid you drink"}, "food"),
    ("Bread", {"uk": "Хліб", "ru": "Хлеб", "en": "Baked food made from flour"}, "food"),
]

# Італійські стартові слова (переклад на en — справжній переклад).
STARTER_IT = [
    ("Ciao", {"uk": "Привіт / Бувай", "ru": "Привет / Пока", "en": "Hello / Bye"}, "greetings"),
    ("Arrivederci", {"uk": "До побачення", "ru": "До свидания", "en": "Goodbye"}, "greetings"),
    ("Grazie", {"uk": "Дякую", "ru": "Спасибо", "en": "Thank you"}, "polite"),
    ("Per favore", {"uk": "Будь ласка", "ru": "Пожалуйста", "en": "Please"}, "polite"),
    ("Scusa", {"uk": "Вибач", "ru": "Извини", "en": "Sorry"}, "polite"),
    ("Sì", {"uk": "Так", "ru": "Да", "en": "Yes"}, "basic"),
    ("No", {"uk": "Ні", "ru": "Нет", "en": "No"}, "basic"),
    ("Aiuto", {"uk": "Допомога", "ru": "Помощь", "en": "Help"}, "basic"),
    ("Acqua", {"uk": "Вода", "ru": "Вода", "en": "Water"}, "food"),
    ("Pane", {"uk": "Хліб", "ru": "Хлеб", "en": "Bread"}, "food"),
]

STARTER = {"en": STARTER_EN, "it": STARTER_IT}

# Слова, доступні користувачу: системні (на його мові) + його власні.
POOL_WHERE = """(
    (owner_id IS NULL AND translation_lang = $2 AND learn_lang = $4::text
        AND ($3::text IS NULL OR category = $3))
    OR (owner_id = $1 AND learn_lang = $4::text AND ($3::text IS NULL OR category = $3))
)"""


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=5)


async def init(pool: asyncpg.Pool) -> None:
    await pool.execute(SCHEMA)
    rows = [
        (orig, tr[lang], lang, learn, cat)
        for learn, words in STARTER.items()
        for orig, tr, cat in words
        for lang in LANG_CODES
    ]
    await pool.executemany(
        """INSERT INTO words (original, translation, translation_lang, learn_lang, category, owner_id)
           VALUES ($1, $2, $3, $4, $5, NULL)
           ON CONFLICT (learn_lang, original, translation_lang) WHERE owner_id IS NULL
           DO NOTHING""",
        rows,
    )


# ---------- Users / settings ----------

async def ensure_user(pool, user_id: int) -> None:
    await pool.execute(
        "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
    )
    await pool.execute(
        "INSERT INTO user_settings (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
    )


async def get_lang(pool, user_id: int) -> str | None:
    return await pool.fetchval("SELECT lang FROM users WHERE user_id = $1", user_id)


async def set_lang(pool, user_id: int, lang: str) -> None:
    await ensure_user(pool, user_id)
    await pool.execute("UPDATE users SET lang = $2 WHERE user_id = $1", user_id, lang)


async def get_learn_lang(pool, user_id: int) -> str:
    return await pool.fetchval("SELECT learn_lang FROM users WHERE user_id = $1", user_id) or "en"


async def set_learn_lang(pool, user_id: int, learn: str) -> None:
    await ensure_user(pool, user_id)
    await pool.execute("UPDATE users SET learn_lang = $2 WHERE user_id = $1", user_id, learn)


async def get_settings(pool, user_id: int):
    return await pool.fetchrow("SELECT * FROM user_settings WHERE user_id = $1", user_id)


async def set_interval(pool, user_id: int, mode: str, minutes: int | None = None) -> None:
    await pool.execute(
        """UPDATE user_settings
           SET mode = $2, interval_minutes = COALESCE($3::int, interval_minutes)
           WHERE user_id = $1""",
        user_id, mode, minutes,
    )


async def set_category(pool, user_id: int, category: str | None) -> None:
    await pool.execute(
        "UPDATE user_settings SET category = $2 WHERE user_id = $1", user_id, category
    )


# ---------- Words ----------

async def add_word(
    pool, user_id: int, original: str, translation: str, lang: str, learn: str
) -> bool:
    count = await pool.fetchval("SELECT count(*) FROM words WHERE owner_id = $1", user_id)
    if count >= MAX_CUSTOM_WORDS:
        return False
    await pool.execute(
        """INSERT INTO words (original, translation, translation_lang, learn_lang, category, owner_id)
           VALUES ($1, $2, $3, $4, 'custom', $5)""",
        original, translation, lang, learn, user_id,
    )
    return True


async def list_words(
    pool, user_id: int, lang: str, learn: str, category: str | None, limit: int = 60
):
    return await pool.fetch(
        f"""SELECT original, translation, category FROM words
            WHERE {POOL_WHERE}
            ORDER BY category, original LIMIT $5""",
        user_id, lang, category, learn, limit,
    )


async def random_word(pool, user_id: int, lang: str, learn: str, category: str | None):
    return await pool.fetchrow(
        f"""SELECT original, translation, category FROM words
            WHERE {POOL_WHERE}
            ORDER BY random() LIMIT 1""",
        user_id, lang, category, learn,
    )


# ---------- AI rate limit (N запитів на 7 днів, вікно стартує з першого запиту) ----------

async def ai_status(pool, user_id: int, limit: int, window: timedelta):
    """Повертає (залишилось запитів, коли оновиться | None)."""
    row = await pool.fetchrow(
        "SELECT ai_requests, ai_window_start FROM users WHERE user_id = $1", user_id
    )
    now = datetime.now(timezone.utc)
    if row is None or row["ai_window_start"] is None or now - row["ai_window_start"] >= window:
        return limit, None
    return max(limit - row["ai_requests"], 0), row["ai_window_start"] + window


async def reserve_ai(pool, user_id: int, limit: int, window: timedelta):
    """Атомарно резервує запит. Повертає (ok, залишилось, коли оновиться)."""
    row = await pool.fetchrow(
        """UPDATE users SET
               ai_requests = CASE
                   WHEN ai_window_start IS NULL OR now() - ai_window_start >= $2::interval
                   THEN 1 ELSE ai_requests + 1 END,
               ai_window_start = CASE
                   WHEN ai_window_start IS NULL OR now() - ai_window_start >= $2::interval
                   THEN now() ELSE ai_window_start END
           WHERE user_id = $1
             AND (ai_window_start IS NULL
                  OR now() - ai_window_start >= $2::interval
                  OR ai_requests < $3::int)
           RETURNING ai_requests, ai_window_start""",
        user_id, window, limit,
    )
    if row:
        return True, limit - row["ai_requests"], row["ai_window_start"] + window
    _, reset_at = await ai_status(pool, user_id, limit, window)
    return False, 0, reset_at


async def refund_ai(pool, user_id: int) -> None:
    await pool.execute(
        "UPDATE users SET ai_requests = GREATEST(ai_requests - 1, 0) WHERE user_id = $1",
        user_id,
    )


# ---------- Notifications ----------

async def users_for_notifications(pool):
    return await pool.fetch(
        """SELECT u.user_id, u.lang, u.learn_lang, s.mode, s.interval_minutes, s.category,
                  s.last_sent_at
           FROM users u JOIN user_settings s USING (user_id)
           WHERE u.lang IS NOT NULL AND s.mode <> 'off'"""
    )


async def touch_sent(pool, user_id: int) -> None:
    await pool.execute(
        "UPDATE user_settings SET last_sent_at = now() WHERE user_id = $1", user_id
    )


async def disable_notifications(pool, user_id: int) -> None:
    await pool.execute("UPDATE user_settings SET mode = 'off' WHERE user_id = $1", user_id)
