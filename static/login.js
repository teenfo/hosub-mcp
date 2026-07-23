// 로그인 실패 시(HTTP 401 + X-Login-Failed 헤더) 서버가 같은 페이지를 돌려주므로,
// 응답 헤더를 볼 수 없는 정적 페이지에서는 status code 로 판단할 수 없다.
// 대신 서버가 실패 시 body 에 심어둔 표식이 없으므로, 여기서는 제출 후
// 페이지가 다시 로그인 화면이면(=쿠키 미설정) 에러를 노출하는 간단한 방식을 쓴다.
(function () {
  // 실패 응답도 200 이 아닌 401 로 오지만 form 제출은 전체 내비게이션이므로
  // JS 로 가로채 fetch 로 처리해 실패를 표시한다.
  var form = document.querySelector("form.login-box");
  var err = document.getElementById("err");
  if (!form) return;
  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    err.classList.remove("show");
    var body = new URLSearchParams(new FormData(form));
    var res = await fetch("/login", { method: "POST", body: body, redirect: "manual" });
    // 성공: 서버가 302 → / (redirect:manual 이면 opaqueredirect 또는 0)
    if (res.type === "opaqueredirect" || res.status === 0 || res.status === 302) {
      window.location.href = "/";
      return;
    }
    if (res.status === 401) {
      err.classList.add("show");
      return;
    }
    // 그 외: 새로고침
    window.location.href = "/";
  });
})();
