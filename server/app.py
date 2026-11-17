"""충남 매장 주차 안내 API — frontend docs/API.md v0.1 계약 + 주차 비서 채팅.

  GET  /health
  GET  /places?query=천안&limit=20          검색(Place 목록)
  GET  /places/{placeId}/parking            목적지 주차 상세(API v0.1 그대로)
  POST /assistant/chat {message, history}   주차 비서(LLM 설정이 없으면 503)

실행: .venv/bin/uvicorn server.app:app --port 8000
환경변수: CARDS_PATH(기본 data/cards/place_parking_cn.jsonl), ALLOWED_ORIGINS(쉼표 구분), LLM_* (assistant/agent.py)
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "assistant"))
from tools import CardStore  # noqa: E402

MAX_LIMIT = 50

@asynccontextmanager
async def lifespan(_: FastAPI):
    store()  # 카드 데이터를 기동 때 미리 읽는다(약 1초, 메모리 약 400MB)
    yield


app = FastAPI(title="충남 매장 주차 안내 API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(","),
    allow_methods=["GET", "POST"], allow_headers=["*"])


@lru_cache(maxsize=1)
def store() -> CardStore:
    return CardStore(Path(os.environ.get("CARDS_PATH", "data/cards/place_parking_cn.jsonl")))


_assistant = None


def assistant():
    """첫 채팅 요청 때 만든다. LLM 설정이 없으면 None."""
    global _assistant
    if _assistant is None:
        from agent import ParkingAssistant, load_env
        load_env()
        if not os.environ.get("LLM_BASE_URL") or not os.environ.get("LLM_API_KEY"):
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
    row = store().rows.get(place_id)
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
