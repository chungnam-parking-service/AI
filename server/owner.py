"""점주 입력과 정확도 피드백 저장소(SQLite) + 카드 응답에 점주 입력을 덮어쓰는 로직.

공개 데이터에 없는 것(매장 주차 방식, 할인·무료 조건, 주차 등록·정산 방법, 입구 안내)을 점주가 채운다.
사업자 인증은 하지 않는다: 관리자가 매장별 점주 키를 발급하고(평문은 발급 때 한 번만 보임, DB엔 해시),
점주는 그 키로만 자기 매장을 고칠 수 있다.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
MAX_ENTRANCE_FROM_PLACE_M = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_keys (
    place_id TEXT PRIMARY KEY, key_hash TEXT NOT NULL, issued_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS owner_entries (
    place_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL, place_id TEXT, accurate INTEGER NOT NULL,
    comment TEXT, client TEXT, created_at TEXT NOT NULL);
"""


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


class OwnerStore:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    # ------------------------------------------------------------ 키
    def issue_key(self, place_id: str) -> str:
        """새 키를 발급한다(이전 키는 무효)."""
        key = secrets.token_urlsafe(18)
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO owner_keys VALUES (?, ?, ?)", (place_id, _hash(key), now_iso()))
        return key

    def check_key(self, place_id: str, key: str | None) -> bool:
        if not key:
            return False
        row = self.db.execute("SELECT key_hash FROM owner_keys WHERE place_id = ?", (place_id,)).fetchone()
        return row is not None and secrets.compare_digest(row[0], _hash(key))

    # ------------------------------------------------------------ 점주 입력
    def get(self, place_id: str) -> dict | None:
        row = self.db.execute("SELECT data, updated_at FROM owner_entries WHERE place_id = ?", (place_id,)).fetchone()
        return None if row is None else {**json.loads(row[0]), "updatedAt": row[1]}

    def put(self, place_id: str, data: dict) -> dict:
        ts = now_iso()
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO owner_entries VALUES (?, ?, ?)",
                            (place_id, json.dumps(data, ensure_ascii=False), ts))
        return {**data, "updatedAt": ts}

    def delete(self, place_id: str) -> bool:
        with self.lock, self.db:
            cur = self.db.execute("DELETE FROM owner_entries WHERE place_id = ?", (place_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------------ 피드백
    def add_feedback(self, lot_id: str, place_id: str | None, accurate: bool, comment: str | None,
                     client: str) -> bool:
        """같은 사람(client)이 같은 주차장에 1시간 안에 다시 보내면 저장하지 않는다(중복·스팸 방지)."""
        since = (datetime.now(KST) - timedelta(hours=1)).isoformat(timespec="seconds")
        with self.lock, self.db:
            dup = self.db.execute("SELECT 1 FROM feedback WHERE lot_id = ? AND client = ? AND created_at >= ?",
                                  (lot_id, client, since)).fetchone()
            if dup:
                return False
            self.db.execute("INSERT INTO feedback (lot_id, place_id, accurate, comment, client, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (lot_id, place_id, int(accurate), comment, client, now_iso()))
        return True

    def feedback_summary(self, lot_id: str) -> dict:
        n, ok = self.db.execute("SELECT COUNT(*), COALESCE(SUM(accurate), 0) FROM feedback WHERE lot_id = ?",
                                (lot_id,)).fetchone()
        return {"lotId": lot_id, "total": n, "accurate": ok, "inaccurate": n - ok}

    def feedback_recent(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT lot_id, place_id, accurate, comment, created_at FROM feedback "
                               "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"lotId": a, "placeId": b, "accurate": bool(c), "comment": d, "createdAt": e} for a, b, c, d, e in rows]

    # ------------------------------------------------------------ 응답에 덮어쓰기
    def apply(self, row: dict) -> dict:
        """카드 응답(API v0.1)에 점주 입력을 반영한 사본을 돌려준다. 입력이 없으면 원본 그대로."""
        entry = self.get(row["place"]["id"])
        return row if entry is None else apply_entry(row, entry)


def apply_entry(row: dict, e: dict) -> dict:
    r = copy.deepcopy(row)
    place = r["place"]
    mode = e["parkingMode"]
    verified = e["updatedAt"]
    if mode == "none":
        place["parkingStatus"] = "unavailable"
        place["parkingSummary"] = "매장 주차 불가 · 점주 확인"
        r["primaryParking"] = []
    else:
        lot_name = e.get("lotName") or (f"{place['name']} 매장 주차장" if mode == "own" else "제휴 주차장")
        ent = e.get("entranceCoordinates")
        option = {
            "lot": {"id": f"owner-{place['id']}", "name": lot_name, "address": e.get("lotAddress") or place["address"],
                    "entranceCoordinates": ent, "entranceSource": "점주 입력" if ent else None,
                    "entrancePhotoUrl": None, "entranceGuide": e.get("entranceGuide"),
                    "feeDescription": e.get("feeDescription"), "hoursDescription": e.get("lotHours"),
                    "verifiedAt": verified},
            "relationship": "dedicated" if mode == "own" else "partner",
            "benefitDescription": e.get("benefitDescription"),
            "validationInstructions": e.get("validationInstructions"),
            "walkingMinutes": e.get("walkingMinutes"), "walkingSteps": e.get("walkingSteps") or [],
            "evidence": "점주 입력",
        }
        r["primaryParking"] = [option]
        place["parkingStatus"] = "available"
        head = "매장 주차 가능" if mode == "own" else f"제휴 주차장({lot_name})"
        place["parkingSummary"] = f"{head} · {e['benefitDescription']}" if e.get("benefitDescription") else head
        place["parkingSummary"] += " · 점주 확인"
        r["operatingHoursWarning"] = None  # 점주 주차장의 운영시간은 점주 문구로만 안내
    r["ownerVerified"] = True
    r["ownerUpdatedAt"] = verified
    return r


def entrance_ok(place: dict, ent: dict | None) -> bool:
    if ent is None:
        return True
    c = place["coordinates"]
    return distance_m(c["latitude"], c["longitude"], ent["latitude"], ent["longitude"]) <= MAX_ENTRANCE_FROM_PLACE_M
