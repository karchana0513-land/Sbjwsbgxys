# -*- coding: utf-8 -*-
"""
ADITYA ACCOUNT SELLER BOT  -  single file project (main.py)

Files in project:  main.py | requirements.txt
"""
import os
import io
import sys
import re
import math
import html
import time
import shutil
import sqlite3
import asyncio
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest, RetryAfter
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, ContextTypes, filters,
)

def _import_telethon():
    """Import Telethon and bind the names at module level. Returns error or None."""
    global TelegramClient, errors, StringSession, UpdateAccountTtlRequest, AccountDaysTTL
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession
    from telethon.tl.types import AccountDaysTTL
    try:
        # old Telethon versions
        from telethon.tl.functions.account import UpdateAccountTtlRequest
    except ImportError:
        # Telethon >= ~1.36 renamed it to SetAccountTTLRequest
        from telethon.tl.functions.account import SetAccountTTLRequest as UpdateAccountTtlRequest
    return None

def _load_telethon():
    log = logging.getLogger("sellbot")  # logging config happens later in the file
    # 1) Try importing as-is
    err = None
    try:
        err = _import_telethon()
        if err is None:
            log.info("Telethon loaded. Live OTP enabled.")
            return True
    except Exception as e:
        err = e
        log.warning("Telethon import failed: %s", err)

    # 2) Auto-install into the CURRENT interpreter (fixes the common case where
    #    'pip install telethon' went to a different Python than the one running the bot)
    try:
        log.warning("Telethon not usable (%s). Auto-installing into %s ...", err, sys.executable)
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "--upgrade", "telethon"],
            check=False, timeout=600,
        )
        err = _import_telethon()
        if err is None:
            log.info("Telethon installed and loaded. Live OTP enabled.")
            return True
    except Exception as e2:
        err = e2
        log.error("Telethon auto-install failed: %s", err)

    # 3) Give up gracefully with an EXACT command the user can copy-paste
    log.error("Live OTP disabled: Telethon is not available (%s).", err)
    log.error("Run this exact command (uses the SAME Python as the bot), then restart the bot:")
    log.error("    \"%s\" -m pip install telethon", sys.executable)
    return False

TELETHON_OK = _load_telethon()
if not TELETHON_OK:
    # Stubs so no code path ever hits an undefined name
    TelegramClient = StringSession = UpdateAccountTtlRequest = AccountDaysTTL = None

# =============================================================================
# MASKING FUNCTIONS (Privacy Utilities)
# =============================================================================
def mask_otp(otp):
    otp = str(otp)
    if len(otp) <= 2:
        return "**"
    return otp[0] + "*" * (len(otp) - 2) + otp[-1]

def mask_phone(phone):
    phone = str(phone)
    if len(phone) <= 6:
        return "*" * len(phone)
    return phone[:5] + "*" * (len(phone) - 8) + phone[-3:]

# =============================================================================
# CONFIG
# =============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "9111698860:AAHqTSTSlCbzBKqZVMuBXB9TQSEDQuqAl0M").strip()

# Hardcoded to your provided values to avoid env issues
API_ID = 34279519
API_HASH = "2ad1efafe588a3331032832562e9bb81"

OWNER_ID = 8725461956                      # Master Owner
ADMIN_IDS = [8725461956]                   # always-admin IDs (hardcoded, no DB needed)
PAYMENT_GROUP_ID = -1004427120410          # deposit approval group
DEV_USERNAME = "@Aditya_dark0"

UPI_ID = "deadlyashfaq@fam"                # UPI ID for deposits

DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "bot_database.db"))
BACKUP_DIR = os.path.join(BASE_DIR, "backups")

DEFAULT_FORCE_JOIN = ["@AdityaXcyber", "@zarionxmain", "@hidustanTeam"]

MIN_DEPOSIT = 10                    # minimum add-fund amount (Rs)
MAX_PENDING_DEPOSITS = 3            # per user
BACKUP_DOWNLOAD_ONLY_OWNER = False  # True = only Master Owner may download backups
RECHECK_INTERVAL = 300              # seconds, referral membership watchdog
MAX_UPLOAD_BYTES = 20 * 1024 * 1024 # Telegram Bot API download limit

REQUIRED_TABLES = {"users", "admins", "channels", "accounts", "orders",
                   "deposits", "transactions", "referrals", "settings"}

TERMS_TEXT = (
    "📜 <b>TERMS OF SERVICE</b>\n\n"
    "1️⃣ All sales are final. Once an account is delivered, no refund is given.\n"
    "2️⃣ Login immediately after purchase; we are not responsible for later issues.\n"
    "3️⃣ Do not use accounts for illegal activity or spam.\n"
    "4️⃣ Deposits are credited only after admin approval of a genuine payment screenshot.\n"
    "5️⃣ Fake screenshots / fake referrals lead to a permanent ban.\n"
    "6️⃣ Referral rewards are valid only while the referred user stays in all mandatory channels.\n"
    "7️⃣ Admin decisions are final.\n\n"
    f"👨‍💻 Developer: {DEV_USERNAME}"
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("sellbot")
logging.getLogger("httpx").setLevel(logging.WARNING)

IST = timezone(timedelta(hours=5, minutes=30))
KIND = {"old": "Old Telegram", "new": "New Telegram"}
esc = html.escape
BG_TASKS = set()

# =============================================================================
# SMALL UTILITIES
# =============================================================================
def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def to_ist(ts):
    if not ts:
        return "-"
    try:
        d = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(IST)
        return d.strftime("%d-%b-%Y %I:%M %p")
    except Exception:
        return str(ts)

def stamp():
    return datetime.now(IST).strftime("%Y-%m-%d_%H-%M-%S")

def inr(x):
    s = f"{float(x):.2f}".rstrip("0").rstrip(".")
    return "₹" + s

def human_size(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024

def parse_amount(t):
    try:
        v = float(str(t).strip().replace("₹", "").replace(",", ""))
    except Exception:
        return None
    if not math.isfinite(v):
        return None
    return round(v, 2)

def parse_int(t):
    try:
        return int(str(t).strip())
    except Exception:
        return None

def kb(rows):
    out = []
    for row in rows:
        out.append([
            InlineKeyboardButton(t, url=d) if d.startswith("http") else InlineKeyboardButton(t, callback_data=d)
            for t, d in row
        ])
    return InlineKeyboardMarkup(out)

CANCEL_KB = kb([[("❌ Cancel", "x:cancel")]])

def name_of(u):
    return esc(u.first_name or u.username or str(u.id))

# =============================================================================
# DATABASE LAYER
# =============================================================================
DB_LOCK = threading.RLock()
_conn = None

def connect():
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=DELETE")
    _conn.execute("PRAGMA synchronous=FULL")

def fetchone(sql, args=()):
    with DB_LOCK:
        return _conn.execute(sql, args).fetchone()

def fetchall(sql, args=()):
    with DB_LOCK:
        return _conn.execute(sql, args).fetchall()

def scalar(sql, args=(), default=0):
    r = fetchone(sql, args)
    return r[0] if r and r[0] is not None else default

def run(sql, args=()):
    with DB_LOCK:
        return _conn.execute(sql, args)

@contextmanager
def tx():
    with DB_LOCK:
        _conn.execute("BEGIN IMMEDIATE")
        try:
            yield _conn
            _conn.execute("COMMIT")
        except BaseException:
            try:
                _conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
    wallet REAL NOT NULL DEFAULT 0, banned INTEGER NOT NULL DEFAULT 0,
    referrer_id INTEGER, joined_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admins(
    user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at TEXT);
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE NOT NULL,
    username TEXT, title TEXT, invite_link TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, country TEXT NOT NULL,
    price REAL NOT NULL DEFAULT 0, phone TEXT NOT NULL, session TEXT, password TEXT,
    status TEXT NOT NULL DEFAULT 'available', buyer_id INTEGER, sold_at TEXT,
    added_at TEXT, added_by INTEGER);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL, price REAL NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS deposits(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', file_id TEXT, created_at TEXT,
    decided_by INTEGER, decided_at TEXT);
CREATE TABLE IF NOT EXISTS transactions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL,
    kind TEXT NOT NULL, note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS referrals(
    id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL UNIQUE, join_date TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING', reward_amount REAL NOT NULL DEFAULT 0,
    reward_given INTEGER NOT NULL DEFAULT 0, reversed_amount REAL NOT NULL DEFAULT 0,
    channel_ok INTEGER NOT NULL DEFAULT 0, activated_at TEXT, cancelled_at TEXT, cancel_reason TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_ref_referrer ON referrals(referrer_id);
CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_acc_status ON accounts(status, kind, country, price);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
"""

COLUMN_MIGRATIONS = {
    "users": [("referrer_id", "INTEGER"), ("banned", "INTEGER NOT NULL DEFAULT 0")],
    "accounts": [("session", "TEXT"), ("password", "TEXT"), ("added_by", "INTEGER")],
    "referrals": [("reward_given", "INTEGER NOT NULL DEFAULT 0"),
                  ("reversed_amount", "REAL NOT NULL DEFAULT 0"),
                  ("channel_ok", "INTEGER NOT NULL DEFAULT 0"),
                  ("activated_at", "TEXT"), ("cancelled_at", "TEXT"), ("cancel_reason", "TEXT")],
}

def init_db():
    with DB_LOCK:
        _conn.executescript(SCHEMA)
        for table, cols in COLUMN_MIGRATIONS.items():
            have = {r[1] for r in _conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols:
                if col not in have:
                    _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        _conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('referral_reward','1')")
        _conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('reactivation','0')")

def get_setting(key, default=""):
    r = fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default

def set_setting(key, value):
    run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)))

def get_user(uid):
    return fetchone("SELECT * FROM users WHERE user_id=?", (uid,))

def is_owner(uid):
    return uid == OWNER_ID

def is_admin(uid):
    return (uid == OWNER_ID or uid in ADMIN_IDS
            or fetchone("SELECT 1 FROM admins WHERE user_id=?", (uid,)) is not None)

def ulabel(uid):
    u = get_user(uid)
    if not u:
        return f"<code>{uid}</code>"
    nm = ("@" + u["username"]) if u["username"] else (u["first_name"] or "user")
    return f"{esc(nm)} (<code>{uid}</code>)"

def register_user(u, ref_id=None):
    with tx() as c:
        row = c.execute("SELECT user_id FROM users WHERE user_id=?", (u.id,)).fetchone()
        if row:
            c.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?",
                      (u.username, u.first_name, u.id))
            return False
        valid_ref = None
        if ref_id and ref_id != u.id:
            r = c.execute("SELECT banned FROM users WHERE user_id=?", (ref_id,)).fetchone()
            if r and not r["banned"]:
                valid_ref = ref_id
        c.execute("INSERT INTO users(user_id,username,first_name,wallet,banned,referrer_id,joined_at) "
                  "VALUES(?,?,?,0,0,?,?)", (u.id, u.username, u.first_name, valid_ref, now_utc()))
        if valid_ref:
            c.execute("INSERT OR IGNORE INTO referrals(referrer_id,referred_id,join_date,status) "
                      "VALUES(?,?,?,'PENDING')", (valid_ref, u.id, now_utc()))
        return True

def add_txn(c, uid, amount, kind, note=""):
    c.execute("INSERT INTO transactions(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)",
              (uid, amount, kind, note, now_utc()))

# =============================================================================
# FORCE JOIN / MEMBERSHIP
# =============================================================================
_join_cache = {}

async def is_member(bot, chat_id, uid):
    try:
        m = await bot.get_chat_member(chat_id, uid)
    except TelegramError as e:
        log.warning("get_chat_member(%s,%s) failed: %s", chat_id, uid, e)
        return None
    if m.status in ("member", "administrator", "creator"):
        return True
    if m.status == "restricted":
        return bool(getattr(m, "is_member", False))
    return False

def member_in(cm):
    if cm.status in ("member", "administrator", "creator"):
        return True
    if cm.status == "restricted":
        return bool(getattr(cm, "is_member", False))
    return False

async def missing_channels(bot, uid, use_cache=True):
    if use_cache and time.time() - _join_cache.get(uid, 0) < 20:
        return []
    miss = []
    for ch in fetchall("SELECT * FROM channels ORDER BY id"):
        if await is_member(bot, ch["chat_id"], uid) is False:
            miss.append(ch)
    if miss:
        _join_cache.pop(uid, None)
    else:
        _join_cache[uid] = time.time()
    return miss

def join_markup(chs):
    rows = []
    for ch in chs:
        link = ("https://t.me/" + ch["username"]) if ch["username"] else (ch["invite_link"] or "")
        title = "📢 " + (ch["title"] or ch["username"] or "Channel")
        rows.append([(title, link if link.startswith("http") else "noop")])
    rows.append([("✅ I Have Joined - Verify", "verify")])
    return kb(rows)

async def gate(update, context):
    uid = update.effective_user.id
    miss = await missing_channels(context.bot, uid)
    if miss:
        text = ("🔒 <b>Access Locked</b>\n\nTo use this bot you must join the channel(s) below, "
                "then press <b>Verify</b>.")
        await send(update, context, text, join_markup(miss))
        return False
    await referral_verified(context.bot, uid)
    return True

# =============================================================================
# REFERRAL ENGINE
# =============================================================================
async def referral_verified(bot, uid):
    r = fetchone("SELECT status, channel_ok FROM referrals WHERE referred_id=?", (uid,))
    if not r:
        return
    reactiv = get_setting("reactivation", "0") == "1"
    if r["status"] == "ACTIVE" and r["channel_ok"]:
        return
    if r["status"] == "CANCELLED" and not reactiv and r["channel_ok"]:
        return
    note = None
    with tx() as c:
        row = c.execute("SELECT * FROM referrals WHERE referred_id=?", (uid,)).fetchone()
        if not row:
            return
        c.execute("UPDATE referrals SET channel_ok=1 WHERE id=?", (row["id"],))
        st = row["status"]
        can = (st == "PENDING") or (st == "CANCELLED" and reactiv)
        if can and not row["reward_given"]:
            try:
                amount = max(0.0, float(get_setting("referral_reward", "1")))
            except ValueError:
                amount = 1.0
            c.execute("UPDATE users SET wallet=ROUND(wallet+?,2) WHERE user_id=?", (amount, row["referrer_id"]))
            c.execute("UPDATE referrals SET status='ACTIVE', reward_amount=?, reward_given=1, reversed_amount=0, "
                      "activated_at=?, cancelled_at=NULL, cancel_reason=NULL WHERE id=?",
                      (amount, now_utc(), row["id"]))
            if amount > 0:
                add_txn(c, row["referrer_id"], amount, "referral", f"Referral {uid}")
            n = c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='ACTIVE'",
                          (row["referrer_id"],)).fetchone()[0]
            note = (row["referrer_id"], n, amount)
    if note:
        ref_id, n, amount = note
        if n == 1:
            text = ("🎉 <b>NEW REFERRAL COMPLETED!</b>\n\nCongratulations!\n\n"
                    "You have successfully referred <b>1</b> user.\n\n"
                    f"User ID: <code>{uid}</code>\n\nReferral Reward: <b>{inr(amount)}</b>\n\nStatus: <b>ACTIVE</b>")
        else:
            text = ("🎉 <b>NEW REFERRAL!</b>\n\n"
                    f"You have successfully referred <b>{n}</b> users.\n\n"
                    f"New Referral ID: <code>{uid}</code>\n\nReward Added: <b>{inr(amount)}</b>\n\n"
                    f"Total Active Referrals: <b>{n}</b>")
        try:
            await bot.send_message(ref_id, text, parse_mode=ParseMode.HTML)
        except TelegramError:
            pass

async def cancel_referral(bot, referred_id, reason, force=False):
    with tx() as c:
        r = c.execute("SELECT * FROM referrals WHERE referred_id=?", (referred_id,)).fetchone()
        if not r or r["status"] == "CANCELLED":
            return False
        if r["status"] == "PENDING" and not force:
            return False
        reversed_amt = 0.0
        if r["status"] == "ACTIVE" and r["reward_given"]:
            w = c.execute("SELECT wallet FROM users WHERE user_id=?", (r["referrer_id"],)).fetchone()
            bal = float(w["wallet"]) if w else 0.0
            reversed_amt = round(min(bal, float(r["reward_amount"])), 2)
            if reversed_amt > 0:
                c.execute("UPDATE users SET wallet=ROUND(wallet-?,2) WHERE user_id=?", (reversed_amt, r["referrer_id"]))
                add_txn(c, r["referrer_id"], -reversed_amt, "referral_reverse", f"Referral {referred_id} cancelled")
        c.execute("UPDATE referrals SET status='CANCELLED', reward_given=0, reversed_amount=?, "
                  "cancelled_at=?, cancel_reason=? WHERE id=?", (reversed_amt, now_utc(), reason, r["id"]))
        referrer = r["referrer_id"]
    text = ("❌ <b>REFERRAL CANCELLED!</b>\n\nYour referral has been cancelled.\n\n"
            f"Referral User ID: <code>{referred_id}</code>\n\n"
            f"Reason: {esc(reason)}\n\nReward: <b>₹0 / REVERSED</b>\n\nStatus: <b>CANCELLED</b>\n\n"
            "Please note that referral rewards are only valid while all mandatory channel "
            "membership requirements are fulfilled.")
    try:
        await bot.send_message(referrer, text, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass
    return True

async def handle_leave(bot, uid):
    r = fetchone("SELECT status FROM referrals WHERE referred_id=?", (uid,))
    if not r:
        return
    miss = await missing_channels(bot, uid, use_cache=False)
    if not miss:
        return
    run("UPDATE referrals SET channel_ok=0 WHERE referred_id=?", (uid,))
    if r["status"] == "ACTIVE":
        await cancel_referral(bot, uid, "User has left a mandatory Force Join Channel.")

async def referral_watchdog(app):
    await asyncio.sleep(20)
    while True:
        try:
            ids = [r[0] for r in fetchall("SELECT referred_id FROM referrals WHERE status='ACTIVE'")]
            for uid in ids:
                await handle_leave(app.bot, uid)
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("watchdog error: %s", e)
        await asyncio.sleep(RECHECK_INTERVAL)

async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu:
        return
    if not fetchone("SELECT 1 FROM channels WHERE chat_id=?", (cmu.chat.id,)):
        return
    uid = cmu.new_chat_member.user.id
    was_in, now_in = member_in(cmu.old_chat_member), member_in(cmu.new_chat_member)
    if was_in and not now_in:
        _join_cache.pop(uid, None)
        await handle_leave(context.bot, uid)
    elif now_in and not was_in:
        _join_cache.pop(uid, None)
        if get_user(uid) and not await missing_channels(context.bot, uid, use_cache=False):
            await referral_verified(context.bot, uid)

# =============================================================================
# TELETHON (live OTP & Session Validation)
# =============================================================================
async def fetch_otp(session_str, purchased_at_str):
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        purchase_time = datetime.strptime(purchased_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        purchase_time = purchase_time - timedelta(minutes=1)
    except Exception:
        purchase_time = datetime.min.replace(tzinfo=timezone.utc)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None, "Session is invalid / expired."

        for m in await client.get_messages(777000, limit=5):
            if m.date and m.date >= purchase_time:
                txt = (m.message or "")
                mt = re.search(r"\b(\d{5,6})\b", txt)
                if mt and "code" in txt.lower():
                    when = m.date.astimezone(IST).strftime("%d-%b %I:%M %p")
                    return mt.group(1), when

        return None, "No new login code received since your purchase."
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

async def verify_and_set_ttl(session_str):
    if not TELETHON_OK or not API_ID or not API_HASH:
        return True

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return False

        try:
            await client(UpdateAccountTtlRequest(ttl=AccountDaysTTL(days=180)))
        except Exception as e:
            log.warning("Could not set TTL for session: %s", e)

        return True
    except Exception as e:
        log.error("Session verification error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

async def session_cleaner_watchdog(app):
    await asyncio.sleep(60)
    while True:
        try:
            rows = fetchall("SELECT id, phone, session, added_by FROM accounts WHERE status='available' AND session IS NOT NULL AND session != ''")
            for r in rows:
                is_alive = await verify_and_set_ttl(r["session"])
                if not is_alive:
                    run("DELETE FROM accounts WHERE id=?", (r["id"],))
                    admin_to_notify = r["added_by"] if r["added_by"] else OWNER_ID
                    text = (f"⚠️ <b>Account Expired & Removed</b>\n\n"
                            f"The session for <code>{r['phone']}</code> has expired.\n"
                            f"It has been automatically removed from the shop's inventory.")
                    try:
                        await app.bot.send_message(admin_to_notify, text, parse_mode=ParseMode.HTML)
                    except TelegramError:
                        pass
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Session cleaner watchdog error: %s", e)

        await asyncio.sleep(14400)

# =============================================================================
# SEND HELPERS / KEYBOARDS
# =============================================================================
async def send(update, context, text, markup=None, edit=None):
    q = update.callback_query
    if edit is None:
        edit = q is not None
    if q and edit and q.message and (markup is None or isinstance(markup, InlineKeyboardMarkup)):
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                      disable_web_page_preview=True)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
        except TelegramError:
            pass
    await context.bot.send_message(update.effective_chat.id, text, parse_mode=ParseMode.HTML,
                                   reply_markup=markup, disable_web_page_preview=True)

def main_kb(uid):
    rows = [["👤 Account", "🛒 Browse Account"], ["📦 My Orders", "💰 Wallet"],
            ["🎁 Referral", "☎️ Support"], ["📜 Terms"]]
    if is_admin(uid):
        rows.insert(0, ["👑 Admin Panel"])   # top row so it is visible without scrolling
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

async def ask(update, context, text, name, **data):
    context.user_data["state"] = {"name": name, **data}
    await send(update, context, text + "\n\n<i>/cancel to cancel</i>", CANCEL_KB)

async def send_welcome(update, context):
    u = update.effective_user
    text = (f"👋 <b>Welcome, {name_of(u)}!</b>\n\n"
            "🛒 Buy ready-made Telegram accounts instantly with live OTP.\n"
            "💰 Add funds easily via UPI.\n"
            f"🎁 Invite friends & earn <b>{inr(get_setting('referral_reward', '1'))}</b> per referral.\n\n"
            f"Use the menu below 👇\n\n👨‍💻 Developer: {DEV_USERNAME}")
    await context.bot.send_message(update.effective_chat.id, text, parse_mode=ParseMode.HTML,
                                   reply_markup=main_kb(u.id))

# =============================================================================
# USER FEATURES
# =============================================================================
async def show_account(update, context):
    u = update.effective_user
    row = get_user(u.id)
    text = (f"👋 <b>Welcome back, {name_of(u)}!</b>\n\n"
            f"🆔 User ID: <code>{u.id}</code>\n💰 Wallet: <b>{inr(row['wallet'])}</b>")
    await send(update, context, text, edit=False)

async def show_support(update, context):
    await send(update, context, f"☎️ <b>Support</b>\n\nFor any help contact: {DEV_USERNAME}", edit=False)

async def show_terms(update, context):
    await send(update, context, TERMS_TEXT, edit=False)

async def show_referral(update, context):
    uid = update.effective_user.id
    link = f"https://t.me/{context.bot.username}?start=ref_{uid}"
    cnt = {s: scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status=?", (uid, s))
           for s in ("ACTIVE", "PENDING", "CANCELLED")}
    earned = scalar("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? "
                    "AND kind IN ('referral','referral_reverse')", (uid,))
    text = ("🎁 <b>REFERRAL PROGRAM</b>\n\n"
            f"Earn <b>{inr(get_setting('referral_reward', '1'))}</b> for every friend who joins using your link "
            "and joins all mandatory channels.\n\n"
            f"🔗 Your link:\n<code>{link}</code>\n\n"
            f"✅ Active: <b>{cnt['ACTIVE']}</b>\n⏳ Pending: <b>{cnt['PENDING']}</b>\n"
            f"❌ Cancelled: <b>{cnt['CANCELLED']}</b>\n"
            f"💰 Net earned: <b>{inr(earned)}</b>\n\n"
            "⚠️ If a referred user leaves a mandatory channel, the referral is cancelled and the reward reversed.")
    await send(update, context, text, edit=False)

async def show_wallet(update, context):
    uid = update.effective_user.id
    u = get_user(uid)
    dep = scalar("SELECT COALESCE(SUM(amount),0) FROM deposits WHERE user_id=? AND status='approved'", (uid,))
    spent = scalar("SELECT COALESCE(-SUM(amount),0) FROM transactions WHERE user_id=? AND kind='purchase'", (uid,))
    text = (f"💰 <b>WALLET</b>\n\nBalance: <b>{inr(u['wallet'])}</b>\n"
            f"Total Deposited: {inr(dep)}\nTotal Spent: {inr(spent)}")
    await send(update, context, text, kb([[("➕ Add Fund", "w:add"), ("📜 Transactions", "w:tx")]]))

TX_ICON = {"deposit": "➕", "purchase": "🛒", "referral": "🎁", "referral_reverse": "↩️",
           "admin_add": "➕", "admin_deduct": "➖"}

def transactions_text(uid):
    rows = fetchall("SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 15", (uid,))
    if not rows:
        return "📜 <b>Transactions</b>\n\nNo transactions yet."
    lines = ["📜 <b>Last transactions</b>\n"]
    for r in rows:
        sign = "+" if r["amount"] >= 0 else "-"
        lines.append(f"{TX_ICON.get(r['kind'], '•')} {sign}{inr(abs(r['amount']))} | {r['kind']} | "
                     f"{esc(r['note'] or '')} | {to_ist(r['created_at'])}")
    return "\n".join(lines)

async def show_orders(update, context):
    uid = update.effective_user.id
    rows = fetchall("SELECT o.id, a.phone FROM orders o JOIN accounts a ON a.id=o.account_id "
                    "WHERE o.user_id=? ORDER BY o.id DESC LIMIT 20", (uid,))
    if not rows:
        await send(update, context, "📦 <b>My Orders</b>\n\nYou have no orders yet.", edit=False)
        return
    buttons = [[(f"#{r['id']}  •  {r['phone']}", f"od:{r['id']}")] for r in rows]
    await send(update, context, "📦 <b>My Orders</b>\n\nTap an order to view details / get OTP:", kb(buttons))

def order_row(oid, uid):
    return fetchone("SELECT o.id, o.price, o.created_at, a.phone, a.country, a.kind, a.password, a.session "
                    "FROM orders o JOIN accounts a ON a.id=o.account_id WHERE o.id=? AND o.user_id=?", (oid, uid))

def order_text(o):
    t = (f"📦 <b>Order #{o['id']}</b>\n\n📱 Number: <code>{esc(o['phone'])}</code>\n"
         f"🌍 Country: {esc(o['country'])}\n🔖 Type: {KIND.get(o['kind'], o['kind'])}\n"
         f"💵 Price: {inr(o['price']) if o['price'] > 0 else 'FREE'}\n🕒 {to_ist(o['created_at'])}")
    if o["password"]:
        t += f"\n🔐 2FA Password: <code>{esc(o['password'])}</code>"
    t += "\n\n👇 Login with the number in Telegram, then press <b>Get OTP</b>."
    return t

def order_kb(oid):
    return kb([[("📩 Get OTP", f"otp:{oid}")], [("⬅️ My Orders", "orders")]])

async def show_browse(update, context):
    free = scalar("SELECT COUNT(*) FROM accounts WHERE status='available' AND price=0")
    old = scalar("SELECT COUNT(*) FROM accounts WHERE status='available' AND kind='old' AND price>0")
    new = scalar("SELECT COUNT(*) FROM accounts WHERE status='available' AND kind='new' AND price>0")
    text = "🛒 <b>BROWSE ACCOUNTS</b>\n\nChoose a category:"
    rows = [[(f"🆓 Free Accounts ({free})", "br:free")],
            [(f"📱 Old Telegram ({old})", "br:old")],
            [(f"🆕 New Telegram ({new})", "br:new")]]
    await send(update, context, text, kb(rows))

def group_rows(cat):
    if cat == "free":
        return fetchall("SELECT MIN(id) id, kind, country, price, COUNT(*) n FROM accounts "
                        "WHERE status='available' AND price=0 GROUP BY kind,country,price ORDER BY country")
    return fetchall("SELECT MIN(id) id, kind, country, price, COUNT(*) n FROM accounts "
                    "WHERE status='available' AND kind=? AND price>0 GROUP BY kind,country,price "
                    "ORDER BY country, price", (cat,))

def buy_account(uid, acc_id):
    with tx() as c:
        a = c.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
        if not a:
            return "gone", None

        # IMPORTANT FIX: Prioritize selling accounts that have a valid session string first
        a2 = c.execute("SELECT * FROM accounts WHERE status='available' AND kind=? AND country=? AND price=? "
                       "ORDER BY (session IS NOT NULL AND session != '') DESC, id ASC LIMIT 1",
                       (a["kind"], a["country"], a["price"])).fetchone()
        if not a2:
            return "soldout", None

        u = c.execute("SELECT wallet FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u or float(u["wallet"]) + 1e-9 < float(a2["price"]):
            return "lowbal", a2

        price = float(a2["price"])
        if price > 0:
            c.execute("UPDATE users SET wallet=ROUND(wallet-?,2) WHERE user_id=?", (price, uid))

        cur = c.execute("UPDATE accounts SET status='sold', buyer_id=?, sold_at=? WHERE id=? AND status='available'",
                        (uid, now_utc(), a2["id"]))
        if cur.rowcount != 1:
            return "soldout", None

        oid = c.execute("INSERT INTO orders(user_id,account_id,price,created_at) VALUES(?,?,?,?)",
                        (uid, a2["id"], price, now_utc())).lastrowid

        if price > 0:
            add_txn(c, uid, -price, "purchase", f"Account #{a2['id']}")
        return "ok", oid

# ---- deposits ---------------------------------------------------------------
def decide_deposit(dep_id, ok, by):
    with tx() as c:
        d = c.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
        if not d or d["status"] != "pending":
            return None
        c.execute("UPDATE deposits SET status=?, decided_by=?, decided_at=? WHERE id=?",
                  ("approved" if ok else "rejected", by, now_utc(), dep_id))
        if ok:
            c.execute("UPDATE users SET wallet=ROUND(wallet+?,2) WHERE user_id=?", (d["amount"], d["user_id"]))
            add_txn(c, d["user_id"], d["amount"], "deposit", f"Deposit #{dep_id}")
        return d

async def cb_deposit(update, context, data):
    q = update.callback_query
    parts = data.split(":")
    ok = parts[1] == "ok"
    dep_id = parse_int(parts[2])
    d = decide_deposit(dep_id, ok, q.from_user.id)
    if d is None:
        await q.answer("Already processed.", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return
    await q.answer("Approved ✅" if ok else "Rejected ❌")
    tag = "✅ APPROVED" if ok else "❌ REJECTED"
    try:
        await q.edit_message_caption(
            caption=(q.message.caption_html or "") + f"\n\n{tag} by {esc(q.from_user.full_name)}",
            parse_mode=ParseMode.HTML, reply_markup=None)
    except TelegramError:
        pass
    try:
        if ok:
            bal = get_user(d["user_id"])["wallet"]
            msg = (f"✅ <b>Deposit Approved!</b>\n\n{inr(d['amount'])} added to your wallet.\n"
                   f"New balance: <b>{inr(bal)}</b>")
        else:
            msg = (f"❌ <b>Deposit Rejected</b>\n\nYour deposit request of {inr(d['amount'])} was rejected.\n"
                   f"Contact {DEV_USERNAME} if you think this is a mistake.")
        await context.bot.send_message(d["user_id"], msg, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass

# =============================================================================
# USER CALLBACKS
# =============================================================================
async def cb_verify(update, context):
    q = update.callback_query
    uid = q.from_user.id
    miss = await missing_channels(context.bot, uid, use_cache=False)
    if miss:
        await q.answer("❌ You have not joined all channels yet!", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=join_markup(miss))
        except TelegramError:
            pass
        return
    await q.answer("✅ Verified!")
    await referral_verified(context.bot, uid)
    try:
        await q.message.delete()
    except TelegramError:
        pass
    await send_welcome(update, context)

async def cb_user(update, context, data):
    q = update.callback_query
    uid = q.from_user.id
    p = data.split(":")
    key = p[0]

    if key == "otp":
        o = order_row(parse_int(p[1]) or 0, uid)
        if not o:
            await q.answer("Order not found.", show_alert=True)
            return

        # Detailed error message added here
        if not o["session"]:
            await q.answer("❌ Live OTP not available! This specific account was added without a session string.", show_alert=True)
            return

        if not (TELETHON_OK and API_ID and API_HASH):
            await q.answer("❌ Bot Configuration Error: Telethon, API_ID, or API_HASH is missing.", show_alert=True)
            return

        if time.time() - context.user_data.get("otp_t", 0) < 5:
            await q.answer("Please wait a few seconds…", show_alert=True)
            return

        context.user_data["otp_t"] = time.time()
        await q.answer("⏳ Checking for new OTP…")
        try:
            code, info = await asyncio.wait_for(fetch_otp(o["session"], o["created_at"]), timeout=40)
        except Exception as e:
            log.error("OTP error: %s", e)
            code, info = None, "Could not connect right now. Try again."

        if code:
            msg = (f"📩 <b>OTP for</b> <code>{esc(o['phone'])}</code>\n\n"
                   f"🔑 Code: <code>{code}</code>\n🕒 {esc(info)}")
        else:
            msg = f"⚠️ {esc(info)}\n\nRequest the login code in Telegram, then press <b>Get OTP</b> again."
        await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.HTML)
        return

    if key == "bb":
        status, oid = buy_account(uid, parse_int(p[1]) or 0)
        if status == "ok":
            await q.answer("✅ Purchase successful!")
            await send(update, context, order_text(order_row(oid, uid)), order_kb(oid))
        elif status == "lowbal":
            await q.answer()
            bal = get_user(uid)["wallet"]
            await send(update, context,
                       f"❌ <b>Insufficient balance!</b>\n\nYour wallet: <b>{inr(bal)}</b>\n"
                       "Please recharge now to buy this account.",
                       kb([[("➕ Recharge Now", "w:add")], [("⬅️ Back", "browse")]]))
        else:
            await q.answer("❌ This account is out of stock.", show_alert=True)
            await show_browse(update, context)
        return

    await q.answer()
    if key == "verify":
        return
    if key == "browse":
        await show_browse(update, context)
    elif key == "br":
        cat = p[1]
        rows = group_rows(cat)
        title = {"free": "🆓 Free Accounts", "old": "📱 Old Telegram", "new": "🆕 New Telegram"}[cat]
        if not rows:
            await send(update, context, f"<b>{title}</b>\n\nNo stock available right now.",
                       kb([[("⬅️ Back", "browse")]]))
            return
        btns = []
        for r in rows:
            price = inr(r["price"]) if r["price"] > 0 else "FREE"
            extra = (" • " + KIND[r["kind"]]) if cat == "free" else ""
            btns.append([(f"{r['country']}{extra} • {price} • {r['n']} left", f"bg:{r['id']}")])
        btns.append([("⬅️ Back", "browse")])
        await send(update, context, f"<b>{title}</b>\n\nSelect a country:", kb(btns))
    elif key == "bg":
        a = fetchone("SELECT * FROM accounts WHERE id=?", (parse_int(p[1]) or 0,))
        if not a:
            await show_browse(update, context)
            return
        n = scalar("SELECT COUNT(*) FROM accounts WHERE status='available' AND kind=? AND country=? AND price=?",
                   (a["kind"], a["country"], a["price"]))
        if n == 0:
            await show_browse(update, context)
            return
        wal = get_user(uid)["wallet"]
        text = (f"🛍 <b>{KIND[a['kind']]}</b>\n\n🌍 Country: {esc(a['country'])}\n"
                f"💵 Price: <b>{inr(a['price']) if a['price'] > 0 else 'FREE'}</b>\n"
                f"📦 In stock: {n}\n💰 Your wallet: {inr(wal)}\n\nConfirm purchase?")
        await send(update, context, text, kb([[("✅ Buy Now", f"bb:{a['id']}")], [("⬅️ Back", "browse")]]))
    elif key == "orders":
        await show_orders(update, context)
    elif key == "od":
        o = order_row(parse_int(p[1]) or 0, uid)
        if o:
            await send(update, context, order_text(o), order_kb(o["id"]))
    elif key == "w":
        if p[1] == "add":
            await ask(update, context, f"💳 <b>Add Fund</b>\n\nEnter the amount in ₹ (minimum {inr(MIN_DEPOSIT)}):",
                      "dep_amount")
        elif p[1] == "tx":
            await send(update, context, transactions_text(uid), kb([[("⬅️ Wallet", "w:menu")]]))
        elif p[1] == "menu":
            await show_wallet(update, context)

# =============================================================================
# ADMIN: TEXTS
# =============================================================================
def admin_kb(uid):
    rows = [
        [("📊 Stats", "a:stats"), ("👥 User List", "a:users")],
        [("🏆 Top Deposit", "a:topdep"), ("🏆 Top Referral", "a:topref")],
        [("🚫 Ban User", "a:ban"), ("✅ Unban User", "a:unban")],
        [("📢 Force Join", "a:fj")],
        [("➕ Add Account", "a:addacc"), ("➖ Remove Account", "a:rmacc")],
        [("✏️ Edit Account", "a:editacc"), ("🌍 Accounts by Country", "a:bycountry")],
        [("🎁 Referral Settings", "a:refset"), ("📈 Referral Stats", "a:refstats")],
        [("🔎 User Referral History", "a:refhist")],
        [("💰 Add Balance", "a:baladd"), ("💸 Deduct Balance", "a:balded")],
        [("📣 Broadcast", "a:bc")],
        [("🗄 DATABASE MANAGEMENT", "db:menu")],
    ]
    if is_owner(uid):
        rows.append([("👮 Add Admin", "a:addadm"), ("🗑 Remove Admin", "a:rmadm")])
    return kb(rows)

async def show_admin(update, context):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    context.user_data.pop("state", None)
    role = "Master Owner" if is_owner(uid) else "Admin"
    await send(update, context, f"👑 <b>ADMIN PANEL</b>\nRole: {role}", admin_kb(uid))

def stats_text():
    S = scalar
    return ("📊 <b>BOT STATISTICS</b>\n\n"
            f"👥 Users: <b>{S('SELECT COUNT(*) FROM users')}</b> (banned: {S('SELECT COUNT(*) FROM users WHERE banned=1')})\n"
            f"📦 Accounts available: <b>{S('SELECT COUNT(*) FROM accounts WHERE status=?', ('available',))}</b>\n"
            f"✅ Accounts sold: <b>{S('SELECT COUNT(*) FROM accounts WHERE status=?', ('sold',))}</b>\n"
            f"🧾 Orders: <b>{S('SELECT COUNT(*) FROM orders')}</b> | Revenue: <b>{inr(S('SELECT COALESCE(SUM(price),0) FROM orders'))}</b>\n"
            f"💳 Deposits approved: <b>{inr(S('SELECT COALESCE(SUM(amount),0) FROM deposits WHERE status=?', ('approved',)))}</b>\n"
            f"⏳ Pending deposits: <b>{S('SELECT COUNT(*) FROM deposits WHERE status=?', ('pending',))}</b>\n"
            f"💰 Total user wallets: <b>{inr(S('SELECT COALESCE(SUM(wallet),0) FROM users'))}</b>\n"
            f"🎁 Referrals: <b>{S('SELECT COUNT(*) FROM referrals')}</b>")

def referral_stats_text():
    S = scalar
    today = S("SELECT COUNT(*) FROM referrals WHERE date(join_date,'+330 minutes')=date('now','+330 minutes')")
    return ("📈 <b>REFERRAL STATISTICS</b>\n\n"
            f"👥 Total Referrals: <b>{S('SELECT COUNT(*) FROM referrals')}</b>\n"
            f"✅ Active Referrals: <b>{S('SELECT COUNT(*) FROM referrals WHERE status=?', ('ACTIVE',))}</b>\n"
            f"⏳ Pending Referrals: <b>{S('SELECT COUNT(*) FROM referrals WHERE status=?', ('PENDING',))}</b>\n"
            f"❌ Cancelled Referrals: <b>{S('SELECT COUNT(*) FROM referrals WHERE status=?', ('CANCELLED',))}</b>\n"
            f"📅 Today's Referrals: <b>{today}</b>\n"
            f"💸 Total Referral Rewards Paid: <b>{inr(S('SELECT COALESCE(SUM(amount),0) FROM transactions WHERE kind=?', ('referral',)))}</b>\n"
            f"↩️ Total Rewards Reversed: <b>{inr(S('SELECT COALESCE(-SUM(amount),0) FROM transactions WHERE kind=?', ('referral_reverse',)))}</b>")

def referral_history_text(uid):
    u = get_user(uid)
    if not u:
        return "❌ User not found."
    lines = [f"🔎 <b>Referral History</b>\nUser: {ulabel(uid)}\n"]
    if u["referrer_id"]:
        r = fetchone("SELECT status FROM referrals WHERE referred_id=?", (uid,))
        lines.append(f"Referred by: {ulabel(u['referrer_id'])} - {r['status'] if r else '-'}\n")
    rows = fetchall("SELECT * FROM referrals WHERE referrer_id=? ORDER BY id DESC LIMIT 40", (uid,))
    total = scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,))
    act = scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='ACTIVE'", (uid,))
    can = scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='CANCELLED'", (uid,))
    lines.append(f"Total: <b>{total}</b> | Active: <b>{act}</b> | Cancelled: <b>{can}</b>\n")
    for r in rows:
        extra = f" ({esc(r['cancel_reason'])})" if r["status"] == "CANCELLED" and r["cancel_reason"] else ""
        lines.append(f"• <code>{r['referred_id']}</code> | {r['status']} | reward {inr(r['reward_amount'])} | "
                     f"joined {to_ist(r['join_date'])}{extra}")
    if not rows:
        lines.append("No referrals made by this user.")
    return "\n".join(lines)[:4000]

def top_text(kind):
    if kind == "dep":
        rows = fetchall("SELECT user_id, SUM(amount) v FROM deposits WHERE status='approved' "
                        "GROUP BY user_id ORDER BY v DESC LIMIT 10")
        title, fmt = "🏆 <b>TOP DEPOSITORS</b>\n", lambda v: inr(v)
    else:
        rows = fetchall("SELECT referrer_id user_id, COUNT(*) v FROM referrals WHERE status='ACTIVE' "
                        "GROUP BY referrer_id ORDER BY v DESC LIMIT 10")
        title, fmt = "🏆 <b>TOP REFERRERS</b> (active)\n", lambda v: str(v)
    if not rows:
        return title + "\nNo data yet."
    return title + "\n" + "\n".join(f"{i}. {ulabel(r['user_id'])} - <b>{fmt(r['v'])}</b>"
                                    for i, r in enumerate(rows, 1))

def bycountry_text():
    rows = fetchall("SELECT country, kind, price, COUNT(*) n FROM accounts WHERE status='available' "
                    "GROUP BY country, kind, price ORDER BY country, kind, price")
    if not rows:
        return "🌍 <b>Accounts by Country</b>\n\nNo accounts in stock."
    lines = ["🌍 <b>Accounts by Country</b>\n"]
    for r in rows:
        price = inr(r["price"]) if r["price"] > 0 else "FREE"
        lines.append(f"• {esc(r['country'])} | {KIND[r['kind']]} | {price} | <b>{r['n']}</b>")
    return "\n".join(lines)[:4000]

def force_join_view():
    chs = fetchall("SELECT * FROM channels ORDER BY id")
    lines = ["📢 <b>FORCE JOIN CHANNELS</b>\n"]
    btns = []
    for ch in chs:
        ref = ("@" + ch["username"]) if ch["username"] else str(ch["chat_id"])
        lines.append(f"• {esc(ch['title'] or '')} - <code>{esc(ref)}</code>")
        btns.append([(f"🗑 Remove {ch['title'] or ref}", f"fj:rm:{ch['id']}")])
    if not chs:
        lines.append("No channels set.")
    lines.append("\n<i>The bot must be an admin in every channel.</i>")
    btns.append([("➕ Add Channel", "fj:add")])
    btns.append([("⬅️ Admin Panel", "a:menu")])
    return "\n".join(lines), kb(btns)

def refset_view():
    reward = get_setting("referral_reward", "1")
    re_on = get_setting("reactivation", "0") == "1"
    text = ("🎁 <b>REFERRAL SETTINGS</b>\n\n"
            f"Reward per referral: <b>{inr(reward)}</b>\n"
            f"Re-activation of cancelled referrals: <b>{'ON' if re_on else 'OFF'}</b>\n\n"
            "<i>Reward changes apply to future referrals only.</i>")
    return text, kb([[("✏️ Change Reward", "a:refreward")],
                     [(f"🔁 Re-activation: {'ON ✅' if re_on else 'OFF ❌'} (toggle)", "a:reactog")],
                     [("❌ Cancel a Referral", "a:refcancel")],
                     [("⬅️ Admin Panel", "a:menu")]])

# =============================================================================
# DATABASE MANAGEMENT
# =============================================================================
def list_backups():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    files = []
    for n in os.listdir(BACKUP_DIR):
        if n.endswith(".db") and n.startswith(("backup_", "uploaded_", "prerestore_")):
            st = os.stat(os.path.join(BACKUP_DIR, n))
            files.append((n, st.st_size, st.st_mtime))
    files.sort(key=lambda x: x[2], reverse=True)
    return files

def make_backup(prefix="backup"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    base = f"{prefix}_{stamp()}"
    name, i = base + ".db", 1
    while os.path.exists(os.path.join(BACKUP_DIR, name)):
        i += 1
        name = f"{base}_{i}.db"
    path = os.path.join(BACKUP_DIR, name)
    with DB_LOCK:
        dst = sqlite3.connect(path)
        try:
            _conn.backup(dst)
        finally:
            dst.close()
    return name, path

def validate_db_file(path):
    try:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False, "File is empty or missing.", {}
        with open(path, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return False, "This is not a valid SQLite database file.", {}
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ic = c.execute("PRAGMA integrity_check").fetchone()[0]
            if ic != "ok":
                return False, f"Database is corrupted: {ic}", {}
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = REQUIRED_TABLES - tables
            if missing:
                return False, "Not a bot database. Missing tables: " + ", ".join(sorted(missing)), {}
            ucols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
            if not {"user_id", "wallet"} <= ucols:
                return False, "users table has an invalid structure.", {}
            dup = c.execute("SELECT COUNT(*)-COUNT(DISTINCT referred_id) FROM referrals").fetchone()[0]
            if dup:
                return False, "Duplicate referral records found in this database.", {}
            dupu = c.execute("SELECT COUNT(*)-COUNT(DISTINCT user_id) FROM users").fetchone()[0]
            if dupu:
                return False, "Duplicate user records found in this database.", {}
            info = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("users", "referrals", "transactions", "deposits", "orders", "accounts", "channels")}
            info["wallets"] = c.execute("SELECT COALESCE(SUM(wallet),0) FROM users").fetchone()[0]
        finally:
            c.close()
        return True, "OK", info
    except sqlite3.DatabaseError as e:
        return False, f"Corrupted / unreadable database: {e}", {}
    except Exception as e:
        return False, f"Validation error: {e}", {}

def restore_database(src_path):
    ok, msg, _ = validate_db_file(src_path)
    if not ok:
        return False, msg
    with DB_LOCK:
        pre_name, pre_path = make_backup("prerestore")
        tmp = DB_PATH + ".restoring"
        try:
            shutil.copyfile(src_path, tmp)
            _conn.close()
            os.replace(tmp, DB_PATH)
            connect()
            init_db()
            if _conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("integrity check failed after restore")
        except Exception as e:
            log.error("restore failed, rolling back: %s", e)
            try:
                _conn.close()
            except Exception:
                pass
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            shutil.copyfile(pre_path, DB_PATH)
            connect()
            init_db()
            return False, f"Restore failed and was rolled back automatically: {e}"
    _join_cache.clear()
    return True, pre_name

def db_menu_view(uid):
    rows = [[("1️⃣ Download Database Backup", "db:dl")]]
    if is_owner(uid):
        rows.append([("2️⃣ Upload Database Backup", "db:up")])
        rows.append([("3️⃣ Restore Previous Backup", "db:restore")])
    rows.append([("4️⃣ Database Information", "db:info")])
    rows.append([("5️⃣ Backup History", "db:hist")])
    rows.append([("⬅️ Admin Panel", "a:menu")])
    text = ("🗄 <b>DATABASE MANAGEMENT</b>\n\nSQLite backup / restore centre.\n"
            + ("" if is_owner(uid) else "<i>Upload & Restore are only for the Master Owner.</i>"))
    return text, kb(rows)

def db_info_text():
    size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    with DB_LOCK:
        ic = _conn.execute("PRAGMA integrity_check").fetchone()[0]
    S = scalar
    bk = list_backups()
    last = datetime.fromtimestamp(bk[0][2], IST).strftime("%d-%b-%Y %I:%M %p") if bk else "never"
    return ("ℹ️ <b>DATABASE INFORMATION</b>\n\n"
            f"📁 File: <code>{esc(os.path.basename(DB_PATH))}</code>\n💾 Size: {human_size(size)}\n"
            f"🧪 Integrity: <b>{esc(ic)}</b>\n🧩 SQLite: {sqlite3.sqlite_version}\n\n"
            f"👥 Users: {S('SELECT COUNT(*) FROM users')}\n"
            f"💰 Total wallets: {inr(S('SELECT COALESCE(SUM(wallet),0) FROM users'))}\n"
            f"🧾 Transactions: {S('SELECT COUNT(*) FROM transactions')}\n"
            f"💳 Deposits: {S('SELECT COUNT(*) FROM deposits')}\n"
            f"📦 Orders: {S('SELECT COUNT(*) FROM orders')}\n"
            f"🛍 Accounts (all): {S('SELECT COUNT(*) FROM accounts')}\n"
            f"🎁 Referrals: {S('SELECT COUNT(*) FROM referrals')}\n"
            f"👮 Admins: {S('SELECT COUNT(*) FROM admins')}\n"
            f"📢 Force-join channels: {S('SELECT COUNT(*) FROM channels')}\n\n"
            f"🗂 Backups stored: {len(bk)}\n🕒 Last backup: {last}")

def db_history_text():
    bk = list_backups()
    if not bk:
        return "🕘 <b>BACKUP HISTORY</b>\n\nNo backups yet."
    label = {"backup": "Manual", "uploaded": "Uploaded", "prerestore": "Auto (pre-restore)"}
    lines = ["🕘 <b>BACKUP HISTORY</b> (latest 15)\n"]
    for n, size, mt in bk[:15]:
        when = datetime.fromtimestamp(mt, IST).strftime("%d-%b-%Y %I:%M %p")
        lines.append(f"• <code>{esc(n)}</code>\n   {label.get(n.split('_')[0], '')} | {human_size(size)} | {when}")
    return "\n".join(lines)

def restore_confirm_view(name):
    path = os.path.join(BACKUP_DIR, name)
    ok, msg, info = validate_db_file(path)
    if not ok:
        return f"❌ <b>Backup is not valid</b>\n\n{esc(msg)}\n\nRestore is blocked.", kb([[("⬅️ Back", "db:menu")]]), False
    text = ("⚠️ <b>CONFIRM RESTORE</b>\n\n"
            f"File: <code>{esc(name)}</code>\n✅ Validation passed\n\n"
            f"👥 Users: {info['users']}\n💰 Wallets total: {inr(info['wallets'])}\n"
            f"🧾 Transactions: {info['transactions']}\n💳 Deposits: {info['deposits']}\n"
            f"📦 Orders: {info['orders']}\n🎁 Referrals: {info['referrals']}\n"
            f"🛍 Accounts: {info['accounts']}\n📢 Channels: {info['channels']}\n\n"
            "The <b>current database will be backed up automatically</b> first, then replaced by this one. "
            "Continue?")
    return text, kb([[("✅ Yes, Restore Now", "db:ok")], [("❌ Cancel", "db:no")]]), True

# =============================================================================
# ADMIN CALLBACKS
# =============================================================================
async def run_broadcast(bot, admin_chat, from_chat, mid):
    ids = [r[0] for r in fetchall("SELECT user_id FROM users WHERE banned=0")]
    sent = failed = 0
    for uid in ids:
        for attempt in (1, 2):
            try:
                await bot.copy_message(uid, from_chat, mid)
                sent += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramError:
                failed += 1
                break
        else:
            failed += 1
        await asyncio.sleep(0.05)
    try:
        await bot.send_message(admin_chat, f"📣 <b>Broadcast finished</b>\n\n✅ Sent: {sent}\n❌ Failed: {failed}",
                               parse_mode=ParseMode.HTML)
    except TelegramError:
        pass

async def cb_admin(update, context, data):
    q = update.callback_query
    uid = q.from_user.id
    chat = update.effective_chat.id
    parts = data.split(":", 2)
    ns, act = parts[0], parts[1]
    arg = parts[2] if len(parts) > 2 else ""
    owner_only = (data in ("a:addadm", "a:rmadm", "db:up", "db:restore", "db:ok")
                  or data.startswith("db:rs:"))
    if owner_only and not is_owner(uid):
        await q.answer("⛔ Only the Master Owner can do this.", show_alert=True)
        return
    await q.answer()
    back = kb([[("⬅️ Admin Panel", "a:menu")]])

    if ns == "a":
        if act == "menu":
            await show_admin(update, context)
        elif act == "stats":
            await send(update, context, stats_text(), back)
        elif act == "users":
            rows = fetchall("SELECT * FROM users ORDER BY joined_at")
            lines = [f"{r['user_id']} | @{r['username'] or '-'} | {r['first_name'] or '-'} | "
                     f"wallet {inr(r['wallet'])} | {'BANNED' if r['banned'] else 'ok'}" for r in rows]
            bio = io.BytesIO(("TOTAL USERS: %d\n\n" % len(rows) + "\n".join(lines)).encode("utf-8"))
            await context.bot.send_document(chat, bio, filename="users.txt", caption=f"👥 Total users: {len(rows)}")
        elif act == "topdep":
            await send(update, context, top_text("dep"), back)
        elif act == "topref":
            await send(update, context, top_text("ref"), back)
        elif act == "ban":
            await ask(update, context, "🚫 Send the <b>User ID</b> to ban:", "ban", ban=1)
        elif act == "unban":
            await ask(update, context, "✅ Send the <b>User ID</b> to unban:", "ban", ban=0)
        elif act == "fj":
            t, m = force_join_view()
            await send(update, context, t, m)
        elif act == "addacc":
            await send(update, context, "➕ <b>Add Account</b>\n\nSelect account type:",
                       kb([[("📱 Old Telegram", "acc:old"), ("🆕 New Telegram", "acc:new")],
                           [("❌ Cancel", "x:cancel")]]))
        elif act == "rmacc":
            rows = fetchall("SELECT * FROM accounts WHERE status='available' ORDER BY id DESC LIMIT 40")
            listing = "\n".join(f"#{r['id']} | {KIND[r['kind']]} | {esc(r['country'])} | "
                                f"{inr(r['price'])} | {esc(r['phone'])}" for r in rows) or "No available accounts."
            await ask(update, context, f"➖ <b>Remove Account</b>\n\n{listing}\n\nSend the <b>Account ID</b> to remove:",
                      "rm_acc")
        elif act == "editacc":
            await ask(update, context, "✏️ <b>Edit Account</b>\n\nSend the <b>Account ID</b> or <b>Phone Number</b> to edit:", "edit_acc_find")
        elif act == "bycountry":
            await send(update, context, bycountry_text(), back)
        elif act == "refset":
            t, m = refset_view()
            await send(update, context, t, m)
        elif act == "refreward":
            await ask(update, context, "✏️ Send the new <b>referral reward</b> in ₹ (e.g. 1 or 2.5):", "ref_reward")
        elif act == "reactog":
            set_setting("reactivation", "0" if get_setting("reactivation", "0") == "1" else "1")
            t, m = refset_view()
            await send(update, context, t, m)
        elif act == "refcancel":
            await ask(update, context, "❌ Send the <b>referred user's ID</b> whose referral you want to cancel:",
                      "ref_cancel")
        elif act == "refstats":
            await send(update, context, referral_stats_text(), back)
        elif act == "refhist":
            await ask(update, context, "🔎 Send the <b>User ID</b> to view referral history:", "ref_hist")
        elif act == "baladd":
            await ask(update, context, "💰 Send: <code>USER_ID AMOUNT</code>\nExample: <code>123456789 50</code>",
                      "bal", mode="add")
        elif act == "balded":
            await ask(update, context, "💸 Send: <code>USER_ID AMOUNT</code>\nExample: <code>123456789 50</code>",
                      "bal", mode="ded")
        elif act == "bc":
            await ask(update, context, "📣 Send the message (text / photo / video / file) to broadcast to all users:",
                      "broadcast")
        elif act == "addadm":
            await ask(update, context, "👮 Send the <b>User ID</b> to make admin:", "adm", add=1)
        elif act == "rmadm":
            lst = "\n".join(f"• {ulabel(r['user_id'])}" for r in fetchall("SELECT user_id FROM admins")) or "No admins."
            await ask(update, context, f"🗑 <b>Admins</b>\n{lst}\n\nSend the <b>User ID</b> to remove:", "adm", add=0)

    elif ns == "acc":
        await ask(update, context, f"📱 <b>{KIND[act]}</b>\n\nSend the <b>phone number</b> with country code (e.g. +919876543210):",
                  "acc_phone", kind=act)

    elif ns == "fj":
        if act == "add":
            await ask(update, context, "📢 Send channel <b>@username</b> (public) or <b>-100… chat ID</b> (private).\n"
                                       "Bot must already be admin there.", "fj_add")
        elif act == "rm":
            run("DELETE FROM channels WHERE id=?", (parse_int(arg) or 0,))
            _join_cache.clear()
            t, m = force_join_view()
            await send(update, context, t, m)

    elif ns == "bc":
        pend = context.user_data.pop("bc", None)
        if act == "ok" and pend:
            task = asyncio.create_task(run_broadcast(context.bot, chat, pend[0], pend[1]))
            BG_TASKS.add(task)
            task.add_done_callback(BG_TASKS.discard)
            await send(update, context, "📣 Broadcast started. You will get a report when it finishes.", back)
        else:
            await send(update, context, "❌ Broadcast cancelled.", back)

    elif ns == "db":
        await cb_db(update, context, act, arg)

async def cb_db(update, context, act, arg):
    q = update.callback_query
    uid = q.from_user.id
    chat = update.effective_chat.id
    back = kb([[("⬅️ Database Menu", "db:menu")]])
    if act == "menu":
        context.user_data.pop("state", None)
        t, m = db_menu_view(uid)
        await send(update, context, t, m)
    elif act == "dl":
        if BACKUP_DOWNLOAD_ONLY_OWNER and not is_owner(uid):
            await context.bot.send_message(chat, "⛔ Only the Master Owner can download backups.")
            return
        name, path = make_backup("backup")
        with open(path, "rb") as f:
            await context.bot.send_document(
                chat, f, filename=name,
                caption=f"🗄 <b>Database backup</b>\n<code>{esc(name)}</code>\n💾 {human_size(os.path.getsize(path))}\n"
                        "Contains users, wallets, deposits, transactions, orders, referrals, accounts, admins, "
                        "force-join channels & settings.", parse_mode=ParseMode.HTML)
    elif act == "info":
        await send(update, context, db_info_text(), back)
    elif act == "hist":
        await send(update, context, db_history_text(), back)
    elif act == "up":
        await ask(update, context, "📤 <b>Upload Database Backup</b>\n\nSend the <b>.db / .sqlite</b> backup file "
                                   "(max 20 MB) as a document.\nIt will be validated before anything changes.",
                  "db_upload")
    elif act == "restore":
        bk = list_backups()
        if not bk:
            await send(update, context, "No backups available to restore.", back)
            return
        btns = []
        for n, size, mt in bk[:10]:
            when = datetime.fromtimestamp(mt, IST).strftime("%d-%b %I:%M %p")
            btns.append([(f"🕒 {n.split('_')[0]} • {when} • {human_size(size)}", f"db:rs:{n}")])
        btns.append([("⬅️ Database Menu", "db:menu")])
        await send(update, context, "♻️ <b>Restore Previous Backup</b>\n\nSelect a backup:", kb(btns))
    elif act == "rs":
        if arg not in {n for n, _, _ in list_backups()}:
            await send(update, context, "❌ Backup file not found.", back)
            return
        text, markup, ok = restore_confirm_view(arg)
        context.user_data["pending_restore"] = arg if ok else None
        await send(update, context, text, markup)
    elif act == "ok":
        name = context.user_data.pop("pending_restore", None)
        if not name or name not in {n for n, _, _ in list_backups()}:
            await send(update, context, "❌ Nothing pending to restore.", back)
            return
        await send(update, context, "⏳ Restoring database… please wait.", None)
        ok, res = await asyncio.get_running_loop().run_in_executor(
            None, restore_database, os.path.join(BACKUP_DIR, name))
        if ok:
            await context.bot.send_message(
                chat, "✅ <b>Database restored successfully!</b>\n\nThe bot is now running on the restored data "
                      "(wallets, users, referrals, transactions are intact).\n"
                      f"🛟 Previous database was saved as <code>{esc(res)}</code>.",
                parse_mode=ParseMode.HTML, reply_markup=back)
        else:
            await context.bot.send_message(chat, f"❌ <b>Restore failed</b>\n\n{esc(res)}",
                                           parse_mode=ParseMode.HTML, reply_markup=back)
    elif act == "no":
        context.user_data.pop("pending_restore", None)
        t, m = db_menu_view(uid)
        await send(update, context, "❌ Restore cancelled.\n\n" + t, m)

# =============================================================================
# STATE (TEXT / FILE INPUT) HANDLERS
# =============================================================================
def clear_state(context):
    context.user_data.pop("state", None)

async def reply(update, text, markup=None):
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                              disable_web_page_preview=True)

async def st_dep_amount(update, context, st):
    amt = parse_amount(update.effective_message.text or "")
    if amt is None or amt < MIN_DEPOSIT or amt > 100000:
        await reply(update, f"❌ Enter a valid amount between {inr(MIN_DEPOSIT)} and ₹1,00,000.", CANCEL_KB)
        return
    context.user_data["state"] = {"name": "dep_ss", "amount": amt}

    caption = (f"💳 <b>Add Fund - {inr(amt)}</b>\n\n"
               f"Please pay exactly <b>{inr(amt)}</b> to this UPI ID:\n"
               f"👉 <code>{UPI_ID}</code>\n\n"
               "Then send the <b>payment screenshot</b> here 👇\n\n<i>/cancel to cancel</i>")

    await reply(update, caption, CANCEL_KB)

async def st_dep_ss(update, context, st):
    msg = update.effective_message
    user = update.effective_user
    fid, is_photo = None, False
    if msg.photo:
        fid, is_photo = msg.photo[-1].file_id, True
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        fid = msg.document.file_id
    if not fid:
        await reply(update, "❌ Please send the payment <b>screenshot</b> as a photo.", CANCEL_KB)
        return
    if scalar("SELECT COUNT(*) FROM deposits WHERE user_id=? AND status='pending'", (user.id,)) >= MAX_PENDING_DEPOSITS:
        clear_state(context)
        await reply(update, "⏳ You already have pending deposit requests. Please wait for approval.")
        return
    amount = st["amount"]
    dep_id = run("INSERT INTO deposits(user_id,amount,status,file_id,created_at) VALUES(?,?,'pending',?,?)",
                 (user.id, amount, fid, now_utc())).lastrowid
    caption = (f"💳 <b>Deposit Request #{dep_id}</b>\n\n👤 {name_of(user)} (<code>{user.id}</code>)\n"
               f"💵 Amount: <b>{inr(amount)}</b>\n🕒 {to_ist(now_utc())}")
    markup = kb([[("✅ Approve", f"dp:ok:{dep_id}"), ("❌ Reject", f"dp:no:{dep_id}")]])
    try:
        if is_photo:
            await context.bot.send_photo(PAYMENT_GROUP_ID, fid, caption=caption, parse_mode=ParseMode.HTML,
                                         reply_markup=markup)
        else:
            await context.bot.send_document(PAYMENT_GROUP_ID, fid, caption=caption, parse_mode=ParseMode.HTML,
                                            reply_markup=markup)
    except TelegramError as e:
        log.error("cannot send to payment group: %s", e)
        run("DELETE FROM deposits WHERE id=?", (dep_id,))
        await reply(update, f"❌ Could not submit your request right now. Contact {DEV_USERNAME}.")
        return
    clear_state(context)
    await reply(update, f"✅ <b>Request submitted!</b>\n\nDeposit #{dep_id} of {inr(amount)} is waiting for admin "
                        "approval. You will be notified.", main_kb(user.id))

async def st_ban(update, context, st):
    target = parse_int(update.effective_message.text or "")
    if target is None or not get_user(target):
        await reply(update, "❌ User ID not found. Send a valid ID.", CANCEL_KB)
        return
    if is_admin(target) and st["ban"]:
        await reply(update, "❌ You cannot ban an admin.", CANCEL_KB)
        return
    run("UPDATE users SET banned=? WHERE user_id=?", (st["ban"], target))
    clear_state(context)
    await reply(update, f"{'🚫 Banned' if st['ban'] else '✅ Unbanned'}: {ulabel(target)}")
    try:
        await context.bot.send_message(target, "🚫 You have been banned from this bot." if st["ban"]
                                       else "✅ You have been unbanned. Send /start")
    except TelegramError:
        pass

async def st_adm(update, context, st):
    if not is_owner(update.effective_user.id):
        clear_state(context)
        return
    target = parse_int(update.effective_message.text or "")
    if target is None:
        await reply(update, "❌ Send a valid numeric User ID.", CANCEL_KB)
        return
    clear_state(context)
    if st["add"]:
        if target == OWNER_ID:
            await reply(update, "ℹ️ That is the Master Owner.")
            return
        run("INSERT OR IGNORE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)",
            (target, update.effective_user.id, now_utc()))
        await reply(update, f"✅ Admin added: <code>{target}</code>")
        try:
            await context.bot.send_message(target, "👮 You are now an admin. Send /start then open the Admin Panel.")
        except TelegramError:
            pass
    else:
        run("DELETE FROM admins WHERE user_id=?", (target,))
        await reply(update, f"✅ Admin removed: <code>{target}</code>")

def parse_chat_ref(t):
    t = t.strip()
    if re.fullmatch(r"-?\d{5,}", t):
        return int(t)
    m = re.fullmatch(r"(?:https?://t\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,31})", t)
    return "@" + m.group(1) if m else None

def add_channel(chat_id, username, title, link):
    run("INSERT OR IGNORE INTO channels(chat_id,username,title,invite_link,added_at) VALUES(?,?,?,?,?)",
        (chat_id, username, title, link, now_utc()))
    _join_cache.clear()

async def st_fj_add(update, context, st):
    ref = parse_chat_ref(update.effective_message.text or "")
    if ref is None:
        await reply(update, "❌ Invalid. Send @username or -100… chat ID.", CANCEL_KB)
        return
    try:
        chat = await context.bot.get_chat(ref)
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
    except TelegramError as e:
        await reply(update, f"❌ Cannot access that chat: {esc(str(e))}\nAdd the bot as admin first.", CANCEL_KB)
        return
    if me.status not in ("administrator", "creator"):
        await reply(update, "❌ Bot is not an admin there. Make the bot admin, then try again.", CANCEL_KB)
        return
    if fetchone("SELECT 1 FROM channels WHERE chat_id=?", (chat.id,)):
        clear_state(context)
        await reply(update, "ℹ️ This channel is already in the list.")
        return
    link = None
    if not chat.username:
        try:
            link = await context.bot.export_chat_invite_link(chat.id)
        except TelegramError:
            link = None
        if not link:
            context.user_data["state"] = {"name": "fj_link", "chat_id": chat.id, "title": chat.title}
            await reply(update, "🔗 Private channel: send its <b>invite link</b> (https://t.me/+…):", CANCEL_KB)
            return
    add_channel(chat.id, chat.username, chat.title, link)
    clear_state(context)
    t, m = force_join_view()
    await reply(update, "✅ Channel added.\n\n" + t, m)

async def st_fj_link(update, context, st):
    link = (update.effective_message.text or "").strip()
    if not link.startswith("https://t.me/"):
        await reply(update, "❌ Send a valid link starting with https://t.me/", CANCEL_KB)
        return
    add_channel(st["chat_id"], None, st["title"], link)
    clear_state(context)
    t, m = force_join_view()
    await reply(update, "✅ Channel added.\n\n" + t, m)

# -----------------------------------------------------------
# DYNAMIC TELETHON LOGIN & ACCOUNT SAVER
# -----------------------------------------------------------
async def st_acc_phone(update, context, st):
    if not TELETHON_OK:
        await reply(update, "❌ Live login is disabled: Telethon is not available on this server.\nInstall it and restart the bot.", CANCEL_KB)
        return
    ph = re.sub(r"[\s\-()]", "", update.effective_message.text or "")
    if not re.fullmatch(r"\+?\d{6,15}", ph):
        await reply(update, "❌ Invalid phone number. Try again.", CANCEL_KB)
        return
    if not ph.startswith("+"):
        ph = "+" + ph
    if fetchone("SELECT 1 FROM accounts WHERE phone=? AND status='available'", (ph,)):
        await reply(update, "❌ This number is already in stock.", CANCEL_KB)
        return

    msg = await update.effective_message.reply_text("⏳ Requesting OTP from Telegram...", reply_markup=CANCEL_KB)

    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        sent_code = await client.send_code_request(ph)

        context.user_data["state"] = {
            **st,
            "name": "acc_login_otp",
            "phone": ph,
            "client": client,
            "phone_code_hash": sent_code.phone_code_hash
        }
        await msg.edit_text(f"<b>📩 OTP Sent!</b>\n\nEnter the code received on <code>{ph}</code>:\n<i>(If it has spaces/hyphens, just enter the numbers)</i>", parse_mode=ParseMode.HTML, reply_markup=CANCEL_KB)
    except Exception as e:
        await msg.edit_text(f"❌ Telegram API Error:\n<code>{e}</code>", parse_mode=ParseMode.HTML, reply_markup=CANCEL_KB)
        clear_state(context)

async def st_acc_login_otp(update, context, st):
    otp = (update.effective_message.text or "").strip()
    client = st["client"]
    ph = st["phone"]
    try:
        await client.sign_in(ph, otp, phone_code_hash=st["phone_code_hash"])
        # Fix: Save session string IMMEDIATELY and disconnect to avoid timeouts
        session_str = client.session.save()
        await client.disconnect()

        context.user_data["state"] = {**st, "name": "acc_country", "session_str": session_str}
        await reply(update, "✅ Logged in successfully!\n\n🌍 Send the <b>country</b> name (e.g. 🇮🇳 India):", CANCEL_KB)
    except errors.SessionPasswordNeededError:
        context.user_data["state"] = {**st, "name": "acc_login_2fa"}
        await reply(update, "<b>🔐 2FA Detected</b>\nPlease enter the Cloud Password:", CANCEL_KB)
    except errors.PhoneCodeExpiredError:
        try:
            await client.disconnect()
        except Exception:
            pass
        await reply(update, "⏰ <b>The OTP expired</b> — Telegram login codes expire after a few minutes.\n\nSend the phone number again in the admin panel to get a fresh code.", CANCEL_KB)
        clear_state(context)
    except errors.PhoneCodeInvalidError:
        await reply(update, "❌ <b>Wrong code</b>. Please check and re-enter the numbers.", CANCEL_KB)
    except Exception as e:
        await reply(update, f"❌ OTP Error:\n<code>{e}</code>", CANCEL_KB)
        try:
            await client.disconnect()
        except Exception:
            pass
        clear_state(context)

async def st_acc_login_2fa(update, context, st):
    pwd = (update.effective_message.text or "").strip()
    client = st["client"]
    try:
        await client.sign_in(password=pwd)
        # Fix: Save session string IMMEDIATELY and disconnect to avoid timeouts
        session_str = client.session.save()
        await client.disconnect()

        st["password"] = pwd
        context.user_data["state"] = {**st, "name": "acc_country", "session_str": session_str}
        await reply(update, "✅ Logged in successfully!\n\n🌍 Send the <b>country</b> name (e.g. 🇮🇳 India):", CANCEL_KB)
    except Exception as e:
        await reply(update, f"❌ 2FA Error:\n<code>{e}</code>", CANCEL_KB)
        await client.disconnect()
        clear_state(context)

async def st_acc_country(update, context, st):
    c = (update.effective_message.text or "").strip()
    if not c or len(c) > 30:
        await reply(update, "❌ Country name must be 1-30 characters.", CANCEL_KB)
        return
    context.user_data["state"] = {**st, "name": "acc_price", "country": c}
    await reply(update, "💵 Send the <b>price</b> in ₹ (send <code>0</code> for a FREE account):", CANCEL_KB)

async def st_acc_price(update, context, st):
    p = parse_amount(update.effective_message.text or "")
    if p is None or p < 0:
        await reply(update, "❌ Send a valid price (0 or more).", CANCEL_KB)
        return

    session_str = st.get("session_str", "")
    pw = st.get("password", "")

    aid = run("INSERT INTO accounts(kind,country,price,phone,session,password,status,added_at,added_by) "
              "VALUES(?,?,?,?,?,?,'available',?,?)",
              (st["kind"], st["country"], p, st["phone"], session_str, pw, now_utc(), update.effective_user.id)).lastrowid

    clear_state(context)
    await reply(update, f"✅ <b>Account #{aid} added</b>\n\n{KIND[st['kind']]} | {esc(st['country'])} | "
                        f"{inr(p) if p > 0 else 'FREE'} | {esc(st['phone'])}\n"
                        f"Live OTP: enabled (Session Generated dynamically)",
                kb([[("➕ Add Another", "a:addacc"), ("⬅️ Admin Panel", "a:menu")]]))

# -----------------------------------------------------------
# REMOVE & EDIT ACCOUNT LOGIC
# -----------------------------------------------------------
async def st_rm_acc(update, context, st):
    aid = parse_int(update.effective_message.text or "")
    if aid is None:
        await reply(update, "❌ Send a valid account ID.", CANCEL_KB)
        return

    acc = fetchone("SELECT * FROM accounts WHERE id=? AND status='available'", (aid,))
    if acc:
        if acc["session"]:
            try:
                client = TelegramClient(StringSession(acc["session"]), API_ID, API_HASH)
                await client.connect()
                if await client.is_user_authorized():
                    await client.log_out()
                await client.disconnect()
            except Exception:
                pass

        run("DELETE FROM accounts WHERE id=?", (aid,))
        clear_state(context)
        await reply(update, "✅ Account removed and session logged out securely.", kb([[("⬅️ Admin Panel", "a:menu")]]))
    else:
        clear_state(context)
        await reply(update, "❌ Not found or already sold.", kb([[("⬅️ Admin Panel", "a:menu")]]))

async def st_edit_acc_find(update, context, st):
    val = (update.effective_message.text or "").strip()
    if val.startswith("+") or (val.isdigit() and len(val) >= 10):
        if not val.startswith("+"): val = "+" + val
        acc = fetchone("SELECT * FROM accounts WHERE phone=? AND status='available'", (val,))
    else:
        acc = fetchone("SELECT * FROM accounts WHERE id=? AND status='available'", (parse_int(val),))

    if not acc:
        await reply(update, "❌ Account not found or already sold.", CANCEL_KB)
        return

    context.user_data["state"] = {"name": "edit_acc_country", "acc_id": acc["id"], "old_c": acc["country"]}
    await reply(update, f"✅ Account Found: <code>{acc['phone']}</code>\nCurrent Country: <b>{acc['country']}</b>\nCurrent Price: <b>{inr(acc['price'])}</b>\n\n🌍 Send new <b>Country</b> name:", CANCEL_KB)

async def st_edit_acc_country(update, context, st):
    c = (update.effective_message.text or "").strip()
    context.user_data["state"] = {**st, "name": "edit_acc_price", "country": c}
    await reply(update, "💵 Send the new <b>price</b> in ₹:", CANCEL_KB)

async def st_edit_acc_price(update, context, st):
    p = parse_amount(update.effective_message.text or "")
    if p is None or p < 0:
        await reply(update, "❌ Send a valid price.", CANCEL_KB)
        return

    run("UPDATE accounts SET country=?, price=? WHERE id=?", (st["country"], p, st["acc_id"]))
    clear_state(context)
    await reply(update, f"✅ Account #{st['acc_id']} Updated!\n🌍 Country: {st['country']}\n💰 Price: {inr(p)}",
                kb([[("⬅️ Admin Panel", "a:menu")]]))

async def st_ref_reward(update, context, st):
    v = parse_amount(update.effective_message.text or "")
    if v is None or v < 0 or v > 10000:
        await reply(update, "❌ Send a valid amount (0 - 10000).", CANCEL_KB)
        return
    set_setting("referral_reward", v)
    clear_state(context)
    await reply(update, f"✅ Referral reward set to <b>{inr(v)}</b> (applies to future referrals).")

async def st_ref_cancel(update, context, st):
    target = parse_int(update.effective_message.text or "")
    clear_state(context)
    if target is None:
        await reply(update, "❌ Invalid ID.")
        return
    done = await cancel_referral(context.bot, target, "Cancelled by Admin.", force=True)
    await reply(update, "✅ Referral cancelled, reward reversed and referrer notified." if done
                else "❌ No active/pending referral found for that user.")

async def st_ref_hist(update, context, st):
    target = parse_int(update.effective_message.text or "")
    clear_state(context)
    if target is None:
        await reply(update, "❌ Invalid ID.")
        return
    await reply(update, referral_history_text(target), kb([[("⬅️ Admin Panel", "a:menu")]]))

async def st_bal(update, context, st):
    parts = (update.effective_message.text or "").split()
    if len(parts) != 2:
        await reply(update, "❌ Format: <code>USER_ID AMOUNT</code>", CANCEL_KB)
        return
    target, amt = parse_int(parts[0]), parse_amount(parts[1])
    if target is None or amt is None or amt <= 0 or not get_user(target):
        await reply(update, "❌ Invalid user ID / amount, or user not found.", CANCEL_KB)
        return
    short = None
    with tx() as c:
        if st["mode"] == "add":
            c.execute("UPDATE users SET wallet=ROUND(wallet+?,2) WHERE user_id=?", (amt, target))
            add_txn(c, target, amt, "admin_add", "Added by admin")
        else:
            w = c.execute("SELECT wallet FROM users WHERE user_id=?", (target,)).fetchone()["wallet"]
            if w + 1e-9 < amt:
                short = w
            else:
                c.execute("UPDATE users SET wallet=ROUND(wallet-?,2) WHERE user_id=?", (amt, target))
                add_txn(c, target, -amt, "admin_deduct", "Deducted by admin")
    if short is not None:
        await reply(update, f"❌ User has only {inr(short)}.", CANCEL_KB)
        return
    clear_state(context)
    bal = get_user(target)["wallet"]
    add = st["mode"] == "add"
    await reply(update, f"✅ {inr(amt)} {'added to' if add else 'deducted from'} {ulabel(target)}.\n"
                        f"New balance: <b>{inr(bal)}</b>")
    try:
        await context.bot.send_message(
            target, f"{'➕' if add else '➖'} {inr(amt)} was {'added to' if add else 'deducted from'} your wallet "
                    f"by admin.\n💰 Balance: <b>{inr(bal)}</b>", parse_mode=ParseMode.HTML)
    except TelegramError:
        pass

async def st_broadcast(update, context, st):
    msg = update.effective_message
    clear_state(context)
    context.user_data["bc"] = (msg.chat_id, msg.message_id)
    n = scalar("SELECT COUNT(*) FROM users WHERE banned=0")
    await context.bot.copy_message(msg.chat_id, msg.chat_id, msg.message_id)   # preview
    await reply(update, f"👆 Preview. Send this to <b>{n}</b> users?",
                kb([[(f"✅ Send to {n} users", "bc:ok"), ("❌ Cancel", "bc:no")]]))

async def st_db_upload(update, context, st):
    if not is_owner(update.effective_user.id):
        clear_state(context)
        return
    msg = update.effective_message
    doc = msg.document
    if not doc:
        await reply(update, "❌ Send the backup as a <b>document</b> (.db / .sqlite).", CANCEL_KB)
        return
    fname = (doc.file_name or "").lower()
    if not fname.endswith((".db", ".sqlite", ".sqlite3")):
        await reply(update, "❌ Only .db / .sqlite files are accepted.", CANCEL_KB)
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await reply(update, "❌ File is larger than 20 MB (Telegram bot limit).", CANCEL_KB)
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    tmp = os.path.join(BACKUP_DIR, f"incoming_{stamp()}.tmp")
    try:
        f = await context.bot.get_file(doc.file_id)
        await f.download_to_drive(tmp)
    except TelegramError as e:
        await reply(update, f"❌ Download failed: {esc(str(e))}", CANCEL_KB)
        return
    ok, m, _ = validate_db_file(tmp)
    if not ok:
        try:
            os.remove(tmp)
        except OSError:
            pass
        clear_state(context)
        await reply(update, f"❌ <b>Invalid database - not restored.</b>\n\n{esc(m)}",
                    kb([[("⬅️ Database Menu", "db:menu")]]))
        return
    name = f"uploaded_{stamp()}.db"
    os.replace(tmp, os.path.join(BACKUP_DIR, name))
    clear_state(context)
    text, markup, okk = restore_confirm_view(name)
    context.user_data["pending_restore"] = name if okk else None
    await reply(update, text, markup)

# =============================================================================
# MAIN HANDLERS
# =============================================================================
MENU_HANDLERS = {
    "👤 Account": show_account,
    "🛒 Browse Account": show_browse,
    "📦 My Orders": show_orders,
    "💰 Wallet": show_wallet,
    "🎁 Referral": show_referral,
    "☎️ Support": show_support,
    "📜 Terms": show_terms,
    "👑 Admin Panel": show_admin,
}

STATE_HANDLERS = {
    "dep_amount": st_dep_amount, "dep_ss": st_dep_ss, "ban": st_ban, "adm": st_adm,
    "fj_add": st_fj_add, "fj_link": st_fj_link,
    "acc_phone": st_acc_phone, "acc_login_otp": st_acc_login_otp, "acc_login_2fa": st_acc_login_2fa,
    "acc_country": st_acc_country, "acc_price": st_acc_price,
    "rm_acc": st_rm_acc, "edit_acc_find": st_edit_acc_find,
    "edit_acc_country": st_edit_acc_country, "edit_acc_price": st_edit_acc_price,
    "ref_reward": st_ref_reward, "ref_cancel": st_ref_cancel, "ref_hist": st_ref_hist,
    "bal": st_bal, "broadcast": st_broadcast, "db_upload": st_db_upload,
}
ADMIN_STATES = set(STATE_HANDLERS) - {"dep_amount", "dep_ss"}

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ref_id = None
    if context.args and context.args[0].startswith("ref_"):
        ref_id = parse_int(context.args[0][4:])
    register_user(user, ref_id)
    if get_user(user.id)["banned"]:
        await reply(update, "🚫 You are banned from using this bot.")
        return
    clear_state(context)
    if not is_admin(user.id) and not await gate(update, context):
        return
    await send_welcome(update, context)

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    await show_admin(update, context)

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await reply(update, f"🆔 Your Telegram ID: <code>{uid}</code>\nAdmin: <b>{'YES' if is_admin(uid) else 'NO'}</b>")

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    context.user_data.pop("bc", None)
    await reply(update, "❌ Cancelled.", main_kb(update.effective_user.id))

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    register_user(user)
    row = get_user(user.id)
    if row["banned"]:
        await reply(update, "🚫 You are banned from using this bot.")
        return
    admin = is_admin(user.id)
    if not admin and not await gate(update, context):
        return
    text = msg.text or ""
    if text in MENU_HANDLERS:
        clear_state(context)
        await MENU_HANDLERS[text](update, context)
        return
    st = context.user_data.get("state")
    if st:
        if st["name"] in ADMIN_STATES and not admin:
            clear_state(context)
            return
        try:
            await STATE_HANDLERS[st["name"]](update, context, st)
        except Exception as e:
            log.exception("State handler %s failed: %s", st["name"], e)
            try:
                await reply(update, f"⚠️ Something went wrong while processing your input ({st['name']}).\nPlease try again or send /cancel.", CANCEL_KB)
            except Exception:
                pass
        return
    await reply(update, "👇 Please use the menu buttons.", main_kb(user.id))

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    user = q.from_user
    if data.startswith("dp:"):
        await cb_deposit(update, context, data)
        return
    if q.message and q.message.chat.type != "private":
        await q.answer()
        return
    register_user(user)
    if get_user(user.id)["banned"]:
        await q.answer("🚫 You are banned.", show_alert=True)
        return
    admin = is_admin(user.id)
    if data == "noop":
        await q.answer()
        return
    if data == "x:cancel":
        await q.answer("Cancelled")
        clear_state(context)
        context.user_data.pop("bc", None)
        await send(update, context, "❌ Cancelled.")
        return
    if data == "verify":
        await cb_verify(update, context)
        return
    if data.startswith(("a:", "fj:", "acc:", "bc:", "db:")):
        if not admin:
            await q.answer("⛔ Admin only.", show_alert=True)
            return
        await cb_admin(update, context, data)
        return
    if not admin:
        if not await gate(update, context):
            await q.answer()
            return
    await cb_user(update, context, data)

async def on_error(update, context):
    log.error("Unhandled error: %s", context.error, exc_info=context.error)

async def post_init(app: Application):
    if scalar("SELECT COUNT(*) FROM channels") == 0:
        for ref in DEFAULT_FORCE_JOIN:
            try:
                chat = await app.bot.get_chat(ref)
                add_channel(chat.id, chat.username, chat.title, None)
                log.info("force-join channel seeded: %s", ref)
            except TelegramError as e:
                log.warning("could not seed %s: %s", ref, e)

    app.bot_data["watchdog"] = asyncio.create_task(referral_watchdog(app))

    # Session Background Cleaner
    app.bot_data["session_cleaner"] = asyncio.create_task(session_cleaner_watchdog(app))

    me = await app.bot.get_me()
    log.info("Bot started as @%s", me.username)
    if not (TELETHON_OK and API_ID and API_HASH):
        log.error("Live OTP disabled: Telethon or API credentials unavailable.")
        log.error("Fix: run this exact command (same Python as the bot), then restart:")
        log.error("    \"%s\" -m pip install telethon", sys.executable)

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is missing.")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    connect()
    init_db()
    app = (Application.builder().token(BOT_TOKEN).post_init(post_init)
           .concurrent_updates(True).build())
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start_cmd, filters=private))
    app.add_handler(CommandHandler("admin", admin_cmd, filters=private))
    app.add_handler(CommandHandler("cancel", cancel_cmd, filters=private))
    app.add_handler(CommandHandler("id", id_cmd, filters=private))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
