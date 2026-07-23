# 개발 요청서 — 대시보드 Bootstrap 5 Admin 테마 리디자인

> 이 문서는 Claude Code(또는 개발자)에게 전달하는 작업 요청서다. 아래 요구사항에
> 맞춰 대시보드 프론트엔드를 Bootstrap 5 기반 admin 테마로 재구성한다.
> (이 브랜치에는 요청서만 있고 구현 코드는 없다. 구현은 이 문서에 따라 진행한다.)

## 1. 목적

현재 hosub MCP 서버의 모니터링 대시보드(`static/`)는 순수 CSS/JS 로 만들어진
단순 카드 레이아웃이다. 이를 **Bootstrap 5 admin 테마** 스타일(좌측 사이드바 +
상단 네비바 + 카드형 콘텐츠)로 리디자인해 완성도를 높인다.

## 2. 전제·제약 (반드시 준수)

1. **외부 CDN 의존 금지 — 자체 완결.** 대시보드는 Cloudflare Tunnel/LAN 으로 노출되며,
   CSP·오프라인 환경에서도 동작해야 한다. Bootstrap 은 로컬에 vendoring 한다(3절).
   CDN `<link>`/`<script>` 를 추가하지 말 것.
2. **조회 전용 유지.** 대시보드는 상태 조회만 한다. 제어(재시작·명령 실행) 버튼을
   추가하지 말 것. 제어는 Claude 대화(MCP 도구)로만 한다.
3. **인증 경계 유지.** 대시보드는 세션 로그인(대시보드 비밀번호)로 보호된다.
   `/api/*` 는 세션 필요, `/mcp` 는 별도 OAuth/Bearer. 이 경계를 바꾸지 말 것.
4. **패널(위젯) 확장 구조 유지.** 새 패널을 파일 하나 추가 + 등록 한 줄로 붙일 수
   있는 현재 구조(4절)를 유지·계승한다. "홈" placeholder 패널과 이스터에그 훅도 보존.
5. **라이트/다크 테마 대응.** Bootstrap 5.3 의 `data-bs-theme` 로 라이트/다크를
   지원하고, OS 설정(`prefers-color-scheme`)을 기본값으로 따른다. 토글 버튼 제공.
6. **테스트·백엔드 불변.** `src/dashboard.py` 의 라우트/`/api` 응답 형태는 그대로 둔다
   (아래 3절의 vendor 공개 서빙 예외만 허용). `pytest` 전체가 계속 통과해야 한다.

## 3. 구현자가 먼저 준비할 것

### 3.1 Bootstrap vendoring (CDN 금지 → 로컬 배치)

`static/vendor/` 아래에 아래 파일을 넣는다:

- `static/vendor/bootstrap/bootstrap.min.css` (5.3.x)
- `static/vendor/bootstrap/bootstrap.bundle.min.js` (5.3.x, Popper 포함)
- `static/vendor/bootstrap-icons/bootstrap-icons.min.css` (1.11.x)
- `static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2`

로컬 환경에서 CDN 이 되면 그대로 내려받아 넣으면 된다. CDN 이 막힌 환경이라면
**npm 레지스트리 tarball** 로 받는다(예):

```bash
cd <repo>
mkdir -p static/vendor/bootstrap static/vendor/bootstrap-icons/fonts
tmp=$(mktemp -d)
curl -sSL -o "$tmp/bs.tgz" https://registry.npmjs.org/bootstrap/-/bootstrap-5.3.3.tgz
curl -sSL -o "$tmp/bi.tgz" https://registry.npmjs.org/bootstrap-icons/-/bootstrap-icons-1.11.3.tgz
tar xzf "$tmp/bs.tgz" -C "$tmp" package/dist/css/bootstrap.min.css package/dist/js/bootstrap.bundle.min.js
tar xzf "$tmp/bi.tgz" -C "$tmp" package/font/bootstrap-icons.min.css package/font/fonts/bootstrap-icons.woff2
cp "$tmp/package/dist/css/bootstrap.min.css"        static/vendor/bootstrap/bootstrap.min.css
cp "$tmp/package/dist/js/bootstrap.bundle.min.js"   static/vendor/bootstrap/bootstrap.bundle.min.js
cp "$tmp/package/font/bootstrap-icons.min.css"      static/vendor/bootstrap-icons/bootstrap-icons.min.css
cp "$tmp/package/font/fonts/bootstrap-icons.woff2"  static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2
# 아이콘 CSS 의 .woff(비-woff2) fallback 은 vendoring 안 하므로 404 방지 위해 제거(선택):
sed -i 's#,url("fonts/bootstrap-icons.woff?[a-f0-9]*") format("woff")##' static/vendor/bootstrap-icons/bootstrap-icons.min.css
```

아이콘 사용 예: `<i class="bi bi-cpu"></i>`.

### 3.2 정적 자산 공개 서빙 (로그인 전 vendor 접근)

로그인 페이지가 Bootstrap CSS 를 로드하려면 `/static/vendor/**` 가 로그인 전에도
서빙되어야 한다. `src/dashboard.py` 의 `static_file` 핸들러에서 공개 허용 조건에
`vendor/` 프리픽스를 추가한다:

```python
# 기존: name in _PUBLIC_ASSETS
public = name in _PUBLIC_ASSETS or name.startswith("vendor/")
if not _is_authed(request) and not public:
    return JSONResponse({"error": "unauthorized"}, status_code=401)
```

이 외의 백엔드 변경은 하지 않는다.

## 4. 현재 프론트엔드 구조 (계승 대상)

```
static/
├── index.html          # 대시보드 셸 (로그인 후)
├── login.html          # 로그인 페이지
├── login.js            # fetch 기반 로그인 처리 (302/401 분기)
├── style.css           # 커스텀 스타일
├── app.js              # 패널 오케스트레이터 + 공용 유틸(fetchJSON, el, bar) + 시계 + 이스터에그
└── panels/
    ├── index.js        # PANELS 배열 (등록 지점)
    ├── system.js       # 시스템 리소스 (CPU/메모리/디스크/프로세스)
    ├── services.js     # 서비스 상태
    ├── jobs.js         # 백그라운드 잡
    ├── audit.js        # 감사 로그
    └── home.js         # 홈 placeholder
```

**패널 계약 (유지):** 각 패널은 `export default { id, title, wide?, refreshMs, render(bodyEl) }`.
`app.js` 가 등록된 패널을 순회하며 카드로 렌더하고 `refreshMs` 주기로 `render` 호출.
새 패널 = `static/panels/<name>.js` 추가 + `panels/index.js` 의 `PANELS` 에 import 한 줄.

**API (변경 금지, 그대로 사용):**
- `GET /api/status` → `{cpu, memory, swap, disks[], uptime_seconds, top_processes[]}`
- `GET /api/services` → `{services:[{name, unit, active_state, sub_state, main_pid, query_ok, ...}]}`
- `GET /api/jobs?limit=` → `{jobs:[{id, label, state, exit_code, ...}]}`
- `GET /api/audit?limit=` → `{audit:[{ts, tool, outcome, confirm, result_summary, job_id}]}`

## 5. 요구 디자인 (Bootstrap 5 Admin 테마)

### 5.1 레이아웃

- **좌측 사이드바** (고정, 반응형 접힘): 브랜드(🖥️ hosub) + 네비 링크
  (대시보드/서비스/잡/감사/홈). 링크 클릭 시 해당 카드로 스크롤 또는 필터.
  모바일에서는 오프캔버스(`offcanvas`) 또는 토글로 접힘.
- **상단 네비바**: 좌측에 사이드바 토글(햄버거), 우측에 실시간 시계 + 테마 토글
  버튼 + 로그아웃 버튼.
- **메인 콘텐츠**: `container-fluid` + `row`/`col` 그리드에 Bootstrap **카드**(`card`)로
  각 패널 배치. 넓은 패널(`wide`)은 `col-12`, 일반 패널은 `col-12 col-lg-6` 등.

### 5.2 컴포넌트 매핑

| 현재 | Bootstrap 5 로 |
|---|---|
| 카드 `.card` | `.card` + `.card-header`(제목) + `.card-body` |
| 지표 값 | `.card` 내 큰 숫자 + 라벨, 또는 stat 타일 (`display-6` 등) |
| 진행 막대 `.bar` | `.progress` + `.progress-bar` (색은 `bg-success/warning/danger`) |
| 배지 `.badge` | `.badge` (`text-bg-success/danger/warning/secondary`) |
| 테이블 | `.table .table-sm .table-hover`, 스크롤은 `.table-responsive` |
| 상단바 | `.navbar` |
| 사이드바 | 커스텀 + `.nav .flex-column`, 아이콘은 `bi` |

- 서비스 상태 배지: active→`text-bg-success`, failed→`text-bg-danger`,
  activating/deactivating→`text-bg-warning`, 기타→`text-bg-secondary`.
- 잡 상태 배지: succeeded→success, running→warning, failed/timeout→danger,
  pending→secondary.
- 감사 outcome 배지: ok/succeeded→success, approval_required/job_started→warning,
  error/failed/timeout→danger, rejected→secondary.
- 시스템 리소스: CPU/메모리/스왑을 stat 카드 + `.progress` 로, 디스크는 마운트별
  테이블 + progress, 상위 프로세스는 테이블.
- 아이콘 예: CPU `bi-cpu`, 메모리 `bi-memory`, 디스크 `bi-hdd`, 서비스 `bi-hdd-stack`,
  잡 `bi-list-task`, 감사 `bi-shield-check`, 홈 `bi-house`.

### 5.3 다크 모드

- `<html data-bs-theme="...">` 로 제어. 초기값은 `prefers-color-scheme`.
- 네비바의 테마 토글로 light/dark 전환, 선택은 `localStorage` 에 저장.

### 5.4 로그인 페이지

- `login.html` 을 Bootstrap 카드(`.card`) 중앙 정렬 폼으로 재구성. `login.js` 의
  fetch 로직(302→`/`, 401→에러 표시)은 유지하되, 에러 표시를 Bootstrap
  `.alert.alert-danger` 로. Bootstrap CSS(vendor)만 로드하면 됨.

## 6. 작업 파일

- 수정: `static/index.html`, `static/login.html`, `static/login.js`,
  `static/style.css`(Bootstrap 위 최소 커스텀만), `static/app.js`,
  `static/panels/*.js` (Bootstrap 마크업으로 렌더)
- 추가: `static/vendor/**` (3.1), `src/dashboard.py` 의 vendor 공개 서빙(3.2)
- 건드리지 말 것: 그 외 `src/*` 백엔드, `/api` 응답 형태
- 유지: 패널 등록 구조, 홈 placeholder, 이스터에그 훅

## 7. 완료 기준 (검증)

1. `.venv/bin/pytest` 전체 통과(현재 56종) — 백엔드 불변 확인.
2. 서버 기동 후 브라우저 검증:
   - `HOSUB_MCP_TOKEN`/`HOSUB_DASH_PASSWORD`/`HOSUB_SESSION_SECRET` 설정 후
     `uvicorn src.asgi:app --port 8700`.
   - `/login` → Bootstrap 폼 렌더, 비밀번호 로그인 → `/` 이동.
   - 대시보드: 사이드바+네비바+카드 레이아웃, 시스템/서비스/잡/감사/홈 패널이
     Bootstrap 컴포넌트로 표시, 5초 폴링 갱신.
   - **네트워크 탭에 외부(호스트 밖) 요청 0건** — 모든 자산이 `/static/**` 에서 로드.
   - 콘솔 에러 0.
   - 다크/라이트 토글 동작, 새로고침 후 선택 유지.
   - 모바일 폭에서 사이드바 접힘/오프캔버스 동작.
3. 새 패널 추가 절차(파일 1개 + 등록 1줄)가 여전히 유효함을 README/주석으로 확인.

## 8. 참고

- 현재 대시보드 동작·API 는 `src/dashboard.py` 와 기존 `static/panels/*.js` 참고.
- 색/배지/진행바 매핑은 기존 패널 로직을 그대로 Bootstrap 클래스로 옮기면 된다.
- 작업은 이 브랜치에서 진행하거나, 여기서 새 브랜치를 따 작업 후 PR → main.
