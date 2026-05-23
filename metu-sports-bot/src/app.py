from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_TZ_TR = timezone(timedelta(hours=3))


def _today() -> Date:
    return datetime.now(_TZ_TR).date()


def _next_sunday_22() -> datetime:
    now = datetime.now(_TZ_TR)
    days_to_sunday = (6 - now.weekday()) % 7
    if days_to_sunday == 0 and now.hour >= 22:
        days_to_sunday = 7
    target = now.date() + timedelta(days=days_to_sunday)
    return datetime(target.year, target.month, target.day, 22, 0, 0, tzinfo=_TZ_TR)

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from metu_client import get_available_dates, get_available_slots, get_my_reservations, make_reservation
from state import AutoWatchConfig, CredentialStore


logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


class Settings:
    def __init__(self) -> None:
        self.gateway_notify_url = os.getenv("GATEWAY_NOTIFY_URL", "http://telegram-bot-gateway:8080/notify")
        self.internal_api_token = os.getenv("INTERNAL_API_TOKEN", "").strip()
        self.data_dir = Path(os.getenv("DATA_DIR", "/data"))
        if not self.internal_api_token:
            raise RuntimeError("INTERNAL_API_TOKEN must be set")


# ── Request / Response models ─────────────────────────────────────────────────

class CredentialsBody(BaseModel):
    user_id: int
    metu_id: str
    password: str
    category: str = "student"


class ReserveBody(BaseModel):
    user_id: int
    slot_id: str
    players: list[dict] = []


class AutoWatchBody(BaseModel):
    user_id: int
    sport: str
    targets: list[dict]  # [{"date": "YYYY-MM-DD", "hour": 9}, ...]
    players: list[dict] = []


# ── Auth helper ───────────────────────────────────────────────────────────────

def _require_auth(req: Request, settings: Settings) -> None:
    auth = req.headers.get("authorization", "")
    bearer = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    header_token = req.headers.get("x-internal-token", "")
    if bearer != settings.internal_api_token and header_token != settings.internal_api_token:
        raise HTTPException(status_code=401, detail="unauthorized")


# ── Auto-watcher background task ──────────────────────────────────────────────

async def _auto_watch_loop(settings: Settings, store: CredentialStore) -> None:
    while True:
        now = datetime.now(_TZ_TR)
        for cfg in store.get_all_auto_watchers():
            try:
                fire_at = datetime.fromisoformat(cfg.fire_at)
            except (ValueError, AttributeError):
                continue
            if now >= fire_at:
                try:
                    await _execute_watcher(cfg, settings, store)
                except Exception:
                    logger.exception("Auto-watch execution failed for user %d", cfg.user_id)
                store.delete_auto_watch(cfg.user_id)

        watchers = store.get_all_auto_watchers()
        if not watchers:
            await asyncio.sleep(60)
            continue

        fire_times = []
        for w in watchers:
            try:
                fire_times.append(datetime.fromisoformat(w.fire_at))
            except (ValueError, AttributeError):
                pass

        if not fire_times:
            await asyncio.sleep(60)
            continue

        seconds_until = (min(fire_times) - datetime.now(_TZ_TR)).total_seconds()
        if seconds_until > 30:
            await asyncio.sleep(seconds_until - 20)  # uyandır 20 sn önce
        else:
            await asyncio.sleep(5)


async def _execute_watcher(cfg: AutoWatchConfig, settings: Settings, store: CredentialStore) -> None:
    creds = store.get_credentials(cfg.user_id)
    if not creds:
        return
    metu_id, password, category = creds

    for target in cfg.targets:
        target_date = target.get("date", "")
        target_hour = target.get("hour", 0)
        try:
            slots = await get_available_slots(metu_id, password, cfg.sport, target_date)
            matching = [s for s in slots if s.hour == target_hour] or slots
            if not matching:
                await _notify_gateway(
                    settings,
                    f"{target_date} tarihinde {cfg.sport.title()} için müsait slot bulunamadı.",
                    cfg.user_id,
                )
                continue
            slot = matching[0]
            result = await make_reservation(metu_id, password, slot.slot_id, cfg.players, category)
            text = (
                f"Otomatik rezervasyon yapıldı!\n\n"
                f"Spor:  {cfg.sport.title()}\n"
                f"Kort:  {result.get('courtName', slot.court_name)}\n"
                f"Tarih: {slot.date}\n"
                f"Saat:  {slot.time_from}–{slot.time_to}"
            )
            await _notify_gateway(settings, text, cfg.user_id)
        except Exception:
            logger.exception("Reservation failed for target %s %s", target_date, target_hour)
            await _notify_gateway(
                settings,
                f"{target_date} {target_hour:02d}:00 rezervasyonu başarısız oldu.",
                cfg.user_id,
            )


async def _notify_gateway(settings: Settings, text: str, chat_id: int | None = None) -> None:
    body: dict[str, Any] = {"text": text}
    if chat_id:
        body["chat_id"] = chat_id
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                settings.gateway_notify_url,
                json=body,
                headers={"authorization": f"Bearer {settings.internal_api_token}"},
            )
            resp.raise_for_status()
    except Exception:
        logger.exception("Failed to notify gateway")


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(settings: Settings, store: CredentialStore) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_auto_watch_loop(settings, store))
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="METU Sports Bot", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.put("/credentials")
    async def put_credentials(req: Request, body: CredentialsBody) -> dict:
        _require_auth(req, settings)
        store.set_credentials(body.user_id, body.metu_id, body.password, body.category)
        return {"ok": True}

    @app.delete("/credentials")
    async def delete_credentials(req: Request, user_id: int) -> dict:
        _require_auth(req, settings)
        store.delete_credentials(user_id)
        return {"ok": True}

    @app.get("/available-dates")
    async def available_dates_endpoint(req: Request, user_id: int, sport: str, days: int = 7) -> dict:
        _require_auth(req, settings)
        creds = store.get_credentials(user_id)
        if not creds:
            raise HTTPException(status_code=404, detail="Kimlik bilgisi bulunamadı. /metu_giris ile giriş yap.")
        metu_id, password, _ = creds
        result = await get_available_dates(metu_id, password, sport, days)
        return {"dates": result}

    @app.get("/slots")
    async def slots_endpoint(req: Request, user_id: int, sport: str, date: str | None = None) -> dict:
        _require_auth(req, settings)
        creds = store.get_credentials(user_id)
        if not creds:
            raise HTTPException(status_code=404, detail="Kimlik bilgisi bulunamadı. /metu_giris ile giriş yap.")
        metu_id, password, _ = creds
        target_date = date or str(_today())
        result = await get_available_slots(metu_id, password, sport, target_date)
        return {"slots": [s.to_api_dict() for s in result]}

    @app.post("/reserve")
    async def reserve(req: Request, body: ReserveBody) -> dict:
        _require_auth(req, settings)
        creds = store.get_credentials(body.user_id)
        if not creds:
            raise HTTPException(status_code=404, detail="Kimlik bilgisi bulunamadı. /metu_giris ile giriş yap.")
        metu_id, password, category = creds
        try:
            return await make_reservation(metu_id, password, body.slot_id, body.players, category)
        except httpx.HTTPStatusError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text
            raise HTTPException(status_code=400, detail=detail)

    @app.get("/my-reservations")
    async def my_reservations(req: Request, user_id: int) -> dict:
        _require_auth(req, settings)
        creds = store.get_credentials(user_id)
        if not creds:
            raise HTTPException(status_code=404, detail="Kimlik bilgisi bulunamadı. /metu_giris ile giriş yap.")
        metu_id, password, _ = creds
        result = await get_my_reservations(metu_id, password)
        return {"reservations": [r.to_api_dict() for r in result]}

    @app.put("/auto-watch")
    async def put_auto_watch(req: Request, body: AutoWatchBody) -> dict:
        _require_auth(req, settings)
        if not store.get_credentials(body.user_id):
            raise HTTPException(status_code=404, detail="Önce /metu_giris ile giriş yap.")
        fire_at = _next_sunday_22().isoformat()
        cfg = AutoWatchConfig(
            user_id=body.user_id,
            sport=body.sport,
            targets=body.targets,
            players=body.players,
            fire_at=fire_at,
        )
        store.set_auto_watch(cfg)
        return {"ok": True, "fire_at": fire_at}

    @app.delete("/auto-watch")
    async def delete_auto_watch(req: Request, user_id: int) -> dict:
        _require_auth(req, settings)
        store.delete_auto_watch(user_id)
        return {"ok": True}

    @app.get("/auto-watch")
    async def get_auto_watch(req: Request, user_id: int) -> dict:
        _require_auth(req, settings)
        cfg = store.get_auto_watch(user_id)
        if not cfg:
            return {"configured": False}
        from dataclasses import asdict
        data = asdict(cfg)
        try:
            fire_dt = datetime.fromisoformat(cfg.fire_at)
            data["fire_at_label"] = fire_dt.strftime("%d.%m.%Y %H:%M")
        except (ValueError, AttributeError):
            data["fire_at_label"] = cfg.fire_at
        return {"configured": True, **data}

    return app


settings = Settings()
store = CredentialStore(settings.data_dir / "metu-state.json")
app = create_app(settings, store)


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8081")))
