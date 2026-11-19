# AI — 충남 매장 주차 정보 데이터·AI 파트

충남 음식점 39,365곳마다 "차로 가면 주차가 어떤가"를 공공데이터로 미리 계산해 두는 파이프라인입니다.
결과물은 `frontend/docs/API.md`(v0.1)의 `GET /places/{placeId}/parking` 응답 모양 그대로라서,
백엔드는 매장 ID로 한 줄을 찾아 돌려주면 됩니다.

- 현장 수집 없음, 공공데이터와 OSM만 씁니다. 유료 API는 쓰지 않습니다.
- 근거가 없으면 "정보 없음"(`parkingStatus: unknown`)으로 둡니다. 모르는 값을 무료·0분으로 채우지 않습니다.
- 추정값은 문구에 `(추정)`을 붙이거나 `walkingMinutesIsEstimate`, `entranceSource` 같은 필드로 표시합니다.

## 결과 파일

전체 결과(각 100MB 안팎)는 레포에 넣지 않고 [Releases](../../releases)에 gzip으로 올립니다.

| 파일 | 내용 |
| --- | --- |
| `place_parking_cn.jsonl` | 한 줄 = 매장 하나, API v0.1 응답 형태 |
| `parking_cards_cn.jsonl` / `.csv` | 같은 내용의 분석용 평면 테이블(출처별 칸 분리, LightGBM 확률 포함) |
| `sources.json` | 출처별 기준일 |
| `samples/place_parking_sample.jsonl` | 화면 경우의 수별 8곳(레포에 포함) |

### 커버리지 (2026-11-16 생성본)

| 항목 | 매장 수 | 비율 |
| --- | ---: | ---: |
| 건물 주차 정보: 건축물대장 | 20,615 | 52.4% |
| 건물 주차 정보: LightGBM 추정 | 18,750 | 47.6% |
| 매장 단위 주차 정보(TourAPI) | 190 | 0.5% |
| 800m 안 대체 주차장 1곳 이상 | 34,074 | 86.6% |
| 그중 공영 주차장(요금·운영시간 있음) | 23,856 | 60.6% |
| 대체 주차장 입구 좌표가 대표 좌표가 아닌 것 | 5,159 | 13.1% |
| 첫 줄 문구가 "정보 없음" | | 18.4% |

`parkingStatus`: available 20,504 / unavailable 203 / unknown 18,658.

## API v0.1 과 다른 점 (프론트·백엔드 협의 필요)

1. **`primaryParking[].lot.entranceCoordinates`가 null일 수 있습니다.** 건물 주차장 입구를 아는 곳은 101곳뿐입니다. 계약서대로 null 허용과 출발 버튼 차단 정책이 필요합니다.
2. 대체 주차장 입구를 모르면 주차장 대표 좌표를 넣고 `entranceSource: "주차장 대표 좌표(입구 미확인)"`로 표시합니다. 내비 목적지로는 쓸 수 있지만 "입구"라고 안내하면 안 됩니다.
3. 추가 필드: `lot.entranceSource`, `lot.feeNote`(공영 주차장 비고 원문), `lot.source`, `lot.holidayOnly`(명절에만 개방), `walkingMinutesIsEstimate`, `distanceMeters`, 대체 주차장별 `operatingHoursWarning`, `primaryParking[].evidence`.
4. `walkingMinutes`는 직선거리 × 1.3 ÷ 75m/분 추정치입니다. API.md는 실제 보행 경로를 요구하므로 경로 API를 붙이기 전까지는 "약 N분"으로 표시하는 편이 맞습니다.
5. 건축물대장에 주차면이 있으면 `available`로 두되 요약에 "손님 이용 여부는 매장 확인 필요"를 붙입니다. TourAPI와 건축물대장이 둘 다 있는 101곳 중 7곳은 건물에 주차면이 있는데 매장은 "주차 불가"였습니다.
6. 운영시간 경고는 매장 영업시간(TourAPI)과 공영 주차장 평일 마감이 둘 다 있어야 계산됩니다. 지금은 7곳만 나옵니다.

## 데이터 출처

| 출처 | 쓰는 곳 | 기준 |
| --- | --- | --- |
| 소상공인시장진흥공단 상가(상권)정보 (15083033) | 매장 목록·좌표·지번 | 2026-09 |
| 한국교통안전공단 건축물대장 주차정보 (15135777) | 건물 주차면·옥내/옥외·기계식 | 2024-12 |
| 한국관광공사 TourAPI KorService2 | 매장 주차 가능 여부·영업시간 | 2026-11-16 조회 |
| 전국주차장정보표준데이터 OpenAPI (15012896) | 공영 주차장 이름·요금·운영시간 | 2026-11-16 조회 |
| 명절 무료개방 주차장 (15099790) | 명절 한정 대체 주차장 | 2026 추석 |
| OpenStreetMap (Overpass) | 주차장 위치, 입구(`parking_entrance`, 진출입로 교차점) | 2026-11-16 조회, © OpenStreetMap contributors, ODbL |
| 천안시 주차장 현황 (15110970) | 표준데이터에 없는 천안 주차장 4곳(유료/무료·면수만) | 기준일 미표기 |
| 카카오 로컬 주소 검색 | 좌표 없는 공영 주차장 91곳 지오코딩 | 2026-11-16 |

## 실행

Python 3.12, `pip install -r requirements.txt`. 키는 `.env.example`을 `.env`로 복사해 채웁니다(커밋 금지).

```bash
python tools/fetch_public_lots.py          # 공영 주차장(충남 1,164곳) → data/public_lots/
python tools/geocode_missing_lots.py --src data/public_lots/public_lots_chungnam.csv
python tools/fetch_tourapi.py              # TourAPI 충남 음식점 240곳 → data/tourapi/
python tools/fetch_osm_entrances.py        # OSM 입구 후보 → data/osm_entrance/raw.json (Overpass가 붐비면 재시도)
python tools/build_parking_cards.py        # → data/cards/ (약 15초)
python tools/make_samples.py               # → samples/
```

`tools/`의 스크립트끼리 import하므로 `tools/` 안에서 실행하거나 `PYTHONPATH=tools`를 줍니다.
상가정보·건축물대장 등 파일 데이터(`data/bundle_cn/`)와 LightGBM 예측(`out/cv_2421_cn/pred_unmatched.csv`)은 Releases의 입력 묶음에 있습니다.
원본은 `tools/dgk_download.py <outdir> <공공데이터 번호>`로 받을 수 있습니다.

### LightGBM 건물 주차 추정

건축물대장과 지번이 안 맞는 매장(47.6%)의 건물 주차 구성을 매장 정보와 주변 맥락으로 추정합니다.
학습은 연구실 Slurm 클러스터 CPU 노드에서 돌렸습니다(8코어, 약 3분).

```bash
python tools/train_parking_lgbm.py --data data/bundle_cn --out out/cv_2421_cn \
  --sangga sangga_chungnam.csv.gz --buildings building_parking_cn.csv \
  --lots osm_parking_bbox.csv --holiday-region '^(?:충남|충청남도)'
```

지번 단위 GroupKFold 5-fold × 3시드로 평가하며, 평가 지번은 학습에 쓰지 않고 baseline도 같은 fold에서 잽니다(`results/`).

| 충남, 지번 단위 분할 | LightGBM | 이웃 5곳(B_knn5) | 법정동 평균(B_dong) | 사전확률 |
| --- | ---: | ---: | ---: | ---: |
| 옥내 자주식 있음 AUC | 0.841 | 0.752 | 0.744 | 0.500 |
| 기계식 있음 PR-AUC | 0.216 | 0.148 | 0.115 | 0.037 |
| 주차면 3구간 macro-F1 | 0.655 | 0.531 | 0.457 | 0.225 |

기계식 예측은 절대 수준이 낮아 화면에 쓰지 않습니다. 법정동 단위로 나누면 성능이 떨어집니다(AUC 0.733). 자세한 수치는 `results/*.json`.

## API 서버

`server/app.py`(FastAPI): 프론트 `docs/API.md` v0.1 계약을 그대로 서빙하고 주차 비서 채팅을 붙였습니다.

| Method | URL | 설명 |
| --- | --- | --- |
| GET | `/places?query=천안 빙빙반점&limit=20` | 검색. 시·군 이름은 주소 필터, 나머지는 매장명·업종. 빈 검색은 빈 결과 |
| GET | `/places/{placeId}/parking` | 목적지 주차 상세(API v0.1 응답 그대로). 없으면 404 `PLACE_NOT_FOUND` |
| POST | `/assistant/chat` | `{"message": "...", "history": [{"role": "user", "content": "..."}]}` → `{answer, toolCalls, model}`. LLM 미설정·장애면 503 |
| GET | `/health` | 매장 수, 출처 기준일 |

```bash
python tools/build_parking_cards.py                 # data/cards/place_parking_cn.jsonl (또는 Releases에서 받기)
uvicorn server.app:app --port 8000                  # 기동 약 1초, 메모리 약 400MB
python -m pytest -q tests                           # 계약·점주·피드백 테스트 17개(샘플 8곳으로)
```

프론트는 `VITE_API_BASE_URL=http://127.0.0.1:8000 npm run dev`로 붙습니다(2026-11-17 확인: 검색 → 상세 → 요금·운영시간·입구 안내 표시).
CORS 허용 출처는 `ALLOWED_ORIGINS`(쉼표 구분, 기본 `localhost:5173`).

### Docker

빌드할 때 Release `data-2026-11-17`의 카드(`place_parking_cn.jsonl.gz`, `sources.json`)를 받아 이미지에 넣습니다. 키는 이미지에 넣지 않고(`.dockerignore`가 `.env` 제외) 실행할 때 환경변수로 줍니다.

```bash
docker build -t parking-ai .                        # 다른 데이터: --build-arg DATA_RELEASE=data-YYYY-MM-DD
docker run --rm -p 7860:7860 \
  -e LLM_PROVIDER=openrouter -e OPENROUTER_API_KEY=... -e OPENROUTER_MODEL=... \
  -e OPENROUTER_FALLBACK_MODELS=... -e ADMIN_API_KEY=... -e ALLOWED_ORIGINS=https://프론트주소 \
  parking-ai
curl localhost:7860/health
```

이미지 약 560MB, 실행 메모리 약 430MB입니다(2026-11-19 측정). 점주 입력·피드백 SQLite(`/app/data/owner.db`)는 컨테이너 안에만 있어서 재시작하면 사라집니다.
백엔드는 이 서버 주소를 `AI_ASSISTANT_URL`로 받아 `/assistant/chat`을 중계합니다(parking-backend PR #2).

## 점주 입력 · 정확도 피드백

공개 데이터에 없는 매장 주차 방식(자체·제휴·불가), 할인·무료 조건, 주차 등록·정산 방법, 입구 안내를 점주가 채웁니다.
저장하면 상세 응답(`primaryParking`)·검색 요약·주차 비서 답에 바로 반영되고 `ownerVerified: true`, `verifiedAt`(입력 시각), 요약 끝 "점주 확인"이 붙습니다. 주변 주차장은 그대로 둡니다.

- 사업자 인증은 하지 않습니다. 관리자가 매장별 점주 키를 발급하고(`POST /admin/owner-keys`, 헤더 `X-Admin-Key`), 점주는 그 키(`X-Owner-Key`)로 자기 매장만 고칠 수 있습니다. DB에는 키 해시만 저장합니다.
- 데모용 입력 페이지: `http://127.0.0.1:8000/owner` (매장 검색 → 점주 키 → 입력·저장·되돌리기).
- 정확도 피드백: `POST /parking-lots/{lotId}/feedback {"placeId", "accurate", "comment"}`, 같은 사람·같은 주차장은 1시간에 한 번(429). 집계 `GET /parking-lots/{lotId}/feedback`, 최근 목록 `GET /admin/feedback`.
- 저장소는 SQLite(`OWNER_DB`, 기본 `data/owner.db`). 서버를 여러 대 띄우면 공유 DB가 필요합니다.

```bash
curl -X POST http://127.0.0.1:8000/admin/owner-keys -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H 'Content-Type: application/json' -d '{"placeId": "sangga-..."}'
```

## 주차 비서 (LLM + 도구 호출)

`assistant/`: 사용자가 "천안 ○○ 주차 돼요?"처럼 물으면 LLM이 `search_places` → `get_parking` 도구로 카드 데이터를 찾아 답합니다.
LLM은 `.env`의 `LLM_PROVIDER`로 고릅니다.

- `openrouter`(기본, 데모용): `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`(기본 `nvidia/nemotron-3-ultra-550b-a55b:free`), `OPENROUTER_FALLBACK_MODELS`(앞 모델이 429·장애일 때 OpenRouter가 넘길 모델). **`:free`가 아닌 모델은 코드에서 거부**합니다(과금 방지). 크레딧 없는 무료 계정은 하루 요청 수 제한이 있고, 질문 하나에 요청 2~3번이 듭니다.
- `lab`(개발용): 연구실 OpenAI 호환 서버(`LLM_BASE_URL`·`LLM_API_KEY`·`LLM_MODEL`). tailnet 안에서만 닿아 데모에는 쓰지 않습니다.

- 프롬프트 규칙: 도구 결과에 없는 것(주차권 처리, 할인 조건, 실시간 빈자리)은 지어내지 않고 매장 확인을 안내, 추정값은 추정이라고 밝힘, 같은 이름이 여러 곳이면 지점을 되물음, 운영시간 경고는 반드시 전달.
- 평가: `assistant/eval_questions.jsonl` 25문항(경우별 17 + 무작위 매장 8). 맞는 매장을 조회했는지, 되묻기·없음 처리, 필수/금지 표현을 규칙으로 채점합니다.

- 결과(2026-11-17, temperature 0.2)
  - OpenRouter `nemotron-3-ultra-550b-a55b:free`: 6문항(q01·q02·q06·q10·q11·q12) **6/6 통과**, 응답 4~16초. 무료 한도 때문에 25문항 전체는 아직 안 돌렸습니다. 사용 금액 0.
  - 랩 `qwen3.8-27b`(thinking 끔): **24/25 통과, 2회 반복 동일**. 응답 1~2초.
  유일한 실패 q12("주차권 등록은 어떻게 해요?")는 도구를 부르지 않고 "데이터에 없어 매장 확인 필요"라고만 답한 경우로, 내용은 맞지만 채점 기준(매장 조회 필수)에 걸렸습니다. 채점 기준은 그대로 둡니다.
  답 25개를 사람이 읽어 확인한 범위에서 데이터에 없는 요금·조건을 지어낸 답은 없었습니다.

```bash
python assistant/agent.py "천안 직산 배나무숲 주차 돼요?"
python assistant/run_eval.py --limit 5 --sleep 2     # 결과 out/assistant_eval/
python assistant/make_eval.py                        # 질문셋 재생성(고정 시드)
```

## 못 하는 것

- 건물 내부 도보 동선, 주차권 처리 방법, 매장별 무료 조건: 공개 데이터에 없어 점주가 입력한 매장만 채워집니다.
- 실제 입구 사진: 수집하지 않습니다(`entrancePhotoUrl: null`).
- 민영 주차장 요금: 표준데이터의 충남 주차장은 거의 전부 공영입니다(민영 2곳).
