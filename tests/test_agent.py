"""주차 비서 연결 순서·폴백 테스트(실제 LLM 호출 없음)."""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest
from openai import APIConnectionError, NotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "assistant"))
import agent  # noqa: E402

REQ = httpx.Request("POST", "http://llm/v1/chat/completions")


def reply(text):
    return NS(choices=[NS(message=NS(content=text, tool_calls=None))])


class FakeBackend:
    def __init__(self, provider, model, result):
        self.provider, self.model, self.wanted, self.result = provider, model, model, result
        self.calls = 0

    def complete(self, msgs):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return reply(self.result)


def test_falls_back_to_next_provider():
    lab = FakeBackend("lab", "qwen", APIConnectionError(request=REQ))
    orr = FakeBackend("openrouter", "nemo:free", "안녕하세요")
    out = agent.ParkingAssistant(store=NS(), backends=[lab, orr]).ask("천안 주차?")
    assert out["answer"] == "안녕하세요" and out["provider"] == "openrouter" and out["model"] == "nemo:free"
    assert out["failed"] == [{"provider": "lab", "error": "APIConnectionError"}] and lab.calls == 1


def test_first_provider_used_when_healthy():
    lab, orr = FakeBackend("lab", "qwen", "랩 답"), FakeBackend("openrouter", "nemo:free", "x")
    out = agent.ParkingAssistant(store=NS(), backends=[lab, orr]).ask("q")
    assert out["provider"] == "lab" and out["failed"] == [] and orr.calls == 0


def test_all_fail_raises():
    bots = [FakeBackend("lab", "q", RuntimeError("down")), FakeBackend("openrouter", "m:free", RuntimeError("429"))]
    with pytest.raises(RuntimeError):
        agent.ParkingAssistant(store=NS(), backends=bots).ask("q")


def test_provider_order_from_env(monkeypatch):
    monkeypatch.setattr(agent, "load_env", lambda: None)
    monkeypatch.setenv("LLM_PROVIDER", "lab, openrouter")
    monkeypatch.setenv("LLM_BASE_URL", "http://lab/v1")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "a/b:free")
    monkeypatch.delenv("OPENROUTER_FALLBACK_MODELS", raising=False)
    assert [c["provider"] for c in agent.llm_configs()] == ["lab", "openrouter"]
    monkeypatch.setenv("OPENROUTER_MODEL", "a/b")  # 유료 모델은 거부
    with pytest.raises(SystemExit):
        agent.llm_configs()


def test_lab_model_resolve_and_rename():
    b = agent.Backend({"provider": "lab", "base_url": "http://lab/v1", "api_key": "k", "model": "qwen-old",
                       "extra": {}, "timeout": 5, "retries": 0})
    served = ["qwen-new"]
    b.client = NS(models=NS(list=lambda: NS(data=[NS(id=i) for i in served])))
    assert b.resolve() == "qwen-new"  # 원하는 이름이 없으면 서버의 첫 모델
    seen = []

    def create(**kw):
        seen.append(kw["model"])
        if kw["model"] != served[0]:
            raise NotFoundError("model not found", response=httpx.Response(404, request=REQ), body=None)
        return reply("ok")

    b.client.chat = NS(completions=NS(create=create))
    served[0] = "qwen-newer"  # 서버가 재기동되어 이름이 또 바뀜
    assert b.complete([]).choices[0].message.content == "ok" and seen == ["qwen-new", "qwen-newer"]
