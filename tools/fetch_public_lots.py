"""전국주차장정보표준데이터 OpenAPI에서 충남 주차장만 받아 저장한다.

키는 .env의 DATA_GO_KR_KEY(없으면 TOUR_API_KEY, 같은 공공데이터포털 Decoding 키)를 쓴다.
사용: .venv/bin/python tools/fetch_public_lots.py [--out data/public_lots]
"""

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

URL = "https://api.data.go.kr/openapi/tn_pubr_prkplce_info_api"
ROWS = 1000


def load_key() -> str:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))
    key = os.environ.get("DATA_GO_KR_KEY") or os.environ.get("TOUR_API_KEY")
    if not key:
        raise SystemExit("DATA_GO_KR_KEY(또는 TOUR_API_KEY)가 .env에 없습니다")
    return key


def fetch_page(key: str, page: int) -> dict:
    q = urllib.parse.urlencode({"serviceKey": key, "pageNo": page, "numOfRows": ROWS, "type": "json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(f"{URL}?{q}", timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # 네트워크·일시 오류는 재시도
            if attempt == 3:
                raise
            print(f"page {page} retry {attempt + 1}: {e}", flush=True)
            time.sleep(3 * (attempt + 1))
    raise AssertionError


def is_chungnam(item: dict) -> bool:
    addr = (item.get("rdnmadr") or "") + " " + (item.get("lnmadr") or "")
    return "충청남도" in addr or "충남" in addr.split()[:1] or (item.get("insttNm") or "").startswith("충청남도")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("data/public_lots"))
    args = ap.parse_args()
    key = load_key()

    first = fetch_page(key, 1)
    if first["header"]["resultCode"] != "00":
        raise SystemExit(f"API 오류: {first['header']}")
    total = int(first["body"]["totalCount"])
    pages = (total + ROWS - 1) // ROWS
    print(f"전국 {total}건, {pages}페이지", flush=True)

    items = first["body"]["items"]["item"] if isinstance(first["body"]["items"], dict) else first["body"]["items"]
    for p in range(2, pages + 1):
        d = fetch_page(key, p)
        if d["header"]["resultCode"] == "03":
            break
        if d["header"]["resultCode"] != "00":
            raise SystemExit(f"API 오류 page {p}: {d['header']}")
        body = d["body"]["items"]
        items += body["item"] if isinstance(body, dict) else body
    if len(items) != total:
        raise SystemExit(f"받은 건수 {len(items)} != totalCount {total}")

    cn = [it for it in items if is_chungnam(it)]
    args.out.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(cn)
    df.to_csv(args.out / "public_lots_chungnam.csv", index=False, encoding="utf-8-sig")
    meta = {"source": "전국주차장정보표준데이터 OpenAPI (15012896)", "fetched_at": time.strftime("%Y-%m-%d %H:%M"),
            "national_total": total, "chungnam": len(cn)}
    (args.out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    print(json.dumps(meta, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
