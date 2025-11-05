import os
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from dotenv import load_dotenv
import sqlite3
from datetime import datetime

# --- Load environment ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# --- Gemini sozlash ---
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# --- SQLite database ---
conn = sqlite3.connect("users.db", check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    last_reset TEXT,
    plan TEXT DEFAULT 'free'
)
""")
conn.commit()

# --- Tarif limitlari ---
LIMITS = {
    "free": {"input": 15000, "output": 5000},
    "pro": {"input": 50000, "output": 15000},
    "premium": {"input": 150000, "output": 50000}
}

ADMIN_ID = 7141093667  # bu yerga o'zingning Telegram ID’ingni yoz

# --- Foydalanuvchini olish yoki yaratish ---
def get_user(user_id):
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = cur.fetchone()
    if not user:
        cur.execute("INSERT INTO users (user_id, last_reset) VALUES (?, ?)", (user_id, str(datetime.now().date())))
        conn.commit()
        return (user_id, 0, 0, str(datetime.now().date()), "free")
    return user

# --- Limitlarni tekshirish ---
def check_limits(user_id, input_len, output_len):
    user = get_user(user_id)
    uid, in_used, out_used, last_reset, plan = user
    today = str(datetime.now().date())

    if last_reset != today:
        cur.execute("UPDATE users SET input_tokens=0, output_tokens=0, last_reset=? WHERE user_id=?", (today, user_id))
        conn.commit()
        in_used = out_used = 0

    limits = LIMITS[plan]
    if in_used + input_len > limits["input"]:
        return False, "📛 Kunning input limiti tugadi!"
    if out_used + output_len > limits["output"]:
        return False, "📛 Kunning output limiti tugadi!"
    return True, plan

def update_usage(user_id, input_len, output_len):
    cur.execute("UPDATE users SET input_tokens=input_tokens+?, output_tokens=output_tokens+? WHERE user_id=?", (input_len, output_len, user_id))
    conn.commit()

# --- Start ---
@dp.message_handler(commands=["start"])
async def start_cmd(msg: types.Message):
    get_user(msg.from_user.id)
    await msg.answer(
        "👋 Salom! Men Gemini AI yordamchiman.\n\n"
        "🧠 Matn yozing — men javob qaytaraman.\n"
        "💎 /upgrade — obuna rejalari haqida ma'lumot."
    )

# --- Upgrade ---
@dp.message_handler(commands=["upgrade"])
async def upgrade_cmd(msg: types.Message):
    await msg.answer("💳 Obuna rejalari:\n\n"
                     "⭐ Free – bepul (15k input / 5k output token)\n"
                     "💎 Pro – $7/oy\n"
                     "👑 Premium – $15/oy\n\n"
                     "To‘lov uchun admin bilan bog‘laning.")

# --- Admin komandasi ---
@dp.message_handler(commands=["setplan"])
async def setplan_cmd(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("Sizda ruxsat yo‘q 😅")
    try:
        _, user_id, plan = msg.text.split()
        cur.execute("UPDATE users SET plan=? WHERE user_id=?", (plan, int(user_id)))
        conn.commit()
        await msg.answer(f"✅ {user_id} foydalanuvchisi {plan.upper()} tarifiga o‘tkazildi.")
    except:
        await msg.answer("Foydalanish: /setplan user_id plan")

# --- Asosiy AI suhbat ---
@dp.message_handler()
async def chat(msg: types.Message):
    user_id = msg.from_user.id
    text = msg.text.strip()

    input_len = len(text.split())
    allowed, info = check_limits(user_id, input_len, 0)
    if not allowed:
        return await msg.answer(info)

    await msg.answer("⏳ Yozilmoqda...")

    try:
        response = model.generate_content(text)
        answer = response.text

        output_len = len(answer.split())
        update_usage(user_id, input_len, output_len)

        await msg.answer(answer)
    except Exception as e:
        await msg.answer("❌ Xatolik yuz berdi: " + str(e))

# --- Botni ishga tushirish ---
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
