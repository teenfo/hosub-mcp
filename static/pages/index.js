// 페이지 레지스트리. 새 페이지는 여기 import 한 줄 추가로 등록된다.
// 배열 순서가 사이드바/라우팅 순서다.

import dashboard from "./dashboard.js";
import trading from "./trading.js";
import discover from "./discover.js";
import backtest from "./backtest.js";
import briefing from "./briefing.js";
import weather from "./weather.js";
import docker from "./docker.js";

export const PAGES = [dashboard, trading, discover, backtest, briefing, weather, docker];
