/**
 * rain_radar.js — Rain Around dashboard tile
 * Requires Leaflet 1.9.4 vendored at /static/vendor/leaflet/
 * Loaded only when site_lat and site_lon are present on the tile element.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Constants
  // ---------------------------------------------------------------------------
  var REFRESH_MS = 10 * 60 * 1000; // 10 minutes

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------
  function getCSSVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function fmtTime(isoStr) {
    if (!isoStr) return "";
    try {
      var d = new Date(isoStr);
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (e) {
      return isoStr;
    }
  }

  // sqrt scale for forecast cell opacity; saturates at 5 mm/h
  function forecastOpacity(mm) {
    if (!mm || mm <= 0) return 0;
    return 0.15 + 0.70 * Math.min(1, Math.sqrt(mm / 5));
  }

  // ---------------------------------------------------------------------------
  // Entry point
  // ---------------------------------------------------------------------------
  var tile = document.getElementById("rain-around-tile");
  if (!tile) return;

  var siteLat = parseFloat(tile.dataset.lat);
  var siteLon = parseFloat(tile.dataset.lon);
  if (isNaN(siteLat) || isNaN(siteLon)) return;

  // State
  var currentRadius = 25;
  var currentMode = "radar"; // "radar" | "1" | "2" | "3"
  var map = null;
  var siteMarker = null;
  var siteCircle = null;
  var radarLayers = [];
  var radarFrames = [];
  var frameIndex = 0;
  var animTimer = null;
  var radarHost = "";
  var forecastData = null;
  var forecastLayers = [];

  // ---------------------------------------------------------------------------
  // Fix Leaflet default icon paths (vendored assets)
  // ---------------------------------------------------------------------------
  if (typeof L !== "undefined") {
    delete L.Icon.Default.prototype._getIconUrl;
    L.Icon.Default.mergeOptions({
      iconUrl: "/static/vendor/leaflet/images/marker-icon.png",
      iconRetinaUrl: "/static/vendor/leaflet/images/marker-icon-2x.png",
      shadowUrl: "/static/vendor/leaflet/images/marker-shadow.png",
    });
  }

  // ---------------------------------------------------------------------------
  // Map initialisation
  // ---------------------------------------------------------------------------
  function initMap() {
    if (typeof L === "undefined") return;

    map = L.map("rain-map", {
      center: [siteLat, siteLon],
      zoom: 9,
      maxZoom: 12,
      zoomControl: true,
    });

    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' +
        ' | Forecast &copy; <a href="https://open-meteo.com/">Open-Meteo</a>',
      maxZoom: 12,
    }).addTo(map);

    var rainColor = getCSSVar("--color-chart-rain") || "#5589bb";

    siteMarker = L.marker([siteLat, siteLon]).addTo(map);
    siteMarker.bindTooltip("Site");

    siteCircle = L.circle([siteLat, siteLon], {
      radius: currentRadius * 1000,
      color: rainColor,
      fillColor: rainColor,
      fillOpacity: 0.05,
      weight: 2,
    }).addTo(map);

    map.fitBounds(siteCircle.getBounds());
  }

  // ---------------------------------------------------------------------------
  // Radar animation
  // ---------------------------------------------------------------------------
  function fetchRadarFrames() {
    fetch("https://api.rainviewer.com/public/weather-maps.json")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        radarHost = data.host || "https://tilecache.rainviewer.com";
        var past = (data.radar && data.radar.past) ? data.radar.past : [];
        if (!past.length) throw new Error("No radar frames");

        clearRadarLayers();
        radarFrames = past;

        past.forEach(function (frame) {
          var layer = L.tileLayer(
            radarHost + frame.path + "/256/{z}/{x}/{y}/2/1_1.png",
            {
              maxNativeZoom: 7,
              maxZoom: 12,
              opacity: 0,
              attribution: 'Radar &copy; <a href="https://www.rainviewer.com/">RainViewer</a>',
              tileSize: 256,
            }
          );
          // Only attach to map immediately in radar mode; showRadarMode adds them otherwise
          if (map && currentMode === "radar") layer.addTo(map);
          radarLayers.push(layer);
        });

        if (currentMode === "radar") {
          frameIndex = 0;
          if (animTimer) clearTimeout(animTimer);
          animateFrame();
        }
      })
      .catch(function (e) {
        console.warn("RainViewer fetch failed:", e);
        var el = document.getElementById("rain-radar-status");
        if (el) el.textContent = "Radar unavailable";
      });
  }

  function clearRadarLayers() {
    if (animTimer) { clearTimeout(animTimer); animTimer = null; }
    radarLayers.forEach(function (l) { if (map) map.removeLayer(l); });
    radarLayers = [];
    radarFrames = [];
    frameIndex = 0;
  }

  function animateFrame() {
    // Hide all frames, then show the current one
    radarLayers.forEach(function (l) { l.setOpacity(0); });
    var currentLayer = radarLayers[frameIndex];
    if (currentLayer) currentLayer.setOpacity(0.6);

    // Update frame-time footer label
    var frameData = radarFrames[frameIndex];
    var timeEl = document.getElementById("rain-frame-time");
    if (timeEl && frameData) {
      var d = new Date(frameData.time * 1000);
      timeEl.textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    var isLast = frameIndex === radarLayers.length - 1;
    var delay = isLast ? 1500 : 600;
    frameIndex = (frameIndex + 1) % (radarLayers.length || 1);
    animTimer = setTimeout(animateFrame, delay);
  }

  // ---------------------------------------------------------------------------
  // Forecast grid
  // ---------------------------------------------------------------------------
  function loadForecast() {
    fetch("/api/rain-forecast?radius=" + currentRadius)
      .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || r.statusText); });
        return r.json();
      })
      .then(function (data) {
        // Discard a stale response for a radius the user has switched away from
        if (data.radius_km !== undefined && data.radius_km !== currentRadius) return;
        forecastData = data;
        updateTimeBtnAvailability(data);
        if (currentMode !== "radar") {
          drawForecastGrid(data, parseInt(currentMode, 10) - 1);
        }
      })
      .catch(function (e) {
        console.warn("Rain-forecast API error:", e);
        forecastData = null;
        if (currentMode !== "radar") {
          clearForecastGrid();
          setFooterText("Forecast unavailable");
        }
      });
  }

  // Enable/disable +N buttons based on how many forecast hours the API returned
  function updateTimeBtnAvailability(data) {
    var hours = (data && data.enabled && data.hours) ? data.hours : [];
    var timeBtns = tile.querySelectorAll(".rain-time-btn");
    timeBtns.forEach(function (btn) {
      var mode = btn.dataset.mode;
      if (mode === "radar") return;
      btn.disabled = parseInt(mode, 10) - 1 >= hours.length;
    });
  }

  function clearForecastGrid() {
    forecastLayers.forEach(function (l) { if (map) map.removeLayer(l); });
    forecastLayers = [];
  }

  function drawForecastGrid(data, hourIndex) {
    clearForecastGrid();
    if (!map || !data || !data.enabled || !data.cells || !data.hours) return;
    if (hourIndex >= data.hours.length) return;

    var rainColor = getCSSVar("--color-chart-rain") || "#5589bb";
    var stepKm = data.step_km || 12.5;

    // Build window label (hours[i] is the END of the one-hour bucket)
    var endDate = new Date(data.hours[hourIndex]);
    var startDate = new Date(endDate.getTime() - 3600000);
    var tOpts = { hour: "2-digit", minute: "2-digit" };
    var windowLabel =
      startDate.toLocaleTimeString([], tOpts) + "–" + endDate.toLocaleTimeString([], tOpts);

    // Half-cell size in degrees
    var latRad = Math.abs(siteLat * Math.PI / 180);
    var halfLat = stepKm / 111.32 / 2;
    var halfLon = stepKm / (111.32 * Math.cos(latRad)) / 2;

    data.cells.forEach(function (cell) {
      var mm = (cell.mm && cell.mm[hourIndex] !== undefined) ? cell.mm[hourIndex] : 0;
      if (!mm || mm <= 0) return;

      var rect = L.rectangle(
        [
          [cell.lat - halfLat, cell.lon - halfLon],
          [cell.lat + halfLat, cell.lon + halfLon],
        ],
        {
          color: "transparent",
          weight: 0,
          fillColor: rainColor,
          fillOpacity: forecastOpacity(mm),
          interactive: true,
        }
      ).addTo(map);

      rect.bindTooltip(windowLabel + " · " + mm.toFixed(1) + " mm");
      forecastLayers.push(rect);
    });

    var legendEl = document.getElementById("rain-forecast-legend");
    if (legendEl) legendEl.hidden = false;

    setFooterText("Forecast " + windowLabel + " · Open-Meteo model");
  }

  function setFooterText(text) {
    var timeEl = document.getElementById("rain-frame-time");
    if (timeEl) timeEl.textContent = text;
  }

  // ---------------------------------------------------------------------------
  // Mode switching
  // ---------------------------------------------------------------------------
  function showRadarMode() {
    // Remove forecast grid and hide legend
    clearForecastGrid();
    var legendEl = document.getElementById("rain-forecast-legend");
    if (legendEl) legendEl.hidden = true;

    // Re-attach radar layers to map and resume animation
    radarLayers.forEach(function (l) { if (map) l.addTo(map); });
    frameIndex = 0;
    if (animTimer) clearTimeout(animTimer);
    if (radarLayers.length) animateFrame();
  }

  function showForecastMode(hourIndex) {
    // Pause animation and detach radar layers
    if (animTimer) { clearTimeout(animTimer); animTimer = null; }
    radarLayers.forEach(function (l) { if (map) map.removeLayer(l); });
    setFooterText("");

    if (forecastData) {
      drawForecastGrid(forecastData, hourIndex);
    } else {
      loadForecast();
    }
  }

  // ---------------------------------------------------------------------------
  // Time selector buttons
  // ---------------------------------------------------------------------------
  var timeBtns = tile.querySelectorAll(".rain-time-btn");
  timeBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (btn.disabled) return;
      timeBtns.forEach(function (b) { b.classList.remove("active"); });
      btn.classList.add("active");
      currentMode = btn.dataset.mode;

      if (currentMode === "radar") {
        showRadarMode();
      } else {
        showForecastMode(parseInt(currentMode, 10) - 1);
      }
    });
  });

  // ---------------------------------------------------------------------------
  // Radius buttons (scoped to .rain-radius-btn — avoid .range-btn to stay
  // independent of the dashboard.js global handler)
  // ---------------------------------------------------------------------------
  var radiusBtns = tile.querySelectorAll(".rain-radius-btn");
  radiusBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      radiusBtns.forEach(function (b) { b.classList.remove("active"); });
      this.classList.add("active");
      currentRadius = parseInt(this.dataset.radius, 10);

      // Update the radius circle and re-fit the map view
      if (siteCircle) {
        var rainColor = getCSSVar("--color-chart-rain") || "#5589bb";
        siteCircle.setRadius(currentRadius * 1000);
        siteCircle.setStyle({ color: rainColor, fillColor: rainColor });
        if (map) map.fitBounds(siteCircle.getBounds());
      }

      // In forecast mode: invalidate cached data and refetch for new radius
      if (currentMode !== "radar") {
        forecastData = null;
        clearForecastGrid();
        loadForecast();
      }
    });
  });

  // ---------------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------------
  initMap();
  fetchRadarFrames();
  loadForecast();

  setInterval(fetchRadarFrames, REFRESH_MS);
  setInterval(loadForecast, REFRESH_MS);
})();
