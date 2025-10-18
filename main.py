# bot.py
import os
import io
import logging
from datetime import datetime
from urllib.parse import urlparse

import pandas as pd
import matplotlib.pyplot as plt
import psycopg2

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# -------------------------------------
# Configure environment & logging
# -------------------------------------
# Make sure matplotlib/fontconfig can write to /tmp (avoid ReadOnly FS errors)
os.environ['MPLCONFIGDIR'] = '/tmp'
os.environ['XDG_CACHE_HOME'] = '/tmp'

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------------------------
# Load configuration from env
# -------------------------------------
BOT_TOKEN = os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_CHAT_IDS_RAW = os.getenv('ADMIN_CHAT_IDS', '')
ADMIN_CHAT_IDS = [int(x) for x in ADMIN_CHAT_IDS_RAW.split(',') if x.strip()]

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set in environment")
    raise SystemExit("BOT_TOKEN required")
if not DATABASE_URL:
    logger.error("DATABASE_URL not set in environment")
    raise SystemExit("DATABASE_URL required")

# -------------------------------------
# Conversation states
# -------------------------------------
(
    STATE_MAIN_MENU,
    STATE_ASK_KM,
    STATE_ASK_LITER,
    STATE_AWAIT_CONFIRM,
    STATE_DATA_MENU,
    STATE_LOAD_CSV,
    STATE_DEL_ID
) = range(7)

# Keyboards
MAIN_MENU_KB = [['ثبت سوختگیری ⛽️'], ['📦 بکاپ سوختگیری', '📊 نمودار مصرف'], ['🗃️ مدیریت داده']]
DATA_MENU_KB = [['📥 وارد کردن داده'], ['🗑️ حذف داده'], ['بازگشت']]
CONFIRM_KB = [['✅ بله', '❌ خیر'], ['بازگشت']]
CANCEL_COMMANDS = ['بازگشت', '/menu', 'لغو']

# -------------------------------------
# Database helpers
# -------------------------------------
def get_connection():
    url = urlparse(DATABASE_URL)
    return psycopg2.connect(
        dbname=url.path[1:],
        user=url.username,
        password=url.password,
        host=url.hostname,
        port=url.port
    )

def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        '''CREATE TABLE IF NOT EXISTS fuel_logs (
           id SERIAL PRIMARY KEY,
           km REAL NOT NULL,
           liter REAL NOT NULL,
           timestamp TIMESTAMP NOT NULL
        )'''
    )
    conn.commit()
    conn.close()

def insert_log(km, liter):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO fuel_logs (km, liter, timestamp) VALUES (%s, %s, %s) RETURNING id',
        (km, liter, datetime.now())
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return new_id

def generate_csv_bytesio():
    conn = get_connection()
    df = pd.read_sql('SELECT * FROM fuel_logs ORDER BY id', conn)
    conn.close()
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    return io.BytesIO(buf.read().encode('utf-8'))

def generate_chart_bytesio():
    conn = get_connection()
    df = pd.read_sql('SELECT km, liter FROM fuel_logs ORDER BY id', conn)
    conn.close()
    if len(df) < 5:
        return None

    df.rename(columns={'km': 'Kilometer', 'liter': 'Liter'}, inplace=True)
    df['distance'] = df['Kilometer'].diff()
    # avoid zero or negative distances
    df = df[df['distance'] > 0].copy()
    if df.empty or (df['distance'] == 0).all():
        return None
    df['fuel_per_100km'] = (df['Liter'] / df['distance']) * 100
    df = df.dropna().copy()
    df['is_reliable'] = df['Liter'] >= 12

    reliable = df[df['is_reliable']].copy()
    noisy = df[~df['is_reliable']].copy()

    if reliable.empty:
        # fallback: use all
        reliable = df.copy()

    reliable['ma_small'] = reliable['fuel_per_100km'].rolling(window=min(5, len(reliable))).mean()
    reliable['ma_large'] = reliable['fuel_per_100km'].rolling(window=min(15, len(reliable))).mean()
    avg = reliable['fuel_per_100km'].mean()
    reliable['is_last'] = False
    reliable.loc[reliable.tail(5).index, 'is_last'] = True

    plt.figure(figsize=(12, 6))
    sc = plt.scatter(
        reliable['Kilometer'], reliable['fuel_per_100km'],
        s=reliable['Liter'] * 7, c=reliable['Liter'], cmap='Blues', alpha=0.8
    )
    if not noisy.empty:
        plt.scatter(
            noisy['Kilometer'], noisy['fuel_per_100km'],
            s=noisy['Liter'] * 7, c='red', marker='x', alpha=0.6
        )
    if 'ma_small' in reliable:
        plt.plot(reliable['Kilometer'], reliable['ma_small'], label='MA 5')
    if 'ma_large' in reliable:
        plt.plot(reliable['Kilometer'], reliable['ma_large'], label='MA 15')
    plt.axhline(avg, linestyle='--', label=f'Average: {avg:.1f}')
    for i, (_, row) in enumerate(reliable[reliable['is_last']].iterrows(), start=1):
        plt.text(row['Kilometer'], row['fuel_per_100km'], str(i), ha='center')
    plt.colorbar(sc, label='Volume Refueled [Liters]')
    plt.xlabel('Kilometer')
    plt.ylabel('Fuel Consumption [L/100km]')
    plt.title('Fuel Consumption Trend')
    plt.legend()
    plt.grid(alpha=0.3)
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

# -------------------------------------
# Utility
# -------------------------------------
def is_admin(chat_id: int) -> bool:
    if not ADMIN_CHAT_IDS:
        return False
    return chat_id in ADMIN_CHAT_IDS

# -------------------------------------
# Handlers
# -------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ensure DB initialized
    init_db()
    user = update.effective_chat.id
    keyboard = MAIN_MENU_KB
    await update.message.reply_text('به بات خوش آمدی! ⛽️', reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return STATE_MAIN_MENU

# Main menu text handler (catch menu selections)
async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    # Cancel commands
    if text in CANCEL_COMMANDS:
        await update.message.reply_text('بازگشت به منوی اصلی.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
        return STATE_MAIN_MENU

    # ثبت سوختگیری
    if text == 'ثبت سوختگیری ⛽️':
        await update.message.reply_text('لطفاً کیلومتر را وارد کنید:', reply_markup=ReplyKeyboardMarkup([[ 'بازگشت' ]], resize_keyboard=True))
        return STATE_ASK_KM

    # بکاپ
    if text == '📦 بکاپ سوختگیری':
        if ADMIN_CHAT_IDS and chat_id not in ADMIN_CHAT_IDS:
            await update.message.reply_text('⛔️ دسترسی ندارید.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
            return STATE_MAIN_MENU
        try:
            bio = generate_csv_bytesio()
            bio.seek(0)
            await context.bot.send_document(chat_id=chat_id, document=InputFile(bio, filename='fuel_backup.csv'), caption='📦 بکاپ داده‌ها')
        except Exception as e:
            logger.exception("Error sending backup")
            await update.message.reply_text(f'❌ خطا در تولید بکاپ: {e}', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
        return STATE_MAIN_MENU

    # نمودار
    if text == '📊 نمودار مصرف':
        if ADMIN_CHAT_IDS and chat_id not in ADMIN_CHAT_IDS:
            await update.message.reply_text('⛔️ دسترسی ندارید.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
            return STATE_MAIN_MENU
        try:
            chart = generate_chart_bytesio()
            if chart:
                chart.seek(0)
                await context.bot.send_photo(chat_id=chat_id, photo=chart, caption='📊 نمودار مصرف')
            else:
                await update.message.reply_text('❗️ داده کافی برای نمودار نیست.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
        except Exception as e:
            logger.exception("Error generating chart")
            await update.message.reply_text(f'❌ خطا در تولید نمودار: {e}', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
        return STATE_MAIN_MENU

    # مدیریت داده
    if text == '🗃️ مدیریت داده':
        await update.message.reply_text('مدیریت داده:', reply_markup=ReplyKeyboardMarkup(DATA_MENU_KB, resize_keyboard=True))
        return STATE_DATA_MENU

    # unknown
    await update.message.reply_text('دستور نامشخص.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
    return STATE_MAIN_MENU

# Ask km handler
async def ask_km_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    if text in CANCEL_COMMANDS:
        await update.message.reply_text('لغو شد.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
        return STATE_MAIN_MENU
    try:
        km = float(text)
        context.user_data['km'] = km
        await update.message.reply_text('لطفاً لیتر را وارد کنید:')
        return STATE_ASK_LITER
    except ValueError:
        await update.message.reply_text('⛔️ لطفاً عدد معتبر وارد کنید.')
        return STATE_ASK_KM

# Ask liter handler
async def ask_liter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in CANCEL_COMMANDS:
        await update.message.reply_text('لغو شد.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
        return STATE_MAIN_MENU
    try:
        liter = float(text)
        context.user_data['liter'] = liter
        km = context.user_data.get('km')
        summary = f"✅ کیلومتر: {km}\nلیتر: {liter}"
        await update.message.reply_text(summary + '\nآیا تأیید می‌کنید؟', reply_markup=ReplyKeyboardMarkup(CONFIRM_KB, resize_keyboard=True))
        return STATE_AWAIT_CONFIRM
    except ValueError:
        await update.message.reply_text('⛔️ لطفاً عدد معتبر وارد کنید.')
        return STATE_ASK_LITER

# Confirm handler
async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    if text == '✅ بله':
        km = context.user_data.get('km')
        liter = context.user_data.get('liter')
        try:
            new_id = insert_log(km, liter)
            await update.message.reply_text(f'✅ ثبت شد (ID: {new_id})', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
        except Exception as e:
            logger.exception("Error inserting log")
            await update.message.reply_text(f'❌ خطا در ثبت: {e}', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
    else:
        await update.message.reply_text('❌ عملیات لغو شد.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
    context.user_data.pop('km', None)
    context.user_data.pop('liter', None)
    return STATE_MAIN_MENU

# Data menu handler
async def data_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == 'بازگشت':
        await update.message.reply_text('بازگشت به منوی اصلی.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
        return STATE_MAIN_MENU
    if text == '📥 وارد کردن داده':
        await update.message.reply_text('لطفاً فایل CSV ارسال کنید.', reply_markup=ReplyKeyboardMarkup([[ 'بازگشت' ]], resize_keyboard=True))
        return STATE_LOAD_CSV
    if text == '🗑️ حذف داده':
        await update.message.reply_text('لطفاً ID رکورد را وارد کنید.', reply_markup=ReplyKeyboardMarkup([[ 'بازگشت' ]], resize_keyboard=True))
        return STATE_DEL_ID
    # default
    await update.message.reply_text('بازگشت به منوی اصلی.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
    return STATE_MAIN_MENU

# Load CSV handler (document upload)
async def load_csv_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = update.effective_chat.id

    # allow cancel/back
    if msg.text and msg.text.strip() in CANCEL_COMMANDS:
        await msg.reply_text('بازگشت به منوی مدیریت داده.', reply_markup=ReplyKeyboardMarkup(DATA_MENU_KB, resize_keyboard=True))
        return STATE_DATA_MENU

    doc = msg.document
    if not doc:
        await msg.reply_text('⛔️ لطفاً فایل CSV ارسال کنید.', reply_markup=ReplyKeyboardMarkup(DATA_MENU_KB, resize_keyboard=True))
        return STATE_LOAD_CSV

    # download file
    try:
        file = await doc.get_file()
        content = await file.download_as_bytearray()
        df = pd.read_csv(io.BytesIO(content))
    except Exception as e:
        logger.exception("Error reading uploaded CSV")
        await msg.reply_text(f'❌ خطا در خواندن CSV: {e}', reply_markup=ReplyKeyboardMarkup(DATA_MENU_KB, resize_keyboard=True))
        return STATE_LOAD_CSV

    # insert rows
    try:
        conn = get_connection()
        cur = conn.cursor()
        count = 0
        for _, row in df.iterrows():
            # Expect columns 'km' and 'liter' (case-insensitive)
            if 'km' in row.index and 'liter' in row.index:
                km_val = float(row['km'])
                liter_val = float(row['liter'])
                cur.execute('INSERT INTO fuel_logs (km, liter, timestamp) VALUES (%s, %s, %s)',
                            (km_val, liter_val, datetime.now()))
                count += 1
        conn.commit()
        conn.close()
        await msg.reply_text(f'✅ {count} رکورد اضافه شد.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
    except Exception as e:
        logger.exception("Error inserting CSV rows")
        await msg.reply_text(f'❌ خطا در وارد کردن داده‌ها: {e}', reply_markup=ReplyKeyboardMarkup(DATA_MENU_KB, resize_keyboard=True))
        return STATE_LOAD_CSV

    return STATE_MAIN_MENU

# Delete by id handler
async def del_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in CANCEL_COMMANDS:
        await update.message.reply_text('بازگشت به منوی مدیریت داده.', reply_markup=ReplyKeyboardMarkup(DATA_MENU_KB, resize_keyboard=True))
        return STATE_DATA_MENU
    try:
        rid = int(text)
    except ValueError:
        await update.message.reply_text('⛔️ لطفاً یک شناسه عددی وارد کنید.', reply_markup=ReplyKeyboardMarkup(DATA_MENU_KB, resize_keyboard=True))
        return STATE_DEL_ID
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute('DELETE FROM fuel_logs WHERE id = %s', (rid,))
        if cur.rowcount:
            await update.message.reply_text(f'✅ رکورد {rid} حذف شد.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
        else:
            await update.message.reply_text(f'⚠️ رکورد {rid} یافت نشد.', reply_markup=ReplyKeyboardMarkup(DATA_MENU_KB, resize_keyboard=True))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("Error deleting record")
        await update.message.reply_text(f'❌ خطا: {e}', reply_markup=ReplyKeyboardMarkup(DATA_MENU_KB, resize_keyboard=True))
        return STATE_DEL_ID
    return STATE_MAIN_MENU

# Fallback / unknown messages
async def unknown_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('دستور نامشخص. از منو استفاده کنید.', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
    return STATE_MAIN_MENU

# Command to show menu
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('منو:', reply_markup=ReplyKeyboardMarkup(MAIN_MENU_KB, resize_keyboard=True))
    return STATE_MAIN_MENU

# -------------------------------------
# Build application & conversation handler
# -------------------------------------
def build_application():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start), CommandHandler('menu', menu_command)],
        states={
            STATE_MAIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler)
            ],
            STATE_ASK_KM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_km_handler)
            ],
            STATE_ASK_LITER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_liter_handler)
            ],
            STATE_AWAIT_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_handler)
            ],
            STATE_DATA_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, data_menu_handler)
            ],
            STATE_LOAD_CSV: [
                MessageHandler(filters.Document.ALL, load_csv_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, load_csv_handler)
            ],
            STATE_DEL_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, del_id_handler)
            ],
        },
        fallbacks=[
            CommandHandler('start', start),
            CommandHandler('menu', menu_command),
            MessageHandler(filters.TEXT & filters.Regex('^(بازگشت|/menu|لغو)$'), menu_command)
        ],
        allow_reentry=True
    )

    app.add_handler(conv_handler)

    # Optional: handle unknown commands
    app.add_handler(MessageHandler(filters.COMMAND, unknown_handler))
    return app

# -------------------------------------
# Run
# -------------------------------------
if __name__ == '__main__':
    init_db()
    application = build_application()
    logger.info("Bot starting (python-telegram-bot)...")
    # Choose polling; for production you may prefer webhook
    application.run_polling()
