"""좌표가 빈 공공 주차장을 카카오 주소 검색으로 채운다(원본 CSV는 그대로 두고 _geo 사본을 만든다).

사용: .venv/bin/python tools/geocode_missing_lots.py [--src data/public_lots/public_lots_chungnam.csv]
"""
import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

from fetch_public_lots import load_key  # .env 로드 겸용

API = "https://dapi.kakao.com/v2/local/search/address.json"


def geocode(addr: str, key: str) -> tuple[float, float] | None:
    req = urllib.request.Request(f"{API}?{urllib.parse.urlencode({'query': addr})}",
                                 headers={"Authorization": f"KakaoAK {key}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        docs = json.loads(r.read().decode())["documents"]
    if not docs:
        return None
    return float(docs[0]["y"]), float(docs[0]["x"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=Path("data/public_lots/public_lots_chungnam.csv"))
    args = ap.parse_args()
    load_key()
    key = os.environ.get("KAKAO_REST_KEY")
    if not key:
        raise SystemExit("KAKAO_REST_KEY가 .env에 없습니다")
    d = pd.read_csv(args.src, dtype=str)
    lat = pd.to_numeric(d["latitude"], errors="coerce")
    d["geo_source"] = "원본"
    d.loc[lat.isna() | (lat == 0), "geo_source"] = ""
    ok = 0
    for i in d.index[d["geo_source"] == ""]:
        for addr in (d.at[i, "rdnmadr"], d.at[i, "lnmadr"]):
            if isinstance(addr, str) and addr.strip():
                hit = geocode(addr.strip(), key)
                time.sleep(0.1)
                if hit:
                    d.at[i, "latitude"], d.at[i, "longitude"] = f"{hit[0]:.7f}", f"{hit[1]:.7f}"
                    d.at[i, "geo_source"] = "카카오 주소 검색"
                    ok += 1
                    break
    out = args.src.with_name(args.src.stem + "_geo.csv")
    d.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"missing {int((lat.isna() | (lat == 0)).sum())}, geocoded {ok} -> {out}")


if __name__ == "__main__":
    main()
