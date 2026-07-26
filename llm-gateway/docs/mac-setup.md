# 맥 스튜디오 준비 (LLM 백엔드)

게이트웨이가 추론을 맡길 실제 백엔드. **M4 Max / 48GB**.

| 항목 | 값 |
|---|---|
| Tailscale 이름 | `macstudio` |
| Tailscale IP | **100.69.201.28** ← 게이트웨이가 쓰는 주소 |
| LAN IP | 192.168.0.31 (DHCP — 쓰지 않는다) |
| Ollama 포트 | 11434 |

Tailscale IP 를 쓰는 이유: LAN IP 는 공유기가 재부팅되면 바뀔 수 있고, tailnet 주소는
고정이다. hosub → 맥은 같은 LAN 이라 tailnet 을 거쳐도 직접 연결(9ms)이라 손해가 없다.

---

## 현재 상태 (hosub 에서 확인한 것)

```
✅ Tailscale 연결됨      tailscale ping → pong via 192.168.0.31, 9ms
✅ 도달 가능             5000·7000·8080 포트 열려 있음(방화벽 차단 아님)
❌ 11434                 Connection refused — 아무것도 듣고 있지 않다
✅ 컨테이너 → tailnet    도커 컨테이너에서 맥 tailnet IP 로 나갈 수 있음
```

`refused` 는 타임아웃과 다르다. 패킷은 맥까지 도달하는데 맥이 거절하는 것이므로,
**방화벽 문제가 아니라 Ollama 가 없거나 `127.0.0.1` 에만 묶여 있는 것**이다.

---

## 할 일

### 1. Ollama 설치 확인

```bash
ollama --version
```
없으면 https://ollama.com/download 또는 `brew install --cask ollama`.

### 2. 외부 접속 허용 (핵심)

Ollama 는 기본으로 `127.0.0.1:11434` 에만 묶인다. hosub 에서 못 부르는 이유다.

```bash
launchctl setenv OLLAMA_HOST 0.0.0.0
# 메뉴바 Ollama 아이콘 → Quit 후 다시 실행 (반드시!)
```

> ⚠️ **`launchctl setenv` 는 재부팅하면 사라진다.** 정전 한 번이면 조용히 죽는다.
> 아래 LaunchAgent 로 고정한다.

```bash
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/com.hosub.ollama-host.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.hosub.ollama-host</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/launchctl</string>
    <string>setenv</string>
    <string>OLLAMA_HOST</string>
    <string>0.0.0.0</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
EOF
launchctl load ~/Library/LaunchAgents/com.hosub.ollama-host.plist
```

`0.0.0.0` 은 LAN 에도 11434 를 연다. 집 NAT 안이라 인터넷에는 노출되지 않지만,
tailnet 으로만 제한하고 싶으면 `0.0.0.0` 대신 `100.69.201.28:11434` 로 둔다.
다만 Ollama 가 Tailscale 보다 먼저 뜨면 바인딩에 실패하므로 `0.0.0.0` 이 더 안전하다.

### 3. 슬립 방지 (중요)

맥이 자면 백엔드가 사라진다. 야간 배치 분석이 조용히 실패하는 가장 흔한 원인이다.

```bash
sudo pmset -a sleep 0 disksleep 0
pmset -g | grep -E "^ *(sleep|disksleep)"    # 0 인지 확인
```

시스템 설정 → 에너지에서 "디스플레이가 꺼져 있을 때 자동으로 잠자기 방지"도 켠다.

### 4. 재부팅 후 자동 복구

정전이 나면 사람이 없어도 돌아와야 한다.

- 시스템 설정 → 사용자 및 그룹 → **자동 로그인 켜기**
- 시스템 설정 → 일반 → 로그인 항목에 **Ollama** 추가
- 시스템 설정 → 에너지 → **"정전 후 자동으로 다시 시작"** 켜기

### 5. Tailscale 키 만료 해제

Tailscale 노드는 기본 180일 후 키가 만료되어 **조용히 연결이 끊긴다**.
서버 성격의 노드에는 반드시 꺼야 한다.

Tailscale 관리 콘솔 → Machines → `macstudio` → ⋯ → **Disable key expiry**.
(같은 이유로 `hosub` 노드도 함께 확인)

### 6. 모델

**미리 받을 필요 없다.** 게이트웨이가 미설치 모델을 만나면 설치 요청을 만들고,
대시보드에서 승인하면 자동으로 내려받는다.

다만 연결 확인용으로 작은 것 하나는 있으면 편하다:

```bash
ollama pull qwen2.5:7b
```

---

## 확인

맥에서:
```bash
curl -s http://localhost:11434/api/tags | head -c 200      # Ollama 살아 있나
lsof -iTCP:11434 -sTCP:LISTEN                              # 0.0.0.0 에 묶였나
```
`127.0.0.1:11434` 로 나오면 2번이 적용되지 않은 것이다(앱 재시작을 빠뜨렸을 가능성).

hosub 에서:
```bash
curl -s http://100.69.201.28:11434/api/tags
```
여기서 모델 목록이 나오면 끝. `.env` 에 다음을 넣고 게이트웨이를 올린다.

```bash
OLLAMA_URL=http://100.69.201.28:11434
MEM_BUDGET_GB=40          # 48GB 중 40GB 를 두 레인 합계 상한으로
```

---

## 운영 메모

- **메모리 예산 40GB** — 48GB 중 macOS 와 다른 앱 몫을 남긴 값이다. 두 레인에서
  동시에 도는 모델 크기 합이 이 값을 넘으면 게이트웨이가 시작을 미룬다.
- **`keep_alive` 는 게이트웨이가 요청마다 보낸다**(기본 10분). 맥에 별도 환경변수를
  설정할 필요 없다.
- **맥이 꺼져 있어도 잡은 안 사라진다.** 게이트웨이가 재시도하고, 잡은 SQLite 에
  남아 있으므로 맥이 돌아오면 이어서 처리된다.
- 맥이 꺼진 동안 게이트웨이는 보유 모델 목록을 모른다. 이때는 "모델이 없다"고
  단정하지 않고 예전처럼 실행을 시도한다(설치 요청을 잘못 만들지 않기 위해).
