// 로그인 폼을 fetch 로 제출해 실패(401)를 인라인으로 표시한다.
(function () {
  var form = document.getElementById("login-form");
  var err = document.getElementById("err");
  if (!form) return;
  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    err.classList.add("d-none");
    var body = new URLSearchParams(new FormData(form));
    var res = await fetch("/login", { method: "POST", body: body, redirect: "manual" });
    if (res.type === "opaqueredirect" || res.status === 0 || res.status === 302) {
      window.location.href = "/";
      return;
    }
    if (res.status === 401) {
      err.classList.remove("d-none");
      return;
    }
    window.location.href = "/";
  });
})();
