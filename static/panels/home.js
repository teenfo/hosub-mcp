import { el } from "../app.js";

// 홈 컨텐츠 자리(placeholder).
// 추후 날씨·미디어·메모·집안 IoT 상태 같은 "홈" 위젯을 이 패널에 넣거나,
// 이 파일을 복제해 새 패널을 만들고 panels/index.js 에 등록하면 된다.
export default {
  id: "home",
  title: "홈",
  async render(body) {
    body.innerHTML = "";
    body.appendChild(
      el("div", { class: "placeholder" }, [
        el("div", { style: "font-size:22px; margin-bottom:6px" }, "🏠"),
        el("div", {}, "홈 관련 컨텐츠 자리입니다."),
        el("div", { style: "margin-top:6px; font-size:12px" },
          "패널 하나를 추가해 날씨·미디어·메모 등을 붙일 수 있습니다."),
      ])
    );
  },
};
