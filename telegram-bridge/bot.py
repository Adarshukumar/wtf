"""The Telegram bot — your control panel and the owner-registration door.

When you send /start, the bot saves your chat id so the server knows where to
deliver messages. Admin commands let you check health, list owners and
broadcast. Polling auto-reconnects and the supervisor in main.py restarts it
forever, so the bot survives network blips without a babysitter.
"""
from __future__ import annotations

import time

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from config import Settings
from store import Store

STARTED_AT = time.time()

WELCOME = (
    "👋 <b>Hey, I'm your server bridge!</b>\n\n"
    "From now on your cloud server can reach you here — instantly.\n\n"
    "🆔 Your chat id: <code>{chat_id}</code>\n"
    "    (save it as OWNER_CHAT_ID if you want to hard-code it)\n\n"
    "Commands:\n"
    "/status  — system & queue health\n"
    "/ping    — test the round trip\n"
    "/hb on|off — toggle heartbeat reports\n"
    "/id      — show your chat id\n"
    "/help    — this message\n\n"
    "Send /start anytime to re-register."
)

HELP = (
    "📖 <b>Available commands</b>\n\n"
    "/status  — uptime, queue, owners, Telegram link\n"
    "/ping    — round-trip latency test\n"
    "/hb on|off — periodic server heartbeat\n"
    "/id      — your chat id\n"
    "/owners  — list registered owners (admin)\n"
    "/say text — broadcast to all owners (admin)\n"
    "/help    — this message"
)


def _is_admin(message: Message, settings: Settings) -> bool:
    if not settings.admin_ids:
        return True
    return message.chat.id in settings.admin_ids


def _uptime() -> str:
    secs = int(time.time() - STARTED_AT)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def build_bot(store: Store, settings: Settings) -> tuple[Bot, Dispatcher]:
    router = Router()

    @router.message(CommandStart())
    async def on_start(message: Message, command: CommandObject) -> None:
        store.register_owner(
            message.chat.id,
            username=message.from_user.username or "",
            first_name=message.from_user.first_name or "",
        )
        await message.answer(WELCOME.format(chat_id=message.chat.id), parse_mode="HTML")

    @router.message(Command("help"))
    async def on_help(message: Message) -> None:
        await message.answer(HELP, parse_mode="HTML")

    @router.message(Command("id"))
    async def on_id(message: Message) -> None:
        await message.answer(
            f"🆔 Your chat id: <code>{message.chat.id}</code>", parse_mode="HTML"
        )

    @router.message(Command("ping"))
    async def on_ping(message: Message) -> None:
        sent = time.time()
        reply = await message.answer("🏓 pong…")
        ms = round((time.time() - sent) * 1000)
        await reply.edit_text(f"🏓 pong — {ms} ms")

    @router.message(Command("status"))
    async def on_status(message: Message) -> None:
        stats = store.stats()
        hb = "ON" if store.heartbeat_setting(message.chat.id) else "OFF"
        lines = (
            "📊 <b>Bridge status</b>\n\n"
            f"Uptime: <code>{_uptime()}</code>\n"
            f"Owners: <code>{stats['owners']}</code>\n"
            f"Pending: <code>{stats['pending']}</code> | "
            f"Failed: <code>{stats['failed']}</code> | "
            f"Dead: <code>{stats['dead']}</code>\n"
            f"Delivered (all time): <code>{stats['sent']}</code>\n"
            f"Heartbeat: <code>{hb}</code>\n"
            f"Telegram link: <code>connected</code> ✅"
        )
        await message.answer(lines, parse_mode="HTML")

    @router.message(Command("hb"))
    async def on_hb(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip().lower()
        if arg not in ("on", "off"):
            await message.answer("Usage: /hb on  or  /hb off")
            return
        on = arg == "on"
        store.set_heartbeat(message.chat.id, on)
        await message.answer(f"🫀 Heartbeat turned <b>{arg.upper()}</b>", parse_mode="HTML")

    @router.message(Command("owners"))
    async def on_owners(message: Message) -> None:
        if not _is_admin(message, settings):
            await message.answer("⛔ Admin only.")
            return
        owners = store.get_owners()
        if not owners:
            await message.answer("No owners registered yet.")
            return
        lines = [f"👥 <b>{len(owners)} owner(s)</b>"]
        for o in owners:
            name = o["first_name"] or o["username"] or "—"
            hb = "🫀" if o["heartbeat"] else "  "
            lines.append(f"{hb} <code>{o['chat_id']}</code> — {name}")
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("say"))
    async def on_say(message: Message, command: CommandObject) -> None:
        if not _is_admin(message, settings):
            await message.answer("⛔ Admin only.")
            return
        text = (command.args or "").strip()
        if not text:
            await message.answer("Usage: /say your message here")
            return
        ids = store.enqueue_broadcast(text)
        await message.answer(f"📣 Queued for {len(ids)} owner(s).")

    # ------------------------------------------------------------------ build
    if not settings.bot_token:
        raise ValueError("BOT_TOKEN is not set — cannot build the bot.")

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp
