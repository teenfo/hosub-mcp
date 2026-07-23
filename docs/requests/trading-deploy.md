# 로컬 실행 요청서 — trading 서비스 최초 배포 + 대시보드 연동

> 이 문서는 LAN 안 로컬 세션(또는 서버 앞에 앉은 사람)이 hosub 서버에서 수행할
> 런북이다. 코드는 `claude/confirmation-needed-2z6swl` 브랜치의 `trading/` 에 있다
> (main 머지 전이므로 해당 브랜치를 직접 체크아웃한다. 머지 후에는 main 으로 전환).

## 1. 목적

1. 키움 REST API 기반 반자동 트레이딩 서비스(`trading/`)를 `/opt/hosub-trading` 에 배포
2. **실계좌 앱키 검증 — 반드시 읽기 전용**(토큰 발급 + 분봉 조회)까지만. 주문 API 호출 금지
3. hosub-mcp 대시보드 "트레이딩" 메뉴 연동(공유 시크릿 설정)

## 2. 전제·주의 (반드시 준수)

- 사용자의 앱키는 **실계좌 키**다. 이 문서에는 키를 적지 않는다 — 사용자에게 직접
  전달받아 `.env` 에만 입력한다. `.env` 는 gitignore 되어 있고, 권한 600 으로 둔다.
- 검증 단계에서 주문 엔드포인트(`/api/dostk/ordr`)를 호출하지 않는다. 주문은 오직
  대시보드 승인 흐름으로만 나가는 것이 이 시스템의 설계 원칙이다.
- 서비스는 `127.0.0.1:8600` 에만 바인딩한다(외부 직접 노출 금지). 외부 접근은
  hosub-mcp 대시보드 프록시(`/api/trading/*`) 경유가 유일한 경로다.

## 3. 배포 절차

```bash
# 1) 별도 클론 (main 자동 배포 클론 /opt/hosub-mcp 와 분리)
sudo git clone -b claude/confirmation-needed-2z6swl \
    https://github.com/teenfo/hosub-mcp.git /opt/hosub-trading
sudo chown -R hosub:hosub /opt/hosub-trading

# 2) venv + 의존성
sudo -u hosub bash -c '
  cd /opt/hosub-trading/trading
  python3 -m venv .venv && . .venv/bin/activate
  pip install -r requirements.txt
'

# 3) .env 작성 (키는 사용자에게 전달받아 입력)
sudo -u hosub tee /opt/hosub-trading/trading/.env >/dev/null <<'EOF'
KIWOOM_ENV=real
KIWOOM_APP_KEY=<사용자에게 받은 앱키>
KIWOOM_SECRET_KEY=<사용자에게 받은 시크릿>
KIWOOM_ACCOUNT=<계좌번호>
DASH_PASSWORD=<대시보드용 새 비밀번호>
SESSION_SECRET=$(openssl rand -hex 32 로 생성한 값)
INTERNAL_TOKEN=$(openssl rand -hex 32 로 생성한 값)
DATA_DIR=/data/trading
EOF
sudo chmod 600 /opt/hosub-trading/trading/.env
sudo -u hosub mkdir -p /data/trading

# 4) systemd 등록
sudo cp /opt/hosub-trading/trading/deploy/trading.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now trading
systemctl status trading --no-pager
```

## 4. 실계좌 키 검증 (읽기 전용)

```bash
sudo -u hosub bash -c '
  cd /opt/hosub-trading/trading && . .venv/bin/activate
  python - <<PY
import asyncio
async def main():
    from app.kiwoom.auth import token_manager
    from app.kiwoom.client import client
    from app.data.collector import parse_chart_response
    token = await token_manager.get()
    print("토큰 발급 OK, 길이:", len(token))
    data = await client.minute_chart("005930", interval=1)
    print("return_code:", data.get("return_code"), data.get("return_msg"))
    df = parse_chart_response(data)
    print("분봉 파싱:", len(df), "건 (마지막 3건 아래)")
    print(df.tail(3) if not df.empty else "빈 응답 - 장 시간/TR 확인")
    await client.aclose()
asyncio.run(main())
PY
'
```

- `return_code: 0` + 분봉 수십 건 파싱 → 검증 성공
- 실패 시 확인: 키움 openapi 포털에서 REST API 서비스 신청 상태, IP 제한 설정,
  `journalctl -u trading` 로그

## 5. hosub-mcp 대시보드 연동

```bash
# hosub-mcp 쪽 .env 에 두 줄 추가 (INTERNAL_TOKEN 과 같은 값)
echo 'HOSUB_TRADING_URL=http://127.0.0.1:8600' | sudo tee -a /opt/hosub-mcp/.env
echo 'HOSUB_TRADING_TOKEN=<trading .env 의 INTERNAL_TOKEN 값>' | sudo tee -a /opt/hosub-mcp/.env
sudo systemctl restart hosub-mcp
```

> 참고: 대시보드 "트레이딩" 메뉴(코드)는 trading 과 같은 브랜치에 있다. main 머지
> 전이라면 hosub-mcp 서비스에는 아직 메뉴가 없다 — 머지 후 자동 배포(5분 폴링)로
> 반영된다. 연동 env 는 미리 넣어 두어도 무해하다.

## 6. 검증 체크리스트

- [ ] `systemctl status trading` → active (running)
- [ ] 4절 스크립트 → 토큰 발급 + 분봉 파싱 성공 (주문 호출 없음)
- [ ] `curl -s -H "X-Internal-Token: <토큰>" http://127.0.0.1:8600/api/status` → JSON 응답
- [ ] (main 머지 후) 대시보드 → 트레이딩 메뉴 → 상태 카드 "실전" 배지 표시
- [ ] hosub-mcp 서비스 레지스트리에 trading 등록 (Claude 대화로 재시작/로그 조회 가능하게)

## 7. 완료 후

- `docs/work-log.md` 에 수행 내역 기록
- 사용자에게 안내: 채팅에 노출됐던 앱키는 **키움 포털에서 재발급(로테이션)** 권장.
  재발급 시 `.env` 만 갱신하고 `sudo systemctl restart trading`
- 이 요청서 브랜치는 핸드오프가 끝나면 정리(`git push origin --delete local-request/trading-deploy`)
