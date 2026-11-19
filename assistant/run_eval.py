"""주차 비서 평가: eval_questions.jsonl 을 돌려 규칙으로 채점한다.

공유 LLM(랩 서버)에 부담을 주지 않도록 질문 사이에 쉬고, --limit 으로 개수를 줄일 수 있다.
사용: python assistant/run_eval.py [--limit 5] [--sleep 2] [--ids q01 q03]
출력: out/assistant_eval/<시각>.jsonl + 요약
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent import ParkingAssistant  # noqa: E402

QS = Path("assistant/eval_questions.jsonl")
# 데이터에 없는 사실을 지어냈다고 볼 표현(모든 질문 공통)
GLOBAL_FORBIDDEN = [r"\d+\s*시간\s*무료\s*(?:제공|혜택)", r"영수증.{0,10}(?:제시|보여)", r"차량\s*번호.{0,10}(?:등록|알려)"]
CLARIFY = r"어느|어떤 (?:지점|곳|매장)|어디(?:로|를|인지)|말씀하시는|중에서|알려 ?주시"


def grade(item: dict, out: dict, store) -> dict:
    ans = out["answer"]
    called = [c for c in out["tool_calls"] if c["name"] == "get_parking"]
    places = [store.rows.get(c["args"].get("place_id", ""), {}).get("place", {}) for c in called]
    checks = {}
    exp = item["expect"]
    if exp == "answer":
        checks["right_place"] = any(p.get("name") == item["expect_name"] and item["expect_addr"] in p.get("address", "")
                                    for p in places)
    elif exp == "clarify":
        checks["asked_branch"] = bool(re.search(CLARIFY, ans)) and not called
    elif exp in ("not_found", "out_of_scope"):
        checks["no_detail_call"] = not called
    for i, pat in enumerate(item.get("must_any", [])):
        checks[f"must_{i}"] = bool(re.search(pat, ans))
    for i, pat in enumerate(item.get("must_not", []) + GLOBAL_FORBIDDEN):
        checks[f"not_{i}"] = not re.search(pat, ans)
    return {"pass": all(checks.values()), "checks": checks}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--sleep", type=float, default=2.0)
    args = ap.parse_args()
    items = [json.loads(line) for line in QS.open(encoding="utf-8")]
    if args.ids:
        items = [x for x in items if x["id"] in args.ids]
    items = items[: args.limit] if args.limit else items

    bot = ParkingAssistant()
    outdir = Path("out/assistant_eval")
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    n_pass = 0
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            try:
                out = bot.ask(it["q"])
            except Exception as e:  # 한 문항 실패가 전체를 멈추지 않게
                out = {"answer": "", "tool_calls": [], "elapsed_s": None, "error": repr(e)}
            g = grade(it, out, bot.store)
            n_pass += g["pass"]
            f.write(json.dumps({**it, **out, **g}, ensure_ascii=False) + "\n")
            failed = [k for k, v in g["checks"].items() if not v]
            print(f"{it['id']} {'PASS' if g['pass'] else 'FAIL'} {out.get('elapsed_s')}s {out.get('provider')} {failed or ''} | {it['q']}",
                  flush=True)
            time.sleep(args.sleep)
    print(f"\n{n_pass}/{len(items)} pass, {bot.provider} {bot.model} -> {path}")


if __name__ == "__main__":
    main()
