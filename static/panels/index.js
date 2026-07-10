// 패널 레지스트리. 새 패널은 여기 import 한 줄 추가로 등록된다.
// 배열 순서가 대시보드 표시 순서다.

import system from "./system.js";
import services from "./services.js";
import jobs from "./jobs.js";
import audit from "./audit.js";
import home from "./home.js";

export const PANELS = [system, services, jobs, audit, home];
