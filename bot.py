import os
import re
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
    ContextTypes, filters
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# ✏️ اكتبي أسماء الشركات هنا فقط (اسم في كل سطر)
# الباسورد بيتولد تلقائي: اسم الشركة + 12
COMPANY_NAMES = [
    "شركة مستورة للمقاولات",
    "سعد فضي",
    "كير الدولية",
    "مكتب الشروق",
    "مؤسسة ماضي الخميس",
    "شركة الممرات",
    "الحمزي",
    "مجد الامة",
    "البابطين",
    "دقة الابعاد",
    "سعد العيسي",
    "الشركة الهندية",
    "المشاف العربي",
    "جال التنمية",
    "الخليفة للمقاولات",
    "المطور الذهبي",
    "تالين الخليج",
]
PASSWORD_SUFFIX = "12"
COMPANIES = {name: f"{name}{PASSWORD_SUFFIX}" for name in COMPANY_NAMES}

CURRENCY = "ريال"   # ✏️ غيريها لو العملة مختلفة

DB = "permits.db"
TZ = ZoneInfo("Africa/Cairo")

# مراحل الأدمن
COMPANY, PERMIT, HUB, FIELD, ATTACH, DATEIN = range(6)
FIND_COMPANY, FIND_PERMIT = range(10, 12)

BTN_TODAY = "📅 تصاريح اليوم"
BTN_MONTH = "📆 تصاريح الشهر"
BTN_LOGOUT = "🚪 خروج"
COMPANY_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_TODAY, BTN_MONTH], [BTN_LOGOUT]], resize_keyboard=True
)

# ------------------------------------------------------------
# ✏️ أنواع الاستلام ورسايلها — تقدري تعدلي الرسايل من هنا
# {name} = اسم الإجراء، والباقي أسماء الخانات
# ------------------------------------------------------------
DIM_FIELDS = [
    ("length", "الطول (متر)"),
    ("width", "العرض (سم)"),
    ("depth", "العمق (سم)"),
]
# صبغريد / صبيز / أسفلت: من غير أسئلة، بيتسجل "تم" بالتاريخ على طول
GENERIC_FIELDS = []
GENERIC_TEMPLATE = "✅ تم {name}"

KINDS = {
    "sand": {
        "label": "🏖 استلام رمل", "name": "استلام رمل",
        "fields": [
            ("thickness", "سمك الطبقة (سم)"),
            ("length", "الطول (متر)"),
            ("width", "العرض (سم)"),
            ("depth", "العمق من سطح طبقة الحماية (سم)"),
            ("total", "إجمالي العمق (متر)"),
        ],
        "template": (
            "تم استلام الموقع\n"
            "الطبقة الأولى (رمل)\n"
            "سمك الطبقة {thickness} سم\n"
            "الطول {length} متر\n"
            "العرض : {width} سم\n"
            "العمق من سطح طبقة الحماية {depth} سم.\n"
            "إجمالي العمق {total} متر"
        ),
    },
    "concrete": {
        "label": "🏗 استلام خرسانة", "name": "استلام خرسانة",
        "fields": DIM_FIELDS,
        "template": (
            "✅ تم استلام الموقع لأعمال صب الخرسانة\n"
            "وتم استلام عدد 6 مكعبات خرسانية لإجراء اختبارات مقاومة الضغط، "
            "مع إجراء اختبارات الهبوط (Slump) وقياس درجة حرارة الخرسانة\n"
            "أبعاد الحفرية:\n"
            "الطول: {length} متر\n"
            "العرض: {width} سم\n"
            "العمق: {depth} سم"
        ),
    },
    "subgrade": {
        "label": "🟤 استلام صبغريد", "name": "استلام صبغريد",
        "fields": GENERIC_FIELDS, "template": GENERIC_TEMPLATE,
    },
    "subbase": {
        "label": "⚪ استلام صبيز", "name": "استلام صبيز",
        "fields": GENERIC_FIELDS, "template": GENERIC_TEMPLATE,
    },
    "asphalt": {
        "label": "⚫ استلام أسفلت", "name": "استلام أسفلت",
        "fields": GENERIC_FIELDS, "template": GENERIC_TEMPLATE,
    },
    "final": {
        "label": "✅ استلام نهائي (سعر)", "name": "استلام نهائي",
        "fields": [("price", "💰 اكتبي سعر الاستلام النهائي (أرقام فقط)")],
        "template": "✅ تم الاستلام النهائي\n💰 السعر: {price_fmt} {currency}",
    },
    "note": {  # بيتسجل لما الأدمن يفوّرد أو يكتب رسالة حرة
        "label": "📝 ملاحظة", "name": "ملاحظة", "hidden": True,
        "fields": [("text", "اكتبي نص الملاحظة")],
        "template": "{text}",
    },
}
BUTTON_ORDER = ["sand", "concrete", "subgrade", "subbase", "asphalt"]


# ---------------- قاعدة البيانات ----------------
def database():
    with sqlite3.connect(DB) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT, permit TEXT, kind TEXT,
            data TEXT, message TEXT, price REAL,
            created TEXT
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id INTEGER, file_id TEXT, file_type TEXT
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            company TEXT, chat_id INTEGER,
            UNIQUE(company, chat_id)
        )""")


def query(sql, params=()):
    with sqlite3.connect(DB) as con:
        con.row_factory = sqlite3.Row
        return con.execute(sql, params).fetchall()


def execute(sql, params=()):
    with sqlite3.connect(DB) as con:
        return con.execute(sql, params).lastrowid


def now_str():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


def add_action(company, permit, kind, data, message, price=None, created=None):
    return execute(
        "INSERT INTO actions (company, permit, kind, data, message, price, created) "
        "VALUES (?,?,?,?,?,?,?)",
        (company, permit, kind, json.dumps(data, ensure_ascii=False),
         message, price, created or now_str()))


def update_action(action_id, data, message, price=None):
    execute("UPDATE actions SET data=?, message=?, price=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), message, price, action_id))


def delete_action(action_id):
    execute("DELETE FROM files WHERE action_id=?", (action_id,))
    execute("DELETE FROM actions WHERE id=?", (action_id,))


def get_action(action_id):
    rows = query("SELECT * FROM actions WHERE id=?", (action_id,))
    return rows[0] if rows else None


def list_actions(company, permit):
    return query("SELECT * FROM actions WHERE company=? AND permit=? ORDER BY id",
                 (company, permit))


def actions_by_prefix(prefix, company=None):
    sql = "SELECT * FROM actions WHERE created LIKE ?"
    params = [f"{prefix}%"]
    if company:
        sql += " AND company=?"
        params.append(company)
    sql += " ORDER BY company, permit, id"
    return query(sql, params)


def add_file(action_id, file_id, file_type):
    execute("INSERT INTO files (action_id, file_id, file_type) VALUES (?,?,?)",
            (action_id, file_id, file_type))


def get_files(action_id):
    return query("SELECT * FROM files WHERE action_id=? ORDER BY id", (action_id,))


def add_sub(company, chat_id):
    execute("INSERT OR IGNORE INTO subscribers (company, chat_id) VALUES (?,?)",
            (company, chat_id))


def remove_sub(chat_id):
    execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))


def get_subs(company):
    return [r["chat_id"] for r in
            query("SELECT chat_id FROM subscribers WHERE company=?", (company,))]


# ---------------- أدوات مساعدة ----------------
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def ar2en(text):
    return (text or "").translate(AR_DIGITS).strip()


def is_admin(user_id):
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def this_month():
    return datetime.now(TZ).strftime("%Y-%m")


def parse_date(text):
    text = ar2en(text).replace(".", "/")
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_month(text):
    """يقبل 2026-11 أو 11/2026 ويرجع YYYY-MM."""
    text = ar2en(text)
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", text)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
    else:
        m = re.fullmatch(r"(\d{1,2})[-/](\d{4})", text)
        if not m:
            return None
        mo, y = int(m.group(1)), int(m.group(2))
    return f"{y:04d}-{mo:02d}" if 1 <= mo <= 12 else None


def parse_number(text):
    text = ar2en(text).replace(",", "").replace("،", "")
    try:
        return float(text)
    except ValueError:
        return None


def fmt_money(x):
    x = x or 0
    return f"{int(x):,}" if float(x).is_integer() else f"{x:,.2f}"


def fmt_created(created):
    """التاريخ المسجل بأثر رجعي (00:00) يظهر بدون ساعة."""
    return created[:-6] if (created or "").endswith(" 00:00") else created


def current_created(ud):
    d = ud.get("add_date")
    return f"{d} 00:00" if d else now_str()


TODAY_WORDS = ("اليوم", "النهاردة", "النهارده", "انهارده")
YESTERDAY_WORDS = ("امس", "أمس", "إمبارح", "امبارح")


def yesterday():
    return (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")


def date_label(created):
    d = (created or "")[:10]
    if d == today():
        return f"{d} (النهاردة)"
    if d == yesterday():
        return f"{d} (أمس)"
    return d


def kind_name(kind):
    return KINDS.get(kind, {}).get("name", kind)


async def send_long(msg, text):
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            await msg.reply_text(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await msg.reply_text(chunk)


async def send_action(msg, a, show_company=False):
    header = f"📅 {fmt_created(a['created'])}\n"
    if show_company:
        header += f"🏢 {a['company']}\n"
    header += f"🔖 تصريح/حي: {a['permit']}\n🏷 {kind_name(a['kind'])}\n\n"
    await msg.reply_text((header + (a["message"] or ""))[:4000])

    caption = f"📎 {kind_name(a['kind'])} — تصريح {a['permit']}"
    for f in get_files(a["id"]):
        if f["file_type"] == "photo":
            await msg.reply_photo(f["file_id"], caption=caption)
        elif f["file_type"] == "video":
            await msg.reply_video(f["file_id"], caption=caption)
        else:
            await msg.reply_document(f["file_id"], caption=caption)


async def send_permit(msg, company, permit):
    rows = list_actions(company, permit)
    if not rows:
        like = query("SELECT DISTINCT permit FROM actions WHERE company=? AND permit LIKE ?",
                     (company, f"%{permit}%"))
        names = [r["permit"] for r in like]
        if len(names) == 1:
            permit = names[0]
            rows = list_actions(company, permit)
        elif len(names) > 1:
            await msg.reply_text(
                "لقيت أكتر من نتيجة، اكتبي الرقم/الاسم بالظبط:\n"
                + "\n".join(f"• {n}" for n in names[:30]))
            return
        else:
            await msg.reply_text("لا توجد بيانات مسجلة لهذا التصريح/الحي.")
            return
    summary = "\n".join(
        f"{i}. {kind_name(a['kind'])} — {fmt_created(a['created'])}"
        for i, a in enumerate(rows, 1))
    await msg.reply_text(
        f"سجل: {permit}\nالشركة: {company}\n"
        f"عدد الإجراءات: {len(rows)}\n\n{summary}"
    )
    for a in rows:
        await send_action(msg, a)


async def send_report(msg, prefix, title, company=None, details=False, kind=None):
    rows = actions_by_prefix(prefix, company)
    if kind:
        keys = {(r["company"], r["permit"]) for r in rows if r["kind"] == kind}
        rows = [r for r in rows if (r["company"], r["permit"]) in keys]
        title = f"{title} — {kind_name(kind)}"
    if not rows:
        await msg.reply_text(f"لا توجد تصاريح مسجلة في {title}.")
        return

    groups = {}
    by_kind = {}
    for r in rows:
        g = groups.setdefault(r["company"], {}).setdefault(
            r["permit"], {"kinds": [], "price": 0.0, "final": False})
        name = kind_name(r["kind"])
        if name not in g["kinds"]:
            g["kinds"].append(name)
        by_kind.setdefault(r["kind"], set()).add((r["company"], r["permit"]))
        if r["kind"] == "final":
            g["final"] = True
            g["price"] += r["price"] or 0

    lines = [f"📋 تقرير {title}"]
    total_permits = 0
    total_amount = 0.0
    for comp, permits in groups.items():
        comp_amount = sum(p["price"] for p in permits.values())
        if company is None:
            lines.append(f"\n🏢 {comp} — {len(permits)} تصريح — "
                         f"{fmt_money(comp_amount)} {CURRENCY}")
        else:
            lines.append("")
        for permit, g in permits.items():
            price = (f"💰 {fmt_money(g['price'])} {CURRENCY}"
                     if g["final"] else "بدون استلام نهائي")
            lines.append(f"• {permit}: {'، '.join(g['kinds'])} — {price}")
        total_permits += len(permits)
        total_amount += comp_amount

    lines.append(f"\n📌 إجمالي عدد التصاريح: {total_permits}")
    lines.append(f"💰 إجمالي المبلغ: {fmt_money(total_amount)} {CURRENCY}")

    if kind is None:
        lines.append("\n📊 حسب النوع (عدد التصاريح):")
        for k in BUTTON_ORDER + ["final", "note"]:
            if k in by_kind:
                lines.append(f"• {kind_name(k)}: {len(by_kind[k])}")

    await send_long(msg, "\n".join(lines))

    if details:
        for r in rows:
            if kind is None or r["kind"] == kind:
                await send_action(msg, r, show_company=(company is None))


async def ask_report_filter(msg, prefix, title):
    btns = [InlineKeyboardButton(KINDS[k]["label"], callback_data=f"rep|{prefix}|{k}")
            for k in BUTTON_ORDER]
    rows = [[InlineKeyboardButton("📋 الكل", callback_data=f"rep|{prefix}|all")]]
    rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]
    await msg.reply_text(f"📋 {title}\nاختاري التصنيف:",
                         reply_markup=InlineKeyboardMarkup(rows))


async def report_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, prefix, kind = q.data.split("|", 2)
    if is_admin(q.from_user.id):
        company = None
    else:
        company = context.user_data.get("logged_company")
        if not company:
            await q.message.reply_text("اكتبي /start لتسجيل الدخول أولاً.")
            return
    is_day = len(prefix) == 10
    title = f"يوم {prefix}" if is_day else f"شهر {prefix}"
    await send_report(q.message, prefix, title, company, details=is_day,
                      kind=None if kind == "all" else kind)


async def notify_company(context, company, text):
    for chat_id in get_subs(company):
        try:
            await context.bot.send_message(chat_id, text[:4000])
        except Exception:
            pass


# ---------------- دخول الشركات ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    remove_sub(update.effective_chat.id)

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"company|{name}")]
        for name in COMPANIES
    ]
    await update.message.reply_text(
        "مرحباً بك في نظام متابعة التصاريح.\n\nاختاري الشركة:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def choose_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_ = update.callback_query
    await query_.answer()

    company = query_.data.split("|", 1)[1]
    context.user_data["pending_company"] = company
    context.user_data.pop("logged_company", None)

    await query_.message.reply_text(
        f"الشركة المختارة: {company}\n\nاكتبي كلمة المرور:"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = update.message
    text = (msg.text or "").strip()

    if is_admin(user_id):
        await msg.reply_text(
            "أنتِ مسجلة كمسؤولة.\n\n"
            "/add ← إضافة/تعديل إجراءات تصريح\n"
            "/find ← بحث عن تصريح\n"
            "/today ← تصاريح اليوم (كل الشركات)\n"
            "/date 2026-11-08 ← تصاريح يوم معين\n"
            "/month ← تصاريح الشهر بالأسعار والإجمالي\n"
            "/month 2026-10 ← شهر معين\n"
            "/backup ← نسخة احتياطية من كل البيانات"
        )
        return

    pending = context.user_data.get("pending_company")
    if pending:
        if ar2en(text) == COMPANIES[pending]:
            context.user_data["logged_company"] = pending
            context.user_data.pop("pending_company", None)
            add_sub(pending, update.effective_chat.id)
            await msg.reply_text(
                f"تم تسجيل الدخول إلى {pending} بنجاح ✅\n\n"
                "• اكتبي رقم التصريح للبحث عنه\n"
                "• أو تاريخ (مثال: 2026-11-08) لتصاريح يوم معين\n"
                "• أو شهر (مثال: 2026-11) لتقرير الشهر\n"
                "• هيجيلك إشعار لما يتضاف تحديث على تصاريحك 🔔",
                reply_markup=COMPANY_KEYBOARD
            )
        else:
            await msg.reply_text(
                "كلمة المرور غير صحيحة.\nاكتبي /start للمحاولة مرة أخرى."
            )
            context.user_data.pop("pending_company", None)
        return

    company = context.user_data.get("logged_company")
    if not company:
        await msg.reply_text("اكتبي /start لاختيار الشركة أولاً.")
        return

    if text == BTN_LOGOUT:
        remove_sub(update.effective_chat.id)
        context.user_data.clear()
        await msg.reply_text("تم تسجيل الخروج. اكتبي /start للدخول من جديد.",
                             reply_markup=ReplyKeyboardRemove())
        return

    if text == BTN_TODAY:
        await ask_report_filter(msg, today(), f"يوم {today()}")
        return

    if text == BTN_MONTH:
        await ask_report_filter(msg, this_month(), f"شهر {this_month()}")
        return

    month = parse_month(text)
    if month:
        await ask_report_filter(msg, month, f"شهر {month}")
        return

    day = parse_date(text)
    if day:
        await ask_report_filter(msg, day, f"يوم {day}")
        return

    await send_permit(msg, company, ar2en(text))


# ---------------- التقارير (أدمن + شركة) ----------------
async def _who(update, context):
    """يرجع (True, None) للأدمن، أو (False, company) للشركة، أو None لو مش مسجل."""
    if is_admin(update.effective_user.id):
        return True, None
    company = context.user_data.get("logged_company")
    if not company:
        await update.message.reply_text("اكتبي /start لتسجيل الدخول أولاً.")
        return None
    return False, company


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    who = await _who(update, context)
    if who:
        await ask_report_filter(update.message, today(), f"يوم {today()}")


async def date_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    who = await _who(update, context)
    if not who:
        return
    if not context.args:
        await update.message.reply_text("اكتبي التاريخ بعد الأمر، مثال:\n/date 2026-11-08")
        return
    day = parse_date(context.args[0])
    if not day:
        await update.message.reply_text("صيغة التاريخ غير صحيحة. مثال: 2026-11-08")
        return
    await ask_report_filter(update.message, day, f"يوم {day}")


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    who = await _who(update, context)
    if not who:
        return
    month = this_month()
    if context.args:
        month = parse_month(context.args[0])
        if not month:
            await update.message.reply_text("صيغة الشهر غير صحيحة. مثال: /month 2026-11")
            return
    await ask_report_filter(update.message, month, f"شهر {month}")


# ---------------- بحث الأدمن ----------------
async def find_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("هذا الأمر للمسؤول فقط.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(n, callback_data=f"find|{n}")] for n in COMPANIES]
    await update.message.reply_text("اختاري الشركة للبحث:",
                                    reply_markup=InlineKeyboardMarkup(keyboard))
    return FIND_COMPANY


async def find_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    company = q.data.split("|", 1)[1]
    context.user_data["find_company"] = company
    await q.message.reply_text(f"الشركة: {company}\n\nاكتبي رقم التصريح (أو اسم الحي لو الشركة معندهاش أرقام تصاريح):")
    return FIND_PERMIT


async def find_permit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_permit(update.message, context.user_data["find_company"],
                      ar2en(update.message.text))
    return ConversationHandler.END


# ---------------- إدارة التصريح (الأدمن) ----------------
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("هذا الأمر للمسؤول فقط.")
        return ConversationHandler.END
    context.user_data.pop("add_date", None)
    keyboard = [[InlineKeyboardButton(n, callback_data=f"add|{n}")] for n in COMPANIES]
    await update.message.reply_text("اختاري الشركة:",
                                    reply_markup=InlineKeyboardMarkup(keyboard))
    return COMPANY


async def add_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    company = q.data.split("|", 1)[1]
    context.user_data["add_company"] = company
    await q.message.reply_text(f"الشركة: {company}\n\nاكتبي رقم التصريح (أو اسم الحي لو الشركة معندهاش أرقام تصاريح):")
    return PERMIT


async def add_permit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add_permit"] = ar2en(update.message.text)
    await show_hub(update.message, context)
    return HUB


async def show_hub(target, context, prefix=""):
    ud = context.user_data
    company, permit = ud["add_company"], ud["add_permit"]
    actions = list_actions(company, permit)

    text = (prefix + "\n\n" if prefix else "")
    text += f"🔖 تصريح/حي: {permit}\n🏢 {company}\n"
    d = ud.get("add_date")
    if d:
        text += f"📅 الإجراء الجاي هيتسجل بتاريخ: {date_label(d)} (مرة واحدة بس)\n\n"
    else:
        text += "📅 التسجيل بتاريخ: النهاردة\n\n"
    if actions:
        text += "الإجراءات المسجلة:\n"
        for i, a in enumerate(actions, 1):
            extra = (f" — {fmt_money(a['price'])} {CURRENCY}"
                     if a["kind"] == "final" else "")
            text += f"{i}. {kind_name(a['kind'])}{extra}\n"
    else:
        text += "لا توجد إجراءات بعد.\n"
    text += "\nاختاري إجراء، أو ابعتي/فوردي أي رسالة أو صورة وتتسجل كملاحظة."

    btns = [InlineKeyboardButton(KINDS[k]["label"], callback_data=f"hub|{k}")
            for k in BUTTON_ORDER]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(KINDS["final"]["label"], callback_data="hub|final")])
    rows.append([
        InlineKeyboardButton("📎 إضافة صور/ملفات", callback_data="hub|attachpick"),
        InlineKeyboardButton("✏️ تعديل / 🗑 حذف", callback_data="hub|managepick"),
    ])
    rows.append([InlineKeyboardButton("📝 كتابة ملاحظة", callback_data="hub|note")])
    rows.append([InlineKeyboardButton("🕓 تسجيل إجراء جديد بتاريخ قديم", callback_data="hub|setdate")])
    rows.append([InlineKeyboardButton("📅 تعديل تاريخ إجراء متسجل", callback_data="hub|datepick")])
    rows.append([InlineKeyboardButton("✅ تم", callback_data="hub|finish")])

    await target.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def hub_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    choice = q.data.split("|", 1)[1]
    ud = context.user_data

    if choice == "finish":
        await q.message.reply_text("تم الانتهاء ✅\nلإضافة تصريح آخر اكتبي /add")
        return ConversationHandler.END

    if choice == "menu":
        await show_hub(q.message, context)
        return HUB

    if choice == "setdate":
        ud["date_target"] = None
        await q.message.reply_text(
            "اكتبي تاريخ الإجراء الجديد اللي هتسجليه (مثال: 2026-11-05 أو 05/11/2026) "
            "أو اكتبي: أمس\n\n"
            "التاريخ ده هيتطبق على الإجراء الجاي بس، وبعده بيرجع لتاريخ النهاردة تلقائي.\n"
            "(لو عايزة تغيري تاريخ إجراء متسجل قبل كده، ارجعي واختاري «تعديل تاريخ إجراء متسجل».)")
        return DATEIN

    if choice in ("attachpick", "managepick", "datepick"):
        actions = list_actions(ud["add_company"], ud["add_permit"])
        if not actions:
            await show_hub(q.message, context, "لا توجد إجراءات على التصريح ده لسه.")
            return HUB
        prefix = {"attachpick": "attach", "managepick": "act", "datepick": "dt"}[choice]
        rows = [[InlineKeyboardButton(f"{i}. {kind_name(a['kind'])}",
                                      callback_data=f"{prefix}|{a['id']}")]
                for i, a in enumerate(actions, 1)]
        rows.append([InlineKeyboardButton("↩️ رجوع", callback_data="hub|menu")])
        title = {
            "attachpick": "اختاري الإجراء اللي هتضيفي له صور/ملفات:",
            "managepick": "اختاري الإجراء:",
            "datepick": "اختاري الإجراء اللي عايزة تغيري تاريخه:",
        }[choice]
        await q.message.reply_text(title, reply_markup=InlineKeyboardMarkup(rows))
        return HUB

    if choice in KINDS:
        if not KINDS[choice]["fields"]:          # صبغريد / صبيز / أسفلت: تم على طول
            await finalize_action(q.message, context, choice, {})
            return HUB
        await start_fields(q.message, context, choice)
        return FIELD
    return HUB


async def start_fields(msg, context, kind, edit_id=None):
    ud = context.user_data
    ud["kind"], ud["data"], ud["idx"], ud["edit_id"] = kind, {}, 0, edit_id
    await msg.reply_text(KINDS[kind]["fields"][0][1] + ":")


async def field_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    ud = context.user_data
    kind = ud["kind"]
    fields = KINDS[kind]["fields"]
    key, _ = fields[ud["idx"]]
    value = ar2en(msg.text)

    if key == "price":
        num = parse_number(value)
        if num is None or num < 0:
            await msg.reply_text("اكتبي السعر أرقام فقط، مثال: 2500")
            return FIELD
        ud["data"]["price"] = num
    else:
        ud["data"][key] = value

    ud["idx"] += 1
    if ud["idx"] < len(fields):
        await msg.reply_text(fields[ud["idx"]][1] + ":")
        return FIELD

    # خلصنا الخانات
    edit_id = ud.get("edit_id")
    ud["edit_id"] = None
    await finalize_action(msg, context, kind, ud["data"], edit_id)
    return HUB


async def finalize_action(msg, context, kind, data, edit_id=None):
    ud = context.user_data
    price = data.get("price")
    if price is not None:
        data["price_fmt"] = fmt_money(price)
        data["currency"] = CURRENCY
    message = KINDS[kind]["template"].format(name=kind_name(kind), **data)

    if edit_id:
        update_action(edit_id, data, message, price)
        await show_hub(msg, context, f"✏️ تم تعديل {kind_name(kind)}:\n\n{message}")
        return

    company, permit = ud["add_company"], ud["add_permit"]
    created = current_created(ud)
    backdated = bool(ud.pop("add_date", None))   # التاريخ القديم بيتطبق مرة واحدة بس
    action_id = add_action(company, permit, kind, data, message, price, created=created)
    await notify_company(
        context, company,
        f"🔔 تحديث جديد على {permit}\n🏷 {kind_name(kind)}\n\n{message}")

    text = f"✅ تم {kind_name(kind)}\n📅 اتسجل بتاريخ {date_label(created)}"
    if KINDS[kind]["fields"]:
        text += f"\n\n{message}"
    if backdated:
        text += "\n\n↩️ الإجراء الجاي هيتسجل بتاريخ النهاردة تلقائي."
    await msg.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📎 إضافة صور/ملفات", callback_data=f"attach|{action_id}"),
            InlineKeyboardButton("↩️ القائمة", callback_data="hub|menu"),
        ]])
    )


async def hub_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أي رسالة/صورة/ملف يتبعت وهو في قايمة التصريح يتسجل كملاحظة."""
    msg = update.message
    ud = context.user_data
    text = msg.caption or msg.text or ""
    file_id = file_type = None
    if msg.photo:
        file_id, file_type = msg.photo[-1].file_id, "photo"
    elif msg.document:
        file_id, file_type = msg.document.file_id, "document"
    elif msg.video:
        file_id, file_type = msg.video.file_id, "video"

    if not text and not file_id:
        return HUB

    company, permit = ud["add_company"], ud["add_permit"]
    action_id = add_action(company, permit, "note", {"text": text}, text,
                           created=current_created(ud))
    ud.pop("add_date", None)
    if file_id:
        add_file(action_id, file_id, file_type)
    await notify_company(
        context, company, f"🔔 تحديث جديد على التصريح {permit}\n\n{text}"[:4000])
    await msg.reply_text(
        "✅ اتسجلت كملاحظة.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ القائمة", callback_data="hub|menu")]])
    )
    return HUB


async def attach_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action_id = int(q.data.split("|", 1)[1])
    a = get_action(action_id)
    if not a:
        await show_hub(q.message, context, "الإجراء ده مش موجود.")
        return HUB
    context.user_data["attach_id"] = action_id
    await q.message.reply_text(
        f"ابعتي الصور أو PDF أو الفيديوهات لـ {kind_name(a['kind'])} "
        "(واحد أو أكتر)، ولما تخلصي اضغطي تم.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ تم", callback_data="attachdone")]])
    )
    return ATTACH


async def attach_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo:
        add_file(context.user_data["attach_id"], msg.photo[-1].file_id, "photo")
    elif msg.video:
        add_file(context.user_data["attach_id"], msg.video.file_id, "video")
    elif msg.document:
        add_file(context.user_data["attach_id"], msg.document.file_id, "document")
    else:
        return ATTACH
    await msg.reply_text("📎 اتحفظ. ابعتي كمان أو اضغطي تم.")
    return ATTACH


async def attach_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await show_hub(q.message, context, "✅ تم حفظ الملفات.")
    return HUB


async def act_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    a = get_action(int(q.data.split("|", 1)[1]))
    if not a:
        await show_hub(q.message, context, "الإجراء ده مش موجود.")
        return HUB
    n_files = len(get_files(a["id"]))
    first = []
    if KINDS.get(a["kind"], {}).get("fields"):
        first.append(InlineKeyboardButton("✏️ تعديل البيانات", callback_data=f"edit|{a['id']}"))
    first.append(InlineKeyboardButton("🗑 حذف", callback_data=f"del|{a['id']}"))
    await q.message.reply_text(
        f"🏷 {kind_name(a['kind'])}\n📅 {fmt_created(a['created'])}\n📎 ملفات: {n_files}\n\n{a['message']}",
        reply_markup=InlineKeyboardMarkup([
            first,
            [InlineKeyboardButton("📅 تغيير تاريخ الإجراء", callback_data=f"dt|{a['id']}")],
            [InlineKeyboardButton("↩️ رجوع", callback_data="hub|menu")],
        ])
    )
    return HUB


async def edit_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    a = get_action(int(q.data.split("|", 1)[1]))
    if not a or a["kind"] not in KINDS:
        await show_hub(q.message, context, "الإجراء ده مش موجود.")
        return HUB
    if not KINDS[a["kind"]]["fields"]:
        await show_hub(q.message, context,
                       "الإجراء ده مفيهوش بيانات تتعدل. تقدري تغيري تاريخه أو تحذفيه.")
        return HUB
    await q.message.reply_text("هنعدل البيانات من الأول 👇")
    await start_fields(q.message, context, a["kind"], edit_id=a["id"])
    return FIELD


async def del_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action_id = int(q.data.split("|", 1)[1])
    await q.message.reply_text(
        "متأكدة إنك عايزة تمسحي الإجراء ده؟ مش هينفع ترجعيه.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 أيوه امسحي", callback_data=f"delok|{action_id}"),
            InlineKeyboardButton("↩️ لأ", callback_data="hub|menu"),
        ]])
    )
    return HUB


async def del_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    delete_action(int(q.data.split("|", 1)[1]))
    await show_hub(q.message, context, "🗑 تم الحذف.")
    return HUB


async def date_target_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["date_target"] = int(q.data.split("|", 1)[1])
    await q.message.reply_text("اكتبي التاريخ الجديد للإجراء ده (مثال: 2026-11-05):")
    return DATEIN


async def date_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    ud = context.user_data
    text = ar2en(msg.text)
    target = ud.get("date_target")

    if text in TODAY_WORDS and target is None:
        ud.pop("add_date", None)
        await show_hub(msg, context, "✅ هيتسجل بتاريخ النهاردة.")
        return HUB

    if text in TODAY_WORDS:
        day = today()
    elif text in YESTERDAY_WORDS:
        day = yesterday()
    else:
        day = parse_date(text)
    if not day:
        await msg.reply_text("صيغة التاريخ غير صحيحة. مثال: 2026-11-05 أو اكتبي: أمس")
        return DATEIN

    if target:
        execute("UPDATE actions SET created=? WHERE id=?", (f"{day} 00:00", target))
        ud["date_target"] = None
        await show_hub(msg, context, f"✅ اتغير تاريخ الإجراء لـ {day}.")
    else:
        ud["add_date"] = day
        await show_hub(msg, context, f"✅ الإجراء الجاي بس هيتسجل بتاريخ {day}، وبعده يرجع لتاريخ النهاردة تلقائي.")
    return HUB


async def finish_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم الانتهاء ✅")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم إلغاء العملية.")
    return ConversationHandler.END


# ---------------- النسخ الاحتياطي ----------------
def make_backup_file():
    path = f"backup_{datetime.now(TZ).strftime('%Y-%m-%d_%H%M')}.db"
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)          # نسخة سليمة حتى لو البوت شغال
    finally:
        dst.close()
        src.close()
    return path


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("هذا الأمر للمسؤول فقط.")
        return
    path = make_backup_file()
    try:
        with open(path, "rb") as f:
            await update.message.reply_document(
                f, filename=path,
                caption="📦 نسخة احتياطية من كل التصاريح. احتفظي بيها في مكان آمن.\n"
                        "للاسترجاع: ابعتي الملف ده للبوت واكتبي في التعليق /restore")
    finally:
        os.remove(path)


async def auto_backup(context: ContextTypes.DEFAULT_TYPE):
    """نسخة احتياطية يومية بتتبعت للأدمن على تليجرام."""
    path = make_backup_file()
    try:
        with open(path, "rb") as f:
            await context.bot.send_document(
                ADMIN_ID, f, filename=path, caption="📦 النسخة الاحتياطية اليومية")
    finally:
        os.remove(path)


def restore_from_path(tmp):
    src = sqlite3.connect(tmp)
    try:
        src.execute("SELECT COUNT(*) FROM actions").fetchone()   # يتأكد إنه ملف بتاعنا
        if os.path.exists(DB):
            shutil.copy(DB, "before_restore.db")                 # نسخة أمان قبل الاستبدال
        dst = sqlite3.connect(DB)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    database()


async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not is_admin(update.effective_user.id):
        await msg.reply_text("هذا الأمر للمسؤول فقط.")
        return
    tmp = "restore_tmp.db"
    try:
        tg_file = await msg.document.get_file()
        await tg_file.download_to_drive(tmp)
        restore_from_path(tmp)
        await msg.reply_text("✅ تم استرجاع النسخة الاحتياطية بنجاح.")
    except Exception:
        await msg.reply_text("❌ الملف ده مش نسخة احتياطية صحيحة، ومفيش حاجة اتغيرت.")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main():
    if not TOKEN or ADMIN_ID == 0:
        print("راجعي ملف .env وضعي BOT_TOKEN و ADMIN_ID")
        return

    database()
    app = Application.builder().token(TOKEN).build()

    media = (filters.PHOTO | filters.Document.ALL | filters.VIDEO)

    add_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            COMPANY: [CallbackQueryHandler(add_company, pattern=r"^add\|")],
            PERMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_permit)],
            HUB: [
                CallbackQueryHandler(hub_choice, pattern=r"^hub\|"),
                CallbackQueryHandler(attach_begin, pattern=r"^attach\|"),
                CallbackQueryHandler(act_view, pattern=r"^act\|"),
                CallbackQueryHandler(edit_begin, pattern=r"^edit\|"),
                CallbackQueryHandler(del_ask, pattern=r"^del\|"),
                CallbackQueryHandler(del_ok, pattern=r"^delok\|"),
                CallbackQueryHandler(date_target_begin, pattern=r"^dt\|"),
                MessageHandler((filters.TEXT & ~filters.COMMAND) | media, hub_forward),
            ],
            FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, field_input)],
            DATEIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, date_input)],
            ATTACH: [
                MessageHandler(media, attach_file),
                CallbackQueryHandler(attach_done, pattern=r"^attachdone$"),
            ],
        },
        fallbacks=[
            CommandHandler("done", finish_cmd),
            CommandHandler("cancel", cancel),
        ],
    )

    find_handler = ConversationHandler(
        entry_points=[CommandHandler("find", find_start)],
        states={
            FIND_COMPANY: [CallbackQueryHandler(find_company, pattern=r"^find\|")],
            FIND_PERMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, find_permit)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # الاسترجاع لازم يتسجل الأول عشان ماتتاخدش كملاحظة
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.CaptionRegex(r"^/restore"), restore_cmd))
    app.add_handler(add_handler)
    app.add_handler(find_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("date", date_cmd))
    app.add_handler(CommandHandler("month", month_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CallbackQueryHandler(choose_company, pattern=r"^company\|"))
    app.add_handler(CallbackQueryHandler(report_cb, pattern=r"^rep\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if app.job_queue:
        app.job_queue.run_daily(auto_backup, time=dtime(23, 0, tzinfo=TZ))
    else:
        print('تنبيه: النسخة اليومية التلقائية محتاجة: pip install "python-telegram-bot[job-queue]"')

    print("Asas Permit Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()