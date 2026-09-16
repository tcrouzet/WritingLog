(() => {
  "use strict";

  const files = ["overview", "projects", "daily", "weekly", "monthly", "size_evolution"];
  const charts = {};
  const palette = ["#1a73e8", "#34a853", "#fbbc04", "#ea4335", "#9334e6", "#00acc1", "#fa7b17", "#5f6368"];
  const colors = new Map();
  const formatter = new Intl.NumberFormat("fr-FR");
  const storageKey = "writing-log-preferences-v1";
  const zoomLevels = [1, 1.5, 2, 3, 4, 6];
  let data;
  let selectedProject = "all";
  let productionGranularity = "day";
  let sizeGranularity = "day";
  const zooms = { daily: 1, size: 1 };
  const weekdayLabels = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"];

  const title = id => data.projects.find(project => project.id === id)?.title || id;
  const color = id => {
    if (!colors.has(id)) colors.set(id, palette[colors.size % palette.length]);
    return colors.get(id);
  };
  function latestDate() {
    const values = [...data.daily.map(row => row.periode), ...data.size_evolution.map(row => row.date)].sort();
    return values.at(-1) || new Date().toISOString().slice(0, 10);
  }

  function isoWeek(dateString) {
    const date = new Date(`${dateString}T12:00:00Z`);
    date.setUTCDate(date.getUTCDate() + 4 - (date.getUTCDay() || 7));
    const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
    const week = Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
    return `${date.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
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
    if (/^\d{4}-\d{2}-\d{2}$/.test(period)) return Date.parse(`${period}T12:00:00Z`);
    return Date.parse(period);
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

  function productionChart(canvas, rows, kind) {
    const visibleIds = new Set(visibleProjects().map(project => project.id));
    const visibleRows = rows.filter(row => visibleIds.has(row.projet));
    const temporal = temporalAxis(visibleRows);
    const datasets = visibleProjects().map(project => {
      const rows = visibleRows.filter(row => row.projet === project.id);
      const point = row => ({
        x: periodTimestamp(row.periode),
        y: row.signes_reels,
        chars: row.signes_reels,
        period: row.periode,
        folders: row.dossiers || []
      });
      return {
        label: project.title,
        data: rows.map(point),
        backgroundColor: color(project.id),
        borderRadius: 2,
        maxBarThickness: 32
      };
    });
    const options = commonOptions(true, temporal);
    if (temporal) {
      const padding = kind === "week" ? 3.5 * 86400000 : kind === "month" ? 15 * 86400000 : 0.5 * 86400000;
      options.scales.x.min = temporal.min - padding;
      options.scales.x.max = temporal.max + padding;
    }
    options.plugins.tooltip.callbacks = {
      title: items => productionPeriodLabel(items[0].raw.period, kind),
      label: context => `Produits : ${formatter.format(context.raw.chars)} signes`,
      afterLabel: context => {
        const folders = context.raw.folders.length ? context.raw.folders.join(", ") : "—";
        return `Dossier analysé : ${folders}`;
      }
    };
    return new Chart(canvas, { type: "bar", data: { datasets }, options });
  }

  function weekdayIndex(dateString) {
    return (new Date(`${dateString}T12:00:00Z`).getUTCDay() + 6) % 7;
  }

  function weekdayOccurrences(firstDay, lastDay) {
    const counts = Array(7).fill(0);
    if (!firstDay || !lastDay || firstDay > lastDay) return counts;
    const cursor = new Date(`${firstDay}T12:00:00Z`);
    const end = new Date(`${lastDay}T12:00:00Z`);
    while (cursor <= end) {
      counts[(cursor.getUTCDay() + 6) % 7] += 1;
      cursor.setUTCDate(cursor.getUTCDate() + 1);
    }
    return counts;
  }

  function weekdayChart(canvas) {
    const lastDay = latestDate().slice(0, 10);
    const datasets = visibleProjects().map(project => {
      const allRows = data.daily.filter(row => row.projet === project.id).sort((a, b) => a.periode.localeCompare(b.periode));
      const projectStart = allRows[0]?.periode || lastDay;
      const occurrences = weekdayOccurrences(projectStart, lastDay);
      const totals = Array(7).fill(0);
      const activeDays = Array(7).fill(0);
      for (const row of allRows) {
        const index = weekdayIndex(row.periode);
        totals[index] += Number(row.signes_reels) || 0;
        if (row.signes_reels > 0) activeDays[index] += 1;
      }
      return {
        label: project.title,
        data: totals.map((total, index) => ({
          x: weekdayLabels[index],
          y: total,
          total,
          occurrences: occurrences[index],
          activeDays: activeDays[index],
          average: occurrences[index] ? Math.round(total / occurrences[index]) : 0
        })),
        backgroundColor: color(project.id),
        borderRadius: 2,
        maxBarThickness: 90
      };
    });
    const options = commonOptions(true);
    options.plugins.tooltip.callbacks = {
      title: items => items[0].raw.x,
      label: context => `${context.dataset.label} : ${formatter.format(context.raw.total)} signes`,
      afterLabel: context => [
        `Moyenne : ${formatter.format(context.raw.average)} signes par ${context.raw.x.toLowerCase()}`,
        `Jours actifs : ${formatter.format(context.raw.activeDays)} sur ${formatter.format(context.raw.occurrences)}`
      ]
    };
    return new Chart(canvas, { type: "bar", data: { labels: weekdayLabels, datasets }, options });
  }

  function filteredDaily() {
    return data.daily.filter(row => selectedProject === "all" || row.projet === selectedProject);
  }

  function renderCards(rows) {
    document.querySelector("#total-chars").textContent = formatter.format(rows.reduce((sum, row) => sum + row.signes_reels, 0));
    document.querySelector("#project-count").textContent = formatter.format(visibleProjects().length);
  }

  function savePreferences() {
    try {
      localStorage.setItem(storageKey, JSON.stringify({ project: selectedProject, productionGranularity, sizeGranularity, zooms }));
    } catch (_) { /* Le dashboard reste utilisable si le stockage est désactivé. */ }
  }

  function applyChartZoom(name, keepCenter = false) {
    const viewport = document.querySelector(`[data-chart-scroll="${name}"]`);
    const stage = document.querySelector(`[data-chart-stage="${name}"]`);
    if (!viewport || !stage) return;
    const center = viewport.scrollWidth
      ? (viewport.scrollLeft + viewport.clientWidth / 2) / viewport.scrollWidth
      : 0.5;
    stage.style.width = `${zooms[name] * 100}%`;
    document.querySelectorAll(`[data-chart-zoom="${name}"]`).forEach(button => {
      const limit = button.dataset.direction === "out" ? zoomLevels[0] : zoomLevels.at(-1);
      button.disabled = zooms[name] === limit;
    });
    requestAnimationFrame(() => {
      charts[name]?.resize();
      if (keepCenter) {
        viewport.scrollLeft = Math.max(0, center * viewport.scrollWidth - viewport.clientWidth / 2);
      }
    });
  }

  function changeChartZoom(name, direction) {
    const current = zoomLevels.indexOf(zooms[name]);
    const offset = direction === "in" ? 1 : -1;
    const next = Math.max(0, Math.min(zoomLevels.length - 1, current + offset));
    if (next === current) return;
    zooms[name] = zoomLevels[next];
    applyChartZoom(name, true);
    savePreferences();
  }

  function installZoomButtons() {
    document.querySelectorAll("[data-chart-zoom]").forEach(button => {
      button.addEventListener("click", () => {
        changeChartZoom(button.dataset.chartZoom, button.dataset.direction);
      });
    });
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

  function sizeRows(granularity) {
    const result = [];
    for (const project of visibleProjects()) {
      const rows = data.size_evolution.filter(row => row.projet === project.id).sort((a, b) => a.date.localeCompare(b.date));
      const buckets = new Map();
      for (const row of rows) {
        const day = row.date.slice(0, 10);
        const bucket = granularity === "year"
          ? day.slice(0, 4)
          : granularity === "month"
            ? day.slice(0, 7)
            : granularity === "week" ? isoWeek(day) : day;
        const previous = buckets.get(bucket);
        buckets.set(bucket, {
          ...row,
          modifie: Boolean(row.modifie || previous?.modifie)
        });
      }
      result.push(...buckets.values());
    }
    return result;
  }

  function render() {
    Object.values(charts).forEach(chart => chart.destroy());
    renderCards(filteredDaily());
    const selectedTitle = document.querySelector("#selected-project-title");
    selectedTitle.hidden = selectedProject === "all";
    selectedTitle.textContent = selectedProject === "all" ? "" : title(selectedProject);
    const productionData = productionGranularity === "week" ? data.weekly : productionGranularity === "month" ? data.monthly : data.daily;
    charts.daily = productionChart(document.querySelector("#daily-chart"), productionData, productionGranularity);
    charts.weekday = weekdayChart(document.querySelector("#weekday-chart"));
    const sizes = sizeRows(sizeGranularity);
    const sizeTemporal = temporalAxis(sizes, "date");
    const sizeDatasets = visibleProjects().map(project => {
      return {
        label: project.title,
        data: sizes.filter(row => row.projet === project.id).map(row => ({
          x: periodTimestamp(row.date),
          y: row.taille_signes,
          rawSize: row.taille_brute ?? row.taille_signes,
          timestamp: row.date,
          commit: row.commit || "",
          sourceCommit: row.source_commit || "",
          folder: row.dossier || "",
          estimated: Boolean(row.estime),
          touched: Boolean(row.modifie)
        })),
        borderColor: color(project.id),
        pointRadius: context => context.raw?.touched ? 1.75 : 0.5,
        pointHoverRadius: 5,
        spanGaps: false,
        stepped: "after",
        tension: 0
      };
    });
    const sizeOptions = commonOptions(false, sizeTemporal);
    sizeOptions.scales.y.beginAtZero = false;
    sizeOptions.elements = { line: { borderWidth: 2 } };
    sizeOptions.plugins.tooltip.callbacks = {
      title: items => new Intl.DateTimeFormat("fr-FR", {
        dateStyle: "long", timeStyle: "medium"
      }).format(new Date(items[0].raw.timestamp)),
      label: context => `Taille : ${formatter.format(context.raw.y)} signes`,
      afterLabel: context => context.raw.estimated
        ? [
            `Estimation quotidienne vers le commit : ${context.raw.sourceCommit.slice(0, 12)}`,
            `Dossier analysé : ${context.raw.folder || "—"}`
          ]
        : context.raw.commit ? [
            `Commit : ${context.raw.commit.slice(0, 12)}`,
            `Dossier analysé : ${context.raw.folder || "—"}`,
            context.raw.touched ? "Projet modifié" : "Taille inchangée",
            ...(context.raw.rawSize !== context.raw.y
              ? [`Mesure brute : ${formatter.format(context.raw.rawSize)} signes`]
              : [])
          ]
        : "Valeur au début de la période"
    };
    charts.size = new Chart(document.querySelector("#size-chart"), { type: "line", data: { datasets: sizeDatasets }, options: sizeOptions });
    applyChartZoom("daily");
    applyChartZoom("size");
  }

  async function start() {
    try {
      // Une URL différente à chaque chargement empêche le navigateur comme un
      // éventuel hébergeur statique de resservir les anciens JSON.
      const dataVersion = Date.now().toString(36);
      const values = await Promise.all(files.map(async name => {
        const response = await fetch(`data/${name}.json?v=${dataVersion}`, { cache: "no-store" });
        if (!response.ok) throw new Error(`${name}.json : HTTP ${response.status}`);
        return response.json();
      }));
      data = Object.fromEntries(files.map((name, index) => [name, values[index]]));
      data.projects.forEach(project => color(project.id));
      try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
        if (saved?.project === "all" || data.projects.some(project => project.id === saved?.project)) selectedProject = saved.project;
        if (["day", "week", "month"].includes(saved?.productionGranularity)) productionGranularity = saved.productionGranularity;
        if (["day", "week", "month", "year"].includes(saved?.sizeGranularity)) sizeGranularity = saved.sizeGranularity;
        if (saved?.zooms) {
          for (const key of Object.keys(zooms)) {
            if (zoomLevels.includes(saved.zooms[key])) zooms[key] = saved.zooms[key];
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
      const sizeGranularitySelect = document.querySelector("#size-granularity");
      sizeGranularitySelect.value = sizeGranularity;
      sizeGranularitySelect.addEventListener("change", event => { sizeGranularity = event.target.value; savePreferences(); render(); });
      document.querySelector("#last-update").textContent = new Intl.DateTimeFormat("fr-FR", { dateStyle: "long", timeStyle: "short" }).format(new Date(data.overview.derniere_mise_a_jour));
      render();
      installZoomButtons();
      installDownloadButtons();
    } catch (error) {
      const box = document.querySelector("#error");
      box.hidden = false;
      box.textContent = `Impossible de charger les données (${error.message}). Lancez scripts/serve_local.py.`;
    }
  }

  window.addEventListener("DOMContentLoaded", start);
})();
