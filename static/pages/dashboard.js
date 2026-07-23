import { mountPanels } from "../app.js";
import system from "../panels/system.js";
import services from "../panels/services.js";
import jobs from "../panels/jobs.js";
import audit from "../panels/audit.js";

// 기존 모니터링 패널들을 담는 기본 대시보드 페이지.
export default {
  id: "dashboard",
  title: "대시보드",
  icon: "bi-speedometer2",
  render(container, ctx) {
    mountPanels(container, [system, services, jobs, audit], ctx);
  },
};
