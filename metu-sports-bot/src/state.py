from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet


logger = logging.getLogger(__name__)


def _fernet() -> Fernet:
    key = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY must be set")
    return Fernet(key.encode())


@dataclass
class AutoWatchConfig:
    user_id: int
    sport: str     # "padel" | "pickleball"
    targets: list  # [{"date": "YYYY-MM-DD", "hour": 9}, ...]
    players: list  # ek oyuncu dicts
    fire_at: str   # ISO datetime — o pazar 22:00 TR saati


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.exception("Could not read state; using empty store")
            return {}

    def _save_raw(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        tmp.replace(self.path)

    # ── Credentials ──────────────────────────────────────────────────────────

    def set_credentials(self, user_id: int, metu_id: str, password: str, category: str = "student") -> None:
        f = _fernet()
        encrypted = f.encrypt(password.encode()).decode()
        data = self._load_raw()
        data.setdefault("credentials", {})[str(user_id)] = {
            "metu_id": metu_id,
            "encrypted_password": encrypted,
            "category": category,
        }
        self._save_raw(data)

    def get_credentials(self, user_id: int) -> tuple[str, str, str] | None:
        data = self._load_raw()
        entry = data.get("credentials", {}).get(str(user_id))
        if not entry:
            return None
        f = _fernet()
        password = f.decrypt(entry["encrypted_password"].encode()).decode()
        return entry["metu_id"], password, entry.get("category", "student")

    def delete_credentials(self, user_id: int) -> None:
        data = self._load_raw()
        data.get("credentials", {}).pop(str(user_id), None)
        self._save_raw(data)

    # ── Auto-watch ────────────────────────────────────────────────────────────

    def set_auto_watch(self, cfg: AutoWatchConfig) -> None:
        data = self._load_raw()
        data.setdefault("auto_watch", {})[str(cfg.user_id)] = asdict(cfg)
        self._save_raw(data)

    def get_auto_watch(self, user_id: int) -> AutoWatchConfig | None:
        data = self._load_raw()
        entry = data.get("auto_watch", {}).get(str(user_id))
        if not entry:
            return None
        return AutoWatchConfig(**entry)

    def delete_auto_watch(self, user_id: int) -> None:
        data = self._load_raw()
        data.get("auto_watch", {}).pop(str(user_id), None)
        self._save_raw(data)

    def get_all_auto_watchers(self) -> list[AutoWatchConfig]:
        data = self._load_raw()
        return [AutoWatchConfig(**v) for v in data.get("auto_watch", {}).values()]
