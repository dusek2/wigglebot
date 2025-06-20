"""
WiggleBot v0.2  –  ADHD-friendly Telegram accountability coach
--------------------------------------------------------------
• /start  → asks for today’s #1 priority
• Any other text becomes a task; bot keeps nagging every N minutes
• User replies "done"  → task closed, nags stop
• User replies "stuck" → GPT-4o mini suggests a micro-action

Storage is in-memory for quick prototyping.
Swap `tasks` + `active_reminders` for a real DB/queue in production.
"""

import os, uuid, asyncio
from typing import Dict

from fastapi import FastAPI, Request
from dotenv import load_dotenv
from aiogram import Bot, types
from aiogram.client.bot import DefaultBotProperties
from aiogram.enums import ParseMode
from openai import AsyncOpenAI

# ------------------------------------------------------------------ ENV
load_dotenv()  # pulls BOT_TOKEN, TG_API, OPENAI_API_KEY

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ROOT  = os.getenv("TG_API", "https://api.telegram.org")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not (BOT_TOKEN and OPENAI_API_KEY):
    raise RuntimeError("BOT_TOKEN and OPENAI_API_KEY must be set in .env")

# ------------------------------------------------------------------ LIBS
bot = Bot(
    BOT_TOKEN,
    api_root=API_ROOT,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

oaiclient = AsyncOpenAI()  # auto-reads OPENAI_API_KEY
llm_sema  = asyncio.Semaphore(3)  # max 3 concurrent LLM calls

app = FastAPI()

# ------------------------------------------------------------------ STATE (naïve, RAM)
tasks: Dict[str, Dict]          = {}  # task_id → dict(chat_id,text,done)
active_reminders: Dict[str, asyncio.Task] = {}
REMINDER_EVERY = 30 * 60  # seconds (30 min)

# ------------------------------------------------------------------ LLM HELPER
SYSTEM_PROMPT = """
You are “WiggleBot”, an upbeat but no-nonsense ADHD accountability coach.
Rules:
• Messages ≤ 40 words.
• Casual tone with an emoji or two.
• If user is stuck, suggest one concrete next step.
"""

async def coach_reply(reason: str, task_text: str) -> str:
    """reason = ack | remind | stuck"""
    user_msg = {
        "ack":   f"I just committed to: {task_text}",
        "remind":f"I haven't finished yet: {task_text}",
        "stuck": f"I'm stuck on: {task_text}",
    }[reason]

    async with llm_sema:  # prevent token flood
        resp = await oaiclient.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system", "content":SYSTEM_PROMPT},
                {"role":"user",   "content":user_msg}
            ],
            max_tokens=60,
        )
    return resp.choices[0].message.content.strip()

# ------------------------------------------------------------------ WEBHOOK
@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = types.Update(**await req.json())

    if not (update.message and update.message.text):
        return {"ok": True}

    chat_id = update.message.chat.id
    text    = update.message.text.strip()

    # ---------- /start ----------
    if text.lower() == "/start":
        await bot.send_message(
            chat_id,
            "👋 Hi! What's your <b>#1 priority</b> today?\n"
            "Just reply with a sentence (e.g. <i>Finish project outline</i>)."
        )
        return {"ok": True}

    # ---------- mark DONE ----------
    if text.lower() in {"done", "✅ done", "finished"}:
        for tid, t in list(tasks.items()):
            if t["chat_id"] == chat_id and not t["done"]:
                t["done"] = True
                reminder = active_reminders.pop(tid, None)
                if reminder: reminder.cancel()
                await bot.send_message(chat_id, "🎉 Nice work! Task closed.")
                break
        else:
            await bot.send_message(chat_id, "No open tasks to close. 🎈")
        return {"ok": True}

    # ---------- STUCK ----------
    if text.lower() == "stuck":
        open_task = next(
            (t for t in tasks.values() if t["chat_id"] == chat_id and not t["done"]),
            None,
        )
        if not open_task:
            await bot.send_message(chat_id, "I don't see any open tasks 🧐")
            return {"ok": True}

        tip = await coach_reply("stuck", open_task["text"])
        await bot.send_message(chat_id, tip)
        return {"ok": True}

    # ---------- NEW TASK ----------
    task_id = str(uuid.uuid4())
    tasks[task_id] = {"chat_id": chat_id, "text": text, "done": False}

    ack = await coach_reply("ack", text)
    await bot.send_message(chat_id, ack)

    loop = asyncio.get_running_loop()
    active_reminders[task_id] = loop.create_task(reminder_loop(task_id))

    return {"ok": True}

# ------------------------------------------------------------------ REMINDER LOOP
async def reminder_loop(task_id: str):
    while True:
        await asyncio.sleep(REMINDER_EVERY)
        t = tasks.get(task_id)
        if not t or t["done"]:
            break
        try:
            msg = await coach_reply("remind", t["text"])
            await bot.send_message(t["chat_id"], msg)
        except Exception as e:
            print("Reminder send error:", e)

# ------------------------------------------------------------------ ENTRYPOINT
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
