"""
WiggleBot v0.3  –  full-day ADHD workflow coach

Features
--------
1. 07:00 PT prompt → user lists goals (newline-separated).
2. Bot builds a queue: 1 top, up to 3 mid, then extras.
3. Works one task at a time; sends inline buttons ✅ Done / 🆘 Stuck.
4. Pings every FOCUS_MIN minutes until Done, then moves on.
5. GPT-4o mini writes all user-facing text.

Storage is in-mem; swap for Postgres & Redis when ready.
"""

import os, uuid, asyncio, datetime as dt
from typing import Dict, List

from fastapi import FastAPI, Request
from dotenv import load_dotenv
from aiogram import Bot, types
from aiogram.client.bot import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openai import AsyncOpenAI

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ------------------------------------------------------------------ ENV
load_dotenv()
BOT_TOKEN        = os.getenv("BOT_TOKEN")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
API_ROOT         = os.getenv("TG_API", "https://api.telegram.org")
PACIFIC_UTC_HOUR = 14                         # 7 AM PT == 14 UTC
FOCUS_MIN        = 25                         # reminder cadence

assert BOT_TOKEN and OPENAI_API_KEY, "Set BOT_TOKEN & OPENAI_API_KEY in .env"

# ------------------------------------------------------------------ LIBS
bot = Bot(
    BOT_TOKEN,
    api_root=API_ROOT,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
oaiclient = AsyncOpenAI()
llm_sema  = asyncio.Semaphore(3)

app = FastAPI()
sched = AsyncIOScheduler(timezone="UTC")

# ------------------------------------------------------------------ STATE
class Task:
    def __init__(self, text: str, priority: str):
        self.id   = str(uuid.uuid4())
        self.text = text
        self.prio = priority  # top | mid | extra
        self.done = False

users: Dict[int, Dict] = {}       # chat_id → {tasks: List[Task], pointer:int, reminder:TaskHandle}

# ------------------------------------------------------------------ LLM
SYSTEM_PROMPT = """
You are “WiggleBot”, an upbeat ADHD productivity coach.
Rules:
• Replies ≤ 40 words, first-person, friendly tone.
• Suggest 1 concrete action when user is stuck.
• Celebrate completion with an emoji.
"""

async def gpt(role: str, content: str) -> str:
    async with llm_sema:
        res = await oaiclient.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": content},
            ],
            max_tokens=60,
        )
    return res.choices[0].message.content.strip()

# ------------------------------------------------------------------ HELPERS
def build_keyboard(task_id: str) -> types.InlineKeyboardMarkup:
    kb = (InlineKeyboardBuilder()
          .button(text="✅ Done",  callback_data=f"done:{task_id}")
          .button(text="🆘 Stuck", callback_data=f"stuck:{task_id}")
          .adjust(2)
          .as_markup())
    return kb

def next_task(chat_id: int):
    u = users.get(chat_id)
    if not u: return None
    while u["pointer"] < len(u["tasks"]) and u["tasks"][u["pointer"]].done:
        u["pointer"] += 1
    return u["tasks"][u["pointer"]] if u["pointer"] < len(u["tasks"]) else None

async def start_focus(chat_id: int):
    task = next_task(chat_id)
    if not task:
        await bot.send_message(chat_id, "🥳 Day’s list complete! Great work.")
        return
    msg = await gpt("coach", f"Start focusing on: {task.text}")
    await bot.send_message(chat_id, msg, reply_markup=build_keyboard(task.id))
    # schedule reminder loop
    loop = asyncio.get_running_loop()
    users[chat_id]["reminder"] = loop.create_task(remind_loop(chat_id, task.id))

async def remind_loop(chat_id: int, task_id: str):
    while True:
        await asyncio.sleep(FOCUS_MIN * 60)
        u = users.get(chat_id)
        if not u: return
        task = next((t for t in u["tasks"] if t.id == task_id), None)
        if not task or task.done:
            return
        nud = await gpt("remind", f"I haven't finished: {task.text}")
        await bot.send_message(chat_id, nud, reply_markup=build_keyboard(task.id))

# ------------------------------------------------------------------ WEBHOOK
@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = types.Update(**await req.json())

    # ---------- Callback buttons ----------
    if update.callback_query:
        data = update.callback_query.data or ""
        action, tid = data.split(":", 1)
        chat_id = update.callback_query.from_user.id
        await bot.answer_callback_query(update.callback_query.id)
        if action == "done":
            mark_done(chat_id, tid)
            await bot.send_message(chat_id, "🎉 Task marked done!")
            await start_focus(chat_id)          # move to next
        elif action == "stuck":
            t = get_task(chat_id, tid)
            tip = await gpt("stuck", f"I'm stuck on: {t.text}")
            await bot.send_message(chat_id, tip, reply_markup=build_keyboard(t.id))
        return {"ok": True}

    if not (update.message and update.message.text):
        return {"ok": True}

    chat_id = update.message.chat.id
    text    = update.message.text.strip()

    # ensure user record exists
    users.setdefault(chat_id, {"tasks": [], "pointer": 0, "reminder": None})

    # ---------- /start ----------
    if text.lower() == "/start":
        await bot.send_message(chat_id, "👋 Welcome! I’ll ping you at 7 AM each morning to plan your day.")
        return {"ok": True}

    # ---------- handle morning goal list ----------
    if is_morning_input(text):
        add_tasks_from_morning(chat_id, text)
        ack = await gpt("ack", "I've planned my goals for today")
        await bot.send_message(chat_id, ack)
        await start_focus(chat_id)
        return {"ok": True}

    # fallback
    await bot.send_message(chat_id, "I didn't catch that. Wait for the morning prompt or press buttons 😉")
    return {"ok": True}

# ------------------------------------------------------------------ TASK OPS
def is_morning_input(txt: str) -> bool:
    return "\n" in txt   # simplest heuristic

def add_tasks_from_morning(chat_id: int, raw: str):
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    top     = lines[:1]
    mids    = lines[1:4]
    extras  = lines[4:]

    u = users[chat_id]
    u["tasks"].clear(); u["pointer"] = 0
    if u["reminder"]:
        u["reminder"].cancel()

    for l in top:
        u["tasks"].append(Task(l, "top"))
    for l in mids:
        u["tasks"].append(Task(l, "mid"))
    for l in extras:
        u["tasks"].append(Task(l, "extra"))

def get_task(chat_id: int, tid: str) -> Task:
    return next(t for t in users[chat_id]["tasks"] if t.id == tid)

def mark_done(chat_id: int, tid: str):
    t = get_task(chat_id, tid); t.done = True
    # cancel reminder
    rem = users[chat_id]["reminder"]
    if rem and not rem.cancelled():
        rem.cancel()

# ------------------------------------------------------------------ MORNING PROMPT JOB
@sched.scheduled_job(CronTrigger(hour=PACIFIC_UTC_HOUR, minute=0))
async def morning_prompt():
    for chat_id in users.keys():
        await bot.send_message(
            chat_id,
            "🌞 Good morning!\n"
            "• Send me <b>ONE top goal</b>.\n"
            "• <b>THREE medium goals</b> (optional).\n"
            "• Any extra tasks.\n"
            "Put each on its own line."
        )

# ------------------------------------------------------------------ ENTRYPOINT
if __name__ == "__main__":
    import uvicorn
    sched.start()
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
