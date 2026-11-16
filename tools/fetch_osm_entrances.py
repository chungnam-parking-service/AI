"""OSM에서 충남 주차장 입구 후보를 받는다(Overpass).

입구 후보 두 종류
  parking_entrance  amenity=parking_entrance 노드(지하·건물 주차장 입구로 태깅된 것)
  junction          주차장 면(way) 경계 노드 중 차량 도로(highway)와 공유되는 노드 = 진출입로가 닿는 점
사용: .venv/bin/python tools/fetch_osm_entrances.py [--out data/osm_entrance/raw.json]
"""
import argparse
import time
import urllib.parse
import urllib.request
from pathlib import Path

BBOX = "35.97,125.9,37.08,127.65"  # 충남(세종·대전 일부 포함, 이후 매장 반경으로 걸러진다)
QUERY = f"""[out:json][timeout:600];
way["amenity"="parking"]({BBOX})->.lots;
node(w.lots)->.ln;
way(bn.ln)["highway"]["highway"!~"footway|path|steps|pedestrian|cycleway|corridor|elevator"]->.roads;
node(w.roads)->.rn;
node.ln.rn->.junction;
node["amenity"="parking_entrance"]({BBOX})->.pe;
(.junction; .pe;);
out body;
.lots out skel center;
"""
SERVERS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("data/osm_entrance/raw.json"))
    args = ap.parse_args()
    body = urllib.parse.urlencode({"data": QUERY}).encode()
    for attempt in range(8):
        url = SERVERS[attempt % len(SERVERS)]
        req = urllib.request.Request(url, data=body, headers={"User-Agent": "parking-data-script/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=700) as r:
                data = r.read()
            if data.lstrip().startswith(b"{") and b'"elements"' in data[:2000]:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_bytes(data)
                print(f"ok {url} {len(data)} bytes", flush=True)
                return
            print(f"non-json from {url}: {data[:120]!r}", flush=True)
        except Exception as e:
            print(f"{url} attempt {attempt + 1}: {e}", flush=True)
        time.sleep(30)
    raise SystemExit("Overpass 응답 실패")


if __name__ == "__main__":
    main()
