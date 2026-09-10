(() => {
  "use strict";

  const files = ["overview", "projects", "daily", "weekly", "monthly", "size_evolution"];
  const charts = {};
  const palette = ["#1a73e8", "#34a853", "#fbbc04", "#ea4335", "#9334e6", "#00acc1", "#fa7b17", "#5f6368"];
  const colors = new Map();
  const formatter = new Intl.NumberFormat("fr-FR");
  const storageKey = "writing-log-preferences-v1";
  let data;
  let selectedProject = "all";
  let productionGranularity = "day";
  const periods = { daily: "30d", productivity: "30d", time: "1y", size: "all" };

  const title = id => data.projects.find(project => project.id === id)?.title || id;
  const color = id => {
    if (!colors.has(id)) colors.set(id, palette[colors.size % palette.length]);
    return colors.get(id);
  };
  const duration = minutes => {
    if (minutes === null || !Number.isFinite(Number(minutes))) return "—";
    const rounded = Math.round(minutes || 0);
    const hours = Math.floor(rounded / 60);
    const rest = rounded % 60;
    return hours ? `${formatter.format(hours)} h ${String(rest).padStart(2, "0")}` : `${rest} min`;
  };

  function latestDate() {
    const values = [...data.daily.map(row => row.periode), ...data.size_evolution.map(row => row.date)].sort();
    return values.at(-1) || new Date().toISOString().slice(0, 10);
  }

  function cutoffDate(period) {
    if (period === "all") return null;
    const date = new Date(`${latestDate()}T12:00:00`);
    if (period === "30d") date.setDate(date.getDate() - 29);
    if (period === "6m") date.setMonth(date.getMonth() - 6);
    if (period === "1y") date.setFullYear(date.getFullYear() - 1);
    return date.toISOString().slice(0, 10);
  }

  function isoWeek(dateString) {
    const date = new Date(`${dateString}T12:00:00Z`);
    date.setUTCDate(date.getUTCDate() + 4 - (date.getUTCDay() || 7));
    const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
    const week = Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
    return `${date.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
  }

  function periodRows(rows, kind, period) {
    const cutoff = cutoffDate(period);
    if (!cutoff) return rows;
    const boundary = kind === "week" ? isoWeek(cutoff) : kind === "month" ? cutoff.slice(0, 7) : cutoff;
    return rows.filter(row => row.periode >= boundary);
  }

  function visibleProjects() {
    return data.projects.filter(project => selectedProject === "all" || project.id === selectedProject);
  }

  function periodTimestamp(period) {
    if (/^\d{4}-W\d{2}$/.test(period)) {
      const [year, week] = period.split("-W").map(Number);
      const januaryFourth = new Date(Date.UTC(year, 0, 4, 12));
      const monday = new Date(januaryFourth);
      monday.setUTCDate(januaryFourth.getUTCDate() - (januaryFourth.getUTCDay() || 7) + 1 + (week - 1) * 7);
      return monday.getTime();
    }
    if (/^\d{4}-\d{2}$/.test(period)) return Date.parse(`${period}-01T12:00:00Z`);
    return Date.parse(`${period}T12:00:00Z`);
  }

  function temporalAxis(rows, field = "periode") {
    const values = rows.map(row => periodTimestamp(row[field])).filter(Number.isFinite).sort((a, b) => a - b);
    if (!values.length) return null;
    const min = values[0];
    const max = values.at(-1);
    const spanDays = Math.max(1, (max - min) / 86400000);
    const unit = spanDays <= 100 ? "week" : spanDays <= 730 ? "month" : "year";
    const cursor = new Date(min);
    cursor.setUTCHours(12, 0, 0, 0);
    if (unit === "week") {
      cursor.setUTCDate(cursor.getUTCDate() + ((8 - (cursor.getUTCDay() || 7)) % 7));
    } else if (unit === "month") {
      cursor.setUTCMonth(cursor.getUTCMonth() + 1, 1);
    } else {
      cursor.setUTCFullYear(cursor.getUTCFullYear() + 1, 0, 1);
    }
    const ticks = [];
    while (cursor.getTime() <= max) {
      ticks.push(cursor.getTime());
      if (unit === "week") cursor.setUTCDate(cursor.getUTCDate() + 7);
      if (unit === "month") cursor.setUTCMonth(cursor.getUTCMonth() + 1, 1);
      if (unit === "year") cursor.setUTCFullYear(cursor.getUTCFullYear() + 1, 0, 1);
    }
    if (!ticks.length) ticks.push(min);
    const weekLabel = value => {
      const date = new Date(value);
      const thursday = new Date(date);
      thursday.setUTCDate(date.getUTCDate() + 4 - (date.getUTCDay() || 7));
      const start = new Date(Date.UTC(thursday.getUTCFullYear(), 0, 1));
      const week = Math.ceil((((thursday - start) / 86400000) + 1) / 7);
      return `S${String(week).padStart(2, "0")}`;
    };
    const label = value => unit === "week"
      ? weekLabel(value)
      : new Intl.DateTimeFormat("fr-FR", unit === "month" ? { month: "short", year: "numeric", timeZone: "UTC" } : { year: "numeric", timeZone: "UTC" }).format(new Date(value));
    return { min, max, ticks, label };
  }

  function commonOptions(stacked = true, temporal = null) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 7, padding: 18, color: "#5f6368" } },
        tooltip: {
          padding: 10,
          callbacks: temporal ? {
            title: items => new Intl.DateTimeFormat("fr-FR", { dateStyle: "long", timeZone: "UTC" }).format(new Date(items[0].parsed.x))
          } : undefined
        }
      },
      scales: {
        x: temporal ? {
          type: "linear",
          stacked,
          min: temporal.min,
          max: temporal.max,
          afterBuildTicks: scale => { scale.ticks = temporal.ticks.map(value => ({ value })); },
          border: { display: false },
          grid: { color: "#dadce0", borderDash: [4, 4], lineWidth: 1 },
          ticks: { maxRotation: 0, color: "#5f6368", callback: value => temporal.label(value) }
        } : { stacked, border: { display: false }, grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, color: "#5f6368" } },
        y: {
          stacked, beginAtZero: true, grace: "8%", border: { display: false },
          grid: {
            color: context => context.tick.value === 0 ? "#9aa0a6" : "#e8eaed",
            lineWidth: context => context.tick.value === 0 ? 2 : 1
          },
          ticks: { color: "#5f6368", callback: value => formatter.format(value) }
        }
      }
    };
  }

  function productionPeriodLabel(period, kind) {
    if (kind === "week") return `Semaine ${period}`;
    if (kind === "month") return new Intl.DateTimeFormat("fr-FR", { month: "long", year: "numeric", timeZone: "UTC" }).format(new Date(`${period}-01T12:00:00Z`));
    return new Intl.DateTimeFormat("fr-FR", { dateStyle: "long", timeZone: "UTC" }).format(new Date(`${period}T12:00:00Z`));
  }

  function stackedChart(canvas, rows, kind) {
    const visibleIds = new Set(visibleProjects().map(project => project.id));
    const visibleRows = rows.filter(row => visibleIds.has(row.projet));
    const temporal = temporalAxis(visibleRows);
    const datasets = visibleProjects().flatMap(project => {
      const rows = visibleRows.filter(row => row.projet === project.id);
      const point = (row, kind) => ({
        x: periodTimestamp(row.periode),
        y: kind === "deleted" ? -(row.signes_supprimes || 0) : row.signes_reels,
        chars: kind === "deleted" ? (row.signes_supprimes || 0) : row.signes_reels,
        activity: row.signes_reels + (row.signes_supprimes || 0),
        kind,
        period: row.periode,
        minutes: row.temps_minutes,
        estimated: Boolean(row.temps_estime),
        folders: row.dossiers || []
      });
      return [
        {
          label: `${project.title} · ajoutés`,
          data: rows.map(row => point(row, "added")),
          backgroundColor: color(project.id),
          borderRadius: 2,
          maxBarThickness: 32
        },
        {
          label: `${project.title} · supprimés`,
          data: rows.map(row => point(row, "deleted")),
          backgroundColor: `${color(project.id)}66`,
          borderColor: color(project.id),
          borderWidth: 1,
          borderRadius: 2,
          maxBarThickness: 32
        }
      ];
    });
    const options = commonOptions(true, temporal);
    if (temporal) {
      const padding = kind === "week" ? 3.5 * 86400000 : kind === "month" ? 15 * 86400000 : 0.5 * 86400000;
      options.scales.x.min = temporal.min - padding;
      options.scales.x.max = temporal.max + padding;
    }
    options.plugins.tooltip.callbacks = {
      title: items => productionPeriodLabel(items[0].raw.period, kind),
      label: context => `${context.raw.kind === "deleted" ? "Supprimés" : "Ajoutés"} : ${context.raw.kind === "deleted" ? "−" : ""}${formatter.format(context.raw.chars)} signes`,
      afterLabel: context => {
        const minutes = context.raw.minutes === null ? null : Number(context.raw.minutes);
        const rate = minutes > 0 ? Math.round(context.raw.activity * 60 / minutes) : null;
        const folders = context.raw.folders.length ? context.raw.folders.join(", ") : "—";
        const timeLabel = minutes === null ? "Temps : inconnu" : `${context.raw.estimated ? "Temps estimé" : "Temps observé"} : ${duration(minutes)}`;
        return [`Dossier : ${folders}`, timeLabel, `Activité : ${rate === null ? "—" : formatter.format(rate)} signes travaillés/heure`];
      }
    };
    return new Chart(canvas, { type: "bar", data: { datasets }, options });
  }

  function filteredDaily(period = "all") {
    return periodRows(data.daily, "day", period).filter(row => selectedProject === "all" || row.projet === selectedProject);
  }

  function renderCards(rows) {
    const timeKnown = rows.every(row => row.temps_minutes !== null);
    const minutes = timeKnown ? rows.reduce((sum, row) => sum + row.temps_minutes, 0) : null;
    document.querySelector("#total-time").textContent = duration(minutes);
    document.querySelector("#total-chars").textContent = formatter.format(rows.reduce((sum, row) => sum + row.signes_reels, 0));
    document.querySelector("#project-count").textContent = formatter.format(visibleProjects().length);
  }

  function savePreferences() {
    try {
      localStorage.setItem(storageKey, JSON.stringify({ project: selectedProject, periods, productionGranularity }));
    } catch (_) { /* Le dashboard reste utilisable si le stockage est désactivé. */ }
  }

  function safeFilename(value) {
    return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function chartPng(canvas) {
    const exported = document.createElement("canvas");
    exported.width = canvas.width;
    exported.height = canvas.height;
    const context = exported.getContext("2d");
    context.fillStyle = "#fff";
    context.fillRect(0, 0, exported.width, exported.height);
    context.drawImage(canvas, 0, 0);
    return exported.toDataURL("image/png");
  }

  function escapeXml(value) {
    return String(value).replace(/[&<>"']/g, character => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&apos;"
    })[character]);
  }

  function svgColor(value, fallback = "#5f6368") {
    return typeof value === "string" ? value : fallback;
  }

  function chartSvg(chart) {
    const width = chart.width;
    const height = chart.height;
    const area = chart.chartArea;
    const parts = [
      `<?xml version="1.0" encoding="UTF-8"?>`,
      `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`,
      `<rect width="100%" height="100%" fill="white"/>`,
      `<g font-family="Arial, Helvetica, sans-serif" font-size="11" fill="#5f6368">`
    ];

    for (const scale of Object.values(chart.scales)) {
      const ticks = scale.ticks || [];
      ticks.forEach((tick, index) => {
        const position = scale.getPixelForTick(index);
        const label = Array.isArray(tick.label) ? tick.label.join(" ") : tick.label;
        if (scale.axis === "x") {
          parts.push(`<line x1="${position}" y1="${area.top}" x2="${position}" y2="${area.bottom}" stroke="#dadce0" stroke-dasharray="4 4"/>`);
          parts.push(`<text x="${position}" y="${Math.min(height - 4, scale.bottom - 3)}" text-anchor="middle">${escapeXml(label ?? "")}</text>`);
        } else {
          if (scale.position !== "right") parts.push(`<line x1="${area.left}" y1="${position}" x2="${area.right}" y2="${position}" stroke="#e8eaed"/>`);
          const right = scale.position === "right";
          const x = right ? scale.right - 2 : scale.left + 2;
          parts.push(`<text x="${x}" y="${position + 4}" text-anchor="${right ? "end" : "start"}">${escapeXml(label ?? "")}</text>`);
        }
      });
    }

    chart.data.datasets.forEach((dataset, datasetIndex) => {
      const meta = chart.getDatasetMeta(datasetIndex);
      if (meta.hidden) return;
      if (meta.type === "bar") {
        meta.data.forEach(element => {
          const horizontal = meta.iScale?.axis === "y";
          const x = horizontal ? Math.min(element.x, element.base) : element.x - element.width / 2;
          const y = horizontal ? element.y - element.height / 2 : Math.min(element.y, element.base);
          const barWidth = horizontal ? Math.abs(element.x - element.base) : element.width;
          const barHeight = horizontal ? element.height : Math.abs(element.y - element.base);
          parts.push(`<rect x="${x}" y="${y}" width="${barWidth}" height="${barHeight}" rx="2" fill="${escapeXml(svgColor(element.options.backgroundColor))}" stroke="${escapeXml(svgColor(element.options.borderColor, "none"))}"/>`);
        });
      }
      if (meta.type === "line") {
        const points = meta.data.filter(point => !point.skip);
        if (points.length) {
          const path = points.map((point, index) => `${index ? "L" : "M"}${point.x} ${point.y}`).join(" ");
          const stroke = svgColor(meta.dataset.options.borderColor, svgColor(dataset.borderColor));
          parts.push(`<path d="${path}" fill="none" stroke="${escapeXml(stroke)}" stroke-width="${meta.dataset.options.borderWidth || 2}" stroke-linejoin="round" stroke-linecap="round"/>`);
          points.forEach(point => {
            const radius = Number(point.options.radius) || 0;
            if (radius > 0) parts.push(`<circle cx="${point.x}" cy="${point.y}" r="${radius}" fill="${escapeXml(svgColor(point.options.backgroundColor, stroke))}"/>`);
          });
        }
      }
    });

    const legend = chart.legend;
    if (legend?.legendItems && legend?.legendHitBoxes) {
      legend.legendItems.forEach((item, index) => {
        const box = legend.legendHitBoxes[index];
        if (!box) return;
        const cy = box.top + box.height / 2;
        parts.push(`<circle cx="${box.left + 5}" cy="${cy}" r="4" fill="${escapeXml(svgColor(item.fillStyle))}"/>`);
        parts.push(`<text x="${box.left + 14}" y="${cy + 4}" fill="#5f6368">${escapeXml(item.text)}</text>`);
      });
    }
    parts.push(`</g></svg>`);
    return parts.join("\n");
  }

  function exportChart(canvas, format) {
    const chart = Chart.getChart(canvas);
    if (!chart) return;
    const heading = canvas.closest(".panel")?.querySelector("h2")?.textContent || "graphique";
    const project = selectedProject === "all" ? "tous-les-projets" : title(selectedProject);
    const filename = `${safeFilename(project)}-${safeFilename(heading)}`;
    if (format === "png") {
      const png = chartPng(canvas);
      const link = document.createElement("a");
      link.href = png;
      link.download = `${filename}.png`;
      link.click();
      return;
    }
    const svg = chartSvg(chart);
    downloadBlob(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }), `${filename}.svg`);
  }

  function installDownloadButtons() {
    document.querySelectorAll(".panel").forEach(panel => {
      const canvas = panel.querySelector("canvas");
      const actions = panel.querySelector(".chart-actions");
      if (!canvas) return;
      if (!actions) return;
      const button = actions.querySelector(".chart-download") || document.createElement("button");
      if (button.dataset.downloadReady === "true") return;
      button.dataset.downloadReady = "true";
      button.type = "button";
      button.className = "chart-download";
      button.title = "Télécharger ce graphique";
      button.setAttribute("aria-label", "Télécharger ce graphique");
      button.setAttribute("aria-expanded", "false");
      const menu = document.createElement("div");
      menu.className = "chart-download-menu";
      menu.hidden = true;
      for (const format of ["PNG", "SVG"]) {
        const choice = document.createElement("button");
        choice.type = "button";
        choice.textContent = format;
        choice.addEventListener("click", () => {
          exportChart(canvas, format.toLowerCase());
          menu.hidden = true;
          button.setAttribute("aria-expanded", "false");
        });
        menu.append(choice);
      }
      button.addEventListener("click", event => {
        event.stopPropagation();
        document.querySelectorAll(".chart-download-menu").forEach(other => {
          if (other !== menu) other.hidden = true;
        });
        menu.hidden = !menu.hidden;
        button.setAttribute("aria-expanded", String(!menu.hidden));
      });
      if (!button.isConnected) actions.append(button);
      actions.append(menu);
    });
    document.addEventListener("click", event => {
      if (event.target.closest(".chart-download-menu")) return;
      document.querySelectorAll(".chart-download-menu").forEach(menu => { menu.hidden = true; });
      document.querySelectorAll(".chart-download").forEach(button => button.setAttribute("aria-expanded", "false"));
    });
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      document.querySelectorAll(".chart-download-menu").forEach(menu => { menu.hidden = true; });
      document.querySelectorAll(".chart-download").forEach(button => button.setAttribute("aria-expanded", "false"));
    });
  }

  function productivityChart(canvas) {
    const visibleIds = new Set(visibleProjects().map(project => project.id));
    const rows = periodRows(data.daily, "day", periods.productivity).filter(row => visibleIds.has(row.projet));
    const days = new Map();
    for (const row of rows) {
      const value = days.get(row.periode) || { signes: 0, minutes: 0, timeKnown: true, estimated: false };
      value.signes += row.signes_reels + (row.signes_supprimes || 0);
      if (row.temps_minutes === null) value.timeKnown = false;
      else value.minutes += row.temps_minutes;
      value.estimated = value.estimated || Boolean(row.temps_estime);
      days.set(row.periode, value);
    }
    const temporal = temporalAxis(rows);
    const entries = [...days.entries()].sort((a, b) => a[0].localeCompare(b[0]));
    const options = commonOptions(false, temporal);
    options.interaction = { mode: "index", intersect: false };
    options.scales.y = {
      type: "linear", position: "left", beginAtZero: true,
      border: { display: false }, grid: { color: "#e8eaed" },
      title: { display: true, text: "Heures" }, ticks: { color: "#5f6368" }
    };
    options.scales.yRate = {
      type: "linear", position: "right", beginAtZero: true,
      border: { display: false }, grid: { drawOnChartArea: false },
      title: { display: true, text: "Signes / heure" }, ticks: { color: "#5f6368", callback: value => formatter.format(value) }
    };
    return new Chart(canvas, {
      data: {
        datasets: [
          {
            type: "bar", label: "Temps disponible",
            data: entries.map(([day, value]) => ({ x: periodTimestamp(day), y: value.timeKnown ? Math.round(value.minutes / 6) / 10 : null, estimated: value.estimated })),
            backgroundColor: "rgba(26, 115, 232, .3)", borderColor: "#1a73e8", borderWidth: 1, borderRadius: 2, yAxisID: "y"
          },
          {
            type: "line", label: "Signes travaillés / heure",
            data: entries.map(([day, value]) => ({ x: periodTimestamp(day), y: value.timeKnown && value.minutes > 0 ? Math.round(value.signes * 60 / value.minutes) : null })),
            borderColor: "#ea4335", backgroundColor: "#ea4335", pointRadius: entries.length > 80 ? 1 : 3,
            pointHoverRadius: 5, tension: .12, yAxisID: "yRate"
          }
        ]
      },
      options
    });
  }

  function filteredSizes(period) {
    const cutoff = cutoffDate(period);
    const result = [];
    for (const project of visibleProjects()) {
      const rows = data.size_evolution.filter(row => row.projet === project.id).sort((a, b) => a.date.localeCompare(b.date));
      if (!cutoff) {
        result.push(...rows);
        continue;
      }
      const before = rows.filter(row => row.date < cutoff).at(-1);
      if (before) result.push({ ...before, date: cutoff });
      result.push(...rows.filter(row => row.date >= cutoff));
    }
    return result;
  }

  function render() {
    Object.values(charts).forEach(chart => chart.destroy());
    renderCards(filteredDaily("all"));
    const selectedTitle = document.querySelector("#selected-project-title");
    selectedTitle.hidden = selectedProject === "all";
    selectedTitle.textContent = selectedProject === "all" ? "" : title(selectedProject);
    const productionData = productionGranularity === "week" ? data.weekly : productionGranularity === "month" ? data.monthly : data.daily;
    charts.daily = stackedChart(document.querySelector("#daily-chart"), periodRows(productionData, productionGranularity, periods.daily), productionGranularity);
    charts.productivity = productivityChart(document.querySelector("#productivity-chart"));

    const timePanel = document.querySelector("#time-panel");
    timePanel.hidden = selectedProject !== "all";
    if (selectedProject === "all") {
      const timeRows = filteredDaily(periods.time);
      const minutes = new Map();
      for (const row of timeRows) {
        const value = minutes.get(row.projet) || { total: 0, known: true };
        if (row.temps_minutes === null) value.known = false;
        else value.total += row.temps_minutes;
        minutes.set(row.projet, value);
      }
      const projects = visibleProjects().slice().sort((a, b) => (minutes.get(b.id)?.total || 0) - (minutes.get(a.id)?.total || 0));
      const timeOptions = commonOptions(false);
      timeOptions.indexAxis = "y";
      timeOptions.plugins.legend.display = false;
      timeOptions.plugins.tooltip.callbacks = { label: context => duration(context.raw) };
      timeOptions.scales.x.ticks.callback = value => duration(value);
      charts.time = new Chart(document.querySelector("#time-chart"), {
        type: "bar",
        data: { labels: projects.map(project => project.title), datasets: [{ data: projects.map(project => minutes.get(project.id)?.known ? minutes.get(project.id).total : null), backgroundColor: projects.map(project => color(project.id)), borderRadius: 2 }] },
        options: timeOptions
      });
    }

    const sizes = filteredSizes(periods.size);
    const sizeTemporal = temporalAxis(sizes, "date");
    const sizeDatasets = visibleProjects().map(project => {
      return {
        label: project.title,
        data: sizes.filter(row => row.projet === project.id).map(row => ({ x: periodTimestamp(row.date), y: row.taille_signes })),
        borderColor: color(project.id),
        pointRadius: sizes.length > 100 ? 0 : 2,
        pointHoverRadius: 5,
        spanGaps: true,
        tension: .12
      };
    });
    const sizeOptions = commonOptions(false, sizeTemporal);
    sizeOptions.elements = { line: { borderWidth: 2 } };
    charts.size = new Chart(document.querySelector("#size-chart"), { type: "line", data: { datasets: sizeDatasets }, options: sizeOptions });
  }

  async function start() {
    try {
      const values = await Promise.all(files.map(async name => {
        const response = await fetch(`data/${name}.json`, { cache: "no-store" });
        if (!response.ok) throw new Error(`${name}.json : HTTP ${response.status}`);
        return response.json();
      }));
      data = Object.fromEntries(files.map((name, index) => [name, values[index]]));
      data.projects.forEach(project => color(project.id));
      try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
        if (saved?.project === "all" || data.projects.some(project => project.id === saved?.project)) selectedProject = saved.project;
        if (["day", "week", "month"].includes(saved?.productionGranularity)) productionGranularity = saved.productionGranularity;
        if (saved?.periods) {
          for (const key of Object.keys(periods)) {
            if (["30d", "6m", "1y", "all"].includes(saved.periods[key])) periods[key] = saved.periods[key];
          }
        }
      } catch (_) { /* Préférences absentes ou anciennes : conserver les valeurs par défaut. */ }
      const projectFilter = document.querySelector("#project-filter");
      data.projects.forEach(project => projectFilter.add(new Option(project.title, project.id)));
      projectFilter.value = selectedProject;
      projectFilter.addEventListener("change", event => { selectedProject = event.target.value; savePreferences(); render(); });
      const granularity = document.querySelector("#production-granularity");
      granularity.value = productionGranularity;
      granularity.addEventListener("change", event => { productionGranularity = event.target.value; savePreferences(); render(); });
      document.querySelectorAll(".chart-range").forEach(select => {
        select.value = periods[select.dataset.chart];
        select.addEventListener("change", event => { periods[event.target.dataset.chart] = event.target.value; savePreferences(); render(); });
      });
      document.querySelector("#last-update").textContent = new Intl.DateTimeFormat("fr-FR", { dateStyle: "long", timeStyle: "short" }).format(new Date(data.overview.derniere_mise_a_jour));
      render();
      installDownloadButtons();
    } catch (error) {
      const box = document.querySelector("#error");
      box.hidden = false;
      box.textContent = `Impossible de charger les données (${error.message}). Lancez scripts/serve_local.py.`;
    }
  }

  window.addEventListener("DOMContentLoaded", start);
})();
