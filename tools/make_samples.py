"""place_parking_cn.jsonl 에서 화면 경우의 수별로 한 곳씩 골라 samples/ 에 저장한다(프론트·백엔드 연동 확인용)."""
import json
from pathlib import Path

SRC = Path("data/cards/place_parking_cn.jsonl")
OUT = Path("samples/place_parking_sample.jsonl")

CASES = {
    "매장 정보: 무료 주차": lambda r: r["place"]["parkingSummary"] == "주차 가능·무료" and "천안" in r["place"]["address"],
    "매장 정보: 주차 불가": lambda r: r["place"]["parkingSummary"] == "주차 불가",
    "대체 주차장 운영시간 경고": lambda r: any(a["operatingHoursWarning"] for a in r["alternatives"]),
    "건물 주차장 + 입구 추정": lambda r: bool(r["primaryParking"]) and bool(r["primaryParking"][0]["lot"]["entranceCoordinates"])
    and "천안" in r["place"]["address"],
    "건물 주차장 없음 + 공영 대체": lambda r: r["place"]["parkingSummary"] == "건물 주차장 없음" and bool(r["alternatives"])
    and r["alternatives"][0]["lot"]["source"] == "전국주차장정보표준데이터",
    "LightGBM 추정": lambda r: "(추정)" in r["place"]["parkingSummary"] and "아산" in r["place"]["address"],
    "근거 부족 + 대체 없음": lambda r: r["place"]["parkingStatus"] == "unknown" and not r["alternatives"],
    "유료 공영 주차장 요금": lambda r: "천안" in r["place"]["address"] and any(
        (a["lot"]["feeDescription"] or "").startswith(("기본", "최초", "일부")) for a in r["alternatives"]),
}


def main() -> None:
    rows = [json.loads(line) for line in SRC.open(encoding="utf-8")]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    used = set()
    with OUT.open("w", encoding="utf-8") as f:
        for case, cond in CASES.items():
            r = next((r for r in rows if r["place"]["id"] not in used and cond(r)), None)
            if r is None:
                print(f"[없음] {case}")
                continue
            used.add(r["place"]["id"])
            f.write(json.dumps({"case": case, **r}, ensure_ascii=False) + "\n")
            print(f"{case}: {r['place']['name']} | {r['place']['parkingSummary']}")


if __name__ == "__main__":
    main()
