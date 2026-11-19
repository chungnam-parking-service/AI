# 충남 매장 주차 안내 API(server/app.py) 배포 이미지.
# 카드 데이터는 git에 없으므로 빌드 때 GitHub Release에서 받아 이미지에 넣는다.
#
#   docker build -t parking-ai .
#   docker run --rm -p 7860:7860 --env-file .env parking-ai
#
# 키(OPENROUTER_API_KEY, ADMIN_API_KEY 등)는 이미지에 넣지 않고 실행 환경변수로 준다.
# Hugging Face Spaces(Docker)에서도 그대로 쓴다(기본 포트 7860, UID 1000).
FROM python:3.12-slim

ARG DATA_RELEASE=data-2026-11-17
ARG DATA_BASE=https://github.com/chungnam-parking-service/AI/releases/download

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    CARDS_PATH=/app/data/cards/place_parking_cn.jsonl \
    OWNER_DB=/app/data/owner.db

RUN useradd -m -u 1000 app
WORKDIR /app

COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

# 카드(API 응답 형식) + 출처 기준일. 받은 뒤 gzip 무결성과 JSON 첫 줄을 확인한다.
ADD ${DATA_BASE}/${DATA_RELEASE}/place_parking_cn.jsonl.gz /tmp/cards.jsonl.gz
ADD ${DATA_BASE}/${DATA_RELEASE}/sources.json data/cards/sources.json
RUN gzip -t /tmp/cards.jsonl.gz \
    && gunzip -c /tmp/cards.jsonl.gz > data/cards/place_parking_cn.jsonl \
    && rm /tmp/cards.jsonl.gz \
    && python -c "import json,sys; f=open('data/cards/place_parking_cn.jsonl',encoding='utf-8'); json.loads(f.readline()); n=1+sum(1 for _ in f); print('cards', n); sys.exit(n < 1000)" \
    && python -c "import json; json.load(open('data/cards/sources.json',encoding='utf-8'))"

COPY assistant/ assistant/
COPY server/ server/
RUN chown -R app:app /app
USER app

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"
CMD ["sh", "-c", "exec uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
