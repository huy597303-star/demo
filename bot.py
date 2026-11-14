import os
import re
import csv
import io
import json
import hmac
import hashlib
import logging
import sqlite3
import asyncio
from datetime import datetime, timezone
from contextlib import contextmanager

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
SEPAY_WEBHOOK_SECRET = os.getenv("SEPAY_WEBHOOK_SECRET", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot_store.db")
PORT = int(os.getenv("PORT", "8080"))
CURRENCY = "VNĐ"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("socialstock")


# ================= DATABASE =================
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def has_column(conn, table, column):
    return column in [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        # Preserve legacy tables and rows.
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL, account_info TEXT NOT NULL,
            status TEXT DEFAULT 'AVAILABLE',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        migrations = {
            "users": {
                "username": "TEXT", "first_name": "TEXT",
                "updated_at": "TEXT"
            },
            "accounts": {
                "cost_price": "INTEGER DEFAULT 0",
                "sale_price": "INTEGER DEFAULT 0",
                "sold_to": "INTEGER",
                "sold_at": "TEXT",
                "note": "TEXT",
                "updated_at": "TEXT"
            }
        }
        for table, cols in migrations.items():
            for col, definition in cols.items():
                if not has_column(c, table, col):
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")

        c.execute("""CREATE TABLE IF NOT EXISTS products (
            name TEXT PRIMARY KEY COLLATE NOCASE,
            sale_price INTEGER NOT NULL DEFAULT 0,
            cost_price INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, account_id INTEGER,
            product_name TEXT NOT NULL, sale_price INTEGER NOT NULL,
            cost_price INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'COMPLETED',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            refunded_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
            kind TEXT NOT NULL, note TEXT DEFAULT '',
            reference TEXT UNIQUE, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS payment_events (
            event_key TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL, content TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER,
            action TEXT NOT NULL, details TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id)")
        # Seed products from existing account types without changing old prices.
        names = c.execute("SELECT DISTINCT product_name FROM accounts").fetchall()
        legacy_prices = {
            "gmail": 5000, "facebook": 2000, "tiktok": 2000,
            "instagram": 500, "ig": 500, "hotmail": 1500
        }
        for row in names:
            pname = row["product_name"]
            price = next((v for k, v in legacy_prices.items() if k in pname.lower()), 2000)
            c.execute("INSERT OR IGNORE INTO products(name,sale_price) VALUES(?,?)", (pname, price))
            c.execute("""UPDATE accounts SET sale_price=?
                         WHERE product_name=? COLLATE NOCASE AND sale_price=0""", (price, pname))


def is_admin(user_id):
    return user_id in ADMIN_IDS


def money(n):
    return f"{int(n):,} {CURRENCY}"


def get_balance(user_id):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id, balance) VALUES(?,0)", (user_id,))
        row = c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["balance"])


def record_transaction(c, user_id, amount, kind, note="", reference=None):
    c.execute("""INSERT INTO transactions(user_id,amount,kind,note,reference)
                 VALUES(?,?,?,?,?)""", (user_id, amount, kind, note, reference))


def change_balance(user_id, amount, kind, note="", reference=None):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id,balance) VALUES(?,0)", (user_id,))
        c.execute("UPDATE users SET balance=balance+?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                  (amount, user_id))
        record_transaction(c, user_id, amount, kind, note, reference)
        return c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()["balance"]


def audit(admin_id, action, details=""):
    with db() as c:
        c.execute("INSERT INTO audit_log(admin_id,action,details) VALUES(?,?,?)",
                  (admin_id, action, details[:1000]))


# ================= KEYBOARDS =================
def home_keyboard(user_id):
    rows = [
        [InlineKeyboardButton("🛍 Sản phẩm", callback_data="products"),
         InlineKeyboardButton("📦 Tồn kho", callback_data="stock")],
        [InlineKeyboardButton("👤 Tài khoản", callback_data="me"),
         InlineKeyboardButton("📜 Lịch sử", callback_data="history")],
        [InlineKeyboardButton("💳 Nạp tiền", callback_data="recharge")]
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("🛠 ADMIN V5", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def back_home():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Trang chủ", callback_data="home")]])


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Kho hàng", callback_data="adm_stock"),
         InlineKeyboardButton("➕ Nhập hàng", callback_data="adm_add")],
        [InlineKeyboardButton("💵 Giá sản phẩm", callback_data="adm_prices"),
         InlineKeyboardButton("🧾 Đơn hàng", callback_data="adm_orders")],
        [InlineKeyboardButton("📊 Báo cáo", callback_data="adm_report"),
         InlineKeyboardButton("👥 Khách hàng", callback_data="adm_users")],
        [InlineKeyboardButton("⚠️ Hàng lỗi", callback_data="adm_defects"),
         InlineKeyboardButton("💾 Sao lưu DB", callback_data="adm_backup")],
        [InlineKeyboardButton("🏠 Trang chủ", callback_data="home")]
    ])


# ================= CUSTOMER =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    with db() as c:
        c.execute("""INSERT INTO users(user_id,username,first_name)
                     VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                     username=excluded.username, first_name=excluded.first_name,
                     updated_at=CURRENT_TIMESTAMP""",
                  (u.id, u.username, u.first_name))
    await update.message.reply_text(
        f"Xin chào {u.first_name or 'bạn'}!\n"
        f"💰 Số dư: {money(get_balance(u.id))}\n\nChọn chức năng:",
        reply_markup=home_keyboard(u.id)
    )


async def show_products(query):
    with db() as c:
        rows = c.execute("""SELECT p.name,p.sale_price,COUNT(a.id) AS qty
            FROM products p LEFT JOIN accounts a
            ON a.product_name=p.name COLLATE NOCASE AND a.status='AVAILABLE'
            WHERE p.enabled=1 GROUP BY p.name ORDER BY p.name""").fetchall()
    if not rows:
        await query.edit_message_text("Hiện chưa có sản phẩm.", reply_markup=back_home())
        return
    buttons = []
    lines = ["🛍 DANH SÁCH SẢN PHẨM"]
    for r in rows:
        lines.append(f"• {r['name']} — {money(r['sale_price'])} — còn {r['qty']}")
        if r["qty"] > 0 and r["sale_price"] > 0:
            buttons.append([InlineKeyboardButton(
                f"Mua {r['name']} · {money(r['sale_price'])}",
                callback_data=f"buy:{r['name']}"
            )])
    buttons.append([InlineKeyboardButton("🔙 Quay lại", callback_data="home")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def purchase(query, context, product):
    uid = query.from_user.id
    # Atomic debit + stock reservation in one SQLite transaction.
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        p = c.execute("SELECT sale_price FROM products WHERE name=? AND enabled=1",
                      (product,)).fetchone()
        if not p or p["sale_price"] <= 0:
            await query.edit_message_text("Sản phẩm chưa được cấu hình giá.", reply_markup=back_home())
            return
        c.execute("INSERT OR IGNORE INTO users(user_id,balance) VALUES(?,0)", (uid,))
        user = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        if user["balance"] < p["sale_price"]:
            await query.edit_message_text(
                f"Không đủ số dư.\nGiá: {money(p['sale_price'])}\nSố dư: {money(user['balance'])}",
                reply_markup=back_home()
            )
            return
        acc = c.execute("""SELECT id,account_info,cost_price FROM accounts
            WHERE product_name=? COLLATE NOCASE AND status='AVAILABLE'
            ORDER BY id LIMIT 1""", (product,)).fetchone()
        if not acc:
            await query.edit_message_text("Sản phẩm vừa hết hàng.", reply_markup=back_home())
            return
        c.execute("UPDATE accounts SET status='SOLD',sold_to=?,sold_at=CURRENT_TIMESTAMP WHERE id=?",
                  (uid, acc["id"]))
        c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (p["sale_price"], uid))
        cur = c.execute("""INSERT INTO orders(user_id,account_id,product_name,sale_price,cost_price)
                           VALUES(?,?,?,?,?)""",
                        (uid, acc["id"], product, p["sale_price"], acc["cost_price"] or 0))
        record_transaction(c, uid, -p["sale_price"], "PURCHASE", f"Đơn #{cur.lastrowid}")
        item_info = acc["account_info"]
    await query.edit_message_text(
        f"✅ Đặt hàng thành công · Đơn #{cur.lastrowid}\nSản phẩm: {product}\nĐã trừ: {money(p['sale_price'])}",
        reply_markup=back_home()
    )
    try:
        await context.bot.send_message(
            uid,
            f"Thông tin sản phẩm đơn #{cur.lastrowid}:\n\n{item_info}\n\n"
            "Vui lòng bảo vệ thông tin nhận được và liên hệ quản trị viên nếu có vấn đề."
        )
    except Exception:
        log.warning("Could not DM delivery to user %s", uid)


async def customer_history(query):
    with db() as c:
        rows = c.execute("""SELECT id,product_name,sale_price,status,created_at
            FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10""",
            (query.from_user.id,)).fetchall()
        tx = c.execute("""SELECT amount,kind,note,created_at FROM transactions
            WHERE user_id=? ORDER BY id DESC LIMIT 8""",
            (query.from_user.id,)).fetchall()
    lines = ["📜 LỊCH SỬ ĐƠN HÀNG"]
    lines += [f"#{r['id']} {r['product_name']} · {money(r['sale_price'])} · {r['status']} · {r['created_at']}"
              for r in rows] or ["Chưa có đơn hàng."]
    lines += ["", "💳 GIAO DỊCH SỐ DƯ"]
    lines += [f"{r['created_at']} · {r['kind']} · {money(r['amount'])} · {r['note']}"
              for r in tx] or ["Chưa có giao dịch."]
    await query.edit_message_text("\n".join(lines)[:4000], reply_markup=back_home())


# ================= ADMIN HELPERS =================
async def require_admin(update_or_query):
    user = update_or_query.effective_user if isinstance(update_or_query, Update) else update_or_query.from_user
    if not is_admin(user.id):
        if isinstance(update_or_query, Update) and update_or_query.effective_message:
            await update_or_query.effective_message.reply_text("Bạn không có quyền sử dụng chức năng này.")
        return False
    return True


async def admin_panel(update, context):
    if not await require_admin(update):
        return
    await update.effective_message.reply_text("🛠 BẢNG QUẢN TRỊ V5", reply_markup=admin_keyboard())


async def admin_import(update, context):
    if not await require_admin(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Cách dùng: /import <tên_sản_phẩm>\n"
            "Sau đó gửi file TXT dạng mỗi dòng: tên_sản_phẩm|thông_tin_hàng\n"
            "Hoặc /import để xem hướng dẫn."
        )
        return
    context.user_data["await_import"] = " ".join(context.args)
    await update.message.reply_text(
        "Hãy gửi file .txt. Mỗi dòng theo dạng:\n"
        "Tên sản phẩm|Thông tin hàng\n\n"
        "Ví dụ:\nDemo|Mã hàng mẫu 001"
    )


async def text_admin_input(update, context):
    if not is_admin(update.effective_user.id):
        return
    if context.user_data.pop("await_manual_add", False):
        raw = (update.message.text or "").strip()
        if "|" not in raw:
            await update.message.reply_text("Sai định dạng. Gửi: Tên sản phẩm | Thông tin hàng")
            return
        name, info = [x.strip() for x in raw.split("|", 1)]
        if not name or not info:
            await update.message.reply_text("Tên sản phẩm và thông tin hàng không được để trống.")
            return
        with db() as c:
            c.execute("INSERT INTO accounts(product_name,account_info,status) VALUES(?,?,'AVAILABLE')",
                      (name, info))
            c.execute("INSERT OR IGNORE INTO products(name) VALUES(?)", (name,))
        audit(update.effective_user.id, "ADD_ITEM_MENU", name)
        await update.message.reply_text(f"Đã thêm hàng vào kho {name}.")
        return


async def document_handler(update, context):
    if not await require_admin(update):
        return
    product = context.user_data.get("await_import")
    doc = update.message.document
    if not product or not doc or not doc.file_name.lower().endswith(".txt"):
        return
    tg_file = await doc.get_file()
    raw = await tg_file.download_as_bytearray()
    text = raw.decode("utf-8-sig", errors="replace")
    added, skipped = 0, 0
    with db() as c:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                name, info = line.split("|", 1)
                name, info = name.strip(), info.strip()
            else:
                name, info = product, line
            if not name or not info:
                skipped += 1
                continue
            exists = c.execute("SELECT 1 FROM accounts WHERE product_name=? AND account_info=?",
                               (name, info)).fetchone()
            if exists:
                skipped += 1
                continue
            c.execute("INSERT INTO accounts(product_name,account_info,status) VALUES(?,?,'AVAILABLE')",
                      (name, info))
            c.execute("INSERT OR IGNORE INTO products(name) VALUES(?)", (name,))
            added += 1
    context.user_data.pop("await_import", None)
    audit(update.effective_user.id, "IMPORT_TXT", f"added={added}, skipped={skipped}")
    await update.message.reply_text(f"Import hoàn tất.\nThêm: {added}\nBỏ qua/trùng: {skipped}")


async def add_item_command(update, context):
    if not await require_admin(update):
        return
    raw = " ".join(context.args)
    if "|" not in raw:
        await update.message.reply_text("Dùng: /themhang Tên sản phẩm | Thông tin hàng")
        return
    name, info = [x.strip() for x in raw.split("|", 1)]
    if not name or not info:
        await update.message.reply_text("Tên sản phẩm và thông tin hàng không được để trống.")
        return
    with db() as c:
        c.execute("INSERT INTO accounts(product_name,account_info,status) VALUES(?,?,'AVAILABLE')",
                  (name, info))
        c.execute("INSERT OR IGNORE INTO products(name) VALUES(?)", (name,))
    audit(update.effective_user.id, "ADD_ITEM", name)
    await update.message.reply_text(f"Đã thêm một mặt hàng vào kho {name}.")


async def set_price(update, context):
    if not await require_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text("Dùng: /setgia <tên_sản_phẩm> <giá_bán> <giá_vốn>")
        return
    name = context.args[0]
    try:
        sale, cost = int(context.args[-2]), int(context.args[-1])
        if sale < 0 or cost < 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Giá phải là số nguyên không âm.")
        return
    with db() as c:
        c.execute("INSERT OR IGNORE INTO products(name) VALUES(?)", (name,))
        c.execute("UPDATE products SET sale_price=?,cost_price=? WHERE name=?", (sale,cost,name))
        c.execute("UPDATE accounts SET sale_price=?,cost_price=? WHERE product_name=? AND status='AVAILABLE'",
                  (sale,cost,name))
    audit(update.effective_user.id, "SET_PRICE", f"{name}: sale={sale}, cost={cost}")
    await update.message.reply_text(f"Đã đặt giá {name}: bán {money(sale)}, vốn {money(cost)}.")


async def stock_text():
    with db() as c:
        rows = c.execute("""SELECT p.name,p.sale_price,p.cost_price,
            SUM(CASE WHEN a.status='AVAILABLE' THEN 1 ELSE 0 END) qty,
            SUM(CASE WHEN a.status='DEFECT' THEN 1 ELSE 0 END) defects
            FROM products p LEFT JOIN accounts a ON a.product_name=p.name COLLATE NOCASE
            GROUP BY p.name ORDER BY p.name""").fetchall()
    if not rows:
        return "Kho chưa có sản phẩm."
    return "📦 TỒN KHO\n" + "\n".join(
        f"• {r['name']}: còn {r['qty'] or 0}, lỗi {r['defects'] or 0}, giá {money(r['sale_price'])}"
        for r in rows
    )


async def report_text():
    with db() as c:
        today = c.execute("""SELECT COUNT(*) n,COALESCE(SUM(sale_price),0) revenue,
            COALESCE(SUM(cost_price),0) cost FROM orders
            WHERE status='COMPLETED' AND date(created_at)=date('now','localtime')""").fetchone()
        total = c.execute("""SELECT COUNT(*) n,COALESCE(SUM(sale_price),0) revenue,
            COALESCE(SUM(cost_price),0) cost FROM orders WHERE status='COMPLETED'""").fetchone()
        available = c.execute("SELECT COUNT(*) n FROM accounts WHERE status='AVAILABLE'").fetchone()["n"]
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    return (
        "📊 BÁO CÁO\n\n"
        f"Hôm nay: {today['n']} đơn\nDoanh thu: {money(today['revenue'])}\n"
        f"Giá vốn: {money(today['cost'])}\nLãi gộp: {money(today['revenue']-today['cost'])}\n\n"
        f"Tổng: {total['n']} đơn\nDoanh thu: {money(total['revenue'])}\n"
        f"Giá vốn: {money(total['cost'])}\nLãi gộp: {money(total['revenue']-total['cost'])}\n\n"
        f"Tồn kho: {available}\nKhách hàng: {users}\n"
        "Lưu ý: chưa trừ chi phí vận hành, hoàn tiền và các khoản ngoài hệ thống."
    )


async def admin_callback(query, context):
    if not is_admin(query.from_user.id):
        await query.answer("Không có quyền.", show_alert=True)
        return
    data = query.data
    if data == "admin":
        await query.edit_message_text("🛠 BẢNG QUẢN TRỊ V5", reply_markup=admin_keyboard())
    elif data == "adm_stock":
        await query.edit_message_text(await stock_text(), reply_markup=admin_keyboard())
    elif data == "adm_add":
        await query.edit_message_text(
            "➕ NHẬP HÀNG\n\nGửi lệnh:\n/themhang Tên sản phẩm | Thông tin hàng\n\n"
            "Hoặc nhập TXT:\n/import Tên sản phẩm\nrồi gửi file .txt.",
            reply_markup=admin_keyboard()
        )
    elif data == "adm_prices":
        with db() as c:
            rows = c.execute("SELECT name,sale_price,cost_price FROM products ORDER BY name").fetchall()
        lines = ["💵 BẢNG GIÁ"]
        lines += [f"{r['name']}: bán {money(r['sale_price'])} · vốn {money(r['cost_price'])}" for r in rows]
        lines += ["", "Cập nhật: /setgia ten 10000 3000"]
        await query.edit_message_text("\n".join(lines), reply_markup=admin_keyboard())
    elif data == "adm_orders":
        with db() as c:
            rows = c.execute("SELECT id,user_id,product_name,sale_price,status,created_at FROM orders ORDER BY id DESC LIMIT 12").fetchall()
        lines = ["🧾 12 ĐƠN GẦN NHẤT"]
        lines += [f"#{r['id']} · {r['product_name']} · {money(r['sale_price'])} · UID {r['user_id']} · {r['status']}"
                  for r in rows] or ["Chưa có đơn."]
        await query.edit_message_text("\n".join(lines), reply_markup=admin_keyboard())
    elif data == "adm_report":
        await query.edit_message_text(await report_text(), reply_markup=admin_keyboard())
    elif data == "adm_users":
        with db() as c:
            rows = c.execute("""SELECT u.user_id,u.username,u.balance,COUNT(o.id) orders
                FROM users u LEFT JOIN orders o ON o.user_id=u.user_id
                GROUP BY u.user_id ORDER BY u.balance DESC LIMIT 15""").fetchall()
        lines = ["👥 KHÁCH HÀNG"]
        lines += [f"{r['user_id']} @{r['username'] or '-'} · dư {money(r['balance'])} · {r['orders']} đơn"
                  for r in rows] or ["Chưa có khách."]
        await query.edit_message_text("\n".join(lines), reply_markup=admin_keyboard())
    elif data == "adm_defects":
        await query.edit_message_text(
            "Đánh dấu hàng lỗi:\n/loi <ID_hàng>\nKhôi phục hàng:\n/khoiphuc <ID_hàng>",
            reply_markup=admin_keyboard()
        )
    elif data == "adm_backup":
        await query.edit_message_text(
            "Dùng /backup để nhận bản sao lưu SQLite. Hãy bảo quản file backup an toàn.",
            reply_markup=admin_keyboard()
        )


async def mark_defect(update, context):
    if not await require_admin(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Dùng: /loi <ID_hàng>")
        return
    item_id = int(context.args[0])
    with db() as c:
        cur = c.execute("UPDATE accounts SET status='DEFECT' WHERE id=? AND status='AVAILABLE'", (item_id,))
    if cur.rowcount:
        audit(update.effective_user.id, "MARK_DEFECT", str(item_id))
        await update.message.reply_text(f"Đã đánh dấu hàng #{item_id} là lỗi.")
    else:
        await update.message.reply_text("Không tìm thấy hàng đang có sẵn với ID này.")


async def restore_item(update, context):
    if not await require_admin(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Dùng: /khoiphuc <ID_hàng>")
        return
    with db() as c:
        cur = c.execute("UPDATE accounts SET status='AVAILABLE' WHERE id=? AND status='DEFECT'",
                        (int(context.args[0]),))
    await update.message.reply_text("Đã khôi phục." if cur.rowcount else "Không thể khôi phục mặt hàng này.")


async def add_money_command(update, context):
    if not await require_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Dùng: /cong <user_id> <số_tiền> [lý_do]")
        return
    try:
        uid, amount = int(context.args[0]), int(context.args[1])
        if amount <= 0: raise ValueError()
    except ValueError:
        await update.message.reply_text("ID và số tiền không hợp lệ.")
        return
    note = " ".join(context.args[2:]) or "Admin cộng tiền"
    balance = change_balance(uid, amount, "ADMIN_CREDIT", note)
    audit(update.effective_user.id, "CREDIT", f"{uid} +{amount} {note}")
    await update.message.reply_text(f"Đã cộng {money(amount)} cho {uid}. Số dư: {money(balance)}")


async def deduct_money_command(update, context):
    if not await require_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Dùng: /tru <user_id> <số_tiền> [lý_do]")
        return
    try:
        uid, amount = int(context.args[0]), int(context.args[1])
        if amount <= 0: raise ValueError()
    except ValueError:
        await update.message.reply_text("ID và số tiền không hợp lệ.")
        return
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id,balance) VALUES(?,0)", (uid,))
        bal = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()["balance"]
        if bal < amount:
            await update.message.reply_text("Số dư người dùng không đủ.")
            return
        c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount,uid))
        record_transaction(c, uid, -amount, "ADMIN_DEBIT", " ".join(context.args[2:]) or "Admin trừ tiền")
        new_bal = bal - amount
    await update.message.reply_text(f"Đã trừ {money(amount)}. Số dư mới: {money(new_bal)}")


async def refund_order(update, context):
    if not await require_admin(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Dùng: /hoantien <ma_don>")
        return
    oid = int(context.args[0])
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        order = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not order or order["status"] != "COMPLETED":
            await update.message.reply_text("Đơn không tồn tại hoặc đã xử lý.")
            return
        c.execute("UPDATE orders SET status='REFUNDED',refunded_at=CURRENT_TIMESTAMP WHERE id=?", (oid,))
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (order["sale_price"],order["user_id"]))
        record_transaction(c, order["user_id"], order["sale_price"], "REFUND", f"Hoàn đơn #{oid}")
        if order["account_id"]:
            c.execute("UPDATE accounts SET status='REFUNDED' WHERE id=?", (order["account_id"],))
    audit(update.effective_user.id, "REFUND", str(oid))
    await update.message.reply_text(f"Đã hoàn số dư {money(order['sale_price'])} cho đơn #{oid}. Hàng đã chuyển trạng thái REFUNDED, không tự đưa lại kho.")


async def backup_command(update, context):
    if not await require_admin(update):
        return
    # SQLite backup API produces a consistent snapshot even in WAL mode.
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(path)
        src.backup(dst)
        dst.close()
        src.close()
        with open(path, "rb") as f:
            await update.message.reply_document(f, filename="socialstock_backup.db",
                                                caption="Bản sao lưu SQLite V5")
    finally:
        try: os.remove(path)
        except OSError: pass


# ================= PAYMENT WEBHOOK =================
async def sepay_webhook(request):
    try:
        raw = await request.read()
        if not SEPAY_WEBHOOK_SECRET:
            return web.json_response({"status":"webhook_secret_not_configured"}, status=503)
        supplied = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not hmac.compare_digest(supplied, SEPAY_WEBHOOK_SECRET):
            return web.json_response({"status":"unauthorized"}, status=401)
        data = json.loads(raw.decode("utf-8"))
        amount = int(data.get("transferAmount", 0))
        content = str(data.get("content", ""))
        # Use provider transaction ID for idempotency; never credit based only on text.
        event_key = str(data.get("id") or data.get("referenceCode") or data.get("transactionId") or "").strip()
        match = re.search(r"\b(\d{5,15})\b", content)
        if amount <= 0 or not event_key or not match:
            return web.json_response({"status":"ignored"}, status=200)
        uid = int(match.group(1))
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            inserted = c.execute("""INSERT OR IGNORE INTO payment_events(event_key,user_id,amount,content)
                VALUES(?,?,?,?)""", (event_key,uid,amount,content))
            if inserted.rowcount == 0:
                return web.json_response({"status":"duplicate"}, status=200)
            c.execute("INSERT OR IGNORE INTO users(user_id,balance) VALUES(?,0)", (uid,))
            c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount,uid))
            record_transaction(c, uid, amount, "SEPAY_DEPOSIT", content, event_key)
            balance = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()["balance"]
        bot = request.app["bot"]
        try:
            await bot.send_message(uid, f"Nạp tiền thành công: +{money(amount)}\nSố dư: {money(balance)}")
        except Exception:
            pass
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, f"SePay: UID {uid} nạp {money(amount)} · mã {event_key}")
            except Exception:
                pass
        return web.json_response({"status":"success"}, status=200)
    except Exception:
        log.exception("SePay webhook error")
        return web.json_response({"status":"error"}, status=400)


# ================= CALLBACK ROUTER =================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id
    if data.startswith("buy:"):
        await purchase(q, context, data[4:])
    elif data.startswith("adm_") or data == "admin":
        await admin_callback(q, context)
    elif data == "home":
        await q.edit_message_text(
            f"Xin chào {q.from_user.first_name or 'bạn'}!\nSố dư: {money(get_balance(uid))}\nChọn chức năng:",
            reply_markup=home_keyboard(uid)
        )
    elif data == "products":
        await show_products(q)
    elif data == "stock":
        await q.edit_message_text(await stock_text(), reply_markup=back_home())
    elif data == "me":
        await q.edit_message_text(
            f"👤 ID: {uid}\nTên: {q.from_user.full_name}\nSố dư: {money(get_balance(uid))}",
            reply_markup=back_home()
        )
    elif data == "history":
        await customer_history(q)
    elif data == "recharge":
        await q.edit_message_text(
            "💳 NẠP TIỀN\nChuyển khoản theo thông tin do quản trị viên cung cấp.\n"
            "Nội dung chuyển khoản cần có Telegram ID của bạn: " + str(uid),
            reply_markup=back_home()
        )


# ================= COMMANDS / APP =================
async def help_command(update, context):
    text = (
        "LỆNH KHÁCH HÀNG\n/start - Menu\n\n"
        "LỆNH ADMIN\n/admin - Bảng quản trị\n"
        "/import <tên> - Nhập file TXT\n"
        "/themhang <tên> | <thông tin> - Thêm hàng\n"
        "/setgia <tên> <giá_bán> <giá_vốn> - Cấu hình giá\n"
        "/loi <ID_hàng> - Đánh dấu lỗi\n"
        "/khoiphuc <ID_hàng> - Khôi phục hàng lỗi\n"
        "/cong <UID> <tiền> [lý do] - Cộng số dư\n"
        "/tru <UID> <tiền> [lý do] - Trừ số dư\n"
        "/hoantien <mã_đơn> - Hoàn số dư đơn\n"
        "/backup - Sao lưu database"
    )
    await update.effective_message.reply_text(text)


async def error_handler(update, context):
    log.exception("Unhandled Telegram error", exc_info=context.error)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Thiếu BOT_TOKEN trong Environment Variables.")
    if not SEPAY_WEBHOOK_SECRET:
        log.warning("SEPAY_WEBHOOK_SECRET chưa cấu hình: webhook nạp tiền sẽ bị từ chối.")
    if not ADMIN_IDS:
        raise RuntimeError("Thiếu ADMIN_IDS, ví dụ 123456789.")
    init_db()

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("admin", admin_panel))
    telegram_app.add_handler(CommandHandler("import", admin_import))
    telegram_app.add_handler(CommandHandler("themhang", add_item_command))
    telegram_app.add_handler(CommandHandler("setgia", set_price))
    telegram_app.add_handler(CommandHandler("loi", mark_defect))
    telegram_app.add_handler(CommandHandler("khoiphuc", restore_item))
    telegram_app.add_handler(CommandHandler("cong", add_money_command))
    telegram_app.add_handler(CommandHandler("tru", deduct_money_command))
    telegram_app.add_handler(CommandHandler("hoantien", refund_order))
    telegram_app.add_handler(CommandHandler("backup", backup_command))
    telegram_app.add_handler(CallbackQueryHandler(callback_handler))
    telegram_app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_admin_input))
    telegram_app.add_error_handler(error_handler)

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)

    web_app = web.Application()
    web_app["bot"] = telegram_app.bot
    web_app.router.add_get("/", lambda r: web.json_response({"service":"SocialStock V5","status":"running"}))
    web_app.router.add_post("/sepay-webhook", sepay_webhook)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("SocialStock V5 running on port %s", PORT)

    try:
        await asyncio.Event().wait()
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
