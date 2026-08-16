const state = {
  dashboard: null,
  currentPage: "overview",
  previousPage: "workers",
  selectedDevice: null,
  charts: {},
  lastSeenIncidentId: null,
  technicalRows: [],
  technicalSensorRows: [],
  refreshTimer: null,
  connectionWasOffline: false,
  health: null,
  storage: null,
  verification: null,
  reviewIncidentId: null,
  selectedFeedback: null,
  healthLastLoaded: 0,
  notifications: null,
  notificationsLastLoaded: 0,
  telegramRecipients: [],
  thresholdLearning: null,
  thresholdLearningLastLoaded: 0,
  selectedActualEventType: null,
};

const pageMeta = {
  overview: ["SITE MONITORING", "Overview"],
  workers: ["PERSONNEL", "Workers"],
  incidents: ["SAFETY EVENTS", "Incidents"],
  analytics: ["SAFETY ANALYTICS", "Analytics"],
  thresholdLab: ["THRESHOLD OPTIMISATION", "Threshold Lab"],
  health: ["PIPELINE MONITORING", "System Health"],
  notifications: ["SAFETY COMMUNICATION", "Telegram Alerts"],
  technical: ["ENGINEERING VIEW", "Technical"],
  admin: ["SYSTEM MANAGEMENT", "Admin"],
  workerDetail: ["WORKER DETAIL", "Worker Dashboard"],
};

const activityMeta = {
  POSSIBLE_FFH: {
    label: "Possible FFH",
    tone: "urgent",
  },
  POSSIBLE_STF: {
    label: "Possible STF",
    tone: "urgent",
  },
  POSSIBLE_NEAR_MISS: {
    label: "Possible Near Miss",
    tone: "warning",
  },
  STANDING: {
    label: "Standing",
    tone: "normal",
  },
  WALKING: {
    label: "Walking",
    tone: "normal",
  },
  RUNNING: {
    label: "Running",
    tone: "info",
  },
  MOVING: {
    label: "Moving",
    tone: "info",
  },
  COLLECTING: {
    label: "Collecting Data",
    tone: "warning",
  },
  OFFLINE: {
    label: "Offline",
    tone: "warning",
  },
};

const $ = id =>
  document.getElementById(id);

const eventLinePlugin = {
  id: "eventLine",
  afterDraw(chart, args, options) {
    if (!options?.enabled || !chart.scales?.x) {
      return;
    }

    const x = chart.scales.x.getPixelForValue(0);
    const { top, bottom } = chart.chartArea;
    const ctx = chart.ctx;

    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.lineWidth = 2;
    ctx.setLineDash([5, 5]);
    ctx.strokeStyle = "#dc2626";
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#b42318";
    ctx.font = "700 11px system-ui";
    ctx.fillText("EVENT", Math.min(x + 6, chart.width - 48), top + 14);
    ctx.restore();
  },
};

if (window.Chart) {
  Chart.register(eventLinePlugin);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function numberText(value, digits = 2, suffix = "") {
  const number = Number(value);

  return Number.isFinite(number)
    ? `${number.toFixed(digits)}${suffix}`
    : "—";
}

function percentageText(value) {
  const number = Number(value);

  return Number.isFinite(number)
    ? `${Math.round(number)}%`
    : "—";
}

function dateTimeText(value) {
  if (!value) {
    return "—";
  }

  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return "—";
  }

  return date.toLocaleString();
}

function timeAgo(value) {
  if (!value) {
    return "No data";
  }

  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return "No data";
  }

  const seconds = Math.max(
    0,
    Math.round(
      (Date.now() - date.getTime()) / 1000
    ),
  );

  if (seconds < 60) {
    return `${seconds}s ago`;
  }

  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)}m ago`;
  }

  if (seconds < 86400) {
    return `${Math.floor(seconds / 3600)}h ago`;
  }

  return `${Math.floor(seconds / 86400)}d ago`;
}

function batteryTone(value) {
  const number = Number(value);

  if (!Number.isFinite(number)) {
    return "unknown";
  }

  if (number <= 20) {
    return "low";
  }

  if (number <= 40) {
    return "medium";
  }

  return "normal";
}

function batteryLabel(value) {
  const tone = batteryTone(value);

  if (tone === "low") {
    return "Low battery";
  }

  if (tone === "medium") {
    return "Battery warning";
  }

  if (tone === "normal") {
    return "Battery normal";
  }

  return "Battery unknown";
}

function isUrgentActivity(status) {
  return [
    "POSSIBLE_FFH",
    "POSSIBLE_STF",
  ].includes(status);
}

function isWarningActivity(status) {
  return status === "POSSIBLE_NEAR_MISS";
}

async function api(path, options = {}) {
  const response = await fetch(
    path,
    {
      cache: "no-store",
      ...options,
    },
  );

  if (!response.ok) {
    let message = `HTTP ${response.status}`;

    try {
      const body = await response.json();
      message = body.detail || message;
    } catch {}

    throw new Error(message);
  }

  return response.json();
}

function kpiCard(
  label,
  value,
  sub = "",
  tone = "",
) {
  return `
    <article class="kpi-card ${tone}">
      <span class="kpi-label">${escapeHtml(label)}</span>
      <strong class="kpi-value">${escapeHtml(value)}</strong>
      <span class="kpi-sub">${escapeHtml(sub)}</span>
    </article>
  `;
}

function emptyState(message) {
  return `
    <div class="empty-state">
      ${escapeHtml(message)}
    </div>
  `;
}

function showToast(title, message, tone = "") {
  const toast = document.createElement("div");
  toast.className = `toast ${tone}`;

  toast.innerHTML = `
    <strong>${escapeHtml(title)}</strong>
    <span>${escapeHtml(message)}</span>
  `;

  $("toastContainer").appendChild(toast);

  setTimeout(
    () => toast.remove(),
    5000,
  );
}

function setConnectionState(mode, text) {
  $("connectionPill").className =
    `connection-pill ${mode}`;

  $("connectionText").textContent = text;

  $("sidebarLiveDot").className =
    `live-dot ${
      mode === "live"
        ? "online"
        : mode === "offline"
          ? "error"
          : ""
    }`;

  $("sidebarSystemText").textContent =
    mode === "live"
      ? "System live"
      : mode === "offline"
        ? "Connection lost"
        : "Connecting";
}

function switchPage(pageName) {
  if (
    pageName !== "workerDetail"
    && state.currentPage !== "workerDetail"
  ) {
    state.previousPage = pageName;
  }

  state.currentPage = pageName;

  document
    .querySelectorAll(".page")
    .forEach(page =>
      page.classList.remove("active")
    );

  const element = $(
    pageName === "workerDetail"
      ? "workerDetailPage"
      : `${pageName}Page`
  );

  element?.classList.add("active");

  document
    .querySelectorAll(".nav-item")
    .forEach(button => {
      button.classList.toggle(
        "active",
        button.dataset.page === pageName,
      );
    });

  const [eyebrow, title] =
    pageMeta[pageName];

  $("pageEyebrow").textContent =
    eyebrow;

  $("pageTitle").textContent =
    title;

  closeSidebar();

  if (pageName === "analytics") {
    renderAnalytics();
  }

  if (pageName === "thresholdLab") {
    loadThresholdLab();
  }

  if (pageName === "health") {
    loadSystemHealth();
  }

  if (pageName === "notifications") {
    loadNotificationsPage(true);
  }

  if (pageName === "technical") {
    loadTechnicalPage();
  }
}

function openSidebar() {
  $("sidebar").classList.add("open");
  $("sidebarBackdrop").classList.add("show");
}

function closeSidebar() {
  $("sidebar").classList.remove("open");
  $("sidebarBackdrop").classList.remove("show");
}

function siteState() {
  const devices =
    state.dashboard?.devices || [];

  const urgent =
    devices.filter(device =>
      isUrgentActivity(device.activity_status)
    );

  const warning =
    devices.filter(device =>
      isWarningActivity(device.activity_status)
      || device.connection_status !== "Active"
      || batteryTone(device.battery_percent) === "low"
    );

  if (urgent.length) {
    return {
      tone: "urgent",
      icon: "!",
      title: "Immediate attention required",
      message:
        `${urgent.length} worker(s) have a possible fall event.`,
    };
  }

  if (warning.length) {
    return {
      tone: "warning",
      icon: "!",
      title: "Attention required",
      message:
        `${warning.length} worker/device warning(s) need review.`,
    };
  }

  return {
    tone: "normal",
    icon: "✓",
    title: "Site operating normally",
    message:
      "No urgent safety incidents detected.",
  };
}

function averageBattery() {
  const values =
    (state.dashboard?.devices || [])
      .map(device =>
        Number(device.battery_percent)
      )
      .filter(Number.isFinite);

  if (!values.length) {
    return null;
  }

  return values.reduce(
    (sum, value) => sum + value,
    0,
  ) / values.length;
}

function renderOverviewKpis() {
  const summary =
    state.dashboard?.summary || {};

  const average =
    averageBattery();

  $("overviewKpis").innerHTML = [
    kpiCard(
      "Registered workers",
      summary.registered_devices ?? 0,
      "Known Nesso devices",
    ),
    kpiCard(
      "Workers online",
      `${summary.workers_online ?? 0} / ${summary.registered_devices ?? 0}`,
      "Updated within the live window",
      (summary.workers_online ?? 0)
        === (summary.registered_devices ?? 0)
        && (summary.registered_devices ?? 0) > 0
        ? "success"
        : "warning",
    ),
    kpiCard(
      "Open incidents",
      summary.open_incidents ?? 0,
      "Not yet acknowledged",
      (summary.open_incidents ?? 0) > 0
        ? "danger"
        : "success",
    ),
    kpiCard(
      "Average battery",
      average == null
        ? "—"
        : `${Math.round(average)}%`,
      "Across devices reporting battery",
      average != null && average <= 25
        ? "danger"
        : average != null && average <= 45
          ? "warning"
          : "",
    ),
  ].join("");
}

function renderSiteStatus() {
  const status =
    siteState();

  const card =
    $("siteStatusCard");

  card.className =
    `site-status-card ${status.tone}`;

  card.querySelector(
    ".site-status-icon"
  ).textContent = status.icon;

  card.querySelector(
    ".site-status-copy h2"
  ).textContent = status.title;

  card.querySelector(
    ".site-status-copy p"
  ).textContent = status.message;

  const summary =
    state.dashboard?.summary || {};

  $("siteStatusMeta").innerHTML = `
    <div class="meta-row">
      <span>Last update</span>
      <strong>${escapeHtml(timeAgo(summary.last_live_update))}</strong>
    </div>

    <div class="meta-row">
      <span>Open incidents</span>
      <strong>${escapeHtml(summary.open_incidents ?? 0)}</strong>
    </div>

    <div class="meta-row">
      <span>Online devices</span>
      <strong>${escapeHtml(summary.workers_online ?? 0)}</strong>
    </div>
  `;
}

function workerCardHtml(device, compact = false) {
  const activity =
    activityMeta[device.activity_status]
    || {
      label: device.activity_status || "Unknown",
      tone: "warning",
    };

  const online =
    device.connection_status === "Active";

  const battery =
    Number(device.battery_percent);

  const batteryWidth =
    Number.isFinite(battery)
      ? Math.max(0, Math.min(100, battery))
      : 0;

  const batteryClass =
    batteryTone(battery);

  const cardTone =
    isUrgentActivity(device.activity_status)
      ? "urgent"
      : isWarningActivity(device.activity_status)
        || batteryClass === "low"
        ? "warning"
        : "";

  const offlineDuration =
    !online && device.last_seen
      ? `Offline for ${timeAgo(device.last_seen)}`
      : online
        ? `Updated ${timeAgo(device.last_seen)}`
        : "No live data";

  return `
    <article class="worker-card ${cardTone}">
      <div class="worker-top">
        <div>
          <h3 class="worker-name">
            ${escapeHtml(device.worker_id)}
          </h3>

          <div class="device-name">
            ${escapeHtml(device.device_name)}
          </div>
        </div>

        <span class="status-chip ${online ? "online" : "offline"}">
          ${online ? "ACTIVE" : "OFFLINE"}
        </span>
      </div>

      <div class="activity-block">
        <span class="activity-chip ${activity.tone}">
          ${escapeHtml(activity.label)}
        </span>

        <p class="worker-message">
          ${escapeHtml(device.status_message || "")}
        </p>
      </div>

      <div class="worker-stats">
        <div class="stat-tile">
          <span>Acceleration</span>
          <strong>
            ${numberText(device.latest_acceleration_g, 3, " g")}
          </strong>
        </div>

        <div class="stat-tile">
          <span>Gyroscope</span>
          <strong>
            ${numberText(device.latest_gyro_dps, 1, " °/s")}
          </strong>
        </div>

        <div class="stat-tile">
          <span>Battery</span>
          <strong>
            ${percentageText(device.battery_percent)}
          </strong>

          <div class="battery-bar">
            <div
              class="battery-fill ${batteryClass}"
              style="width:${batteryWidth}%"
            ></div>
          </div>
        </div>

        <div class="stat-tile">
          <span>Status</span>
          <strong>
            ${escapeHtml(offlineDuration)}
          </strong>
        </div>
      </div>

      <button
        class="primary-button"
        data-open-worker="${escapeHtml(device.device_name)}"
      >
        View Worker
      </button>
    </article>
  `;
}

function bindWorkerButtons(scope = document) {
  scope
    .querySelectorAll("[data-open-worker]")
    .forEach(button => {
      button.onclick = () =>
        openWorkerDetail(
          button.dataset.openWorker
        );
    });
}

function renderOverviewWorkers() {
  const devices =
    state.dashboard?.devices || [];

  const ordered =
    [...devices].sort(
      (a, b) => {
        const rank = status => {
          if (isUrgentActivity(status)) {
            return 0;
          }

          if (isWarningActivity(status)) {
            return 1;
          }

          return 2;
        };

        return (
          rank(a.activity_status)
          - rank(b.activity_status)
        );
      }
    );

  $("overviewWorkerGrid").innerHTML =
    ordered.length
      ? ordered
          .slice(0, 3)
          .map(device =>
            workerCardHtml(device, true)
          )
          .join("")
      : emptyState(
          "No worker devices have sent data yet."
        );

  bindWorkerButtons(
    $("overviewWorkerGrid")
  );
}

function renderLatestIncidents() {
  const incidents =
    state.dashboard?.incidents || [];

  $("overviewIncidentList").innerHTML =
    incidents.length
      ? incidents
          .slice(0, 5)
          .map(incident => `
            <div class="compact-row">
              <div class="compact-row-main">
                <strong>
                  ${escapeHtml(incident.worker_id)}
                  ·
                  ${escapeHtml(incident.event_type)}
                </strong>

                <span>
                  ${escapeHtml(incident.description)}
                </span>
              </div>

              <div class="compact-row-side">
                ${escapeHtml(timeAgo(incident.received_timestamp))}
                <br>
                ${
                  incident.acknowledged
                    ? "Reviewed"
                    : "Open"
                }
              </div>
            </div>
          `)
          .join("")
      : emptyState(
          "No safety incidents have been recorded."
        );
}

function renderDeviceHealth() {
  const devices =
    state.dashboard?.devices || [];

  $("deviceHealthList").innerHTML =
    devices.length
      ? devices
          .map(device => {
            const online =
              device.connection_status === "Active";

            return `
              <div class="compact-row">
                <div class="compact-row-main">
                  <strong>
                    ${escapeHtml(device.worker_id)}
                  </strong>

                  <span>
                    ${
                      online
                        ? "Connected"
                        : `Last seen ${timeAgo(device.last_seen)}`
                    }
                  </span>
                </div>

                <div class="compact-row-side">
                  ${escapeHtml(percentageText(device.battery_percent))}
                  <br>
                  ${escapeHtml(batteryLabel(device.battery_percent))}
                </div>
              </div>
            `;
          })
          .join("")
      : emptyState(
          "No device health data available."
        );
}

function workerFilterCategory(device) {
  if (isUrgentActivity(device.activity_status)) {
    return "incident";
  }

  if (
    isWarningActivity(device.activity_status)
    || batteryTone(device.battery_percent) === "low"
  ) {
    return "warning";
  }

  return "normal";
}

function renderWorkersPage() {
  const search =
    $("workerSearch").value
      .trim()
      .toLowerCase();

  const connectionFilter =
    $("workerConnectionFilter").value;

  const activityFilter =
    $("workerActivityFilter").value;

  const allDevices =
    state.dashboard?.devices || [];

  const filtered =
    allDevices.filter(device => {
      const matchesSearch =
        !search
        || String(device.worker_id)
          .toLowerCase()
          .includes(search)
        || String(device.device_name)
          .toLowerCase()
          .includes(search);

      const online =
        device.connection_status === "Active";

      const matchesConnection =
        connectionFilter === "all"
        || (
          connectionFilter === "online"
          && online
        )
        || (
          connectionFilter === "offline"
          && !online
        );

      const category =
        workerFilterCategory(device);

      const matchesActivity =
        activityFilter === "all"
        || category === activityFilter;

      return (
        matchesSearch
        && matchesConnection
        && matchesActivity
      );
    });

  $("workersSummary").textContent =
    `Showing ${filtered.length} of ${allDevices.length} worker device(s).`;

  $("workersGrid").innerHTML =
    filtered.length
      ? filtered
          .map(device =>
            workerCardHtml(device)
          )
          .join("")
      : emptyState(
          "No workers match the selected filters."
        );

  bindWorkerButtons(
    $("workersGrid")
  );
}

function populateIncidentWorkerFilter() {
  const select =
    $("incidentWorkerFilter");

  const previous =
    select.value || "all";

  const workers =
    [
      ...new Set(
        (state.dashboard?.incidents || [])
          .map(incident =>
            incident.worker_id
          )
          .filter(Boolean)
      ),
    ].sort();

  select.innerHTML = `
    <option value="all">
      All workers
    </option>
    ${
      workers
        .map(worker => `
          <option value="${escapeHtml(worker)}">
            ${escapeHtml(worker)}
          </option>
        `)
        .join("")
    }
  `;

  if (
    previous === "all"
    || workers.includes(previous)
  ) {
    select.value = previous;
  }
}

function filteredIncidents() {
  const type =
    $("incidentTypeFilter").value;

  const severity =
    $("incidentSeverityFilter").value;

  const status =
    $("incidentStatusFilter").value;

  const worker =
    $("incidentWorkerFilter").value;

  return (
    state.dashboard?.incidents || []
  ).filter(incident => {
    const matchesType =
      type === "all"
      || incident.event_type === type;

    const matchesSeverity =
      severity === "all"
      || incident.severity === severity;

    const matchesStatus =
      status === "all"
      || (
        status === "open"
        && !incident.acknowledged
      )
      || (
        status === "acknowledged"
        && incident.acknowledged
      );

    const matchesWorker =
      worker === "all"
      || incident.worker_id === worker;

    return (
      matchesType
      && matchesSeverity
      && matchesStatus
      && matchesWorker
    );
  });
}

function incidentCardHtml(
  incident,
  showAcknowledge = false,
) {
  const urgent =
    incident.severity === "URGENT";

  return `
    <article
      class="incident-card ${
        urgent
          ? "urgent"
          : "attention"
      }"
    >
      <div class="incident-head">
        <div>
          <h3 class="incident-title">
            ${escapeHtml(incident.event_type)}
          </h3>

          <div class="incident-meta">
            ${escapeHtml(incident.worker_id)}
            ·
            ${escapeHtml(incident.device_name)}
            ·
            ${escapeHtml(dateTimeText(incident.received_timestamp))}
          </div>
        </div>

        <span class="severity-chip ${urgent ? "urgent" : "attention"}">
          ${escapeHtml(incident.severity)}
        </span>
      </div>

      <p class="incident-description">
        ${escapeHtml(incident.description)}
      </p>

      <div class="incident-measures">
        <div class="measure">
          <span>Acceleration peak</span>
          <strong>
            ${numberText(incident.acceleration_peak_g, 3, " g")}
          </strong>
        </div>

        <div class="measure">
          <span>Gyroscope peak</span>
          <strong>
            ${numberText(incident.gyroscope_peak_dps, 1, " °/s")}
          </strong>
        </div>

        <div class="measure">
          <span>Minimum acceleration</span>
          <strong>
            ${numberText(incident.minimum_acceleration_g, 3, " g")}
          </strong>
        </div>

        <div class="measure">
          <span>Tilt change</span>
          <strong>
            ${numberText(incident.tilt_change_deg, 1, "°")}
          </strong>
        </div>
      </div>

      <div class="incident-footer">
        <div>
          ${
            incident.acknowledged
              ? `
                <span class="reviewed-label">
                  ✓ Acknowledged
                </span>
              `
              : `
                <span class="open-label">
                  ● Open incident
                </span>
              `
          }
          ${
            incident.feedback_label
              ? `<span class="incident-meta"> · ${escapeHtml(feedbackLabelText(incident.feedback_label))}</span>`
              : ""
          }
        </div>

        <div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end">
          <button
            class="review-button"
            data-review-incident="${incident.id}"
          >
            Review incident
          </button>

          ${
            showAcknowledge
            && !incident.acknowledged
              ? `
                <button
                  class="primary-button"
                  data-ack-incident="${incident.id}"
                >
                  Acknowledge
                </button>
              `
              : ""
          }
        </div>
      </div>
    </article>
  `;
}

function renderIncidentsPage() {
  populateIncidentWorkerFilter();

  const incidents =
    filteredIncidents();

  const total =
    state.dashboard?.incidents?.length || 0;

  $("incidentSummary").textContent =
    `Showing ${incidents.length} of ${total} loaded incident(s).`;

  $("incidentCards").innerHTML =
    incidents.length
      ? incidents
          .map(incident =>
            incidentCardHtml(incident)
          )
          .join("")
      : emptyState(
          "No incidents match the selected filters."
        );

  bindReviewButtons($("incidentCards"));
}

function destroyChart(key) {
  if (state.charts[key]) {
    state.charts[key].destroy();
    delete state.charts[key];
  }
}

function makeChart(
  key,
  canvasId,
  type,
  labels,
  datasets,
  options = {},
) {
  destroyChart(key);

  const canvas =
    $(canvasId);

  if (!canvas) {
    return;
  }

  state.charts[key] =
    new Chart(
      canvas,
      {
        type,
        data: {
          labels,
          datasets,
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: {
            mode: "index",
            intersect: false,
          },
          plugins: {
            legend: {
              display: true,
              position: "bottom",
              labels: {
                boxWidth: 10,
                boxHeight: 10,
                usePointStyle: true,
              },
            },
          },
          scales:
            type === "doughnut"
              ? undefined
              : {
                  x: {
                    grid: {
                      display: false,
                    },
                    ticks: {
                      maxTicksLimit: 8,
                    },
                  },
                  y: {
                    beginAtZero: true,
                    grid: {
                      color: "#eef2f6",
                    },
                  },
                },
          ...options,
        },
      }
    );
}

function renderAnalytics() {
  const devices =
    state.dashboard?.devices || [];

  const incidents =
    state.dashboard?.incidents || [];

  const open =
    incidents.filter(
      incident =>
        !incident.acknowledged
    );

  const urgent =
    incidents.filter(
      incident =>
        incident.severity === "URGENT"
    );

  const average =
    averageBattery();

  $("analyticsKpis").innerHTML = [
    kpiCard(
      "Loaded incidents",
      incidents.length,
      "Latest incident history returned by API",
    ),
    kpiCard(
      "Open incidents",
      open.length,
      "Require review",
      open.length ? "danger" : "success",
    ),
    kpiCard(
      "Urgent events",
      urgent.length,
      "Possible FFH / STF",
      urgent.length ? "danger" : "",
    ),
    kpiCard(
      "Average battery",
      average == null
        ? "—"
        : `${Math.round(average)}%`,
      "Latest device values",
    ),
  ].join("");

  const eventTypes = [
    "POSSIBLE_FFH",
    "POSSIBLE_STF",
    "POSSIBLE_NEAR_MISS",
  ];

  const eventLabels = [
    "Possible FFH",
    "Possible STF",
    "Near Miss",
  ];

  const eventCounts =
    eventTypes.map(type =>
      incidents.filter(
        incident =>
          incident.event_type === type
      ).length
    );

  makeChart(
    "incidentType",
    "incidentTypeChart",
    "doughnut",
    eventLabels,
    [
      {
        label: "Incidents",
        data: eventCounts,
        backgroundColor: [
          "#dc2626",
          "#f97316",
          "#f59e0b",
        ],
        borderWidth: 0,
      },
    ],
  );

  const dayCounts = {};

  incidents.forEach(incident => {
    const date =
      new Date(
        incident.received_timestamp
      );

    if (
      Number.isNaN(
        date.getTime()
      )
    ) {
      return;
    }

    const key =
      date.toISOString()
        .slice(0, 10);

    dayCounts[key] =
      (dayCounts[key] || 0)
      + 1;
  });

  const trendLabels =
    Object.keys(dayCounts)
      .sort();

  makeChart(
    "incidentTrend",
    "incidentTrendChart",
    "line",
    trendLabels,
    [
      {
        label: "Incidents",
        data:
          trendLabels.map(
            day => dayCounts[day]
          ),
        borderColor: "#2563eb",
        backgroundColor:
          "rgba(37, 99, 235, 0.12)",
        borderWidth: 2.2,
        tension: 0.3,
        fill: true,
        pointRadius: 3,
      },
    ],
  );

  const workerCounts = {};

  incidents.forEach(
    incident => {
      const worker =
        incident.worker_id
        || "Unknown";

      workerCounts[worker] =
        (workerCounts[worker] || 0)
        + 1;
    }
  );

  const workerLabels =
    Object.keys(workerCounts)
      .sort();

  makeChart(
    "workerIncident",
    "workerIncidentChart",
    "bar",
    workerLabels,
    [
      {
        label: "Incidents",
        data:
          workerLabels.map(
            worker =>
              workerCounts[worker]
          ),
        backgroundColor:
          "#2563eb",
        borderRadius: 8,
      },
    ],
  );

  const batteryDevices =
    devices.filter(
      device =>
        Number.isFinite(
          Number(
            device.battery_percent
          )
        )
    );

  makeChart(
    "battery",
    "batteryChart",
    "bar",
    batteryDevices.map(
      device =>
        device.worker_id
    ),
    [
      {
        label: "Battery %",
        data:
          batteryDevices.map(
            device =>
              Number(
                device.battery_percent
              )
          ),
        backgroundColor:
          batteryDevices.map(
            device => {
              const value =
                Number(
                  device.battery_percent
                );

              if (value <= 20) {
                return "#dc2626";
              }

              if (value <= 40) {
                return "#d97706";
              }

              return "#16a34a";
            }
          ),
        borderRadius: 8,
      },
    ],
    {
      scales: {
        y: {
          beginAtZero: true,
          max: 100,
        },
      },
    },
  );
}

function labelsFor(
  rows,
  field,
) {
  return rows.map(row => {
    const value = row[field];

    if (!value) {
      return "";
    }

    const date =
      new Date(value);

    return Number.isNaN(
      date.getTime()
    )
      ? ""
      : date.toLocaleTimeString(
          [],
          {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          }
        );
  });
}

async function loadTechnicalPage() {
  const devices =
    state.dashboard?.devices || [];

  const select =
    $("technicalDevice");

  const previous =
    select.value;

  select.innerHTML =
    devices.length
      ? devices
          .map(device => `
            <option value="${escapeHtml(device.device_name)}">
              ${escapeHtml(device.worker_id)}
              ·
              ${escapeHtml(device.device_name)}
            </option>
          `)
          .join("")
      : `
        <option value="">
          No devices
        </option>
      `;

  if (
    previous
    && devices.some(
      device =>
        device.device_name
        === previous
    )
  ) {
    select.value =
      previous;
  }

  const deviceName =
    select.value;

  if (!deviceName) {
    $("technicalKpis").innerHTML =
      kpiCard(
        "Status",
        "No devices",
        "Waiting for sensor data",
      );

    return;
  }

  try {
    const [
      featuresData,
      sensorData,
    ] = await Promise.all(
      [
        api(
          `/api/v1/devices/${
            encodeURIComponent(
              deviceName
            )
          }/features?limit=300`
        ),
        api(
          `/api/v1/devices/${
            encodeURIComponent(
              deviceName
            )
          }/sensor?limit=1200`
        ),
      ]
    );

    state.technicalRows =
      featuresData.rows || [];

    state.technicalSensorRows =
      sensorData.rows || [];

    renderTechnicalCharts();
  } catch (error) {
    $("technicalKpis").innerHTML =
      kpiCard(
        "Technical data",
        "Unavailable",
        error.message,
        "danger",
      );
  }
}

function renderTechnicalCharts() {
  const rows =
    state.technicalRows;

  if (!rows.length) {
    $("technicalKpis").innerHTML =
      kpiCard(
        "Feature windows",
        "0",
        "No processed feature rows stored yet",
      );

    [
      "featureAccel",
      "featureGyro",
      "featureFrequency",
      "featureTilt",
    ].forEach(
      destroyChart
    );

    return;
  }

  const latest =
    rows.at(-1);

  $("technicalKpis").innerHTML = [
    kpiCard(
      "Activity",
      latest.activity_status || "—",
      "Latest classification",
    ),
    kpiCard(
      "Acceleration peak",
      numberText(
        latest.acc_peak_g,
        3,
        " g",
      ),
      "Latest feature window",
    ),
    kpiCard(
      "Dominant frequency",
      numberText(
        latest.dominant_frequency_hz,
        2,
        " Hz",
      ),
      "FFT-derived feature",
    ),
    kpiCard(
      "Tilt change",
      numberText(
        latest.tilt_change_deg,
        1,
        "°",
      ),
      "Estimated posture change",
    ),
  ].join("");

  const labels =
    labelsFor(
      rows,
      "window_end",
    );

  makeChart(
    "featureAccel",
    "featureAccelChart",
    "line",
    labels,
    [
      {
        label: "Acceleration peak (g)",
        data:
          rows.map(
            row =>
              Number(
                row.acc_peak_g
              )
          ),
        borderColor: "#2563eb",
        backgroundColor:
          "rgba(37, 99, 235, 0.08)",
        tension: 0.25,
        borderWidth: 2,
        pointRadius: 0,
      },
    ],
  );

  makeChart(
    "featureGyro",
    "featureGyroChart",
    "line",
    labels,
    [
      {
        label: "Gyroscope peak (°/s)",
        data:
          rows.map(
            row =>
              Number(
                row.gyro_max_dps
              )
          ),
        borderColor: "#7c3aed",
        backgroundColor:
          "rgba(124, 58, 237, 0.08)",
        tension: 0.25,
        borderWidth: 2,
        pointRadius: 0,
      },
    ],
  );

  makeChart(
    "featureFrequency",
    "featureFrequencyChart",
    "line",
    labels,
    [
      {
        label: "Dominant frequency (Hz)",
        data:
          rows.map(
            row =>
              Number(
                row.dominant_frequency_hz
              )
          ),
        borderColor: "#0891b2",
        backgroundColor:
          "rgba(8, 145, 178, 0.08)",
        tension: 0.25,
        borderWidth: 2,
        pointRadius: 0,
      },
    ],
  );

  makeChart(
    "featureTilt",
    "featureTiltChart",
    "line",
    labels,
    [
      {
        label: "Tilt change (°)",
        data:
          rows.map(
            row =>
              Number(
                row.tilt_change_deg
              )
          ),
        borderColor: "#d97706",
        backgroundColor:
          "rgba(217, 119, 6, 0.08)",
        tension: 0.25,
        borderWidth: 2,
        pointRadius: 0,
      },
    ],
  );
}

function populateAdminDevices() {
  const devices =
    state.dashboard?.devices || [];

  const select =
    $("adminDevice");

  const previous =
    select.value;

  select.innerHTML =
    devices.length
      ? devices
          .map(device => `
            <option value="${escapeHtml(device.device_name)}">
              ${escapeHtml(device.device_name)}
            </option>
          `)
          .join("")
      : `
        <option value="">
          No devices
        </option>
      `;

  if (
    previous
    && devices.some(
      device =>
        device.device_name
        === previous
    )
  ) {
    select.value = previous;
  }

  updateAdminWorkerField();
}

function updateAdminWorkerField() {
  const deviceName =
    $("adminDevice").value;

  const device =
    state.dashboard
      ?.devices
      ?.find(
        row =>
          row.device_name
          === deviceName
      );

  $("adminWorker").value =
    device?.worker_id || "";
}

function renderAdminIncidents() {
  const open =
    (state.dashboard?.incidents || [])
      .filter(
        incident =>
          !incident.acknowledged
      );

  $("adminIncidentList").innerHTML =
    open.length
      ? open
          .map(incident => `
            <div class="compact-row">
              <div class="compact-row-main">
                <strong>
                  ${escapeHtml(incident.worker_id)}
                  ·
                  ${escapeHtml(incident.event_type)}
                </strong>

                <span>
                  ${escapeHtml(timeAgo(incident.received_timestamp))}
                  ·
                  ${escapeHtml(incident.severity)}
                </span>
              </div>

              <button
                class="secondary-button"
                data-ack-incident="${incident.id}"
              >
                Acknowledge
              </button>
            </div>
          `)
          .join("")
      : emptyState(
          "No open incidents."
        );

  bindAcknowledgeButtons(
    $("adminIncidentList")
  );
}

function bindAcknowledgeButtons(scope = document) {
  scope
    .querySelectorAll("[data-ack-incident]")
    .forEach(button => {
      button.onclick = () =>
        acknowledgeIncident(
          Number(
            button.dataset.ackIncident
          )
        );
    });
}

async function acknowledgeIncident(
  incidentId,
) {
  const password =
    $("adminPassword").value;

  if (!password) {
    $("adminMessage").textContent =
      "Enter the admin password first.";

    switchPage("admin");

    showToast(
      "Admin password required",
      "Enter your password before acknowledging an incident.",
    );

    return;
  }

  try {
    await api(
      `/api/v1/incidents/${incidentId}/acknowledge`,
      {
        method: "POST",
        headers: {
          "X-Admin-Password":
            password,
        },
      },
    );

    $("adminMessage").textContent =
      "Incident acknowledged.";

    showToast(
      "Incident acknowledged",
      "The incident has been marked as reviewed.",
      "success",
    );

    await refreshDashboard();
  } catch (error) {
    $("adminMessage").textContent =
      error.message;

    showToast(
      "Could not acknowledge incident",
      error.message,
    );
  }
}

async function saveWorkerAssignment() {
  const deviceName =
    $("adminDevice").value;

  const workerId =
    $("adminWorker")
      .value
      .trim();

  const password =
    $("adminPassword").value;

  if (
    !deviceName
    || !workerId
    || !password
  ) {
    $("adminMessage").textContent =
      "Device, worker name and admin password are required.";

    return;
  }

  try {
    await api(
      `/api/v1/devices/${
        encodeURIComponent(
          deviceName
        )
      }/assign`,
      {
        method: "POST",
        headers: {
          "Content-Type":
            "application/json",
          "X-Admin-Password":
            password,
        },
        body: JSON.stringify(
          {
            worker_id: workerId,
          }
        ),
      },
    );

    $("adminMessage").textContent =
      "Worker assignment saved.";

    showToast(
      "Worker updated",
      `${workerId} is now assigned to ${deviceName}.`,
      "success",
    );

    await refreshDashboard();
  } catch (error) {
    $("adminMessage").textContent =
      error.message;

    showToast(
      "Could not save worker",
      error.message,
    );
  }
}

async function openWorkerDetail(
  deviceName,
) {
  state.previousPage =
    state.currentPage === "workerDetail"
      ? "workers"
      : state.currentPage;

  state.selectedDevice =
    deviceName;

  switchPage(
    "workerDetail"
  );

  await loadWorkerDetail();
}

async function loadWorkerDetail() {
  const deviceName =
    state.selectedDevice;

  if (!deviceName) {
    return;
  }

  const device =
    state.dashboard
      ?.devices
      ?.find(
        row =>
          row.device_name
          === deviceName
      );

  if (!device) {
    $("workerDetailHero").innerHTML =
      emptyState(
        "Device not found in dashboard data."
      );

    return;
  }

  const activity =
    activityMeta[
      device.activity_status
    ] || {
      label:
        device.activity_status
        || "Unknown",
      tone: "warning",
    };

  $("workerDetailHero").innerHTML = `
    <div class="detail-title">
      <h2>${escapeHtml(device.worker_id)}</h2>
      <span>${escapeHtml(device.device_name)}</span>
    </div>

    <div class="detail-status">
      <strong>
        ${escapeHtml(activity.label)}
      </strong>

      <span>
        ${
          device.connection_status === "Active"
            ? `Live · updated ${escapeHtml(timeAgo(device.last_seen))}`
            : `Offline · last seen ${escapeHtml(timeAgo(device.last_seen))}`
        }
      </span>
    </div>
  `;

  $("workerDetailKpis").innerHTML = [
    kpiCard(
      "Acceleration",
      numberText(
        device.latest_acceleration_g,
        3,
        " g",
      ),
      "Latest magnitude",
    ),
    kpiCard(
      "Gyroscope",
      numberText(
        device.latest_gyro_dps,
        1,
        " °/s",
      ),
      "Latest magnitude",
    ),
    kpiCard(
      "Classification confidence",
      Number.isFinite(
        Number(
          device.confidence
        )
      )
        ? `${
            Math.round(
              Number(
                device.confidence
              )
              * 100
            )
          }%`
        : "—",
      "Latest model/rule confidence",
    ),
    kpiCard(
      "Battery",
      percentageText(
        device.battery_percent
      ),
      batteryLabel(
        device.battery_percent
      ),
      batteryTone(
        device.battery_percent
      ) === "low"
        ? "danger"
        : batteryTone(
            device.battery_percent
          ) === "medium"
          ? "warning"
          : "",
    ),
  ].join("");

  try {
    const [
      sensorData,
      incidentData,
    ] = await Promise.all(
      [
        api(
          `/api/v1/devices/${
            encodeURIComponent(
              deviceName
            )
          }/sensor?limit=1000`
        ),
        api(
          `/api/v1/incidents?device_name=${
            encodeURIComponent(
              deviceName
            )
          }&limit=100`
        ),
      ]
    );

    const rows =
      sensorData.rows || [];

    renderWorkerSensorCharts(
      rows
    );

    const incidents =
      incidentData.incidents || [];

    $("workerDetailIncidentList").innerHTML =
      incidents.length
        ? incidents
            .map(incident =>
              incidentCardHtml(
                incident
              )
            )
            .join("")
        : emptyState(
            "No incidents recorded for this worker."
          );

    bindReviewButtons($("workerDetailIncidentList"));
  } catch (error) {
    $("workerDetailIncidentList").innerHTML =
      emptyState(
        `Could not load worker data: ${error.message}`
      );
  }
}

function renderWorkerSensorCharts(
  rows,
) {
  if (!rows.length) {
    [
      "detailAccelMagnitude",
      "detailAccelAxes",
      "detailGyroMagnitude",
      "detailGyroAxes",
    ].forEach(
      destroyChart
    );

    return;
  }

  const labels =
    labelsFor(
      rows,
      "timestamp",
    );

  makeChart(
    "detailAccelMagnitude",
    "detailAccelMagnitudeChart",
    "line",
    labels,
    [
      {
        label:
          "Acceleration magnitude (g)",
        data:
          rows.map(
            row =>
              Number(
                row.acceleration_magnitude_g
              )
          ),
        borderColor: "#2563eb",
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.2,
      },
    ],
  );

  makeChart(
    "detailAccelAxes",
    "detailAccelAxesChart",
    "line",
    labels,
    [
      {
        label: "X",
        data:
          rows.map(
            row =>
              Number(
                row.accelerometer_x_g
              )
          ),
        borderColor: "#2563eb",
        borderWidth: 1.5,
        pointRadius: 0,
      },
      {
        label: "Y",
        data:
          rows.map(
            row =>
              Number(
                row.accelerometer_y_g
              )
          ),
        borderColor: "#16a34a",
        borderWidth: 1.5,
        pointRadius: 0,
      },
      {
        label: "Z",
        data:
          rows.map(
            row =>
              Number(
                row.accelerometer_z_g
              )
          ),
        borderColor: "#d97706",
        borderWidth: 1.5,
        pointRadius: 0,
      },
    ],
  );

  makeChart(
    "detailGyroMagnitude",
    "detailGyroMagnitudeChart",
    "line",
    labels,
    [
      {
        label:
          "Gyroscope magnitude (°/s)",
        data:
          rows.map(
            row =>
              Number(
                row.gyroscope_magnitude_deg_s
              )
          ),
        borderColor: "#7c3aed",
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.2,
      },
    ],
  );

  makeChart(
    "detailGyroAxes",
    "detailGyroAxesChart",
    "line",
    labels,
    [
      {
        label: "X",
        data:
          rows.map(
            row =>
              Number(
                row.gyroscope_x_deg_s
              )
          ),
        borderColor: "#7c3aed",
        borderWidth: 1.5,
        pointRadius: 0,
      },
      {
        label: "Y",
        data:
          rows.map(
            row =>
              Number(
                row.gyroscope_y_deg_s
              )
          ),
        borderColor: "#db2777",
        borderWidth: 1.5,
        pointRadius: 0,
      },
      {
        label: "Z",
        data:
          rows.map(
            row =>
              Number(
                row.gyroscope_z_deg_s
              )
          ),
        borderColor: "#0891b2",
        borderWidth: 1.5,
        pointRadius: 0,
      },
    ],
  );
}

function csvEscape(value) {
  const text =
    value == null
      ? ""
      : String(value);

  if (
    text.includes(",")
    || text.includes('"')
    || text.includes("\n")
  ) {
    return `"${text.replaceAll('"', '""')}"`;
  }

  return text;
}

function downloadRowsAsCsv(
  rows,
  filename,
) {
  if (!rows.length) {
    showToast(
      "Nothing to export",
      "There are no loaded records for this export.",
    );

    return;
  }

  const columns =
    [
      ...new Set(
        rows.flatMap(
          row =>
            Object.keys(row)
        )
      ),
    ];

  const lines = [
    columns
      .map(csvEscape)
      .join(","),
    ...rows.map(
      row =>
        columns
          .map(column =>
            csvEscape(
              row[column]
            )
          )
          .join(",")
    ),
  ];

  const blob =
    new Blob(
      [
        lines.join("\n"),
      ],
      {
        type:
          "text/csv;charset=utf-8",
      },
    );

  const url =
    URL.createObjectURL(
      blob
    );

  const anchor =
    document.createElement("a");

  anchor.href = url;
  anchor.download =
    filename;

  document.body
    .appendChild(anchor);

  anchor.click();
  anchor.remove();

  URL.revokeObjectURL(
    url
  );
}

function detectNewIncidents() {
  const incidents =
    state.dashboard?.incidents || [];

  if (!incidents.length) {
    return;
  }

  const latestId =
    Math.max(
      ...incidents.map(
        incident =>
          Number(incident.id)
      )
    );

  if (
    state.lastSeenIncidentId
    == null
  ) {
    state.lastSeenIncidentId =
      latestId;

    return;
  }

  const newIncidents =
    incidents
      .filter(
        incident =>
          Number(incident.id)
          > state.lastSeenIncidentId
      )
      .sort(
        (a, b) =>
          Number(a.id)
          - Number(b.id)
      );

  if (!newIncidents.length) {
    return;
  }

  newIncidents.forEach(
    incident => {
      showToast(
        `${incident.event_type} · ${incident.worker_id}`,
        incident.description,
        incident.severity === "URGENT"
          ? "urgent"
          : "",
      );
    }
  );

  if (
    $("soundToggle").checked
  ) {
    $("alertAudio")
      .play()
      .catch(
        () => {}
      );
  }

  state.lastSeenIncidentId =
    latestId;
}

function renderNavigationBadge() {
  const open =
    (state.dashboard?.incidents || [])
      .filter(
        incident =>
          !incident.acknowledged
      )
      .length;

  const badge =
    $("incidentNavBadge");

  badge.textContent =
    open;

  badge.classList.toggle(
    "hidden",
    open === 0,
  );
}

function feedbackLabelText(label) {
  const labels = {
    ACTUAL_EVENT: "Confirmed event",
    FALSE_ALARM: "False alarm",
    UNSURE: "Unsure",
  };

  return labels[label] || label || "Not reviewed";
}


function actualEventTypeText(value) {
  const labels = {
    FFH: "Fall from height (FFH)",
    STF: "Slip / trip / fall (STF)",
    NEAR_MISS: "Near miss",
  };

  return labels[value] || value || "";
}

function updateActualEventTypeVisibility() {
  const isActual =
    state.selectedFeedback === "ACTUAL_EVENT";

  $("actualEventTypeWrap")
    ?.classList
    .toggle(
      "hidden",
      !isActual,
    );

  if (!isActual) {
    state.selectedActualEventType = null;

    if ($("actualEventType")) {
      $("actualEventType").value = "";
    }
  }
}

function updateWelcome() {
  const hour = new Date().getHours();
  let greeting = "Welcome back";

  if (hour < 12) {
    greeting = "Good morning";
  } else if (hour < 18) {
    greeting = "Good afternoon";
  } else {
    greeting = "Good evening";
  }

  const summary = state.dashboard?.summary || {};
  const open = Number(summary.open_incidents || 0);
  const online = Number(summary.workers_online || 0);
  const registered = Number(summary.registered_devices || 0);

  $("welcomeHeading").textContent = `${greeting} — your safety dashboard is ready`;
  $("welcomeMessage").textContent = open > 0
    ? `${open} open incident${open === 1 ? "" : "s"} need review. ${online} of ${registered} worker device${registered === 1 ? " is" : "s are"} currently online.`
    : `No open incidents need attention right now. ${online} of ${registered} worker device${registered === 1 ? " is" : "s are"} currently online.`;
}

function bindReviewButtons(scope = document) {
  scope
    .querySelectorAll("[data-review-incident]")
    .forEach(button => {
      button.onclick = () => openIncidentReview(Number(button.dataset.reviewIncident));
    });
}

async function openIncidentReview(incidentId) {
  state.reviewIncidentId = incidentId;
  state.selectedFeedback = null;

  $("incidentModal").classList.remove("hidden");
  document.body.classList.add("modal-open");
  $("feedbackMessage").textContent = "";
  $("feedbackAdminPassword").value = $("adminPassword")?.value || "";

  document
    .querySelectorAll(".feedback-button")
    .forEach(button => button.classList.remove("selected"));

  const incident = (state.dashboard?.incidents || []).find(row => Number(row.id) === Number(incidentId));

  if (incident) {
    renderIncidentReviewHeader(incident);
    renderExplanation(incident);
    prefillFeedback(incident);
  }

  $("replayCoverage").textContent = "Loading replay…";

  try {
    const replay = await api(`/api/v1/incidents/${incidentId}/replay`);
    const fullIncident = replay.incident || incident;

    if (fullIncident) {
      renderIncidentReviewHeader(fullIncident);
      renderExplanation(fullIncident);
      prefillFeedback(fullIncident);
    }

    renderReplayCharts(replay.rows || []);

    const rows = replay.rows || [];
    if (rows.length) {
      const min = Math.min(...rows.map(row => Number(row.relative_seconds)));
      const max = Math.max(...rows.map(row => Number(row.relative_seconds)));
      $("replayCoverage").textContent = `${rows.length} samples · ${min.toFixed(1)}s to +${Math.max(0, max).toFixed(1)}s`;
    } else {
      $("replayCoverage").textContent = "No replay samples saved";
    }
  } catch (error) {
    $("replayCoverage").textContent = "Replay unavailable";
    showToast("Could not load replay", error.message);
  }
}

function closeIncidentReview() {
  $("incidentModal").classList.add("hidden");
  document.body.classList.remove("modal-open");
  destroyChart("replayAccel");
  destroyChart("replayGyro");
}

function renderIncidentReviewHeader(incident) {
  $("incidentModalTitle").textContent = `${incident.event_type} · ${incident.worker_id}`;
  $("incidentModalSubtitle").textContent = `${incident.device_name} · ${dateTimeText(incident.received_timestamp)}`;

  const feedback = incident.feedback_label
    ? feedbackLabelText(incident.feedback_label)
    : "Awaiting review";

  $("incidentReviewStatus").innerHTML = `
    <div class="review-status-card">
      <strong>${escapeHtml(incident.severity || "Incident")}</strong>
      <span>${escapeHtml(incident.description || "")}</span>
      <span>
        Review: ${escapeHtml(feedback)}
        ${
          incident.actual_event_type
            ? ` · ${escapeHtml(actualEventTypeText(incident.actual_event_type))}`
            : ""
        }
      </span>
    </div>
  `;
}

function renderExplanation(incident) {
  const details = incident.trigger_details || {};
  const checks = Array.isArray(details.checks) ? details.checks : [];

  if (!checks.length) {
    $("explanationChecks").innerHTML = emptyState(
      "Detailed trigger explanation is available for incidents created after this upgrade is deployed."
    );
    return;
  }

  $("explanationChecks").innerHTML = checks.map(check => `
    <div class="explanation-check ${check.passed ? "pass" : "fail"}">
      <div class="explanation-check-icon">${check.passed ? "✓" : "·"}</div>
      <div>
        <strong>${escapeHtml(check.label)}</strong>
        <small>Rule: ${escapeHtml(check.comparison)} ${escapeHtml(check.threshold)} ${escapeHtml(check.unit)}</small>
      </div>
      <div class="explanation-value">
        ${escapeHtml(check.value)} ${escapeHtml(check.unit)}
      </div>
    </div>
  `).join("");
}

function prefillFeedback(incident) {
  state.selectedFeedback =
    incident.feedback_label
    || null;

  state.selectedActualEventType =
    incident.actual_event_type
    || null;

  $("feedbackNotes").value =
    incident.feedback_notes
    || "";

  if ($("actualEventType")) {
    $("actualEventType").value =
      state.selectedActualEventType
      || "";
  }

  updateActualEventTypeVisibility();

  document
    .querySelectorAll(".feedback-button")
    .forEach(button => {
      button.classList.toggle(
        "selected",
        button.dataset.feedbackLabel
          === state.selectedFeedback,
      );
    });

  $("feedbackMessage").textContent =
    incident.feedback_label
      ? `Current review: ${
          feedbackLabelText(
            incident.feedback_label
          )
        }${
          incident.actual_event_type
            ? ` · ${
                actualEventTypeText(
                  incident.actual_event_type
                )
              }`
            : ""
        }`
      : "Choose a review result, then save.";
}

function renderReplayCharts(rows) {
  destroyChart("replayAccel");
  destroyChart("replayGyro");

  if (!rows.length) {
    return;
  }

  const accelPoints = rows.map(row => ({
    x: Number(row.relative_seconds),
    y: Number(row.acceleration_magnitude_g),
  }));

  const gyroPoints = rows.map(row => ({
    x: Number(row.relative_seconds),
    y: Number(row.gyroscope_magnitude_deg_s),
  }));

  state.charts.replayAccel = new Chart($("replayAccelChart"), {
    type: "line",
    data: {
      datasets: [{
        label: "Acceleration magnitude (g)",
        data: accelPoints,
        parsing: false,
        borderColor: "#2563eb",
        backgroundColor: "rgba(37,99,235,0.08)",
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.15,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { position: "bottom" },
        eventLine: { enabled: true },
      },
      scales: {
        x: {
          type: "linear",
          title: { display: true, text: "Seconds from event" },
          grid: { color: "#eef2f6" },
        },
        y: {
          beginAtZero: true,
          title: { display: true, text: "g" },
          grid: { color: "#eef2f6" },
        },
      },
    },
  });

  state.charts.replayGyro = new Chart($("replayGyroChart"), {
    type: "line",
    data: {
      datasets: [{
        label: "Gyroscope magnitude (°/s)",
        data: gyroPoints,
        parsing: false,
        borderColor: "#7c3aed",
        backgroundColor: "rgba(124,58,237,0.08)",
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.15,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { position: "bottom" },
        eventLine: { enabled: true },
      },
      scales: {
        x: {
          type: "linear",
          title: { display: true, text: "Seconds from event" },
          grid: { color: "#eef2f6" },
        },
        y: {
          beginAtZero: true,
          title: { display: true, text: "°/s" },
          grid: { color: "#eef2f6" },
        },
      },
    },
  });
}

async function saveIncidentFeedback() {
  const incidentId =
    state.reviewIncidentId;

  const label =
    state.selectedFeedback;

  const password =
    $("feedbackAdminPassword").value;

  const notes =
    $("feedbackNotes").value.trim();

  const actualEventType =
    $("actualEventType")?.value
    || null;

  if (!incidentId) {
    return;
  }

  if (!label) {
    $("feedbackMessage").textContent =
      "Choose Actual event, False alarm, or Unsure.";
    return;
  }

  if (
    label === "ACTUAL_EVENT"
    && !actualEventType
  ) {
    $("feedbackMessage").textContent =
      "Choose whether the actual event was FFH, STF, or Near Miss.";
    return;
  }

  if (!password) {
    $("feedbackMessage").textContent =
      "Enter the admin password to save this review.";
    return;
  }

  try {
    await api(
      `/api/v1/incidents/${incidentId}/feedback`,
      {
        method: "POST",
        headers: {
          "Content-Type":
            "application/json",
          "X-Admin-Password":
            password,
        },
        body: JSON.stringify(
          {
            label,
            actual_event_type:
              actualEventType,
            notes,
          }
        ),
      },
    );

    $("feedbackMessage").textContent =
      "Review saved.";

    showToast(
      "Incident review saved",
      "Your feedback was stored in Supabase and can now be used by Threshold Lab.",
      "success",
    );

    state.thresholdLearningLastLoaded =
      0;

    await refreshDashboard();
    await loadSystemHealth(true);

    if (
      state.currentPage
      === "thresholdLab"
    ) {
      await loadThresholdLab(true);
    }
  } catch (error) {
    $("feedbackMessage").textContent =
      error.message;
  }
}

function thresholdValueText(item) {
  const value =
    Number(item.value);

  const text =
    Number.isFinite(value)
      ? value.toFixed(
          Math.abs(value) < 1
            ? 3
            : 2
        )
      : "—";

  return `${
    item.comparison
    || ""
  } ${text} ${
    item.unit
    || ""
  }`.trim();
}


function thresholdAccuracyText(value) {
  const number =
    Number(value);

  return Number.isFinite(number)
    ? `${
        (
          number
          *
          100
        ).toFixed(1)
      }%`
    : "—";
}


function renderThresholdReadiness(data) {
  const readiness =
    data.readiness
    || {};

  const summary =
    data.dataset
    || {};

  $("thresholdReadiness").innerHTML =
    readiness.ready
      ? `
        <div class="threshold-ready-box ready">
          <strong>Ready to train</strong>
          <span>
            ${
              escapeHtml(
                summary.reviewed_samples
                || 0
              )
            } reviewed + ${
              escapeHtml(
                summary.imported_samples
                || 0
              )
            } imported labelled examples, plus ${
              escapeHtml(
                summary.auto_normal_samples
                || 0
              )
            } background normal windows are available.
          </span>
        </div>
      `
      : `
        <div class="threshold-ready-box waiting">
          <strong>More reviewed data needed</strong>
          <ul>
            ${
              (
                readiness.reasons
                || []
              )
                .map(
                  reason =>
                    `<li>${
                      escapeHtml(reason)
                    }</li>`
                )
                .join("")
            }
          </ul>
        </div>
      `;

  $("trainThresholdButton").disabled =
    !readiness.ready;
}


function renderCurrentThresholds(data) {
  const rows =
    data.current_thresholds
    || [];

  $("currentThresholdList").innerHTML =
    rows.length
      ? rows
          .map(
            item => `
              <div class="current-threshold-row">
                <div>
                  <strong>
                    ${
                      escapeHtml(
                        item.label
                      )
                    }
                  </strong>
                  <span>
                    ${
                      escapeHtml(
                        item.key
                      )
                    }
                  </span>
                </div>

                <b>
                  ${
                    escapeHtml(
                      thresholdValueText(
                        item
                      )
                    )
                  }
                </b>
              </div>
            `
          )
          .join("")
      : emptyState(
          "No threshold values returned."
        );
}


function renderThresholdKpis(data) {
  const summary =
    data.dataset
    || {};

  const latest =
    data.latest_run
    || {};

  const metrics =
    latest.decision_tree_metrics
    || {};

  const classes =
    summary.class_counts
    || {};

  $("thresholdLabKpis").innerHTML =
    [
      kpiCard(
        "Labelled examples",
        String(
          summary.labelled_samples
          || 0
        ),
        `${
          summary.reviewed_samples
          || 0
        } reviewed + ${
          summary.imported_samples
          || 0
        } historical`,
      ),
      kpiCard(
        "Training samples",
        String(
          summary.total_samples
          || 0
        ),
        `${
          summary.auto_normal_samples
          || 0
        } background NO_FALL windows`,
      ),
      kpiCard(
        "FALL examples",
        String(
          classes.FALL
          || 0
        ),
        "Reviewed or imported FALL examples",
      ),
      kpiCard(
        "Latest FALL recall",
        thresholdAccuracyText(
          metrics.fall_recall
        ),
        latest.holdout_reliable
          ? "How many true falls the tree detected"
          : latest.run_id
            ? "Training-set estimate only"
            : "Train a recommendation first",
        latest.run_id
        && latest.holdout_reliable
          ? "success"
          : "",
      ),
    ]
      .join("");
}


function renderThresholdComparison(run) {
  if (!run) {
    $("thresholdRunMeta").textContent =
      "No recommendation trained yet.";

    $("thresholdModelComparison").innerHTML =
      emptyState(
        "Review incidents first, then train your first Decision Tree recommendation."
      );

    $("thresholdSplitRules").innerHTML =
      emptyState(
        "No learned rules yet."
      );

    $("thresholdFeatureImportance").innerHTML =
      emptyState(
        "No feature importance yet."
      );

    $("thresholdConfusionMatrix").innerHTML =
      emptyState(
        "No evaluation yet."
      );

    return;
  }

  const treeMetrics =
    run.decision_tree_metrics
    || {};

  const currentMetrics =
    run.current_system_metrics
    || {};

  const dataset =
    run.dataset
    || {};

  $("thresholdRunMeta").textContent =
    `Run #${
      run.run_id
      || "—"
    } · ${
      dateTimeText(
        run.database_created_at
        || run.trained_at
      )
    } · ${
      dataset.total_samples
      || 0
    } samples`;

  $("thresholdModelComparison").innerHTML = `
    <div class="threshold-compare-grid">
      <article>
        <span>
          Current stored predictions
        </span>
        <strong>
          ${
            escapeHtml(
              thresholdAccuracyText(
                currentMetrics.accuracy
              )
            )
          }
        </strong>
        <small>
          Existing rule system vs the labels used in this run
        </small>
      </article>

      <article>
        <span>
          Decision Tree accuracy
        </span>
        <strong>
          ${
            escapeHtml(
              thresholdAccuracyText(
                treeMetrics.accuracy
              )
            )
          }
        </strong>
        <small>
          Balanced: ${
            escapeHtml(
              thresholdAccuracyText(
                treeMetrics.balanced_accuracy
              )
            )
          }
        </small>
      </article>

      <article>
        <span>
          FALL recall
        </span>
        <strong>
          ${
            escapeHtml(
              thresholdAccuracyText(
                treeMetrics.fall_recall
              )
            )
          }
        </strong>
        <small>
          Higher means fewer missed falls
        </small>
      </article>

      <article>
        <span>
          FALL precision
        </span>
        <strong>
          ${
            escapeHtml(
              thresholdAccuracyText(
                treeMetrics.fall_precision
              )
            )
          }
        </strong>
        <small>
          Higher means fewer false fall alarms
        </small>
      </article>
    </div>

    <p class="friendly-note">
      ${
        escapeHtml(
          run.safety_note
          || ""
        )
      }
    </p>
  `;

  const rules =
    run.tree?.split_rules
    || [];

  $("thresholdSplitRules").innerHTML =
    rules.length
      ? rules
          .map(
            (rule, index) => `
              <div class="threshold-rule-row">
                <span class="threshold-rule-number">
                  ${index + 1}
                </span>

                <div>
                  <strong>
                    ${
                      escapeHtml(
                        rule.rule_text
                      )
                    }
                  </strong>

                  <small>
                    Depth ${
                      escapeHtml(
                        rule.depth
                      )
                    } ·
                    ${
                      escapeHtml(
                        rule.samples
                      )
                    } training samples at this node
                  </small>
                </div>
              </div>
            `
          )
          .join("")
      : emptyState(
          "The tree did not need any splits."
        );

  const importances =
    run.tree?.feature_importances
    || [];

  const maximum =
    Math.max(
      0.000001,
      ...importances
        .map(
          item =>
            Number(
              item.importance
              || 0
            )
        )
    );

  $("thresholdFeatureImportance").innerHTML =
    importances.length
      ? importances
          .map(
            item => {
              const percentage =
                (
                  Number(
                    item.importance
                    || 0
                  )
                  /
                  maximum
                )
                *
                100;

              return `
                <div class="feature-importance-row">
                  <div>
                    <strong>
                      ${
                        escapeHtml(
                          item.label
                        )
                      }
                    </strong>

                    <span>
                      ${
                        (
                          Number(
                            item.importance
                            || 0
                          )
                          *
                          100
                        ).toFixed(1)
                      }%
                    </span>
                  </div>

                  <div class="feature-importance-track">
                    <div
                      style="width:${
                        Math.max(
                          0,
                          Math.min(
                            100,
                            percentage
                          )
                        )
                      }%"
                    ></div>
                  </div>
                </div>
              `;
            }
          )
          .join("")
      : emptyState(
          "No feature importance available."
        );

  renderThresholdConfusionMatrix(
    treeMetrics,
    run,
  );
}


function renderThresholdConfusionMatrix(
  metrics,
  run,
) {
  const classes =
    metrics?.classes
    || [];

  const matrix =
    metrics?.confusion_matrix
    || [];

  $("thresholdEvaluationNote").textContent =
    run?.holdout_reliable
      ? (
          "Rows are actual FALL / NO_FALL labels; "
          +
          "columns are Decision Tree predictions on the holdout set."
        )
      : (
          "This is based on the training data because "
          +
          "there are not yet enough examples per class "
          +
          "for a reliable stratified holdout."
        );

  if (
    !classes.length
    ||
    !matrix.length
  ) {
    $("thresholdConfusionMatrix").innerHTML =
      emptyState(
        "No confusion matrix available."
      );

    return;
  }

  $("thresholdConfusionMatrix").innerHTML = `
    <div class="threshold-matrix-wrap">
      <table class="threshold-matrix">
        <thead>
          <tr>
            <th>
              Actual \\ Predicted
            </th>

            ${
              classes
                .map(
                  label =>
                    `<th>${
                      escapeHtml(label)
                    }</th>`
                )
                .join("")
            }
          </tr>
        </thead>

        <tbody>
          ${
            classes
              .map(
                (label, rowIndex) => `
                  <tr>
                    <th>
                      ${
                        escapeHtml(label)
                      }
                    </th>

                    ${
                      (
                        matrix[
                          rowIndex
                        ]
                        || []
                      )
                        .map(
                          (value, colIndex) => `
                            <td
                              class="${
                                rowIndex
                                === colIndex
                                  ? "matrix-correct"
                                  : ""
                              }"
                            >
                              ${
                                escapeHtml(value)
                              }
                            </td>
                          `
                        )
                        .join("")
                    }
                  </tr>
                `
              )
              .join("")
          }
        </tbody>
      </table>
    </div>
  `;
}


function renderThresholdLab(data) {
  state.thresholdLearning =
    data;

  renderThresholdKpis(
    data
  );

  renderCurrentThresholds(
    data
  );

  renderThresholdReadiness(
    data
  );

  renderThresholdComparison(
    data.latest_run
    || null
  );
}


async function loadThresholdLab(
  force = false,
) {
  const now =
    Date.now();

  if (
    !force
    &&
    state.thresholdLearning
    &&
    now
    -
    state.thresholdLearningLastLoaded
    <
    10000
  ) {
    renderThresholdLab(
      state.thresholdLearning
    );

    return;
  }

  try {
    const data =
      await api(
        "/api/v1/threshold-learning/status"
      );

    state.thresholdLearning =
      data;

    state.thresholdLearningLastLoaded =
      now;

    renderThresholdLab(
      data
    );
  } catch (error) {
    $("thresholdTrainMessage").textContent =
      error.message;

    showToast(
      "Threshold Lab unavailable",
      error.message,
    );
  }
}



async function importThresholdHistoricalCsv() {
  const password =
    $("thresholdLabAdminPassword").value;

  const input =
    $("thresholdCsvFile");

  const file =
    input.files?.[0];

  if (!password) {
    $("thresholdImportMessage").textContent =
      "Enter the admin password first.";

    return;
  }

  if (!file) {
    $("thresholdImportMessage").textContent =
      "Choose a marked FALL / NO_FALL CSV first.";

    return;
  }

  if (file.size > 8_000_000) {
    $("thresholdImportMessage").textContent =
      "Keep each CSV below 8 MB.";

    return;
  }

  $("importThresholdCsvButton").disabled =
    true;

  $("thresholdImportMessage").textContent =
    `Reading ${file.name}…`;

  try {
    const csvText =
      await file.text();

    const result =
      await api(
        "/api/v1/threshold-learning/import-csv",
        {
          method: "POST",
          headers: {
            "Content-Type":
              "application/json",
            "X-Admin-Password":
              password,
          },
          body: JSON.stringify(
            {
              filename:
                file.name,
              csv_text:
                csvText,
            }
          ),
        },
      );

    const counts =
      result.class_counts
      || {};

    $("thresholdImportMessage").textContent =
      `Imported ${
        result.inserted_samples
        || 0
      } new training samples (${
        counts.FALL
        || 0
      } FALL, ${
        counts.NO_FALL
        || 0
      } NO_FALL).`;

    showToast(
      "Historical training data imported",
      `${
        result.filename
        || file.name
      } is now available to Threshold Lab.`,
      "success",
    );

    input.value = "";

    state.thresholdLearningLastLoaded =
      0;

    await loadThresholdLab(
      true
    );
  } catch (error) {
    $("thresholdImportMessage").textContent =
      error.message;
  } finally {
    $("importThresholdCsvButton").disabled =
      false;
  }
}


async function clearThresholdHistoricalCsv() {
  const password =
    $("thresholdLabAdminPassword").value;

  if (!password) {
    $("thresholdImportMessage").textContent =
      "Enter the admin password first.";

    return;
  }

  if (
    !window.confirm(
      "Remove all historical CSV training samples? Live incidents and raw sensor data will not be deleted."
    )
  ) {
    return;
  }

  $("clearThresholdCsvButton").disabled =
    true;

  try {
    const result =
      await api(
        "/api/v1/threshold-learning/imported",
        {
          method: "DELETE",
          headers: {
            "X-Admin-Password":
              password,
          },
        },
      );

    $("thresholdImportMessage").textContent =
      `Removed ${
        result.removed_samples
        || 0
      } imported training samples.`;

    state.thresholdLearningLastLoaded =
      0;

    await loadThresholdLab(
      true
    );
  } catch (error) {
    $("thresholdImportMessage").textContent =
      error.message;
  } finally {
    $("clearThresholdCsvButton").disabled =
      false;
  }
}



async function trainThresholdRecommendation() {
  const password =
    $("thresholdLabAdminPassword").value;

  if (!password) {
    $("thresholdTrainMessage").textContent =
      "Enter the admin password first.";

    return;
  }

  $("trainThresholdButton").disabled =
    true;

  $("thresholdTrainMessage").textContent =
    "Training Decision Tree recommendation…";

  try {
    const result =
      await api(
        "/api/v1/threshold-learning/train",
        {
          method: "POST",
          headers: {
            "X-Admin-Password":
              password,
          },
        },
      );

    if (!result.trained) {
      const reasons =
        result.readiness?.reasons
        || [];

      $("thresholdTrainMessage").textContent =
        reasons.join(" ")
        || "More labelled data is required.";

      await loadThresholdLab(true);

      return;
    }

    $("thresholdTrainMessage").textContent =
      `Recommendation trained as run #${
        result.run?.run_id
        || "—"
      }.`;

    showToast(
      "Threshold recommendation ready",
      "The Decision Tree learned new split points from the reviewed data.",
      "success",
    );

    state.thresholdLearningLastLoaded =
      0;

    await loadThresholdLab(true);
  } catch (error) {
    $("thresholdTrainMessage").textContent =
      error.message;
  } finally {
    $("trainThresholdButton").disabled =
      !(
        state.thresholdLearning
        ?.readiness
        ?.ready
      );
  }
}


function countText(value) {
  return Number(value || 0).toLocaleString();
}

function pctText(value, digits = 1) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(digits)}%` : "—";
}

function healthToneFromDelivery(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  if (number >= 95) return "success";
  if (number >= 85) return "warning";
  return "danger";
}

async function loadSystemHealth(force = false) {
  if (!$("healthHours")) {
    return;
  }

  if (
    !force
    && state.health
    && Date.now() - state.healthLastLoaded < 10000
  ) {
    renderSystemHealth();
    return;
  }

  const hours = Number($("healthHours").value || 24);

  try {
    const [quality, storage, verification] = await Promise.all([
      api(`/api/v1/system/quality?hours=${hours}`),
      api(`/api/v1/system/storage?hours=${hours}`),
      api("/api/v1/system/verification"),
    ]);

    state.health = quality;
    state.storage = storage;
    state.verification = verification;
    state.healthLastLoaded = Date.now();
    renderSystemHealth();
  } catch (error) {
    $("qualityKpis").innerHTML = kpiCard(
      "System health",
      "Unavailable",
      error.message,
      "danger",
    );
  }
}

function renderSystemHealth() {
  const quality = state.health?.summary || {};
  const storage = state.storage?.summary || {};
  const verification = state.verification?.summary || {};

  $("qualityKpis").innerHTML = [
    kpiCard(
      "Estimated cloud delivery",
      pctText(quality.estimated_delivery_pct),
      "Received vs expected 20 Hz cloud samples",
      healthToneFromDelivery(quality.estimated_delivery_pct),
    ),
    kpiCard(
      "Observed cloud rate",
      numberText(quality.average_observed_cloud_hz, 1, " Hz"),
      "Average of recent batches",
    ),
    kpiCard(
      "Estimated missing samples",
      countText(quality.estimated_missing_cloud_samples),
      `Across ${countText(quality.batch_count)} batch(es)`,
      Number(quality.estimated_missing_cloud_samples || 0) > 0 ? "warning" : "success",
    ),
    kpiCard(
      "API processing",
      numberText(quality.average_processing_ms, 1, " ms"),
      "Average server processing time",
    ),
  ].join("");

  const devices = state.health?.devices || [];
  $("qualityDeviceList").innerHTML = devices.length
    ? devices.map(device => {
        const received = Number(device.cloud_samples_received || 0);
        const missing = Number(device.estimated_missing_cloud_samples || 0);
        const expected = received + missing;
        const delivery = expected > 0 ? (received / expected) * 100 : null;

        return `
          <div class="health-device-row">
            <div class="health-device-head">
              <div>
                <strong>${escapeHtml(device.worker_id || device.device_name)}</strong>
                <span>${escapeHtml(device.device_name)}</span>
              </div>
              <span>${escapeHtml(timeAgo(device.last_batch))}</span>
            </div>
            <div class="health-device-metrics">
              <div class="health-device-metric">
                <span>Delivery</span>
                <strong>${pctText(delivery)}</strong>
              </div>
              <div class="health-device-metric">
                <span>Cloud rate</span>
                <strong>${numberText(device.observed_cloud_hz, 1, " Hz")}</strong>
              </div>
              <div class="health-device-metric">
                <span>Processing</span>
                <strong>${numberText(device.processing_ms, 1, " ms")}</strong>
              </div>
            </div>
          </div>
        `;
      }).join("")
    : emptyState("No pipeline metrics yet. They will appear after Nesso batches are received.");

  const storageItems = [
    {
      label: "Estimated local IMU samples",
      value: countText(storage.estimated_local_samples),
      progress: 100,
    },
    {
      label: "Cloud samples received",
      value: countText(storage.cloud_samples_received),
      progress: Math.max(0, 100 - Number(storage.local_to_cloud_reduction_pct || 0)),
    },
    {
      label: "Rows stored in sensor_data",
      value: countText(storage.sensor_rows_saved),
      progress: Math.max(0, 100 - Number(storage.estimated_end_to_end_reduction_pct || 0)),
    },
    {
      label: "Estimated end-to-end reduction",
      value: pctText(storage.estimated_end_to_end_reduction_pct),
      progress: Math.min(100, Number(storage.estimated_end_to_end_reduction_pct || 0)),
    },
  ];

  $("storageKpis").innerHTML = storageItems.map(item => `
    <div class="storage-stat">
      <div class="storage-stat-head">
        <span>${escapeHtml(item.label)}</span>
        <strong>${escapeHtml(item.value)}</strong>
      </div>
      <div class="storage-progress">
        <div style="width:${Math.max(0, Math.min(100, item.progress))}%"></div>
      </div>
    </div>
  `).join("");

  $("verificationKpis").innerHTML = [
    ["Total alerts", verification.total_incidents],
    ["Confirmed events", verification.actual_events],
    ["False alarms", verification.false_alarms],
    ["Unsure", verification.unsure],
    ["Verified detection rate", pctText(verification.verified_detection_rate_pct)],
  ].map(([label, value]) => `
    <div class="verification-stat">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value == null ? "—" : value)}</strong>
    </div>
  `).join("");

  $("verificationNote").textContent = state.verification?.note || "";
}


function notificationBool(value) {
  return Boolean(value);
}

function notificationLastSentText(recent) {
  if (!recent?.length) {
    return "No messages yet";
  }

  const successful = recent.find(row => row.success);
  return successful
    ? timeAgo(successful.sent_at)
    : "No successful delivery yet";
}

function renderNotificationPage() {
  const data = state.notifications;

  if (!data) {
    return;
  }

  const telegram = data.telegram || {};
  const settings = data.settings || {};
  const recent = data.recent || [];
  const connected = telegram.configured;
  const botReady = telegram.bot_ready || telegram.bot_token_configured;
  const masterEnabled = settings.telegram_enabled !== false;

  const card = $("telegramConnectionCard");
  card.classList.toggle("connected", botReady && masterEnabled);
  card.classList.toggle("missing", !botReady || !masterEnabled);

  $("telegramConnectionTitle").textContent = connected
    ? (masterEnabled ? "Telegram connected" : "Telegram paused")
    : botReady
      ? "Telegram bot ready"
      : "Telegram setup incomplete";

  $("telegramConnectionSubtitle").textContent = connected
    ? `${telegram.recipient_count || 0} active recipient/group destination(s)`
    : botReady
      ? "Bot token is ready. Connect the first person or group below."
      : "Add TELEGRAM_BOT_TOKEN in Render, then connect recipients from this page.";

  $("telegramNavDot").className = `telegram-nav-dot ${connected && masterEnabled ? "live" : "warning"}`;

  $("notificationKpis").innerHTML = [
    kpiCard(
      "Telegram bot",
      botReady ? "Ready" : "Not configured",
      botReady
        ? (telegram.bot_username ? `@${telegram.bot_username}` : "Bot token configured")
        : "Add only the bot token in Render",
      botReady ? "success" : "warning",
    ),
    kpiCard(
      "Recipients",
      String(telegram.recipient_count || 0),
      "Managed from this website",
    ),
    kpiCard(
      "Critical repeat",
      `${settings.critical_repeat_seconds || 30}s`,
      "FFH repeats only if still unacknowledged",
    ),
    kpiCard(
      "Last delivery",
      notificationLastSentText(recent),
      "Most recent successful Telegram message",
    ),
  ].join("");

  $("notifyTelegramEnabled").checked = notificationBool(settings.telegram_enabled);
  $("notifyNormalEnabled").checked = notificationBool(settings.normal_updates_enabled);
  $("notifyNearMissEnabled").checked = notificationBool(settings.near_miss_enabled);
  $("notifyStfEnabled").checked = notificationBool(settings.stf_enabled);
  $("notifyFfhEnabled").checked = notificationBool(settings.ffh_enabled);
  $("notifyOfflineEnabled").checked = notificationBool(settings.device_offline_enabled);
  $("notifyBatteryEnabled").checked = notificationBool(settings.low_battery_enabled);
  $("notifyRepeatSeconds").value = String(settings.critical_repeat_seconds || 30);

  $("notificationLog").innerHTML = recent.length
    ? recent.map(row => `
        <div class="notification-log-row">
          <span class="notification-log-priority ${escapeHtml(row.priority)}">
            ${escapeHtml(row.priority)}
          </span>

          <div class="notification-log-main">
            <strong>${escapeHtml(row.category)}${row.worker_id ? ` · ${escapeHtml(row.worker_id)}` : ""}${row.recipient_name ? ` → ${escapeHtml(row.recipient_name)}` : ""}</strong>
            <span title="${escapeHtml(row.message_preview || row.error_message || "")}">
              ${escapeHtml(row.message_preview || row.error_message || "No message preview")}
            </span>
          </div>

          <div class="notification-log-result ${row.success ? "success" : "failed"}">
            ${row.success ? "✓ Sent" : "✕ Failed"}<br>
            <span>${escapeHtml(timeAgo(row.sent_at))}</span>
          </div>
        </div>
      `).join("")
    : emptyState("No Telegram messages have been sent yet. Use a test button after configuring the bot.");
}

function telegramAdminPassword() {
  return $("notificationAdminPassword").value;
}

function renderTelegramRecipients() {
  const recipients = state.telegramRecipients || [];
  const container = $("telegramRecipientList");

  if (!recipients.length) {
    container.innerHTML = emptyState(
      "No website-managed recipients are connected yet. Create a pairing code above."
    );
    return;
  }

  container.innerHTML = recipients.map(recipient => {
    const username = recipient.telegram_username
      ? `@${escapeHtml(recipient.telegram_username)}`
      : "No public username";
    const typeLabel = recipient.chat_type === "private" ? "Person" : "Telegram group";

    return `
      <article class="telegram-recipient-card ${recipient.active ? "" : "inactive"}" data-recipient-card="${recipient.id}">
        <div class="telegram-recipient-identity">
          <input
            type="text"
            maxlength="120"
            value="${escapeHtml(recipient.display_name)}"
            aria-label="Recipient display name"
            data-recipient-name="${recipient.id}"
          >
          <div class="telegram-recipient-meta">
            ${escapeHtml(typeLabel)} · ${username} · Telegram ID ending ${escapeHtml(recipient.chat_id_last4 || "—")}
          </div>
        </div>

        <div class="telegram-recipient-levels">
          <label>
            <input type="checkbox" data-recipient-active="${recipient.id}" ${recipient.active ? "checked" : ""}>
            Active
          </label>
          <label>
            <input type="checkbox" data-recipient-normal="${recipient.id}" ${recipient.normal_enabled ? "checked" : ""}>
            Normal
          </label>
          <label>
            <input type="checkbox" data-recipient-urgent="${recipient.id}" ${recipient.urgent_enabled ? "checked" : ""}>
            Urgent
          </label>
          <label>
            <input type="checkbox" data-recipient-critical="${recipient.id}" ${recipient.critical_enabled ? "checked" : ""}>
            Critical
          </label>
        </div>

        <div class="telegram-recipient-actions">
          <button class="secondary-button" data-recipient-test="${recipient.id}">Test</button>
          <button class="secondary-button" data-recipient-save="${recipient.id}">Save</button>
          <button class="danger-button" data-recipient-remove="${recipient.id}">Remove</button>
        </div>
      </article>
    `;
  }).join("");

  container.querySelectorAll("[data-recipient-save]").forEach(button => {
    button.addEventListener("click", () => saveTelegramRecipient(Number(button.dataset.recipientSave)));
  });

  container.querySelectorAll("[data-recipient-test]").forEach(button => {
    button.addEventListener("click", () => sendTelegramTest("NORMAL", Number(button.dataset.recipientTest)));
  });

  container.querySelectorAll("[data-recipient-remove]").forEach(button => {
    button.addEventListener("click", () => removeTelegramRecipient(Number(button.dataset.recipientRemove)));
  });
}

async function loadTelegramRecipients(silent = false) {
  const password = telegramAdminPassword();
  if (!password) {
    $("telegramRecipientMessage").textContent = "Enter the admin password first.";
    return;
  }

  try {
    const data = await api("/api/v1/notifications/recipients", {
      headers: {
        "X-Admin-Password": password,
      },
    });
    state.telegramRecipients = data.recipients || [];
    renderTelegramRecipients();
    if (!silent) {
      $("telegramRecipientMessage").textContent = `Loaded ${state.telegramRecipients.length} connected recipient(s).`;
    }
  } catch (error) {
    $("telegramRecipientMessage").textContent = error.message;
    if (!silent) {
      showToast("Could not load recipients", error.message, "urgent");
    }
  }
}

function renderPairingInstructions(pairing) {
  const box = $("telegramPairingInstructions");
  const isPerson = pairing.recipient_type === "PERSON";
  const botName = pairing.bot_username ? `@${pairing.bot_username}` : "your Nesso Telegram bot";
  const instruction = isPerson
    ? `Open ${botName} privately and press Start. If needed, send ${pairing.command}.`
    : `Open the link to add ${botName} to the Telegram group. If needed, send ${pairing.command} inside that group.`;

  box.className = "pairing-instructions";
  box.innerHTML = `
    <div class="pairing-icon">✓</div>
    <div>
      <strong>Pair ${escapeHtml(pairing.display_name)}</strong>
      <span>${escapeHtml(instruction)}</span>
      <div class="pairing-code">${escapeHtml(pairing.code)}</div>
      <span>This code expires in about 10 minutes. After Telegram confirms the connection, click <strong>Load recipients</strong>.</span>
      <div class="pairing-actions">
        ${pairing.deep_link ? `<a class="primary-button" href="${escapeHtml(pairing.deep_link)}" target="_blank" rel="noopener">Open Telegram</a>` : ""}
        <button id="pairingRefreshRecipientsButton" class="secondary-button">Check connection</button>
      </div>
    </div>
  `;

  $("pairingRefreshRecipientsButton")?.addEventListener("click", async () => {
    await loadTelegramRecipients();
  });
}

async function createTelegramPairing() {
  const password = telegramAdminPassword();
  const displayName = $("newRecipientName").value.trim();

  if (!password) {
    $("telegramRecipientMessage").textContent = "Enter the admin password first.";
    return;
  }

  if (!displayName) {
    $("telegramRecipientMessage").textContent = "Enter a name for the person or group.";
    return;
  }

  const payload = {
    display_name: displayName,
    recipient_type: $("newRecipientType").value,
    normal_enabled: $("newRecipientNormal").checked,
    urgent_enabled: $("newRecipientUrgent").checked,
    critical_enabled: $("newRecipientCritical").checked,
  };

  try {
    const result = await api("/api/v1/notifications/pairings", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Admin-Password": password,
      },
      body: JSON.stringify(payload),
    });

    renderPairingInstructions(result.pairing);
    $("telegramRecipientMessage").textContent = "Pairing code created. Finish the connection in Telegram.";
    showToast("Pairing ready", "Open Telegram and complete the one-time connection.", "success");
  } catch (error) {
    $("telegramRecipientMessage").textContent = error.message;
    showToast("Could not create pairing", error.message, "urgent");
  }
}

function recipientPayload(recipientId) {
  return {
    display_name: document.querySelector(`[data-recipient-name="${recipientId}"]`).value.trim(),
    active: document.querySelector(`[data-recipient-active="${recipientId}"]`).checked,
    normal_enabled: document.querySelector(`[data-recipient-normal="${recipientId}"]`).checked,
    urgent_enabled: document.querySelector(`[data-recipient-urgent="${recipientId}"]`).checked,
    critical_enabled: document.querySelector(`[data-recipient-critical="${recipientId}"]`).checked,
  };
}

async function saveTelegramRecipient(recipientId) {
  const password = telegramAdminPassword();
  if (!password) {
    $("telegramRecipientMessage").textContent = "Enter the admin password first.";
    return;
  }

  try {
    await api(`/api/v1/notifications/recipients/${recipientId}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Admin-Password": password,
      },
      body: JSON.stringify(recipientPayload(recipientId)),
    });
    $("telegramRecipientMessage").textContent = "Recipient alert preferences saved.";
    showToast("Recipient saved", "Telegram alert levels were updated.", "success");
    await Promise.all([loadTelegramRecipients(true), loadNotificationsPage(true)]);
  } catch (error) {
    $("telegramRecipientMessage").textContent = error.message;
    showToast("Could not save recipient", error.message, "urgent");
  }
}

async function removeTelegramRecipient(recipientId) {
  const password = telegramAdminPassword();
  if (!password) {
    $("telegramRecipientMessage").textContent = "Enter the admin password first.";
    return;
  }

  const recipient = (state.telegramRecipients || []).find(row => Number(row.id) === Number(recipientId));
  const label = recipient?.display_name || "this recipient";
  if (!window.confirm(`Stop sending Telegram alerts to ${label}?`)) {
    return;
  }

  try {
    await api(`/api/v1/notifications/recipients/${recipientId}`, {
      method: "DELETE",
      headers: {
        "X-Admin-Password": password,
      },
    });
    $("telegramRecipientMessage").textContent = `${label} was removed.`;
    showToast("Recipient removed", `${label} will no longer receive alerts.`, "success");
    await Promise.all([loadTelegramRecipients(true), loadNotificationsPage(true)]);
  } catch (error) {
    $("telegramRecipientMessage").textContent = error.message;
    showToast("Could not remove recipient", error.message, "urgent");
  }
}

async function loadNotificationsPage(force = false) {
  const now = Date.now();
  if (!force && state.notifications && now - state.notificationsLastLoaded < 5000) {
    renderNotificationPage();
    return;
  }

  try {
    state.notifications = await api("/api/v1/notifications/status");
    state.notificationsLastLoaded = now;
    renderNotificationPage();
  } catch (error) {
    $("notificationSettingsMessage").textContent = `Could not load Telegram settings: ${error.message}`;
    $("telegramConnectionTitle").textContent = "Telegram status unavailable";
    $("telegramConnectionSubtitle").textContent = error.message;
    $("telegramConnectionCard").classList.add("missing");
  }
}

async function saveNotificationSettings() {
  const password = $("notificationAdminPassword").value;

  if (!password) {
    $("notificationSettingsMessage").textContent = "Enter the admin password first.";
    return;
  }

  const payload = {
    telegram_enabled: $("notifyTelegramEnabled").checked,
    normal_updates_enabled: $("notifyNormalEnabled").checked,
    near_miss_enabled: $("notifyNearMissEnabled").checked,
    stf_enabled: $("notifyStfEnabled").checked,
    ffh_enabled: $("notifyFfhEnabled").checked,
    device_offline_enabled: $("notifyOfflineEnabled").checked,
    low_battery_enabled: $("notifyBatteryEnabled").checked,
    critical_repeat_seconds: Number($("notifyRepeatSeconds").value),
  };

  try {
    await api("/api/v1/notifications/settings", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Admin-Password": password,
      },
      body: JSON.stringify(payload),
    });

    $("notificationSettingsMessage").textContent = "Telegram notification settings saved.";
    showToast("Notification settings saved", "Your Telegram alert priorities are updated.", "success");
    await loadNotificationsPage(true);
  } catch (error) {
    $("notificationSettingsMessage").textContent = error.message;
    showToast("Could not save settings", error.message, "urgent");
  }
}

async function sendTelegramTest(priority, recipientId = null) {
  const password = $("notificationAdminPassword").value;

  if (!password) {
    $("notificationSettingsMessage").textContent = "Enter the admin password before sending a test message.";
    return;
  }

  try {
    await api("/api/v1/notifications/test", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Admin-Password": password,
      },
      body: JSON.stringify({ priority, recipient_id: recipientId }),
    });

    const destinationText = recipientId ? "selected recipient" : "enabled recipients";
    $("notificationSettingsMessage").textContent = `${priority} Telegram test queued for ${destinationText}.`;
    showToast("Telegram test queued", `${priority} test is being sent to ${destinationText}.`, "success");
    setTimeout(() => loadNotificationsPage(true), 1200);
  } catch (error) {
    $("notificationSettingsMessage").textContent = error.message;
    showToast("Telegram test failed", error.message, "urgent");
  }
}

function renderAll() {
  updateWelcome();
  renderOverviewKpis();
  renderSiteStatus();
  renderOverviewWorkers();
  renderLatestIncidents();
  renderDeviceHealth();

  renderWorkersPage();
  renderIncidentsPage();

  populateAdminDevices();
  renderAdminIncidents();

  renderNavigationBadge();

  if (
    state.currentPage
    === "analytics"
  ) {
    renderAnalytics();
  }

  if (
    state.currentPage
    === "health"
  ) {
    loadSystemHealth();
  }

  if (
    state.currentPage
    === "notifications"
  ) {
    loadNotificationsPage();
  }

  if (
    state.currentPage
    === "workerDetail"
    && state.selectedDevice
  ) {
    loadWorkerDetail();
  }

  $("sidebarUpdateText").textContent =
    `Updated ${
      timeAgo(
        state.dashboard?.summary
          ?.last_live_update
      )
    }`;
}

async function refreshDashboard() {
  try {
    setConnectionState(
      "connecting",
      "Updating"
    );

    const data =
      await api(
        "/api/v1/dashboard"
      );

    state.dashboard =
      data;

    setConnectionState(
      "live",
      "LIVE"
    );

    if (
      state.connectionWasOffline
    ) {
      showToast(
        "Connection restored",
        "The dashboard is receiving data again.",
        "success",
      );
    }

    state.connectionWasOffline =
      false;

    detectNewIncidents();
    renderAll();
  } catch (error) {
    setConnectionState(
      "offline",
      "OFFLINE"
    );

    $("sidebarUpdateText").textContent =
      "Trying to reconnect";

    if (
      !state.connectionWasOffline
    ) {
      showToast(
        "Dashboard connection lost",
        `${error.message}. Retrying automatically.`,
        "urgent",
      );
    }

    state.connectionWasOffline =
      true;
  }
}

document
  .querySelectorAll(
    ".nav-item"
  )
  .forEach(button => {
    button.addEventListener(
      "click",
      () =>
        switchPage(
          button.dataset.page
        )
    );
  });

document
  .querySelectorAll(
    "[data-go-page]"
  )
  .forEach(button => {
    button.addEventListener(
      "click",
      () =>
        switchPage(
          button.dataset.goPage
        )
    );
  });

[
  "workerSearch",
  "workerConnectionFilter",
  "workerActivityFilter",
].forEach(id => {
  $(id).addEventListener(
    id === "workerSearch"
      ? "input"
      : "change",
    renderWorkersPage,
  );
});

[
  "incidentTypeFilter",
  "incidentSeverityFilter",
  "incidentStatusFilter",
  "incidentWorkerFilter",
].forEach(id => {
  $(id).addEventListener(
    "change",
    renderIncidentsPage,
  );
});

$("technicalDevice")
  .addEventListener(
    "change",
    loadTechnicalPage,
  );

$("adminDevice")
  .addEventListener(
    "change",
    updateAdminWorkerField,
  );

$("saveWorkerButton")
  .addEventListener(
    "click",
    saveWorkerAssignment,
  );

$("backFromWorkerButton")
  .addEventListener(
    "click",
    () =>
      switchPage(
        state.previousPage
        && state.previousPage !== "workerDetail"
          ? state.previousPage
          : "workers"
      ),
  );

$("exportIncidentsButton")
  .addEventListener(
    "click",
    () =>
      downloadRowsAsCsv(
        filteredIncidents(),
        "nesso_incidents.csv",
      ),
  );

$("exportSensorButton")
  .addEventListener(
    "click",
    () =>
      downloadRowsAsCsv(
        state.technicalSensorRows,
        `${
          $("technicalDevice").value
          || "device"
        }_sensor_data.csv`,
      ),
  );

$("exportFeatureButton")
  .addEventListener(
    "click",
    () =>
      downloadRowsAsCsv(
        state.technicalRows,
        `${
          $("technicalDevice").value
          || "device"
        }_features.csv`,
      ),
  );

$("mobileMenuButton")
  .addEventListener(
    "click",
    openSidebar,
  );

$("sidebarBackdrop")
  .addEventListener(
    "click",
    closeSidebar,
  );

$("healthHours")
  .addEventListener(
    "change",
    () => loadSystemHealth(true),
  );

$("incidentModalClose")
  .addEventListener(
    "click",
    closeIncidentReview,
  );

$("incidentModal")
  .addEventListener(
    "click",
    event => {
      if (event.target === $("incidentModal")) {
        closeIncidentReview();
      }
    },
  );

document
  .querySelectorAll(".feedback-button")
  .forEach(button => {
    button.addEventListener("click", () => {
      state.selectedFeedback = button.dataset.feedbackLabel;

      document
        .querySelectorAll(".feedback-button")
        .forEach(other => other.classList.toggle("selected", other === button));

      updateActualEventTypeVisibility();

      $("feedbackMessage").textContent =
        `Selected: ${feedbackLabelText(state.selectedFeedback)}`;
    });
  });

$("actualEventType")
  .addEventListener(
    "change",
    () => {
      state.selectedActualEventType =
        $("actualEventType").value
        || null;
    },
  );

$("saveFeedbackButton")
  .addEventListener(
    "click",
    saveIncidentFeedback,
  );

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !$("incidentModal").classList.contains("hidden")) {
    closeIncidentReview();
  }
});


$("createTelegramPairingButton")
  .addEventListener("click", createTelegramPairing);

$("loadTelegramRecipientsButton")
  .addEventListener("click", () => loadTelegramRecipients());

$("saveNotificationSettingsButton")
  .addEventListener("click", saveNotificationSettings);

$("refreshNotificationsButton")
  .addEventListener("click", () => loadNotificationsPage(true));

document
  .querySelectorAll("[data-telegram-test]")
  .forEach(button => {
    button.addEventListener("click", () => sendTelegramTest(button.dataset.telegramTest));
  });

$("importThresholdCsvButton")
  .addEventListener(
    "click",
    importThresholdHistoricalCsv,
  );

$("clearThresholdCsvButton")
  .addEventListener(
    "click",
    clearThresholdHistoricalCsv,
  );

$("trainThresholdButton")
  .addEventListener(
    "click",
    trainThresholdRecommendation,
  );

$("refreshThresholdButton")
  .addEventListener(
    "click",
    () => loadThresholdLab(true),
  );

switchPage("overview");
refreshDashboard();

state.refreshTimer =
  setInterval(
    refreshDashboard,
    2000,
  );
