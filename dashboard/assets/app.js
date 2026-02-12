const DEFAULT_CONSTANTS = {
  MS_PER_SEC: 1000,
  SEC_PER_MIN: 60,
  SEC_PER_HOUR: 3600,
  MAP_WIDTH: 1000,
  MAP_HEIGHT: 500,
  WORLD_LON_MAX: 180,
  WORLD_LAT_MAX: 90,
  WORLD_LON_SPAN: 360,
  WORLD_LAT_SPAN: 180,
  GRID_LAT_START: -60,
  GRID_LAT_END: 60,
  GRID_LON_START: -150,
  GRID_LON_END: 150,
  GRID_STEP_DEG: 30,
  KM_PER_DEG_LAT: 111.0,
  REFRESH_MS_FALLBACK: 1000,
};

const {
  MS_PER_SEC,
  SEC_PER_MIN,
  SEC_PER_HOUR,
  MAP_WIDTH,
  MAP_HEIGHT,
  WORLD_LON_MAX,
  WORLD_LAT_MAX,
  WORLD_LON_SPAN,
  WORLD_LAT_SPAN,
  GRID_LAT_START,
  GRID_LAT_END,
  GRID_LON_START,
  GRID_LON_END,
  GRID_STEP_DEG,
  KM_PER_DEG_LAT,
  REFRESH_MS_FALLBACK,
} = Object.assign({}, DEFAULT_CONSTANTS, window.LATTICE_CONSTANTS || {});
const WORLD_LAND_PATH = window.LATTICE_WORLD_LAND_PATH || "";
const MAP_SCALE_MIN = 1.0;
const MAP_SCALE_MAX = 6.0;
const DEG_TO_RAD = Math.PI / 180.0;
let latestState = null;
let apiReady = false;
const driftHistory = [];
const DRIFT_HISTORY_MAX = 120;
let lastDriftTs = 0;
const mapTransform = { scale: 1.0, tx: 0.0, ty: 0.0 };
let mapDragging = false;
let mapDragStart = null;
let refreshPaused = false;
let calibPollTimer = null;

function getApi() {
  return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
}

function fmtMs(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "n/a";
  return `${v.toFixed(2)} ms`;
}

function fmtKm(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "n/a";
  return `${v.toFixed(1)} km`;
}

function fmtDate(ms) {
  if (!ms) return "n/a";
  const d = new Date(ms);
  return d.toLocaleString();
}

function fmtPct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "n/a";
  return `${v.toFixed(1)}%`;
}

function fmtAge(ms, nowMs) {
  if (!ms || !nowMs) return "n/a";
  const s = Math.max(0, (nowMs - ms) / MS_PER_SEC);
  if (s < SEC_PER_MIN) return `${Math.round(s)}s`;
  if (s < SEC_PER_HOUR) return `${Math.round(s / SEC_PER_MIN)}m`;
  return `${(s / SEC_PER_HOUR).toFixed(1)}h`;
}

function fmtDuration(ms) {
  if (!ms || Number.isNaN(ms)) return "n/a";
  const s = Math.max(0, ms / MS_PER_SEC);
  if (s < SEC_PER_MIN) return `${Math.round(s)}s`;
  if (s < SEC_PER_HOUR) return `${Math.round(s / SEC_PER_MIN)}m`;
  const hours = s / SEC_PER_HOUR;
  if (hours < 24) return `${hours.toFixed(1)}h`;
  return `${(hours / 24).toFixed(1)}d`;
}

function endpointsToCsv(endpoints) {
  const headers = ["id", "host", "port", "region", "lat", "lon"];
  const rows = [headers.join(",")];
  (endpoints || []).forEach((ep) => {
    rows.push([
      ep.id || "",
      ep.host || "",
      ep.port || "",
      ep.regionHint || ep.region || "",
      ep.lat ?? "",
      ep.lon ?? "",
    ].join(","));
  });
  return rows.join("\n");
}

function probePathsToCsv(paths) {
  if (!paths || paths.length === 0) {
    return "";
  }
  const headers = ["id", "bindInterface", "bindIp"];
  const rows = [headers.join(",")];
  paths.forEach((p) => {
    rows.push([
      p.id || "",
      p.bindInterface || "",
      p.bindIp || "",
    ].join(","));
  });
  return rows.join("\n");
}

function mapXY(lat, lon) {
  const x = ((lon + WORLD_LON_MAX) / WORLD_LON_SPAN) * MAP_WIDTH;
  const y = ((WORLD_LAT_MAX - lat) / WORLD_LAT_SPAN) * MAP_HEIGHT;
  return [x, y];
}

function kmToPxLat(km) {
  const deg = km / KM_PER_DEG_LAT;
  const [, y1] = mapXY(0 + deg, 0);
  const [, y0] = mapXY(0, 0);
  return Math.abs(y1 - y0);
}

function kmToPxLon(km, lat) {
  const cosLat = Math.cos(lat * DEG_TO_RAD);
  if (cosLat < 1e-6) return 0;
  const deg = km / (KM_PER_DEG_LAT * cosLat);
  const [x1] = mapXY(lat, 0 + deg);
  const [x0] = mapXY(lat, 0);
  return Math.abs(x1 - x0);
}

function ellipseSvg(cx, cy, lat, ellipse, className) {
  if (!ellipse) return "";
  const major = ellipse.majorKm || 0;
  const minor = ellipse.minorKm || 0;
  if (!major || !minor) return "";
  const rx = kmToPxLon(major, lat);
  const ry = kmToPxLat(minor);
  const angle = ellipse.angleDeg || 0;
  return `<ellipse class="${className}" cx="${cx}" cy="${cy}" rx="${rx}" ry="${ry}" transform="rotate(${angle} ${cx} ${cy})" />`;
}

function clearNode(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function pauseRefresh() {
  refreshPaused = true;
}

function resumeRefresh() {
  refreshPaused = false;
  refresh();
}

function setCalibStatus(text, busy = false) {
  const calibStatus = document.getElementById("calib-status");
  if (!calibStatus) return;
  calibStatus.textContent = text;
  if (busy) {
    calibStatus.classList.add("busy");
  } else {
    calibStatus.classList.remove("busy");
  }
}

function handleCalibrationResult(kind, result, error) {
  if (error) {
    setCalibStatus(error, false);
    return;
  }
  if (!result || result.ok === false) {
    setCalibStatus((result && result.error) || "Calibration failed", false);
    return;
  }
  if (kind === "generate") {
    setCalibStatus(`Saved (${result.count || 0} endpoints)`, false);
    const calibPathInput = document.getElementById("calib-path");
    if (calibPathInput && result.path) {
      calibPathInput.value = result.path;
    }
  } else if (kind === "load") {
    setCalibStatus("Loaded", false);
  } else if (kind === "clear") {
    setCalibStatus("Cleared", false);
  } else {
    setCalibStatus("Done", false);
  }
}

function startCalibrationPoll(kind) {
  if (calibPollTimer) {
    clearInterval(calibPollTimer);
    calibPollTimer = null;
  }
  const api = getApi();
  if (!api) return;
  pauseRefresh();
  setCalibStatus("Working…", true);
  calibPollTimer = setInterval(async () => {
    try {
      const status = await api.get_calibration_status();
      if (!status || status.running) return;
      clearInterval(calibPollTimer);
      calibPollTimer = null;
      handleCalibrationResult(status.kind || kind, status.result, status.error);
      resumeRefresh();
    } catch (err) {
      clearInterval(calibPollTimer);
      calibPollTimer = null;
      setCalibStatus("Calibration failed", false);
      resumeRefresh();
    }
  }, 600);
}

function mapTransformStr() {
  return `translate(${mapTransform.tx.toFixed(2)} ${mapTransform.ty.toFixed(2)}) scale(${mapTransform.scale.toFixed(3)})`;
}

function applyMapTransform() {
  const layer = document.getElementById("map-layer");
  if (layer) layer.setAttribute("transform", mapTransformStr());
}

function setupMapControls() {
  const svg = document.getElementById("map");
  if (!svg) return;

  svg.addEventListener(
    "wheel",
    (ev) => {
      ev.preventDefault();
      const rect = svg.getBoundingClientRect();
      const cx = ev.clientX - rect.left;
      const cy = ev.clientY - rect.top;
      const direction = ev.deltaY > 0 ? 0.9 : 1.1;
      const nextScale = Math.min(MAP_SCALE_MAX, Math.max(MAP_SCALE_MIN, mapTransform.scale * direction));
      if (nextScale === mapTransform.scale) return;
      const x = (cx - mapTransform.tx) / mapTransform.scale;
      const y = (cy - mapTransform.ty) / mapTransform.scale;
      mapTransform.scale = nextScale;
      mapTransform.tx = cx - x * nextScale;
      mapTransform.ty = cy - y * nextScale;
      applyMapTransform();
    },
    { passive: false }
  );

  svg.addEventListener("mousedown", (ev) => {
    if (ev.button !== 0) return;
    mapDragging = true;
    mapDragStart = { x: ev.clientX, y: ev.clientY, tx: mapTransform.tx, ty: mapTransform.ty };
    svg.classList.add("dragging");
  });

  window.addEventListener("mousemove", (ev) => {
    if (!mapDragging || !mapDragStart) return;
    mapTransform.tx = mapDragStart.tx + (ev.clientX - mapDragStart.x);
    mapTransform.ty = mapDragStart.ty + (ev.clientY - mapDragStart.y);
    applyMapTransform();
  });

  window.addEventListener("mouseup", () => {
    if (!mapDragging) return;
    mapDragging = false;
    mapDragStart = null;
    svg.classList.remove("dragging");
  });

  svg.addEventListener("mouseleave", () => {
    if (!mapDragging) return;
    mapDragging = false;
    mapDragStart = null;
    svg.classList.remove("dragging");
  });

  svg.addEventListener("dblclick", () => {
    mapTransform.scale = 1.0;
    mapTransform.tx = 0.0;
    mapTransform.ty = 0.0;
    applyMapTransform();
  });
}

function renderSparkline(values, width = 180, height = 48) {
  if (!values || values.length < 2) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 4;
  const w = width - pad * 2;
  const h = height - pad * 2;
  const step = w / (values.length - 1);
  const points = values.map((v, i) => {
    const x = pad + i * step;
    const y = pad + h - ((v - min) / span) * h;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });
  const midY = pad + h / 2;
  return `
    <svg class="sparkline" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <rect width="${width}" height="${height}" fill="#0b0b0b"></rect>
      <line x1="${pad}" y1="${midY}" x2="${width - pad}" y2="${midY}" stroke="#202020" stroke-width="1" />
      <polyline fill="none" stroke="#f5a623" stroke-width="1.5" points="${points.join(" ")}" />
    </svg>
  `;
}

function renderTableRows(tableBody, rows, cols, emptyText) {
  clearNode(tableBody);
  if (!rows || rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = cols;
    td.className = "table-empty";
    td.textContent = emptyText;
    tr.appendChild(td);
    tableBody.appendChild(tr);
    return;
  }
  rows.forEach((r) => {
    const tr = document.createElement("tr");
    r.forEach((cell) => {
      const td = document.createElement("td");
      td.innerHTML = cell;
      tr.appendChild(td);
    });
    tableBody.appendChild(tr);
  });
}

function drawMap(state) {
  const svg = document.getElementById("map");
  clearNode(svg);

  const land = WORLD_LAND_PATH
    ? `<path class="land" d="${WORLD_LAND_PATH}" />`
    : "";

  const gridLines = [];
  for (let lat = GRID_LAT_START; lat <= GRID_LAT_END; lat += GRID_STEP_DEG) {
    const [x1, y1] = mapXY(lat, -WORLD_LON_MAX);
    const [x2, y2] = mapXY(lat, WORLD_LON_MAX);
    gridLines.push(`<line class="grid" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" />`);
  }
  for (let lon = GRID_LON_START; lon <= GRID_LON_END; lon += GRID_STEP_DEG) {
    const [x1, y1] = mapXY(WORLD_LAT_MAX, lon);
    const [x2, y2] = mapXY(-WORLD_LAT_MAX, lon);
    gridLines.push(`<line class="grid" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" />`);
  }

  const points = [];
  const endpoints = state.endpoints || [];
  endpoints.forEach((ep) => {
    if (ep.lat === undefined || ep.lon === undefined) return;
    const [x, y] = mapXY(ep.lat, ep.lon);
    points.push(`<circle class="endpoint" cx="${x}" cy="${y}" r="4" />`);
  });

  let bandLoose = "";
  let bandTight = "";
  let region = "";
  let estPoint = "";
  if (state.estimate) {
    const { lat, lon, bandTight: tight, bandLoose: loose } = state.estimate;
    const [x, y] = mapXY(lat, lon);
    estPoint = `<circle class="estimate" cx="${x}" cy="${y}" r="5" />`;
    if (loose && loose.radiusKm) {
      const radiusDeg = loose.radiusKm / KM_PER_DEG_LAT;
      const [rx, ry] = mapXY(lat + radiusDeg, lon);
      const r = Math.abs(ry - y);
      bandLoose = loose.ellipse
        ? ellipseSvg(x, y, lat, loose.ellipse, "band band-loose band-ellipse")
        : `<circle class="band band-loose" cx="${x}" cy="${y}" r="${r}" />`;
      if (
        loose.minLat !== undefined &&
        loose.maxLat !== undefined &&
        loose.minLon !== undefined &&
        loose.maxLon !== undefined
      ) {
        const [x1, y1] = mapXY(loose.maxLat, loose.minLon);
        const [x2, y2] = mapXY(loose.minLat, loose.maxLon);
        const w = Math.max(0, x2 - x1);
        const h = Math.max(0, y2 - y1);
        region = `<rect class="region" x="${x1}" y="${y1}" width="${w}" height="${h}" />`;
      }
    }
    if (tight && tight.radiusKm) {
      const radiusDeg = tight.radiusKm / KM_PER_DEG_LAT;
      const [rx, ry] = mapXY(lat + radiusDeg, lon);
      const r = Math.abs(ry - y);
      bandTight = tight.ellipse
        ? ellipseSvg(x, y, lat, tight.ellipse, "band band-tight band-ellipse")
        : `<circle class="band band-tight" cx="${x}" cy="${y}" r="${r}" />`;
    }
  }

  let claimPoint = "";
  if (state.claim) {
    const [x, y] = mapXY(state.claim.lat, state.claim.lon);
    claimPoint = `<circle class="claim" cx="${x}" cy="${y}" r="4" />`;
  }

  const transform = mapTransformStr();
  svg.innerHTML = `
    <rect width="${MAP_WIDTH}" height="${MAP_HEIGHT}" fill="#0b0b0b" />
    <g id="map-layer" transform="${transform}">
      ${land}
      ${gridLines.join("")}
      ${region}
      ${bandLoose}
      ${bandTight}
      ${points.join("")}
      ${estPoint}
      ${claimPoint}
    </g>
  `;
}

function updateUI(state) {
  latestState = state;
  const logStatus = state.logStatus || {};
  let statusText = state.endpoints ? "Live" : "No data";
  if (logStatus.missing) statusText = "Waiting for log";
  if (logStatus.resetReason) statusText += ` (log ${logStatus.resetReason})`;
  if (logStatus.error) statusText = `Log error`;
  document.getElementById("status").textContent = statusText;
  document.getElementById("window").textContent = `Window: ${state.windowMinutes || 0}m`;
  document.getElementById("updated").textContent = `Last update: ${fmtDate(state.updatedAt)}`;

  const estimate = document.getElementById("estimate-main");
  if (!state.estimate) {
    estimate.innerHTML = "<span class=\"table-empty\">Insufficient data</span>";
  } else {
    const e = state.estimate;
    const bandTight = e.bandTight;
    const bandLoose = e.bandLoose;
    const baseline = state.autoBaseline;
    const session = state.session;
    const hygiene = state.hygiene || {};
    const missingCoords = hygiene.missingCoords || [];
    const missingRegion = hygiene.missingRegion || [];
    const dupHosts = hygiene.duplicateHosts || [];
    const calib = state.calibrationHealth;
    let hygieneHtml = "";
    if (missingCoords.length || missingRegion.length || dupHosts.length) {
      const items = [];
      if (missingCoords.length) {
        items.push(`missing coords: ${missingCoords.length}`);
      }
      if (missingRegion.length) {
        items.push(`missing region: ${missingRegion.length}`);
      }
      if (dupHosts.length) {
        items.push(`dup hosts: ${dupHosts.length}`);
      }
      hygieneHtml = `<div class="badge warn">hygiene ${items.join(" / ")}</div>`;
    } else {
      hygieneHtml = `<div class="badge">hygiene ok</div>`;
    }

    let calHtml = "";
    if (calib) {
      const age = fmtDuration(calib.ageMs);
      const drift = calib.drift || {};
      const driftMedian = drift.medianAbsMs;
      const driftMax = drift.maxAbsMs;
      const driftBadge = driftMedian !== undefined && driftMedian !== null
        ? `<div class="badge ${calib.driftWarn ? "warn" : ""}">drift med ${fmtMs(driftMedian)}</div>`
        : `<div class="badge">drift n/a</div>`;
      const driftMaxBadge = driftMax !== undefined && driftMax !== null
        ? `<div class="badge">drift max ${fmtMs(driftMax)}</div>`
        : "";
      calHtml = `
        <div class="badge">cal age ${age}</div>
        ${driftBadge}
        ${driftMaxBadge}
      `;
    }
    const baselineBadge = baseline
      ? baseline.complete
        ? `<div class="badge">baseline locked (${baseline.minutes}m)</div>`
        : `<div class="badge">baseline capturing</div>`
      : "";
    const sessionBadge = session
      ? `<div class="badge">session ${fmtDate(session.startMs)}</div>`
      : "";
    estimate.innerHTML = `
      <div class="badge accent">lat ${e.lat.toFixed(4)}</div>
      <div class="badge accent">lon ${e.lon.toFixed(4)}</div>
      <div class="badge">bias ${e.biasMs.toFixed(2)} ms</div>
      <div class="badge">sse ${e.sse.toFixed(2)}</div>
      <div class="badge">points ${e.points}</div>
      ${bandTight ? `<div class="badge">tight ${bandTight.radiusKm.toFixed(1)} km</div>` : ""}
      ${bandLoose ? `<div class="badge">loose ${bandLoose.radiusKm.toFixed(1)} km</div>` : ""}
      ${calHtml}
      ${baselineBadge}
      ${sessionBadge}
      ${hygieneHtml}
    `;
  }

  const detailsCal = document.getElementById("details-calibration");
  const detailsHygiene = document.getElementById("details-hygiene");
  const detailsLog = document.getElementById("details-log");
  const detailsChart = document.getElementById("details-drift-chart");

  if (detailsCal) {
    const calib = state.calibrationHealth;
    if (!calib) {
      detailsCal.innerHTML = `<div class="details-muted">No calibration loaded.</div>`;
    } else {
      const age = fmtDuration(calib.ageMs);
      const drift = calib.drift || {};
      const median = drift.medianAbsMs;
      const max = drift.maxAbsMs;
      const warn = calib.driftWarn;
      const worst = drift.worst || [];
      const worstList = worst.length
        ? `<ul class="details-list">${worst
            .map((w) => `<li>${w.id}: ${fmtMs(w.deltaMs)}</li>`)
            .join("")}</ul>`
        : `<div class="details-muted">No drift samples yet.</div>`;
      detailsCal.innerHTML = `
        <div>Age: ${age}</div>
        <div>Median drift: ${median !== undefined ? fmtMs(median) : "n/a"}</div>
        <div>Max drift: ${max !== undefined ? fmtMs(max) : "n/a"}</div>
        <div class="${warn ? "badge warn" : "badge"}">threshold ${fmtMs(
          calib.warnThresholdMs
        )}</div>
        <div class="details-title">Worst endpoints</div>
        ${worstList}
      `;
    }
  }

  if (detailsHygiene) {
    const hygiene = state.hygiene || {};
    const missingCoords = hygiene.missingCoords || [];
    const missingRegion = hygiene.missingRegion || [];
    const dupHosts = hygiene.duplicateHosts || [];
    if (!missingCoords.length && !missingRegion.length && !dupHosts.length) {
      detailsHygiene.innerHTML = `<div class="details-muted">All endpoints have coords and region; no duplicate hosts.</div>`;
    } else {
      const rows = [];
      if (missingCoords.length) rows.push(`<li>Missing coords: ${missingCoords.join(", ")}</li>`);
      if (missingRegion.length) rows.push(`<li>Missing region: ${missingRegion.join(", ")}</li>`);
      if (dupHosts.length) {
        rows.push(
          `<li>Duplicate hosts: ${dupHosts
            .map((d) => `${d.host} (${d.ids.join(", ")})`)
            .join("; ")}</li>`
        );
      }
      detailsHygiene.innerHTML = `<ul class="details-list">${rows.join("")}</ul>`;
    }
  }

  if (detailsLog) {
    if (logStatus.error) {
      detailsLog.innerHTML = `<div class="details-muted">Error: ${logStatus.error}</div>`;
    } else if (logStatus.missing) {
      detailsLog.innerHTML = `<div class="details-muted">Log not found: ${logStatus.path}</div>`;
    } else {
      const reset = logStatus.resetReason
        ? `Reset: ${logStatus.resetReason} (${fmtAge(logStatus.resetAtMs, state.updatedAt)} ago)`
        : "No resets";
      detailsLog.innerHTML = `
        <div>Path: ${logStatus.path}</div>
        <div>${reset}</div>
      `;
    }
  }

  if (detailsChart) {
    const calib = state.calibrationHealth;
    const drift = calib ? calib.drift : null;
    if (drift && drift.medianAbsMs !== undefined && drift.medianAbsMs !== null) {
      if (state.updatedAt && state.updatedAt !== lastDriftTs) {
        driftHistory.push({ t: state.updatedAt, v: drift.medianAbsMs });
        lastDriftTs = state.updatedAt;
        if (driftHistory.length > DRIFT_HISTORY_MAX) driftHistory.shift();
      }
    }
    if (driftHistory.length < 2) {
      detailsChart.innerHTML = `<div class="details-muted">Drift chart needs more samples.</div>`;
    } else {
      const values = driftHistory.map((d) => d.v);
      detailsChart.innerHTML = renderSparkline(values);
    }
  }

  const statsBody = document.querySelector("#stats tbody");
  const healthById = new Map((state.health || []).map((h) => [h.id, h]));
  const statsRows = (state.endpoints || []).map((r) => [
    r.id,
    fmtMs(r.p05Ms),
    fmtMs(r.p50Ms),
    fmtMs(r.p95Ms),
    fmtMs(r.jitterMs),
    fmtKm(r.maxDistKmTight),
    fmtKm(r.maxDistKmLoose),
    (() => {
      const h = healthById.get(r.id);
      if (!h) return "n/a";
      if (h.expectedSamples) return `${h.sampleCount}/${h.expectedSamples}`;
      return `${h.sampleCount}`;
    })(),
    (() => {
      const h = healthById.get(r.id);
      return h ? fmtPct(h.lossPct) : "n/a";
    })(),
    (() => {
      const h = healthById.get(r.id);
      return h ? fmtAge(h.lastSeenMs, state.updatedAt) : "n/a";
    })(),
  ]);
  renderTableRows(statsBody, statsRows, 10, "No endpoint data yet");

  const claimsBody = document.querySelector("#claims tbody");
  const claimRows = (state.claimChecks || []).map((c) => [
    c.id,
    fmtKm(c.distKm),
    fmtKm(c.maxTightKm),
    fmtKm(c.maxLooseKm),
    `${c.falsifyTight ? "tight" : ""} ${c.falsifyLoose ? "loose" : ""}`.trim(),
  ]);
  renderTableRows(claimsBody, claimRows, 5, "No claim checks");

  const deltasBody = document.querySelector("#deltas tbody");
  const deltas = state.baseline ? state.baseline.deltas || [] : [];
  const deltaRows = deltas.map((d) => [
    d.id,
    fmtMs(d.deltaP05Ms),
    fmtMs(d.baselineP05Ms),
    fmtMs(d.sessionP05Ms),
  ]);
  renderTableRows(deltasBody, deltaRows, 4, "No baseline data");

  const legend = document.getElementById("map-legend");
  legend.innerHTML = `
    <span class="badge">endpoints</span>
    <span class="badge accent">estimate</span>
    <span class="badge">claim</span>
    <span class="badge">tight band</span>
    <span class="badge">loose band</span>
    <span class="badge">region</span>
  `;

  const calibMeta = document.getElementById("calib-meta");
  const calibPathInput = document.getElementById("calib-path");
  if (calibMeta) {
    if (state.calibration) {
      const c = state.calibration;
      const loc =
        c.calibrationLat !== undefined && c.calibrationLon !== undefined
          ? `@ ${c.calibrationLat.toFixed(4)}, ${c.calibrationLon.toFixed(4)}`
          : "";
      const samples = c.sampleCount ? ` • ${c.sampleCount} samples` : "";
      calibMeta.textContent = `calibration: ${c.count || 0} endpoints${samples} ${loc}`;
      if (calibPathInput && !calibPathInput.value && c.path) {
        calibPathInput.value = c.path;
      }
    } else {
      calibMeta.textContent = "calibration: none";
    }
  }

  const startBtn = document.getElementById("client-start");
  const stopBtn = document.getElementById("client-stop");
  const serverStart = document.getElementById("server-start");
  const serverStop = document.getElementById("server-stop");
  const statusEl = document.getElementById("endpoints-status");
  if (state.client && startBtn && stopBtn) {
    startBtn.disabled = state.client.running;
    stopBtn.disabled = !state.client.running;
    if (statusEl) {
      statusEl.textContent = state.client.running
        ? `client running (pid ${state.client.pid})`
        : "client stopped";
    }
  }
  if (state.server && serverStart && serverStop) {
    serverStart.disabled = state.server.running;
    serverStop.disabled = !state.server.running;
    if (statusEl) {
      statusEl.textContent += state.server.running
        ? ` • server running (pid ${state.server.pid})`
        : " • server stopped";
    }
  }

  if (state.claimChecks && state.claimChecks.length) {
    const first = state.claimChecks[0];
    state.claim = state.claim || {};
  }

  drawMap(state);
}

async function refresh() {
  if (refreshPaused) return;
  const api = getApi();
  if (!api) return;
  try {
    const state = await api.get_state();
    updateUI(state);
  } catch (err) {
    document.getElementById("status").textContent = "Error";
  }
}

async function loadEndpointsIntoBox() {
  const api = getApi();
  const endpointsInput = document.getElementById("endpoints-input");
  const probePathsInput = document.getElementById("probe-paths-input");
  const endpointErrors = document.getElementById("endpoint-errors");
  const probePathErrors = document.getElementById("probe-paths-errors");
  if (!api || !endpointsInput) return;
  try {
    const cfg = await api.get_config();
    endpointsInput.value = endpointsToCsv(cfg.endpoints || []);
    if (probePathsInput) {
      probePathsInput.value = probePathsToCsv(cfg.probePaths || []);
    }
  } catch (err) {
    if (endpointErrors) endpointErrors.textContent = "Failed to load endpoints";
    if (probePathErrors) probePathErrors.textContent = "Failed to load probe paths";
  }
}

function setupActions() {
  const exportBtn = document.getElementById("export-btn");
  const sessionBtn = document.getElementById("session-btn");
  const statusEl = document.getElementById("export-status");
  const endpointsInput = document.getElementById("endpoints-input");
  const endpointsLoad = document.getElementById("endpoints-load");
  const endpointsApply = document.getElementById("endpoints-apply");
  const probeTemplate = document.getElementById("probe-template");
  const endpointsStatus = document.getElementById("endpoints-status");
  const endpointErrors = document.getElementById("endpoint-errors");
  const probePathErrors = document.getElementById("probe-paths-errors");
  const clientStart = document.getElementById("client-start");
  const clientStop = document.getElementById("client-stop");
  const serverStart = document.getElementById("server-start");
  const serverStop = document.getElementById("server-stop");
  const clearBtn = document.getElementById("clear-btn");
  const calibLat = document.getElementById("calib-lat");
  const calibLon = document.getElementById("calib-lon");
  const calibPath = document.getElementById("calib-path");
  const calibGenerate = document.getElementById("calib-generate");
  const calibLoad = document.getElementById("calib-load");
  const calibClear = document.getElementById("calib-clear");
  const calibStatus = document.getElementById("calib-status");

  if (exportBtn) {
    exportBtn.addEventListener("click", async () => {
      if (!latestState) return;
      const api = getApi();
      if (!api) return;
      exportBtn.disabled = true;
      if (statusEl) statusEl.textContent = "Exporting…";
      try {
        const mapSvg = document.getElementById("map").outerHTML;
        const res = await api.export_state({
          state: latestState,
          mapSvg,
        });
        if (statusEl) statusEl.textContent = res.path ? `Exported: ${res.path}` : "Exported";
      } catch (err) {
        if (statusEl) statusEl.textContent = "Export failed";
      } finally {
        exportBtn.disabled = false;
      }
    });
  }

  if (sessionBtn) {
    sessionBtn.addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      sessionBtn.disabled = true;
      if (statusEl) statusEl.textContent = "Session marked";
      try {
        await api.mark_session();
      } catch (err) {
        if (statusEl) statusEl.textContent = "Session failed";
      } finally {
        sessionBtn.disabled = false;
      }
    });
  }

  if (endpointsLoad && endpointsInput) {
    endpointsLoad.addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      endpointsLoad.disabled = true;
      if (endpointsStatus) endpointsStatus.textContent = "Loading…";
      if (endpointErrors) endpointErrors.textContent = "";
      if (probePathErrors) probePathErrors.textContent = "";
      try {
        const cfg = await api.get_config();
        endpointsInput.value = endpointsToCsv(cfg.endpoints || []);
        const probePathsInput = document.getElementById("probe-paths-input");
        if (probePathsInput) {
          probePathsInput.value = probePathsToCsv(cfg.probePaths || []);
        }
        if (endpointsStatus) endpointsStatus.textContent = "Loaded";
      } catch (err) {
        if (endpointsStatus) endpointsStatus.textContent = "Load failed";
      } finally {
        endpointsLoad.disabled = false;
      }
    });
  }

  if (endpointsApply && endpointsInput) {
    endpointsApply.addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      endpointsApply.disabled = true;
      if (endpointsStatus) endpointsStatus.textContent = "Applying…";
      if (endpointErrors) endpointErrors.textContent = "";
      if (probePathErrors) probePathErrors.textContent = "";
      try {
        const probePathsInput = document.getElementById("probe-paths-input");
        const res = await api.set_config_parts({
          endpointsText: endpointsInput.value,
          probePathsText: probePathsInput ? probePathsInput.value : "",
        });
        if (res.ok) {
          const suffix = latestState?.client?.running ? " — restart client" : "";
          if (endpointsStatus) endpointsStatus.textContent = `Applied (${res.count})${suffix}`;
        } else {
          if (endpointsStatus) endpointsStatus.textContent = res.error || "Apply failed";
          if (endpointErrors && res.endpointErrors) {
            endpointErrors.textContent = (res.endpointErrors || []).join(" • ");
          }
          if (probePathErrors && res.probePathErrors) {
            probePathErrors.textContent = (res.probePathErrors || []).join(" • ");
          }
        }
      } catch (err) {
        if (endpointsStatus) endpointsStatus.textContent = "Apply failed";
      } finally {
        endpointsApply.disabled = false;
      }
    });
  }

  if (probeTemplate) {
    probeTemplate.addEventListener("click", () => {
      const probePathsInput = document.getElementById("probe-paths-input");
      if (!probePathsInput) return;
      probePathsInput.value = [
        "id,bindInterface,bindIp",
        "vpn,,",
        "direct,en0,",
      ].join("\n");
      if (endpointsStatus) {
        endpointsStatus.textContent = "Split template loaded (edit en0 if needed)";
      }
      if (probePathErrors) probePathErrors.textContent = "";
    });
  }

  if (clientStart) {
    clientStart.addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      clientStart.disabled = true;
      if (endpointsStatus) endpointsStatus.textContent = "Starting…";
      try {
        const res = await api.start_client();
        if (endpointsStatus) {
          endpointsStatus.textContent = res.error || "Client started";
        }
      } catch (err) {
        if (endpointsStatus) endpointsStatus.textContent = "Start failed";
      } finally {
        clientStart.disabled = false;
      }
    });
  }

  if (clientStop) {
    clientStop.addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      clientStop.disabled = true;
      if (endpointsStatus) endpointsStatus.textContent = "Stopping…";
      try {
        await api.stop_client();
        if (endpointsStatus) endpointsStatus.textContent = "Client stopped";
      } catch (err) {
        if (endpointsStatus) endpointsStatus.textContent = "Stop failed";
      } finally {
        clientStop.disabled = false;
      }
    });
  }

  if (serverStart) {
    serverStart.addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      serverStart.disabled = true;
      if (endpointsStatus) endpointsStatus.textContent = "Starting server…";
      if (endpointErrors) endpointErrors.textContent = "";
      try {
        const res = await api.start_server();
        if (endpointsStatus) {
          endpointsStatus.textContent = res.error || "Server started";
        }
      } catch (err) {
        if (endpointsStatus) endpointsStatus.textContent = "Server start failed";
      } finally {
        serverStart.disabled = false;
      }
    });
  }

  if (serverStop) {
    serverStop.addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      serverStop.disabled = true;
      if (endpointsStatus) endpointsStatus.textContent = "Stopping server…";
      if (endpointErrors) endpointErrors.textContent = "";
      try {
        await api.stop_server();
        if (endpointsStatus) endpointsStatus.textContent = "Server stopped";
      } catch (err) {
        if (endpointsStatus) endpointsStatus.textContent = "Server stop failed";
      } finally {
        serverStop.disabled = false;
      }
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      clearBtn.disabled = true;
      if (endpointsStatus) endpointsStatus.textContent = "Clearing…";
      if (endpointErrors) endpointErrors.textContent = "";
      try {
        await api.clear_state({ truncateLog: true });
        if (endpointsStatus) endpointsStatus.textContent = "Cleared";
      } catch (err) {
        if (endpointsStatus) endpointsStatus.textContent = "Clear failed";
      } finally {
        clearBtn.disabled = false;
      }
    });
  }

  if (calibGenerate) {
    calibGenerate.addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      const lat = parseFloat(calibLat ? calibLat.value.trim() : "");
      const lon = parseFloat(calibLon ? calibLon.value.trim() : "");
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        setCalibStatus("Enter valid lat/lon", false);
        return;
      }
      calibGenerate.disabled = true;
      try {
        const res = await api.generate_calibration({
          lat,
          lon,
          outputPath: calibPath ? calibPath.value.trim() : "",
        });
        if (res && res.ok && res.running) {
          startCalibrationPoll("generate");
        } else if (res && res.ok) {
          handleCalibrationResult("generate", res, null);
        } else {
          setCalibStatus((res && res.error) || "Generate failed", false);
        }
      } catch (err) {
        setCalibStatus("Generate failed", false);
      } finally {
        calibGenerate.disabled = false;
      }
    });
  }

  if (calibLoad) {
    calibLoad.addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      const path = calibPath ? calibPath.value.trim() : "";
      if (!path) {
        setCalibStatus("Enter calibration path", false);
        return;
      }
      calibLoad.disabled = true;
      try {
        const res = await api.load_calibration({ path });
        if (res && res.ok && res.running) {
          startCalibrationPoll("load");
        } else if (res && res.ok) {
          handleCalibrationResult("load", res, null);
        } else {
          setCalibStatus((res && res.error) || "Load failed", false);
        }
      } catch (err) {
        setCalibStatus("Load failed", false);
      } finally {
        calibLoad.disabled = false;
      }
    });
  }

  if (calibClear) {
    calibClear.addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      calibClear.disabled = true;
      try {
        const res = await api.clear_calibration();
        if (res && res.ok && res.running) {
          startCalibrationPoll("clear");
        } else if (res && res.ok) {
          handleCalibrationResult("clear", res, null);
        } else {
          setCalibStatus((res && res.error) || "Clear failed", false);
        }
      } catch (err) {
        setCalibStatus("Clear failed", false);
      } finally {
        calibClear.disabled = false;
      }
    });
  }

  const detailsToggle = document.getElementById("details-toggle");
  const detailsPanel = document.getElementById("details-panel");
  if (detailsToggle && detailsPanel) {
    detailsToggle.addEventListener("click", () => {
      detailsPanel.classList.toggle("hidden");
    });
  }
}

window.addEventListener("load", () => {
  const refreshMs = window.__REFRESH_MS__ || REFRESH_MS_FALLBACK;

  function init() {
    if (apiReady) return;
    apiReady = true;
    setupActions();
    setupMapControls();
    loadEndpointsIntoBox();
    refresh();
    setInterval(refresh, refreshMs);
  }

  if (getApi()) {
    init();
  } else {
    window.addEventListener("pywebviewready", init);
  }
});
