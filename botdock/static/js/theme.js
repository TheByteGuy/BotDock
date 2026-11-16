// Classic (non-module) script loaded in <head> so the saved theme applies before first paint.
(function () {
  try {
    var t = localStorage.getItem("botdock-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: stay dark */ }
})();
