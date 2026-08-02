# 레시피 — 측정 결과 반영·새 측정 도구

측정 거버넌스 전문은 `docs/trading/measurement.md` — **판정 원칙 7개를 먼저
읽는다.** 여기는 코드 절차만.

## 측정 결과를 코드에 반영할 때

1. 그 측정의 **판정 기준이 사전에 못박혀 있었는가** 확인 — 없었으면 반영 전에
   사용자에게 그 사실을 밝힌다
2. 반영은 **되돌릴 수 있는 형태로** — config 값·시간창·게이트 파라미터.
   구조 변경이 필요하면 관측을 유지한 채(백테스트는 게이트 밖) 적용한다
3. **미채택도 기록한다** — 다중 비교 이상치·표본 미달로 보류한 것을
   measurement.md 에 남긴다 (13:30 버킷 선례)
4. 채택·기각 모두 measurement.md 원장에 한 절 추가 (측정일·표본·결정·근거)

## 새 측정(연구) 도구

1. 모듈: `trading/app/research/<이름>.py`
   - docstring: 무엇을 재나 / **판정 기준(미리 못박음)** / 근사와 한계
   - `run_once()` → `data/<이름>.json` 저장, `latest()` 로 조회
   - 무거우면 `backtest/job.py` JOBS 등록 (오프로드 자식 프로세스)
2. IC 를 재면 **잔차화(atr_pct 통제) + 본페로니**를 기본으로 — 발굴 점수가
   베타 프록시로 판명된 전철
3. **IC ≠ R** — 선별 신호는 IC 통과 후에도 랭킹 하네스(브래킷 실현 R)로
   재검증해야 편입 논의가 가능하다 (atr_pct: 잔차 IC +0.39 → 브래킷 −0.49)
4. 유니버스를 실전과 맞춘다 — ETF 는 이름 제외(`data/exclude.is_excluded`).
   유니버스 오염이 측정 둘을 반대 방향으로 왜곡한 전례가 있다
5. 표본 미달이면 **판정하지 말고** 필요 표본과 도달일을 보고 — 기준 완화 금지

## 서버에서 측정 실행

- 30분 넘거나 중요하면 **systemd-run 독립 유닛** (MCP run_command 자식은
  MCP 재시작에 죽는다 — 실측 2026-08-01 두 번):
  `systemd-run --collect --unit=<이름> -p User=hosub -p WorkingDirectory=/opt/hosub-trading/trading -p StandardOutput=append:/tmp/<이름>.out -p StandardError=append:/tmp/<이름>.err <venv python> -m app.backtest.job <잡>`
- 완료 확인·판독은 send_later 예약으로 (폴링 sleep 금지)
- API 대량 조회(백필)는 키움 점검 시간(주말 저녁~) 회피, 페이지네이션 확인
  (ka10086 은 페이지당 20일 — 실측 2026-08-02)

## 판정 예약

구현과 동시에 4주(또는 사전 정의 기간) 뒤 판정 트리거를 등록한다 — 질문은
docstring 그대로, "표본 미달이면 연기 보고" 조항 포함.
