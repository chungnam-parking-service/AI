"""충남 매장 주차 안내 API — frontend docs/API.md v0.1 계약 + 주차 비서 채팅.

  GET  /health
  GET  /places?query=천안&limit=20          검색(Place 목록)
  GET  /places/{placeId}/parking            목적지 주차 상세(API v0.1 그대로)
  POST /assistant/chat {message, history}   주차 비서(LLM 설정이 없으면 503)

  점주 입력(사업자 인증 대신 관리자가 매장별 점주 키 발급)
  POST   /admin/owner-keys {placeId}          헤더 X-Admin-Key → {ownerKey} (평문은 이때만)
  GET    /owner/places/{placeId}              헤더 X-Owner-Key → 현재 입력
  PUT    /owner/places/{placeId}              헤더 X-Owner-Key, 본문 OwnerEntry → 반영된 상세 응답
  DELETE /owner/places/{placeId}              헤더 X-Owner-Key → 공개 데이터로 되돌림
  GET    /owner                               점주 입력 페이지(데모용)

  정확도 피드백(API.md 후속 MVP)
  POST /parking-lots/{lotId}/feedback {placeId, accurate, comment}   같은 사람·주차장 1시간 1회
  GET  /parking-lots/{lotId}/feedback                                  집계
  GET  /admin/feedback                                                 최근 피드백(X-Admin-Key)

실행: .venv/bin/uvicorn server.app:app --port 8000
환경변수: CARDS_PATH(기본 data/cards/place_parking_cn.jsonl), OWNER_DB(기본 data/owner.db), ADMIN_API_KEY,
         ALLOWED_ORIGINS(쉼표 구분), LLM_* (assistant/agent.py)
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal

import hashlib
import secrets

from fastapi import FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator

from server.owner import OwnerStore, entrance_ok

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "assistant"))
from tools import CardStore  # noqa: E402

MAX_LIMIT = 50
OWNER_PAGE = Path(__file__).resolve().parent / "owner.html"

@asynccontextmanager
async def lifespan(_: FastAPI):
    from agent import load_env
    load_env()  # .env 의 ADMIN_API_KEY·LLM_* (이미 있는 환경변수가 우선)
    store()  # 카드 데이터를 기동 때 미리 읽는다(약 1초, 메모리 약 400MB)
    yield


app = FastAPI(title="충남 매장 주차 안내 API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(","),
    allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["*"])


@lru_cache(maxsize=1)
def owner_store() -> OwnerStore:
    path = Path(os.environ.get("OWNER_DB", "data/owner.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return OwnerStore(str(path))


@lru_cache(maxsize=1)
def store() -> CardStore:
    s = CardStore(Path(os.environ.get("CARDS_PATH", "data/cards/place_parking_cn.jsonl")))
    s.overlay = owner_store().apply  # 검색·상세·주차 비서 모두 점주 입력을 반영해 본다
    return s


@lru_cache(maxsize=1)
def lot_ids() -> frozenset[str]:
    return frozenset(o["lot"]["id"] for r in store().rows.values() for o in r["primaryParking"] + r["alternatives"])


_assistant = None


def assistant():
    """첫 채팅 요청 때 만든다. LLM 설정이 없으면 None."""
    global _assistant
    if _assistant is None:
        from agent import ParkingAssistant, llm_config
        if llm_config() is None:
            return None
        _assistant = ParkingAssistant(store())
    return _assistant


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "places": len(store().rows), "sources": store().sources}


@app.get("/places")
def search_places(query: str = Query(..., max_length=100), limit: int = Query(20, ge=1, le=MAX_LIMIT)):
    q = query.strip()
    if not q:  # 목 어댑터와 같게: 빈 검색은 빈 결과
        return {"items": [], "total": 0}
    total, items = store().search(q, limit)
    return {"items": items, "total": total}


@app.get("/places/{place_id}/parking")
def place_parking(place_id: str):
    row = store().row(place_id)
    if row is None:
        return error(404, "PLACE_NOT_FOUND", "목적지를 찾을 수 없습니다.")
    return row


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    history: list[Turn] = Field(default_factory=list, max_length=10)


@app.post("/assistant/chat")
def chat(req: ChatRequest):
    bot = assistant()
    if bot is None:
        return error(503, "ASSISTANT_UNAVAILABLE", "주차 비서가 설정되지 않았습니다.")
    try:
        out = bot.ask(req.message, [t.model_dump() for t in req.history])
    except Exception:  # LLM 서버 장애는 일시 실패로
        return error(503, "ASSISTANT_FAILED", "주차 비서 응답에 실패했습니다. 잠시 후 다시 시도해 주세요.")
    return {"answer": out["answer"], "toolCalls": out["tool_calls"], "model": bot.model}


# ---------------------------------------------------------------- 점주 입력
class Coordinates(BaseModel):
    latitude: float = Field(ge=35.5, le=37.5)
    longitude: float = Field(ge=125.5, le=128.0)


class OwnerEntry(BaseModel):
    parkingMode: Literal["own", "partner", "none"]
    lotName: str | None = Field(None, max_length=60)
    lotAddress: str | None = Field(None, max_length=120)
    benefitDescription: str | None = Field(None, max_length=200)
    validationInstructions: str | None = Field(None, max_length=300)
    entranceGuide: str | None = Field(None, max_length=300)
    entranceCoordinates: Coordinates | None = None
    feeDescription: str | None = Field(None, max_length=200)
    lotHours: str | None = Field(None, max_length=100)
    walkingMinutes: int | None = Field(None, ge=0, le=60)
    walkingSteps: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _check(self):
        for k, v in self.__dict__.items():
            if isinstance(v, str):
                setattr(self, k, v.strip() or None)
        self.walkingSteps = [x.strip()[:120] for x in self.walkingSteps if x.strip()]
        if self.parkingMode == "partner" and not self.lotName:
            raise ValueError("제휴 주차장은 lotName이 필요합니다")
        return self


def admin_ok(key: str | None) -> bool:
    expected = os.environ.get("ADMIN_API_KEY")
    return bool(expected) and bool(key) and secrets.compare_digest(expected, key)


class KeyRequest(BaseModel):
    placeId: str


@app.post("/admin/owner-keys")
def issue_owner_key(req: KeyRequest, x_admin_key: str | None = Header(None)):
    if not os.environ.get("ADMIN_API_KEY"):
        return error(503, "ADMIN_DISABLED", "ADMIN_API_KEY가 설정되지 않았습니다.")
    if not admin_ok(x_admin_key):
        return error(401, "UNAUTHORIZED", "관리자 키가 필요합니다.")
    if req.placeId not in store().rows:
        return error(404, "PLACE_NOT_FOUND", "목적지를 찾을 수 없습니다.")
    return {"placeId": req.placeId, "ownerKey": owner_store().issue_key(req.placeId)}


def _owner_guard(place_id: str, key: str | None) -> JSONResponse | None:
    if place_id not in store().rows:
        return error(404, "PLACE_NOT_FOUND", "목적지를 찾을 수 없습니다.")
    if not owner_store().check_key(place_id, key):
        return error(401, "UNAUTHORIZED", "이 매장의 점주 키가 필요합니다.")
    return None


@app.get("/owner/places/{place_id}")
def owner_get(place_id: str, x_owner_key: str | None = Header(None)):
    if (bad := _owner_guard(place_id, x_owner_key)) is not None:
        return bad
    return {"place": store().rows[place_id]["place"], "entry": owner_store().get(place_id)}


@app.put("/owner/places/{place_id}")
def owner_put(place_id: str, entry: OwnerEntry, x_owner_key: str | None = Header(None)):
    if (bad := _owner_guard(place_id, x_owner_key)) is not None:
        return bad
    data = entry.model_dump()
    if not entrance_ok(store().rows[place_id]["place"], data["entranceCoordinates"]):
        return error(400, "ENTRANCE_TOO_FAR", "입구 좌표가 매장에서 500m보다 멉니다.")
    if data["parkingMode"] == "none":  # 주차 불가면 주차장 칸은 버린다
        data = {"parkingMode": "none"}
    owner_store().put(place_id, data)
    return store().row(place_id)


@app.delete("/owner/places/{place_id}")
def owner_delete(place_id: str, x_owner_key: str | None = Header(None)):
    if (bad := _owner_guard(place_id, x_owner_key)) is not None:
        return bad
    return {"deleted": owner_store().delete(place_id)}


@app.get("/owner")
def owner_page():
    return FileResponse(OWNER_PAGE, media_type="text/html; charset=utf-8")


# ---------------------------------------------------------------- 정확도 피드백
class FeedbackRequest(BaseModel):
    placeId: str | None = None
    accurate: bool
    comment: str | None = Field(None, max_length=300)


@app.post("/parking-lots/{lot_id}/feedback", status_code=201)
def feedback(lot_id: str, req: FeedbackRequest, request: Request):
    if lot_id not in lot_ids() and not lot_id.startswith("owner-"):
        return error(404, "LOT_NOT_FOUND", "주차장을 찾을 수 없습니다.")
    ua = request.headers.get("user-agent", "")
    client = hashlib.sha256(f"{request.client.host if request.client else ''}|{ua}".encode()).hexdigest()[:16]
    comment = (req.comment or "").strip() or None
    if not owner_store().add_feedback(lot_id, req.placeId, req.accurate, comment, client):
        return error(429, "FEEDBACK_DUPLICATE", "같은 주차장에는 1시간에 한 번만 의견을 보낼 수 있어요.")
    return {"saved": True, **owner_store().feedback_summary(lot_id)}


@app.get("/parking-lots/{lot_id}/feedback")
def feedback_summary(lot_id: str):
    return owner_store().feedback_summary(lot_id)


@app.get("/admin/feedback")
def feedback_recent(x_admin_key: str | None = Header(None), limit: int = Query(50, ge=1, le=500)):
    if not admin_ok(x_admin_key):
        return error(401, "UNAUTHORIZED", "관리자 키가 필요합니다.")
    return {"items": owner_store().feedback_recent(limit)}
