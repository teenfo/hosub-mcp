import { fetchJSON, el, card, badge } from "../app.js";
import { makeLayoutEditable } from "../layout.js";
import { createProChart, MA_DEFS } from "../chart.js";
import { postJSON, fmt, won, pct, priceCellHTML, agoStr, leftStr, sideBadge } from "./tradelib.js";

// 매매 데스크 (트레이딩 그룹): 장중 실행에 필요한 것만 — 상태·가드·승인대기·신호·1분봉.
// 종목 소싱은 '발굴·감시', 리뷰는 '성과·백테스트' 페이지가 담당한다.

export default {
  id: "trading",
  title: "매매 데스크",
  icon: "bi-graph-down-arrow",
  group: "트레이딩",
  async render(container, ctx) {
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    // 변경 감지: 폴링 데이터가 실제로 바뀔 때만 DOM 을 다시 그린다.
    // (차트는 자체 캔버스라 무관 — 이걸로 나머지 카드의 주기적 깜빡임을 없앤다)
    const _memo = {};
    const changed = (key, data) => {
      const s = JSON.stringify(data);
      if (_memo[key] === s) return false;
      _memo[key] = s;
      return true;
    };

    const status = card("트레이딩 상태", null, { icon: "bi-activity" });
    const pending = card("승인 대기 주문", null, { wide: true, icon: "bi-hourglass-split" });
    const chart = card("1분봉 차트", null, { wide: true, icon: "bi-candlestick" });
    const signals = card("최근 신호", null, { wide: true, icon: "bi-lightning" });
    const guardC = card("일일 목표·가드", null, { icon: "bi-shield-check" });
    const posC = card("보유 포지션 · 청산 관리", null, { wide: true, icon: "bi-briefcase" });
    // 각 카드를 독립 그리드 아이템으로 등록(id·기본 폭). 편집 모드에서 자유 배치·크기조절.
    const CARDS = [
      ["status", status, 6], ["guard", guardC, 6],
      ["positions", posC, 12],
      ["pending", pending, 12], ["signals", signals, 12],
      ["chart", chart, 12],
    ];
    CARDS.forEach(([id, c, w], i) => {
      c.col.dataset.cardId = id;
      c.col.dataset.cardIndex = i;      // 초기화 시 원래 순서 복원용
      c.col.className = "col-12 col-xl-" + w;
      c.col.querySelector(".card").classList.add("h-100");
      row.appendChild(c.col);
    });
    // 저장된 배치·크기 복원 + '레이아웃 편집' 툴바 (브라우저별 localStorage)
    makeLayoutEditable(row, { key: "trading" });

    // --- 매매 설정 입력 ---
    // 값 편집은 전부 기어 모달(섹션별 접이식)로 모으고, 카드에는 상태만 남긴다.
    const numIn = (attrs) => el("input", Object.assign(
      { class: "form-control form-control-sm", type: "number", style: "max-width:96px" },
      attrs));
    const chk = (id) => el("input", { class: "form-check-input", type: "checkbox", id });
    const gTarget = numIn({ step: "0.1", min: "0", max: "50" });
    const gLoss = numIn({ step: "0.1", min: "0", max: "50" });
    const gRisk = numIn({ step: "0.1", min: "0", max: "50" });
    const gWeight = numIn({ step: "1", min: "0", max: "100" });
    const gMaxPos = numIn({ step: "1", min: "1", max: "20" });
    const gTtl = numIn({ step: "1", min: "1", max: "240" });
    const gScan = numIn({ step: "5", min: "10", max: "300" });
    const gAuto = chk("gAutoChk");
    const gLongOnly = chk("gLongOnlyChk");
    const gConfirm = chk("gConfirmChk");
    const gStatus = el("div", { class: "mt-2 small" });
    const fld = (lbl, input, hint) => el("div", {}, [
      el("label", { class: "form-label small text-secondary mb-0" }, lbl),
      input,
      hint ? el("div", { class: "form-text small lh-sm", style: "max-width:120px" }, hint) : null,
    ]);
    const sw = (input, label, desc) => el("div", { class: "mb-2" }, [
      el("div", { class: "form-check form-switch" }, [
        input,
        el("label", { class: "form-check-label small fw-semibold", for: input.id }, label),
      ]),
      el("div", { class: "small text-secondary lh-sm" }, desc),
    ]);

    // 저장 — 섹션별로 나눠 실수로 다른 값까지 덮어쓰지 않게 한다.
    const saveMsg = (box, ok, text) => {
      box.className = "small mt-2 " + (ok ? "text-success" : "text-danger");
      box.textContent = text;
    };
    const fundMsg = el("div", { class: "small mt-2" });
    const gSave = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "자금·리스크 저장");
    gSave.onclick = async () => {
      gSave.disabled = true;
      try {
        await postJSON("/api/trading/risk", {
          risk_per_trade_pct: parseFloat(gRisk.value),
          max_position_weight_pct: parseFloat(gWeight.value),
          max_positions: parseInt(gMaxPos.value, 10),
          daily_target_pct: parseFloat(gTarget.value),
          daily_loss_limit_pct: parseFloat(gLoss.value),
          signal_ttl_min: parseInt(gTtl.value, 10),
          auto_approve: gAuto.checked,
          long_only: gLongOnly.checked,
        });
        _memo["risk"] = undefined;
        await loadRisk();
        saveMsg(fundMsg, true, "저장됨 — 다음 신호부터 적용");
      } catch (e) { saveMsg(fundMsg, false, "저장 실패: " + e.message); }
      finally { gSave.disabled = false; }
    };

    const scanMsg = el("div", { class: "small mt-2" });
    const scanSave = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "감시·신호 저장");
    scanSave.onclick = async () => {
      scanSave.disabled = true;
      try {
        await postJSON("/api/trading/risk", {
          scan_interval_sec: parseInt(gScan.value, 10),
          confirm_on_close: gConfirm.checked,
        });
        _memo["risk"] = undefined;
        await loadRisk();
        saveMsg(scanMsg, true,
          `저장됨 — ${gScan.value}초 주기 · 돌파 확인 ${gConfirm.checked ? "켜짐" : "꺼짐"} (다음 스캔부터)`);
      } catch (e) { saveMsg(scanMsg, false, "저장 실패: " + e.message); }
      finally { scanSave.disabled = false; }
    };

    // --- 일일 목표·가드 카드: 상태 표시 전용 ---
    const openCfgBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" },
      [el("i", { class: "bi bi-gear" }), " 매매 설정"]);
    guardC.body.append(
      el("div", { class: "d-flex justify-content-between align-items-start gap-2 mb-2" }, [
        el("div", { class: "small text-secondary" },
          el("span", { html: '<i class="bi bi-shield-check"></i> <b>일일 목표/손실한도</b>는 <b>완전 자동 발주를 위한 안전장치</b>입니다. 당일 실현손익이 도달하면 자동 발주 모드에서만 신규 진입을 멈추고, 자동 발주를 끄면 신호는 계속 나와 <b>사용자 책임 하에 직접 승인</b>할 수 있습니다. 어느 쪽이든 보유 포지션의 손절·목표 감시는 계속됩니다. 값 변경은 <b>매매 설정</b>에서. (실거래 성과 로그 기준)' })),
        openCfgBtn,
      ]),
      gStatus,
    );

    const loadRisk = async () => {
      let r;
      try { r = await fetchJSON("/api/trading/risk"); } catch (e) { return; }
      if (!changed("risk", r)) return;
      // 입력 중인 칸은 건드리지 않는다(폴링이 타이핑을 덮어쓰지 않게).
      const set = (input, v) => { if (document.activeElement !== input) input.value = v; };
      set(gTarget, r.daily_target_pct);
      set(gLoss, r.daily_loss_limit_pct);
      set(gRisk, r.risk_per_trade_pct);
      set(gWeight, r.max_position_weight_pct ?? 0);
      set(gMaxPos, r.max_positions ?? 3);
      set(gTtl, r.signal_ttl_min ?? 10);
      set(gScan, r.scan_interval_sec ?? 60);
      if (Array.isArray(r.scan_interval_range)) {
        gScan.min = r.scan_interval_range[0];
        gScan.max = r.scan_interval_range[1];
      }
      gAuto.checked = !!r.auto_approve;
      gLongOnly.checked = !!r.long_only;
      gConfirm.checked = !!r.confirm_on_close;
      gStatus.innerHTML = "";
      const cls = r.pct >= 0 ? "text-danger" : "text-primary";
      gStatus.append(el("div", {}, [
        el("span", { class: "text-secondary" }, "오늘 실현손익 "),
        el("span", { class: "fw-semibold " + cls }, `${won(r.krw)} (${pct(r.pct)})`),
        el("span", { class: "text-secondary" }, ` · ${r.trades}건`),
      ]));
      const bar = el("div", { class: "progress mt-2", style: "height:8px" });
      const hi = r.daily_target_pct || 0;
      const frac = hi > 0 ? Math.max(0, Math.min(100, r.pct / hi * 100)) : 0;
      bar.appendChild(el("div", { class: "progress-bar " + (r.pct >= 0 ? "bg-danger" : "bg-primary"), style: `width:${r.pct >= 0 ? frac : 0}%` }));
      gStatus.appendChild(bar);
      gStatus.appendChild(el("div", { class: "mt-2 d-flex gap-2 align-items-center flex-wrap" }, [
        r.halted
          ? el("span", {
              class: "badge text-bg-" + (r.auto_approve ? "warning" : "secondary"),
              title: r.auto_approve
                ? "완전 자동 발주 모드 — 가드가 신규 진입을 막습니다"
                : "수동 승인 모드 — 가드는 경고만 하고 진입을 막지 않습니다(사용자 책임)" },
              r.reason + (r.auto_approve ? "" : " · 수동 승인이라 진입 허용"))
          : el("span", { class: "badge text-bg-success" }, "정상 — 진입 허용"),
        r.auto_approve ? el("span", { class: "badge text-bg-danger" }, "⚡ 완전 자동 발주 중") : null,
        el("span", { class: "badge text-bg-light text-dark", title: "한 종목 최대 비중 · 최대 동시 포지션" },
          `비중 ${r.max_position_weight_pct ? r.max_position_weight_pct + "%" : "무제한"} · 최대 ${r.max_positions ?? 3}종목`),
        el("span", { class: "badge text-bg-light text-dark", title: "신호 스캔 간격 · 돌파 확인" },
          `${r.scan_interval_sec ?? 60}초${r.confirm_on_close ? " · 돌파확인" : ""}`),
        r.regime ? el("span", {
          class: "badge text-bg-" + (r.regime === "강세" ? "danger" : r.regime === "약세" ? "primary" : "secondary"),
          title: `전일 breadth ${r.base_regime || "-"} · 당일 시가갭 ${r.gap_bias || "-"} · 야간리포트 ${r.night_bias || "-"}` },
          `시장 ${r.regime}${r.regime === "강세" ? " · 인버스 매수 보류" : " · 인버스 매수 허용"}`) : null,
      ]));
      if (!r.halted && hi > 0 && r.pct < hi) {
        gStatus.appendChild(el("div", { class: "text-secondary mt-1" }, `목표까지 ${(hi - r.pct).toFixed(2)}% 남음`));
      }
    };

    // 상태 카드 헤더에 설정(기어) 버튼 추가 → 클릭 시 매매 설정 모달 표시
    const statusHeader = status.col.querySelector(".card-header");
    statusHeader.classList.add("d-flex", "justify-content-between", "align-items-center");
    const gearBtn = el("button", {
      class: "btn btn-sm btn-link p-0 text-secondary", title: "매매 설정",
    }, el("i", { class: "bi bi-gear-fill" }));
    statusHeader.appendChild(gearBtn);

    // --- 키움 API 자격 (시크릿은 서버가 원문을 돌려주지 않음 — 변경 시에만 입력) ---
    const envSel = el("select", { class: "form-select form-select-sm" }, [
      el("option", { value: "mock" }, "모의투자 (mockapi)"),
      el("option", { value: "real" }, "실전 (api.kiwoom.com)"),
    ]);
    const appKeyIn = el("input", { class: "form-control form-control-sm", autocomplete: "off" });
    const secretIn = el("input", { class: "form-control form-control-sm", type: "password", autocomplete: "new-password" });
    const accountIn = el("input", { class: "form-control form-control-sm", autocomplete: "off" });
    const saveBtn = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "자격 저장");
    const cfgMsg = el("div", { class: "small mt-2" });
    const field = (label, input) =>
      el("div", { class: "mb-2" }, [el("label", { class: "form-label small mb-1" }, label), input]);
    const usageBox = el("div", { class: "small" });

    // --- 설정 모달: 섹션별 접이식(아코디언) ---
    const ACC_ID = "tradeCfgAcc";
    const accItem = (id, icon, title, children, open) => el("div", { class: "accordion-item" }, [
      el("h2", { class: "accordion-header" },
        el("button", {
          class: "accordion-button py-2 px-3" + (open ? "" : " collapsed"),
          type: "button", "data-bs-toggle": "collapse", "data-bs-target": "#" + id,
        }, el("span", { class: "small fw-semibold", html: `<i class="bi bi-${icon}"></i> ${title}` }))),
      el("div", {
        id, class: "accordion-collapse collapse" + (open ? " show" : ""),
        "data-bs-parent": "#" + ACC_ID,
      }, el("div", { class: "accordion-body py-2 px-3" }, children)),
    ]);

    const modalBody = el("div", { class: "modal-body pt-2" },
      el("div", { class: "accordion", id: ACC_ID }, [
        accItem("cfgFund", "cash-coin", "자금 · 리스크", [
          el("div", { class: "d-flex gap-3 flex-wrap align-items-start" }, [
            fld("거래당 리스크 %", gRisk, "1회 손절 시 계좌 대비 최대 손실"),
            fld("종목당 비중 %", gWeight, "한 종목 최대 비중 (0=무제한)"),
            fld("최대 동시 포지션", gMaxPos, "동시에 열 수 있는 종목 수"),
            fld("일일 목표 %", gTarget, "도달 시 이익 확정·진입 중단"),
            fld("손실 한도 %", gLoss, "도달 시 그날 진입 중단"),
            fld("신호 만료(분)", gTtl, "승인 대기 신호 자동 만료"),
          ]),
          el("div", { class: "small text-secondary mt-2 lh-sm" },
            "거래당 리스크는 일일 손실한도보다 작게 두세요(예: 0.5% ↔ 1.5% = 하루 손절 3번 여유). 종목당 비중은 100 ÷ 동시 보유 목표수 정도가 기준입니다 — 5종목 목표면 20%. 비중 상한이 없으면 손절폭이 좁은 첫 신호가 예수금을 독식하고 이후 신호가 전부 밀립니다."),
          el("hr", { class: "my-2" }),
          sw(gAuto, "⚡ 완전 자동 발주",
            "신호를 승인 없이 즉시 발주하고 목표 도달 청산도 자동 실행합니다. 끄면 화면에서 승인해야 발주됩니다. 위 일일 목표·손실 한도는 이 모드를 위한 안전장치라, 끄면 한도에 도달해도 신호가 계속 나오고 진입 여부는 사용자 판단이 됩니다(신호에 '가드 도달 후' 표시가 붙습니다)."),
          sw(gLongOnly, "롱 전용",
            "현물 계좌는 개별주 공매도가 불가하므로 숏 신호를 발주하지 않고 기록만 합니다."),
          el("div", {}, gSave), fundMsg,
        ], true),
        accItem("cfgScan", "speedometer2", "감시 · 신호", [
          el("div", { class: "d-flex gap-3 flex-wrap align-items-start mb-2" },
            [fld("감시 주기(초)", gScan, "신호 스캔 간격 (10~300)")]),
          el("div", { class: "small text-secondary mb-2 lh-sm" },
            "짧을수록 진입이 빨라지지만 감시 종목수 × 호출이 늘어 API 한도에 가까워집니다. 저장 즉시 적용(재시작 불필요). 한도 초과(429)가 나면 서버가 호출 속도를 자동으로 절반까지 낮추고, 안정되면 단계적으로 회복합니다."),
          sw(gConfirm, "🔒 돌파 확인 — 봉이 마감된 뒤에만 발사",
            "키움 분봉은 '형성 중인 현재 봉'을 현재가로 함께 내려줍니다. 끄면 지금 이 순간 가격으로 판단해 더 빨리 진입하지만, 찍고 되밀린 가짜 돌파에도 반응합니다. 신호는 (종목·규칙)당 하루 한 번만 발사되므로 가짜 돌파 한 번이 그날의 기회를 소진합니다. 켜면 백테스트와 같은 조건이 됩니다."),
          el("div", {}, scanSave), scanMsg,
        ]),
        accItem("cfgUsage", "activity", "API 호출 부하", [usageBox]),
        accItem("cfgKeys", "key", "키움 API 자격", [
          field("환경", envSel),
          field("앱키 (App Key)", appKeyIn),
          field("시크릿 키 (Secret Key)", secretIn),
          field("계좌번호", accountIn),
          el("div", {}, saveBtn), cfgMsg,
        ]),
      ]));
    const modalEl = el("div", { class: "modal fade", tabindex: "-1" },
      el("div", { class: "modal-dialog modal-dialog-centered modal-lg modal-dialog-scrollable" },
        el("div", { class: "modal-content" }, [
          el("div", { class: "modal-header py-2" }, [
            el("h5", { class: "modal-title", html: '<i class="bi bi-gear-fill"></i> 매매 설정' }),
            el("button", { class: "btn-close", type: "button", "data-bs-dismiss": "modal" }),
          ]),
          modalBody,
          el("div", { class: "modal-footer py-2" },
            el("button", { class: "btn btn-sm btn-secondary", type: "button", "data-bs-dismiss": "modal" }, "닫기")),
        ]),
      ),
    );
    container.appendChild(modalEl);
    const settingsModal = new bootstrap.Modal(modalEl);
    const renderUsage = (u) => {
      usageBox.innerHTML = "";
      if (!u || !u.max_rps) {
        usageBox.appendChild(el("div", { class: "text-secondary" }, "API 호출 기록 없음"));
        return;
      }
      const pctUse = u.usage_pct ?? 0;
      const heavy = pctUse >= 80 || (u.rate_limited_1h || 0) > 0 || u.auto_throttled;
      const tone = heavy ? "bg-danger" : pctUse >= 50 ? "bg-warning" : "bg-success";
      usageBox.append(
        el("div", { class: "d-flex justify-content-between" }, [
          el("span", { class: "text-secondary" }, "현재 호출 속도"),
          el("span", { class: heavy ? "fw-semibold text-danger" : "" },
            `${u.rps_10s}/${u.max_rps} rps (${pctUse}%)`),
        ]),
        el("div", { class: "progress", style: "height:6px" },
          el("div", { class: `progress-bar ${tone}`, style: `width:${Math.min(100, pctUse)}%` })),
        el("div", { class: "text-secondary mt-1" },
          `최근 1분 ${u.calls_1m}회 · 1시간 ${u.calls_1h}회` +
          (u.throttle_wait_sec ? ` · 대기 ${u.throttle_wait_sec}s` : "") +
          (u.errors_1h ? ` · 오류 ${u.errors_1h}회(1h)` : "")),
        u.auto_throttled
          ? el("div", { class: "alert alert-warning py-1 px-2 mt-2 mb-0 small" },
              `⚠ 자동 감속 중 — 한도 초과로 상한을 ${u.configured_rps} → ${u.max_rps} rps 로 낮췄습니다(누적 ${u.penalties}회). 안정되면 자동 회복하며, 반복되면 감시 주기를 늘리세요.`)
          : el("div", { class: "text-secondary small mt-1" },
              `자동 감속 미작동 (설정 상한 ${u.configured_rps} rps) — 429 발생 시 서버가 스스로 속도를 낮춥니다.`),
      );
    };

    openCfgBtn.onclick = () => gearBtn.onclick();
    gearBtn.onclick = async () => {
      loadSettings();
      settingsModal.show();
      try {
        const st = await fetchJSON("/api/trading/status");
        renderUsage(st.api_usage);
      } catch (e) { /* 상태 카드가 알림 */ }
    };

    const loadSettings = async () => {
      try {
        const s = await fetchJSON("/api/trading/settings");
        envSel.value = s.env;
        appKeyIn.placeholder = s.app_key_masked || "미설정";
        secretIn.placeholder = s.has_secret ? "설정됨 — 변경 시에만 입력" : "미설정";
        accountIn.placeholder = s.account || s.account_masked || "미설정";
      } catch (e) { /* 서비스 다운은 상태 카드가 알림 */ }
    };
    saveBtn.onclick = async () => {
      if (envSel.value === "real" &&
          !confirm("실전 환경으로 저장합니다. 승인된 주문은 실제 계좌로 발주됩니다. 계속할까요?")) return;
      saveBtn.disabled = true;
      cfgMsg.textContent = "";
      try {
        const r = await postJSON("/api/trading/settings", {
          env: envSel.value,
          app_key: appKeyIn.value,
          secret_key: secretIn.value,
          account: accountIn.value,
        });
        cfgMsg.className = "small mt-2 text-success";
        cfgMsg.textContent = `저장됨 (환경: ${r.env === "real" ? "실전" : "모의투자"})`;
        appKeyIn.value = secretIn.value = accountIn.value = "";
        loadSettings();
        loadStatus();
      } catch (e) {
        cfgMsg.className = "small mt-2 text-danger";
        cfgMsg.textContent = "저장 실패: " + e.message;
      } finally {
        saveBtn.disabled = false;
      }
    };

    // 1분봉도 상용급 인터랙티브 차트(시각 축) — 실시간 갱신은 확대/이동을 보존.
    const mHost = el("div", { style: "width:100%;height:52vh;min-height:300px" });
    const symbolSel = el("select", { class: "form-select form-select-sm w-auto" });
    const mChart = createProChart(mHost, { up: "#d64545", down: "#3a6fd8", axis: "time" });
    const mPeriods = [["30분", 30], ["1시간", 60], ["2시간", 120], ["전체", "all"]];
    const mPeriodGroup = el("div", { class: "btn-group btn-group-sm" });
    const mPeriodBtns = mPeriods.map(([lbl, n]) => {
      const b = el("button", { class: "btn btn-outline-secondary", type: "button" }, lbl);
      b.onclick = () => { mChart.setVisibleCount(n); mPeriodBtns.forEach((x) => x.classList.toggle("active", x === b)); };
      mPeriodGroup.appendChild(b);
      return b;
    });
    const mBB = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "볼린저밴드");
    let mBBon = false;
    mBB.onclick = () => { mBBon = !mBBon; mBB.classList.toggle("active", mBBon); mChart.setIndicator("bb", mBBon); };
    const mLegend = el("div", { class: "small d-flex gap-2 flex-wrap align-items-center ms-auto" },
      MA_DEFS.map((d) => el("span", { style: `color:${d.color};font-weight:600` }, `━ MA${d.p}`)));
    // 분봉은 하루 단위로 본다 — 기본 오늘(최근 거래일), 데이터 있는 날짜만 선택 가능
    const mDate = el("input", { type: "date", class: "form-control form-control-sm w-auto",
                                title: "데이터가 있는 날짜만 선택됩니다" });
    const mDateList = el("datalist", { id: "bar-dates" });
    const mToday = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "오늘");
    chart.body.append(
      el("div", { class: "d-flex align-items-center gap-2 flex-wrap mb-2" },
        [symbolSel, mDate, mToday, mDateList, mPeriodGroup, mBB,
         el("span", { class: "small text-secondary" }, "휠 확대·드래그 이동·더블클릭 리셋"), mLegend]),
      mHost,
    );

    let watch = {};
    let mCurSym = "";
    let mCurDate = "";          // "" = 오늘(최근 거래일)
    let mDates = [];            // 데이터 보유 날짜 (최신순)

    const loadDates = async () => {
      if (!symbolSel.value) return;
      try {
        const d = await fetchJSON(`/api/trading/bars/${symbolSel.value}/dates`);
        mDates = d.dates || [];
      } catch (e) { mDates = []; }
      // 데이터 있는 날짜만 선택 가능하도록 범위·목록 제한 (브라우저 기본 표기)
      mDateList.innerHTML = "";
      for (const day of mDates) mDateList.appendChild(el("option", { value: day }));
      mDate.setAttribute("list", "bar-dates");
      if (mDates.length) {
        mDate.min = mDates[mDates.length - 1];
        mDate.max = mDates[0];
      }
      mDate.value = mCurDate || (mDates[0] || "");
      mDate.title = mDates.length
        ? `데이터 보유 ${mDates.length}일 (${mDates[mDates.length - 1]} ~ ${mDates[0]})`
        : "저장된 분봉 없음";
    };

    const loadChart = async (force = false) => {
      if (!symbolSel.value) return;
      try {
        const q = mCurDate ? `?tf=1m&date=${mCurDate}` : "?tf=1m&live=1";
        const bars = await fetchJSON(`/api/trading/bars/${symbolSel.value}${q}`);
        const key = symbolSel.value + "|" + mCurDate;
        if (force || key !== mCurSym) {
          mCurSym = key;
          mChart.setData(bars);                       // 종목·날짜 전환 → 새로 그림
          mPeriodBtns.forEach((x) => x.classList.remove("active"));
        } else {
          mChart.update(bars);                        // 실시간 갱신 → 확대/이동 보존
        }
      } catch (e) { /* 서비스 다운 시 상태 카드에 표시됨 */ }
    };
    symbolSel.onchange = async () => { mCurDate = ""; await loadDates(); loadChart(true); };
    mDate.onchange = () => {
      const v = mDate.value;
      if (v && mDates.length && !mDates.includes(v)) {
        alert("해당 날짜의 분봉 데이터가 없습니다 (보유: " + mDates.slice(0, 5).join(", ") + " …)");
        mDate.value = mCurDate || mDates[0] || "";
        return;
      }
      mCurDate = (v && v === mDates[0]) ? "" : v;     // 최신일이면 실시간 모드
      loadChart(true);
    };
    mToday.onclick = () => { mCurDate = ""; mDate.value = mDates[0] || ""; loadChart(true); };

    // --- 종목 → 1분봉 차트 연결 ---
    // 신호·주문 표의 종목명을 누르면 차트 카드가 그 종목으로 바뀐다.
    // 감시목록에서 빠진 종목(과거 주문 등)도 분봉이 남아 있으면 볼 수 있어야 하므로
    // 드롭다운에 임시로 유지한다(chartExtra) — 감시목록 갱신에도 지워지지 않는다.
    const chartExtra = {};
    const rebuildSymbols = () => {
      const keep = symbolSel.value;
      const all = { ...watch, ...chartExtra };
      symbolSel.innerHTML = "";
      Object.entries(all)
        .sort((a, b) => String(a[1]).localeCompare(String(b[1]), "ko"))   // 종목명 오름차순
        .forEach(([code, nm]) =>
          symbolSel.appendChild(el("option", { value: code }, `${nm} (${code})`)));
      if (keep && all[keep]) symbolSel.value = keep;
    };

    const showOnChart = async (symbol, name) => {
      if (!symbol) return;
      if (!watch[symbol] && !chartExtra[symbol]) {
        chartExtra[symbol] = name || symbol;
        rebuildSymbols();
      }
      const jump = () => chart.col.scrollIntoView({ behavior: "smooth", block: "start" });
      if (symbolSel.value === symbol) { jump(); return; }   // 이미 그 종목이면 이동만
      symbolSel.value = symbol;
      mCurDate = "";
      jump();
      await loadDates();
      loadChart(true);
    };

    /** 표의 '종목명 (코드)' 셀 — 클릭하면 1분봉 차트로 이동.
     *  a[href="#"] 는 해시 라우터(#/페이지)를 건드리므로 button 을 쓴다. */
    const symbolCell = (symbol, name) => {
      const label = name && name !== symbol ? `${name} (${symbol})` : symbol;
      // 색은 본문색 유지 — 이 화면에서 빨강/파랑은 손익을 뜻하므로 링크색을 쓰면
      // 종목명이 손실처럼 읽힌다. 점선 밑줄로만 '누를 수 있음'을 표시한다.
      const b = el("button", {
        type: "button",
        class: "btn btn-link p-0 border-0 align-baseline text-start link-body-emphasis",
        style: "text-decoration: underline dotted; text-underline-offset: 2px",
        title: "클릭하면 1분봉 차트에서 이 종목을 봅니다",
      }, label);
      b.onclick = () => showOnChart(symbol, name);
      return el("td", {}, b);
    };

    // --- 보유 포지션 · 청산 관리 ---
    // 실제 계좌 보유(키움)와 시스템 추적 포지션(손절/목표 감시 대상)을 나란히 본다.
    // 둘이 어긋나면(체결 실패·수동 매매 등) 경고를 띄운다 — 유령 포지션 감시 방지.
    const posBody = el("div", { class: "small" });
    posC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-shield-check"></i> 장중 30초마다 <b>손절·목표</b>를 감시해 도달 시 자동 시장가 청산하고, <b>15:30 이후</b> 남은 물량은 종가로 정리합니다(오버나이트 없음).' })),
      posBody);

    const closePos = async (id, name) => {
      if (!confirm(`${name} 추적 포지션을 청산 처리할까요? (장부 기록 — 실제 매도 주문은 별도)`)) return;
      try { await postJSON(`/api/trading/positions/${id}/close`); changed("pos", null); await loadPositions(); }
      catch (e) { alert("실패: " + e.message); }
    };

    const loadPositions = async () => {
      let perf = null, acct = null;
      try { perf = await fetchJSON("/api/trading/performance"); } catch (e) { return; }
      try { acct = await fetchJSON("/api/trading/account"); } catch (e) { acct = null; }
      if (!changed("positions", [perf, acct])) return;
      posBody.innerHTML = "";
      const open = (perf && perf.open) || [];
      const holds = (acct && acct.ok && acct.holdings) || [];
      const holdBy = {};
      for (const h of holds) holdBy[h.code] = h;
      const trackBy = {};
      for (const p of open) trackBy[p.symbol] = p;

      // 불일치 경고 — 계좌엔 있는데 미추적 / 추적 중인데 계좌에 없음
      const untracked = holds.filter((h) => !trackBy[h.code]);
      const ghost = open.filter((p) => !holdBy[p.symbol]);
      if (acct && acct.ok && (untracked.length || ghost.length)) {
        const msgs = [];
        if (ghost.length) msgs.push(`추적 중이나 계좌에 없음 ${ghost.length}건 (${ghost.map((p) => p.name || p.symbol).join(", ")}) — 미체결·수동매도 가능성`);
        if (untracked.length) msgs.push(`계좌 보유이나 미추적 ${untracked.length}건 (${untracked.map((h) => h.name).join(", ")}) — 손절·목표 감시 대상 아님`);
        posBody.appendChild(el("div", { class: "alert alert-warning py-2 px-3 mb-2" },
          [el("div", { class: "fw-semibold" }, "⚠ 계좌와 추적 포지션 불일치"),
           ...msgs.map((m) => el("div", { class: "small" }, m))]));
      }

      // 계좌 실보유 (진짜 돈이 들어간 것)
      posBody.appendChild(el("div", { class: "fw-semibold mb-1" },
        `계좌 보유 ${holds.length}건` + (acct && acct.ok ? ` · 평가 ${won(acct.total_eval)} · 손익 ${won(acct.total_pl)}` : "")));
      if (holds.length) {
        const t = el("table", { class: "table table-sm align-middle mb-3 small" });
        t.appendChild(el("thead", { html: "<tr><th>종목</th><th>수량</th><th>평단</th><th>현재가</th><th>평가손익</th><th>손절/목표</th><th></th></tr>" }));
        const tb = el("tbody");
        for (const h of holds) {
          const p = trackBy[h.code];
          const tone = h.pl_amt >= 0 ? "text-danger" : "text-primary";   // 한국식
          const tr = el("tr", { html:
            `<td>${h.name || h.code}</td><td>${fmt(h.qty)}</td><td>${fmt(h.avg_price)}</td>` +
            `<td>${fmt(h.cur_price)}</td>` +
            `<td class="${tone}">${won(h.pl_amt)} (${(h.pl_rt ?? 0).toFixed(2)}%)</td>` +
            `<td>${p ? `${fmt(p.stop)} / ${fmt(p.target)}` : '<span class="text-warning">감시 없음</span>'}</td>` });
          const td = el("td");
          if (p) {
            const b = el("button", { class: "btn btn-sm btn-outline-secondary py-0", type: "button" }, "청산");
            b.onclick = () => closePos(p.id, h.name || h.code);
            td.appendChild(b);
          }
          tr.appendChild(td);
          tb.appendChild(tr);
        }
        t.appendChild(tb);
        posBody.appendChild(el("div", { class: "table-responsive" }, t));
      } else {
        posBody.appendChild(el("div", { class: "text-secondary mb-3" },
          acct && acct.ok ? "계좌 보유 종목 없음" : "계좌 조회 불가"));
      }

      // 추적 중이나 계좌에 없는 포지션(유령) — 별도 표시
      if (ghost.length) {
        posBody.appendChild(el("div", { class: "fw-semibold mb-1" }, `추적 전용 ${ghost.length}건 (계좌 미보유)`));
        const t = el("table", { class: "table table-sm align-middle mb-0 small" });
        t.appendChild(el("thead", { html: "<tr><th>종목</th><th>규칙</th><th>진입</th><th>손절</th><th>목표</th><th></th></tr>" }));
        const tb = el("tbody");
        for (const p of ghost) {
          const tr = el("tr", { class: "table-warning", html:
            `<td>${p.name || p.symbol}</td><td>${p.rule}</td><td>${fmt(p.entry)}</td>` +
            `<td>${fmt(p.stop)}</td><td>${fmt(p.target)}</td>` });
          const td = el("td");
          const b = el("button", { class: "btn btn-sm btn-outline-danger py-0", type: "button" }, "추적 종료");
          b.onclick = () => closePos(p.id, p.name || p.symbol);
          td.appendChild(b); tr.appendChild(td); tb.appendChild(tr);
        }
        t.appendChild(tb);
        posBody.appendChild(el("div", { class: "table-responsive" }, t));
      }
    };

    // --- 다음 스캔 카운트다운 ---
    // 상태 API 는 10초마다 오므로 서버가 준 next_scan_sec 을 기준점으로 삼고
    // 브라우저가 1초씩 깎는다(주기를 짧게 써도 매끄럽게 보이도록).
    const cdText = el("span", {});
    const cdBar = el("div", { class: "progress-bar", style: "width:0%" });
    const cdBox = el("div", { class: "small mt-1" }, [
      cdText,
      el("div", { class: "progress mt-1", style: "height:4px" }, cdBar),
    ]);
    let scanState = null;      // { interval, next, at } — at = 기준점을 받은 시각
    const tickCountdown = () => {
      if (!scanState) { cdBox.classList.add("d-none"); return; }
      cdBox.classList.remove("d-none");
      const iv = scanState.interval || 60;
      const left = Math.max(0, scanState.next - (Date.now() - scanState.at) / 1000);
      const what = scanState.halted ? "수집" : "스캔";
      cdText.textContent = left >= 1 ? `다음 ${what}까지 ${Math.ceil(left)}초`
                                     : `${what} 실행 중…`;
      cdBar.style.width = `${Math.max(0, Math.min(100, (1 - left / iv) * 100))}%`;
    };

    const loadStatus = async () => {
      let s;
      try {
        s = await fetchJSON("/api/trading/status");
      } catch (e) {
        if (changed("status", "__err__")) {
          status.body.innerHTML = "";
          status.body.appendChild(
            el("div", { class: "text-danger small" },
              "trading 서비스에 연결할 수 없습니다. systemctl status trading 확인.")
          );
        }
        return;
      }
      let a = null;
      try { a = await fetchJSON("/api/trading/account"); } catch (e) { a = null; }
      // 상태+계좌가 직전과 동일하면 다시 그리지 않는다 (깜빡임 제거)
      if (!changed("status", [s, a])) return;
      status.body.innerHTML = "";
      // --- 감시 상태(장중 스캔 중인지) — 가장 먼저, 크게 ---
      const mk = s.market || {};
      const gd = mk.guard || {};
      const TONE = { open: "success", halted: "warning", pre: "warning",
                     closed: "secondary", disabled: "danger" };
      const ICON = { open: "🟢", halted: "⏸", pre: "🟡", closed: "⚪", disabled: "🔴" };
      // collecting = 루프가 도는가(분봉 수집). scanning = 신호를 평가·발주하는가.
      // 가드가 걸리면 앞은 계속되고 뒤만 멈춘다 — 배너가 그 차이를 말해야 한다.
      const running = mk.collecting ?? mk.scanning;
      const ageTxt = mk.last_scan_age_sec == null ? "스캔 기록 없음"
        : mk.last_scan_age_sec < 120 ? `${mk.last_scan_age_sec}초 전 스캔`
        : `${Math.floor(mk.last_scan_age_sec / 60)}분 전 스캔`;
      const stale = running && mk.last_scan_age_sec != null && mk.last_scan_age_sec > 180;
      // 카운트다운 기준점 갱신 — 루프가 돌지 않으면 숨긴다
      scanState = (running && !stale && mk.next_scan_sec != null)
        ? { interval: mk.scan_interval_sec || 60, next: mk.next_scan_sec,
            at: Date.now(), halted: mk.phase === "halted" }
        : null;
      tickCountdown();
      const detail = mk.phase === "halted"
        ? "신규 진입만 중단됩니다 — 보유 포지션의 손절·목표 감시와 장 마감 정리, 분봉 수집은 계속됩니다. 완전 자동 발주를 끄면 사용자 승인 방식으로 매매를 재개할 수 있습니다."
        : gd.manual_override
          ? `일일 가드 도달(${gd.reason || ""}) — 수동 승인 모드라 신호는 계속 나옵니다. 승인 여부는 사용자 판단입니다.`
          : null;
      status.body.appendChild(el("div", {
        class: `alert alert-${stale ? "danger" : (TONE[mk.phase] || "secondary")} py-2 px-3 mb-2`,
      }, [
        el("div", { class: "fw-semibold" },
          `${ICON[mk.phase] || "⚪"} ${mk.label || "상태 미상"}` +
          (running ? ` · ${mk.watch_count ?? "-"}종목 · ${mk.scan_interval_sec ?? 60}초 주기` : "")),
        el("div", { class: "small" },
          (running ? ageTxt + (stale ? " ⚠ 스캔이 멈춘 것 같습니다" : "")
                   : `정규장 ${mk.session || "09:00~15:30"}`)),
        detail ? el("div", { class: "small mt-1" }, detail) : null,
        cdBox,
      ]));
      const envB = badge(s.env === "real" ? "실전" : "모의투자", s.env === "real" ? "danger" : "success");
      const clockBadge = s.server_time
        ? [" ", badge(s.clock_synced ? "동기화 ✓" : "미동기화 ⚠", s.clock_synced ? "success" : "danger")]
        : [];
      const list = el("ul", { class: "list-unstyled small mb-0" }, [
        el("li", {}, ["환경: ", envB]),
        el("li", {}, "엔진: " + (s.engine_enabled ? "가동" : "꺼짐(API 키 미설정)")),
        s.server_time ? el("li", {}, ["서버 시각: " + s.server_time.slice(0, 19).replace("T", " "), ...clockBadge]) : null,
        el("li", {}, "마지막 평가: " + (s.last_run ? s.last_run.slice(0, 19).replace("T", " ") : "—")),
        el("li", {}, "당일 실현손익: " + fmt(s.daily_pnl) + " 원"),
        s.loss_limit_hit
          ? el("li", { class: "text-danger fw-bold" }, "일일 손실 한도 도달 — 신규 신호 차단 중")
          : null,
      ]);
      status.body.appendChild(list);
      // --- 계좌 내역 ---
      if (a) {
        status.body.appendChild(el("hr", { class: "my-2" }));
        if (!a.ok) {
          status.body.appendChild(
            el("div", { class: "text-secondary small" }, "계좌 조회 불가: " + (a.error || ""))
          );
        } else {
          const plTone = a.total_pl >= 0 ? "text-danger" : "text-primary"; // 한국식: 수익 빨강
          status.body.appendChild(
            el("ul", { class: "list-unstyled small mb-1" }, [
              el("li", {}, `계좌번호: ${a.account_no || a.account_name || "—"}`),
              el("li", {}, `추정예탁자산: ${fmt(a.deposit_est)} 원`),
              el("li", {}, `총평가금액: ${fmt(a.total_eval)} 원 (매입 ${fmt(a.total_buy)})`),
              el("li", { class: plTone },
                `평가손익: ${fmt(a.total_pl)} 원 (${a.total_pl_rt.toFixed(2)}%)`),
            ])
          );
          if (a.holdings.length) {
            const tbl = el("table", { class: "table table-sm small mb-0" });
            tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>수량</th><th>평단</th><th>현재가</th><th>손익</th></tr>" }));
            const tb = el("tbody");
            for (const h of a.holdings) {
              tb.appendChild(el("tr", {}, [
                el("td", {}, h.name || h.code),
                el("td", {}, fmt(h.qty)),
                el("td", {}, fmt(h.avg_price)),
                el("td", {}, fmt(h.cur_price)),
                el("td", { class: h.pl_amt >= 0 ? "text-danger" : "text-primary" },
                  `${fmt(h.pl_amt)} (${h.pl_rt.toFixed(1)}%)`),
              ]));
            }
            tbl.appendChild(tb);
            status.body.appendChild(el("div", { class: "table-responsive" }, tbl));
          } else {
            status.body.appendChild(el("div", { class: "text-secondary small" }, "보유 종목 없음"));
          }
        }
      }
      if (s.watchlist && JSON.stringify(s.watchlist) !== JSON.stringify(watch)) {
        watch = s.watchlist;
        rebuildSymbols();
        loadDates().then(() => loadChart(true));
        loadPositions();
      }
    };

    const ORDER_STATUS = {
      pending: ["대기", "secondary"], approved: ["승인", "info"],
      sent: ["발주됨", "success"], rejected: ["거부됨", "danger"],
      error: ["오류", "danger"], expired: ["만료", "secondary"],
    };
    const orderMsg = (o) => {
      try {
        const r = JSON.parse(o.result || "{}");
        if (o.status === "sent") return r.ord_no ? "주문번호 " + r.ord_no : "발주 접수";
        return r.return_msg || r.error || "";
      } catch (e) { return ""; }
    };
    const loadOrders = async () => {
      let all, pendingRows;
      try {
        // pending 은 별도 조회 — 최근 이력 50건에 밀려 대기 주문이 안 보이는 것 방지
        [pendingRows, all] = await Promise.all([
          fetchJSON("/api/trading/orders?status=pending"),
          fetchJSON("/api/trading/orders"),
        ]);
      } catch (e) { return; }
      // 사용자가 발주 수량/금액을 편집 중이면 새로고침으로 입력값을 덮어쓰지 않는다.
      const act = document.activeElement;
      if (act && act.tagName === "INPUT" && pending.body.contains(act)) return;
      if (!changed("orders", [pendingRows, all])) return;
      const orders = pendingRows;
      const history = all.filter((o) => o.status !== "pending").slice(0, 8);
      pending.body.innerHTML = "";
      if (!orders.length) {
        pending.body.appendChild(el("div", { class: "text-secondary small mb-2" }, "대기 중인 주문 없음"));
      } else {
      const tbl = el("table", { class: "table table-sm align-middle mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>규칙</th><th>방향</th><th>진입/손절/목표</th><th>수량 / 발주금액</th><th>경과·현재가 괴리</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const o of orders) {
        const isExit = o.kind === "exit";
        const approve = el("button", { class: "btn btn-sm " + (isExit ? "btn-warning" : "btn-success") + " me-1" }, isExit ? "청산 승인" : "승인");
        const rejectB = el("button", { class: "btn btn-sm btn-outline-danger" }, isExit ? "보류" : "거부");
        // 발주 수량·금액 — 사용자가 승인 전 조정 가능(진입 주문). 금액은 현재가/진입가 기준 예상치.
        const baseQty = o.exec_qty ?? o.qty;
        const px = Number(o.cur_price) || Number(o.entry) || 0;
        let qtyCell;
        const readQty = () => Math.max(0, Math.floor(Number(o.qty) || 0));
        if (isExit) {
          qtyCell = el("td", {}, `${String(baseQty)}주`);
        } else {
          const qtyIn = el("input", { type: "number", min: "1", step: "1", value: String(baseQty),
            class: "form-control form-control-sm text-end", style: "width:5rem" });
          const amtIn = el("input", { type: "number", min: "0", step: "100", value: px ? String(Math.round(baseQty * px)) : "",
            class: "form-control form-control-sm text-end", style: "width:8rem", disabled: px ? null : "" });
          qtyIn.oninput = () => { if (px) amtIn.value = String(Math.round((Number(qtyIn.value) || 0) * px)); };
          amtIn.oninput = () => { if (px) qtyIn.value = String(Math.floor((Number(amtIn.value) || 0) / px)); };
          o._qtyIn = qtyIn;   // approve 핸들러에서 읽음
          qtyCell = el("td", {}, el("div", { class: "d-flex flex-column gap-1" }, [
            el("div", { class: "input-group input-group-sm", style: "width:6.5rem" }, [qtyIn, el("span", { class: "input-group-text" }, "주")]),
            el("div", { class: "input-group input-group-sm", style: "width:9.5rem" }, [amtIn, el("span", { class: "input-group-text" }, "원")]),
          ]));
        }
        approve.onclick = async () => {
          const qty = isExit ? Number(baseQty) : (o._qtyIn ? Math.floor(Number(o._qtyIn.value) || 0) : readQty());
          if (!isExit && qty < 1) { alert("발주 수량은 1주 이상이어야 합니다"); return; }
          const amtStr = px ? ` (약 ${fmt(qty * px)}원)` : "";
          const msg = isExit
            ? `[${o.symbol}] 목표 도달 — ${qty}주 시장가 매도(청산)할까요?`
            : `[${o.symbol}] ${o.rule} ${o.side} ${qty}주${amtStr} — 실제로 발주할까요?`;
          if (!confirm(msg)) return;
          approve.disabled = true;
          try {
            const r = await postJSON(`/api/trading/orders/${o.id}/approve`, isExit ? undefined : { qty });
            if (r.ok) alert("✅ 발주 접수됨\n" + (r.message || ""));
            else if (r.retryable) alert("⚠️ " + (r.message || "증거금 부족 — 대기열에 유지됨"));
            else alert("❌ 발주 거부/실패\n" + (r.message || r.error || ""));
          } catch (e) { alert("발주 오류: " + e.message); }
          loadOrders(); _memo["risk"] = undefined; loadRisk();
        };
        rejectB.onclick = async () => {
          try { await postJSON(`/api/trading/orders/${o.id}/reject`); } catch (e) {}
          loadOrders();
        };
        // 신호 진입가 대비 현재가 괴리 (이미 멀어진 신호를 걸러내게)
        const gap = (o.cur_price && o.entry) ? (o.cur_price - o.entry) / o.entry * 100 : null;
        const gapEl = gap == null ? null : el("div", { class: Math.abs(gap) >= 0.5 ? "text-danger" : "text-secondary" },
          `현재 ${fmt(o.cur_price)} (${gap >= 0 ? "+" : ""}${gap.toFixed(2)}%)`);
        tb.appendChild(el("tr", {}, [
          symbolCell(o.symbol, o.name),
          el("td", {}, o.rule),
          el("td", {}, isExit ? badge("청산", "warning") : sideBadge(o.side)),
          el("td", {}, `${fmt(o.entry)} / ${fmt(o.stop)} / ${fmt(o.target)}`),
          qtyCell,
          el("td", { class: "small text-secondary text-nowrap" }, [
            el("div", {}, agoStr(o.created)),
            el("div", {}, leftStr(o.expires)),
            gapEl,
          ]),
          el("td", {}, [approve, rejectB]),
        ]));
      }
      tbl.appendChild(tb);
      pending.body.appendChild(el("div", { class: "table-responsive" }, tbl));
      }

      // 최근 주문 결과 — 승인 후 실제 발주/거부 이력 (사용자가 결과를 확인)
      const histWrap = el("div", { class: "mt-3" });
      histWrap.appendChild(el("div", { class: "small text-secondary mb-1" }, "최근 주문 결과"));
      if (!history.length) {
        histWrap.appendChild(el("div", { class: "text-secondary small" }, "아직 발주 이력 없음"));
      } else {
        const htbl = el("table", { class: "table table-sm align-middle mb-0" });
        htbl.appendChild(el("thead", { html: "<tr><th>시각</th><th>종목</th><th>방향</th><th>수량</th><th>상태</th><th>결과</th></tr>" }));
        const htb = el("tbody");
        for (const o of history) {
          const [label, color] = ORDER_STATUS[o.status] || [o.status, "secondary"];
          htb.appendChild(el("tr", {}, [
            el("td", { class: "small text-nowrap" }, agoStr(o.created)),
            symbolCell(o.symbol, o.name),
            el("td", {}, o.kind === "exit" ? badge("청산", "warning") : sideBadge(o.side)),
            el("td", {}, String(o.exec_qty ?? o.qty)),
            el("td", {}, badge(label, color)),
            el("td", { class: "small" }, orderMsg(o)),
          ]));
        }
        htbl.appendChild(htb);
        histWrap.appendChild(el("div", { class: "table-responsive" }, htbl));
      }
      pending.body.appendChild(histWrap);
    };

    const loadSignals = async () => {
      let sigs;
      try {
        sigs = await fetchJSON("/api/trading/signals");
      } catch (e) { return; }
      // 가격은 refreshPrices 가 셀만 갱신 — 표 재렌더는 신호 목록이 바뀔 때만.
      const sKey = sigs.map((s) => `${s.ts}:${s.symbol}:${s.rule}:${s.qty}:${s.actionable ? 1 : 0}`).join("|");
      if (!changed("signals", sKey)) return;
      signals.body.innerHTML = "";
      if (!sigs.length) {
        signals.body.appendChild(el("div", { class: "text-secondary small" }, "오늘 신호 없음"));
        return;
      }
      signals.body.appendChild(el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-info-circle"></i> 감시 신호는 <b>금액 제한 없이</b> 산출(감사용) · 실제 발주는 잔고를 반영한 “승인 대기 주문”에서 · 진입가는 <b>감지 시점 기준</b>' })));
      const tbl = el("table", { class: "table table-sm mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>시각</th><th>종목</th><th>현재가</th><th>규칙</th><th>방향</th><th>진입/손절/목표</th><th>수량·상태</th><th>사유</th></tr>" }));
      const tb = el("tbody");
      for (const s of sigs.slice(0, 15)) {
        // 비발주 사유를 note 로 구분 표시(잔고 부족/리스크 한도/국면/롱전용/보류)
        const noteLabel = (n) => {
          if (!n) return ["보류", "secondary"];
          if (n.startsWith("잔고 부족")) return ["잔고 부족", "warning"];
          if (n.startsWith("리스크 한도")) return ["리스크 한도", "warning"];
          if (n.startsWith("국면 게이트")) return ["국면 보류", "info"];
          if (n.startsWith("롱 전용")) return ["숏 미발주", "secondary"];
          return ["보류", "secondary"];
        };
        const statusEl = s.actionable
          ? (s.auto_status
              ? el("span", { title: s.auto_message || "" },
                  badge(s.auto_status === "sent" ? `⚡자동발주 ${s.qty}주` : `자동발주 ${s.auto_status}`,
                        s.auto_status === "sent" ? "danger" : "warning"))
              : el("span", { title: s.guard_warn || "" }, [
                  badge(`승인대기 ${s.qty}주`, "success"),
                  s.guard_warn ? el("span", { class: "ms-1" },
                    badge("⚠ 가드 도달 후", "warning")) : null,
                ]))
          : el("span", { class: "small text-secondary", title: s.note || "" },
              badge(...noteLabel(s.note)));
        // 현재가 셀 — data-px/data-entry 로 태깅해 refreshPrices 가 값만 갱신한다.
        const curTd = el("td", { class: "small text-end text-nowrap", "data-px": s.symbol,
          "data-entry": s.entry || "" }, s.cur_price ? fmt(s.cur_price) : "—");
        priceCellHTML(curTd, s.cur_price, s.entry);
        tb.appendChild(el("tr", {}, [
          el("td", { class: "small text-secondary text-nowrap" }, agoStr(s.ts)),
          symbolCell(s.symbol, s.name),
          curTd,
          el("td", { title: s.priority != null
            ? `발주 우선순위 ${s.priority} — 같은 사이클의 신호는 이 점수가 높은 순으로 발주됩니다(규칙 기대값 × 손익비).` : "" },
            s.priority != null ? `${s.rule} (${Math.round(s.priority)})` : s.rule),
          el("td", {}, sideBadge(s.side)),
          el("td", { class: "small" }, s.entry ? `${fmt(s.entry)} / ${fmt(s.stop)} / ${fmt(s.target)}` : "—"),
          el("td", { class: "small text-nowrap" }, statusEl),
          el("td", { class: "small text-secondary" }, s.note || s.reason),
        ]));
      }
      tbl.appendChild(tb);
      signals.body.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    // 현재가만 2초 주기로 셀 부분 갱신(표 재렌더 없이 → 버튼 안 흔들림)
    const refreshPrices = async () => {
      let m;
      try { m = await fetchJSON("/api/trading/prices"); } catch (e) { return; }
      const prices = (m && m.prices) || {};
      container.querySelectorAll("[data-px]").forEach((cell) => {
        const p = prices[cell.getAttribute("data-px")];
        if (p == null) return;   // 값 없으면 직전 표시 유지
        priceCellHTML(cell, p, cell.getAttribute("data-entry"));
      });
    };

    await Promise.all([loadStatus(), loadOrders(), loadSignals(), loadRisk()]);
    ctx.addTimer(setInterval(refreshPrices, 2_000));   // 현재가 셀만 2초 갱신
    ctx.addTimer(setInterval(tickCountdown, 1_000));   // 다음 스캔 카운트다운
    ctx.addTimer(setInterval(() => { loadStatus(); loadOrders(); loadSignals(); }, 10_000));
    ctx.addTimer(setInterval(loadRisk, 30_000));
    ctx.addTimer(setInterval(loadPositions, 15_000));
    ctx.addTimer(setInterval(() => { if (!mCurDate) loadChart(); }, 5_000)); // 실시간 분봉(오늘일 때만)
  },
};
