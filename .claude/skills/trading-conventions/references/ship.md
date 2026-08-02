# 레시피 — 커밋·PR·배포·검증

## 커밋 전

1. 전체 스위트: `cd trading && python -m pytest tests -q` → 루트
   `python -m pytest tests -q` → (tnm 변경 시) `pytest tnm/tests -q` →
   (JS 변경 시) `node --check static/pages/<파일>.js`
2. config ↔ 코드 DEFAULTS 동기 확인, 프록시 화이트리스트 3곳 확인(wiring.md)
3. 커밋 메시지: 한국어, **무엇을 왜** — 실측 근거·사고 번호·측정 날짜 포함.
   새 API 콜이 늘면 분당 콜 수 명시

## PR·머지

- 브랜치에서 PR → squash 머지. 머지 후 다음 작업 전에
  `git fetch origin main && git merge origin/main` (스쿼시 겹침 충돌은 브랜치
  쪽(ours) 유지가 대부분 정답 — 브랜치가 상위집합이므로)
- PR 본문: 근거(실측·측정) → 변경 → 되돌리기 방법 → 테스트

## 배포

```
deploy_service("trading")        # 백엔드 (git pull + 재시작)
restart_service("dash")          # static/·src/dashboard.py 변경 시
```

**금지 시간**: 평일 장중(09:00~15:30), 야간 배치 중(17:30~, 진행 상태 API 확인).
docs/ 만 바뀐 PR 은 배포 불필요.

배포 후 스모크(읽기 전용): 기동 로그(감시목록 로드·엔진 루프·WS 구독),
핵심 API 200, traceback 없음.

## 실거래에 닿는 신기능

`enabled: false` 로 배포 → 다음 거래일 장중 활성화 → 30분 관찰 —
배포와 활성화를 분리한다(매매 데스크 선례).

## 개장 검증 예약

장중에만 동작하는 변경(게이트·데스크·WS·소스)은 다음 거래일 09:20 개장 검증
트리거에 확인 항목을 추가한다: 무엇이 정상 동작인지 / 어떤 로그가 이상 신호인지 /
이상 시 되돌릴 값. **오탐이 실거래를 건드리는 변경(void·청산·차단)은 "오탐
1건이라도 즉시 보고" 조항을 넣는다.**

## 되돌리기 원칙

- 런타임 오버라이드로 끌 수 있는 것 먼저 (배포 불필요)
- 코드 원복이 필요하면 revert PR — 직접 push 금지, 같은 절차로
