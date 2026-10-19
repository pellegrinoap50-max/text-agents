"""Run several Claude-powered agents as Telegram bots from one process.

Each agent in agents.json gets its own Telegram bot (its own contact in your
chat list), its own personality, and its own memory of your conversation.
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("agents")

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))
# Only these Telegram user IDs may talk to the agents. Anyone else is ignored,
# so a stranger who finds your bot can't run up your API bill.
ALLOWED_USER_IDS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
# When a conversation grows past MAX_HISTORY messages, keep the most recent KEEP_HISTORY.
MAX_HISTORY = 80
KEEP_HISTORY = 40
TELEGRAM_LIMIT = 4096

claude = anthropic.AsyncAnthropic()


# ---------- conversation memory (SQLite) ----------

DATA_DIR.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DATA_DIR / "history.db", check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS history (agent TEXT, chat_id INTEGER, messages TEXT, PRIMARY KEY (agent, chat_id))")
db.commit()


def load_history(agent: str, chat_id: int) -> list:
    row = db.execute("SELECT messages FROM history WHERE agent = ? AND chat_id = ?", (agent, chat_id)).fetchone()
    return json.loads(row[0]) if row else []


def save_history(agent: str, chat_id: int, messages: list) -> None:
    db.execute("INSERT OR REPLACE INTO history VALUES (?, ?, ?)", (agent, chat_id, json.dumps(messages)))
    db.commit()


def trim_history(messages: list) -> list:
    """Drop old turns once the conversation gets long.

    Thinking blocks are tied to the exact history that produced them, so once
    earlier turns are removed the retained turns' thinking blocks are stripped
    (their text and tool results stay). This happens once per trim, not every message.
    """
    if len(messages) <= MAX_HISTORY:
        return messages
    kept = messages[-KEEP_HISTORY:]
    # The history must start with a plain user message, not an assistant turn.
    while kept and not (kept[0]["role"] == "user" and isinstance(kept[0]["content"], str)):
        kept = kept[1:]
    for m in kept:
        if m["role"] == "assistant":
            m["content"] = [b for b in m["content"] if b.get("type") not in ("thinking", "redacted_thinking")]
    return kept


# ---------- talking to Claude ----------

def assistant_blocks(content) -> list:
    """Turn a response's content into JSON blocks to store in the history."""
    blocks = [b.model_dump(mode="json", exclude_none=True) for b in content]
    # If a safety fallback switched models mid-answer, only what follows the
    # last fallback marker is passed back in later turns.
    fallback_idx = [i for i, b in enumerate(blocks) if b.get("type") == "fallback"]
    if fallback_idx:
        blocks = blocks[fallback_idx[-1] + 1:]
    return blocks


async def ask_claude(agent: dict, history: list) -> tuple[str, list]:
    """Send the conversation to Claude; return (reply text, assistant blocks to store)."""
    tools = []
    if agent.get("web_search", False):
        tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 5})

    blocks: list = []
    for _ in range(5):  # web search can pause a long turn; resume it a few times
        messages = history + ([{"role": "assistant", "content": blocks}] if blocks else [])
        response = await claude.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=agent["system_prompt"],
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": agent.get("effort", "medium")},
            # If a request is declined by a safety classifier, retry it on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return "Sorry, I can't help with that one.", []
        blocks += assistant_blocks(response.content)
        if response.stop_reason != "pause_turn":
            break

    text = "\n\n".join(b["text"] for b in blocks if b.get("type") == "text" and b["text"].strip())
    if response.stop_reason == "max_tokens":
        text += "\n\n(I ran out of room. Say \"continue\" to get the rest.)"
    return text or "(no reply)", blocks


# ---------- Telegram handlers ----------

def make_handlers(agent: dict):
    name = agent["name"]

    def allowed(update: Update) -> bool:
        user = update.effective_user
        if user and user.id in ALLOWED_USER_IDS:
            return True
        log.warning("[%s] ignored message from user id %s (not in ALLOWED_USER_IDS)", name, user.id if user else "?")
        return False

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not allowed(update):
            await update.message.reply_text(
                f"This bot is private. Your Telegram user ID is {user.id}. "
                "If you're the owner, add it to ALLOWED_USER_IDS and redeploy."
            )
            return
        await update.message.reply_text(agent.get("greeting", f"Hi, I'm {name}. Text me anytime."))

    async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not allowed(update):
            return
        save_history(name, update.effective_chat.id, [])
        await update.message.reply_text("Fresh start. I've forgotten our previous conversation.")

    locks: dict[int, asyncio.Lock] = {}

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not allowed(update):
            return
        chat_id = update.effective_chat.id
        # Handle one message at a time per chat so replies don't interleave.
        async with locks.setdefault(chat_id, asyncio.Lock()):
            await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
            now = datetime.now(TIMEZONE).strftime("%A, %B %d, %Y %I:%M %p %Z")
            history = load_history(name, chat_id)
            # The timestamp goes in the message, not the system prompt, so the prompt stays stable.
            history.append({"role": "user", "content": f"[{now}]\n{update.message.text}"})
            try:
                reply, blocks = await ask_claude(agent, history)
            except anthropic.RateLimitError:
                await update.message.reply_text("I'm getting too many requests right now. Try again in a minute.")
                return
            except anthropic.APIStatusError as e:
                log.exception("[%s] Claude API error", name)
                await update.message.reply_text(f"Something went wrong on my end (error {e.status_code}). Try again shortly.")
                return
            except anthropic.APIConnectionError:
                log.exception("[%s] could not reach Claude", name)
                await update.message.reply_text("I couldn't reach my brain just now. Try again shortly.")
                return

            if blocks:
                history.append({"role": "assistant", "content": blocks})
                save_history(name, chat_id, trim_history(history))
            for i in range(0, len(reply), TELEGRAM_LIMIT):
                await update.message.reply_text(reply[i:i + TELEGRAM_LIMIT])

    return [
        CommandHandler("start", start),
        CommandHandler("reset", reset),
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_message),
    ]


async def main() -> None:
    agents = json.loads(Path(os.environ.get("AGENTS_FILE", "agents.json")).read_text())
    if not ALLOWED_USER_IDS:
        log.warning("ALLOWED_USER_IDS is empty: the agents will ignore everyone. "
                    "Message a bot and send /start to see your user ID.")

    apps = []
    for agent in agents:
        token = os.environ.get(agent["telegram_token_env"])
        if not token:
            raise SystemExit(f"Missing environment variable {agent['telegram_token_env']} for agent {agent['name']}")
        app = Application.builder().token(token).build()
        app.add_handlers(make_handlers(agent))
        apps.append(app)

    for app in apps:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=False)
        me = await app.bot.get_me()
        log.info("Agent online as @%s", me.username)

    try:
        await asyncio.Event().wait()  # run until the process is stopped
    finally:
        for app in apps:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
