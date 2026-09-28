# hosub MCP 개선 요구사항 — 트레이딩 운영 측

- 작성: 2026-09-28 · 트레이딩 프로젝트(`/opt/hosub-trading`, trading.service :8600) 운영 세션
- 수신: 대시보드·MCP 관리 프로젝트
- 대상 코드: `src/policy.py`, `src/tools/shell.py`, `src/jobs.py`, `src/tools/control.py`, `deploy/update.sh`, `config/registry.yaml`
- 요약: 요구 5건. **R1 읽기 전용 계층**과 **R2 자동 업데이트 재시작 범위**가 우선입니다. 나머지는 사고 재발 방지와 정리 항목입니다.

---

## 먼저 정정 — 지난 보고의 오해

지난 보고에서 "run_command 가 읽기 명령에도 승인을 요구하도록 정책이 바뀐 것 같다"고 했는데, **사실이 아닙니다.**
`TOOL_RISK["run_command"] = Risk.HIGH` 는 #243 이후 바뀐 적이 없습니다. 트레이딩 세션이 그동안 `confirm=true` 로 호출해 왔고, 이번에는 그것을 빠뜨렸을 뿐입니다. 정책 회귀가 아니므로 되돌릴 것은 없습니다.
다만 이 일로 드러난 구조 문제가 R1 입니다.

---

## R1. 읽기 전용 명령 계층 (우선순위: 높음)

### 현상
- 트레이딩 운영 점검은 대부분 **읽기**입니다: `systemctl is-active <unit>`, `journalctl -u <unit> -n N`, `ls`/`stat`, `date`, `df`, `sqlite3` SELECT 등.
- 이 명령들은 전용 도구가 없어 `run_command`(HIGH)로만 돌릴 수 있습니다. 그래서 `date` 한 줄과 `rm -rf` 가 **같은 승인 등급**입니다.
- 결과는 둘 중 하나입니다. 사용자가 매번 승인하느라 지치거나, 세션이 `confirm=true` 를 습관적으로 붙여 **승인 게이트가 형식이 됩니다.** 지금은 후자에 가깝고, 이쪽이 더 위험합니다.
- 예약된 무인 점검(2026-09-29 국면 판정, 10-26 flow 재판정, 10-27 presurge 재판정)은 사람이 없는 시간에 깨어나 서버 상태를 봐야 합니다.

### 요청
LOW 등급의 읽기 전용 도구를 추가해 주세요. 형태는 둘 중 편한 쪽이면 됩니다.

**안 A — 전용 조회 도구 몇 개**
| 도구 | 동작 | 비고 |
|---|---|---|
| `unit_status(unit)` | `systemctl is-active` + `show -p ActiveEnterTimestamp,ExecMainStatus,Result` | transient 유닛(`systemd-run --unit=…`)도 조회 가능해야 함 |
| `unit_logs(unit, lines, since)` | `journalctl -u` | `read_service_logs` 가 registry 등록 서비스만 받는다면, transient 유닛까지 받도록 확장하는 것으로 대체 가능 |
| `stat_path(path)` | 존재·크기·mtime | `list_directory` 로 일부 대체되지만, 단일 파일 mtime 확인이 잦음 |
| `sqlite_query(db, sql)` | `sqlite3 -readonly`, **SELECT/WITH 만** 허용, 행 수 상한 | 대상: `/data/trading/*.db` 등 허용 경로만 |

**안 B — `run_readonly(command)` 하나**
- 명령 첫 토큰 허용목록(예: `systemctl is-active|status|show`, `journalctl`, `ls`, `stat`, `cat|head|tail|wc` (허용 경로 한정), `date`, `df`, `du`, `free`, `uptime`, `sqlite3 -readonly`)으로 판정합니다.
- 파이프·리다이렉트·`;`·`&&`·`$(…)`·백틱은 **금지**합니다(우회 경로 차단). sudo 도 금지합니다.
- 허용목록에 없으면 "run_command(HIGH)로 다시 호출하라"는 응답을 돌려줍니다.

두 안 모두 감사 로그는 그대로 남기되 `risk="low"` 로 기록하면 됩니다.

### 수용 기준
- 위 읽기 명령을 `confirm` 없이 실행할 수 있다.
- 쓰기·삭제·서비스 조작·sudo 는 여전히 HIGH 로만 가능하다(우회 불가를 테스트로 확인).

---

## R2. 자동 업데이트가 MCP 를 너무 자주, 너무 넓게 재시작한다 (우선순위: 높음)

### 현상
- `hosub-mcp-update.timer` 가 5분마다 `update.sh` 를 실행합니다. main 에 새 커밋이 있으면 **변경 경로와 무관하게** `hosub-mcp` 와 `hosub-dash` 를 재시작합니다.
- 이 저장소는 `trading/`, `tnm/`, `docs/` 커밋이 대부분입니다. 트레이딩 측은 측정 결과 기록(`docs/trading/measurement.md`)만으로도 하루 여러 번 커밋합니다. **그 커밋들이 전부 MCP 재시작을 일으킵니다.**
- `src/jobs.py` 가 명시하듯 잡은 인메모리입니다. 재시작하면 ① 잡 상태가 사라지고 ② `run_command(background=true)` 로 띄운 **자식 프로세스도 함께 죽습니다.**

### 실제 사고
| 일시 | 무엇 | 결과 |
|---|---|---|
| 2026-09-27 11:22 KST | 스윕 전후 비교 스크립트(~55분)를 MCP background 잡으로 실행 중에 트레이딩 PR #296 이 머지됨 | 5분 안에 update.sh 가 hosub-mcp 를 재시작해 잡이 사망했고, `systemd-run` 독립 유닛으로 다시 돌렸다 |

트레이딩 측은 이미 "30분 넘는 작업은 systemd-run 독립 유닛으로" 라는 우회 규칙을 두고 있었고 이번에는 그 규칙을 어긴 실수였지만, **변경과 무관한 재시작**이 근본 원인입니다.

### 요청 (위에서부터 효과 큼)
1. **경로 필터**: `git diff --name-only LOCAL REMOTE` 를 보고 재시작 대상을 결정합니다.
   - `src/`, `requirements.txt`, `config/`, `deploy/` 변경 → hosub-mcp (+dash) 재시작
   - `static/`, `src/dashboard.py`·`src/asgi_dash.py` 변경 → dash 만 재시작
   - `trading/`, `tnm/`, `docs/`, `tests/`, `scripts/` 만 변경 → **pull 만 하고 재시작 안 함**
   - `llm-gateway/` 는 현행대로(드리프트 경고만)
2. **실행 중 잡이 있으면 재시작 연기**: MCP 가 running 잡 수를 파일(예: `/run/hosub-mcp/active-jobs`)이나 로컬 엔드포인트로 노출하고, `update.sh` 는 0이 아니면 다음 주기로 미룹니다. 무한 연기를 막으려면 최대 연기 시간(예: 2시간)을 둡니다.
3. **잡 소실을 알려주기**: 재시작 전에 잡 목록을 파일로 떨궈 두고, 재시작 후 `get_job_status` 가 그 id 에 `unknown_job` 대신 `lost_on_restart`(재시작 시각 포함)를 돌려주도록 합니다. 지금은 "모르는 잡"과 "죽은 잡"을 구분할 수 없습니다.

### 수용 기준
- `docs/` 만 바뀐 커밋이 main 에 들어가도 hosub-mcp 의 `ActiveEnterTimestamp` 가 바뀌지 않는다.
- background 잡 실행 중에 MCP 코드 커밋이 들어와도, 잡이 끝날 때까지(또는 최대 연기 시간까지) 재시작되지 않는다.

---

## R3. 장기 작업을 서비스와 분리해 실행하는 도구 (우선순위: 중간)

### 현상
- R2 가 해결돼도 MCP 코드 변경에 따른 재시작이나 서버 재부팅은 남습니다. 30분~수 시간짜리 측정(백테스트 스윕, 기여도 재산출)은 MCP 프로세스 수명과 분리돼야 합니다.
- 지금 우회 방법은 `run_command(use_sudo=true, confirm=true)` 로 `systemd-run --unit=… --collect --uid=hosub …` 를 직접 조립하는 것입니다. 인자가 길고 틀리기 쉽습니다(9/27 에도 PYTHONPATH 누락으로 한 번 실패).

### 요청
`run_unit(name, command, workdir, env, uid, timeout)` (HIGH) 도구를 제안합니다.
- 내부에서 `systemd-run --unit=mcp-<name> --collect --property=RuntimeMaxSec=<timeout> …` 로 실행합니다.
- 상태·로그 조회는 R1 의 `unit_status` / `unit_logs` (LOW) 로 합니다.
- 결과 파일 경로를 받으면 완료 시 크기·mtime 을 함께 돌려주면 더 좋습니다.

---

## R4. `deploy_service("trading")` 의 시간창 가드 (우선순위: 중간)

### 현상
- `deploy_service("trading")` 는 pull → pip → `systemctl restart trading.service` 를 무조건 실행합니다.
- trading.service 는 **평일 09:00~15:30 장중 매매**와 **17:30~ 야간 배치**(EOD 백테스트 리포트 포함)를 프로세스 안에서 돌립니다. 이 창에 재시작하면 배치가 끊깁니다.
- 실제로 EOD 백테스트 리포트가 배포 재시작에 끊긴 일이 **두 번**(rc=-15) 있었습니다. 지금은 트레이딩 세션이 배포 시각을 스스로 지키는 규칙만으로 막고 있습니다.

### 요청 (둘 중 하나)
- **안 A (간단)**: registry 의 deploy 블록에 `blackout` 창(요일·시각, Asia/Seoul)을 선언하게 하고, 창 안이면 `deploy_service` 가 거부합니다. 사용자가 명시적으로 원하면 `force=true` 로 통과시킵니다.
  ```yaml
  trading:
    deploy:
      blackout:
        - {days: mon-fri, from: "08:50", to: "15:40"}
        - {days: mon-fri, from: "17:25", to: "20:00"}
  ```
- **안 B (정확)**: 트레이딩 측이 `GET 127.0.0.1:8600/api/busy` (`{"busy": true, "reason": "eod_report"}`)를 제공하고, `deploy_service` 가 재시작 직전에 이를 조회해 busy 면 거부합니다. 트레이딩 측 구현은 저희가 맡을 수 있으니, 채택하시면 엔드포인트 계약만 알려 주세요.

---

## R5. 정리 항목 (우선순위: 낮음)

- `config/registry.yaml` 의 trading 설명이 `"반자동 트레이딩 대시보드"` 입니다. 트레이딩 측은 #296 에서 "반자동" 표현을 정정했습니다(실제 발주 방식은 `auto_approve` 설정에 따라 전체 자동 또는 승인형이고, 현재 값은 매매 데스크의 운용 모드 배지가 정본). 설명을 `"트레이딩 서비스 (키움 REST API, 127.0.0.1:8600)"` 정도로 바꿔 주세요.
- `run_command` 의 `approval_required` 응답에 "읽기 전용이면 `run_readonly`/`unit_status` 를 쓰라"는 안내를 한 줄 넣어 주면(R1 이후), 세션이 습관적으로 `confirm=true` 를 붙이는 일이 줄어듭니다.

---

## 우선순위·일정 제안

| 순서 | 항목 | 이유 |
|---|---|---|
| 1 | R2-1 경로 필터 | 수십 줄 셸 변경으로 무관한 재시작의 대부분이 사라진다 |
| 2 | R1 읽기 전용 계층 | 승인 게이트를 형식에서 실질로 되돌린다. 9/29부터 무인 점검이 이어진다 |
| 3 | R2-2·3 잡 보호·소실 표시 | |
| 4 | R4 시간창 가드 | 운영 규칙으로 막고 있지만 사람의 실수에 기대고 있다 |
| 5 | R3·R5 | |

## 트레이딩 측이 당분간 지키는 규칙 (요구 반영 전까지)
- 30분 넘는 서버 작업은 MCP background 잡이 아니라 `systemd-run` 독립 유닛으로 실행
- `deploy_service("trading")` 는 평일 장중·17:25 이후 배치 시간에는 하지 않음
- 읽기 명령도 `run_command` 를 쓸 때는 승인 대상임을 전제로, 무엇을 읽는지 명시
