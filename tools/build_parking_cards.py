"""충남 음식점 매장별 주차 카드 테이블과 프론트 API(v0.1) 형태의 응답 묶음을 만든다.

한 행 = 상가정보 음식점 한 곳. 출처를 섞지 않고 칸마다 출처를 따로 둔다.
  building_*  건축물대장 주차정보(지번 매칭) 또는 LightGBM 추정(미매칭)
  store_*     TourAPI 음식점 상세(매장 단위 주차시설·영업시간, 이름+거리로 매칭)
  lots        주변 주차장 후보(전국주차장정보표준데이터 공영, OSM 주차장, 명절 개방 주차장)
표시 문구(summary_*)는 근거가 없으면 "정보 없음"으로 둔다.

출력
  data/cards/parking_cards_cn.csv      평면 테이블(UTF-8 BOM, 엑셀 호환)
  data/cards/parking_cards_cn.jsonl    주변 주차장 목록을 배열로 담은 버전(LLM 도구·분석용)
  data/cards/place_parking_cn.jsonl    frontend docs/API.md v0.1 의 GET /places/{id}/parking 응답 형태
  data/cards/sources.json              출처별 기준일
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree

EARTH_M = 6_371_000.0
WALK_M_PER_MIN = 75.0   # 시속 4.5km
DETOUR = 1.3            # 직선거리 → 도보 경로 보정(추정)
LOT_RADIUS_M = 800
LOT_TOP_K = 3
TOUR_MATCH_M = 300
DEDUP_M = 40            # 공공데이터 주차장과 이 거리 안의 OSM 주차장은 같은 시설로 보고 OSM 쪽을 뺀다
SELF_LOT_M = 15         # 매장 바로 옆 주차장은 매장 건물 자체 주차장으로 보고 대체 후보에서 뺀다
PRIMARY_ENTRANCE_M = 30  # 건물 주차장 입구 후보: 매장에서 이 거리 안의 OSM 주차장 입구
PE_ATTACH_M = 60        # amenity=parking_entrance 노드를 주차장에 붙이는 거리

SOURCE_DATES = {
    "상가정보": "2026-09",
    "건축물대장 주차정보": "2024-12",
    "TourAPI 음식점": "2026-11-16 조회",
    "전국주차장정보표준데이터": "2026-11-16 조회(기준일 2026)",
    "OSM 주차장": "2026-11-16 조회",
    "명절 개방 주차장": "2026 추석",
}
ENTRANCE_LABEL = {
    "pe": "OSM 주차장 입구 표시",
    "junction": "OSM 진출입로 교차점(추정)",
    "center": "주차장 대표 좌표(입구 미확인)",
}


def entrance_guide(source: str | None) -> str | None:
    """입구 좌표의 근거를 운전자용 문구로. 현장 확인한 입구는 없으므로 모두 확인 필요를 밝힌다."""
    if not source:
        return None
    if "입구 미확인" in source:
        return "입구 위치를 확인하지 못했어요. 주차장 위치로 안내하니 도착 후 입구를 확인해 주세요."
    if "입구 표시" in source:
        return "지도(OSM)에 표시된 주차장 입구로 안내해요. 현장과 다를 수 있어요."
    return "도로와 주차장이 만나는 지점을 입구로 추정해 안내해요. 현장과 다를 수 있어요."


def haversine_m(lat1, lon1, lat2, lon2) -> np.ndarray:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_M * np.arcsin(np.sqrt(a))


def norm_name(s: str) -> str:
    s = re.sub(r"\(.*?\)|\[.*?\]", "", str(s))
    s = re.sub(r"(본점|직영점|\d+호점)$", "", s.replace(" ", ""))
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s).lower()


# ---------------------------------------------------------------- 매장·건물
def load_stores(data: Path) -> pd.DataFrame:
    with gzip.open(data / "bundle_cn/sangga_chungnam.csv.gz", "rt", encoding="utf-8") as f:
        s = pd.read_csv(f, dtype=str).fillna("")
    s = s[s["상권업종대분류명"] == "음식"].copy()
    for c in ("지번본번지", "지번부번지"):
        s[c] = pd.to_numeric(s[c], errors="coerce").fillna(0).astype(int).astype(str)
    s["key"] = s["법정동코드"] + "-" + s["지번본번지"] + "-" + s["지번부번지"]
    s["lat"] = pd.to_numeric(s["위도"], errors="coerce")
    s["lon"] = pd.to_numeric(s["경도"], errors="coerce")
    return s.dropna(subset=["lat", "lon"]).reset_index(drop=True)


def load_buildings(data: Path) -> pd.DataFrame:
    b = pd.read_csv(data / "bundle_cn/building_parking_cn.csv", dtype=str).fillna("0")
    b["key"] = (b["주소 코드"] + "-" + pd.to_numeric(b["지번(본번)"]).astype(int).astype(str)
                + "-" + pd.to_numeric(b["지번(부번)"]).astype(int).astype(str))
    num = [c for c in b.columns if c not in ("주소 코드", "지번(본번)", "지번(부번)", "key")]
    b[num] = b[num].apply(pd.to_numeric, errors="coerce").fillna(0)
    b = b.groupby("key", as_index=False)[num].sum()
    out = pd.DataFrame({"key": b["key"]})
    out["bld_spaces"] = b["주차면수"].astype(int)
    out["bld_indoor_self"] = b["옥내_자주식_주차대수"].astype(int)
    out["bld_outdoor_self"] = b["옥외_자주식_주차대수"].astype(int)
    out["bld_mechanical"] = (b["기계식주차면수"] + b["옥내_기계식_주차대수"] + b["옥외_기계식_주차대수"]).astype(int)
    return out


def building_summary(r: pd.Series) -> str:
    if r["building_source"] == "건축물대장":
        if r["bld_spaces"] <= 0:
            return "건물 주차장 없음"
        kind = "건물 내 주차장" if r["bld_indoor_self"] > 0 else "건물 옥외 주차장"
        mech = ", 기계식 포함" if r["bld_mechanical"] > 0 else ""
        return f"{kind} {r['bld_spaces']}면{mech}"
    p = r["p_indoor_self"]
    if pd.isna(p):
        return "정보 없음"
    if p >= 0.7:
        head = "건물 내 주차장 있을 가능성 높음(추정)"
    elif p <= 0.2:
        head = "건물 내 주차장 없을 가능성 높음(추정)"
    else:
        return "정보 없음"
    probs = [r["p_spaces_0-5"], r["p_spaces_6-20"], r["p_spaces_21+"]]
    i = int(np.argmax(probs))
    if probs[i] >= 0.6:
        head += f", 약 {('0~5', '6~20', '21면 이상')[i]}{'' if i == 2 else '면'}"
    return head


# ---------------------------------------------------------------- TourAPI 매장
def tour_class(t: str) -> str:
    t = re.sub(r"<br\s*/?>", " ", str(t)).strip()
    if not t:
        return ""
    if "불가" in t[:6]:
        return "주차 불가"
    if "무료" in t or "없음" in t.split("요금")[-1]:
        return "주차 가능·무료"
    if "유료" in t:
        return "주차 가능·유료"
    return "주차 가능"


def match_tour(s: pd.DataFrame, tour: pd.DataFrame) -> pd.DataFrame:
    """TourAPI 음식점을 상가 매장에 붙인다: 정규화한 이름이 같거나 한쪽이 다른 쪽을 포함하고 300m 안."""
    tree = BallTree(np.radians(s[["lat", "lon"]].to_numpy()), metric="haversine")
    names = s["상호명"].map(norm_name).to_numpy()
    rows = []
    for _, t in tour.iterrows():
        tn = norm_name(t["title"])
        if len(tn) < 2:
            continue
        idx, dist = tree.query_radius(np.radians([[t["lat"], t["lon"]]]), TOUR_MATCH_M / EARTH_M,
                                      return_distance=True, sort_results=True)
        for i, d in zip(idx[0], dist[0]):
            sn = names[i]
            if sn and (sn == tn or (min(len(sn), len(tn)) >= 3 and (sn in tn or tn in sn))):
                rows.append({"row": i, "contentid": t["contentid"], "tour_title": t["title"],
                             "tour_dist_m": round(d * EARTH_M), "store_parking_raw": t["parkingfood"],
                             "store_hours": re.sub(r"<br\s*/?>", " / ", str(t["opentimefood"])).strip(),
                             "store_rest": re.sub(r"<br\s*/?>", " / ", str(t["restdatefood"])).strip(),
                             "store_modified": t["modified"]})
                break
    m = pd.DataFrame(rows)
    if m.empty:
        return m
    return m.sort_values("tour_dist_m").drop_duplicates("row")  # 한 매장에 여러 건이면 가까운 쪽


_RANGE = re.compile(r"(\d{1,2}):(\d{2})\s*[~\-–]\s*(\d{1,2}):(\d{2})")
_SKIP = re.compile(r"준비|브레이크|휴게|마지막|주문|라스트")


def store_close_min(text: str) -> int | None:
    """TourAPI 영업시간 문구에서 가장 늦은 마감 시각(자정 기준 분, 자정 넘기면 1440+)을 뽑는다. 모르면 None."""
    if not isinstance(text, str) or not text.strip():
        return None
    ends = []
    for seg in re.split(r"<br\s*/?>|/|\n", text):
        if _SKIP.search(seg):
            continue
        for h1, m1, h2, m2 in _RANGE.findall(seg):
            start, end = int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)
            ends.append(end + 1440 if end <= start else end)
    return max(ends) if ends else None


# ---------------------------------------------------------------- 주차장
def _hhmm(v) -> int | None:
    if not isinstance(v, str) or not re.fullmatch(r"\d{1,2}:\d{2}", v.strip()):
        return None
    h, m = v.strip().split(":")
    return int(h) * 60 + int(m)


def _span(o, c) -> str | None:
    a, b = _hhmm(o), _hhmm(c)
    if a is None or b is None:
        return None
    if a == 0 and b in (0, 1439, 1440):
        return "24시간"
    return f"{o.strip()}–{c.strip()}"


def public_hours(r: pd.Series) -> tuple[str | None, int | None]:
    """(운영시간 문구, 평일 마감 분 — 24시간·미상이면 None)."""
    days = str(r.get("operDay") or "")
    wk = _span(r["weekdayOperOpenHhmm"], r["weekdayOperColseHhmm"])
    sat = _span(r["satOperOperOpenHhmm"], r["satOperCloseHhmm"]) if "토" in days else "휴무"
    hol = _span(r["holidayOperOpenHhmm"], r["holidayCloseOpenHhmm"]) if "공휴" in days else "휴무"
    if wk is None:
        return None, None
    text = f"매일 {wk}" if wk == sat == hol else f"평일 {wk} · 토 {sat or '미상'} · 공휴일 {hol or '미상'}"
    close = None if wk == "24시간" else _hhmm(r["weekdayOperColseHhmm"])
    if close is not None and close <= (_hhmm(r["weekdayOperOpenHhmm"]) or 0):
        close += 1440
    return text, close


def _s(v) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _won(v) -> str:
    return f"{int(float(v)):,}원"


def public_fee(r: pd.Series) -> str | None:
    label = r.get("parkingchrgeInfo")
    if label == "무료":
        return "무료"
    num = {k: pd.to_numeric(r.get(k), errors="coerce") for k in
           ("basicTime", "basicCharge", "addUnitTime", "addUnitCharge", "dayCmmtkt")}
    parts = []
    if pd.notna(num["basicTime"]) and num["basicTime"] > 0:
        bc = num["basicCharge"]
        parts.append(f"최초 {int(num['basicTime'])}분 무료" if pd.isna(bc) or bc == 0
                     else f"기본 {int(num['basicTime'])}분 {_won(bc)}")
    if pd.notna(num["addUnitTime"]) and num["addUnitTime"] > 0 and pd.notna(num["addUnitCharge"]):
        parts.append(f"이후 {int(num['addUnitTime'])}분당 {_won(num['addUnitCharge'])}")
    if pd.notna(num["dayCmmtkt"]) and num["dayCmmtkt"] > 0:
        parts.append(f"1일권 {_won(num['dayCmmtkt'])}")
    if parts:
        return ("일부 무료 · " if label == "혼합" else "") + ", ".join(parts)
    return {"유료": "유료(요금 상세 없음)", "혼합": "무료·유료 혼합(상세 없음)"}.get(label)


def load_public_lots(data: Path) -> pd.DataFrame:
    p = pd.read_csv(data / "public_lots/public_lots_chungnam_geo.csv", dtype=str)
    p = p[p["geo_source"].notna()].copy()
    hours = p.apply(public_hours, axis=1)
    return pd.DataFrame({
        "lot_id": "pub-" + p["prkplceNo"], "name": p["prkplceNm"].str.strip(),
        "type": p["prkplceType"].fillna(""), "fee": p["parkingchrgeInfo"].fillna(""),
        "fee_desc": p.apply(public_fee, axis=1), "fee_note": p["spcmnt"],
        "hours_desc": [h[0] for h in hours], "close_min": [h[1] for h in hours],
        "address": p["rdnmadr"].fillna(p["lnmadr"]), "spaces": pd.to_numeric(p["prkcmprt"], errors="coerce"),
        "phone": p["phoneNumber"], "access": "", "holiday_only": False, "open_info": "",
        "source": "전국주차장정보표준데이터", "verified_at": p["referenceDate"],
        "coord_source": p["geo_source"],
        "lat": pd.to_numeric(p["latitude"]), "lon": pd.to_numeric(p["longitude"]),
        "osm_key": ""})


def load_osm_lots(data: Path, include_private: bool = False) -> pd.DataFrame:
    o = pd.read_csv(data / "bundle_cn/osm_parking_bbox.csv", dtype=str).fillna("")
    if not include_private:
        o = o[~o["access"].isin(["private", "no"])]
    kind = {"underground": "지하", "multi-storey": "주차빌딩", "surface": "노외", "street_side": "노상"}
    fee = o["fee"].map({"no": "무료", "무료": "무료", "yes": "유료"}).fillna("")
    return pd.DataFrame({
        "lot_id": "osm-" + o["osm_type"] + "-" + o["osm_id"],
        "name": np.where(o["이름"] != "", o["이름"], "이름 없는 주차장"),
        "type": o["parking"].map(kind).fillna(""), "fee": fee,
        "fee_desc": fee.replace("", None), "fee_note": None, "hours_desc": None, "close_min": None,
        "address": None, "spaces": pd.to_numeric(o["capacity"], errors="coerce"), "phone": None,
        "access": o["access"].map({"customers": "이용객 전용", "private": "비공개"}).fillna(""),
        "holiday_only": False, "open_info": "", "source": "OSM 주차장", "verified_at": None,
        "coord_source": "OSM", "lat": pd.to_numeric(o["위도"]), "lon": pd.to_numeric(o["경도"]),
        "osm_key": o["osm_type"] + "-" + o["osm_id"]})


def load_holiday_lots(data: Path) -> pd.DataFrame:
    h = pd.read_csv(data / "bundle_cn/holiday_open_lots.csv", dtype=str).fillna("")
    h = h[h["주소"].str.match(r"^(?:충남|충청남도)")].drop_duplicates(["자원명", "위도", "경도"])
    days = h[["휴일_1_개방일", "휴일_4_개방일"]].agg(lambda r: f"{r.iloc[0]}~{r.iloc[1]}", axis=1)
    return pd.DataFrame({
        "lot_id": "hol-" + h.index.astype(str), "name": h["자원명"] + " (명절에만 개방)", "type": h["주차장유형"],
        "fee": h["요금정책"], "fee_desc": h["요금정책"].replace("", None), "fee_note": None,
        "hours_desc": None, "close_min": None, "address": h["주소"], "spaces": np.nan, "phone": None,
        "access": "", "holiday_only": True, "open_info": h["명절구분"] + " " + days + " " + h["휴일_1_개방시간"],
        "source": "명절 개방 주차장", "verified_at": None, "coord_source": "원본",
        "lat": pd.to_numeric(h["위도"], errors="coerce"), "lon": pd.to_numeric(h["경도"], errors="coerce"),
        "osm_key": ""})


def load_entrances(path: Path) -> tuple[dict[str, list[tuple[float, float]]], np.ndarray]:
    """(OSM way 키 → 진출입로 교차점 목록, parking_entrance 노드 좌표 배열)."""
    if not path.exists():
        print(f"[warn] {path} 없음: 입구 좌표는 주차장 대표 좌표로 대체")
        return {}, np.empty((0, 2))
    els = json.load(open(path, encoding="utf-8"))["elements"]
    nodes = {e["id"]: (e["lat"], e["lon"]) for e in els if e["type"] == "node"}
    pe = np.array([(e["lat"], e["lon"]) for e in els
                   if e["type"] == "node" and e.get("tags", {}).get("amenity") == "parking_entrance"]).reshape(-1, 2)
    junction = {}
    for e in els:
        if e["type"] == "way" and "nodes" in e:
            pts = [nodes[n] for n in dict.fromkeys(e["nodes"]) if n in nodes]
            if pts:
                junction[f"way-{e['id']}"] = pts
    return junction, pe


def attach_entrances(lots: pd.DataFrame, junction: dict, pe: np.ndarray) -> list[list[tuple[float, float, str]]]:
    """주차장마다 입구 후보 [(lat, lon, kind)]. parking_entrance 우선, 없으면 교차점, 없으면 빈 목록."""
    pe_tree = BallTree(np.radians(pe), metric="haversine") if len(pe) else None
    out = []
    for lat, lon, key in zip(lots["lat"], lots["lon"], lots["osm_key"]):
        cands: list[tuple[float, float, str]] = []
        if pe_tree is not None:
            idx = pe_tree.query_radius(np.radians([[lat, lon]]), PE_ATTACH_M / EARTH_M)[0]
            cands += [(pe[i][0], pe[i][1], "pe") for i in idx]
        if not cands and key in junction:
            cands += [(a, b, "junction") for a, b in junction[key]]
        out.append(cands)
    return out


def pick_entrance(cands: list, lot_lat: float, lot_lon: float, s_lat: float, s_lon: float) -> dict:
    if not cands:
        return {"lat": round(lot_lat, 6), "lon": round(lot_lon, 6), "source": ENTRANCE_LABEL["center"]}
    arr = np.array([(c[0], c[1]) for c in cands])
    i = int(np.argmin(haversine_m(arr[:, 0], arr[:, 1], s_lat, s_lon)))
    return {"lat": round(cands[i][0], 6), "lon": round(cands[i][1], 6), "source": ENTRANCE_LABEL[cands[i][2]]}


def build_lots(data: Path, junction: dict, pe: np.ndarray) -> pd.DataFrame:
    pub, osm, hol = load_public_lots(data), load_osm_lots(data), load_holiday_lots(data)
    # 공공데이터와 겹치는 OSM 주차장은 빼고, 그 OSM 면의 입구 정보는 공공데이터 주차장이 물려받는다
    tree = BallTree(np.radians(pub[["lat", "lon"]].to_numpy()), metric="haversine")
    dist, idx = tree.query(np.radians(osm[["lat", "lon"]].to_numpy()), k=1)
    dup = dist[:, 0] * EARTH_M < DEDUP_M
    inherit = (osm[dup].assign(pub_i=idx[dup, 0])[lambda d: d["osm_key"].isin(junction)]
               .drop_duplicates("pub_i").set_index("pub_i")["osm_key"])
    pub.loc[pub.index[inherit.index], "osm_key"] = inherit.to_numpy()
    print(f"lots: 공공 {len(pub)}, OSM {len(osm)}(공공과 중복 {int(dup.sum())} 제외), 명절 {len(hol)}")
    lots = pd.concat([pub, osm[~dup], hol], ignore_index=True).dropna(subset=["lat", "lon"]).reset_index(drop=True)
    lots["entrances"] = attach_entrances(lots, junction, pe)
    return lots


def nearby_lots(s: pd.DataFrame, lots: pd.DataFrame) -> list[list[dict]]:
    tree = BallTree(np.radians(lots[["lat", "lon"]].to_numpy()), metric="haversine")
    idx, dist = tree.query_radius(np.radians(s[["lat", "lon"]].to_numpy()), LOT_RADIUS_M / EARTH_M,
                                  return_distance=True, sort_results=True)
    rec = lots.to_dict("records")
    out = []
    for (slat, slon), ii, dd in zip(s[["lat", "lon"]].to_numpy(), idx, dist):
        regular, holiday = [], []
        for i, d in zip(ii, dd):
            lot = rec[i]
            m = d * EARTH_M
            if m < SELF_LOT_M:
                continue
            ent = pick_entrance(lot["entrances"], lot["lat"], lot["lon"], slat, slon)
            item = {"lot_id": lot["lot_id"], "name": lot["name"], "type": lot["type"], "fee": lot["fee"],
                    "fee_desc": _s(lot["fee_desc"]), "fee_note": _s(lot["fee_note"]),
                    "hours_desc": _s(lot["hours_desc"]),
                    "close_min": None if pd.isna(lot["close_min"]) else int(lot["close_min"]),
                    "address": _s(lot["address"]),
                    "spaces": None if pd.isna(lot["spaces"]) else int(lot["spaces"]),
                    "access": lot["access"], "dist_m": round(m),
                    "walk_min_est": max(1, round(m * DETOUR / WALK_M_PER_MIN)),
                    "lat": round(lot["lat"], 6), "lon": round(lot["lon"], 6),
                    "entrance_lat": ent["lat"], "entrance_lon": ent["lon"], "entrance_source": ent["source"],
                    "source": lot["source"], "verified_at": _s(lot["verified_at"]),
                    "holiday_only": bool(lot["holiday_only"]), "open_info": lot["open_info"]}
            (holiday if lot["holiday_only"] else regular).append(item)
        out.append(regular[:LOT_TOP_K] + holiday[:1])
    return out


def primary_entrances(s: pd.DataFrame, data: Path, junction: dict, pe: np.ndarray) -> list[dict | None]:
    """건물 주차장 입구 후보: 매장 30m 안의 OSM 주차장(비공개 포함 — 건물 부설은 대개 이용객 전용)의 입구.
    공공데이터 주차장과 겹치는 OSM 주차장은 공영이므로 후보에서 뺀다."""
    osm = load_osm_lots(data, include_private=True)
    pub = load_public_lots(data)
    d, _ = BallTree(np.radians(pub[["lat", "lon"]].to_numpy()), metric="haversine").query(
        np.radians(osm[["lat", "lon"]].to_numpy()), k=1)
    osm = osm[d[:, 0] * EARTH_M >= DEDUP_M].reset_index(drop=True)  # 공영 주차장을 건물 주차장으로 오인하지 않게
    osm["entrances"] = attach_entrances(osm, junction, pe)
    osm = osm[osm["entrances"].map(len) > 0].reset_index(drop=True)
    if osm.empty:
        return [None] * len(s)
    tree = BallTree(np.radians(osm[["lat", "lon"]].to_numpy()), metric="haversine")
    dist, idx = tree.query(np.radians(s[["lat", "lon"]].to_numpy()), k=1)
    out = []
    for (slat, slon), d, i in zip(s[["lat", "lon"]].to_numpy(), dist[:, 0], idx[:, 0]):
        if d * EARTH_M > PRIMARY_ENTRANCE_M:
            out.append(None)
            continue
        lot = osm.iloc[i]
        ent = pick_entrance(lot["entrances"], lot["lat"], lot["lon"], slat, slon)
        ent["source"] = "매장 인접 " + ent["source"]
        out.append(ent)
    return out


# ---------------------------------------------------------------- API 형태
def hours_warning(store_close: int | None, lot_close: int | None, lot_hours: str | None) -> str | None:
    if store_close is None or lot_close is None or lot_close >= store_close:
        return None
    diff = store_close - lot_close
    hh = f"{(lot_close // 60) % 24:02d}:{lot_close % 60:02d}"
    gap = f"{diff // 60}시간" + (f" {diff % 60}분" if diff % 60 else "") if diff >= 60 else f"{diff}분"
    return f"주차장은 평일 {hh}에 닫혀요. 매장 마감보다 {gap} 빠르니 이용 시간을 확인해 주세요."


def parking_status(r: dict) -> str:
    st = r["summary_store"]
    if st == "주차 불가":
        return "unavailable"
    if st.startswith("주차 가능"):
        return "available"
    if r["building_source"] == "건축물대장":
        return "available" if (r["bld_spaces"] or 0) > 0 else "unavailable"
    return "unknown"


def to_api(r: dict, lots: list[dict], primary_ent: dict | None) -> dict:
    status = parking_status(r)
    summary = r["summary_headline"]
    if r["summary_store"] == "정보 없음" and status == "available":
        summary += " · 손님 이용 여부는 매장 확인 필요"
    addr = r["도로명주소"] or r["지번주소"]
    name = r["상호명"] + (f" {r['지점명']}" if r["지점명"] else "")
    place = {"id": f"sangga-{r['상가업소번호']}", "name": name, "category": r["상권업종소분류명"],
             "address": addr, "coordinates": {"latitude": r["lat"], "longitude": r["lon"]},
             "parkingStatus": status, "parkingSummary": summary}
    store_close = store_close_min(r["store_hours"])

    primary = []
    if status == "available":
        from_tour = r["summary_store"] != "정보 없음"
        primary.append({
            "lot": {"id": f"tour-{r['contentid']}" if from_tour else f"bld-{r['key']}",
                    "name": f"{name} 매장 주차장" if from_tour else f"{r['건물명'] or name} 건물 주차장",
                    "address": addr,
                    "entranceCoordinates": None if primary_ent is None else
                    {"latitude": primary_ent["lat"], "longitude": primary_ent["lon"]},
                    "entranceSource": None if primary_ent is None else primary_ent["source"],
                    "entrancePhotoUrl": None, "entranceGuide": None if primary_ent is None else entrance_guide(primary_ent["source"]),
                    "feeDescription": "무료(매장 정보)" if r["summary_store"] == "주차 가능·무료" else None,
                    "hoursDescription": None,
                    "verifiedAt": None},
            "relationship": "dedicated" if from_tour else "shared",
            "benefitDescription": None, "validationInstructions": None,
            "walkingMinutes": None, "walkingSteps": [],
            "evidence": r["summary_store"] if from_tour else f"{r['building_source']}: {r['summary_building']}",
        })

    alts = []
    for x in lots:
        w = hours_warning(store_close, x["close_min"], x["hours_desc"])
        alts.append({
            "lot": {"id": x["lot_id"], "name": x["name"], "address": x["address"],
                    "entranceCoordinates": {"latitude": x["entrance_lat"], "longitude": x["entrance_lon"]},
                    "entranceSource": x["entrance_source"],
                    "entrancePhotoUrl": None, "entranceGuide": entrance_guide(x["entrance_source"]),
                    "feeDescription": x["fee_desc"], "feeNote": x["fee_note"],
                    "hoursDescription": x["open_info"] if x["holiday_only"] else x["hours_desc"],
                    "verifiedAt": x["verified_at"], "source": x["source"], "holidayOnly": x["holiday_only"]},
            "relationship": "nearby", "benefitDescription": None, "validationInstructions": None,
            "walkingMinutes": x["walk_min_est"], "walkingMinutesIsEstimate": True, "distanceMeters": x["dist_m"],
            "walkingSteps": [], "operatingHoursWarning": w})
    first = alts[0]["operatingHoursWarning"] if (not primary and alts) else None
    return {"place": place, "placeHoursDescription": r["store_hours"] or None,
            "operatingHoursWarning": first, "primaryParking": primary, "alternatives": alts,
            "isDemo": False}


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--pred", type=Path, default=Path("out/cv_2421_cn/pred_unmatched.csv"))
    ap.add_argument("--entrances", type=Path, default=Path("data/osm_entrance/raw.json"))
    ap.add_argument("--out", type=Path, default=Path("data/cards"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    s = load_stores(args.data)
    s = s.merge(load_buildings(args.data), on="key", how="left")
    s["building_source"] = np.where(s["bld_spaces"].notna(), "건축물대장", "")
    pred = pd.read_csv(args.pred, dtype={"상가업소번호": str}, encoding="utf-8-sig")
    pcols = ["p_indoor_self", "p_mechanical", "p_spaces_0-5", "p_spaces_6-20", "p_spaces_21+"]
    s = s.merge(pred[["상가업소번호", *pcols]], on="상가업소번호", how="left")
    s.loc[(s["building_source"] == "") & s["p_indoor_self"].notna(), "building_source"] = "LightGBM 추정"
    for c in ("bld_spaces", "bld_indoor_self", "bld_outdoor_self", "bld_mechanical"):
        s[c] = s[c].astype("Int64")
    s["summary_building"] = s.apply(building_summary, axis=1)

    tour = pd.DataFrame(json.load(open(args.data / "tourapi/food_chungnam.json", encoding="utf-8")))
    tour["lat"] = pd.to_numeric(tour["lat"], errors="coerce")
    tour["lon"] = pd.to_numeric(tour["lon"], errors="coerce")
    tm = match_tour(s, tour.dropna(subset=["lat", "lon"]))
    s = s.join(tm.set_index("row"), how="left")
    s["summary_store"] = s["store_parking_raw"].fillna("").map(tour_class).replace("", "정보 없음")
    s["summary_headline"] = np.where(s["summary_store"] != "정보 없음", s["summary_store"], s["summary_building"])

    junction, pe = load_entrances(args.entrances)
    lots = build_lots(args.data, junction, pe)
    near = nearby_lots(s, lots)
    prim = primary_entrances(s, args.data, junction, pe)
    s["lots_json"] = [json.dumps(x, ensure_ascii=False) for x in near]
    s["nearest_lot_m"] = [next((x["dist_m"] for x in n if not x["holiday_only"]), None) for n in near]
    s["n_lots_800m"] = [sum(not x["holiday_only"] for x in n) for n in near]
    s["building_entrance_lat"] = [p["lat"] if p else None for p in prim]
    s["building_entrance_lon"] = [p["lon"] if p else None for p in prim]
    s["building_entrance_source"] = [p["source"] if p else None for p in prim]
    (args.out / "sources.json").write_text(json.dumps(SOURCE_DATES, ensure_ascii=False, indent=1), encoding="utf-8")

    cols = ["상가업소번호", "상호명", "지점명", "상권업종중분류명", "상권업종소분류명", "시군구명", "법정동명",
            "지번주소", "도로명주소", "건물명", "층정보", "lat", "lon",
            "summary_headline", "summary_store", "summary_building", "building_source",
            "bld_spaces", "bld_indoor_self", "bld_outdoor_self", "bld_mechanical", *pcols,
            "building_entrance_lat", "building_entrance_lon", "building_entrance_source",
            "contentid", "tour_title", "tour_dist_m", "store_parking_raw", "store_hours", "store_rest",
            "store_modified", "nearest_lot_m", "n_lots_800m", "lots_json"]
    t = s[cols]
    t.to_csv(args.out / "parking_cards_cn.csv", index=False, encoding="utf-8-sig")
    flat = t.drop(columns=["lots_json"])
    base = flat.astype(object).where(flat.notna(), None).to_dict("records")
    raw = [{"key": k, "store_hours": h if isinstance(h, str) else ""} for k, h in zip(s["key"], s["store_hours"])]
    n_warn = n_prim_ent = 0
    with open(args.out / "parking_cards_cn.jsonl", "w", encoding="utf-8") as f, \
            open(args.out / "place_parking_cn.jsonl", "w", encoding="utf-8") as g:
        for rec, extra, lots_i, p in zip(base, raw, near, prim):
            f.write(json.dumps({**rec, "lots": lots_i}, ensure_ascii=False, default=str) + "\n")
            r = {**rec, **extra}
            r["지점명"] = r["지점명"] or ""
            r["건물명"] = r["건물명"] or ""
            r["도로명주소"] = r["도로명주소"] or ""
            api = to_api(r, lots_i, p)
            n_warn += any(a["operatingHoursWarning"] for a in api["alternatives"])
            n_prim_ent += bool(api["primaryParking"] and api["primaryParking"][0]["lot"]["entranceCoordinates"])
            g.write(json.dumps(api, ensure_ascii=False, default=str) + "\n")

    n = len(t)
    print(f"stores {n}")
    print("building_source", t["building_source"].replace("", "없음").value_counts().to_dict())
    print(f"tour matched {t['contentid'].notna().sum()} / {len(tour)}")
    print("summary_store", t["summary_store"].value_counts().to_dict())
    print("headline 정보 없음", round((t["summary_headline"] == "정보 없음").mean(), 3))
    print("lots within 800m: any", round((t["n_lots_800m"] > 0).mean(), 3),
          "median nearest m", t["nearest_lot_m"].median())
    srcs = pd.Series([x["source"] for n_ in near for x in n_]).value_counts().to_dict()
    ents = pd.Series([x["entrance_source"] for n_ in near for x in n_ if not x["holiday_only"]]).value_counts().to_dict()
    print("lot items by source", srcs)
    print("entrance source (regular lots)", ents)
    print(f"stores with any hours warning {n_warn}, primary parking with entrance {n_prim_ent}")


if __name__ == "__main__":
    main()
