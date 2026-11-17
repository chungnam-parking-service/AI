"""주차 비서가 쓰는 도구: 매장 검색, 매장 주차 상세. 데이터는 place_parking_cn.jsonl(API v0.1 형태)."""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_DATA = Path("data/cards/place_parking_cn.jsonl")
SOURCES = Path("data/cards/sources.json")
MAX_RESULTS = 5
REGIONS = ("천안", "공주", "보령", "아산", "서산", "논산", "계룡", "당진", "금산", "부여", "서천", "청양",
           "홍성", "예산", "태안")


def _norm(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s or "").lower()


class CardStore:
    def __init__(self, path: Path = DEFAULT_DATA):
        self.rows: dict[str, dict] = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                self.rows[r["place"]["id"]] = r
        self._index = [(pid, _norm(r["place"]["name"]), r["place"]["address"]) for pid, r in self.rows.items()]
        self.sources = json.loads(SOURCES.read_text(encoding="utf-8")) if SOURCES.exists() else {}

    # ------------------------------------------------------------ search_places
    def search_places(self, query: str, region: str | None = None) -> dict:
        """매장명(일부)으로 찾는다. 지역(시·군 이름)이 있으면 주소로 거른다.
        질의에 지역이 섞여 있으면("천안 맘스터치") 그대로 먼저 찾고, 없으면 지역을 떼어 다시 찾는다."""
        q = (query or "").strip()
        if len(_norm(q)) < 2:
            return {"error": "매장 이름을 두 글자 이상 알려주세요.", "items": []}
        hits = self._match(_norm(q), region)
        if not hits and not region:
            for reg in REGIONS:
                if reg in q:
                    rest = q.replace(reg + "시", "").replace(reg + "군", "").replace(reg, "")
                    if len(_norm(rest)) >= 2:
                        hits = self._match(_norm(rest), reg)
                    break
        items = []
        for _, _, pid in hits[:MAX_RESULTS]:
            p = self.rows[pid]["place"]
            items.append({"place_id": pid, "name": p["name"], "category": p["category"], "address": p["address"],
                          "parking_status": p["parkingStatus"], "parking_summary": p["parkingSummary"]})
        return {"total_matches": len(hits), "items": items,
                "note": "결과가 여러 곳이면 사용자에게 지점을 확인하세요." if len(hits) > 1 else None}

    def search(self, query: str, limit: int = 20) -> tuple[int, list[dict]]:
        """API용 검색(GET /places): 시·군 이름은 주소 필터, 나머지는 매장명(우선)·업종에 부분 일치.
        "천안"만 오면 천안 매장 전체가 대상. 반환: (전체 일치 수, Place 목록 limit개)."""
        regions = [r for r in REGIONS if r in query]
        rest = query
        for r in regions:
            rest = rest.replace(r + "시", "").replace(r + "군", "").replace(r, "")
        nq = _norm(rest)
        hits = []
        for pid, name, addr in self._index:
            if regions and not any(r in addr for r in regions):
                continue
            if not nq:  # 지역만 온 경우: 주차 정보가 확인된 매장을 앞에
                score = {"available": 0, "unavailable": 1}.get(self.rows[pid]["place"]["parkingStatus"], 2)
            elif nq == name:
                score = 0
            elif name.startswith(nq):
                score = 1
            elif nq in name:
                score = 2
            elif nq in _norm(self.rows[pid]["place"]["category"]):
                score = 3
            else:
                continue
            hits.append((score, len(name), pid))
        hits.sort()
        return len(hits), [self.rows[pid]["place"] for _, _, pid in hits[:limit]]

    def _match(self, nq: str, region: str | None) -> list[tuple[int, int, str]]:
        hits = []
        for pid, name, addr in self._index:
            if region and region not in addr:
                continue
            if nq == name:
                score = 0
            elif name.startswith(nq):
                score = 1
            elif nq in name:
                score = 2
            else:
                continue
            hits.append((score, len(name), pid))
        hits.sort()
        return hits

    # ------------------------------------------------------------ get_parking
    def get_parking(self, place_id: str) -> dict:
        r = self.rows.get(place_id)
        if r is None:
            return {"error": f"place_id {place_id} 없음. search_places로 먼저 찾으세요."}
        p = r["place"]
        out = {
            "place": {"place_id": p["id"], "name": p["name"], "address": p["address"], "category": p["category"]},
            "parking_status": p["parkingStatus"],
            "parking_summary": p["parkingSummary"],
            "store_hours": r["placeHoursDescription"],
            "operating_hours_warning": r["operatingHoursWarning"],
            "own_parking": [],
            "nearby_lots": [],
            "not_available_in_data": ["주차권·주차 등록 방법", "매장 할인·무료 조건(매장 정보에 '무료'로 표시된 경우 제외)",
                                      "건물 안 도보 동선", "실시간 빈자리"],
            "data_dates": self.sources,
        }
        for o in r["primaryParking"]:
            lot = o["lot"]
            out["own_parking"].append({
                "name": lot["name"], "relationship": o["relationship"], "evidence": o.get("evidence"),
                "evidence_source": "TourAPI 매장 정보" if o["relationship"] == "dedicated" else "건축물대장 또는 LightGBM 추정",
                "fee": lot["feeDescription"],
                "entrance": lot["entranceCoordinates"], "entrance_source": lot.get("entranceSource")})
        for o in r["alternatives"]:
            lot = o["lot"]
            out["nearby_lots"].append({
                "name": lot["name"], "source": lot.get("source"), "holiday_only": lot.get("holidayOnly", False),
                "distance_m": o.get("distanceMeters"), "walk_minutes_estimate": o["walkingMinutes"],
                "fee": lot["feeDescription"], "fee_note": lot.get("feeNote"), "hours": lot["hoursDescription"],
                "hours_warning": o.get("operatingHoursWarning"), "entrance_source": lot.get("entranceSource"),
                "verified_at": lot["verifiedAt"]})
        return out


TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "search_places",
        "description": "충남 음식점을 이름으로 찾는다. 결과의 place_id로 get_parking을 호출한다.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "매장 이름 또는 그 일부, 예: '맘스터치', '배나무숲'"},
            "region": {"type": "string", "description": "시·군 이름(선택), 예: '천안', '아산'"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_parking",
        "description": "매장 하나의 주차 정보: 매장·건물 주차, 주변 주차장(거리·추정 도보시간·요금·운영시간), 운영시간 경고, 데이터 기준일.",
        "parameters": {"type": "object", "properties": {"place_id": {"type": "string"}}, "required": ["place_id"]}}},
]


def dispatch(store: CardStore, name: str, args: dict) -> dict:
    if name == "search_places":
        return store.search_places(args.get("query", ""), args.get("region"))
    if name == "get_parking":
        return store.get_parking(args.get("place_id", ""))
    return {"error": f"알 수 없는 도구 {name}"}
