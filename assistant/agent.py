"""충남 매장 주차 비서: OpenAI 호환 채팅 API + 도구 호출.

LLM 연결은 환경변수(또는 .env)로만 바꾼다. LLM_PROVIDER 로 고른다.
  openrouter (기본, 데모용)
    OPENROUTER_API_KEY
    OPENROUTER_MODEL   반드시 ':free' 모델(유료 모델은 거부 — 과금 방지)
    OPENROUTER_FALLBACK_MODELS  쉼표 구분 ':free' 모델(선택). 앞 모델이 429·장애면 OpenRouter가 다음 모델로 넘긴다
  lab (개발용, 연구실 OpenAI 호환 서버. tailnet 안에서만 닿는다)
    LLM_BASE_URL, LLM_API_KEY
    LLM_MODEL     비우면 /models 첫 번째 모델(재기동마다 이름이 바뀐다)
    LLM_DISABLE_THINKING=1  Qwen3 계열 thinking 끄기(chat_template_kwargs)

사용: python assistant/agent.py "천안 배나무숲 주차 돼?"
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools import TOOL_SPECS, CardStore, dispatch  # noqa: E402

MAX_STEPS = 5

SYSTEM_PROMPT = """너는 충남 음식점 방문 운전자를 돕는 주차 안내 비서다. 한국어 존댓말로 짧게 답한다.

규칙
1. 주차 정보는 반드시 도구 결과에서만 가져온다. 매장을 모르면 search_places → get_parking 순서로 호출한다.
2. 검색 결과가 여러 곳이고 사용자가 지점을 특정하지 않았으면, 후보 2~3곳(주소 포함)을 보여주고 어느 곳인지 묻는다.
3. 도구 결과에 없는 것(주차권 처리 방법, 할인·무료 조건, 건물 안 동선, 실시간 빈자리)은 지어내지 말고 "데이터에 없어 매장에 확인이 필요하다"고 말한다.
4. "(추정)"이 붙은 정보, walk_minutes_estimate, entrance_source가 '입구 미확인'이거나 '(추정)'인 값은 추정이라고 밝힌다. 도보 시간은 "약 3분"처럼 앞에 "약"을 붙여 말한다.
5. parking_status가 unknown이면 주차 가능/불가를 단정하지 않는다. 건물 주차장 정보만 있으면 "손님 이용 가능 여부는 매장 확인 필요"를 덧붙인다.
6. 주변 주차장을 권할 때는 가까운 순으로 최대 2곳, 이름·거리·요금·운영시간을 말한다. holiday_only=true 주차장은 명절에만 열린다고 밝힌다. 요금이 null이면 "요금 정보 없음"이라고 한다.
7. hours_warning이나 operating_hours_warning이 있으면 꼭 전한다.
8. 답 끝에 한 줄로 데이터 기준(예: "건축물대장 2024-12, 공영주차장 2026-11 조회 기준")을 붙인다.
9. 충남 음식점이 아닌 질문이나 도구로 답할 수 없는 질문은 할 수 없다고 짧게 말한다."""


def load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip().strip('"'))


OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def llm_config() -> dict | None:
    """환경변수에서 LLM 연결을 고른다. 설정이 모자라면 None."""
    load_env()
    provider = os.environ.get("LLM_PROVIDER", "openrouter").strip().lower()
    if provider == "openrouter":
        key, model = os.environ.get("OPENROUTER_API_KEY"), os.environ.get("OPENROUTER_MODEL", "").strip()
        if not key or not model:
            return None
        fallbacks = [m.strip() for m in os.environ.get("OPENROUTER_FALLBACK_MODELS", "").split(",") if m.strip()]
        for m in [model, *fallbacks]:
            if not m.endswith(":free"):
                raise SystemExit(f"OpenRouter 모델 {m}: ':free' 모델만 허용합니다(유료 과금 방지)")
        extra = {"models": [model, *fallbacks]} if fallbacks else {}
        return {"provider": provider, "base_url": OPENROUTER_BASE, "api_key": key, "model": model, "extra": extra}
    if provider == "lab":
        base, key = os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_API_KEY")
        if not base or not key:
            return None
        extra = ({"chat_template_kwargs": {"enable_thinking": False}}
                 if os.environ.get("LLM_DISABLE_THINKING") == "1" else {})
        return {"provider": provider, "base_url": base, "api_key": key,
                "model": os.environ.get("LLM_MODEL", "").strip(), "extra": extra}
    raise SystemExit(f"LLM_PROVIDER={provider}: openrouter 또는 lab")


class ParkingAssistant:
    def __init__(self, store: CardStore | None = None):
        cfg = llm_config()
        if cfg is None:
            raise SystemExit("LLM 설정이 없습니다: OPENROUTER_API_KEY·OPENROUTER_MODEL 또는 LLM_PROVIDER=lab + LLM_*")
        self.provider = cfg["provider"]
        self.client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=120, max_retries=2)
        self.model = cfg["model"] or self.client.models.list().data[0].id
        self.extra = cfg["extra"]
        self.store = store or CardStore()

    def ask(self, question: str, history: list[dict] | None = None) -> dict:
        """한 질문에 답한다. 반환: answer, tool_calls(이름·인자), elapsed_s."""
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}, *(history or []), {"role": "user", "content": question}]
        calls = []
        t0 = time.time()
        for _ in range(MAX_STEPS):
            resp = self.client.chat.completions.create(
                model=self.model, messages=msgs, tools=TOOL_SPECS, temperature=0.2, max_tokens=800,
                extra_body=self.extra or None)
            m = resp.choices[0].message
            if not m.tool_calls:
                return {"answer": (m.content or "").strip(), "tool_calls": calls, "elapsed_s": round(time.time() - t0, 1)}
            msgs.append({"role": "assistant", "content": m.content or "",
                         "tool_calls": [tc.model_dump() for tc in m.tool_calls]})
            for tc in m.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = dispatch(self.store, tc.function.name, args)
                calls.append({"name": tc.function.name, "args": args})
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})
        return {"answer": "죄송합니다. 지금은 답을 정리하지 못했습니다. 매장 이름을 다시 알려주세요.",
                "tool_calls": calls, "elapsed_s": round(time.time() - t0, 1), "step_limit": True}


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    a = ParkingAssistant()
    out = a.ask(" ".join(sys.argv[1:]))
    print(f"[{a.provider} {a.model}] [{out['elapsed_s']}s] tools: {out['tool_calls']}\n")
    print(out["answer"])


if __name__ == "__main__":
    main()
