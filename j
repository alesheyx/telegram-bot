import TelegramBot from "node-telegram-bot-api";
import OpenAI from "openai";
import dotenv from "dotenv";

dotenv.config(); // .env faylni yuklaydi

// 🔑 Tokenlar
const TELEGRAM_TOKEN = process.env.TELEGRAM_TOKEN;
const OPENAI_KEY = process.env.OPENAI_KEY;

// 🔌 Telegram bot yaratamiz
const bot = new TelegramBot(TELEGRAM_TOKEN, { polling: true });

// 🤖 OpenAI klientini sozlaymiz
const openai = new OpenAI({
  apiKey: OPENAI_KEY,
});

// 💬 Foydalanuvchi xabar yozganda
bot.on("message", async (msg) => {
  const chatId = msg.chat.id;
  const userText = msg.text;

  // Foydalanuvchi yozgan matnni OpenAI ga yuboramiz
  try {
    const completion = await openai.chat.completions.create({
      model: "gpt-4o-mini",
      messages: [
        { role: "system", content: "Sen foydalanuvchi bilan samimiy suhbatlashadigan yordamchisan." },
        { role: "user", content: userText },
      ],
    });

    const reply = completion.choices[0].message.content;
    bot.sendMessage(chatId, reply);
  } catch (error) {
    console.error(error);
    bot.sendMessage(chatId, "Xatolik yuz berdi 😔");
  }
});
