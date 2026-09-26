/**
 * dashboard.js – Overview page chart + run-now button
 * Requires: Chart.js 4.4.4 + chartjs-adapter-date-fns 3.0.0 (vendored)
 * Colours are read from CSS custom properties at runtime.
 */
(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // CSS custom property helpers
  // -------------------------------------------------------------------------
  function getCSSVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function statusColor(status) {
    if (status === "normal")   return getCSSVar("--color-status-normal");
    if (status === "warning")  return getCSSVar("--color-status-warning");
    if (status === "critical") return getCSSVar("--color-status-critical");
    return getCSSVar("--color-status-unknown");
  }

  // -------------------------------------------------------------------------
  // Chart initialization
  // -------------------------------------------------------------------------
  let levelChart = null;
  let currentRange = "24h";
  let lastData = null;
  let lastWarn = null;
  let lastCrit = null;

  function initChart(data, warningThreshold, criticalThreshold) {
    const ctx = document.getElementById("levelChart");
    if (!ctx || typeof Chart === "undefined") return;

    lastData = data;
    lastWarn = warningThreshold;
    lastCrit = criticalThreshold;

    const readings  = data.readings  || [];
    const weather   = data.weather   || [];
    const lensRows  = data.lens_rows || {};

    // Build level dataset with per-point status colours
    const levelData = readings
      .filter((r) => r.level_index !== null)
      .map((r) => ({
        x: r.ts,
        y: r.level_index,
        status: r.status,
      }));

    // Per-lens coverage datasets
    const lensDatasets = {};
    Object.values(lensRows).forEach((lrs) => {
      lrs.forEach((lr) => {
        if (lr.water_coverage_pct !== null && lr.water_coverage_pct !== undefined) {
          if (!lensDatasets[lr.label]) lensDatasets[lr.label] = [];
          const ts = readings.find((r) => r.id === lr.reading_id);
          if (ts) {
            lensDatasets[lr.label].push({ x: ts.ts, y: lr.water_coverage_pct });
          }
        }
      });
    });

    // Rainfall dataset
    const rainfallData = weather.map((w) => ({
      x: w.hour_ts,
      y: w.precipitation_mm || 0,
    }));

    const pointColors = levelData.map((pt) => statusColor(pt.status));
    const inkColor     = getCSSVar("--color-ink");
    const levelColor   = getCSSVar("--color-chart-level");
    const rainColor    = getCSSVar("--color-chart-rain");
    const mutedColor   = getCSSVar("--color-muted");
    const gridColor    = getCSSVar("--color-chart-grid");
    const bodyFont     = getCSSVar("--font-body").split(",")[0].trim().replace(/["']/g, "");
    const warnColor    = getCSSVar("--color-status-warning");
    const critColor    = getCSSVar("--color-status-critical");
    // Lens series: chart tokens (not accent/accent-2 which are status-ambiguous in day)
    const lensSeriesColors = [
      getCSSVar("--color-chart-lens-a"),
      getCSSVar("--color-chart-lens-b"),
      getCSSVar("--color-ink-2"),
    ];

    const datasets = [
      {
        label: "Level Index",
        data: levelData,
        borderColor: levelColor,
        backgroundColor: pointColors,
        pointBackgroundColor: pointColors,
        pointBorderColor: pointColors,
        pointRadius: 4,
        pointHoverRadius: 6,
        borderWidth: 2,
        tension: 0.2,
        yAxisID: "y",
        spanGaps: false,
        order: 1,
      },
    ];

    // Lens coverage lines
    let ci = 0;
    for (const [label, pts] of Object.entries(lensDatasets)) {
      datasets.push({
        label: `${label} coverage %`,
        data: pts,
        borderColor: lensSeriesColors[ci % lensSeriesColors.length],
        backgroundColor: "transparent",
        borderWidth: 1.5,
        borderDash: [4, 2],
        pointRadius: 2,
        tension: 0.2,
        yAxisID: "y2",
        order: 2,
      });
      ci++;
    }

    // Threshold lines as flat datasets
    const xs = levelData.map((pt) => new Date(pt.x).getTime()).filter((t) => !isNaN(t));
    if (xs.length > 0) {
      const x0 = new Date(Math.min(...xs)).toISOString();
      const x1 = new Date(Math.max(...xs)).toISOString();
      [
        ["Warning / เตือน", warningThreshold, warnColor],
        ["Critical / วิกฤต", criticalThreshold, critColor],
      ].forEach(([name, yv, color]) => {
        datasets.push({
          label: name,
          data: [{ x: x0, y: yv }, { x: x1, y: yv }],
          borderColor: color,
          backgroundColor: "transparent",
          borderWidth: 1.5,
          borderDash: [6, 3],
          pointRadius: 0,
          yAxisID: "y",
          order: 0,
        });
      });
    }

    // Rainfall bars
    if (rainfallData.length > 0) {
      datasets.push({
        type: "bar",
        label: "Rainfall mm/h",
        data: rainfallData,
        backgroundColor: rainColor + "B3",  // 70% opacity
        borderColor: rainColor,
        borderWidth: 1,
        yAxisID: "y3",
        order: 3,
      });
    }

    if (levelChart) {
      levelChart.destroy();
      levelChart = null;
    }

    levelChart = new Chart(ctx, {
      type: "line",
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: true,
            labels: { color: mutedColor, font: { size: 11, family: bodyFont } },
          },
          tooltip: {
            callbacks: {
              title: function (items) {
                const d = new Date(items[0].parsed.x);
                return d.toLocaleString(undefined, {
                  month: "short",
                  day: "numeric",
                  hour: "2-digit",
                  minute: "2-digit",
                  hour12: false,
                });
              },
            },
          },
        },
        scales: {
          x: {
            type: "time",
            time: { tooltipFormat: "PP HH:mm" },
            grid: { color: gridColor },
            ticks: { color: mutedColor, maxTicksLimit: 8, font: { size: 10, family: bodyFont } },
          },
          y: {
            position: "left",
            min: 0,
            max: 100,
            grid: { color: gridColor },
            ticks: { color: mutedColor, font: { size: 10, family: bodyFont } },
            title: {
              display: true,
              text: "Level Index",
              color: mutedColor,
              font: { size: 10, family: bodyFont },
            },
          },
          y2: {
            position: "right",
            min: 0,
            max: 100,
            grid: { display: false },
            ticks: { color: mutedColor, font: { size: 10, family: bodyFont } },
            title: {
              display: true,
              text: "Coverage %",
              color: mutedColor,
              font: { size: 10, family: bodyFont },
            },
            display: Object.keys(lensDatasets).length > 0,
          },
          y3: {
            position: "right",
            min: 0,
            grid: { display: false },
            ticks: { color: mutedColor, font: { size: 10, family: bodyFont } },
            title: {
              display: true,
              text: "Rainfall mm/h",
              color: mutedColor,
              font: { size: 10, family: bodyFont },
            },
            display: rainfallData.length > 0,
            offset: true,
          },
        },
      },
    });
  }

  // -------------------------------------------------------------------------
  // Fetch series data and refresh chart
  // -------------------------------------------------------------------------
  function loadChart(range) {
    currentRange = range;
    fetch("/api/summary")
      .then((r) => r.json())
      .then((summary) => {
        const warnThreshold = summary.level_warning  || 50;
        const critThreshold = summary.level_critical || 90;
        return fetch(`/api/series?range=${range}`).then((r) =>
          r.json().then((data) => ({ data, warnThreshold, critThreshold }))
        );
      })
      .then(({ data, warnThreshold, critThreshold }) => {
        initChart(data, warnThreshold, critThreshold);
      })
      .catch((e) => console.error("Chart load error:", e));
  }

  // -------------------------------------------------------------------------
  // Range buttons
  // -------------------------------------------------------------------------
  function initRangeButtons() {
    document.querySelectorAll(".range-btn").forEach((btn) => {
      btn.addEventListener("click", function () {
        document.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active"));
        this.classList.add("active");
        loadChart(this.dataset.range);
      });
    });
  }

  // -------------------------------------------------------------------------
  // Run-now form
  // -------------------------------------------------------------------------
  function initRunNow() {
    const form = document.getElementById("run-now-form");
    if (!form) return;
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      const btn = form.querySelector("button[type=submit]");
      if (btn) {
        btn.disabled = true;
        btn.textContent = "REQUESTED…";
      }
      const fd = new FormData(form);
      fetch("/run-now", { method: "POST", body: fd })
        .then((r) => r.json())
        .then(() => {
          if (btn) btn.textContent = "✓ REQUESTED";
          setTimeout(() => {
            if (btn) {
              btn.disabled = false;
              btn.textContent = "▶ RUN NOW";
            }
          }, 5000);
        })
        .catch(() => {
          if (btn) {
            btn.disabled = false;
            btn.textContent = "▶ RUN NOW";
          }
        });
    });
  }

  // -------------------------------------------------------------------------
  // Colour-scheme change: rebuild chart with fresh token values
  // -------------------------------------------------------------------------
  function initColorSchemeWatcher() {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (lastData !== null) {
        initChart(lastData, lastWarn, lastCrit);
      }
    });
  }

  // -------------------------------------------------------------------------
  // Init
  // -------------------------------------------------------------------------
  function init() {
    initRangeButtons();
    initRunNow();
    initColorSchemeWatcher();
    if (document.getElementById("levelChart")) {
      loadChart("24h");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();


// Stop-siren button on the overview: POST, show the result on the button itself.
(function () {
  const form = document.getElementById("siren-stop-form");
  if (!form) return;
  const label = "◆ STOP SIREN";
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    if (btn) { btn.disabled = true; btn.textContent = "STOPPING…"; }
    fetch("/siren/stop", { method: "POST", body: new FormData(form), headers: { Accept: "application/json" } })
      .then((r) => r.json())
      .then((d) => {
        if (btn) btn.textContent = d.ok ? "✓ SIREN STOPPED" : "✗ STOP FAILED";
        if (btn && !d.ok && d.error) btn.title = d.error;
      })
      .catch(() => { if (btn) btn.textContent = "✗ STOP FAILED"; })
      .finally(() => {
        setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = label; } }, 5000);
      });
  });
})();
