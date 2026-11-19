"""충남 매장 주차 비서: OpenAI 호환 채팅 API + 도구 호출.

LLM 연결은 환경변수(또는 .env)로만 바꾼다. LLM_PROVIDER 에 쉼표로 순서를 주면 앞에서부터 쓰고,
앞 연결이 장애·시간 초과면 같은 질문을 다음 연결로 다시 묻는다(예: LLM_PROVIDER=lab,openrouter).
  openrouter (OpenRouter 무료 모델)
    OPENROUTER_API_KEY
    OPENROUTER_MODEL   반드시 ':free' 모델(유료 모델은 거부 — 과금 방지)
    OPENROUTER_FALLBACK_MODELS  쉼표 구분 ':free' 모델(선택). 앞 모델이 429·장애면 OpenRouter가 다음 모델로 넘긴다
  lab (연구실 OpenAI 호환 서버. tailnet 안에서만 닿는다)
    LLM_BASE_URL, LLM_API_KEY
    LLM_MODEL     비우거나 서버에 없으면 /models 첫 번째 모델(재기동마다 이름이 바뀐다)
    LLM_DISABLE_THINKING=1  Qwen3 계열 thinking 끄기(chat_template_kwargs)
    LLM_TIMEOUT   초(기본 45). 넘으면 다음 연결로 넘어간다

사용: python assistant/agent.py "천안 배나무숲 주차 돼?"
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from openai import NotFoundError, OpenAI

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
9. 충남 음식점이 아닌 질문이나 도구로 답할 수 없는 질문은 할 수 없다고 짧게 말한다.
10. own_parking의 evidence_source가 '점주 입력'이면 benefit(할인·무료 조건), how_to_validate(주차 등록·정산 방법), entrance_guide를 그대로 전하고, "점주가 YYYY-MM-DD에 입력한 정보"라고 owner_verified_at 날짜를 밝힌다."""


def load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip().strip('"'))


OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _provider_config(provider: str) -> dict | None:
    if provider == "openrouter":
        key, model = os.environ.get("OPENROUTER_API_KEY"), os.environ.get("OPENROUTER_MODEL", "").strip()
        if not key or not model:
            return None
        fallbacks = [m.strip() for m in os.environ.get("OPENROUTER_FALLBACK_MODELS", "").split(",") if m.strip()]
        for m in [model, *fallbacks]:
            if not m.endswith(":free"):
                raise SystemExit(f"OpenRouter 모델 {m}: ':free' 모델만 허용합니다(유료 과금 방지)")
        extra = {"models": [model, *fallbacks]} if fallbacks else {}
        return {"provider": provider, "base_url": OPENROUTER_BASE, "api_key": key, "model": model, "extra": extra,
                "timeout": 60.0, "retries": 1}
    if provider == "lab":
        base, key = os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_API_KEY")
        if not base or not key:
            return None
        extra = ({"chat_template_kwargs": {"enable_thinking": False}}
                 if os.environ.get("LLM_DISABLE_THINKING") == "1" else {})
        return {"provider": provider, "base_url": base, "api_key": key,
                "model": os.environ.get("LLM_MODEL", "").strip(), "extra": extra,
                "timeout": float(os.environ.get("LLM_TIMEOUT", "45")), "retries": 0}
    raise SystemExit(f"LLM_PROVIDER={provider}: openrouter 또는 lab")


def llm_configs() -> list[dict]:
    """LLM_PROVIDER 순서대로 설정이 갖춰진 연결 목록. 하나도 없으면 빈 목록."""
    load_env()
    names = [p.strip().lower() for p in os.environ.get("LLM_PROVIDER", "openrouter").split(",") if p.strip()]
    return [c for c in (_provider_config(n) for n in names) if c is not None]


def llm_config() -> dict | None:
    """첫 번째 연결(하위 호환)."""
    cfgs = llm_configs()
    return cfgs[0] if cfgs else None


class Backend:
    """LLM 연결 하나. lab 은 모델 이름이 재기동마다 바뀌므로 서버 목록과 대조해 고른다."""

    def __init__(self, cfg: dict):
        self.provider = cfg["provider"]
        self.client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=cfg["timeout"],
                             max_retries=cfg["retries"])
        self.wanted = cfg["model"]
        self.extra = cfg["extra"]
        self.model = cfg["model"] if self.provider == "openrouter" else ""

    def resolve(self) -> str:
        """lab: 원하는 모델이 서버에 있으면 그것, 없으면 첫 번째 모델."""
        if self.provider == "openrouter":
            return self.model
        ids = [m.id for m in self.client.models.list().data]
        if not ids:
            raise RuntimeError("LLM 서버에 모델이 없습니다")
        chosen = self.wanted if self.wanted in ids else ids[0]
        if self.wanted and chosen != self.wanted:
            print(f"[agent] LLM_MODEL={self.wanted} 없음 → {chosen}", file=sys.stderr, flush=True)
        self.model = chosen
        return chosen

    def complete(self, msgs: list[dict]):
        if not self.model:
            self.resolve()
        try:
            return self._create(msgs)
        except NotFoundError:  # 재기동으로 모델 이름이 바뀐 경우 한 번만 다시 고른다
            if self.provider != "lab":
                raise
            self.resolve()
            return self._create(msgs)

    def _create(self, msgs: list[dict]):
        return self.client.chat.completions.create(
            model=self.model, messages=msgs, tools=TOOL_SPECS, temperature=0.2, max_tokens=800,
            extra_body=self.extra or None)


class ParkingAssistant:
    def __init__(self, store: CardStore | None = None, backends: list | None = None):
        if backends is None:
            cfgs = llm_configs()
            if not cfgs:
                raise SystemExit("LLM 설정이 없습니다: OPENROUTER_API_KEY·OPENROUTER_MODEL 또는 LLM_PROVIDER=lab + LLM_*")
            backends = [Backend(c) for c in cfgs]
        self.backends = backends
        self.store = store or CardStore()

    @property
    def provider(self) -> str:
        return ",".join(b.provider for b in self.backends)

    @property
    def model(self) -> str:
        return self.backends[0].model or self.backends[0].wanted or "(서버 기본)"

    def ask(self, question: str, history: list[dict] | None = None) -> dict:
        """한 질문에 답한다. 앞 연결이 실패하면 다음 연결로 처음부터 다시 묻는다.
        반환: answer, tool_calls(이름·인자), elapsed_s, provider, model, failed(건너뛴 연결)."""
        failed = []
        t0 = time.time()
        for i, b in enumerate(self.backends):
            try:
                out = self._ask(b, question, history)
            except Exception as e:  # 마지막 연결까지 실패하면 호출자에게 넘긴다
                failed.append({"provider": b.provider, "error": type(e).__name__})
                print(f"[agent] {b.provider} 실패({type(e).__name__}: {str(e)[:200]})"
                      + (" → 다음 연결" if i + 1 < len(self.backends) else ""), file=sys.stderr, flush=True)
                if i + 1 == len(self.backends):
                    raise
                continue
            return {**out, "elapsed_s": round(time.time() - t0, 1), "provider": b.provider, "model": b.model,
                    "failed": failed}
        raise RuntimeError("LLM 연결이 없습니다")

    def _ask(self, b: Backend, question: str, history: list[dict] | None) -> dict:
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}, *(history or []), {"role": "user", "content": question}]
        calls = []
        for _ in range(MAX_STEPS):
            m = b.complete(msgs).choices[0].message
            if not m.tool_calls:
                return {"answer": (m.content or "").strip(), "tool_calls": calls}
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
                "tool_calls": calls, "step_limit": True}


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    a = ParkingAssistant()
    out = a.ask(" ".join(sys.argv[1:]))
    print(f"[{out['provider']} {out['model']}] [{out['elapsed_s']}s] tools: {out['tool_calls']} 건너뜀: {out['failed']}\n")
    print(out["answer"])


if __name__ == "__main__":
    main()
