"""Traffic domain services."""

from __future__ import annotations
import datetime as dt
import contextlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from .files import ensure_private_parent


class TrafficStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def _db(self):
        if not self._initialized:
            ensure_private_parent(self.path)
            self._initialized = True
            self._initialize()
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._lock, self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id TEXT,
                    profile_name TEXT NOT NULL,
                    server TEXT,
                    started_at INTEGER NOT NULL,
                    ended_at INTEGER,
                    download_bytes INTEGER NOT NULL DEFAULT 0,
                    upload_bytes INTEGER NOT NULL DEFAULT 0,
                    peak_download_bps INTEGER NOT NULL DEFAULT 0,
                    peak_upload_bps INTEGER NOT NULL DEFAULT 0,
                    max_connections INTEGER NOT NULL DEFAULT 0
                )
                """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_profile ON sessions(profile_id, started_at)"
            )
            db.execute("""
                CREATE TABLE IF NOT EXISTS traffic_daily (
                    day TEXT PRIMARY KEY,
                    download_bytes INTEGER NOT NULL DEFAULT 0,
                    upload_bytes INTEGER NOT NULL DEFAULT 0
                )
                """)

    @staticmethod
    def _local_day(timestamp: float | None = None) -> str:
        moment = dt.datetime.fromtimestamp(timestamp or time.time()).astimezone()
        return moment.date().isoformat()

    @staticmethod
    def _local_month_prefix() -> str:
        return dt.datetime.now().astimezone().strftime("%Y-%m-")

    def start_session(
        self, profile_id: str | None, profile_name: str, server: str
    ) -> int:
        now = int(time.time())
        with self._lock, self._db() as db:
            cursor = db.execute(
                "INSERT INTO sessions(profile_id, profile_name, server, started_at) VALUES (?, ?, ?, ?)",
                (profile_id, profile_name[:80], server[:255], now),
            )
            return int(cursor.lastrowid)

    def record_delta(
        self, download_bytes: int, upload_bytes: int, timestamp: float | None = None
    ) -> None:
        down = max(0, int(download_bytes))
        up = max(0, int(upload_bytes))
        if down == 0 and up == 0:
            return
        day = self._local_day(timestamp)
        with self._lock, self._db() as db:
            db.execute(
                """
                INSERT INTO traffic_daily(day, download_bytes, upload_bytes)
                VALUES (?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    download_bytes = download_bytes + excluded.download_bytes,
                    upload_bytes = upload_bytes + excluded.upload_bytes
                """,
                (day, down, up),
            )

    def update_session(
        self,
        session_id: int,
        download_bytes: int,
        upload_bytes: int,
        download_bps: int,
        upload_bps: int,
        connections: int,
        finish: bool = False,
    ) -> None:
        with self._lock, self._db() as db:
            db.execute(
                """
                UPDATE sessions
                SET download_bytes = ?, upload_bytes = ?,
                    peak_download_bps = MAX(peak_download_bps, ?),
                    peak_upload_bps = MAX(peak_upload_bps, ?),
                    max_connections = MAX(max_connections, ?),
                    ended_at = CASE WHEN ? THEN ? ELSE ended_at END
                WHERE id = ?
                """,
                (
                    max(0, int(download_bytes)),
                    max(0, int(upload_bytes)),
                    max(0, int(download_bps)),
                    max(0, int(upload_bps)),
                    max(0, int(connections)),
                    1 if finish else 0,
                    int(time.time()),
                    session_id,
                ),
            )

    def summary(self) -> dict[str, Any]:
        today = self._local_day()
        month_prefix = self._local_month_prefix()
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT COALESCE(download_bytes,0) AS d, COALESCE(upload_bytes,0) AS u FROM traffic_daily WHERE day = ?",
                (today,),
            ).fetchone()
            today_data = {
                "downloadBytes": int(row["d"]) if row else 0,
                "uploadBytes": int(row["u"]) if row else 0,
            }
            row = db.execute(
                "SELECT COALESCE(SUM(download_bytes),0) AS d, COALESCE(SUM(upload_bytes),0) AS u FROM traffic_daily WHERE day LIKE ?",
                (f"{month_prefix}%",),
            ).fetchone()
            month_data = {"downloadBytes": int(row["d"]), "uploadBytes": int(row["u"])}
            row = db.execute(
                "SELECT COALESCE(SUM(download_bytes),0) AS d, COALESCE(SUM(upload_bytes),0) AS u FROM traffic_daily"
            ).fetchone()
            all_time = {"downloadBytes": int(row["d"]), "uploadBytes": int(row["u"])}
        return {"today": today_data, "month": month_data, "allTime": all_time}

    def period_totals(self, start_day: str, end_day: str):
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(download_bytes),0) AS d, COALESCE(SUM(upload_bytes),0) AS u FROM traffic_daily WHERE day >= ? AND day < ?",
                (start_day, end_day),
            ).fetchone()
        return {"downloadBytes": int(row["d"]), "uploadBytes": int(row["u"])}

    def daily_series(self, days=30):
        days = max(1, min(90, int(days)))
        today = dt.datetime.now().astimezone().date()
        start = today - dt.timedelta(days=days - 1)
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT day,download_bytes,upload_bytes FROM traffic_daily WHERE day >= ? ORDER BY day",
                (start.isoformat(),),
            ).fetchall()
        records = {row["day"]: row for row in rows}
        result = []
        for offset in range(days):
            day = (start + dt.timedelta(days=offset)).isoformat()
            row = records.get(day)
            result.append(
                {
                    "day": day,
                    "downloadBytes": int(row["download_bytes"]) if row else 0,
                    "uploadBytes": int(row["upload_bytes"]) if row else 0,
                }
            )
        return result

    def recent_sessions(self, limit=12):
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT profile_name,started_at,ended_at,download_bytes,upload_bytes FROM sessions ORDER BY started_at DESC,id DESC LIMIT ?",
                (max(1, min(50, int(limit))),),
            ).fetchall()
        return [
            {
                "profileName": r["profile_name"],
                "startedAt": r["started_at"],
                "endedAt": r["ended_at"],
                "downloadBytes": r["download_bytes"],
                "uploadBytes": r["upload_bytes"],
            }
            for r in rows
        ]
