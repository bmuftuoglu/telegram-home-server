from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

TELEGRAM_APP: Application | None = None
_TZ_TR = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    default_chat_id: int
    internal_api_token: str
    aski_service_url: str
    notify_host: str
    notify_port: int

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN must be set")

        allowed_raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        if not allowed_raw:
            raise RuntimeError("TELEGRAM_ALLOWED_USER_IDS must be set")
        allowed = frozenset(
            int(uid.strip()) for uid in allowed_raw.split(",") if uid.strip().isdigit()
        )
        if not allowed:
            raise RuntimeError("TELEGRAM_ALLOWED_USER_IDS must contain valid user IDs")

        default_chat_raw = os.getenv("TELEGRAM_DEFAULT_CHAT_ID", "").strip()
        if not default_chat_raw:
            raise RuntimeError("TELEGRAM_DEFAULT_CHAT_ID must be set")

        internal_token = os.getenv("INTERNAL_API_TOKEN", "").strip()
        if not internal_token:
            raise RuntimeError("INTERNAL_API_TOKEN must be set")

        return cls(
            telegram_bot_token=token,
            allowed_user_ids=allowed,
            default_chat_id=int(default_chat_raw),
            internal_api_token=internal_token,
            aski_service_url=os.getenv(
                "ASKI_SERVICE_URL", "http://aski-water-watch:8081"
            ).rstrip("/"),
            notify_host=os.getenv("BOT_NOTIFY_HOST", "0.0.0.0"),
            notify_port=int(os.getenv("PORT", "8080")),
        )


# ── FastAPI /notify endpoint ──────────────────────────────────────────────────

class NotifyPayload(BaseModel):
    text: str
    chat_id: int | None = None


notify_api = FastAPI(title="Telegram Bot Gateway")


@notify_api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@notify_api.post("/notify")
async def notify(req: Request, body: NotifyPayload) -> dict[str, str]:
    settings: Settings = req.app.state.settings

    auth = req.headers.get("authorization", "")
    bearer = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    header_token = req.headers.get("x-internal-token", "")

    if bearer != settings.internal_api_token and header_token != settings.internal_api_token:
        raise HTTPException(status_code=401, detail="unauthorized")

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    if TELEGRAM_APP is None:
        raise HTTPException(status_code=503, detail="bot not ready")

    chat_id = body.chat_id or settings.default_chat_id
    for part in _split_message(text):
        await TELEGRAM_APP.bot.send_message(chat_id=chat_id, text=part, parse_mode=None)

    return {"ok": "true"}


# ── Telegram command handlers ─────────────────────────────────────────────────

def _is_allowed(update: Update, settings: Settings) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid in settings.allowed_user_ids


async def _deny(update: Update) -> None:
    if update.message:
        await update.message.reply_text(
            "Bu botu kullanmaya yetkiniz yok.", parse_mode=None
        )


async def start_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings):
        await _deny(update)
        return
    if update.message:
        await update.message.reply_text(
            "Komutlar:\n"
            "/help — Bu listeyi göster\n"
            "/services — Kayıtlı servisleri listele\n"
            "/aski_durum — ASKİ kesinti durumu\n"
            "/aski_kontrol — Manuel ASKİ kontrolü başlat",
            parse_mode=None,
        )


async def services_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings):
        await _deny(update)
        return
    if update.message:
        await update.message.reply_text(
            "Kayıtlı servisler:\n\naski-water-watch\n  /aski_durum\n  /aski_kontrol",
            parse_mode=None,
        )


async def aski_durum(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings):
        await _deny(update)
        return
    if update.message is None:
        return
    try:
        data = await _call_service(settings, f"{settings.aski_service_url}/status", "GET")
        await update.message.reply_text(format_aski_status(data), parse_mode=None)
    except Exception:
        logger.exception("aski /status call failed")
        await update.message.reply_text("ASKİ servisi yanıt vermedi.", parse_mode=None)


async def aski_kontrol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings):
        await _deny(update)
        return
    if update.message is None:
        return
    await update.message.reply_text("Kontrol başlatıldı...", parse_mode=None)
    try:
        data = await _call_service(settings, f"{settings.aski_service_url}/check", "POST")
        await update.message.reply_text(format_aski_check(data), parse_mode=None)
    except Exception:
        logger.exception("aski /check call failed")
        await update.message.reply_text("ASKİ servisi yanıt vermedi.", parse_mode=None)


async def _call_service(settings: Settings, url: str, method: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.request(
            method,
            url,
            headers={"authorization": f"Bearer {settings.internal_api_token}"},
        )
        response.raise_for_status()
        return response.json()


# ── Message formatting ────────────────────────────────────────────────────────

def format_aski_status(status: dict[str, Any]) -> str:
    checked_at = _fmt_dt(status.get("lastCheckedAt"))

    if status.get("lastError"):
        return (
            "ASKİ kesinti durumu\n\n"
            f"Son kontrolde hata oluştu.\n"
            f"Hata: {status['lastError']}\n"
            f"Son kontrol: {checked_at}"
        )

    match = status.get("lastMatch")
    if not match:
        return (
            "ASKİ kesinti durumu\n\n"
            f"Aktif su kesintisi yok.\n"
            f"Son kontrol: {checked_at}"
        )

    return _fmt_outage("ASKİ kesinti durumu", match, checked_at)


def format_aski_check(result: dict[str, Any]) -> str:
    checked_at = _fmt_dt(result.get("lastCheckedAt"))
    match = result.get("match")

    if not match:
        return (
            "Manuel kontrol tamamlandı.\n\n"
            f"Aktif su kesintisi yok.\n"
            f"Kontrol: {checked_at}"
        )

    return _fmt_outage("Manuel kontrol tamamlandı.", match, checked_at)


def _fmt_outage(header: str, outage: dict[str, Any], checked_at: str) -> str:
    return "\n".join([
        header,
        "",
        "Aktif su kesintisi var.",
        "",
        f"Arıza tarihi:    {outage.get('faultDate') or '-'}",
        f"Tahmini bitiş:   {outage.get('repairDate') or '-'}",
        f"Etkilenen yerler: {outage.get('affectedPlaces') or '-'}",
        f"Detay:           {outage.get('detail') or '-'}",
        "",
        f"Son kontrol: {checked_at}",
    ])


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "yok"
    try:
        dt = datetime.fromisoformat(iso).astimezone(_TZ_TR)
        return dt.strftime("%d.%m.%Y %H:%M") + " (TR)"
    except ValueError:
        return iso


def _split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < 1:
            split_at = limit
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        parts.append(remaining)
    return parts


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    global TELEGRAM_APP

    settings = Settings.from_env()

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["settings"] = settings
    TELEGRAM_APP = application

    application.add_handler(CommandHandler("start", start_help))
    application.add_handler(CommandHandler("help", start_help))
    application.add_handler(CommandHandler("services", services_command))
    application.add_handler(CommandHandler("aski_durum", aski_durum))
    application.add_handler(CommandHandler("aski_kontrol", aski_kontrol))

    notify_api.state.settings = settings

    server = uvicorn.Server(
        uvicorn.Config(
            notify_api,
            host=settings.notify_host,
            port=settings.notify_port,
            log_level=os.getenv("UVICORN_LOG_LEVEL", "info"),
        )
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await application.initialize()
    await application.start()
    if application.updater is None:
        raise RuntimeError("Telegram updater is not available")
    await application.updater.start_polling()

    server_task = asyncio.create_task(server.serve())
    try:
        await stop_event.wait()
    finally:
        server.should_exit = True
        await server_task
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
