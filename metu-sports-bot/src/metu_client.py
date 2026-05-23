from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta, timezone

_TZ_TR = timezone(timedelta(hours=3))


def _today() -> Date:
    return datetime.now(_TZ_TR).date()

import httpx


logger = logging.getLogger(__name__)

_BASE = "https://metuapp.ceng.metu.edu.tr/sports/api"


@dataclass(frozen=True)
class Slot:
    slot_id: str       # "{court_id}:{hour}:{date}"
    sport: str
    date: str          # "YYYY-MM-DD"
    time_from: str     # "09:00"
    time_to: str       # "10:00"
    court_name: str
    court_id: str
    hour: int

    def to_api_dict(self) -> dict:
        return {
            "slotId": self.slot_id,
            "sport": self.sport,
            "date": self.date,
            "timeFrom": self.time_from,
            "timeTo": self.time_to,
            "courtName": self.court_name,
            "courtId": self.court_id,
            "hour": self.hour,
            "label": f"{self.time_from}–{self.time_to} / {self.court_name}",
        }


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    sport: str
    date: str
    time_from: str
    time_to: str
    court_name: str
    status: str
    player2_name: str = ""
    player3_name: str = ""
    player4_name: str = ""

    def to_api_dict(self) -> dict:
        return {
            "reservationId": self.reservation_id,
            "sport": self.sport,
            "date": self.date,
            "timeFrom": self.time_from,
            "timeTo": self.time_to,
            "courtName": self.court_name,
            "status": self.status,
            "player2": self.player2_name,
            "player3": self.player3_name,
            "player4": self.player4_name,
        }


async def _login(metu_id: str, password: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as c:
        r = await c.post(f"{_BASE}/login/", json={"username": metu_id, "password": password})
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise ValueError(f"Login failed: {data}")
        session = r.cookies.get("sports_session", "")
        if not session:
            raise ValueError("No session cookie after login")
        return session


async def get_available_slots(
    metu_id: str, password: str, sport: str, date: str
) -> list[Slot]:
    session = await _login(metu_id, password)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=15,
        cookies={"sports_session": session},
    ) as c:
        r = await c.get(f"{_BASE}/slots/", params={"date": date})
        r.raise_for_status()
        data = r.json()

    slots: list[Slot] = []
    for slot_data in data.get("slots", []):
        label: str = slot_data.get("label", "")
        parts = label.split(" - ")
        time_from = parts[0].strip() if parts else "?"
        time_to = parts[1].strip() if len(parts) > 1 else "?"
        hour: int = slot_data.get("hour", 0)

        for court in slot_data.get("courts", []):
            if court.get("court_type", "").lower() != sport.lower():
                continue
            if court.get("is_booked"):
                continue
            court_id = court["court_id"]
            slots.append(Slot(
                slot_id=f"{court_id}:{hour}:{date}",
                sport=sport,
                date=date,
                time_from=time_from,
                time_to=time_to,
                court_name=court["court_name"],
                court_id=court_id,
                hour=hour,
            ))

    logger.info("Found %d available %s slots on %s", len(slots), sport, date)
    return slots


async def make_reservation(
    metu_id: str,
    password: str,
    slot_id: str,
    players: list[dict],
    booker_category: str = "student",
) -> dict:
    # slot_id format: "{court_id}:{hour}:{date}"
    try:
        court_id, hour_str, date = slot_id.split(":", 2)
        hour = int(hour_str)
    except ValueError:
        raise ValueError(f"Invalid slot_id format: {slot_id}")

    session = await _login(metu_id, password)

    body: dict = {
        "court_id": court_id,
        "date": date,
        "hour": hour,
        "booker_category": booker_category,
    }
    # players[0] = player2, players[1] = player3, players[2] = player4
    for idx, player in enumerate(players, start=2):
        if player.get("name"):
            body[f"player{idx}_name"] = player["name"]
        if player.get("category"):
            body[f"player{idx}_category"] = player["category"]
        if player.get("metu_id"):
            body[f"player{idx}_identifier"] = player["metu_id"]

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=15,
        cookies={"sports_session": session},
    ) as c:
        r = await c.post(f"{_BASE}/reservations/", json=body)
        if not r.is_success:
            logger.error("Reservation API %s: %s | body sent: %s", r.status_code, r.text, body)
        r.raise_for_status()
        data = r.json()

    logger.info("Reservation created: %s", data)
    return {
        "ok": True,
        "reservationId": data.get("id", ""),
        "courtName": data.get("court_name", ""),
        "sport": data.get("court_type", ""),
        "date": data.get("date", date),
        "slotLabel": data.get("slot_label", ""),
    }


async def get_available_dates(
    metu_id: str, password: str, sport: str, days: int = 7
) -> list[dict]:
    session = await _login(metu_id, password)
    today = _today()
    result = []
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=15, cookies={"sports_session": session}
    ) as c:
        for i in range(days):
            d = str(today + timedelta(days=i))
            try:
                r = await c.get(f"{_BASE}/slots/", params={"date": d})
                if r.status_code == 400:
                    break  # beyond booking window — no point checking further dates
                r.raise_for_status()
                data = r.json()
                count = sum(
                    1
                    for slot in data.get("slots", [])
                    for court in slot.get("courts", [])
                    if court.get("court_type", "").lower() == sport.lower()
                    and not court.get("is_booked")
                )
                if count > 0:
                    result.append({"date": d, "slot_count": count})
            except httpx.HTTPStatusError:
                break
            except Exception:
                logger.exception("Date check failed for %s", d)
    return result


async def get_my_reservations(metu_id: str, password: str) -> list[Reservation]:
    session = await _login(metu_id, password)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=15,
        cookies={"sports_session": session},
    ) as c:
        r = await c.get(f"{_BASE}/reservations/")
        r.raise_for_status()
        data = r.json()

    result: list[Reservation] = []
    for rv in data.get("reservations", []):
        label = rv.get("slot_label", "")
        parts = label.split(" - ")
        result.append(Reservation(
            reservation_id=rv["id"],
            sport=rv.get("court_type", ""),
            date=rv.get("date", ""),
            time_from=parts[0].strip() if parts else "",
            time_to=parts[1].strip() if len(parts) > 1 else "",
            court_name=rv.get("court_name", ""),
            status="active",
            player2_name=rv.get("player2_name", ""),
            player3_name=rv.get("player3_name", ""),
            player4_name=rv.get("player4_name", ""),
        ))
    return result
