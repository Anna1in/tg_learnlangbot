"""Vocabulary Bot — Telegram-бот для вивчення слів (python-telegram-bot + PostgreSQL + Gemini)."""
from dotenv import load_dotenv

load_dotenv()  # підхоплює .env локально; має виконатися до імпорту модуля ai

import asyncio
import html
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import ai
import db
from texts import CHOOSE_LANG, LANGS, T, cat_label, t

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("vocab-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Kyiv"))
AI_LIMIT = int(os.environ.get("AI_WEEKLY_LIMIT", "5"))
AI_WINDOW = timedelta(days=7)
MORNING_HOUR, EVENING_HOUR = 9, 20
# Нічний режим: у цей проміжок сповіщення не надсилаються (за часовим поясом TZ_NAME)
QUIET_START = int(os.environ.get("QUIET_HOURS_START", "22"))
QUIET_END = int(os.environ.get("QUIET_HOURS_END", "9"))
QUIZ_LEN = 10  # питань в одній вікторині

INTERVAL_OPTIONS = [  # (callback value, ключ тексту)
    ("60", "i_60"),
    ("120", "i_120"),
    ("240", "i_240"),
    ("1440", "i_1440"),
    ("twice", "i_twice"),
    ("off", "i_off"),
]

# ---------- утиліти ----------


def btn_filter(key: str) -> filters.BaseFilter:
    """Фільтр на текст кнопки Reply Keyboard будь-якою мовою інтерфейсу."""
    return filters.Regex("^(" + "|".join(re.escape(T[l][key]) for l in T) + ")$")


MENU_KEYS = (
    "btn_add", "btn_words", "btn_quiz", "btn_ai", "btn_interval", "btn_lang", "btn_learn",
)
MENU_FILTER = filters.Regex(
    "^(" + "|".join(re.escape(T[l][k]) for l in T for k in MENU_KEYS) + ")$"
)
TEXT_INPUT = filters.TEXT & ~filters.COMMAND & ~MENU_FILTER


def pool_of(ctx: ContextTypes.DEFAULT_TYPE):
    return ctx.application.bot_data["db"]


def main_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [t(lang, "btn_add"), t(lang, "btn_words")],
            [t(lang, "btn_quiz"), t(lang, "btn_ai")],
            [t(lang, "btn_interval"), t(lang, "btn_learn")],
            [t(lang, "btn_lang")],
        ],
        resize_keyboard=True,
    )


def lang_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(name, callback_data=f"lang:{code}")] for code, name in LANGS.items()]
    )


async def lang_of(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    lang = ctx.user_data.get("lang")
    if not lang:
        lang = await db.get_lang(pool_of(ctx), update.effective_user.id)
        if lang:
            ctx.user_data["lang"] = lang
    return lang


async def learn_of(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """Мова, яку вивчає користувач (en / it)."""
    learn = ctx.user_data.get("learn")
    if not learn:
        learn = await db.get_learn_lang(pool_of(ctx), update.effective_user.id)
        ctx.user_data["learn"] = learn
    return learn


def learn_name(lang: str, learn: str) -> str:
    return t(lang, f"learn_{learn}")


async def require_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Повертає мову користувача або показує вибір мови (перший запуск)."""
    await db.ensure_user(pool_of(ctx), update.effective_user.id)
    lang = await lang_of(update, ctx)
    if not lang:
        await update.effective_message.reply_text(CHOOSE_LANG, reply_markup=lang_inline_kb())
    return lang


def fmt_dt(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M") if dt else "—"


# ---------- /start та вибір мови (2.1) ----------


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await require_lang(update, ctx)
    if lang:
        learn = await learn_of(update, ctx)
        await update.message.reply_text(
            t(lang, "menu_hint", learn=learn_name(lang, learn)), reply_markup=main_kb(lang)
        )


async def language_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await db.ensure_user(pool_of(ctx), update.effective_user.id)
    await update.effective_message.reply_text(CHOOSE_LANG, reply_markup=lang_inline_kb())


async def on_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    lang = q.data.split(":", 1)[1]
    if lang not in LANGS:
        return
    await db.set_lang(pool_of(ctx), q.from_user.id, lang)
    ctx.user_data["lang"] = lang
    await q.edit_message_text(t(lang, "lang_set"))
    learn = await learn_of(update, ctx)
    await q.message.reply_text(
        t(lang, "menu_hint", learn=learn_name(lang, learn)), reply_markup=main_kb(lang)
    )


# ---------- Вибір мови, яку вивчаємо (англійська / італійська) ----------


async def learn_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await require_lang(update, ctx)
    if not lang:
        return
    learn = await learn_of(update, ctx)
    rows = [
        [
            InlineKeyboardButton(
                ("✅ " if code == learn else "") + learn_name(lang, code),
                callback_data=f"learn:{code}",
            )
        ]
        for code in db.LEARN_CODES
    ]
    await update.effective_message.reply_text(
        t(lang, "learn_title", current=learn_name(lang, learn)),
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_learn(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    code = q.data.split(":", 1)[1]
    if code not in db.LEARN_CODES:
        return
    lang = await lang_of(update, ctx) or "en"
    pool = pool_of(ctx)
    await db.set_learn_lang(pool, q.from_user.id, code)
    await db.set_category(pool, q.from_user.id, None)  # нова мова — показуємо всі слова
    ctx.user_data["learn"] = code
    await q.edit_message_text(t(lang, "learn_set", label=learn_name(lang, code)))


# ---------- Додавання власних слів (2.3) ----------

W_WORD, W_TR = range(2)


async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    lang = await require_lang(update, ctx)
    if not lang:
        return ConversationHandler.END
    await update.message.reply_text(t(lang, "add_word_prompt"))
    return W_WORD


async def add_got_word(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    lang = await lang_of(update, ctx) or "en"
    text = update.message.text.strip()
    if not 1 <= len(text) <= 100:
        await update.message.reply_text(t(lang, "word_invalid"))
        return W_WORD
    ctx.user_data["new_word"] = text
    await update.message.reply_text(t(lang, "add_tr_prompt", word=text))
    return W_TR


async def add_got_translation(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    lang = await lang_of(update, ctx) or "en"
    tr = update.message.text.strip()
    word = ctx.user_data.pop("new_word", None)
    if not word:
        return ConversationHandler.END
    if not 1 <= len(tr) <= 100:
        ctx.user_data["new_word"] = word
        await update.message.reply_text(t(lang, "word_invalid"))
        return W_TR
    learn = await learn_of(update, ctx)
    ok = await db.add_word(pool_of(ctx), update.effective_user.id, word, tr, lang, learn)
    if ok:
        await update.message.reply_text(t(lang, "word_saved", word=word, tr=tr))
    else:
        await update.message.reply_text(t(lang, "words_limit", n=db.MAX_CUSTOM_WORDS))
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    lang = await lang_of(update, ctx) or "en"
    ctx.user_data.pop("new_word", None)
    await update.message.reply_text(t(lang, "cancelled"), reply_markup=main_kb(lang))
    return ConversationHandler.END


# ---------- Категорії та список слів (2.2) ----------


async def words_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await require_lang(update, ctx)
    if not lang:
        return
    pool = pool_of(ctx)
    s = await db.get_settings(pool, update.effective_user.id)
    current = s["category"] if s else None
    learn = await learn_of(update, ctx)
    cats = [None, *await db.list_categories(pool, lang, learn), "custom"]
    buttons = []
    for cat in cats:
        label = cat_label(lang, cat)
        if cat == current:
            label = "✅ " + label
        buttons.append(InlineKeyboardButton(label, callback_data=f"cat:{cat or 'all'}"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    await update.effective_message.reply_text(
        t(lang, "cat_title"), reply_markup=InlineKeyboardMarkup(rows)
    )


async def on_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    cat = q.data.split(":", 1)[1]
    cat = None if cat == "all" else cat
    lang = await lang_of(update, ctx) or "en"
    learn = await learn_of(update, ctx)
    pool, uid = pool_of(ctx), q.from_user.id
    if cat is not None and cat not in [*await db.list_categories(pool, lang, learn), "custom"]:
        return
    await db.set_category(pool, uid, cat)
    words = await db.list_words(pool, uid, lang, learn, cat, limit=1000)
    header = t(lang, "words_header", cat=cat_label(lang, cat), n=len(words))
    lines = (
        [f"• {w['original']} — {w['translation']}" for w in words]
        if words
        else [t(lang, "words_empty")]
    )
    # Telegram обмежує повідомлення 4096 символами — довгі списки ділимо на частини
    chunks, cur = [], header + "\n"
    for line in lines:
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += "\n" + line if cur else line
    chunks.append(cur)
    await q.edit_message_text(chunks[0])
    for extra in chunks[1:]:
        await q.message.reply_text(extra)


# ---------- AI-пояснення з лімітом 5/тиждень (2.4) ----------

AI_ASK = 0


async def ai_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    lang = await require_lang(update, ctx)
    if not lang:
        return ConversationHandler.END
    left, reset_at = await db.ai_status(
        pool_of(ctx), update.effective_user.id, AI_LIMIT, AI_WINDOW
    )
    if left <= 0:
        await update.message.reply_text(
            t(lang, "ai_limit", limit=AI_LIMIT, when=fmt_dt(reset_at))
        )
        return ConversationHandler.END
    await update.message.reply_text(t(lang, "ai_prompt", left=left, limit=AI_LIMIT))
    return AI_ASK


async def ai_got_term(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    lang = await lang_of(update, ctx) or "en"
    uid = update.effective_user.id
    term = update.message.text.strip()
    if not 1 <= len(term) <= 100:
        await update.message.reply_text(t(lang, "word_invalid"))
        return AI_ASK

    ok, left, reset_at = await db.reserve_ai(pool_of(ctx), uid, AI_LIMIT, AI_WINDOW)
    if not ok:
        await update.message.reply_text(
            t(lang, "ai_limit", limit=AI_LIMIT, when=fmt_dt(reset_at))
        )
        return ConversationHandler.END

    wait = await update.message.reply_text(t(lang, "ai_wait"))
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        answer = await ai.explain(term, lang, await learn_of(update, ctx))
    except Exception:
        log.exception("Gemini request failed")
        await db.refund_ai(pool_of(ctx), uid)  # невдалий запит не списуємо з ліміту
        await wait.edit_text(t(lang, "ai_error"))
        return ConversationHandler.END

    footer = t(lang, "ai_footer", left=left, limit=AI_LIMIT)
    await wait.edit_text(f"{answer}\n\n{footer}")
    return ConversationHandler.END


# ---------- Інтервал сповіщень (2.5) ----------


def interval_label(lang: str, s) -> str:
    if s["mode"] == "off":
        return t(lang, "i_off")
    if s["mode"] == "twice_daily":
        return t(lang, "i_twice")
    key = f"i_{s['interval_minutes']}"
    return t(lang, key) if key in T[lang] else f"{s['interval_minutes']} min"


async def interval_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await require_lang(update, ctx)
    if not lang:
        return
    s = await db.get_settings(pool_of(ctx), update.effective_user.id)
    rows = [
        [InlineKeyboardButton(t(lang, key), callback_data=f"int:{val}")]
        for val, key in INTERVAL_OPTIONS
    ]
    await update.effective_message.reply_text(
        t(lang, "int_title", current=interval_label(lang, s)),
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    val = q.data.split(":", 1)[1]
    lang = await lang_of(update, ctx) or "en"
    pool, uid = pool_of(ctx), q.from_user.id
    if val == "off":
        await db.set_interval(pool, uid, "off")
    elif val == "twice":
        await db.set_interval(pool, uid, "twice_daily")
    elif val.isdigit() and val in {v for v, _ in INTERVAL_OPTIONS}:
        await db.set_interval(pool, uid, "interval", int(val))
    else:
        return
    s = await db.get_settings(pool, uid)
    await q.edit_message_text(t(lang, "int_set", label=interval_label(lang, s)))


# ---------- Вікторина: слово + 4 варіанти перекладу ----------


async def build_question(ctx, uid: int, lang: str, learn: str, category, seen: list[int]):
    """Питання: випадкове слово + правильний переклад і 3 хибні з тієї ж добірки слів."""
    pool = pool_of(ctx)
    rows = await db.quiz_pool(pool, uid, lang, learn, category)
    if len({r["translation"] for r in rows}) < 4 and category is not None:
        rows = await db.quiz_pool(pool, uid, lang, learn, None)  # у категорії замало слів
    translations = {r["translation"] for r in rows}
    if len(translations) < 4:
        return None
    candidates = [r for r in rows if r["id"] not in seen] or rows
    word = random.choice(candidates)
    wrong = random.sample(sorted(translations - {word["translation"]}), 3)
    options = [word["translation"], *wrong]
    random.shuffle(options)
    return {
        "word_id": word["id"],
        "word": word["original"],
        "options": options,
        "correct": options.index(word["translation"]),
        "answered": False,
    }


def quiz_question_view(lang: str, quiz: dict):
    q = quiz["q"]
    text = t(lang, "quiz_question", n=quiz["n"] + 1, total=QUIZ_LEN, word=q["word"])
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(o[:60], callback_data=f"qz:a:{i}")] for i, o in enumerate(q["options"])]
    )
    return text, kb


async def new_quiz(ctx, uid: int, lang: str, learn: str):
    """Створює сесію вікторини; повертає (текст, клавіатура) або None, якщо слів замало."""
    s = await db.get_settings(pool_of(ctx), uid)
    category = s["category"] if s else None
    question = await build_question(ctx, uid, lang, learn, category, [])
    if question is None:
        return None
    quiz = {"score": 0, "n": 0, "seen": [], "category": category, "learn": learn, "q": question}
    ctx.user_data["quiz"] = quiz
    return quiz_question_view(lang, quiz)


async def quiz_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await require_lang(update, ctx)
    if not lang:
        return
    learn = await learn_of(update, ctx)
    view = await new_quiz(ctx, update.effective_user.id, lang, learn)
    if view is None:
        await update.effective_message.reply_text(t(lang, "quiz_need_words"))
        return
    await update.effective_message.reply_text(view[0], reply_markup=view[1])


async def on_quiz(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = q.data.split(":")  # qz:a:<i> | qz:next | qz:stop | qz:again
    action = parts[1] if len(parts) > 1 else ""
    lang = await lang_of(update, ctx) or "en"

    if action == "again":
        await q.answer()
        learn = await learn_of(update, ctx)
        view = await new_quiz(ctx, q.from_user.id, lang, learn)
        if view is None:
            await q.edit_message_text(t(lang, "quiz_need_words"))
        else:
            await q.edit_message_text(view[0], reply_markup=view[1])
        return

    quiz = ctx.user_data.get("quiz")
    if not quiz:  # бот перезапускався або вікторину вже завершено
        await q.answer(t(lang, "quiz_expired"), show_alert=True)
        return
    cur = quiz["q"]

    if action == "a":
        try:
            idx = int(parts[2])
        except (IndexError, ValueError):
            await q.answer()
            return
        if cur["answered"] or not 0 <= idx < len(cur["options"]):
            await q.answer()
            return
        cur["answered"] = True
        quiz["n"] += 1
        quiz["seen"].append(cur["word_id"])
        if idx == cur["correct"]:
            quiz["score"] += 1
            verdict = t(lang, "quiz_correct")
        else:
            verdict = t(lang, "quiz_wrong", correct=cur["options"][cur["correct"]])
        text = (
            f"{cur['word']}\n\n{verdict}\n"
            f"{t(lang, 'quiz_score_line', score=quiz['score'], n=quiz['n'])}"
        )
        if quiz["n"] >= QUIZ_LEN:
            rows = [[InlineKeyboardButton(t(lang, "quiz_finish_btn"), callback_data="qz:stop")]]
        else:
            rows = [
                [InlineKeyboardButton(t(lang, "quiz_next"), callback_data="qz:next")],
                [InlineKeyboardButton(t(lang, "quiz_stop"), callback_data="qz:stop")],
            ]
        await q.answer()
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))

    elif action == "next":
        await q.answer()
        if not cur["answered"]:
            return
        nxt = await build_question(
            ctx, q.from_user.id, lang, quiz["learn"], quiz["category"], quiz["seen"]
        )
        if nxt is None:
            ctx.user_data.pop("quiz", None)
            await q.edit_message_text(t(lang, "quiz_need_words"))
            return
        quiz["q"] = nxt
        text, kb = quiz_question_view(lang, quiz)
        await q.edit_message_text(text, reply_markup=kb)

    elif action == "stop":
        await q.answer()
        ctx.user_data.pop("quiz", None)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t(lang, "quiz_again"), callback_data="qz:again")]]
        )
        await q.edit_message_text(
            t(lang, "quiz_result", score=quiz["score"], n=quiz["n"]), reply_markup=kb
        )
    else:
        await q.answer()


# ---------- Планувальник сповіщень (APScheduler + БД) ----------


def is_due(r, now: datetime) -> bool:
    last = r["last_sent_at"]
    if r["mode"] == "interval":
        return last is None or now + timedelta(seconds=30) >= last + timedelta(
            minutes=r["interval_minutes"]
        )
    if r["mode"] == "twice_daily":
        local = now.astimezone(TZ)
        if local.hour not in (MORNING_HOUR, EVENING_HOUR):
            return False
        if last is None:
            return True
        l = last.astimezone(TZ)
        return (l.date(), l.hour) != (local.date(), local.hour)
    return False


async def send_notification(app: Application, r) -> None:
    pool, uid, lang = app.bot_data["db"], r["user_id"], r["lang"]
    word = await db.random_word(pool, uid, lang, r["learn_lang"], r["category"])
    try:
        if word:
            text = (
                f"🔔 <b>{html.escape(word['original'])}</b>\n"
                f"<tg-spoiler>{html.escape(word['translation'])}</tg-spoiler>\n\n"
                f"<i>{html.escape(t(lang, 'notif_footer'))}</i>"
            )
            await app.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
    except Forbidden:
        log.info("User %s blocked the bot — notifications disabled", uid)
        await db.disable_notifications(pool, uid)
        return
    except TelegramError:
        log.exception("Failed to notify %s", uid)
    await db.touch_sent(pool, uid)


def in_quiet_hours(now: datetime) -> bool:
    """True, якщо зараз нічний режим (за замовчуванням 22:00–09:00 за TZ_NAME)."""
    h = now.astimezone(TZ).hour
    if QUIET_START > QUIET_END:  # проміжок через північ
        return h >= QUIET_START or h < QUIET_END
    return QUIET_START <= h < QUIET_END


async def tick(app: Application) -> None:
    now = datetime.now(timezone.utc)
    if in_quiet_hours(now):
        return  # вночі не турбуємо; о 09:00 відправляться ті, чий час уже настав
    try:
        rows = await db.users_for_notifications(app.bot_data["db"])
        for r in rows:
            if is_due(r, now):
                await send_notification(app, r)
                await asyncio.sleep(0.05)  # ~20 повідомлень/с — у межах лімітів Telegram
    except Exception:
        log.exception("Scheduler tick failed")


# ---------- Решта ----------


async def hint(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await lang_of(update, ctx)
    if lang:
        await update.message.reply_text(t(lang, "hint"), reply_markup=main_kb(lang))
    else:
        await require_lang(update, ctx)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=ctx.error)


async def post_init(app: Application) -> None:
    pool = await db.create_pool(DATABASE_URL)
    await db.init(pool)
    app.bot_data["db"] = pool
    log.info("Gemini model: %s | quiet hours: %02d:00-%02d:00 (%s)", ai.MODEL, QUIET_START, QUIET_END, TZ)

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        tick, "interval", seconds=60, args=[app], max_instances=1, coalesce=True,
        next_run_time=datetime.now(TZ) + timedelta(seconds=5),
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler

    await app.bot.set_my_commands(
        [
            BotCommand("start", "Start / menu"),
            BotCommand("add", "Add a word"),
            BotCommand("words", "Categories and word list"),
            BotCommand("quiz", "Quiz: pick the right translation"),
            BotCommand("explain", "AI explanation of a word"),
            BotCommand("settings_interval", "Notification interval"),
            BotCommand("language", "Translation language"),
            BotCommand("learn", "Language to learn (English / Italian)"),
            BotCommand("cancel", "Cancel current action"),
        ]
    )


async def post_shutdown(app: Application) -> None:
    app.bot_data["scheduler"].shutdown(wait=False)
    await app.bot_data["db"].close()


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)  # повільний AI-запит не блокує інших користувачів
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            MessageHandler(btn_filter("btn_add"), add_start),
        ],
        states={
            W_WORD: [MessageHandler(TEXT_INPUT, add_got_word)],
            W_TR: [MessageHandler(TEXT_INPUT, add_got_translation)],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(MENU_FILTER, cancel)],
        allow_reentry=True,
    )
    ai_conv = ConversationHandler(
        entry_points=[
            CommandHandler("explain", ai_start),
            MessageHandler(btn_filter("btn_ai"), ai_start),
        ],
        states={AI_ASK: [MessageHandler(TEXT_INPUT, ai_got_term)]},
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(MENU_FILTER, cancel)],
        allow_reentry=True,
    )

    app.add_handler(add_conv)
    app.add_handler(ai_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("words", words_menu))
    app.add_handler(MessageHandler(btn_filter("btn_words"), words_menu))
    app.add_handler(CommandHandler("settings_interval", interval_menu))
    app.add_handler(MessageHandler(btn_filter("btn_interval"), interval_menu))
    app.add_handler(CommandHandler("language", language_menu))
    app.add_handler(MessageHandler(btn_filter("btn_lang"), language_menu))
    app.add_handler(CommandHandler("quiz", quiz_start))
    app.add_handler(MessageHandler(btn_filter("btn_quiz"), quiz_start))
    app.add_handler(CallbackQueryHandler(on_quiz, pattern=r"^qz:"))
    app.add_handler(CommandHandler("learn", learn_menu))
    app.add_handler(MessageHandler(btn_filter("btn_learn"), learn_menu))
    app.add_handler(CallbackQueryHandler(on_lang, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(on_learn, pattern=r"^learn:"))
    app.add_handler(CallbackQueryHandler(on_category, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(on_interval, pattern=r"^int:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, hint))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
