"""백테스트 자식 프로세스 진입점 — `python -m app.backtest.job <report|sweep>`.

부모(트레이딩 서비스)의 이벤트 루프와 GIL 을 공유하지 않기 위해 별도 프로세스로
실행된다(offload.run_job). 결과는 SQLite 에 직접 쓰고, 요약만 stdout JSON 으로
낸다. 로그는 stderr 로 보내 stdout 을 JSON 전용으로 유지한다.
"""
import json
import logging
import sys

JOBS = ("report", "sweep", "study", "rank", "news", "trailing", "timeofday",
        "flowsignal")


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    job = argv[1] if len(argv) > 1 else ""
    if job not in JOBS:
        print(json.dumps({"ok": False, "error": f"알 수 없는 작업: {job}"}))
        return 2
    if job == "report":
        from .report import BacktestReporter

        result = BacktestReporter().run_once()
    elif job == "study":
        from ..research import eventstudy

        result = eventstudy.run_once()
    elif job == "rank":
        from ..research import ranking

        result = ranking.run_once()
    elif job == "news":
        from ..research import newsimpact

        result = newsimpact.run_once()
    elif job == "trailing":
        from ..research import trailing

        result = trailing.run_once()
    elif job == "timeofday":
        from ..research import timeofday

        result = timeofday.run_once()
    elif job == "flowsignal":
        from ..research import flowsignal

        result = flowsignal.run_once()
    else:
        from .sweep import run_sweep

        result = run_sweep()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":       # pragma: no cover - 자식 프로세스 진입점
    sys.exit(main(sys.argv))
