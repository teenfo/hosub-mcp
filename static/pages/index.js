// 페이지 레지스트리. 새 페이지는 여기 import 한 줄 추가로 등록된다.
// 배열 순서가 사이드바/라우팅 순서다. 같은 group 페이지는 연속 배치할 것.

import dashboard from "./dashboard.js";
import trading from "./trading.js";
import discover from "./discover.js";
import scout from "./scout.js";
import rules from "./rules.js";
import backtest from "./backtest.js";
import journal from "./journal.js";
import tnm from "./tnm.js";
import tnmSettings from "./tnm-settings.js";
import llm from "./llm.js";
import llmModels from "./llm-models.js";
import briefing from "./briefing.js";
import weather from "./weather.js";
import docker from "./docker.js";

export const PAGES = [dashboard, trading, discover, scout, rules, backtest, journal,
  tnm, tnmSettings, llm, llmModels, briefing, weather, docker];
