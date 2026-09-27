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
  // Tooltip interaction: group datasets by time, not array index
  // -------------------------------------------------------------------------
  const HALF_HOUR_MS = 30 * 60 * 1000;
  // Max gap for a line point to share a tooltip with the anchor; set per
  // chart from the reading spacing in initChart.
  let lineToleranceMs = 10 * 60 * 1000;

  // Built-in "index" mode pairs the Nth element of every dataset, which is
  // wrong here: rain is hourly, readings are ~15 min apart. This mode picks
  // the element nearest the cursor along x, then adds from each other
  // dataset the element nearest that time, if close enough.
  function sameTimeMode(chart, e, options, useFinalPosition) {
    const pos = Chart.helpers.getRelativePosition(e, chart);
    const xScale = chart.scales.x;
    const metas = chart.getSortedVisibleDatasetMetas()
      .filter((m) => !chart.data.datasets[m.index].skipTooltip);

    let anchor = null;
    let best = Infinity;
    metas.forEach((meta) => {
      meta.data.forEach((el, index) => {
        const { x } = el.getProps(["x"], useFinalPosition);
        const d = Math.abs(x - pos.x);
        if (d < best) {
          best = d;
          anchor = { element: el, datasetIndex: meta.index, index, meta };
        }
      });
    });
    if (!anchor) return [];

    const anchorIsBar = anchor.meta.type === "bar";
    const t = xScale.getValueForPixel(anchor.element.getProps(["x"], useFinalPosition).x);
    const items = [anchor];
    metas.forEach((meta) => {
      if (meta.index === anchor.datasetIndex) return;
      const isBar = meta.type === "bar";
      // A bar covers its hour; line points must be near the anchor time
      // (or inside the anchor bar's hour).
      const tol = isBar || anchorIsBar ? HALF_HOUR_MS : lineToleranceMs;
      let pick = null;
      let pickD = Infinity;
      meta.data.forEach((el, index) => {
        const elT = xScale.getValueForPixel(el.getProps(["x"], useFinalPosition).x);
        const d = Math.abs(elT - t);
        if (d <= tol && d < pickD) {
          pickD = d;
          pick = { element: el, datasetIndex: meta.index, index };
        }
      });
      if (pick) items.push(pick);
    });
    return items;
  }

  if (typeof Chart !== "undefined") {
    Chart.Interaction.modes.sameTime = sameTimeMode;
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

    // Rainfall dataset. hour_ts is the hour's start; bars are centred on x,
    // so plot at the half-hour to make each bar span its own hour.
    const rainfallData = weather.map((w) => ({
      x: new Date(w.hour_ts).getTime() + HALF_HOUR_MS,
      y: w.precipitation_mm || 0,
      hour: w.hour_ts,
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

    // Half the median reading gap, so a tooltip never borrows a point from
    // a neighbouring reading.
    const gaps = xs.slice(1).map((t, i) => t - xs[i]).filter((g) => g > 0).sort((a, b) => a - b);
    lineToleranceMs = gaps.length ? gaps[Math.floor(gaps.length / 2)] / 2 : 10 * 60 * 1000;
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
          skipTooltip: true,
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
        interaction: { mode: "sameTime", intersect: false },
        plugins: {
          legend: {
            display: true,
            labels: { color: mutedColor, font: { size: 11, family: bodyFont } },
          },
          tooltip: {
            callbacks: {
              // items[0] is the anchor the sameTime mode picked.
              title: function (items) {
                const fmt = {
                  month: "short",
                  day: "numeric",
                  hour: "2-digit",
                  minute: "2-digit",
                  hour12: false,
                };
                const raw = items[0].raw;
                if (raw && raw.hour) {
                  const start = new Date(raw.hour);
                  const end = new Date(start.getTime() + 2 * HALF_HOUR_MS);
                  const endStr = end.toLocaleTimeString(undefined, {
                    hour: "2-digit",
                    minute: "2-digit",
                    hour12: false,
                  });
                  return `${start.toLocaleString(undefined, fmt)}–${endStr}`;
                }
                return new Date(items[0].parsed.x).toLocaleString(undefined, fmt);
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

// Mute-siren button on the overview.
(function () {
  const form = document.getElementById("siren-mute-form");
  if (!form) return;
  const label = "◆ MUTE UNTIL WATER DROPS";
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    if (btn) { btn.disabled = true; btn.textContent = "MUTING…"; }
    fetch("/siren/mute", { method: "POST", body: new FormData(form), headers: { Accept: "application/json" } })
      .then((r) => r.json())
      .then((d) => {
        if (btn) btn.textContent = d.muted ? "✓ MUTED" : "✗ FAILED";
        if (btn && d.error) btn.title = d.error;
        setTimeout(() => { location.reload(); }, 1500);
      })
      .catch(() => {
        if (btn) btn.textContent = "✗ FAILED";
        setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = label; } }, 5000);
      });
  });
})();

// Unmute-siren button on the overview.
(function () {
  const form = document.getElementById("siren-unmute-form");
  if (!form) return;
  const label = "◆ UNMUTE SIREN";
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    if (btn) { btn.disabled = true; btn.textContent = "UNMUTING…"; }
    fetch("/siren/unmute", { method: "POST", body: new FormData(form), headers: { Accept: "application/json" } })
      .then((r) => r.json())
      .then((d) => {
        if (btn) btn.textContent = d.ok ? "✓ UNMUTED" : "✗ FAILED";
        setTimeout(() => { location.reload(); }, 1500);
      })
      .catch(() => {
        if (btn) btn.textContent = "✗ FAILED";
        setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = label; } }, 5000);
      });
  });
})();
