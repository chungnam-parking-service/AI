"""주차 비서 평가 질문셋을 만든다: 손으로 쓴 경우별 질문 + 무작위 매장 질문(재현용 고정 시드).

expect
  answer     get_parking이 expect_name 매장(주소에 expect_addr 포함)으로 호출돼야 한다
  clarify    같은 이름이 여러 곳 → 지점을 되물어야 한다(get_parking 호출 없이)
  not_found  데이터에 없는 매장 → 없다고 말해야 한다
  out_of_scope  충남 음식점 밖 질문
must_any: 답에 하나 이상 들어가야 하는 정규식 / must_not: 들어가면 안 되는 정규식
"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools import CardStore  # noqa: E402

OUT = Path("assistant/eval_questions.jsonl")
SEED = 20261117
N_RANDOM = 8

HAND = [
    {"q": "천안 직산에 있는 배나무숲 주차 돼요?", "expect": "answer", "expect_name": "배나무숲", "expect_addr": "직산",
     "must_any": ["무료"]},
    {"q": "천안 배나무숲 주차 어때요?", "expect": "clarify"},
    {"q": "당진 빙빙반점 차 가져가도 돼요?", "expect": "answer", "expect_name": "빙빙반점", "expect_addr": "당진",
     "must_any": ["불가|어렵|없"]},
    {"q": "부여 백제의집 갈 건데 근처 주차장 몇 시까지 해요?", "expect": "answer", "expect_name": "백제의집",
     "expect_addr": "부여", "must_any": ["19:00|19시|오후 7시"]},
    {"q": "천안 동남구 서부대로에 있는 에그드랍 주차장 있어요?", "expect": "answer", "expect_name": "에그드랍",
     "expect_addr": "서부대로", "must_any": ["매장.{0,6}확인|확인.{0,6}필요"]},
    {"q": "아산 다담삼계탕 주차 어디에 해요?", "expect": "answer", "expect_name": "다담삼계탕", "expect_addr": "아산",
     "must_any": ["없"]},
    {"q": "뚜레쥬르 아산모종점 주차장 있나요?", "expect": "answer", "expect_name": "뚜레쥬르 아산모종점",
     "expect_addr": "아산", "must_any": ["추정|가능성"]},
    {"q": "서천 장항횟집 주차 가능해요?", "expect": "answer", "expect_name": "장항횟집", "expect_addr": "서천",
     "must_any": ["추정|가능성|확인"], "must_not": ["주차 가능합니다"]},
    {"q": "천안 정든포차 근처 공영주차장 요금 알려줘", "expect": "answer", "expect_name": "정든포차", "expect_addr": "천안",
     "must_any": ["\\d+원|무료"]},
    {"q": "천안 맘스터치 주차 돼요?", "expect": "clarify"},
    {"q": "가나다라없는식당 주차 정보 알려줘", "expect": "not_found", "must_any": ["찾지 못|찾을 수 없|없습니다|없어요"]},
    {"q": "직산 배나무숲에서 주차권 등록은 어떻게 해요?", "expect": "answer", "expect_name": "배나무숲", "expect_addr": "직산",
     "must_any": ["매장.{0,8}확인|데이터에 없|정보가 없"], "must_not": ["카운터|차량번호"]},
    {"q": "서울 강남역 근처 주차장 알려줘", "expect": "out_of_scope", "must_any": ["충남|충청남도|어렵|할 수 없|드릴 수 없"]},
    {"q": "천안 정든포차에서 제일 가까운 주차장까지 걸어서 몇 분이에요?", "expect": "answer", "expect_name": "정든포차",
     "expect_addr": "천안", "must_any": ["약 ?\\d+ ?분|\\d+분 정도"]},
    {"q": "에그드랍 신불당점 지금 주차 자리 남았어요?", "expect": "answer", "expect_name": "에그드랍 신불당점",
     "expect_addr": "불당", "must_any": ["실시간|확인"]},
    {"q": "스타벅스 공주신관 주차 정보", "expect": "answer", "expect_name": "스타벅스 공주신관", "expect_addr": "공주"},
    {"q": "직산 배나무숲 근처에 명절에만 여는 주차장도 있어요?", "expect": "answer", "expect_name": "배나무숲",
     "expect_addr": "직산", "must_any": ["명절|추석"]},
]


def main() -> None:
    store = CardStore()
    names: dict[str, int] = {}
    for r in store.rows.values():
        names[r["place"]["name"]] = names.get(r["place"]["name"], 0) + 1
    rng = random.Random(SEED)
    pool = [r for r in store.rows.values()
            if names[r["place"]["name"]] == 1 and len(r["place"]["name"]) >= 3 and r["alternatives"]]
    rand = []
    for r in rng.sample(pool, N_RANDOM):
        p = r["place"]
        city = p["address"].split()[1][:-1]  # "천안시" → "천안"
        rand.append({"q": f"{city} {p['name']} 갈 때 주차 어디에 하면 돼요?", "expect": "answer",
                     "expect_name": p["name"], "expect_addr": p["address"].split()[1], "random": True})
    rows = HAND + rand
    for i, r in enumerate(rows):
        r["id"] = f"q{i + 1:02d}"
        if r["expect"] == "answer":  # 기대 매장이 데이터에 실제로 한 곳 있는지 확인
            n = sum(1 for x in store.rows.values()
                    if x["place"]["name"] == r["expect_name"] and r["expect_addr"] in x["place"]["address"])
            assert n == 1, (r["q"], n)
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    print(f"{len(rows)} questions -> {OUT}")


if __name__ == "__main__":
    main()
