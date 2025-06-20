import os, json, asyncio
from fastapi import FastAPI, Request
from aiogram import Bot, types

from dotenv import load_dotenv
load_dotenv()           # pulls vars from .env in the current directory

import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ROOT  = os.getenv("TG_API", "http://localhost:8081")   # points at container

print("BOT_TOKEN:", BOT_TOKEN)
print("API_ROOT:", API_ROOT)
print(BOT_TOKEN)
print(API_ROOT)

bot = Bot(BOT_TOKEN, api_root=API_ROOT)   # no parse_mode here

app = FastAPI()

@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = types.Update(**await req.json())
    if update.message and update.message.text:
        chat_id = update.message.chat.id
        if update.message.text.lower() == "/start":
            await bot.send_message(chat_id,
                "👋 Hi! What's your #1 priority today?")
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", port=8000, reload=True)
