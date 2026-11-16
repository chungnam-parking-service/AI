"""한국관광공사 TourAPI(KorService2)에서 충남 음식점의 주차시설·영업시간을 받는다.

키는 .env의 DATA_GO_KR_KEY(없으면 TOUR_API_KEY).
사용: .venv/bin/python tools/fetch_tourapi.py [--out data/tourapi/food_chungnam.json]
"""
import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from fetch_public_lots import load_key

BASE = "https://apis.data.go.kr/B551011/KorService2"
AREA_CHUNGNAM = 34
FOOD = 39


def call(op: str, key: str, **params) -> dict:
    q = urllib.parse.urlencode({"serviceKey": key, "MobileOS": "ETC", "MobileApp": "parking-ai",
                                "_type": "json", **params})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(f"{BASE}/{op}?{q}", timeout=30) as r:
                body = json.loads(r.read().decode())["response"]["body"]
            items = body["items"]
            return {"total": body.get("totalCount", 0),
                    "items": (items.get("item", []) if isinstance(items, dict) else [])}
        except Exception as e:
            if attempt == 3:
                raise
            print(f"{op} retry {attempt + 1}: {e}", flush=True)
            time.sleep(2 * (attempt + 1))
    raise AssertionError


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("data/tourapi/food_chungnam.json"))
    args = ap.parse_args()
    key = load_key()
    lst = call("areaBasedList2", key, areaCode=AREA_CHUNGNAM, contentTypeId=FOOD, numOfRows=1000, pageNo=1)
    if len(lst["items"]) != int(lst["total"]):
        raise SystemExit(f"목록 {len(lst['items'])} != totalCount {lst['total']}")
    out = []
    for it in lst["items"]:
        d = call("detailIntro2", key, contentId=it["contentid"], contentTypeId=FOOD)["items"]
        d = d[0] if d else {}
        out.append({"sigungu": it.get("sigungucode"), "contentid": it["contentid"], "title": it["title"],
                    "addr": it.get("addr1"), "lat": it.get("mapy"), "lon": it.get("mapx"),
                    "modified": it.get("modifiedtime"), "parkingfood": d.get("parkingfood", ""),
                    "opentimefood": d.get("opentimefood", ""), "restdatefood": d.get("restdatefood", ""),
                    "firstmenu": d.get("firstmenu", "")})
        time.sleep(0.05)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"충남 음식점 {len(out)}곳, 주차 텍스트 {sum(bool(x['parkingfood']) for x in out)}곳 -> {args.out}")


if __name__ == "__main__":
    main()
