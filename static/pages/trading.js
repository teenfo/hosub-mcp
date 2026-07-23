import { fetchJSON, el, card, badge } from "../app.js";
import { mdToHtml, renderIframe } from "./briefing.js";

// 트레이딩 페이지: trading 서비스(127.0.0.1:8600)를 /api/trading/* 프록시로 조회하고
// 승인 대기 주문을 승인/거부한다. 차트는 외부 의존성 없는 캔버스 캔들로 그린다.

async function postJSON(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
  return data;
}

const fmt = (n) => Number(n).toLocaleString("ko-KR", { maximumFractionDigits: 0 });
const sideBadge = (side) =>
  badge(side === "short" ? "숏" : "롱", side === "short" ? "danger" : "success");

// --- 캔버스 캔들차트 (한국식: 상승 빨강 / 하락 파랑) ---
function drawCandles(canvas, bars) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 600;
  const h = 320;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  canvas.style.height = h + "px";
  const g = canvas.getContext("2d");
  g.scale(dpr, dpr);
  g.clearRect(0, 0, w, h);
  if (!bars.length) {
    g.fillStyle = "#888";
    g.font = "13px sans-serif";
    g.fillText("데이터 없음 — 장중 수집 후 표시됩니다", 16, 40);
    return;
  }
  const view = bars.slice(-120);
  const padL = 8, padR = 56, padY = 12;
  const lo = Math.min(...view.map((b) => b.low));
  const hi = Math.max(...view.map((b) => b.high));
  const y = (p) => padY + (hi - p) / (hi - lo || 1) * (h - padY * 2);
  const cw = (w - padL - padR) / view.length;
  view.forEach((b, i) => {
    const x = padL + i * cw + cw / 2;
    const up = b.close >= b.open;
    g.strokeStyle = g.fillStyle = up ? "#d64545" : "#3a6fd8";
    g.beginPath();
    g.moveTo(x, y(b.high));
    g.lineTo(x, y(b.low));
    g.stroke();
    const bh = Math.max(1, Math.abs(y(b.open) - y(b.close)));
    g.fillRect(x - cw * 0.35, Math.min(y(b.open), y(b.close)), cw * 0.7, bh);
  });
  // 우측 가격 축 (고/중/저)
  g.fillStyle = "#999";
  g.font = "11px sans-serif";
  [hi, (hi + lo) / 2, lo].forEach((p) => g.fillText(fmt(p), w - padR + 6, y(p) + 4));
  // 하단 시간 축 (시작/끝)
  const t = (s) => new Date(s * 1000).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
  g.fillText(t(view[0].time), padL, h - 2);
  g.fillText(t(view[view.length - 1].time), w - padR - 40, h - 2);
}

export default {
  id: "trading",
  title: "트레이딩",
  icon: "bi-graph-down-arrow",
  async render(container, ctx) {
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    const status = card("트레이딩 상태", null, { icon: "bi-activity" });
    const watchC = card("감시목록 관리", null, { icon: "bi-eye" });
    const pending = card("승인 대기 주문", null, { wide: true, icon: "bi-hourglass-split" });
    const scannerC = card("급등 스캐너", null, { wide: true, icon: "bi-rocket-takeoff" });
    const discoveryC = card("야간 발굴 (전일 전종목 분석)", null, { wide: true, icon: "bi-moon-stars" });
    const chart = card("1분봉 차트", null, { wide: true, icon: "bi-candlestick" });
    const signals = card("최근 신호", null, { wide: true, icon: "bi-lightning" });
    const reportC = card("야간 분석 리포트", null, { icon: "bi-journal-text" });
    // 레이아웃: 상단(상태|감시목록) → 중단(왼쪽: 승인/스캐너/발굴 스택, 오른쪽: 분석 리포트 세로 구역) → 하단(차트/신호)
    const leftStack = el("div", { class: "col-12 col-xl-6 d-flex flex-column gap-3" });
    for (const c of [pending, scannerC, discoveryC]) {
      const cardEl = c.col.querySelector(".card");
      cardEl.classList.remove("h-100");
      leftStack.appendChild(cardEl);
    }
    const rightStack = el("div", { class: "col-12 col-xl-6" });
    const reportCard = reportC.col.querySelector(".card");
    reportCard.classList.add("h-100");
    rightStack.appendChild(reportCard);
    row.append(status.col, watchC.col, leftStack, rightStack, chart.col, signals.col);

    // --- 야간 분석 리포트: 스케줄러가 쓴 브리핑을 트레이딩 페이지에서 바로 표시 ---
    const rSel = el("select", { class: "form-select form-select-sm w-auto d-none" });
    const rTime = el("span", { class: "small text-secondary" });
    const rBody = el("div");
    reportC.body.append(
      el("div", { class: "d-flex align-items-center gap-2 mb-2" }, [rSel, rTime]),
      rBody,
    );
    const loadReport = async (date) => {
      let d;
      try {
        d = await fetchJSON("/api/briefing" + (date ? "?date=" + encodeURIComponent(date) : ""));
      } catch (e) { return; }
      rBody.innerHTML = "";
      if (!d.exists) {
        rSel.classList.add("d-none");
        rTime.textContent = "";
        rBody.appendChild(el("div", { class: "text-secondary small py-3 text-center" },
          "아직 분석 리포트가 없습니다 — Cowork 예약 작업이 리포트를 생성하면 여기 표시됩니다."));
        return;
      }
      if (d.dates && d.dates.length > 1) {
        rSel.innerHTML = "";
        for (const dt of d.dates) {
          const opt = el("option", { value: dt }, dt);
          if (dt === d.date) opt.selected = true;
          rSel.appendChild(opt);
        }
        rSel.classList.remove("d-none");
      } else {
        rSel.classList.add("d-none");
      }
      rTime.textContent = (d.date ? `기준일 ${d.date}` : "") +
        (d.updated_at ? " · " + new Date(d.updated_at).toLocaleString("ko-KR") : "");
      if (d.format === "md") {
        const holder = el("div", { class: "briefing-body" });
        holder.innerHTML = mdToHtml(d.content);
        rBody.appendChild(holder);
      } else {
        renderIframe(rBody, d.content);  // HTML 은 iframe 격리 (스타일 누출 방지)
      }
    };
    rSel.addEventListener("change", () => loadReport(rSel.value));

    // 상태 카드 헤더에 설정(기어) 버튼 추가 → 클릭 시 API 설정 모달 표시
    const statusHeader = status.col.querySelector(".card-header");
    statusHeader.classList.add("d-flex", "justify-content-between", "align-items-center");
    const gearBtn = el("button", {
      class: "btn btn-sm btn-link p-0 text-secondary", title: "키움 API 설정",
    }, el("i", { class: "bi bi-gear-fill" }));
    statusHeader.appendChild(gearBtn);

    // --- 감시목록 관리: 종목명/코드로 추가 + 영속 목록 + 제거 ---
    const SOURCE_BADGE = { seed: ["기본", "secondary"], manual: ["수동", "primary"], auto: ["발굴", "warning"] };
    const wQuery = el("input", { class: "form-control form-control-sm",
      placeholder: "종목명 또는 코드 (예: 삼성전자 / 005930)" });
    const wAdd = el("button", { class: "btn btn-sm btn-primary" }, "추가");
    const wMsg = el("div", { class: "small mt-1" });
    const wCands = el("div", { class: "d-flex flex-wrap gap-1 mt-1" });  // 후보 선택 칩
    const wTblWrap = el("div", { class: "table-responsive mt-2" });
    watchC.body.append(
      el("div", { class: "d-flex gap-1" }, [wQuery, wAdd]), wMsg, wCands, wTblWrap,
    );
    const loadWatch = async () => {
      let w;
      try { w = await fetchJSON("/api/trading/watchlist"); } catch (e) { return; }
      wTblWrap.innerHTML = "";
      const tbl = el("table", { class: "table table-sm align-middle mb-0 small" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>출처</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const it of w.entries) {
        const [label, tone] = SOURCE_BADGE[it.source] || [it.source, "secondary"];
        const rm = el("button", { class: "btn btn-sm btn-outline-danger py-0" }, "제거");
        rm.onclick = async () => {
          if (!confirm(`${it.name}(${it.code}) 을 감시목록에서 제거할까요?`)) return;
          try { await postJSON("/api/trading/watchlist/remove", { code: it.code }); }
          catch (e) { alert("실패: " + e.message); }
          loadWatch(); loadStatus();
        };
        tb.appendChild(el("tr", {}, [
          el("td", {}, `${it.name} (${it.code})`),
          el("td", {}, badge(label, tone)),
          el("td", {}, rm),
        ]));
      }
      tbl.appendChild(tb);
      wTblWrap.appendChild(tbl);
    };
    const addByQuery = async (payload, msgOnMulti) => {
      wMsg.textContent = "";
      wMsg.className = "small mt-1";
      wCands.innerHTML = "";
      wAdd.disabled = true;
      try {
        const r = await postJSON("/api/trading/watchlist", payload);
        if (r.candidates) {
          // 여러 종목 매칭 → 선택 칩 표시
          wMsg.className = "small mt-1 text-secondary";
          wMsg.textContent = msgOnMulti || `여러 종목이 검색됨 — 선택하세요 (${r.candidates.length})`;
          for (const c of r.candidates) {
            const chip = el("button", { class: "btn btn-sm btn-outline-primary py-0" },
              `${c.name} (${c.code})`);
            chip.onclick = () => addByQuery({ code: c.code, name: c.name });
            wCands.appendChild(chip);
          }
          return;
        }
        // 성공
        wQuery.value = "";
        loadWatch(); loadStatus();
      } catch (e) {
        wMsg.className = "small mt-1 text-danger";
        wMsg.textContent = "추가 실패: " + e.message;
      } finally {
        wAdd.disabled = false;
      }
    };
    wAdd.onclick = () => {
      const q = wQuery.value.trim();
      if (!q) { wMsg.className = "small mt-1 text-danger"; wMsg.textContent = "종목명 또는 코드를 입력하세요"; return; }
      addByQuery({ query: q });
    };
    wQuery.onkeydown = (e) => { if (e.key === "Enter") wAdd.onclick(); };

    // --- 키움 API 설정 폼 (시크릿은 서버가 원문을 돌려주지 않음 — 변경 시에만 입력) ---
    const envSel = el("select", { class: "form-select form-select-sm" }, [
      el("option", { value: "mock" }, "모의투자 (mockapi)"),
      el("option", { value: "real" }, "실전 (api.kiwoom.com)"),
    ]);
    const appKeyIn = el("input", { class: "form-control form-control-sm", autocomplete: "off" });
    const secretIn = el("input", { class: "form-control form-control-sm", type: "password", autocomplete: "new-password" });
    const accountIn = el("input", { class: "form-control form-control-sm", autocomplete: "off" });
    const saveBtn = el("button", { class: "btn btn-sm btn-primary mt-2" }, "저장");
    const cfgMsg = el("div", { class: "small mt-2" });
    const field = (label, input) =>
      el("div", { class: "mb-2" }, [el("label", { class: "form-label small mb-1" }, label), input]);

    // API 설정 모달 (기어 버튼으로 연다)
    const modalBody = el("div", { class: "modal-body" }, [
      field("환경", envSel),
      field("앱키 (App Key)", appKeyIn),
      field("시크릿 키 (Secret Key)", secretIn),
      field("계좌번호", accountIn),
      cfgMsg,
    ]);
    const modalEl = el("div", { class: "modal fade", tabindex: "-1" },
      el("div", { class: "modal-dialog modal-dialog-centered" },
        el("div", { class: "modal-content" }, [
          el("div", { class: "modal-header" }, [
            el("h5", { class: "modal-title", html: '<i class="bi bi-key"></i> 키움 API 설정' }),
            el("button", { class: "btn-close", type: "button", "data-bs-dismiss": "modal" }),
          ]),
          modalBody,
          el("div", { class: "modal-footer" }, [
            el("button", { class: "btn btn-sm btn-secondary", type: "button", "data-bs-dismiss": "modal" }, "닫기"),
            saveBtn,
          ]),
        ]),
      ),
    );
    container.appendChild(modalEl);
    saveBtn.className = "btn btn-sm btn-primary";  // 모달 푸터용 (mt-2 제거)
    const settingsModal = new bootstrap.Modal(modalEl);
    gearBtn.onclick = () => { loadSettings(); settingsModal.show(); };

    const loadSettings = async () => {
      try {
        const s = await fetchJSON("/api/trading/settings");
        envSel.value = s.env;
        appKeyIn.placeholder = s.app_key_masked || "미설정";
        secretIn.placeholder = s.has_secret ? "설정됨 — 변경 시에만 입력" : "미설정";
        accountIn.placeholder = s.account_masked || "미설정";
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

    const canvas = el("canvas", { style: "width:100%" });
    const symbolSel = el("select", { class: "form-select form-select-sm w-auto mb-2" });
    chart.body.append(symbolSel, canvas);

    let watch = {};
    const loadChart = async () => {
      if (!symbolSel.value) return;
      try {
        drawCandles(canvas, await fetchJSON("/api/trading/bars/" + symbolSel.value));
      } catch (e) { /* 서비스 다운 시 상태 카드에 표시됨 */ }
    };
    symbolSel.onchange = loadChart;

    const loadStatus = async () => {
      let s;
      try {
        s = await fetchJSON("/api/trading/status");
      } catch (e) {
        status.body.innerHTML = "";
        status.body.appendChild(
          el("div", { class: "text-danger small" },
            "trading 서비스에 연결할 수 없습니다. systemctl status trading 확인.")
        );
        return;
      }
      status.body.innerHTML = "";
      const envB = badge(s.env === "real" ? "실전" : "모의투자", s.env === "real" ? "danger" : "success");
      const list = el("ul", { class: "list-unstyled small mb-0" }, [
        el("li", {}, ["환경: ", envB]),
        el("li", {}, "엔진: " + (s.engine_enabled ? "가동" : "꺼짐(API 키 미설정)")),
        el("li", {}, "마지막 평가: " + (s.last_run || "—")),
        el("li", {}, "당일 실현손익: " + fmt(s.daily_pnl) + " 원"),
        s.loss_limit_hit
          ? el("li", { class: "text-danger fw-bold" }, "일일 손실 한도 도달 — 신규 신호 차단 중")
          : null,
      ]);
      status.body.appendChild(list);
      // --- 계좌 내역 ---
      try {
        const a = await fetchJSON("/api/trading/account");
        status.body.appendChild(el("hr", { class: "my-2" }));
        if (!a.ok) {
          status.body.appendChild(
            el("div", { class: "text-secondary small" }, "계좌 조회 불가: " + (a.error || ""))
          );
        } else {
          const plTone = a.total_pl >= 0 ? "text-danger" : "text-primary"; // 한국식: 수익 빨강
          status.body.appendChild(
            el("ul", { class: "list-unstyled small mb-1" }, [
              el("li", {}, `계좌: ${a.account_name || "—"}`),
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
      } catch (e) { /* 계좌 조회 실패는 치명적이지 않음 */ }
      if (s.watchlist && JSON.stringify(s.watchlist) !== JSON.stringify(watch)) {
        const keep = symbolSel.value;
        watch = s.watchlist;
        symbolSel.innerHTML = "";
        for (const [code, name] of Object.entries(watch)) {
          symbolSel.appendChild(el("option", { value: code }, `${name} (${code})`));
        }
        if (keep && watch[keep]) symbolSel.value = keep;
        loadChart();
      }
    };

    const loadScanner = async () => {
      let sc;
      try {
        sc = await fetchJSON("/api/trading/scanner");
      } catch (e) { return; }
      scannerC.body.innerHTML = "";
      const cfg = sc.config || {};
      scannerC.body.appendChild(el("div", { class: "text-secondary small mb-2" },
        `등락률 +${cfg.min_change_pct ?? 3}% ↑ · 거래대금 상위 교차 · ` +
        (sc.last_scan ? `마지막 스캔 ${sc.last_scan.slice(11, 19)}` : "장중에만 스캔")));
      const watchBtn = (r) => {
        const add = el("button", { class: "btn btn-sm btn-outline-primary" }, "감시 추가");
        add.onclick = async () => {
          add.disabled = true;
          try {
            await postJSON("/api/trading/watchlist", { code: r.code, name: r.name });
            add.textContent = "추가됨";
            loadStatus();
          } catch (e) { alert("실패: " + e.message); add.disabled = false; }
        };
        return add;
      };
      // --- 급등 조짐 (거래량 선행) ---
      if ((sc.presurge || []).length) {
        scannerC.body.appendChild(el("div", { class: "small fw-bold text-warning" }, "⚡ 급등 조짐 — 거래량 급증, 가격은 아직"));
        const ptbl = el("table", { class: "table table-sm align-middle mb-2" });
        ptbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>현재가</th><th>등락률</th><th>거래량 급증률</th><th></th></tr>" }));
        const ptb = el("tbody");
        for (const r of sc.presurge) {
          ptb.appendChild(el("tr", {}, [
            el("td", {}, `${r.name} (${r.code})`),
            el("td", {}, fmt(r.price)),
            el("td", {}, `${r.change_pct >= 0 ? "+" : ""}${r.change_pct.toFixed(1)}%`),
            el("td", { class: "text-warning" }, `+${fmt(r.surge_pct)}%`),
            el("td", {}, watchBtn(r)),
          ]));
        }
        ptbl.appendChild(ptb);
        scannerC.body.appendChild(el("div", { class: "table-responsive" }, ptbl));
      }
      if (!sc.results.length) {
        scannerC.body.appendChild(el("div", { class: "text-secondary small" }, "조건에 맞는 급등 종목 없음"));
        return;
      }
      const tbl = el("table", { class: "table table-sm align-middle mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>현재가</th><th>등락률</th><th>거래대금</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const r of sc.results) {
        tb.appendChild(el("tr", {}, [
          el("td", {}, `${r.name} (${r.code})`),
          el("td", {}, fmt(r.price)),
          el("td", { class: "text-danger" }, `+${r.change_pct.toFixed(1)}%`),
          el("td", {}, fmt(r.trade_value)),
          el("td", {}, watchBtn(r)),
        ]));
      }
      tbl.appendChild(tb);
      scannerC.body.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    const loadDiscovery = async () => {
      let d;
      try {
        d = await fetchJSON("/api/trading/discovery");
      } catch (e) { return; }
      discoveryC.body.innerHTML = "";
      const runBtn = el("button", { class: "btn btn-sm btn-outline-secondary mb-2" },
        d.running ? "실행 중… " + (d.progress || "") : "지금 분석 실행");
      runBtn.disabled = !!d.running;
      runBtn.onclick = async () => {
        if (!confirm("전종목 일봉 수집·분석을 시작할까요? (약 10~15분, 주문 없음)")) return;
        try { await postJSON("/api/trading/discovery/run"); } catch (e) { alert(e.message); }
        loadDiscovery();
      };
      discoveryC.body.appendChild(el("div", { class: "d-flex gap-2 align-items-center" }, [
        runBtn,
        el("span", { class: "text-secondary small" },
          d.date ? `기준일 ${d.date} · ${d.progress || ""}` : "아직 분석 결과 없음 (평일 17:30 자동 실행)"),
      ]));
      if (d.dataset) {
        discoveryC.body.appendChild(el("div", { class: "text-secondary small mb-2" },
          `📄 데이터셋: ${d.dataset.symbol_count}종목 피처 → ${d.dataset.features_file} (스케줄러 분석용)`));
      }
      if (!(d.picks || []).length) return;
      const tbl = el("table", { class: "table table-sm align-middle mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>종가</th><th>점수</th><th>발굴 사유</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const p of d.picks) {
        const add = el("button", { class: "btn btn-sm btn-outline-primary" }, "감시 추가");
        add.onclick = async () => {
          add.disabled = true;
          try {
            await postJSON("/api/trading/watchlist", { code: p.code, name: p.name });
            add.textContent = "추가됨";
            loadStatus();
          } catch (e) { alert("실패: " + e.message); add.disabled = false; }
        };
        tb.appendChild(el("tr", {}, [
          el("td", {}, `${p.name} (${p.code})`),
          el("td", {}, fmt(p.close)),
          el("td", {}, String(p.score)),
          el("td", { class: "small text-secondary" }, (p.reasons || []).join(" · ")),
          el("td", {}, add),
        ]));
      }
      tbl.appendChild(tb);
      discoveryC.body.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    const loadOrders = async () => {
      let orders;
      try {
        orders = await fetchJSON("/api/trading/orders?status=pending");
      } catch (e) { return; }
      pending.body.innerHTML = "";
      if (!orders.length) {
        pending.body.appendChild(el("div", { class: "text-secondary small" }, "대기 중인 주문 없음"));
        return;
      }
      const tbl = el("table", { class: "table table-sm align-middle mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>규칙</th><th>방향</th><th>진입/손절/목표</th><th>수량</th><th>사유</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const o of orders) {
        const approve = el("button", { class: "btn btn-sm btn-success me-1" }, "승인");
        const rejectB = el("button", { class: "btn btn-sm btn-outline-danger" }, "거부");
        approve.onclick = async () => {
          if (!confirm(`[${o.symbol}] ${o.rule} ${o.side} ${o.qty}주 — 실제로 발주할까요?`)) return;
          approve.disabled = true;
          try { await postJSON(`/api/trading/orders/${o.id}/approve`); }
          catch (e) { alert("발주 실패: " + e.message); }
          loadOrders();
        };
        rejectB.onclick = async () => {
          try { await postJSON(`/api/trading/orders/${o.id}/reject`); } catch (e) {}
          loadOrders();
        };
        tb.appendChild(el("tr", {}, [
          el("td", {}, o.symbol),
          el("td", {}, o.rule),
          el("td", {}, sideBadge(o.side)),
          el("td", {}, `${fmt(o.entry)} / ${fmt(o.stop)} / ${fmt(o.target)}`),
          el("td", {}, String(o.qty)),
          el("td", { class: "small text-secondary" }, o.reason || ""),
          el("td", {}, [approve, rejectB]),
        ]));
      }
      tbl.appendChild(tb);
      pending.body.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    const loadSignals = async () => {
      let sigs;
      try {
        sigs = await fetchJSON("/api/trading/signals");
      } catch (e) { return; }
      signals.body.innerHTML = "";
      if (!sigs.length) {
        signals.body.appendChild(el("div", { class: "text-secondary small" }, "오늘 신호 없음"));
        return;
      }
      const tbl = el("table", { class: "table table-sm mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>규칙</th><th>방향</th><th>사유</th></tr>" }));
      const tb = el("tbody");
      for (const s of sigs.slice(0, 15)) {
        tb.appendChild(el("tr", {}, [
          el("td", {}, `${s.name} (${s.symbol})`),
          el("td", {}, s.rule),
          el("td", {}, sideBadge(s.side)),
          el("td", { class: "small text-secondary" }, s.reason),
        ]));
      }
      tbl.appendChild(tb);
      signals.body.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    await Promise.all([loadStatus(), loadOrders(), loadSignals(), loadScanner(), loadDiscovery(), loadWatch(), loadReport()]);
    ctx.addTimer(setInterval(() => { loadStatus(); loadOrders(); loadSignals(); loadScanner(); }, 10_000));
    ctx.addTimer(setInterval(() => { loadDiscovery(); loadWatch(); }, 30_000));
    ctx.addTimer(setInterval(() => loadReport(rSel.classList.contains("d-none") ? null : rSel.value), 300_000));
    ctx.addTimer(setInterval(loadChart, 5_000)); // 실시간 분봉 (WS 집계 + 형성 중 봉 포함)
  },
};
