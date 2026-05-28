"""
Conversational Claude assistant accessible via Telegram.

The user messages the bot and Claude responds as a stock analyst,
with full access to the monitored account's tweet history.
"""

import asyncio
import html
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import anthropic
import httpx

from config import Config

if TYPE_CHECKING:
    from x_monitor import Tweet

logger = logging.getLogger(__name__)

BASE_SYSTEM_PROMPT = """\
You are a stock market analyst and trading assistant for Adrien, who is a smart \
but non-technical investor. He understands business and money but does NOT have \
a finance or engineering background — so you always explain things in plain English.

Your style (always follow this):
- Use simple real-world analogies for anything technical. \
  e.g. "think of CPO like upgrading a city's copper phone lines to fiber optic"
- Explain WHY each stock matters in the context of the theme being discussed
- Translate every piece of jargon immediately after using it
- Be direct and give a clear opinion — never "it depends" without explaining which way you lean
- End every substantive response with a "Bottom line:" sentence that summarises \
  what Adrien should take away
- When discussing a specific stock: cover what the company does in one plain sentence, \
  why it matters right now, and your honest take
- Format tickers with $ prefix (e.g. $AAPL)
- When the user refers to "her posts", "she", or "the account", they mean \
  the monitored X account (@aleabitoreddit) whose recent posts are listed below

Never give generic risk disclaimers. Treat Adrien as a smart adult who just \
wants clear, useful information — not legal cover.
"""

MAX_HISTORY = 30   # conversation turns kept in memory
MAX_POSTS_IN_CONTEXT = 60   # tweet history lines injected into system prompt


def _parse_tweet_date(created_at: str) -> str:
    """Convert Twitter date string to a readable short format."""
    try:
        dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S +0000 %Y")
        return dt.strftime("%b %d %H:%M UTC")
    except Exception:
        return created_at[:16] if created_at else "unknown date"


class TelegramChatHandler:
    def __init__(self, config: Config, tweet_history: list):
        self.config = config
        self._tweet_history = tweet_history   # shared list, mutated by x_monitor_loop
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._history: list[dict] = []   # conversation turns
        self._offset: int = 0

    # ------------------------------------------------------------------ #
    #  Main loop                                                           #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Long-poll Telegram for user messages and respond with Claude."""
        logger.info("Chat assistant ready — message @Serenity_Tracker_bot to start")
        while True:
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Chat loop error: %s", e)
                await asyncio.sleep(5)

    # ------------------------------------------------------------------ #
    #  Telegram polling                                                    #
    # ------------------------------------------------------------------ #

    async def _get_updates(self) -> list[dict]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/getUpdates",
                params={
                    "offset": self._offset,
                    "timeout": 30,
                    "allowed_updates": ["message"],
                },
                timeout=40.0,
            )
            resp.raise_for_status()
            updates = resp.json().get("result", [])
            if updates:
                self._offset = updates[-1]["update_id"] + 1
            return updates

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message", {})
        text = message.get("text", "").strip()
        chat_id = str(message.get("chat", {}).get("id", ""))

        if not text or not chat_id:
            return
        if chat_id != str(self.config.TELEGRAM_CHAT_ID):
            return

        if text.startswith("/"):
            await self._handle_command(text)
            return

        await self._send_typing()
        self._history.append({"role": "user", "content": text})
        reply = await self._ask_claude()
        self._history.append({"role": "assistant", "content": reply})
        await self._send(reply)

    # ------------------------------------------------------------------ #
    #  Commands                                                            #
    # ------------------------------------------------------------------ #

    async def _handle_command(self, text: str) -> None:
        cmd = text.split()[0].lower()
        if cmd == "/start":
            n = len(self._tweet_history)
            await self._send(
                f"👋 <b>Stock Analyst Assistant ready.</b>\n\n"
                f"I have <b>{n} posts</b> from the monitored account loaded as context — "
                f"ask me about any of them, any ticker, or any trade setup.\n\n"
                f"Commands:\n"
                f"  /clear — wipe conversation history\n"
                f"  /posts — show how many posts I have loaded\n"
                f"  /help  — show this message"
            )
        elif cmd in ("/clear", "/reset"):
            self._history = []
            await self._send("🗑️ Conversation cleared. Post history still loaded.")
        elif cmd == "/posts":
            n = len(self._tweet_history)
            if n == 0:
                await self._send("No posts loaded yet — the monitor is still starting up.")
            else:
                newest = sorted(self._tweet_history, key=lambda t: t.id, reverse=True)[0]
                oldest = sorted(self._tweet_history, key=lambda t: t.id)[0]
                await self._send(
                    f"📚 <b>{n} posts loaded</b>\n"
                    f"Oldest: {_parse_tweet_date(oldest.created_at)}\n"
                    f"Newest: {_parse_tweet_date(newest.created_at)}"
                )
        elif cmd == "/help":
            await self._send(
                "Just talk to me normally. I can see all the monitored account's recent posts.\n\n"
                "<b>Example questions:</b>\n"
                "• <i>When did she first mention $SIVE?</i>\n"
                "• <i>What's her current take on $NVTS?</i>\n"
                "• <i>Has she posted about any semiconductor plays?</i>\n"
                "• <i>What's your view on $AAPL at current levels?</i>\n\n"
                "/clear — reset conversation memory\n"
                "/posts — show loaded post count"
            )
        else:
            await self._send("Unknown command. Type /help for options.")

    # ------------------------------------------------------------------ #
    #  Claude                                                              #
    # ------------------------------------------------------------------ #

    def _build_system_prompt(self) -> str:
        """Inject the tweet history into the system prompt so Claude can reference it."""
        system = BASE_SYSTEM_PROMPT

        if not self._tweet_history:
            system += (
                "\n\nNote: No posts from the monitored account have been loaded yet. "
                "If the user asks about her posts, let them know the monitor just started."
            )
            return system

        # Sort newest-first, cap at MAX_POSTS_IN_CONTEXT
        posts = sorted(self._tweet_history, key=lambda t: t.id, reverse=True)[:MAX_POSTS_IN_CONTEXT]
        author = posts[0].author

        lines = []
        for t in posts:
            date = _parse_tweet_date(t.created_at)
            # Truncate very long tweets for token efficiency
            body = t.text if len(t.text) <= 280 else t.text[:277] + "…"
            lines.append(f"[{date}] {body}")

        post_block = "\n".join(lines)
        system += (
            f"\n\n--- All known posts from @{author} (newest first) ---\n"
            f"{post_block}\n"
            f"--- end of post history ({len(posts)} posts) ---"
        )
        return system

    async def _ask_claude(self) -> str:
        try:
            messages = self._history[-MAX_HISTORY:]
            system = self._build_system_prompt()
            response = await asyncio.to_thread(
                self.client.messages.create,
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                messages=messages,
            )
            return response.content[0].text.strip()
        except anthropic.APIError as e:
            logger.error("Claude API error: %s", e)
            return "⚠️ Claude hit an error — try again in a moment."
        except Exception as e:
            logger.error("Unexpected Claude error: %s", e)
            return "⚠️ Something went wrong — try again."

    # ------------------------------------------------------------------ #
    #  Telegram send helpers                                               #
    # ------------------------------------------------------------------ #

    async def _send(self, text: str) -> None:
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.config.TELEGRAM_CHAT_ID,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=10.0,
                )
                if not resp.is_success:
                    # Fall back to plain text if HTML parse fails
                    await client.post(
                        f"{self.base_url}/sendMessage",
                        json={
                            "chat_id": self.config.TELEGRAM_CHAT_ID,
                            "text": html.unescape(text),
                        },
                        timeout=10.0,
                    )
            except httpx.HTTPError as e:
                logger.error("Telegram send error: %s", e)

    async def _send_typing(self) -> None:
        async with httpx.AsyncClient() as client:
            try:
                await client.post(
                    f"{self.base_url}/sendChatAction",
                    json={"chat_id": self.config.TELEGRAM_CHAT_ID, "action": "typing"},
                    timeout=5.0,
                )
            except Exception:
                pass
