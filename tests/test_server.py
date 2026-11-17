"""API 계약 테스트: 샘플 카드 8곳으로 서버를 띄워 frontend docs/API.md v0.1 모양을 확인한다."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ["CARDS_PATH"] = str(ROOT / "samples/place_parking_sample.jsonl")
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import server.app as srv  # noqa: E402

PLACE_KEYS = {"id", "name", "category", "address", "coordinates", "parkingStatus", "parkingSummary"}
LOT_KEYS = {"id", "name", "address", "entranceCoordinates", "entrancePhotoUrl", "entranceGuide",
            "feeDescription", "hoursDescription", "verifiedAt"}
OPTION_KEYS = {"lot", "relationship", "benefitDescription", "validationInstructions", "walkingMinutes", "walkingSteps"}


@pytest.fixture(scope="module")
def client():
    with TestClient(srv.app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["places"] == 8


def test_search_shape(client):
    r = client.get("/places", params={"query": "배나무숲"})
    body = r.json()
    assert r.status_code == 200 and body["total"] == 1
    assert PLACE_KEYS <= set(body["items"][0])
    assert body["items"][0]["parkingStatus"] in ("available", "unavailable", "unknown")


def test_search_region_only(client):
    body = client.get("/places", params={"query": "천안"}).json()
    assert body["total"] >= 3 and all("천안" in p["address"] for p in body["items"])


def test_search_empty_and_none(client):
    assert client.get("/places", params={"query": "  "}).json() == {"items": [], "total": 0}
    body = client.get("/places", params={"query": "가나다라없는식당"}).json()
    assert body == {"items": [], "total": 0}


def test_detail_contract(client):
    pid = client.get("/places", params={"query": "배나무숲"}).json()["items"][0]["id"]
    d = client.get(f"/places/{pid}/parking").json()
    assert {"place", "placeHoursDescription", "operatingHoursWarning", "primaryParking", "alternatives", "isDemo"} <= set(d)
    assert d["isDemo"] is False
    for opt in d["primaryParking"] + d["alternatives"]:
        assert OPTION_KEYS <= set(opt) and LOT_KEYS <= set(opt["lot"])
        assert opt["relationship"] in ("dedicated", "shared", "partner", "nearby")
        assert isinstance(opt["walkingSteps"], list)
    for opt in d["alternatives"]:  # 대체 주차장은 항상 내비 목적지 좌표가 있다
        assert opt["lot"]["entranceCoordinates"]["latitude"] and opt["lot"]["entranceGuide"]


def test_detail_404(client):
    r = client.get("/places/없는-id/parking")
    assert r.status_code == 404 and r.json()["code"] == "PLACE_NOT_FOUND"


def test_chat_unconfigured(client, monkeypatch):
    monkeypatch.setattr(srv, "assistant", lambda: None)
    r = client.post("/assistant/chat", json={"message": "배나무숲 주차?"})
    assert r.status_code == 503 and r.json()["code"] == "ASSISTANT_UNAVAILABLE"


def test_chat_with_fake_llm(client, monkeypatch):
    bot = NS(model="fake", ask=lambda msg, hist: {"answer": f"답:{msg}:{len(hist)}", "tool_calls": []})
    monkeypatch.setattr(srv, "assistant", lambda: bot)
    r = client.post("/assistant/chat", json={"message": "안녕", "history": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and r.json() == {"answer": "답:안녕:1", "toolCalls": [], "model": "fake"}


def test_chat_validation(client):
    assert client.post("/assistant/chat", json={"message": ""}).status_code == 422
    assert client.post("/assistant/chat", json={"message": "x" * 501}).status_code == 422
