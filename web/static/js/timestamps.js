/**
 * timestamps.js – Convert ISO UTC timestamps to local display strings.
 * Finds all elements with data-ts attribute and formats them in local time.
 */
(function () {
  "use strict";

  function formatLocal(isoStr) {
    if (!isoStr) return "—";
    try {
      const d = new Date(isoStr);
      if (isNaN(d.getTime())) return isoStr;
      return d.toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      });
    } catch (e) {
      return isoStr;
    }
  }

  function updateTimestamps() {
    document.querySelectorAll("[data-ts]").forEach(function (el) {
      const ts = el.getAttribute("data-ts");
      if (ts) {
        el.textContent = formatLocal(ts);
        el.title = ts; // keep original as tooltip
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", updateTimestamps);
  } else {
    updateTimestamps();
  }
})();
