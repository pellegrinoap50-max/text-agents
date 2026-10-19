# Two text-message agents (Telegram + Claude)

Two AI agents you can text from your phone at any time. Each one appears as its own contact in Telegram, has its own personality, and remembers your conversation. Both run from one small program on a cloud host.

## What you need

- A Telegram account (free; get the app on your phone).
- An Anthropic API key from https://console.anthropic.com. You pay per message; normal texting usually costs a few cents a day.
- A Railway account (https://railway.app), about $5/month. Any host that can run a Docker container also works.

## Step 1: Create the two bots in Telegram (5 min)

1. In Telegram, open a chat with **@BotFather** (it has a blue checkmark).
2. Send `/newbot`, choose a display name (for example "Max") and a username ending in `bot` (for example `max_assistant_bot`).
3. BotFather replies with a **token** that looks like `123456789:AAH...`. Save it. This is `AGENT1_TELEGRAM_TOKEN`.
4. Repeat for the second agent. That token is `AGENT2_TELEGRAM_TOKEN`.
5. Optional: use `/setuserpic` in BotFather to give each one a profile photo.

## Step 2: Describe your agents

Edit `agents.json`. For each agent, change:

- `name`: what it calls itself.
- `system_prompt`: its role, personality, and anything it should know about you. Replace the `PLACEHOLDER` sentence and keep the paragraph about texting style.
- `greeting`: the first message it sends after you press Start.
- `web_search`: `true` lets it look things up online.
- `effort`: `low` is fastest and cheapest, `medium` is the default, `high` thinks harder on difficult questions.

## Step 3: Deploy on Railway

1. Put this folder in a GitHub repo, or install the Railway CLI and run `railway up` from this folder.
2. In Railway, create a **New Project** from that repo. It finds the `Dockerfile` automatically.
3. Under **Variables**, add:
   - `ANTHROPIC_API_KEY`
   - `AGENT1_TELEGRAM_TOKEN`
   - `AGENT2_TELEGRAM_TOKEN`
   - `TIMEZONE`, for example `America/New_York` or `America/Los_Angeles`
   - `ALLOWED_USER_IDS`: leave this empty for now (see Step 4)
4. Add a **Volume** to the service mounted at `/data`, so the agents' memory survives redeploys.
5. Deploy. The logs should show `Agent online as @...` twice.

## Step 4: Lock it to you

So that strangers who find your bots can't use your API credits, the agents only answer people on an allowlist.

1. In Telegram, search for your bot's username, open it, and press **Start**.
2. It replies with "This bot is private. Your Telegram user ID is 123456789".
3. Set `ALLOWED_USER_IDS=123456789` in Railway. Separate several IDs with commas to let family use it too. Railway redeploys automatically.
4. Text either bot. Done.

## Using it

- Just text it like a person.
- `/reset` wipes that agent's memory of your conversation.
- Pin both chats in Telegram so they sit at the top, like iMessage threads.

## Running it on your own computer instead (for testing)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # fill it in
set -a; . ./.env; set +a
.venv/bin/python bot.py
```

The agents only answer while this is running. Stop it before you deploy to Railway, because a bot token can be polled by only one copy of the program at a time.

## Notes

- Model: Claude Opus 5.5 (`claude-opus-5-5`). Set the `CLAUDE_MODEL` variable to change it.
- If Claude's safety filter declines a message, the request is automatically retried on a fallback model. If that also declines, the agent says it can't help with that.
- Long conversations are trimmed to the most recent messages, so very old details eventually fade. Put anything permanent ("I'm vegetarian", "my kids are Sam and Jo") in the `system_prompt`.
- Adding a third agent: create another bot in BotFather, add another entry to `agents.json` with a new `telegram_token_env` name, and add that variable in Railway.
