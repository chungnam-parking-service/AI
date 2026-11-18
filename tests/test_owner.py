"""점주 입력·관리자 키·정확도 피드백."""
import pytest
from fastapi.testclient import TestClient

import server.app as srv

ADMIN = {"X-Admin-Key": "test-admin-key"}


@pytest.fixture(scope="module")
def client():
    with TestClient(srv.app) as c:
        yield c


def place_id(client, q):
    return client.get("/places", params={"query": q}).json()["items"][0]["id"]


@pytest.fixture(scope="module")
def owned(client):
    pid = place_id(client, "정든포차")
    key = client.post("/admin/owner-keys", json={"placeId": pid}, headers=ADMIN).json()["ownerKey"]
    return pid, {"X-Owner-Key": key}


def test_issue_key_requires_admin(client):
    pid = place_id(client, "정든포차")
    assert client.post("/admin/owner-keys", json={"placeId": pid}).status_code == 401
    assert client.post("/admin/owner-keys", json={"placeId": pid}, headers={"X-Admin-Key": "x"}).status_code == 401
    assert client.post("/admin/owner-keys", json={"placeId": "nope"}, headers=ADMIN).status_code == 404


def test_owner_requires_key(client, owned):
    pid, _ = owned
    assert client.get(f"/owner/places/{pid}").status_code == 401
    assert client.put(f"/owner/places/{pid}", json={"parkingMode": "none"},
                      headers={"X-Owner-Key": "wrong"}).status_code == 401
    other = place_id(client, "배나무숲")  # 다른 매장의 키로는 못 고친다
    assert client.put(f"/owner/places/{other}", json={"parkingMode": "none"}, headers=owned[1]).status_code == 401


def test_owner_put_overrides_detail_and_search(client, owned):
    pid, h = owned
    before = client.get(f"/places/{pid}/parking").json()
    assert before["place"]["parkingStatus"] == "unknown" and not before.get("ownerVerified")
    body = {"parkingMode": "own", "benefitDescription": "2만 원 이상 2시간 무료",
            "validationInstructions": "계산할 때 차량번호 뒤 4자리를 알려주세요",
            "entranceGuide": "건물 뒤편 골목 입구", "walkingMinutes": 1,
            "entranceCoordinates": {"latitude": 36.81103, "longitude": 127.14970}}
    r = client.put(f"/owner/places/{pid}", json=body, headers=h)
    d = r.json()
    assert r.status_code == 200 and d["ownerVerified"] is True
    assert d["place"]["parkingStatus"] == "available" and "점주 확인" in d["place"]["parkingSummary"]
    opt = d["primaryParking"][0]
    assert opt["relationship"] == "dedicated" and opt["validationInstructions"].startswith("계산할 때")
    assert opt["lot"]["verifiedAt"] and opt["lot"]["entranceCoordinates"]["latitude"] == 36.81103
    assert len(d["alternatives"]) == len(before["alternatives"])  # 주변 주차장은 그대로
    assert client.get(f"/places/{pid}/parking").json()["ownerVerified"] is True
    item = client.get("/places", params={"query": "정든포차"}).json()["items"][0]
    assert item["parkingStatus"] == "available"
    assert client.get(f"/owner/places/{pid}", headers=h).json()["entry"]["parkingMode"] == "own"


def test_owner_input_reaches_assistant_tool(client, owned):
    pid, _ = owned
    out = srv.store().get_parking(pid)
    assert out["own_parking"][0]["evidence_source"] == "점주 입력"
    assert out["own_parking"][0]["how_to_validate"] and "주차권·주차 등록 방법" not in out["not_available_in_data"]


def test_owner_validation(client, owned):
    pid, h = owned
    assert client.put(f"/owner/places/{pid}", json={"parkingMode": "partner"}, headers=h).status_code == 422
    far = {"parkingMode": "own", "entranceCoordinates": {"latitude": 36.9, "longitude": 127.2}}
    assert client.put(f"/owner/places/{pid}", json=far, headers=h).json()["code"] == "ENTRANCE_TOO_FAR"
    assert client.put(f"/owner/places/{pid}", json={"parkingMode": "own", "benefitDescription": "x" * 201},
                      headers=h).status_code == 422


def test_owner_none_then_delete(client, owned):
    pid, h = owned
    d = client.put(f"/owner/places/{pid}", json={"parkingMode": "none", "benefitDescription": "무시됨"}, headers=h).json()
    assert d["place"]["parkingStatus"] == "unavailable" and d["primaryParking"] == []
    assert client.delete(f"/owner/places/{pid}", headers=h).json() == {"deleted": True}
    back = client.get(f"/places/{pid}/parking").json()
    assert back["place"]["parkingStatus"] == "unknown" and "ownerVerified" not in back


def test_feedback(client):
    d = client.get("/places/" + place_id(client, "정든포차") + "/parking").json()
    lot = d["alternatives"][0]["lot"]["id"]
    r = client.post(f"/parking-lots/{lot}/feedback", json={"placeId": d["place"]["id"], "accurate": False,
                                                          "comment": "요금이 바뀌었어요"})
    assert r.status_code == 201 and r.json()["inaccurate"] == 1
    again = client.post(f"/parking-lots/{lot}/feedback", json={"accurate": True})
    assert again.status_code == 429
    assert client.post("/parking-lots/nope/feedback", json={"accurate": True}).status_code == 404
    assert client.get("/admin/feedback").status_code == 401
    items = client.get("/admin/feedback", headers=ADMIN).json()["items"]
    assert items[0]["comment"] == "요금이 바뀌었어요"


def test_owner_page(client):
    r = client.get("/owner")
    assert r.status_code == 200 and "점주 주차 정보 입력" in r.text
