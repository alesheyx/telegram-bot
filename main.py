#!/usr/bin/env python3
"""
Telegram bot using aiogram that connects to Google Gemini (Generative Language API).
Features:
- User registration and daily token limits (input/output tokens).
- Subscription plans: free, pro, premium.
- Admin command /setplan to change a user's plan.
- SQLite database storing user data (id, tokens_remaining, last_reset, plan).
- Environment variables: BOT_TOKEN, GEMINI_API_KEY, optional GEMINI_MODEL, ADMIN_IDS.
- Uses aiohttp for async HTTP requests to the Gemini API.
- Error handling and clear responses to users.

Requirements:
- Python 3.8+
- aiogram (v2.x)
- aiohttp
- aiosqlite (optional, but we use sqlite3 synchronous module carefully inside async functions)
Install:
    pip install aiogram aiohttp
Run:
    export BOT_TOKEN="your-telegram-bot-token"
    export GEMINI_API_KEY="your-google-api-key"
    (optional) export GEMINI_MODEL="models/gemini-1.0"
    (optional) export ADMIN_IDS="123456789,987654321"
    python main.py
"""

import os
import asyncio
import logging
import sqlite3
from datetime import datetime, date, timezone
from typing import Optional, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

# ----------------------------
# Configuration and constants
# ----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "models/gemini-1.0")
ADMIN_IDS = os.environ.get("ADMIN_IDS", "")  # comma-separated telegram user IDs

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required.")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable is required.")

try:
    ADMIN_IDS_SET = {int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip()}
except Exception:
    ADMIN_IDS_SET = set()

DATABASE_FILE = os.environ.get("BOT_DB", "users.db")

# Subscription plans and daily token allowances (token unit is estimated; adjust as needed)
PLANS = {
    "free": {"daily_tokens": 1_000},
    "pro": {"daily_tokens": 20_000},
    "premium": {"daily_tokens": 100_000},
}
DEFAULT_PLAN = "free"

# Minimum tokens reserved for an output
MIN_OUTPUT_TOKENS = 20

# Gemini API endpoint (Generative Language API v1beta2 style)
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta2"

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------
# Database helpers
# ----------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    plan TEXT NOT NULL,
    tokens_remaining INTEGER NOT NULL,
    last_reset TEXT NOT NULL
);
"""

def init_db():
    conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()
    return conn

DB = init_db()

def iso_today_utc() -> str:
    return date.today().isoformat()

def get_user_row(user_id: int) -> Optional[sqlite3.Row]:
    cur = DB.cursor()
    cur.execute("SELECT user_id, plan, tokens_remaining, last_reset FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    return row

def create_user(user_id: int, plan: str = DEFAULT_PLAN) -> None:
    plan = plan if plan in PLANS else DEFAULT_PLAN
    tokens = PLANS[plan]["daily_tokens"]
    last_reset = iso_today_utc()
    DB.execute(
        "INSERT OR REPLACE INTO users (user_id, plan, tokens_remaining, last_reset) VALUES (?, ?, ?, ?)",
        (user_id, plan, tokens, last_reset),
    )
    DB.commit()

def update_user_tokens_and_plan(user_id: int, tokens_remaining: int, plan: Optional[str] = None) -> None:
    if plan:
        DB.execute(
            "UPDATE users SET tokens_remaining = ?, plan = ? WHERE user_id = ?",
            (tokens_remaining, plan, user_id),
        )
    else:
        DB.execute(
            "UPDATE users SET tokens_remaining = ? WHERE user_id = ?",
            (tokens_remaining, user_id),
        )
    DB.commit()

def reset_user_daily_if_needed(user_id: int) -> Tuple[str, int]:
    """
    Ensure the user's daily tokens are reset if the last_reset is before today.
    Returns (plan, tokens_remaining_after_reset)
    """
    row = get_user_row(user_id)
    if row is None:
        create_user(user_id)
        row = get_user_row(user_id)

    _, plan, tokens_remaining, last_reset = row
    today = iso_today_utc()
    if last_reset != today:
        tokens = PLANS.get(plan, PLANS[DEFAULT_PLAN])["daily_tokens"]
        DB.execute(
            "UPDATE users SET tokens_remaining = ?, last_reset = ? WHERE user_id = ?",
            (tokens, today, user_id),
        )
        DB.commit()
        tokens_remaining = tokens
    return plan, tokens_remaining

def set_user_plan(user_id: int, plan: str) -> None:
    if plan not in PLANS:
        raise ValueError("Unknown plan")
    tokens = PLANS[plan]["daily_tokens"]
    # Also reset last_reset to today and tokens to plan allowance
    today = iso_today_utc()
    DB.execute(
        "INSERT OR REPLACE INTO users (user_id, plan, tokens_remaining, last_reset) VALUES (?, ?, ?, ?)",
        (user_id, plan, tokens, today),
    )
    DB.commit()

# ----------------------------
# Token estimation helpers
# ----------------------------

def estimate_tokens(text: str) -> int:
    """
    Very rough token estimate: average token ~ 4 characters in English.
    This is intentionally conservative and simple to avoid dependency on tokenizers.
    """
    if not text:
        return 1
    approx = max(1, int(len(text) / 4))
    return approx

# ----------------------------
# Gemini API client
# ----------------------------

async def call_gemini(prompt: str, max_output_tokens: int = 256, temperature: float = 0.2) -> Tuple[str, Optional[dict]]:
    """
    Call Google's Generative Language API (Gemini) using the API key.
    Returns (generated_text, raw_response_json)
    """
    url = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateText"
    params = {"key": GEMINI_API_KEY}

    payload = {
        "prompt": {"text": prompt},
        "maxOutputTokens": int(max_output_tokens),
        "temperature": float(temperature),
    }

    headers = {
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, params=params, headers=headers, timeout=60) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error("Gemini API returned status %s: %s", resp.status, text)
                    raise RuntimeError(f"Gemini API error: {resp.status} - {text}")
                data = await resp.json()
                # Typical field: data['candidates'][0]['output'] but API versions vary
                output = None
                if isinstance(data, dict):
                    cands = data.get("candidates")
                    if cands and isinstance(cands, list) and len(cands) > 0:
                        output = cands[0].get("output") or cands[0].get("content") or ""
                    else:
                        # Fallbacks
                        output = data.get("output") or data.get("text") or ""
                else:
                    output = str(data)
                if output is None:
                    output = ""
                return output, data
        except asyncio.TimeoutError:
            logger.exception("Timeout calling Gemini API")
            raise
        except Exception:
            logger.exception("Unexpected error calling Gemini API")
            raise

# ----------------------------
# Aiogram bot setup
# ----------------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ----------------------------
# Bot command handlers
# ----------------------------

async def ensure_user_exists_and_reset(user_id: int) -> Tuple[str, int]:
    """
    Ensure the user exists in DB and daily reset occurs.
    Returns (plan, tokens_remaining)
    """
    return reset_user_daily_if_needed(user_id)

@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    from_user = message.from_user
    if not from_user:
        await message.reply("Could not identify you. Please send a direct message to the bot.")
        return
    user_id = from_user.id
    # Create user if needed and reset daily if necessary
    plan, tokens_remaining = ensure_user_exists_and_reset_sync(user_id)
    text = (
        f"Hello, {from_user.first_name}!\n\n"
        f"You are on the '{plan}' plan. Daily tokens remaining: {tokens_remaining}.\n\n"
        "How to use:\n"
        "- Send any message and I'll reply using Gemini.\n"
        "- Commands: /balance to check tokens, /help for this message.\n\n"
        "Upgrade plans: contact an admin or use /setplan if you're an admin."
    )
    await message.reply(text)

def ensure_user_exists_and_reset_sync(user_id: int) -> Tuple[str, int]:
    # wrapper to call the DB sync function from handlers
    return reset_user_daily_if_needed(user_id)

@dp.message_handler(commands=["balance"])
async def cmd_balance(message: types.Message):
    from_user = message.from_user
    if not from_user:
        await message.reply("Could not identify you.")
        return
    plan, tokens_remaining = ensure_user_exists_and_reset_sync(from_user.id)
    await message.reply(f"Plan: {plan}\nDaily tokens remaining: {tokens_remaining}")

@dp.message_handler(commands=["setplan"])
async def cmd_setplan(message: types.Message):
    """
    /setplan <user_id> <plan>
    Only allowed for admins defined in ADMIN_IDS env var.
    """
    if not message.from_user:
        await message.reply("Could not identify you.")
        return
    admin_id = message.from_user.id
    if admin_id not in ADMIN_IDS_SET:
        await message.reply("You are not authorized to use this command.")
        return

    args = message.get_args().split()
    if len(args) != 2:
        await message.reply("Usage: /setplan <user_id> <plan>\nPlans: " + ", ".join(PLANS.keys()))
        return
    try:
        target_user_id = int(args[0])
    except ValueError:
        await message.reply("Invalid user_id. It must be an integer Telegram user id.")
        return
    plan = args[1].lower()
    if plan not in PLANS:
        await message.reply("Unknown plan. Available plans: " + ", ".join(PLANS.keys()))
        return
    try:
        set_user_plan(target_user_id, plan)
        await message.reply(f"Set user {target_user_id} to plan '{plan}'. Tokens reset to daily allowance.")
        try:
            await bot.send_message(target_user_id, f"An admin set your plan to '{plan}'. Your daily tokens have been reset.")
        except Exception:
            # user may not be reachable by bot (didn't start or blocked)
            logger.info("Could not send notification to user %s (maybe hasn't started the bot)", target_user_id)
    except Exception as e:
        logger.exception("Failed to set plan")
        await message.reply(f"Failed to set plan: {e}")

@dp.message_handler()
async def handle_message(message: types.Message):
    """
    Handle normal user messages: check tokens, call Gemini, deduct tokens, send response.
    """
    if not message.from_user:
        await message.reply("Cannot determine sender.")
        return
    user_id = message.from_user.id
    text = (message.text or "") + ("\n" + message.caption if message.caption else "")
    if not text.strip():
        await message.reply("Please send text for me to respond to.")
        return

    # Ensure user exists and daily reset
    plan, tokens_remaining = reset_user_daily_if_needed(user_id)

    # Estimate input tokens
    input_tokens = estimate_tokens(text)
    logger.info("User %s input tokens estimate: %s", user_id, input_tokens)

    # Determine allowed max output tokens based on remaining tokens
    if tokens_remaining <= 0:
        await message.reply(
            "You have exhausted your daily tokens. Please wait until they reset tomorrow or contact an admin to upgrade your plan."
        )
        return

    # Reserve some tokens for output; ensure there's at least MIN_OUTPUT_TOKENS available
    if tokens_remaining - input_tokens < MIN_OUTPUT_TOKENS:
        await message.reply(
            f"Not enough tokens to process your request. You need at least {MIN_OUTPUT_TOKENS} tokens for a response. "
            f"Your remaining tokens: {tokens_remaining}. Consider upgrading your plan."
        )
        return

    # Compute the max output tokens we can allow (cap to a reasonable limit)
    max_output_tokens = min(2048, max(MIN_OUTPUT_TOKENS, tokens_remaining - input_tokens))
    logger.info("User %s allowed max_output_tokens: %s", user_id, max_output_tokens)

    # Inform user that the bot is thinking (optional)
    sent_typing = None
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except Exception:
        pass

    # Call Gemini
    try:
        gen_text, raw = await call_gemini(prompt=text, max_output_tokens=max_output_tokens)
    except Exception as e:
        logger.exception("Error calling Gemini for user %s", user_id)
        await message.reply(f"Sorry, an error occurred while contacting the language model: {e}")
        return

    # Estimate output tokens
    output_tokens = estimate_tokens(gen_text)
    total_used = input_tokens + output_tokens
    logger.info("User %s estimated output tokens: %s total_used: %s", user_id, output_tokens, total_used)

    # Deduct tokens (ensure we don't go negative)
    new_remaining = max(0, tokens_remaining - total_used)
    update_user_tokens_and_plan(user_id, new_remaining)
    logger.info("User %s tokens updated: %s -> %s", user_id, tokens_remaining, new_remaining)

    # Send generated text to user; handle long messages by splitting
    try:
        if not gen_text.strip():
            await message.reply("Model returned no text.")
        else:
            # Telegram message length limit ~ 4096 characters, split if needed.
            MAX_MSG = 4000
            if len(gen_text) <= MAX_MSG:
                await message.reply(gen_text)
            else:
                # Split gracefully
                parts = [gen_text[i:i+MAX_MSG] for i in range(0, len(gen_text), MAX_MSG)]
                for part in parts:
                    await message.reply(part)
    except Exception:
        logger.exception("Failed to send message to user %s", user_id)
        await message.reply("Failed to deliver the generated response. Please try again later.")

# ----------------------------
# Utility: command to show admin stats (optional)
# ----------------------------
@dp.message_handler(commands=["admin_stats"])
async def cmd_admin_stats(message: types.Message):
    if not message.from_user:
        await message.reply("Cannot identify sender.")
        return
    if message.from_user.id not in ADMIN_IDS_SET:
        await message.reply("You are not authorized to use this command.")
        return
    try:
        cur = DB.cursor()
        cur.execute("SELECT COUNT(*), SUM(tokens_remaining) FROM users")
        row = cur.fetchone()
        count = row[0] or 0
        total_tokens = row[1] or 0
        await message.reply(f"Registered users: {count}\nTotal tokens remaining across users: {total_tokens}")
    except Exception:
        logger.exception("admin_stats failed")
        await message.reply("Failed to fetch admin stats.")

# ----------------------------
# Graceful startup/shutdown
# ----------------------------

async def on_startup(dp):
    logger.info("Bot is starting up.")
    # Ensure DB table exists (redundant but safe)
    DB.execute(CREATE_TABLE_SQL)
    DB.commit()
    logger.info("Database ready.")

async def on_shutdown(dp):
    logger.info("Shutting down bot.")
    try:
        await bot.close()
    except Exception:
        pass
    try:
        DB.close()
    except Exception:
        pass

# ----------------------------
# Run bot
# ----------------------------
if __name__ == "__main__":
    logger.info("Starting Telegram Gemini bot.")
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)