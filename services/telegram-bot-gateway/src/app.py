from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta, timezone
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

_TZ_TR = timezone(timedelta(hours=3))
_TR_DAYS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
_TR_MONTHS = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]


def _today() -> Date:
    return datetime.now(_TZ_TR).date()


def _tr_date_label(d: Date) -> str:
    return f"{d.day} {_TR_MONTHS[d.month - 1]} {_TR_DAYS[d.weekday()]}"


def _next_week_dates() -> list[Date]:
    now = datetime.now(_TZ_TR)
    today = now.date()
    days_to_sunday = (6 - today.weekday()) % 7
    if days_to_sunday == 0 and now.hour >= 22:
        days_to_sunday = 7
    upcoming_sunday = today + timedelta(days=days_to_sunday)
    return [upcoming_sunday + timedelta(days=i + 1) for i in range(7)]


def _next_sunday_22_label() -> str:
    now = datetime.now(_TZ_TR)
    days_to_sunday = (6 - now.date().weekday()) % 7
    if days_to_sunday == 0 and now.hour >= 22:
        days_to_sunday = 7
    target = now.date() + timedelta(days=days_to_sunday)
    fire = datetime(target.year, target.month, target.day, 22, 0, 0)
    return f"{fire.day} {_TR_MONTHS[fire.month - 1]} {_TR_DAYS[fire.weekday()]} 22:00"


async def _date_buttons(settings: Settings, user_id: int, sport: str, cb_prefix: str) -> list | None:
    """Returns button rows for dates with slots. Returns None if credentials missing, [] if no slots."""
    try:
        resp = await _call_service(
            settings,
            f"{settings.metu_service_url}/available-dates?user_id={user_id}&sport={sport}",
            "GET",
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        logger.exception("available-dates call failed")
        return []
    except Exception:
        logger.exception("available-dates call failed")
        return []
    buttons = []
    for item in resp.get("dates", []):
        d = Date.fromisoformat(item["date"])
        label = f"{_tr_date_label(d)} ({item['slot_count']} slot)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"{cb_prefix}:{item['date']}")])
    return buttons

TELEGRAM_APP: Application | None = None

# ConversationHandler states for /metu_giris
_METU_GIRIS_ID = 1
_METU_GIRIS_PW = 2
_METU_GIRIS_CAT = 3


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    default_chat_id: int
    internal_api_token: str
    aski_service_url: str | None
    metu_service_url: str | None
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

        aski_url = os.getenv("ASKI_SERVICE_URL", "").strip().rstrip("/") or None
        metu_url = os.getenv("METU_SERVICE_URL", "").strip().rstrip("/") or None

        return cls(
            telegram_bot_token=token,
            allowed_user_ids=allowed,
            default_chat_id=int(default_chat_raw),
            internal_api_token=internal_token,
            aski_service_url=aski_url,
            metu_service_url=metu_url,
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


# ── Shared helpers ────────────────────────────────────────────────────────────

def _is_allowed(update: Update, settings: Settings) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid in settings.allowed_user_ids


async def _deny(update: Update) -> None:
    if update.message:
        await update.message.reply_text("Bu botu kullanmaya yetkiniz yok.", parse_mode=None)


async def _call_service(settings: Settings, url: str, method: str, json: dict | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.request(
            method,
            url,
            headers={"authorization": f"Bearer {settings.internal_api_token}"},
            json=json,
        )
        response.raise_for_status()
        return response.json()


# ── ASKİ command handlers ─────────────────────────────────────────────────────

async def start_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings):
        await _deny(update)
        return
    if update.message:
        lines = [
            "Komutlar:",
            "/help — Bu listeyi göster",
            "/services — Kayıtlı servisleri listele",
        ]
        if settings.aski_service_url:
            lines += [
                "/aski_durum — ASKİ kesinti durumu",
                "/aski_kontrol — Manuel ASKİ kontrolü başlat",
            ]
        if settings.metu_service_url:
            lines += [
                "",
                "METU Spor:",
                "/metu_giris — METU kimlik bilgilerini kaydet",
                "/musait_slotlar — Müsait kortları listele",
                "/rezervasyon_yap — Rezervasyon oluştur",
                "/rezervasyonlarim — Mevcut rezervasyonlarım",
                "/oto_rezervasyon — Otomatik rezervasyon kur",
            ]
        await update.message.reply_text("\n".join(lines), parse_mode=None)


async def services_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings):
        await _deny(update)
        return
    if update.message:
        parts = []
        if settings.aski_service_url:
            parts.append("aski-water-watch\n  /aski_durum\n  /aski_kontrol")
        if settings.metu_service_url:
            parts.append("metu-sports-watcher\n  /musait_slotlar\n  /rezervasyon_yap\n  /rezervasyonlarim\n  /oto_rezervasyon")
        text = "Kayıtlı servisler:\n\n" + "\n\n".join(parts) if parts else "Kayıtlı servis yok."
        await update.message.reply_text(text, parse_mode=None)


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


# ── METU command handlers ─────────────────────────────────────────────────────

# /metu_giris — 2-step ConversationHandler

async def metu_giris_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings):
        await _deny(update)
        return ConversationHandler.END
    if update.message:
        await update.message.reply_text(
            "METU ID'nizi girin (öğrenci/personel numarası):", parse_mode=None
        )
    return _METU_GIRIS_ID


async def metu_giris_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return _METU_GIRIS_ID
    context.user_data["metu_pending_id"] = update.message.text.strip()
    await update.message.reply_text(
        "Şifrenizi girin:\n(Mesaj gönderildikten hemen sonra silinmesini önermiyorum — özel sohbette kullanın)",
        parse_mode=None,
    )
    return _METU_GIRIS_PW


async def metu_giris_get_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return _METU_GIRIS_PW

    context.user_data["metu_pending_pw"] = update.message.text.strip()
    keyboard = [
        [
            InlineKeyboardButton("Öğrenci", callback_data="ms:cat:student"),
            InlineKeyboardButton("Personel", callback_data="ms:cat:personnel"),
        ],
        [
            InlineKeyboardButton("Mezun", callback_data="ms:cat:alumni"),
        ],
    ]
    await update.message.reply_text(
        "Hesap kategoriniz?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=None,
    )
    return _METU_GIRIS_CAT


async def metu_giris_get_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings: Settings = context.application.bot_data["settings"]
    query = update.callback_query
    if query is None:
        return _METU_GIRIS_CAT
    await query.answer()

    category = query.data.split(":")[2]  # ms:cat:student → student
    metu_id = context.user_data.pop("metu_pending_id", "")
    password = context.user_data.pop("metu_pending_pw", "")
    user_id = update.effective_user.id if update.effective_user else 0

    if not metu_id or not password:
        await query.edit_message_text("Bir hata oluştu, /metu_giris ile tekrar deneyin.", parse_mode=None)
        return ConversationHandler.END

    try:
        await _call_service(
            settings,
            f"{settings.metu_service_url}/credentials",
            "PUT",
            json={"user_id": user_id, "metu_id": metu_id, "password": password, "category": category},
        )
        await query.edit_message_text(
            "Kimlik bilgileri kaydedildi.\n/musait_slotlar ile kort sorgulayabilirsin.",
            parse_mode=None,
        )
    except Exception:
        logger.exception("metu /credentials PUT failed")
        await query.edit_message_text("Kimlik bilgileri kaydedilemedi. Servis yanıt vermedi.", parse_mode=None)

    return ConversationHandler.END


async def metu_giris_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("metu_pending_id", None)
    context.user_data.pop("metu_pending_pw", None)
    if update.message:
        await update.message.reply_text("İptal edildi.", parse_mode=None)
    return ConversationHandler.END


# text input during booking player collection

async def metu_booking_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Determine which flow is active
    booking = context.user_data.get("metu_booking")
    ow = context.user_data.get("metu_ow")
    if booking and booking.get("collecting_player"):
        await _collect_player_text(update, context, booking, "metu_booking", _after_booking_players)
    elif ow and ow.get("collecting_player"):
        await _collect_player_text(update, context, ow, "metu_ow", _after_ow_players)


async def _collect_player_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    key: str,
    after_fn,
) -> None:
    if update.message is None or not update.message.text:
        return

    text = update.message.text.strip()
    current_player = state["collecting_player"]
    parts = [p.strip() for p in text.split("|")]

    if len(parts) < 2 or not parts[0] or not parts[1]:
        await update.message.reply_text(
            "Format hatalı. Lütfen tekrar deneyin:\n"
            "Ad Soyad | METU_ID | kategori\n"
            "Örnek: Ahmet Yılmaz | e123456 | ogrenci",
            parse_mode=None,
        )
        return

    _CAT_MAP = {
        "ogrenci": "student", "öğrenci": "student", "student": "student",
        "personel": "personnel", "personnel": "personnel",
        "mezun": "alumni", "alumni": "alumni",
        "misafir": "guest", "guest": "guest",
    }
    raw_cat = parts[2].strip().lower() if len(parts) > 2 else "student"
    state["players"].append({
        "name": parts[0],
        "metu_id": parts[1],
        "category": _CAT_MAP.get(raw_cat, raw_cat),
    })

    next_player = current_player + 1
    if next_player <= state["player_count"]:
        state["collecting_player"] = next_player
        context.user_data[key] = state
        await update.message.reply_text(
            f"Oyuncu {next_player} bilgilerini girin:\n"
            "Format: Ad Soyad | METU_ID | kategori\n"
            "Örnek: Ahmet Yılmaz | e123456 | ogrenci",
            parse_mode=None,
        )
    else:
        state.pop("collecting_player", None)
        context.user_data[key] = state
        await after_fn(update, context, state)


async def _after_booking_players(update: Update, context: ContextTypes.DEFAULT_TYPE, booking: dict) -> None:
    slots = context.user_data.get("metu_slots", [])
    slot_idx = booking.get("slot_idx", 0)
    slot = slots[slot_idx] if slot_idx < len(slots) else {}
    keyboard = [[
        InlineKeyboardButton("Onayla", callback_data="ms:cf"),
        InlineKeyboardButton("İptal", callback_data="ms:x"),
    ]]
    await update.message.reply_text(
        format_reservation_confirm(slot, booking),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=None,
    )


async def _after_ow_players(update: Update, context: ContextTypes.DEFAULT_TYPE, ow: dict) -> None:
    d = Date.fromisoformat(ow.get("target_date", str(_today())))
    fire_label = _next_sunday_22_label()
    keyboard = [[
        InlineKeyboardButton("Onayla", callback_data="ms:ow:cf"),
        InlineKeyboardButton("İptal", callback_data="ms:x"),
    ]]
    await update.message.reply_text(
        f"Oto-rezervasyon özeti:\n\n"
        f"Spor:  {ow.get('sport','?').title()}\n"
        f"Tarih: {_tr_date_label(d)}\n"
        f"Saat:  {ow.get('target_hour',0):02d}:00\n"
        f"Oyuncu: {ow.get('player_count',1)}\n\n"
        f"Ateşlenme: {fire_label}\n\n"
        "Onaylıyor musunuz?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=None,
    )


# /musait_slotlar [tarih] — listele ve inline keyboard sun

async def musait_slotlar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings):
        await _deny(update)
        return
    if update.message is None:
        return

    keyboard = [
        [
            InlineKeyboardButton("Padel", callback_data="ms:qsp:padel"),
            InlineKeyboardButton("Pickleball", callback_data="ms:qsp:pickleball"),
        ],
    ]
    await update.message.reply_text(
        "Hangi spor için müsait kortları göstereyim?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=None,
    )


# /rezervasyon_yap — inline keyboard akışı başlat

async def rezervasyon_yap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings):
        await _deny(update)
        return
    if update.message is None:
        return

    keyboard = [
        [
            InlineKeyboardButton("Padel", callback_data="ms:sp:padel"),
            InlineKeyboardButton("Pickleball", callback_data="ms:sp:pickleball"),
        ],
        [InlineKeyboardButton("İptal", callback_data="ms:x")],
    ]
    await update.message.reply_text(
        "Rezervasyon — Spor seçin:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=None,
    )


# /rezervasyonlarim

async def rezervasyonlarim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings):
        await _deny(update)
        return
    if update.message is None:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    await update.message.reply_text("Rezervasyonlar yükleniyor...", parse_mode=None)
    try:
        data = await _call_service(
            settings,
            f"{settings.metu_service_url}/my-reservations?user_id={user_id}",
            "GET",
        )
        text = format_my_reservations(data.get("reservations", []))
        await update.message.reply_text(text, parse_mode=None)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await update.message.reply_text(
                "Kimlik bilgisi bulunamadı. /metu_giris ile giriş yap.", parse_mode=None
            )
        else:
            await update.message.reply_text("METU servisi yanıt vermedi.", parse_mode=None)
    except Exception:
        logger.exception("metu /my-reservations failed")
        await update.message.reply_text("METU servisi yanıt vermedi.", parse_mode=None)


# /oto_rezervasyon — inline keyboard ile yapılandır

async def oto_rezervasyon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings):
        await _deny(update)
        return
    if update.message is None:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    try:
        data = await _call_service(
            settings,
            f"{settings.metu_service_url}/auto-watch?user_id={user_id}",
            "GET",
        )
    except Exception:
        data = {"configured": False}

    keyboard = [
        [
            InlineKeyboardButton("Padel", callback_data="ms:ow:sp:padel"),
            InlineKeyboardButton("Pickleball", callback_data="ms:ow:sp:pickleball"),
        ],
    ]
    if data.get("configured"):
        targets = data.get("targets", [])
        target_lines = []
        for t in targets:
            try:
                d = Date.fromisoformat(t["date"])
                target_lines.append(f"  • {_tr_date_label(d)} {t['hour']:02d}:00")
            except Exception:
                pass
        cfg_text = (
            f"Mevcut: {data.get('sport','?').title()}\n"
            + "\n".join(target_lines) + "\n"
            f"Ateşlenme: {data.get('fire_at_label','?')}"
        )
        keyboard.append([InlineKeyboardButton("Mevcut kurulumu sil", callback_data="ms:ow:del")])
        keyboard.append([InlineKeyboardButton("İptal", callback_data="ms:x")])
        header = f"Otomatik rezervasyon\n\n{cfg_text}\n\nYeniden kurmak için spor seçin:"
    else:
        keyboard.append([InlineKeyboardButton("İptal", callback_data="ms:x")])
        header = (
            f"Otomatik rezervasyon kurulumu\n\n"
            f"Ateşlenme zamanı: {_next_sunday_22_label()}\n\n"
            "Spor seçin:"
        )

    await update.message.reply_text(
        header, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None
    )


# ── Inline keyboard callback handler ─────────────────────────────────────────

async def metu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    logger.info("METU callback received: data=%r user=%s", query.data, update.effective_user.id if update.effective_user else "?")
    await query.answer()

    settings: Settings = context.application.bot_data["settings"]
    data = query.data or ""
    user_id = update.effective_user.id if update.effective_user else 0

    # ── Cancel ────────────────────────────────────────────────────────────────
    if data == "ms:x":
        context.user_data.pop("metu_booking", None)
        context.user_data.pop("metu_ow", None)
        await query.edit_message_text("İptal edildi.", parse_mode=None)
        return

    # ── Slot query: spor seçimi (musait_slotlar) ─────────────────────────────
    if data.startswith("ms:qsp:"):
        sport = data.split(":")[2]
        await query.edit_message_text(f"{sport.title()} — tarihler kontrol ediliyor...", parse_mode=None)
        buttons = await _date_buttons(settings, user_id, sport, f"ms:q:{sport}")
        if buttons is None:
            await query.edit_message_text("Kimlik bilgisi bulunamadı. /metu_giris ile giriş yap.", parse_mode=None)
            return
        if not buttons:
            await query.edit_message_text(
                f"{sport.title()} — Önümüzdeki 7 gün içinde müsait slot bulunamadı.", parse_mode=None
            )
            return
        buttons.append([InlineKeyboardButton("İptal", callback_data="ms:x")])
        await query.edit_message_text(
            f"{sport.title()} — Tarih seçin:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=None,
        )
        return

    # ── Slot query: ms:q:sport:YYYY-MM-DD ────────────────────────────────────
    if data.startswith("ms:q:"):
        parts = data.split(":")
        sport = parts[2]
        target_date = parts[3]
        await query.edit_message_text(f"Slotlar yükleniyor ({sport.title()}, {target_date})...", parse_mode=None)
        try:
            resp = await _call_service(
                settings,
                f"{settings.metu_service_url}/slots?user_id={user_id}&sport={sport}&date={target_date}",
                "GET",
            )
            slots = resp.get("slots", [])
            text, kb = format_slots(slots, sport, target_date)
            await query.edit_message_text(text, reply_markup=kb, parse_mode=None)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                await query.edit_message_text("Kimlik bilgisi bulunamadı. /metu_giris ile giriş yap.", parse_mode=None)
            else:
                await query.edit_message_text("METU servisi yanıt vermedi.", parse_mode=None)
        except Exception:
            logger.exception("metu /slots failed")
            await query.edit_message_text("METU servisi yanıt vermedi.", parse_mode=None)
        return

    # ── Rezervasyon akışı: spor seç ──────────────────────────────────────────
    if data.startswith("ms:sp:"):
        sport = data.split(":")[2]
        context.user_data["metu_booking"] = {"sport": sport}
        await query.edit_message_text(f"{sport.title()} — tarihler kontrol ediliyor...", parse_mode=None)
        buttons = await _date_buttons(settings, user_id, sport, "ms:dt")
        if buttons is None:
            await query.edit_message_text("Kimlik bilgisi bulunamadı. /metu_giris ile giriş yap.", parse_mode=None)
            return
        if not buttons:
            await query.edit_message_text(
                f"{sport.title()} — Önümüzdeki 7 gün içinde müsait slot bulunamadı.", parse_mode=None
            )
            return
        buttons.append([InlineKeyboardButton("İptal", callback_data="ms:x")])
        await query.edit_message_text(
            f"Rezervasyon — {sport.title()} — Tarih seçin:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=None,
        )
        return

    # ── Rezervasyon akışı: tarih seç ms:dt:YYYY-MM-DD ────────────────────────
    if data.startswith("ms:dt:"):
        target_date = data[6:]
        booking = context.user_data.get("metu_booking", {})
        booking["date"] = target_date
        context.user_data["metu_booking"] = booking
        sport = booking.get("sport", "padel")
        await query.edit_message_text(f"Slotlar yükleniyor ({sport.title()}, {target_date})...", parse_mode=None)
        try:
            resp = await _call_service(
                settings,
                f"{settings.metu_service_url}/slots?user_id={user_id}&sport={sport}&date={target_date}",
                "GET",
            )
            slots = resp.get("slots", [])
            context.user_data["metu_slots"] = slots
            text, kb = format_slots_for_booking(slots, sport, target_date)
            await query.edit_message_text(text, reply_markup=kb, parse_mode=None)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                await query.edit_message_text("Kimlik bilgisi bulunamadı. /metu_giris ile giriş yap.", parse_mode=None)
            else:
                await query.edit_message_text("METU servisi yanıt vermedi.", parse_mode=None)
        except Exception:
            logger.exception("metu /slots failed")
            await query.edit_message_text("METU servisi yanıt vermedi.", parse_mode=None)
        return

    # ── Rezervasyon akışı: slot seç ──────────────────────────────────────────
    if data.startswith("ms:sl:"):
        idx = int(data.split(":")[2])
        slots = context.user_data.get("metu_slots", [])
        if idx >= len(slots):
            await query.edit_message_text("Geçersiz slot.", parse_mode=None)
            return
        slot = slots[idx]
        booking = context.user_data.get("metu_booking", {})
        booking["slot_idx"] = idx
        booking["slot_id"] = slot["slotId"]
        context.user_data["metu_booking"] = booking

        keyboard = [
            [
                InlineKeyboardButton("2 Oyuncu", callback_data="ms:pl:2"),
                InlineKeyboardButton("3 Oyuncu", callback_data="ms:pl:3"),
                InlineKeyboardButton("4 Oyuncu", callback_data="ms:pl:4"),
            ],
            [InlineKeyboardButton("İptal", callback_data="ms:x")],
        ]
        await query.edit_message_text(
            f"Seçilen slot: {slot.get('label', slot['slotId'])}\n\nOyuncu sayısı?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=None,
        )
        return

    # ── Rezervasyon akışı: oyuncu sayısı ─────────────────────────────────────
    if data.startswith("ms:pl:"):
        player_count = int(data.split(":")[2])
        booking = context.user_data.get("metu_booking", {})
        booking["player_count"] = player_count
        booking["players"] = []
        context.user_data["metu_booking"] = booking

        slots = context.user_data.get("metu_slots", [])
        slot_idx = booking.get("slot_idx", 0)
        slot = slots[slot_idx] if slot_idx < len(slots) else {}

        if player_count > 1:
            booking["collecting_player"] = 2
            context.user_data["metu_booking"] = booking
            await query.edit_message_text(
                f"Seçilen: {slot.get('label', booking.get('slot_id', '?'))}\n{player_count} oyuncu\n\n"
                "Oyuncu 2 bilgilerini girin (tek mesaj olarak):\n"
                "Format: Ad Soyad | Kimlik | kategori\n"
                "Kategori: ogrenci / personel / mezun / misafir\n"
                "  ogrenci/personel → METU ID (eXXXXXX)\n"
                "  mezun/misafir   → TC Kimlik No\n"
                "Örnek: Ahmet Yılmaz | e123456 | ogrenci\n\n"
                "İptal için aşağıdaki butona basın.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("İptal", callback_data="ms:x")]]),
                parse_mode=None,
            )
        else:
            keyboard = [[
                InlineKeyboardButton("Onayla", callback_data="ms:cf"),
                InlineKeyboardButton("İptal", callback_data="ms:x"),
            ]]
            await query.edit_message_text(
                format_reservation_confirm(slot, booking),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=None,
            )
        return

    # ── Rezervasyon akışı: onayla ─────────────────────────────────────────────
    if data == "ms:cf":
        booking = context.user_data.get("metu_booking", {})
        if booking.get("collecting_player"):
            await query.answer("Önce tüm oyuncu bilgilerini girin.", show_alert=True)
            return
        booking = context.user_data.pop("metu_booking", {})
        if not booking.get("slot_id"):
            await query.edit_message_text("Rezervasyon bilgisi bulunamadı. Lütfen /rezervasyon_yap ile tekrar başlatın.", parse_mode=None)
            return
        await query.edit_message_text("Rezervasyon yapılıyor...", parse_mode=None)
        try:
            result = await _call_service(
                settings,
                f"{settings.metu_service_url}/reserve",
                "POST",
                json={
                    "user_id": user_id,
                    "slot_id": booking.get("slot_id", ""),
                    "players": booking.get("players", []),
                },
            )
            await query.edit_message_text(format_reservation_confirmed(result), parse_mode=None)
        except httpx.HTTPStatusError as e:
            logger.exception("metu /reserve HTTP error %s: %s", e.response.status_code, e.response.text)
            try:
                err = e.response.json().get("detail", e.response.text)
            except Exception:
                err = e.response.text
            await query.edit_message_text(f"Rezervasyon yapılamadı: {err}", parse_mode=None)
        except Exception:
            logger.exception("metu /reserve failed")
            await query.edit_message_text("Rezervasyon yapılamadı. METU servisi yanıt vermedi.", parse_mode=None)
        return

    # ── Oto-rezervasyon: spor seç → gelecek hafta tarihleri ──────────────────
    if data.startswith("ms:ow:sp:"):
        sport = data.split(":")[3]
        context.user_data["metu_ow"] = {"sport": sport}
        dates = _next_week_dates()
        buttons = [
            [InlineKeyboardButton(_tr_date_label(d), callback_data=f"ms:ow:date:{d}")]
            for d in dates
        ]
        buttons.append([InlineKeyboardButton("İptal", callback_data="ms:x")])
        await query.edit_message_text(
            f"Oto-rezervasyon — {sport.title()} — Hangi gün?",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=None,
        )
        return

    # ── Oto-rezervasyon: tarih seç → saat seç ────────────────────────────────
    if data.startswith("ms:ow:date:"):
        target_date = data[11:]
        ow = context.user_data.get("metu_ow", {})
        ow["target_date"] = target_date
        context.user_data["metu_ow"] = ow
        hours = list(range(9, 23))
        buttons = []
        for i in range(0, len(hours), 2):
            row = [InlineKeyboardButton(f"{h:02d}:00", callback_data=f"ms:ow:h:{h}") for h in hours[i:i+2]]
            buttons.append(row)
        buttons.append([InlineKeyboardButton("İptal", callback_data="ms:x")])
        d = Date.fromisoformat(target_date)
        await query.edit_message_text(
            f"Oto-rezervasyon — {_tr_date_label(d)} — Saat seçin:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=None,
        )
        return

    # ── Oto-rezervasyon: saat seç → oyuncu sayısı ────────────────────────────
    if data.startswith("ms:ow:h:"):
        hour = int(data.split(":")[3])
        ow = context.user_data.get("metu_ow", {})
        ow["target_hour"] = hour
        context.user_data["metu_ow"] = ow
        keyboard = [
            [
                InlineKeyboardButton("1 Oyuncu", callback_data="ms:ow:pl:1"),
                InlineKeyboardButton("2 Oyuncu", callback_data="ms:ow:pl:2"),
            ],
            [
                InlineKeyboardButton("3 Oyuncu", callback_data="ms:ow:pl:3"),
                InlineKeyboardButton("4 Oyuncu", callback_data="ms:ow:pl:4"),
            ],
            [InlineKeyboardButton("İptal", callback_data="ms:x")],
        ]
        await query.edit_message_text(
            f"Oto-rezervasyon — {ow.get('target_date','?')} {hour:02d}:00 — Oyuncu sayısı:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=None,
        )
        return

    # ── Oto-rezervasyon: oyuncu sayısı → oyuncu bilgisi veya onayla ──────────
    if data.startswith("ms:ow:pl:"):
        player_count = int(data.split(":")[3])
        ow = context.user_data.get("metu_ow", {})
        ow["player_count"] = player_count
        ow["players"] = []
        context.user_data["metu_ow"] = ow

        if player_count > 1:
            ow["collecting_player"] = 2
            context.user_data["metu_ow"] = ow
            await query.edit_message_text(
                f"Oyuncu 2 bilgilerini girin (tek mesaj):\n"
                "Format: Ad Soyad | METU_ID | kategori\n"
                "Kategori: ogrenci / personel / mezun / misafir\n"
                "Örnek: Ahmet Yılmaz | e123456 | ogrenci",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("İptal", callback_data="ms:x")]]),
                parse_mode=None,
            )
        else:
            d = Date.fromisoformat(ow.get("target_date", str(_today())))
            fire_label = _next_sunday_22_label()
            keyboard = [[
                InlineKeyboardButton("Onayla", callback_data="ms:ow:cf"),
                InlineKeyboardButton("İptal", callback_data="ms:x"),
            ]]
            await query.edit_message_text(
                f"Oto-rezervasyon özeti:\n\n"
                f"Spor:  {ow.get('sport','?').title()}\n"
                f"Tarih: {_tr_date_label(d)}\n"
                f"Saat:  {ow.get('target_hour',0):02d}:00\n\n"
                f"Ateşlenme: {fire_label}\n\n"
                "Onaylıyor musunuz?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=None,
            )
        return

    # ── Oto-rezervasyon: bu hedefi listeye ekle, başka ekle? ─────────────────
    if data == "ms:ow:cf":
        ow = context.user_data.get("metu_ow", {})
        if ow.get("collecting_player"):
            await query.answer("Önce tüm oyuncu bilgilerini girin.", show_alert=True)
            return
        targets = ow.setdefault("targets", [])
        targets.append({"date": ow.get("target_date", ""), "hour": ow.get("target_hour", 0)})
        ow.pop("target_date", None)
        ow.pop("target_hour", None)
        context.user_data["metu_ow"] = ow

        lines = [f"Eklendi: {ow.get('sport','?').title()}"]
        for t in targets:
            d = Date.fromisoformat(t["date"])
            lines.append(f"  • {_tr_date_label(d)} {t['hour']:02d}:00")
        lines.append(f"\nAteşlenme: {_next_sunday_22_label()}")
        lines.append("\nBaşka gün/saat eklemek ister misin?")

        keyboard = [
            [InlineKeyboardButton("Evet, başka ekle", callback_data="ms:ow:more")],
            [InlineKeyboardButton("Hayır, kaydet", callback_data="ms:ow:save")],
            [InlineKeyboardButton("İptal", callback_data="ms:x")],
        ]
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=None,
        )
        return

    # ── Oto-rezervasyon: başka gün ekle → tarih seçimine dön ─────────────────
    if data == "ms:ow:more":
        ow = context.user_data.get("metu_ow", {})
        sport = ow.get("sport", "padel")
        dates = _next_week_dates()
        booked = {t["date"] for t in ow.get("targets", [])}
        buttons = [
            [InlineKeyboardButton(_tr_date_label(d), callback_data=f"ms:ow:date:{d}")]
            for d in dates if str(d) not in booked
        ]
        if not buttons:
            await query.edit_message_text("Tüm günler zaten eklendi.", parse_mode=None)
            return
        buttons.append([InlineKeyboardButton("İptal", callback_data="ms:x")])
        await query.edit_message_text(
            f"Oto-rezervasyon — {sport.title()} — Hangi gün daha ekleyelim?",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=None,
        )
        return

    # ── Oto-rezervasyon: kaydet ───────────────────────────────────────────────
    if data == "ms:ow:save":
        ow = context.user_data.pop("metu_ow", {})
        targets = ow.get("targets", [])
        if not targets:
            await query.edit_message_text("Hiç hedef eklenmedi.", parse_mode=None)
            return
        try:
            await _call_service(
                settings,
                f"{settings.metu_service_url}/auto-watch",
                "PUT",
                json={
                    "user_id": user_id,
                    "sport": ow.get("sport", "padel"),
                    "targets": targets,
                    "players": ow.get("players", []),
                },
            )
            lines = [f"Oto-rezervasyon kuruldu!\n\nSpor: {ow.get('sport','?').title()}"]
            for t in targets:
                d = Date.fromisoformat(t["date"])
                lines.append(f"  • {_tr_date_label(d)} {t['hour']:02d}:00")
            lines.append(f"\nAteşlenme: {_next_sunday_22_label()}")
            await query.edit_message_text("\n".join(lines), parse_mode=None)
        except Exception:
            logger.exception("metu /auto-watch PUT failed")
            await query.edit_message_text("Oto-rezervasyon kaydedilemedi.", parse_mode=None)
        return

    # ── Oto-rezervasyon: sil ─────────────────────────────────────────────────
    if data == "ms:ow:del":
        try:
            await _call_service(
                settings,
                f"{settings.metu_service_url}/auto-watch?user_id={user_id}",
                "DELETE",
            )
            await query.edit_message_text("Oto-rezervasyon silindi.", parse_mode=None)
        except Exception:
            logger.exception("metu /auto-watch DELETE failed")
            await query.edit_message_text("Silinemedi.", parse_mode=None)
        return


# ── METU formatters ───────────────────────────────────────────────────────────

def format_slots(slots: list, sport: str, date: str) -> tuple[str, InlineKeyboardMarkup]:
    if not slots:
        return f"{sport.title()} — {date}\n\nMüsait slot bulunamadı.", InlineKeyboardMarkup([])
    lines = [f"{sport.title()} — {date}\n\nMüsait slotlar:"]
    for s in slots:
        lines.append(f"• {s.get('label', s.get('slotId', '?'))}")
    text = "\n".join(lines)
    return text, InlineKeyboardMarkup([])


def format_slots_for_booking(slots: list, sport: str, date: str) -> tuple[str, InlineKeyboardMarkup]:
    if not slots:
        keyboard = [[InlineKeyboardButton("İptal", callback_data="ms:x")]]
        return f"{sport.title()} — {date}\n\nMüsait slot bulunamadı.", InlineKeyboardMarkup(keyboard)

    buttons = []
    for i, s in enumerate(slots):
        label = s.get("label", s.get("slotId", f"Slot {i+1}"))
        buttons.append([InlineKeyboardButton(label, callback_data=f"ms:sl:{i}")])
    buttons.append([InlineKeyboardButton("İptal", callback_data="ms:x")])
    text = f"{sport.title()} — {date}\n\nSlot seçin:"
    return text, InlineKeyboardMarkup(buttons)


def format_reservation_confirm(slot: dict, booking: dict) -> str:
    label = slot.get("label", booking.get("slot_id", "?"))
    sport = booking.get("sport", "?").title()
    date = booking.get("date", "?")
    player_count = booking.get("player_count", 1)
    lines = [
        "Rezervasyon özeti:\n",
        f"Spor:  {sport}",
        f"Tarih: {date}",
        f"Slot:  {label}",
        f"Oyuncu sayısı: {player_count}",
    ]
    for i, p in enumerate(booking.get("players", []), start=2):
        cat = p.get("category", "student")
        lines.append(f"Oyuncu {i}: {p.get('name', '?')} ({p.get('metu_id', '?')}, {cat})")
    lines.append("\nOnaylıyor musunuz?")
    return "\n".join(lines)


def format_reservation_confirmed(result: dict) -> str:
    if not result.get("ok"):
        return "Rezervasyon başarısız."
    lines = [
        "Rezervasyon oluşturuldu!\n",
        f"Spor:  {result.get('sport', '?').title()}",
        f"Tarih: {result.get('date', '?')}",
    ]
    if result.get("slotLabel"):
        lines.append(f"Saat:  {result['slotLabel']}")
    if result.get("courtName"):
        lines.append(f"Kort:  {result['courtName']}")
    return "\n".join(lines)


def format_my_reservations(reservations: list) -> str:
    if not reservations:
        return "Aktif rezervasyon bulunamadı."
    lines = ["Rezervasyonlarım:\n"]
    for r in reservations:
        lines.append(
            f"• {r.get('sport','?').title()} — {r.get('date','?')} "
            f"{r.get('timeFrom','?')}–{r.get('timeTo','?')} / {r.get('courtName','?')} "
            f"[{r.get('status','?')}]"
        )
    return "\n".join(lines)


# ── ASKİ formatters ───────────────────────────────────────────────────────────

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

    # Debug: log every update
    async def _log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info("UPDATE type=%s data=%r",
                    type(update.effective_message or update.callback_query).__name__,
                    getattr(update.callback_query, 'data', None) or
                    getattr(update.effective_message, 'text', None))
    application.add_handler(MessageHandler(filters.ALL, _log_update), group=-1)
    application.add_handler(CallbackQueryHandler(_log_update), group=-1)

    # Core handlers
    application.add_handler(CommandHandler("start", start_help))
    application.add_handler(CommandHandler("help", start_help))
    application.add_handler(CommandHandler("services", services_command))

    # ASKİ handlers
    if settings.aski_service_url:
        application.add_handler(CommandHandler("aski_durum", aski_durum))
        application.add_handler(CommandHandler("aski_kontrol", aski_kontrol))

    # METU handlers
    if settings.metu_service_url:
        metu_giris_conv = ConversationHandler(
            entry_points=[CommandHandler("metu_giris", metu_giris_start)],
            states={
                _METU_GIRIS_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, metu_giris_get_id)],
                _METU_GIRIS_PW: [MessageHandler(filters.TEXT & ~filters.COMMAND, metu_giris_get_password)],
                _METU_GIRIS_CAT: [CallbackQueryHandler(metu_giris_get_category, pattern="^ms:cat:")],
            },
            fallbacks=[CommandHandler("iptal", metu_giris_cancel)],
        )
        application.add_handler(metu_giris_conv)
        application.add_handler(CommandHandler("musait_slotlar", musait_slotlar))
        application.add_handler(CommandHandler("rezervasyon_yap", rezervasyon_yap))
        application.add_handler(CommandHandler("rezervasyonlarim", rezervasyonlarim))
        application.add_handler(CommandHandler("oto_rezervasyon", oto_rezervasyon))
        application.add_handler(CallbackQueryHandler(metu_callback, pattern="^ms:"))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, metu_booking_text_input))

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
    await application.updater.start_polling(
        allowed_updates=["message", "callback_query", "edited_message"],
    )

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
