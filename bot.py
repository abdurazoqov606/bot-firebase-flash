import os, io, json, time, sqlite3, threading, logging, html, hashlib
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

# =====================================================================
#                           SOZLAMALAR
# =====================================================================
TOKEN = os.getenv("BOT_TOKEN", "9185306152:AAFGpN-iaE1ymP-X2w_8uZ6JWIn5zY1uvcE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8426582765"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://abdrzqv-flash.vercel.app")
FOUNDER = "@uzs_kr"
DB_PATH = os.getenv("DB_PATH", "bot.db")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL",
                            "https://bot-db-6b124-default-rtdb.firebaseio.com").rstrip("/")

# Ixtiyoriy (xavfsizroq rejim): service account .json faylining BUTUN ICHINI
# uchta ''' orasiga qo'ying. Bo'sh qoldirsangiz bot REST rejimida ishlaydi
# (buning uchun Realtime Database rules ochiq bo'lishi kerak).
FIREBASE_KEY_JSON = os.getenv("FIREBASE_CREDENTIALS") or r'''

'''

TZ = timezone(timedelta(hours=5))  # Toshkent vaqti

logging.basicConfig(level=logging.INFO)
bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True, num_threads=8)
app = Flask(__name__)

# =====================================================================
#                              BAZA (SQLite)
# =====================================================================
_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row


def db(sql, args=(), one=False, all=False):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        if one:
            return cur.fetchone()
        if all:
            return cur.fetchall()
        return cur.lastrowid


db("""CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, first_name TEXT, username TEXT,
    joined TEXT, last_active TEXT, banned INTEGER DEFAULT 0,
    blocked INTEGER DEFAULT 0, downloads INTEGER DEFAULT 0)""")
db("""CREATE TABLE IF NOT EXISTS channels(
    chat_id TEXT PRIMARY KEY, title TEXT, link TEXT)""")
db("""CREATE TABLE IF NOT EXISTS dl_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, ts TEXT)""")
db("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")


def now():
    return datetime.now(TZ)


def ts(dt=None):
    return (dt or now()).strftime("%Y-%m-%d %H:%M:%S")


def fmt(dt=None):
    return (dt or now()).strftime("%d.%m.%Y %H:%M:%S")


def get_setting(k, default=""):
    r = db("SELECT value FROM settings WHERE key=?", (k,), one=True)
    return r["value"] if r else default


def set_setting(k, v):
    db("INSERT INTO settings(key,value) VALUES(?,?) "
       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))


def esc(s):
    return html.escape(str(s or ""))


def is_admin(uid):
    return uid == ADMIN_ID


def add_download(uid):
    db("INSERT INTO dl_log(user_id, ts) VALUES(?,?)", (uid, ts()))
    db("UPDATE users SET downloads=downloads+1 WHERE id=?", (uid,))


# =====================================================================
#                              MATNLAR
# =====================================================================
DEFAULT_WELCOME = (
    "👋 Assalomu alaykum, <b>{name}</b>!\n\n"
    "✨ <b>Flash</b> botiga xush kelibsiz!\n\n"
    "🚀 Quyidagi <b>«Ilovani ochish»</b> tugmasi orqali mini ilovamizga kiring "
    "va barcha imkoniyatlardan bepul foydalaning.\n\n"
    "🆘 Savollar bo'lsa — <b>Yordam</b> bo'limiga o'ting.\n"
    f"👨‍💻 Asoschi: {FOUNDER}"
)

HELP_TEXT = (
    "🆘 <b>Yordam</b>\n\n"
    "1️⃣ <b>🚀 Ilovani ochish</b> tugmasini bosing.\n"
    "2️⃣ Mini ilova ichida kerakli bo'limni tanlang.\n"
    "3️⃣ Natijani shu yerning o'zida oling.\n\n"
    "📌 <b>Buyruqlar:</b>\n"
    "/start — botni qayta ishga tushirish\n"
    "/help — yordam\n"
    "/me — mening statistikam\n\n"
    "❓ Muammo yoki taklif bo'lsa, asoschiga yozing:\n"
    f"👨‍💻 {FOUNDER}"
)


# =====================================================================
#                            KLAVIATURALAR
# =====================================================================
def main_reply_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(types.KeyboardButton("🚀 Ilovani ochish",
                                web_app=types.WebAppInfo(WEBAPP_URL)))
    kb.add("🆘 Yordam", "👨‍💻 Asoschi")
    kb.add("📊 Mening statistikam")
    return kb


def main_inline_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🚀 Ilovani ochish",
                                      web_app=types.WebAppInfo(WEBAPP_URL)))
    kb.add(types.InlineKeyboardButton("🆘 Yordam", callback_data="help"),
           types.InlineKeyboardButton("👨‍💻 Asoschi",
                                      url=f"https://t.me/{FOUNDER[1:]}"))
    return kb


def B(text, data):
    return types.InlineKeyboardButton(text, callback_data=data)


def admin_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(B("👥 Foydalanuvchilar", "adm_users"), B("📊 Statistika", "adm_stats"))
    kb.add(B("📢 Reklama yuborish", "adm_ads"), B("📺 Kanallar", "adm_channels"))
    kb.add(B("➕ Kanal qo'shish", "adm_addch"), B("➖ Kanal o'chirish", "adm_delch"))
    kb.add(B("🚫 Bloklash", "adm_ban"), B("✅ Blokdan chiqarish", "adm_unban"))
    kb.add(B("🏆 Top foydalanuvchilar", "adm_top"), B("📁 Eksport (TXT)", "adm_export"))
    kb.add(B("✍️ Xush kelibsiz matni", "adm_welcome"), B("🤖 Bot holati", "adm_toggle"))
    kb.add(B("☁️ Firebase holati", "adm_fb"), B("🔄 Yangilash", "adm_home"))
    return kb


def back_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(B("⬅️ Admin panel", "adm_home"))
    return kb


def admin_text():
    users = db("SELECT COUNT(*) c FROM users", one=True)["c"]
    dls = db("SELECT COUNT(*) c FROM dl_log", one=True)["c"]
    chs = db("SELECT COUNT(*) c FROM channels", one=True)["c"]
    state = "🟢 Yoqilgan" if get_setting("bot_on", "1") == "1" else "🔴 O'chirilgan"
    return (
        "👑 <b>ADMIN PANEL</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users}</b>\n"
        f"📥 Jami yuklamalar: <b>{dls}</b>\n"
        f"📺 Majburiy kanallar: <b>{chs}</b>\n"
        f"🤖 Bot holati: <b>{state}</b>\n"
        f"⏰ {fmt()}"
    )


def safe_edit(call, text, kb=None):
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=kb, disable_web_page_preview=True)
    except ApiTelegramException as e:
        if "not modified" not in str(e):
            bot.send_message(call.message.chat.id, text, reply_markup=kb,
                             disable_web_page_preview=True)


# =====================================================================
#                          MAJBURIY OBUNA
# =====================================================================
def not_subscribed(uid):
    missing = []
    for ch in db("SELECT * FROM channels", all=True):
        try:
            st = bot.get_chat_member(ch["chat_id"], uid).status
            if st in ("left", "kicked"):
                missing.append(ch)
        except Exception as e:
            logging.warning("Obuna tekshirishda xato (%s): %s", ch["chat_id"], e)
    return missing


def sub_kb(missing):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for ch in missing:
        kb.add(types.InlineKeyboardButton(f"📺 {ch['title']}", url=ch["link"]))
    kb.add(B("✅ Obunani tekshirish", "check_sub"))
    return kb


def allowed(user, chat_id):
    if is_admin(user.id):
        return True
    row = db("SELECT banned FROM users WHERE id=?", (user.id,), one=True)
    if row and row["banned"]:
        bot.send_message(chat_id, "🚫 Siz botdan foydalanishdan <b>bloklangansiz</b>.\n"
                                  f"Murojaat uchun: {FOUNDER}")
        return False
    if get_setting("bot_on", "1") != "1":
        bot.send_message(chat_id, "🛠 Botda <b>texnik ishlar</b> olib borilmoqda.\n"
                                  "Iltimos, birozdan so'ng urinib ko'ring.")
        return False
    missing = not_subscribed(user.id)
    if missing:
        bot.send_message(chat_id, "📢 <b>Botdan foydalanish uchun quyidagi "
                                  "kanallarga obuna bo'ling:</b>",
                         reply_markup=sub_kb(missing))
        return False
    return True


def upsert_user(u):
    exists = db("SELECT id FROM users WHERE id=?", (u.id,), one=True)
    db("INSERT INTO users(id,first_name,username,joined,last_active) VALUES(?,?,?,?,?) "
       "ON CONFLICT(id) DO UPDATE SET first_name=excluded.first_name, "
       "username=excluded.username, last_active=excluded.last_active, blocked=0",
       (u.id, u.first_name, u.username, ts(), ts()))
    return not exists


def send_welcome(chat_id, name):
    text = (get_setting("welcome") or DEFAULT_WELCOME).replace("{name}", esc(name))
    bot.send_message(chat_id, text, reply_markup=main_reply_kb())
    bot.send_message(chat_id, "👇 Tezkor tugmalar:", reply_markup=main_inline_kb())


# =====================================================================
#                      FOYDALANUVCHI HANDLERLARI
# =====================================================================
@bot.message_handler(commands=["start"], chat_types=["private"])
def cmd_start(m):
    if upsert_user(m.from_user):
        total = db("SELECT COUNT(*) c FROM users", one=True)["c"]
        try:
            u = m.from_user
            bot.send_message(
                ADMIN_ID,
                f"🆕 <b>Yangi foydalanuvchi</b>\n👤 {esc(u.first_name)}\n"
                f"🔗 @{esc(u.username) if u.username else '—'}\n"
                f"🆔 <code>{u.id}</code>\n👥 Jami: <b>{total}</b>")
        except Exception:
            pass
    if allowed(m.from_user, m.chat.id):
        send_welcome(m.chat.id, m.from_user.first_name)
        if is_admin(m.from_user.id):
            bot.send_message(m.chat.id, "👑 Admin panel uchun: /admin")


@bot.message_handler(commands=["help"], chat_types=["private"])
@bot.message_handler(func=lambda m: m.text == "🆘 Yordam", chat_types=["private"])
def cmd_help(m):
    upsert_user(m.from_user)
    if allowed(m.from_user, m.chat.id):
        bot.send_message(m.chat.id, HELP_TEXT)


@bot.message_handler(func=lambda m: m.text == "👨‍💻 Asoschi", chat_types=["private"])
def cmd_founder(m):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✉️ Asoschiga yozish",
                                      url=f"https://t.me/{FOUNDER[1:]}"))
    bot.send_message(m.chat.id, f"👨‍💻 <b>Asoschi:</b> {FOUNDER}\n\n"
                                "Savol, taklif yoki hamkorlik uchun yozing.",
                     reply_markup=kb)


@bot.message_handler(commands=["me"], chat_types=["private"])
@bot.message_handler(func=lambda m: m.text == "📊 Mening statistikam", chat_types=["private"])
def cmd_me(m):
    upsert_user(m.from_user)
    if not allowed(m.from_user, m.chat.id):
        return
    r = db("SELECT * FROM users WHERE id=?", (m.from_user.id,), one=True)
    bot.send_message(m.chat.id,
                     "📊 <b>Sizning statistikangiz</b>\n\n"
                     f"🆔 ID: <code>{r['id']}</code>\n"
                     f"📥 Yuklamalar: <b>{r['downloads']}</b>\n"
                     f"📅 Qo'shilgan sana: <b>{r['joined']}</b>")


@bot.callback_query_handler(func=lambda c: c.data == "help")
def cb_help(c):
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, HELP_TEXT)


@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def cb_check_sub(c):
    missing = not_subscribed(c.from_user.id)
    if missing:
        bot.answer_callback_query(c.id, "❌ Hali barcha kanallarga obuna bo'lmadingiz!",
                                  show_alert=True)
        return
    bot.answer_callback_query(c.id, "✅ Rahmat!")
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass
    send_welcome(c.message.chat.id, c.from_user.first_name)


# Mini ilovadan kelgan ma'lumot (Telegram.WebApp.sendData) => yuklama hisoblanadi
@bot.message_handler(content_types=["web_app_data"])
def on_webapp_data(m):
    add_download(m.from_user.id)
    bot.send_message(m.chat.id, "✅ Yuklama muvaffaqiyatli qabul qilindi!")


# =====================================================================
#                             ADMIN PANEL
# =====================================================================
@bot.message_handler(commands=["admin"], chat_types=["private"])
def cmd_admin(m):
    if is_admin(m.from_user.id):
        bot.send_message(m.chat.id, admin_text(), reply_markup=admin_kb())


def admin_only(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "⛔ Ruxsat yo'q", show_alert=True)
        return False
    return True


pending_ads = {}


@bot.callback_query_handler(func=lambda c: c.data.startswith(("adm_", "delch_", "ads_", "fb_")))
def admin_callbacks(c):
    if not admin_only(c):
        return
    d = c.data
    cid = c.message.chat.id
    bot.answer_callback_query(c.id)

    if d == "adm_home":
        safe_edit(c, admin_text(), admin_kb())

    elif d == "adm_users":
        rows = db("SELECT * FROM users ORDER BY joined DESC LIMIT 20", all=True)
        total = db("SELECT COUNT(*) c FROM users", one=True)["c"]
        txt = f"👥 <b>Foydalanuvchilar</b> (jami: {total})\n<i>Oxirgi 20 tasi:</i>\n\n"
        for r in rows:
            un = f"@{esc(r['username'])}" if r["username"] else "—"
            flag = " 🚫" if r["banned"] else ""
            txt += f"• {esc(r['first_name'])} | {un} | <code>{r['id']}</code>{flag}\n"
        safe_edit(c, txt, back_kb())

    elif d == "adm_stats":
        t0 = now().strftime("%Y-%m-%d 00:00:00")
        w0 = ts(now() - timedelta(days=7))
        m0 = ts(now() - timedelta(days=30))
        q = lambda s, a=(): db(s, a, one=True)["c"]
        txt = (
            "📊 <b>Statistika</b>\n\n"
            f"👥 Jami foydalanuvchilar: <b>{q('SELECT COUNT(*) c FROM users')}</b>\n"
            f"🆕 Bugun qo'shilgan: <b>{q('SELECT COUNT(*) c FROM users WHERE joined>=?', (t0,))}</b>\n"
            f"📅 7 kunda: <b>{q('SELECT COUNT(*) c FROM users WHERE joined>=?', (w0,))}</b>\n"
            f"🗓 30 kunda: <b>{q('SELECT COUNT(*) c FROM users WHERE joined>=?', (m0,))}</b>\n"
            f"🔥 Bugun faol: <b>{q('SELECT COUNT(*) c FROM users WHERE last_active>=?', (t0,))}</b>\n\n"
            f"📥 Jami yuklamalar: <b>{q('SELECT COUNT(*) c FROM dl_log')}</b>\n"
            f"📥 Bugungi yuklamalar: <b>{q('SELECT COUNT(*) c FROM dl_log WHERE ts>=?', (t0,))}</b>\n\n"
            f"🚫 Bloklangan: <b>{q('SELECT COUNT(*) c FROM users WHERE banned=1')}</b>\n"
            f"😢 Botni to'xtatganlar: <b>{q('SELECT COUNT(*) c FROM users WHERE blocked=1')}</b>\n"
            f"📺 Majburiy kanallar: <b>{q('SELECT COUNT(*) c FROM channels')}</b>\n\n"
            f"⏰ {fmt()}"
        )
        safe_edit(c, txt, back_kb())

    elif d == "adm_top":
        rows = db("SELECT * FROM users WHERE downloads>0 ORDER BY downloads DESC LIMIT 10",
                  all=True)
        txt = "🏆 <b>Top foydalanuvchilar</b>\n\n"
        if not rows:
            txt += "Hozircha ma'lumot yo'q."
        for i, r in enumerate(rows, 1):
            txt += f"{i}. {esc(r['first_name'])} — <b>{r['downloads']}</b> ta (<code>{r['id']}</code>)\n"
        safe_edit(c, txt, back_kb())

    elif d == "adm_export":
        rows = db("SELECT * FROM users ORDER BY joined", all=True)
        data = "\n".join(f"{r['id']}\t{r['first_name']}\t@{r['username'] or '-'}\t{r['joined']}"
                         for r in rows)
        f = io.BytesIO(data.encode("utf-8"))
        f.name = "users.txt"
        bot.send_document(cid, f, caption=f"📁 Foydalanuvchilar: {len(rows)} ta")

    elif d == "adm_toggle":
        new = "0" if get_setting("bot_on", "1") == "1" else "1"
        set_setting("bot_on", new)
        safe_edit(c, admin_text(), admin_kb())

    elif d == "adm_channels":
        rows = db("SELECT * FROM channels", all=True)
        txt = "📺 <b>Majburiy kanallar</b>\n\n"
        if not rows:
            txt += "Hozircha kanal yo'q."
        for i, r in enumerate(rows, 1):
            txt += f"{i}. <a href=\"{esc(r['link'])}\">{esc(r['title'])}</a> — <code>{esc(r['chat_id'])}</code>\n"
        safe_edit(c, txt, back_kb())

    elif d == "adm_addch":
        msg = bot.send_message(cid, "➕ Kanal <b>@username</b> yoki <b>ID</b> sini yuboring.\n"
                                    "⚠️ Bot o'sha kanalda <b>admin</b> bo'lishi shart.\n\n"
                                    "Bekor qilish: /cancel")
        bot.register_next_step_handler(msg, step_add_channel)

    elif d == "adm_delch":
        rows = db("SELECT * FROM channels", all=True)
        if not rows:
            safe_edit(c, "📺 O'chirish uchun kanal yo'q.", back_kb())
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for r in rows:
            kb.add(B(f"🗑 {r['title']}", f"delch_{r['chat_id']}"))
        kb.add(B("⬅️ Admin panel", "adm_home"))
        safe_edit(c, "➖ O'chiriladigan kanalni tanlang:", kb)

    elif d.startswith("delch_"):
        db("DELETE FROM channels WHERE chat_id=?", (d[6:],))
        safe_edit(c, "✅ Kanal o'chirildi.", back_kb())

    elif d == "adm_ban":
        msg = bot.send_message(cid, "🚫 Bloklanadigan foydalanuvchi <b>ID</b> sini yuboring:\n/cancel")
        bot.register_next_step_handler(msg, lambda m: step_ban(m, 1))

    elif d == "adm_unban":
        msg = bot.send_message(cid, "✅ Blokdan chiqariladigan foydalanuvchi <b>ID</b> sini yuboring:\n/cancel")
        bot.register_next_step_handler(msg, lambda m: step_ban(m, 0))

    elif d == "adm_welcome":
        msg = bot.send_message(cid, "✍️ Yangi xush kelibsiz matnini yuboring.\n"
                                    "Ismni qo'yish uchun <code>{name}</code> yozing.\n"
                                    "HTML ruxsat etiladi. Standart matn uchun: <code>reset</code>\n/cancel")
        bot.register_next_step_handler(msg, step_welcome)

    elif d == "adm_ads":
        msg = bot.send_message(cid, "📢 Reklama xabarini yuboring (matn, rasm, video, "
                                    "fayl — istalgani).\n/cancel")
        bot.register_next_step_handler(msg, step_ads)

    elif d == "ads_yes":
        info = pending_ads.pop(c.from_user.id, None)
        if not info:
            safe_edit(c, "❌ Xabar topilmadi.", back_kb())
            return
        safe_edit(c, "🚀 Yuborish boshlandi...")
        threading.Thread(target=do_broadcast, args=(info[0], info[1], cid), daemon=True).start()

    elif d == "ads_no":
        pending_ads.pop(c.from_user.id, None)
        safe_edit(c, "❌ Reklama bekor qilindi.", back_kb())

    # ---------- Firebase holati ----------
    elif d == "adm_fb":
        if fs:
            mode = ("Admin SDK (xavfsiz)" if fb_mode == "admin"
                    else "REST (rules ochiq bo'lishi kerak)")
            txt = ("☁️ <b>Firebase holati</b>\n\n"
                   "🟢 Ulangan\n"
                   f"🔌 Rejim: <b>{mode}</b>\n"
                   f"🕒 Oxirgi sinxron: <b>{last_sync or 'hali bo`lmagan'}</b>\n"
                   f"📤 Oxirgi yozilgan yozuvlar: <b>{last_sync_count}</b>\n"
                   f"⚠️ Oxirgi xato: <b>{esc(last_error) or 'yo`q'}</b>")
        else:
            txt = ("☁️ <b>Firebase holati</b>\n\n🔴 Ulanmagan\n"
                   f"⚠️ Sabab: <b>{esc(last_error) or 'noma`lum'}</b>\n"
                   "FIREBASE_DB_URL va rules sozlamalarini tekshiring.")
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(B("🔄 Hozir sinxronlash", "fb_now"), B("⬅️ Admin panel", "adm_home"))
        safe_edit(c, txt, kb)

    elif d == "fb_now":
        if not fs:
            bot.send_message(cid, "🔴 Firebase ulanmagan.", reply_markup=back_kb())
            return
        fb_sync_once()
        bot.send_message(cid, f"✅ Sinxronlandi: {last_sync}", reply_markup=back_kb())


def cancelled(m):
    if m.text and m.text.strip() == "/cancel":
        bot.send_message(m.chat.id, "❌ Bekor qilindi.", reply_markup=back_kb())
        return True
    return False


def step_add_channel(m):
    if not is_admin(m.from_user.id) or cancelled(m):
        return
    raw = (m.text or "").strip()
    if raw.lstrip("-").isdigit():
        raw = int(raw)
    try:
        chat = bot.get_chat(raw)
        me = bot.get_chat_member(chat.id, bot.get_me().id)
        if me.status not in ("administrator", "creator"):
            bot.send_message(m.chat.id, "⚠️ Bot bu kanalda admin emas. Avval admin qiling.",
                             reply_markup=back_kb())
            return
        if chat.username:
            link = f"https://t.me/{chat.username}"
        else:
            link = bot.create_chat_invite_link(chat.id).invite_link
        db("INSERT OR REPLACE INTO channels(chat_id,title,link) VALUES(?,?,?)",
           (str(chat.id), chat.title or str(chat.id), link))
        bot.send_message(m.chat.id, f"✅ Kanal qo'shildi: <b>{esc(chat.title)}</b>",
                         reply_markup=back_kb())
    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Xatolik: <code>{esc(e)}</code>\n"
                                    "Kanal topilmadi yoki bot admin emas.", reply_markup=back_kb())


def step_ban(m, flag):
    if not is_admin(m.from_user.id) or cancelled(m):
        return
    t = (m.text or "").strip()
    if not t.isdigit():
        bot.send_message(m.chat.id, "❌ ID faqat raqam bo'lishi kerak.", reply_markup=back_kb())
        return
    uid = int(t)
    if uid == ADMIN_ID:
        bot.send_message(m.chat.id, "😅 O'zingizni bloklab bo'lmaydi.", reply_markup=back_kb())
        return
    db("INSERT OR IGNORE INTO users(id,first_name,joined,last_active) VALUES(?,?,?,?)",
       (uid, "Noma'lum", ts(), ts()))
    db("UPDATE users SET banned=? WHERE id=?", (flag, uid))
    bot.send_message(m.chat.id, "🚫 Bloklandi." if flag else "✅ Blokdan chiqarildi.",
                     reply_markup=back_kb())


def step_welcome(m):
    if not is_admin(m.from_user.id) or cancelled(m):
        return
    if (m.text or "").strip().lower() == "reset":
        set_setting("welcome", "")
        bot.send_message(m.chat.id, "✅ Standart matn tiklandi.", reply_markup=back_kb())
        return
    set_setting("welcome", m.html_text or m.text or "")
    bot.send_message(m.chat.id, "✅ Yangi xush kelibsiz matni saqlandi.", reply_markup=back_kb())


def step_ads(m):
    if not is_admin(m.from_user.id) or cancelled(m):
        return
    pending_ads[m.from_user.id] = (m.chat.id, m.message_id)
    total = db("SELECT COUNT(*) c FROM users WHERE blocked=0 AND banned=0", one=True)["c"]
    kb = types.InlineKeyboardMarkup()
    kb.add(B("✅ Yuborish", "ads_yes"), B("❌ Bekor", "ads_no"))
    bot.send_message(m.chat.id, f"📢 Reklama <b>{total}</b> ta foydalanuvchiga yuboriladi.\n"
                                "Tasdiqlaysizmi?", reply_markup=kb)


def do_broadcast(from_chat, msg_id, admin_chat):
    ids = [r["id"] for r in db("SELECT id FROM users WHERE blocked=0 AND banned=0", all=True)]
    ok = fail = 0
    status = bot.send_message(admin_chat, f"📤 Yuborilmoqda... 0/{len(ids)}")
    for i, uid in enumerate(ids, 1):
        for attempt in range(2):
            try:
                bot.copy_message(uid, from_chat, msg_id)
                ok += 1
                break
            except ApiTelegramException as e:
                if e.error_code == 429 and attempt == 0:
                    time.sleep(int(e.result_json.get("parameters", {}).get("retry_after", 5)) + 1)
                    continue
                fail += 1
                if e.error_code in (400, 403):
                    db("UPDATE users SET blocked=1 WHERE id=?", (uid,))
                break
            except Exception:
                fail += 1
                break
        time.sleep(0.05)
        if i % 50 == 0:
            try:
                bot.edit_message_text(f"📤 Yuborilmoqda... {i}/{len(ids)}",
                                      admin_chat, status.message_id)
            except Exception:
                pass
    bot.send_message(admin_chat, f"✅ <b>Reklama yakunlandi</b>\n\n"
                                 f"📨 Yetkazildi: <b>{ok}</b>\n❌ Xato: <b>{fail}</b>",
                     reply_markup=back_kb())


# Boshqa har qanday matnda foydalanuvchini ro'yxatga olish
@bot.message_handler(func=lambda m: True, chat_types=["private"], content_types=["text"])
def fallback(m):
    upsert_user(m.from_user)
    if allowed(m.from_user, m.chat.id):
        bot.send_message(m.chat.id, "👇 Menyudan foydalaning yoki /start bosing.",
                         reply_markup=main_reply_kb())


# =====================================================================
#          FIREBASE ZAXIRA (Realtime Database: Admin SDK yoki REST)
# =====================================================================
try:
    import firebase_admin
    from firebase_admin import credentials
    from firebase_admin import db as rtdb   # 'db' nomi SQLite funksiyasiga tegishli
except ImportError:
    firebase_admin = None

fs = None            # Firebase ulangan-ulanmaganligi
fb_mode = None       # "admin" yoki "rest"
_synced = {}
last_sync = ""
last_sync_count = 0
last_error = ""


def init_firebase():
    global fs, fb_mode, last_error
    raw = (FIREBASE_KEY_JSON or "").strip()

    # 1) Admin SDK (xavfsiz rejim)
    if raw and firebase_admin:
        try:
            firebase_admin.initialize_app(credentials.Certificate(json.loads(raw)),
                                          {"databaseURL": FIREBASE_DB_URL})
            rtdb.reference("/").get(shallow=True)
            fs, fb_mode = True, "admin"
            logging.info("Firebase ulandi (Admin SDK).")
            return
        except Exception as e:
            last_error = f"Admin SDK: {e}"
            logging.error("Admin SDK xatosi: %s — REST rejimiga o'tilmoqda", e)

    # 2) REST rejimi (kalit kerak emas, rules ochiq bo'lishi shart)
    try:
        r = requests.get(f"{FIREBASE_DB_URL}/.json?shallow=true", timeout=15)
        if r.status_code == 200:
            fs, fb_mode = True, "rest"
            last_error = ""
            logging.info("Firebase ulandi (REST).")
        else:
            last_error = f"REST {r.status_code}: {r.text[:120]}"
            logging.error("Firebase REST xatosi: %s", last_error)
    except Exception as e:
        last_error = f"REST: {e}"
        logging.error("Firebase REST xatosi: %s", e)


def fb_get(path):
    if fb_mode == "admin":
        return rtdb.reference(path).get()
    r = requests.get(f"{FIREBASE_DB_URL}/{path}.json", timeout=20)
    r.raise_for_status()
    return r.json()


def fb_update(updates):
    if fb_mode == "admin":
        rtdb.reference("/").update(updates)
    else:
        r = requests.patch(f"{FIREBASE_DB_URL}/.json", json=updates, timeout=30)
        r.raise_for_status()


def _items(node):
    """RTDB ba'zan dict o'rniga list qaytaradi; ikkalasini ham qabul qiladi."""
    if not node:
        return {}
    if isinstance(node, list):
        return {str(i): v for i, v in enumerate(node) if v is not None}
    return node


def fb_restore():
    """Lokal baza bo'sh bo'lsa, ma'lumotlarni Firebase'dan tiklaydi."""
    global last_error
    if not fs:
        return False
    if db("SELECT COUNT(*) c FROM users", one=True)["c"] > 0:
        return False
    try:
        n = 0
        for uid, x in _items(fb_get("users")).items():
            db("INSERT OR REPLACE INTO users(id,first_name,username,joined,last_active,"
               "banned,blocked,downloads) VALUES(?,?,?,?,?,?,?,?)",
               (int(uid), x.get("first_name"), x.get("username"), x.get("joined"),
                x.get("last_active"), x.get("banned", 0), x.get("blocked", 0),
                x.get("downloads", 0)))
            n += 1
        for cid, x in _items(fb_get("channels")).items():
            db("INSERT OR REPLACE INTO channels(chat_id,title,link) VALUES(?,?,?)",
               (x.get("chat_id", cid), x.get("title"), x.get("link")))
        for k, x in _items(fb_get("settings")).items():
            set_setting(k, x.get("value", ""))
        for day, x in _items(fb_get("downloads_daily")).items():
            cnt = int(x.get("count", 0))
            with _lock:
                _conn.executemany("INSERT INTO dl_log(user_id, ts) VALUES(?,?)",
                                  [(0, f"{day} 12:00:00")] * cnt)
                _conn.commit()
        logging.info("Firebase'dan tiklandi: %s ta foydalanuvchi", n)
        return n > 0
    except Exception as e:
        last_error = f"Tiklash: {e}"
        logging.error("Tiklashda xato: %s", e)
        return False


def _h(obj):
    return hashlib.md5(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def fb_sync_once(prime=False):
    """Faqat o'zgargan yozuvlarni Firebase'ga yozadi."""
    global last_sync, last_sync_count, last_error
    if not fs:
        return
    items = []  # (collection, doc_id, data)
    for r in db("SELECT * FROM users", all=True):
        items.append(("users", str(r["id"]), dict(r)))
    for r in db("SELECT * FROM channels", all=True):
        items.append(("channels", str(r["chat_id"]), dict(r)))
    for r in db("SELECT key, value FROM settings", all=True):
        items.append(("settings", r["key"], {"value": r["value"]}))
    for r in db("SELECT substr(ts,1,10) d, COUNT(*) c FROM dl_log GROUP BY d", all=True):
        items.append(("downloads_daily", r["d"], {"count": r["c"]}))

    updates, keys = {}, []
    for col, did, data in items:
        key, hh = (col, did), _h(data)
        if prime:
            _synced[key] = hh
        elif _synced.get(key) != hh:
            updates[f"{col}/{did}"] = data
            keys.append((key, hh))
    if prime:
        return

    # Lokal bazadan o'chirilgan kanallarni Firebase'dan ham o'chirish
    current = {(c, d) for c, d, _ in items}
    removed = [k for k in list(_synced) if k[0] == "channels" and k not in current]
    for key in removed:
        updates[f"channels/{key[1]}"] = None

    last_sync = fmt()
    if not updates:
        last_sync_count = 0
        return
    try:
        fb_update(updates)
        for key, hh in keys:
            _synced[key] = hh
        for key in removed:
            _synced.pop(key, None)
        last_sync_count = len(updates)
        last_error = ""
    except Exception as e:
        last_error = f"Yozish: {e}"
        logging.error("Firebase yozish xatosi: %s", e)


def firebase_loop():
    while True:
        time.sleep(60)
        try:
            fb_sync_once()
        except Exception as e:
            logging.error("Firebase sinxron xatosi: %s", e)


# =====================================================================
#                        FLASK (Render uchun)
# =====================================================================
@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/")
def index():
    return "🤖 Bot ishlayapti! " + fmt()


@app.route("/health")
def health():
    return jsonify(status="ok", time=fmt(), firebase=fb_mode)


# Sayt yuklama bo'lganda shu manzilga so'rov yuborishi mumkin:
# fetch(BOT_URL + "/api/download", {method:"POST", headers:{"Content-Type":"application/json"},
#       body: JSON.stringify({user_id: Telegram.WebApp.initDataUnsafe.user.id})})
@app.route("/api/download", methods=["POST", "GET", "OPTIONS"])
def api_download():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True) or {}
    uid = data.get("user_id") or request.args.get("user_id")
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="user_id kerak"), 400
    if not db("SELECT id FROM users WHERE id=?", (uid,), one=True):
        return jsonify(ok=False, error="foydalanuvchi topilmadi"), 404
    add_download(uid)
    return jsonify(ok=True)


def keep_alive():
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        time.sleep(240)
        try:
            requests.get(url + "/health", timeout=15)
        except Exception:
            pass


def run_bot():
    try:
        bot.remove_webhook()
        bot.set_my_commands([
            types.BotCommand("start", "Botni ishga tushirish"),
            types.BotCommand("help", "Yordam"),
            types.BotCommand("me", "Mening statistikam"),
        ])
        bot.set_chat_menu_button(menu_button=types.MenuButtonWebApp(
            "🚀 Ilova", types.WebAppInfo(WEBAPP_URL)))
    except Exception as e:
        logging.warning("Boshlang'ich sozlash xatosi: %s", e)
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:
            logging.error("Polling xatosi: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    init_firebase()
    if fb_restore():
        fb_sync_once(prime=True)   # tiklangan ma'lumotni qayta yozmaslik uchun
    threading.Thread(target=firebase_loop, daemon=True).start()
    threading.Thread(target=run_bot, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), threaded=True)
