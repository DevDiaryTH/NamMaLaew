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
  var DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  var BEARINGS = { N: 0, NE: 45, E: 90, SE: 135, S: 180, SW: 225, W: 270, NW: 315 };
  var REFRESH_MS = 10 * 60 * 1000; // 10 minutes

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------
  function getCSSVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function toRad(deg) {
    return (deg * Math.PI) / 180;
  }

  // Convert meteorological bearing (0=N, clockwise) to SVG angle (0=E, clockwise)
  function svgAngleRad(bearing) {
    return toRad(bearing - 90);
  }

  // Square-root scale so light rain (0.2–1 mm/h, the common case) is still
  // visibly different from dry and from heavier rain; saturates at 2 mm/h.
  function precipOpacity(precipNow) {
    if (precipNow === null || precipNow === undefined || precipNow <= 0) return 0.06;
    return 0.2 + 0.75 * Math.min(1, Math.sqrt(precipNow / 2));
  }

  function fmtMM(v) {
    if (v === null || v === undefined) return "—";
    return v.toFixed(1) + " mm/h";
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
  var map = null;
  var siteMarker = null;
  var siteCircle = null;
  var dirMarkers = [];
  var radarLayers = [];
  var radarFrames = [];
  var frameIndex = 0;
  var animTimer = null;
  var radarHost = "";

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
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
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

        // Clear old layers
        clearRadarLayers();
        radarFrames = past;

        past.forEach(function (frame) {
          var layer = L.tileLayer(radarHost + frame.path + "/256/{z}/{x}/{y}/2/1_1.png", {
            maxNativeZoom: 7,
            maxZoom: 12,
            opacity: 0,
            attribution: 'Radar &copy; <a href="https://www.rainviewer.com/">RainViewer</a>',
            tileSize: 256,
          });
          if (map) layer.addTo(map);
          radarLayers.push(layer);
        });

        // Start animation
        frameIndex = 0;
        if (animTimer) clearTimeout(animTimer);
        animateFrame();
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
    // Hide all
    radarLayers.forEach(function (l) { l.setOpacity(0); });

    // Show current
    var currentLayer = radarLayers[frameIndex];
    if (currentLayer) currentLayer.setOpacity(0.6);

    // Update frame time label
    var frameFrame = radarFrames[frameIndex];
    var timeEl = document.getElementById("rain-frame-time");
    if (timeEl && frameFrame) {
      var d = new Date(frameFrame.time * 1000);
      timeEl.textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    var isLast = (frameIndex === radarLayers.length - 1);
    var delay = isLast ? 1500 : 600;

    frameIndex = (frameIndex + 1) % (radarLayers.length || 1);
    animTimer = setTimeout(animateFrame, delay);
  }

  // ---------------------------------------------------------------------------
  // API data fetch
  // ---------------------------------------------------------------------------
  function loadRainData() {
    fetch("/api/rain-around?radius=" + currentRadius)
      .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || r.statusText); });
        return r.json();
      })
      .then(function (data) {
        // A slower response for a radius the user has already switched away from.
        if (data.enabled && data.radius_km !== currentRadius) return;
        renderCompass(data);
        updateMapMarkers(data);
      })
      .catch(function (e) {
        console.warn("Rain-around API error:", e);
        showCompassError(e.message || "Error loading rain data");
      });
  }

  // ---------------------------------------------------------------------------
  // Compass rendering
  // ---------------------------------------------------------------------------
  var svgNS = "http://www.w3.org/2000/svg";
  var CX = 150, CY = 150, OUTER_R = 108, INNER_R = 28, LABEL_R = 128;

  function makeSVGEl(tag, attrs) {
    var el = document.createElementNS(svgNS, tag);
    Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    return el;
  }

  function wedgePath(bearingDeg) {
    var startRad = svgAngleRad(bearingDeg - 22.5);
    var endRad   = svgAngleRad(bearingDeg + 22.5);

    var ox1 = CX + OUTER_R * Math.cos(startRad);
    var oy1 = CY + OUTER_R * Math.sin(startRad);
    var ox2 = CX + OUTER_R * Math.cos(endRad);
    var oy2 = CY + OUTER_R * Math.sin(endRad);
    var ix1 = CX + INNER_R * Math.cos(startRad);
    var iy1 = CY + INNER_R * Math.sin(startRad);
    var ix2 = CX + INNER_R * Math.cos(endRad);
    var iy2 = CY + INNER_R * Math.sin(endRad);

    return [
      "M", ix1.toFixed(2), iy1.toFixed(2),
      "L", ox1.toFixed(2), oy1.toFixed(2),
      "A", OUTER_R, OUTER_R, "0 0 1", ox2.toFixed(2), oy2.toFixed(2),
      "L", ix2.toFixed(2), iy2.toFixed(2),
      "A", INNER_R, INNER_R, "0 0 0", ix1.toFixed(2), iy1.toFixed(2),
      "Z"
    ].join(" ");
  }

  function renderCompass(data) {
    var svg = document.getElementById("rain-compass-svg");
    if (!svg) return;

    // Clear previous content
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    var rainColor = getCSSVar("--color-chart-rain") || "#5589bb";
    var inkColor  = getCSSVar("--color-ink") || "#222";
    var mutedColor = getCSSVar("--color-muted") || "#666";
    var paperColor = getCSSVar("--color-paper") || "#efe";

    if (!data || !data.enabled) {
      // Show a simple "disabled" message inside SVG
      var msg = makeSVGEl("text", {
        x: CX, y: CY, "text-anchor": "middle", "dominant-baseline": "middle",
        fill: mutedColor, "font-size": "14", "font-family": "sans-serif"
      });
      msg.textContent = data && !data.enabled ? "Rain data disabled" : "No data";
      svg.appendChild(msg);

      updateCaption(data, null);
      return;
    }

    var center = data.center || {};
    var dirs = data.directions || [];

    // Build lookup by dir name
    var dirMap = {};
    dirs.forEach(function (d) { dirMap[d.dir] = d; });

    // Draw wedges
    DIRS.forEach(function (dirName) {
      var bearing = BEARINGS[dirName];
      var dirData = dirMap[dirName];
      var precip = dirData ? dirData.precip_now : 0;
      var opacity = precipOpacity(precip);

      var path = makeSVGEl("path", {
        d: wedgePath(bearing),
        fill: rainColor,
        "fill-opacity": opacity,
        stroke: inkColor,
        "stroke-width": "1",
        "stroke-opacity": "0.4",
      });

      // Tooltip
      if (dirData) {
        var title = document.createElementNS(svgNS, "title");
        title.textContent = dirName
          + ": now " + fmtMM(dirData.precip_now)
          + " · past 3h " + fmtMM(dirData.precip_past3h)
          + " · next 3h " + fmtMM(dirData.precip_next3h);
        path.appendChild(title);
      }

      svg.appendChild(path);
    });

    // Direction labels
    DIRS.forEach(function (dirName) {
      var bearing = BEARINGS[dirName];
      var midRad = svgAngleRad(bearing);
      var lx = CX + LABEL_R * Math.cos(midRad);
      var ly = CY + LABEL_R * Math.sin(midRad);

      var txt = makeSVGEl("text", {
        x: lx.toFixed(1), y: ly.toFixed(1),
        "text-anchor": "middle",
        "dominant-baseline": "middle",
        fill: inkColor,
        "font-size": "11",
        "font-weight": "700",
        "font-family": "sans-serif",
      });
      txt.textContent = dirName;
      svg.appendChild(txt);
    });

    // Center circle background
    var centerCircle = makeSVGEl("circle", {
      cx: CX, cy: CY, r: INNER_R - 2,
      fill: paperColor,
      stroke: inkColor,
      "stroke-width": "1",
      "stroke-opacity": "0.4",
    });
    svg.appendChild(centerCircle);

    // Center precip text
    var sitePrec = center.precip_now;
    var centerText = makeSVGEl("text", {
      x: CX, y: CY - 6,
      "text-anchor": "middle",
      "dominant-baseline": "middle",
      fill: inkColor,
      "font-size": "9",
      "font-weight": "700",
      "font-family": "sans-serif",
    });
    centerText.textContent = sitePrec !== null && sitePrec !== undefined
      ? sitePrec.toFixed(1) : "—";
    svg.appendChild(centerText);

    var centerUnit = makeSVGEl("text", {
      x: CX, y: CY + 7,
      "text-anchor": "middle",
      "dominant-baseline": "middle",
      fill: mutedColor,
      "font-size": "7",
      "font-family": "sans-serif",
    });
    centerUnit.textContent = "mm/h";
    svg.appendChild(centerUnit);

    // Wind arrow
    if (center.wind_from_deg !== null && center.wind_from_deg !== undefined) {
      drawWindArrow(svg, center.wind_from_deg, center.wind_kmh, rainColor, inkColor);
    }

    // Caption and timestamp
    updateCaption(data, center);
  }

  function drawWindArrow(svg, windFromDeg, windKmh, rainColor, inkColor) {
    // Arrow shows where wind comes FROM: tail at that direction, head toward center
    var arrowR = OUTER_R - 6; // slightly inside the outer ring
    var angleRad = svgAngleRad(windFromDeg);

    var tailX = CX + arrowR * Math.cos(angleRad);
    var tailY = CY + arrowR * Math.sin(angleRad);

    // Arrowhead is near center (at inner_R)
    var headX = CX + (INNER_R + 2) * Math.cos(angleRad);
    var headY = CY + (INNER_R + 2) * Math.sin(angleRad);

    var line = makeSVGEl("line", {
      x1: tailX.toFixed(1), y1: tailY.toFixed(1),
      x2: headX.toFixed(1), y2: headY.toFixed(1),
      stroke: inkColor,
      "stroke-width": "2",
      "stroke-linecap": "round",
      "marker-end": "url(#wind-arrow-head)",
    });

    // Arrowhead marker definition
    var defs = svg.querySelector("defs");
    if (!defs) {
      defs = makeSVGEl("defs", {});
      svg.insertBefore(defs, svg.firstChild);
    }
    var markerId = "wind-arrow-head";
    if (!document.getElementById(markerId)) {
      var marker = makeSVGEl("marker", {
        id: markerId,
        markerWidth: "6", markerHeight: "6",
        refX: "3", refY: "3",
        orient: "auto",
      });
      var arrowPoly = makeSVGEl("polygon", {
        points: "0 0, 6 3, 0 6",
        fill: inkColor,
      });
      marker.appendChild(arrowPoly);
      defs.appendChild(marker);
    }

    svg.appendChild(line);

    // Wind speed label near the tail
    if (windKmh !== null && windKmh !== undefined) {
      var lblR = arrowR + 12;
      var lblX = CX + lblR * Math.cos(angleRad);
      var lblY = CY + lblR * Math.sin(angleRad);
      var wTxt = makeSVGEl("text", {
        x: lblX.toFixed(1), y: lblY.toFixed(1),
        "text-anchor": "middle",
        "dominant-baseline": "middle",
        fill: getCSSVar("--color-muted") || "#666",
        "font-size": "8",
        "font-family": "sans-serif",
      });
      wTxt.textContent = Math.round(windKmh) + " km/h";
      svg.appendChild(wTxt);
    }
  }

  function updateCaption(data, center) {
    var capEl = document.getElementById("rain-compass-caption");
    var tsEl  = document.getElementById("rain-compass-fetched");

    if (capEl) {
      if (!data || !data.enabled) {
        capEl.textContent = "ไม่มีข้อมูลฝน · Rain data unavailable";
      } else {
        var rainFrom = data.rain_from || [];
        if (rainFrom.length >= 6) {
          // rain_from is sorted heaviest first
          var top = rainFrom.slice(0, 2).join(", ");
          capEl.textContent = "ฝนรอบทุกทิศ หนักสุดทาง " + top
            + " · Rain all around, heaviest " + top;
        } else if (rainFrom.length) {
          capEl.textContent = "ฝนมาจากทิศ " + rainFrom.join(", ")
            + " · Rain from " + rainFrom.join(", ");
        } else {
          capEl.textContent = "ไม่มีฝนในรัศมี " + data.radius_km + " กม."
            + " · No rain within " + data.radius_km + " km";
        }
      }
    }

    if (tsEl && data && data.fetched_at) {
      tsEl.textContent = "Updated " + fmtTime(data.fetched_at);
    } else if (tsEl) {
      tsEl.textContent = "";
    }
  }

  function showCompassError(msg) {
    var svg = document.getElementById("rain-compass-svg");
    if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    var mutedColor = getCSSVar("--color-muted") || "#666";
    var txt = makeSVGEl("text", {
      x: CX, y: CY, "text-anchor": "middle", "dominant-baseline": "middle",
      fill: mutedColor, "font-size": "12", "font-family": "sans-serif",
    });
    txt.textContent = msg;
    svg.appendChild(txt);

    var capEl = document.getElementById("rain-compass-caption");
    if (capEl) capEl.textContent = "";
  }

  // ---------------------------------------------------------------------------
  // Map markers update
  // ---------------------------------------------------------------------------
  function updateMapMarkers(data) {
    if (!map) return;

    var rainColor = getCSSVar("--color-chart-rain") || "#5589bb";

    // Remove old direction markers
    dirMarkers.forEach(function (m) { map.removeLayer(m); });
    dirMarkers = [];

    // Update site circle radius
    if (siteCircle) {
      siteCircle.setRadius(currentRadius * 1000);
      siteCircle.setStyle({ color: rainColor, fillColor: rainColor });
      map.fitBounds(siteCircle.getBounds());
    }

    if (!data || !data.enabled) return;

    var dirs = data.directions || [];
    dirs.forEach(function (d) {
      var precip = d.precip_now || 0;
      var opacity = precipOpacity(precip);
      var marker = L.circleMarker([d.lat, d.lon], {
        radius: 6,
        color: rainColor,
        fillColor: rainColor,
        fillOpacity: opacity,
        weight: 1,
      }).addTo(map);
      marker.bindTooltip(
        d.dir + ": now " + fmtMM(d.precip_now)
        + " · past 3h " + fmtMM(d.precip_past3h)
        + " · next 3h " + fmtMM(d.precip_next3h)
      );
      dirMarkers.push(marker);
    });
  }

  // ---------------------------------------------------------------------------
  // Radius buttons (scoped to .rain-radius-btn only)
  // ---------------------------------------------------------------------------
  var radiusBtns = tile.querySelectorAll(".rain-radius-btn");
  radiusBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      radiusBtns.forEach(function (b) { b.classList.remove("active"); });
      this.classList.add("active");
      currentRadius = parseInt(this.dataset.radius, 10);
      loadRainData();
    });
  });

  // ---------------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------------
  initMap();
  loadRainData();
  fetchRadarFrames();

  setInterval(loadRainData, REFRESH_MS);
  setInterval(fetchRadarFrames, REFRESH_MS);
})();
