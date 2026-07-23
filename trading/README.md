# hosub-trading

키움증권 REST API 기반 **개인 반자동 트레이딩 대시보드**.
하락장 차트 매매 규칙(ORB, 갭 매매, 반등 페이드, 지지선 붕괴 리테스트)을 신호로 만들고,
**사람이 대시보드에서 승인해야만** 주문이 나가는 반자동 구조다.

## 안전 원칙 (설계의 최우선순위)

1. **기본값은 모의투자** — `KIWOOM_ENV=mock` 이 기본. 실전(`real`) 전환은 `.env` 에서 명시적으로만 한다.
2. **완전 자동 발주 없음** — 신호는 `pending` 큐에 쌓이고, 대시보드에서 승인해야 발주된다.
   승인 대기 신호는 TTL(기본 10분)이 지나면 자동 만료된다.
3. **Exit 우선** — 모든 진입 신호는 손절가·목표가가 계산된 상태로만 생성된다.
4. **리스크 한도** — 1회 리스크(계좌의 %) / 일일 손실 한도 초과 시 신규 신호 발주 차단.
5. **비용 반영 백테스트** — 수수료 + 매도 거래세 + 슬리피지를 뺀 성과만 신뢰한다.

## 구조

```
trading/
  app/
    settings.py        # .env + config.yaml 로드
    kiwoom/            # 키움 REST API 클라이언트 (auth / REST / WebSocket)
    data/              # SQLite 시세 저장(store) + 수집기(collector)
    signals/           # 지표(indicators) / 규칙(rules) / 평가 루프(engine)
    trade/             # 리스크 계산(risk) / 승인 큐·주문(orders)
    backtest/          # 비용 반영 백테스터
    main.py            # FastAPI 앱 (대시보드 + API)
  templates/dashboard.html
  deploy/              # systemd unit, Caddy 스니펫
  tests/
```

## 매매 규칙 (딥 리서치에서 교차 검증된 것만)

| 규칙 | 방향 | 요약 |
|---|---|---|
| ORB | 롱/숏 | 9:00~9:15 시초가 범위 돌파. 손절 = 범위 반대끝, 목표 = 1.5R |
| 갭 매매 | 롱/숏 | 첫 1시간 범위 형성 대기 후 돌파. 트레일링 롱 -8% / 숏 -4% |
| 반등 페이드 | 숏 | 하락 추세(VWAP 아래 + 저점 갱신)에서 VWAP/20MA 반등 소진 시 매도 |
| 지지 붕괴 리테스트 | 숏 | 지지선 붕괴 후 되돌림이 옛 지지선(현 저항)에서 실패할 때 |

숏 신호는 현물 계좌에서는 (1) 보유 물량 매도, (2) 인버스 ETF 매수로 집행한다.
개인 대주거래 연동은 2단계 과제.

**주의**: 딥 리서치 결론대로 이 규칙들의 승률 통계는 학술적으로 보장되지 않는다.
반드시 모의투자·백테스트로 본인 데이터를 쌓은 뒤 실전 전환할 것.

## 설정

```bash
cd trading
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 앱키/시크릿 입력
```

- 앱키 발급: https://openapi.kiwoom.com (키움 REST API → API키 발급)
- `config.yaml` 에서 감시 종목·규칙 파라미터·리스크 한도를 조정한다.

## 실행

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8600
```

대시보드: `http://127.0.0.1:8600/` (비밀번호는 `.env` 의 `DASH_PASSWORD`)

## TR ID 검증 (중요)

키움 REST API 의 TR ID(`ka10080` 분봉차트, `kt10000/kt10001` 매수/매도 등)와
요청 필드는 `app/kiwoom/client.py` 상단 상수에 모아뒀다.
공식 문서(https://openapi.kiwoom.com 로그인 후 API 가이드)와 대조해
**모의투자에서 실제 호출로 확인한 뒤** 사용할 것. 문서 포털은 로그인이 필요해
이 저장소의 값은 공개 자료 기준의 초안이다.

## 배포 (hosub 서버)

```bash
sudo cp deploy/trading.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now trading
```

Caddy 뒤에 붙이려면 `deploy/caddy-snippet.txt` 참고.
hosub-mcp 의 `deploy_service` 레지스트리에 등록하면 Claude 대화로 배포할 수 있다.

## 세금·수수료 가정 (backtest)

- 위탁 수수료: 0.015% (양방향, 증권사 이벤트 요율 기준 — `config.yaml` 에서 조정)
- 증권거래세: 매도 시 0.15% (2025년 코스피·코스닥 기준 — 변경 시 config 수정)
- 슬리피지: 기본 5bp/체결
