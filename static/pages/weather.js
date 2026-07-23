import { fetchJSON, el } from "../app.js";

// Open-Meteo weather_code → 이모지/설명
const WMO = {
  0: ["☀️", "맑음"], 1: ["🌤️", "대체로 맑음"], 2: ["⛅", "구름 조금"], 3: ["☁️", "흐림"],
  45: ["🌫️", "안개"], 48: ["🌫️", "서리 안개"],
  51: ["🌦️", "약한 이슬비"], 53: ["🌦️", "이슬비"], 55: ["🌧️", "강한 이슬비"],
  61: ["🌧️", "약한 비"], 63: ["🌧️", "비"], 65: ["🌧️", "강한 비"],
  71: ["🌨️", "약한 눈"], 73: ["🌨️", "눈"], 75: ["❄️", "강한 눈"], 77: ["🌨️", "싸락눈"],
  80: ["🌦️", "소나기"], 81: ["🌧️", "소나기"], 82: ["⛈️", "강한 소나기"],
  85: ["🌨️", "눈 소나기"], 86: ["❄️", "강한 눈 소나기"],
  95: ["⛈️", "뇌우"], 96: ["⛈️", "우박 뇌우"], 99: ["⛈️", "강한 우박 뇌우"],
};
function wmo(code) {
  return WMO[code] || ["🌡️", "코드 " + code];
}

const DOW = ["일", "월", "화", "수", "목", "금", "토"];

export default {
  id: "weather",
  title: "날씨",
  icon: "bi-cloud-sun",
  async render(container, ctx) {
    const row = el("div", { class: "row g-3", id: "weather-row" });
    container.appendChild(row);

    const load = async () => {
      const res = await fetchJSON("/api/weather");
      row.innerHTML = "";
      if (!res.ok) {
        row.appendChild(
          el("div", { class: "col-12" },
            el("div", { class: "alert alert-warning mb-0" },
              `날씨를 불러오지 못했습니다 (${res.label || ""}). ${res.error || ""}`))
        );
        return;
      }
      const cur = res.data.current;
      const [emoji, desc] = wmo(cur.weather_code);

      // 현재 날씨 카드
      const curCard = el("div", { class: "col-12 col-lg-5" },
        el("div", { class: "card shadow-sm h-100" }, [
          el("div", { class: "card-header" }, el("span", { html: `<i class="bi bi-geo-alt"></i> ${res.label} · 현재` })),
          el("div", { class: "card-body text-center" }, [
            el("div", { style: "font-size:3.5rem; line-height:1" }, emoji),
            el("div", { class: "display-5 fw-semibold mt-2" }, `${Math.round(cur.temperature_2m)}°`),
            el("div", { class: "text-secondary" }, desc),
            el("div", { class: "d-flex justify-content-center gap-3 mt-3 small text-secondary" }, [
              el("span", { html: `<i class="bi bi-thermometer-half"></i> 체감 ${Math.round(cur.apparent_temperature)}°` }),
              el("span", { html: `<i class="bi bi-droplet"></i> 습도 ${cur.relative_humidity_2m}%` }),
              el("span", { html: `<i class="bi bi-wind"></i> ${cur.wind_speed_10m} km/h` }),
            ]),
          ]),
        ])
      );
      row.appendChild(curCard);

      // 예보 카드
      const daily = res.data.daily;
      const items = daily.time.map((t, i) => {
        const [em, ds] = wmo(daily.weather_code[i]);
        const d = new Date(t);
        const label = i === 0 ? "오늘" : DOW[d.getDay()] + "요일";
        return el("div", { class: "d-flex align-items-center py-2 border-bottom" }, [
          el("div", { class: "flex-grow-1" }, label),
          el("div", { style: "font-size:1.4rem", class: "mx-3" }, em),
          el("div", { class: "text-secondary small me-3" }, ds),
          el("div", { class: "fw-medium" }, `${Math.round(daily.temperature_2m_max[i])}° / ${Math.round(daily.temperature_2m_min[i])}°`),
        ]);
      });
      const fcCard = el("div", { class: "col-12 col-lg-7" },
        el("div", { class: "card shadow-sm h-100" }, [
          el("div", { class: "card-header" }, el("span", { html: '<i class="bi bi-calendar-week"></i> 예보' })),
          el("div", { class: "card-body py-2" }, items),
        ])
      );
      row.appendChild(fcCard);
    };

    await load();
    ctx.addTimer(setInterval(load, 600000)); // 10분마다
  },
};
