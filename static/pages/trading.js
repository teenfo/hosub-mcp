import { fetchJSON, el, card, badge } from "../app.js";
import { makeLayoutEditable } from "../layout.js";
import { postJSON, fmt, won, pct, priceCellHTML, agoStr, leftStr, sideBadge, stockHTML,
         openStockModal } from "./tradelib.js";

// 매매 데스크 (트레이딩 그룹): 장중 실행에 필요한 것만 — 상태·가드·승인대기·신호.
// 종목 소싱은 '발굴·감시', 리뷰는 '성과·백테스트' 페이지가 담당한다.
// 1분봉 차트는 상시 카드에서 빼서 공통 종목 모달의 탭으로 옮겼다(사용자 결정
// 2026-08-04) — 종목명 클릭(기본정보) 또는 차트 아이콘(차트 탭 바로)으로 연다.

// 실시간 가격 스트림(SSE) — 페이지 재진입 시 이전 연결을 닫기 위한 싱글턴.
// 스트림이 죽어도 2초 폴링(refreshPrices)이 폴백이라 화면은 계속 움직인다.
let priceStream = null;

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
    const signals = card("최근 신호", null, { wide: true, icon: "bi-lightning" });
    const guardC = card("일일 목표·가드", null, { icon: "bi-shield-check" });
    const posC = card("보유 포지션 · 청산 관리", null, { wide: true, icon: "bi-briefcase" });
    // 각 카드를 독립 그리드 아이템으로 등록(id·기본 폭). 편집 모드에서 자유 배치·크기조절.
    // (구 "chart" 카드는 종목 모달 탭으로 이관 — 저장된 레이아웃에 남은 id 는 무시된다)
    const CARDS = [
      ["status", status, 6], ["guard", guardC, 6],
      ["positions", posC, 12],
      ["pending", pending, 12], ["signals", signals, 12],
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

    // --- 일일 손실 가드 임시 해제 ---
    // 한도에 걸렸을 때 수동 승인으로 내려가지 않고 자동 매매를 이어가기 위한 장치.
    // 당일 실현손익은 그대로 두고 '멈추는 선'만 옮긴다.
    const ovMinutes = el("select", { class: "form-select form-select-sm w-auto" },
      [["", "장 마감까지"], [30, "30분"], [60, "1시간"], [120, "2시간"]]
        .map(([v, t]) => el("option", { value: v }, t)));
    const ovReset = el("button", { class: "btn btn-sm btn-warning", type: "button" },
      "기준 리셋");
    const ovExtra = el("input", { class: "form-control form-control-sm", type: "number",
      step: "0.1", min: "0.1", max: "10", value: "0.5", style: "max-width:76px" });
    const ovExtend = el("button", { class: "btn btn-sm btn-outline-warning", type: "button" },
      "추가 허용");
    const ovClear = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" },
      "해제 취소");
    const ovMsg = el("div", { class: "small mt-1" });
    const ovBox = el("div", { class: "border rounded p-2 mt-2 d-none" });

    const ovCall = async (path, body, btn) => {
      if (!confirm("일일 손실 가드를 임시로 엽니다. 당일 실현손익은 그대로 유지되고, "
        + "자동 매매가 멈추는 기준선만 옮겨집니다. 계속할까요?")) return;
      btn.disabled = true;
      ovMsg.textContent = "";
      try {
        const r = await postJSON("/api/trading/" + path, body);
        _memo["risk"] = undefined;
        await loadRisk();
        const ov = r.override || {};
        ovMsg.className = "small mt-1 text-success";
        ovMsg.textContent = ov.active
          ? `적용됨 — 추가 허용 +${ov.extra_loss_pct}% · ${String(ov.until).slice(11, 16)}까지 (오늘 ${ov.count}/${ov.max_count}회)`
          : "임시 해제 취소됨 — 설정 한도로 복귀";
      } catch (e) {
        ovMsg.className = "small mt-1 text-danger";
        ovMsg.textContent = "실패: " + e.message;
      } finally { btn.disabled = false; }
    };
    ovReset.onclick = () => ovCall("guard/override",
      { mode: "reset", minutes: ovMinutes.value ? Number(ovMinutes.value) : null }, ovReset);
    ovExtend.onclick = () => ovCall("guard/override",
      { mode: "extend", extra_pct: parseFloat(ovExtra.value),
        minutes: ovMinutes.value ? Number(ovMinutes.value) : null }, ovExtend);
    ovClear.onclick = async () => {
      ovClear.disabled = true;
      try {
        await postJSON("/api/trading/guard/override/clear", {});
        _memo["risk"] = undefined;
        await loadRisk();
        ovMsg.className = "small mt-1 text-secondary";
        ovMsg.textContent = "임시 해제 취소됨 — 설정 한도로 복귀";
      } catch (e) {
        ovMsg.className = "small mt-1 text-danger";
        ovMsg.textContent = "실패: " + e.message;
      } finally { ovClear.disabled = false; }
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
      ovBox,
    );

    const loadRisk = async () => {
      let r, rz = null;
      try { r = await fetchJSON("/api/trading/risk"); } catch (e) { return; }
      // 실현손익은 증권사 라이브 값을 주값으로 쓴다(원장 합산은 모델 비용이라 어긋난다).
      // 조회 실패 시에는 원장값으로 폴백하되 화면에 그 사실을 적는다.
      try { rz = await fetchJSON("/api/trading/account/realized"); } catch (e) { rz = null; }
      if (!changed("risk", [r, rz])) return;
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
      // 주값 = 증권사 당일 실현손익. 원장값(가드가 판정에 쓰는 값)은 대조로 나란히 둔다.
      const live = rz && rz.today ? rz.today.realized : null;
      const shown = live == null ? r.krw : live;
      // %는 같은 분모(가드가 쓰는 equity)로 다시 계산한다 — 금액만 바꾸고 %를
      // 원장값으로 두면 둘이 어긋나 보인다.
      const shownPct = live == null || !r.equity
        ? r.pct : Number((live / r.equity * 100).toFixed(4));
      const cls = shown >= 0 ? "text-danger" : "text-primary";
      const uc = (rz && rz.unconfirmed) || {};
      const ucN = (uc.entries || 0) + (uc.exits || 0);
      gStatus.append(el("div", { class: "d-flex align-items-baseline gap-2 flex-wrap" }, [
        el("span", { class: "text-secondary" }, "오늘 실현손익 "),
        el("span", { class: "fw-semibold " + cls }, `${won(shown)} (${pct(shownPct)})`),
        el("span", { class: "text-secondary" }, `· ${r.trades}건`),
        live == null
          ? el("span", { class: "badge text-bg-secondary",
              title: "증권사 실현손익을 조회하지 못해 원장(모델 비용) 값을 표시합니다" }, "원장 기준")
          : el("span", { class: "badge text-bg-light text-dark",
              title: "키움 일자별실현손익(수수료·세금 차감 후). 원장 합산은 모델 비용이라 어긋납니다" }, "증권사 실측"),
        live != null && Math.round(live) !== Math.round(r.krw)
          ? el("span", { class: "text-secondary",
              title: "원장은 승인·발주된 주문만 기록하고 비용을 모델로 근사합니다. 가드는 이 원장값으로 판정합니다" },
              `원장 ${won(r.krw)} · 차이 ${won(Math.round(live - r.krw))}`) : null,
        ucN ? el("span", { class: "badge text-bg-warning",
          title: "체결가가 아직 증권사 실측으로 확정되지 않은 건. 마감 후 대사에서 채워집니다" },
          `실측 미확정 ${ucN}건`) : null,
      ]));
      const bar = el("div", { class: "progress mt-2", style: "height:8px",
        title: "표시 금액과 같은 기준(증권사 실측)입니다. 가드 판정은 원장값으로 합니다" });
      const hi = r.daily_target_pct || 0;
      const frac = hi > 0 ? Math.max(0, Math.min(100, shownPct / hi * 100)) : 0;
      bar.appendChild(el("div", { class: "progress-bar " + (shownPct >= 0 ? "bg-danger" : "bg-primary"), style: `width:${shownPct >= 0 ? frac : 0}%` }));
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
      renderOverride(r);
    };

    // 임시 해제 영역 — 한도에 걸렸거나 이미 해제가 살아 있을 때만 펼친다.
    const renderOverride = (r) => {
      const ov = r.override || {};
      const lossHalt = r.halted && String(r.reason || "").includes("손실");
      const show = ov.enabled && (lossHalt || ov.active || ov.count > 0);
      ovBox.classList.toggle("d-none", !show);
      if (!show) return;
      ovBox.innerHTML = "";
      const base = r.daily_loss_limit_pct ?? 0;
      const eff = r.loss_limit_effective_pct ?? base;
      ovBox.append(
        el("div", { class: "small fw-semibold mb-1",
          html: '<i class="bi bi-unlock"></i> 손실 가드 임시 해제' }),
        el("div", { class: "small text-secondary mb-2 lh-sm" },
          `당일 실현손익(${pct(r.pct)})은 그대로 유지되고 '멈추는 선'만 옮깁니다. ` +
          `기준 리셋 = 지금 손익부터 한도를 다시 셈 · 추가 허용 = 한도에 그만큼만 더함. ` +
          `오늘 총량 상한 ${ov.max_pct}% · ${ov.max_count}회.`),
        el("div", { class: "d-flex gap-2 align-items-center flex-wrap mb-1" }, [
          el("span", { class: "badge text-bg-" + (ov.active ? "danger" : "secondary") },
            ov.active
              ? `임시 허용 +${ov.extra_loss_pct}% · ${String(ov.until).slice(11, 16)}까지`
              : "임시 허용 없음"),
          el("span", { class: "small text-secondary" },
            `정지선 ${base}%` + (eff !== base ? ` → ${eff}%` : "")),
          el("span", { class: "small text-secondary" }, `오늘 ${ov.count}/${ov.max_count}회`),
        ]),
        el("div", { class: "d-flex gap-2 align-items-center flex-wrap" }, [
          ovMinutes, ovReset,
          el("div", { class: "input-group input-group-sm w-auto" },
            [ovExtra, el("span", { class: "input-group-text" }, "%")]),
          ovExtend,
          ov.active ? ovClear : null,
        ]),
        ovMsg,
      );
      const spent = ov.granted_pct || 0;
      if (spent >= ov.max_pct || ov.count >= ov.max_count) {
        ovReset.disabled = ovExtend.disabled = true;
        ovBox.appendChild(el("div", { class: "small text-danger mt-1" },
          "오늘 임시 해제 한도를 모두 썼습니다 — 더 늘리려면 매매를 멈추고 점검하세요."));
      } else {
        ovReset.disabled = ovExtend.disabled = false;
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

    // 감시목록 스냅샷 — 변경 감지(loadPositions 트리거)에만 쓴다.
    let watch = {};

    /** 표의 '종목명 (코드)' 셀 — 종목명은 기본정보 모달(공통), 차트 아이콘은
     *  같은 모달의 1분봉 탭으로 바로 연다. 클릭 하나에 둘을 묶으면 어느 쪽이
     *  뜰지 예측할 수 없다 — 눌러야 할 것을 눈에 보이게 나눈다. */
    const symbolCell = (symbol, name) => {
      const chartBtn = el("button", {
        type: "button", class: "btn btn-link p-0 border-0 ms-1 align-baseline text-secondary",
        title: "1분봉 차트 보기",
      }, el("i", { class: "bi bi-graph-up" }));
      chartBtn.onclick = () => openStockModal(symbol, name, { tab: "chart" });
      return el("td", { class: "text-nowrap" },
                [el("span", { html: stockHTML(symbol, name) }), chartBtn]);
    };

    // 데스크 설정(손절 전환선 계산용). renderDesk 가 갱신한다 — 보유 표와
    // 데스크 카드가 같은 값을 봐야 표시가 실제 동작과 일치한다.
    let deskState = {};

    /** 진입가 대비 현재가 위치 + **손절 전환선**까지의 거리.
     *
     * `tighten_at` 이 넘어가면 손절선 계산이 '진입 시 갭 유지' 에서
     * '진입가 + 상승분 × 확정률' 로 바뀐다 — **익절선이 아니라 손절선** 얘기다.
     * 종전 표기('추종 발동')는 익절선으로 읽혔다.
     *
     * 왜 % 하나로 안 되는가: 전환 조건은 **진입가 대비 %가 아니라 진입→목표 갭
     * 대비 비율**(`tighten_at`)이다. 목표가가 종목마다 다르므로 같은 +1.5% 가
     * 어떤 종목에서는 전환이고 어떤 종목에서는 아니다. 그래서 둘을 같이
     * 보여준다 — 진입가 대비 %(직관)와 갭 진행률(실제 조건).
     *
     * 평단(계좌)과 진입가(원장)는 다를 수 있다. 판정은 **원장 진입가**로 하므로
     * 그 기준을 쓰고, 원장이 없으면 평단으로 대체한다. */
    const entryProgress = (h, p, px) => {
      const cur = Number(px ?? h.cur_price ?? 0);
      const entry = Number((p && p.entry) || h.avg_price || 0);
      if (!(cur > 0 && entry > 0)) return '<span class="text-secondary">—</span>';
      const pct = (cur / entry - 1) * 100;
      const tone = pct > 0 ? "text-danger" : pct < 0 ? "text-primary" : "";
      let out = `<span class="${tone}">${pct > 0 ? "+" : ""}${pct.toFixed(2)}%</span>`;
      // 라인 추종이 꺼져 있으면 전환선을 말할 이유가 없다 — 없는 기능을 있는
      // 것처럼 보여주지 않는다.
      const d = deskState || {};
      const target = Number((p && (p.target_live ?? p.target)) || 0);
      const at = Number(d.tighten_at ?? 0);
      if (d.trailing && target > entry && at > 0) {
        const gap = target - entry;
        const trigger = entry + gap * at;
        const prog = (cur - entry) / gap * 100;
        out += ` <span class="text-secondary" style="font-size:.72rem">`
             + `갭 ${prog.toFixed(0)}%`
             + (cur >= trigger
                 ? ` <span class="badge text-bg-warning" title="손절선이 '진입가 + 상승분 × ${d.lock_gain_pct ?? 30}%' 로 계산됩니다">손절 전환됨</span>`
                 : ` · 손절 전환 ${fmt(trigger)}`)
             + `</span>`;
      }
      return out;
    };

    /** 계좌 보유 표의 2초 갱신 대상. `loadPositions` 가 표를 그릴 때 채운다.
     *
     *  왜 필요한가: 이 표는 `/api/account`(키움 잔고)로만 갱신되고 그 호출은
     *  15초 폴링 + **서버 30초 캐시**다. 데스크는 2초마다 판정하는데 화면 숫자는
     *  최대 30초 낡아 있었다 — 사용자가 "가격이 고정돼 보인다"고 지적한 것 중
     *  이 표는 데스크 폴백 수정의 영향을 받지 않는 별도 배관이었다.
     *
     *  추가 API 콜은 0이다. `/api/prices` 를 이미 2초마다 부르고 있고, 보유 종목은
     *  감시목록에 반드시 있으므로 그 응답에 값이 들어 있다. */
    const holdRows = [];

    /** 2초 값으로 현재가·진입가 대비·평가손익을 다시 칠한다.
     *
     *  **평가손익을 `(현재가 − 평단) × 수량` 으로 다시 계산하지 않는다.** 키움의
     *  `evltv_prft` 는 제반비용을 반영한 값이다 — 실측(서산 18주 · 평단 4,100 ·
     *  현재가 4,075)에서 계좌 −616원 vs 단순계산 −450원, 차이 166원이 비용이다.
     *  모델 비용으로 손익을 다시 만들면 그 차이를 잃는다(2026-07-29 대사에서
     *  모델 비용이 손실을 1,796원 과대계상하고 있었던 것과 같은 함정이다).
     *
     *  그래서 **잔고 값에 가격 변화분만 더한다** — 비용 성분은 그대로 보존되고
     *  숫자는 2초마다 움직인다. 잔고가 다시 오면(15초) 그 값이 기준점을 갱신한다. */
    const paintHold = (r, px) => {
      const price = Number(px);
      if (!(price > 0)) return;
      r.cur.textContent = fmt(price);
      r.prog.innerHTML = entryProgress(r.h, r.p, price);
      const qty = Number(r.h.qty || 0);
      const base = Number(r.h.cur_price || 0);
      const cost = Number(r.h.avg_price || 0) * qty;
      if (!(qty > 0 && base > 0 && cost > 0)) return;
      const pl = Number(r.h.pl_amt || 0) + (price - base) * qty;
      const rt = pl / cost * 100;
      r.pl.className = pl >= 0 ? "text-danger" : "text-primary";   // 한국식
      r.pl.textContent = `${won(pl)} (${rt.toFixed(2)}%)`;
    };

    // --- 보유 포지션 · 청산 관리 ---
    // 실제 계좌 보유(키움)와 시스템 추적 포지션(손절/목표 감시 대상)을 나란히 본다.
    // 둘이 어긋나면(체결 실패·수동 매매 등) 경고를 띄운다 — 유령 포지션 감시 방지.
    const posBody = el("div", { class: "small" });
    const deskBox = el("div", { class: "small mb-2" });
    posC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-shield-check"></i> <b>손절·목표</b>에 닿으면 자동 시장가 청산하고, <b>15:30 이후</b> 남은 물량은 종가로 정리합니다(오버나이트 없음). 감시·발주 주기는 아래 <b>매매 데스크</b> 표시를 따릅니다 — 켜져 있으면 데스크가, 꺼져 있으면 기존 30초 루프가 맡습니다.' })),
      deskBox,
      posBody);

    // 청산 = 실제 매도 발주. 2026-07-29 까지 이 버튼은 주문 없이 원장만 닫아
    // 계좌에 종목이 남는 고아를 만들었다. 문구도 그때 것이라 사실과 달랐다.
    const closePos = async (id, name) => {
      if (!confirm(`${name}을(를) 지금 시장가로 매도합니다.\n\n실제 매도 주문이 나갑니다 — 되돌릴 수 없습니다.`)) return;
      try {
        const r = await postJSON(`/api/trading/positions/${id}/close`);
        if (r && r.need_void) {
          // 체결된 적 없는 주문이다 — 팔 것이 없으므로 장부에서 지우는 게 맞다
          if (confirm(`${r.error}\n\n지금 제외할까요?`)) { await voidPos(id, name); return; }
        } else if (r && r.ok === false) {
          alert("발주 거부: " + (r.message || r.error || "사유 미상") + "\n\n원장은 닫지 않았습니다 — 계좌를 확인하세요.");
        }
        changed("pos", null); await loadPositions();
      } catch (e) { alert("실패: " + e.message); }
    };

    // 제외 = 주문 없이 원장만 무효화(고아 정리). 계좌 수량을 먼저 확인시킨다 —
    // 계좌에 실재하는 종목을 제외하면 손절·목표 감시가 사라진다.
    const voidPos = async (id, name) => {
      if (!confirm(`${name}을(를) 추적에서 제외할까요?\n\n매도 주문은 나가지 않습니다. 계좌와 어긋난 장부 항목만 무효 처리합니다.`)) return;
      try {
        let r = await postJSON(`/api/trading/positions/${id}/void`);
        if (r && r.need_confirm) {
          const q = r.held_qty == null ? "확인 불가" : `${fmt(r.held_qty)}주`;
          if (!confirm(`⚠ ${r.error}\n\n계좌 보유 ${q} · 장부 ${fmt(r.ledger_qty ?? 0)}주\n\n그래도 제외할까요? 이 종목의 손절·목표 감시가 사라집니다.`)) return;
          r = await postJSON(`/api/trading/positions/${id}/void`, { confirm: true });
        }
        if (r && r.ok === false) alert("실패: " + (r.error || "사유 미상"));
        changed("pos", null); await loadPositions();
      } catch (e) { alert("실패: " + e.message); }
    };

    // 매매 데스크 계측 — WS 대비 REST 비율이 이 설계의 성적표다.
    // REST 가 늘고 있으면 발굴 예산을 다시 먹는 중이므로 눈에 보여야 한다.
    // 켜고 끄기는 배포 없이 즉시 반영된다(런타임 오버라이드). 초 단위로 실거래
    // 청산을 내는 루프이므로 이상이 보이면 그 자리에서 멈출 수 있어야 한다.
    const deskToggle = el("input", { class: "form-check-input", type: "checkbox",
      role: "switch", id: "deskOn" });
    deskToggle.onchange = async () => {
      const on = deskToggle.checked;
      if (on && !confirm("매매 데스크를 켭니다.\n\n보유 종목 전부의 손절·목표를 초 단위로 보고, 닿으면 데스크가 그 자리에서 시장가로 발주합니다. 그동안 30초 루프는 손절·목표를 보지 않습니다(시간 손절·마감 정리는 그대로).\n\n판정과 발주가 빨라질 뿐 승인 규칙은 지금과 같습니다.\n\n이상이 보이면 이 스위치로 즉시 끌 수 있습니다.")) {
        deskToggle.checked = false; return;
      }
      deskToggle.disabled = true;
      try { await postJSON("/api/trading/desk", { enabled: on }); changed("status", null); await loadStatus(); }
      catch (e) { alert("실패: " + e.message); deskToggle.checked = !on; }
      finally { deskToggle.disabled = false; }
    };
    const deskSwitch = (d) => el("div", { class: "form-check form-switch mb-0 me-2" }, [
      deskToggle,
      el("label", { class: "form-check-label small", for: "deskOn" },
        d.enabled ? "데스크 켜짐" : "데스크 꺼짐"),
    ]);

    /** 추종 파라미터 편집 줄 — 실거래 청산선을 정하는 값이라 화면에서 바로 본다.
     *  폴링이 입력을 덮어쓰지 않도록 포커스 중인 칸은 건드리지 않는다.
     *
     *  `trailing`(라인 추종)이 꺼져 있으면 아래 값들은 `desk.update_lines` 가
     *  첫 줄에서 return 하므로 **하나도 동작하지 않는다**. 그래도 조작 가능하게
     *  두면 화면이 거짓말을 한다 — 실제로 그 상태로 오래 돌았다(2026-07-30:
     *  라인 추종 OFF · 사이클 2,577 · 라인 갱신 0건인데 발동·확정 입력칸이 열려
     *  있었다). 그래서 마스터가 꺼지면 같이 잠근다. */
    const trailInputs = {};
    const trailNum = (key, label, val, step, hint, off) => {
      const inp = trailInputs[key] || (trailInputs[key] = el("input", {
        class: "form-control form-control-sm py-0", type: "number", step,
        style: "width:5rem" }));
      inp.title = off ? "라인 추종이 꺼져 있어 이 값은 쓰이지 않습니다" : hint;
      inp.disabled = !!off;
      if (document.activeElement !== inp) inp.value = val;
      inp.onchange = async () => {
        const v = Number(inp.value);
        if (!isFinite(v)) return;
        inp.disabled = true;
        // tighten_at 만 비율(0~1)이다 — 화면은 %로 보이고 보낼 때 환산한다
        try { await postJSON("/api/trading/desk", { [key]: key === "tighten_at" ? v / 100 : v }); changed("status", null); await loadStatus(); }
        catch (e) { alert("실패: " + e.message); }
        finally { inp.disabled = false; }
      };
      return el("div", { class: "d-flex align-items-center gap-1" },
        [el("span", { class: off ? "text-secondary opacity-50" : "text-secondary" }, label), inp]);
    };

    // 라인 추종 — **마스터 스위치**다. 꺼져 있으면 아래 파라미터·익절선 추종이
    // 전부 무효다(`desk.update_lines` 가 `if not trailing(): return None`).
    // 종전에는 배지로만 보여 조작할 수 없었다 — 하위 옵션만 토글이 있고 상위는
    // 없는 거꾸로 된 구조였다.
    const trailToggle = el("input", { class: "form-check-input", type: "checkbox",
      role: "switch", id: "deskTrail" });
    trailToggle.onchange = async () => {
      const on = trailToggle.checked;
      if (on && !confirm("라인 추종을 켭니다.\n\n손절선이 가격을 따라 올라갑니다 — 진입 시 정한 값에 고정되지 않고, 오르면 따라붙고 내려도 그 자리에 남습니다. 실거래 청산선이 움직입니다.\n\n끄면 즉시 진입 시 값으로 돌아갑니다(이미 올려둔 값은 원본을 보존하고 있습니다).")) {
        trailToggle.checked = false; return;
      }
      trailToggle.disabled = true;
      try { await postJSON("/api/trading/desk", { trailing: on }); changed("status", null); await loadStatus(); }
      catch (e) { alert("실패: " + e.message); trailToggle.checked = !on; }
      finally { trailToggle.disabled = false; }
    };

    const trailSwitch = (d) => {
      if (document.activeElement !== trailToggle) trailToggle.checked = !!d.trailing;
      return el("div", { class: "form-check form-switch mb-0",
        title: d.trailing
          ? `가격이 오르면 같은 갭으로 손절선을 올리고 내려도 그 자리에 둡니다. 목표까지 ${((d.tighten_at ?? 0.5) * 100).toFixed(0)}% 오면 손절선을 상승분의 ${d.lock_gain_pct ?? 30}% 로 끌어올립니다. 익절선은 ${d.trail_target ? '같이 따라 올라가되 진입가 +' + (d.max_gain_pct ?? 3) + '% 를 넘지 않습니다' : '진입 시 값 그대로 고정입니다'}. 손절선이 이익 구간으로 올라가면 시간 손절을 다시 셉니다.`
          : "진입 시 정한 손절·목표를 그대로 씁니다. 아래 파라미터는 켜야 쓰입니다." },
        [trailToggle,
         el("label", { class: "form-check-label", for: "deskTrail" },
           d.trailing ? "라인 추종 ON" : "라인 추종 OFF")]);
    };

    const tgtToggle = el("input", { class: "form-check-input", type: "checkbox",
      role: "switch", id: "deskTgt" });
    tgtToggle.onchange = async () => {
      try { await postJSON("/api/trading/desk", { trail_target: tgtToggle.checked }); changed("status", null); await loadStatus(); }
      catch (e) { alert("실패: " + e.message); tgtToggle.checked = !tgtToggle.checked; }
    };

    // 라벨은 **어느 선을 움직이는지**를 이름에 담는다. '발동'·'확정'은 둘 다
    // 손절선 얘기인데 종전 라벨에 그 말이 없어 익절선으로 읽혔다.
    const trailRow = (d) => {
      const off = !d.trailing;
      if (document.activeElement !== tgtToggle) tgtToggle.checked = !!d.trail_target;
      tgtToggle.disabled = off;
      return el("div", { class: "d-flex gap-3 align-items-center flex-wrap mt-1 small" }, [
        trailNum("tighten_at", "손절 전환", Math.round((d.tighten_at ?? 0.5) * 100), 5,
          "목표까지 이만큼 오면 손절선 계산을 '상승분의 N%' 로 바꿉니다(%)", off),
        trailNum("lock_gain_pct", "손절 확정률", d.lock_gain_pct ?? 30, 5,
          "전환 뒤 손절선 = 진입가 + 상승분 × 이 비율(%). 오른 것 중 몇 %를 지킬지입니다. 클수록 확정분이 크지만 잔진동에 먼저 걸립니다", off),
        trailNum("max_gain_pct", "익절 상한", d.max_gain_pct ?? 3, 0.5,
          "익절선 천장(진입가 대비 %). 0이면 상한 없음", off),
        el("div", { class: "form-check form-switch mb-0" }, [
          tgtToggle,
          el("label", { class: "form-check-label" + (off ? " opacity-50" : ""), for: "deskTgt",
            title: off
              ? "라인 추종이 꺼져 있어 이 설정은 쓰이지 않습니다"
              : "익절선도 손절선처럼 따라 올릴지. 실측에서 켜면 손해였습니다 — 실거래 57건 투사에서 목표에 닿아 익절했을 7건이 되밀려 손절로 나왔습니다(−6,593원). 기본 끔" },
            "익절선 추종"),
        ]),
      ]);
    };

    const renderDesk = (d) => {
      deskBox.innerHTML = "";
      if (!d) return;
      if (document.activeElement !== deskToggle) deskToggle.checked = !!d.enabled;
      if (!d.enabled) {
        deskBox.appendChild(el("div", { class: "d-flex gap-2 align-items-center flex-wrap" }, [
          deskSwitch(d),
          el("span", { class: "text-secondary", html:
            `손절·목표에 닿아도 <b>자동 발주하지 않고 승인 대기</b>로 올립니다 — 승인해야 매도됩니다. `
            + `켜면 보유 ${d.max_symbols ? d.max_symbols + "종목까지" : "전 종목을"} ${d.interval_sec}초마다 보고 데스크가 그 자리에서 발주합니다.` }),
        ]));
        return;
      }
      const total = (d.ws || 0) + (d.rest || 0);
      const restPct = total ? (d.rest / total * 100) : 0;
      deskBox.append(el("div", { class: "d-flex gap-2 align-items-center flex-wrap" }, [
        deskSwitch(d),
        el("span", { class: "badge text-bg-" + (d.degraded ? "warning" : "success") },
          d.degraded ? `강등 (WS 미연결 — ${d.degraded_interval_sec}초)` : `${d.interval_sec}초 주기`),
        el("span", { class: "badge text-bg-light text-dark",
          title: d.max_symbols ? "보유 감시 대상 / 상한" : "보유 전 종목을 봅니다" },
          `감시 ${d.watched ?? 0}종목` + (d.max_symbols ? ` / 상한 ${d.max_symbols}` : "")),
        el("span", {
          class: "badge text-bg-" + (restPct > 20 ? "warning" : "light") + (restPct > 20 ? "" : " text-dark"),
          title: `WS 값으로 판정한 횟수 대 REST 로 보충한 횟수. REST 가 늘면 추가 호출이 늘고 있다는 뜻입니다(${d.stale_sec}초 넘게 틱이 없는 종목만 보충).` },
          `WS ${fmt(d.ws || 0)} · REST ${fmt(d.rest || 0)} (${restPct.toFixed(0)}%)`),
        d.no_price ? el("span", { class: "badge text-bg-secondary", title: "가격을 모르면 판정하지 않습니다" },
          `가격없음 ${fmt(d.no_price)}`) : null,
        trailSwitch(d),
        el("span", { class: "text-secondary" },
          `사이클 ${fmt(d.cycles || 0)} · 청산 ${fmt(d.exits || 0)}건`
          + (d.proposed ? ` · 승인대기 ${fmt(d.proposed)}건` : "")
          + (d.last_tick ? ` · ${d.last_tick}` : "")),
      ]));
      deskBox.appendChild(trailRow(d));
      if (d.override && Object.keys(d.override).length) {
        deskBox.appendChild(el("div", { class: "text-secondary mt-1",
          title: "config.yaml 이 아니라 화면·API 로 바꾼 값입니다. 재배포하면 이 파일이 우선합니다" },
          "런타임 설정 적용 중: " + JSON.stringify(d.override)));
      }
      // 갱신 0건의 이유가 둘이다 — 추종이 꺼져 있어서인지, 켜져 있는데 아직
      // 전환선에 닿은 종목이 없어서인지. 구분하지 않으면 꺼진 것을 모른다.
      deskBox.appendChild(el("div", { class: "text-secondary mt-1" },
        d.last_line
          ? `라인 갱신 ${fmt(d.lines || 0)}회 · 최근 ${d.last_line.at} ${d.last_line.name || d.last_line.symbol} 손절 ${d.last_line.stop == null ? "—" : fmt(d.last_line.stop)} / 목표 ${d.last_line.target == null ? "—" : fmt(d.last_line.target)}`
          : d.trailing
            ? "라인 추종 켜짐 — 아직 갱신된 포지션이 없습니다(가격이 손절 전환선에 닿은 종목 없음)."
            : "라인 추종이 꺼져 있습니다 — 진입 시 정한 손절·목표를 그대로 씁니다. 위 파라미터는 켜야 쓰입니다."));
    };

    /** 손절/목표 셀 — 데스크가 끌어올린 값이 있으면 **그 값이 실제 판정선**이다.
     *  원본을 그대로 보여주면 화면이 거짓말을 한다. 움직인 건 화살표로 알린다. */
    const lineCell = (p) => {
      const s = p.stop_live ?? p.stop, t = p.target_live ?? p.target;
      const moved = p.stop_live != null || p.target_live != null;
      const orig = moved ? ` title="진입 시 ${fmt(p.stop)} / ${fmt(p.target)}${p.lines_updated ? " · 갱신 " + String(p.lines_updated).slice(11, 19) : ""}"` : "";
      return `<span${orig}>${fmt(s)} / ${fmt(t)}${moved ? ' <span class="text-info">↑추종</span>' : ""}</span>`;
    };

    const loadPositions = async () => {
      let perf = null, acct = null;
      try { perf = await fetchJSON("/api/trading/performance"); } catch (e) { return; }
      try { acct = await fetchJSON("/api/trading/account"); } catch (e) { acct = null; }
      if (!changed("positions", [perf, acct])) return;
      posBody.innerHTML = "";
      holdRows.length = 0;             // 표를 다시 그리므로 셀 참조도 버린다
      const open = (perf && perf.open) || [];
      const holds = (acct && acct.ok && acct.holdings) || [];
      const holdBy = {};
      for (const h of holds) holdBy[h.code] = h;
      // 계좌와 대조할 때는 **실제 체결 종목**을 쓴다 — 숏 신호는 현물 계좌에서
      // 공매도가 안 되므로 인버스 ETF 매수로 나간다(exec_symbol). 원 종목으로
      // 보면 실보유가 전부 '계좌에 없음' 으로 읽힌다.
      const execSym = (p) => p.exec_symbol || p.symbol;
      const trackBy = {};
      for (const p of open) trackBy[execSym(p)] = p;

      // 불일치 경고 — 계좌엔 있는데 미추적 / 추적 중인데 계좌에 없음
      const untracked = holds.filter((h) => !trackBy[h.code]);
      const ghost = open.filter((p) => !holdBy[execSym(p)]);
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
        t.appendChild(el("thead", { html: '<tr><th>종목</th><th>수량</th><th>평단</th><th title="2초마다 갱신됩니다(감시목록 실시간 시세). 나머지 열은 계좌 조회 주기입니다">현재가 <i class="bi bi-broadcast"></i></th><th>진입가 대비</th><th>평가손익</th><th>손절/목표</th><th></th></tr>' }));
        const tb = el("tbody");
        for (const h of holds) {
          const p = trackBy[h.code];
          const tone = h.pl_amt >= 0 ? "text-danger" : "text-primary";   // 한국식
          const tr = el("tr", { html:
            `<td>${stockHTML(h.code, h.name)}</td><td>${fmt(h.qty)}</td><td>${fmt(h.avg_price)}</td>` +
            `<td>${fmt(h.cur_price)}</td>` +
            `<td>${entryProgress(h, p)}</td>` +
            `<td class="${tone}">${won(h.pl_amt)} (${(h.pl_rt ?? 0).toFixed(2)}%)</td>` +
            `<td>${p ? lineCell(p) : '<span class="text-warning">감시 없음</span>'}</td>` });
          const td = el("td");
          if (p) {
            const b = el("button", { class: "btn btn-sm btn-outline-secondary py-0", type: "button" }, "청산");
            b.onclick = () => closePos(p.id, h.name || h.code);
            td.appendChild(b);
          }
          tr.appendChild(td);
          tb.appendChild(tr);
          // 셀 참조를 들고 있어야 표 재렌더 없이 값만 갱신할 수 있다 — 표를 다시
          // 그리면 [청산] 버튼이 흔들리고 누르는 중에 사라진다(신호 표와 같은 이유).
          const cells = tr.children;
          holdRows.push({ h, p, cur: cells[3], prog: cells[4], pl: cells[5] });
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
        posBody.appendChild(el("div", { class: "text-secondary mb-1" },
          "계좌에 없는 장부 항목입니다 — 미체결이거나 계좌 밖에서 정리된 건. 제외하면 실현손익·일지·가드 집계에서 빠지고 기록은 남습니다."));
        const t = el("table", { class: "table table-sm align-middle mb-0 small" });
        t.appendChild(el("thead", { html: "<tr><th>종목</th><th>규칙</th><th>진입</th><th colspan=\"2\">손절 / 목표</th><th></th></tr>" }));
        const tb = el("tbody");
        for (const p of ghost) {
          const tr = el("tr", { class: "table-warning", html:
            `<td>${stockHTML(p.symbol, p.name)}</td><td>${p.rule}</td><td>${fmt(p.entry)}</td>` +
            `<td colspan="2">${lineCell(p)}</td>` });
          const td = el("td", { class: "text-nowrap" });
          const b = el("button", { class: "btn btn-sm btn-outline-danger py-0", type: "button",
            title: "주문 없이 장부에서만 제외합니다" }, "제외");
          b.onclick = () => voidPos(p.id, p.name || p.symbol);
          const c = el("button", { class: "btn btn-sm btn-outline-secondary py-0 ms-1", type: "button",
            title: "실제 매도 주문을 냅니다 — 계좌에 없으면 거부될 수 있습니다" }, "청산");
          c.onclick = () => closePos(p.id, p.name || p.symbol);
          td.append(b, c); tr.appendChild(td); tb.appendChild(tr);
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
      deskState = s.desk || {};
      renderDesk(s.desk);
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
                el("td", { html: stockHTML(h.code, h.name) }),
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
            // 사유는 서버가 reason 문구로 실어 보낸다 — 손절을 '목표 도달'로 읽으면 안 된다
            ? `[${o.symbol}] ${o.reason || "청산"} — ${qty}주 시장가 매도(청산)할까요?`
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
      // 계좌 보유 표도 같은 응답으로 갱신한다. 이 표는 키움 잔고(15초 폴링 +
      // 서버 30초 캐시)로만 움직였는데, 데스크는 2초마다 판정한다 — 판정에 쓰는
      // 값과 화면 값이 최대 30초 어긋나 있었다.
      for (const r of holdRows) paintHold(r, prices[r.h.code]);
    };

    // 틱 도착 즉시 해당 종목 셀만 갱신 — 폴링(2초)은 SSE 가 못 받는 구간의 폴백.
    // EventSource 는 끊기면 자동 재접속하므로 onerror 는 조용히 둔다.
    const patchPrice = (sym, p) => {
      if (p == null) return;
      container.querySelectorAll(`[data-px="${sym}"]`).forEach((cell) =>
        priceCellHTML(cell, p, cell.getAttribute("data-entry")));
      for (const r of holdRows) if (r.h.code === sym) paintHold(r, p);
    };
    if (priceStream) { try { priceStream.close(); } catch (e) { /* 무시 */ } }
    try {
      priceStream = new EventSource("/api/trading/prices/stream");
      priceStream.onmessage = (ev) => {
        try {
          const d = JSON.parse(ev.data);
          if (d && d.s) patchPrice(d.s, Number(d.p));
        } catch (e) { /* 깨진 프레임은 버린다 */ }
      };
    } catch (e) { priceStream = null; }

    await Promise.all([loadStatus(), loadOrders(), loadSignals(), loadRisk()]);
    ctx.addTimer(setInterval(refreshPrices, 2_000));   // 현재가 셀만 2초 갱신(SSE 폴백)
    ctx.addTimer(setInterval(tickCountdown, 1_000));   // 다음 스캔 카운트다운
    ctx.addTimer(setInterval(() => { loadStatus(); loadOrders(); loadSignals(); }, 10_000));
    ctx.addTimer(setInterval(loadRisk, 30_000));
    ctx.addTimer(setInterval(loadPositions, 15_000));
    // (구 분봉 카드의 5초/20초 폴링은 종목 모달 이관으로 제거 — 모달이 열려
    //  있는 동안만 tradelib 쪽 타이머가 돈다)
  },
};
