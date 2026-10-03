(() => {
  "use strict";

  const files = ["overview", "projects", "daily", "weekly", "monthly", "size_evolution", "files", "file_daily", "file_weekly", "file_monthly", "file_size_evolution", "file_activity"];
  const charts = {};
  const palette = ["#1a73e8", "#34a853", "#fbbc04", "#ea4335", "#9334e6", "#00acc1", "#fa7b17", "#5f6368"];
  const colors = new Map();
  const formatter = new Intl.NumberFormat("fr-FR");
  const storageKey = "writing-log-preferences-v1";
  const zoomLevels = [1, 1.5, 2, 3, 4, 6];
  const parisHourFormatter = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Paris", hour: "2-digit", hourCycle: "h23"
  });
  let data;
  let selectedProject = "all";
  let productionGranularity = "day";
  let sizeGranularity = "day";
  let activityCellMinutes = 60;
  const zooms = { daily: 1, size: 1 };
  const activityResolutions = [60, 30, 15];
  const weekdayLabels = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"];
  const isDayBoundary = context => context.tick?.value !== undefined
    && Number(parisHourFormatter.format(new Date(context.tick.value))) === 0;

  const selectionKind = () => selectedProject.startsWith("file:") ? "file" : selectedProject === "all" ? "all" : "project";
  const selectionId = () => selectedProject.replace(/^(project|file):/, "");
  const title = value => {
    const id = value.replace(/^(project|file):/, "");
    const items = value.startsWith("file:") ? data.files : data.projects;
    return items.find(item => item.id === id)?.title || id;
  };
  const color = id => {
    if (id === "__all__") return palette[0];
    if (!colors.has(id)) colors.set(id, palette[colors.size % palette.length]);
    return colors.get(id);
  };
  function latestDate() {
    const values = selectionKind() === "file"
      ? [
          ...data.file_daily.filter(row => row.fichier === selectionId()).map(row => row.periode),
          ...data.file_size_evolution.filter(row => row.fichier === selectionId()).map(row => row.date)
        ].sort()
      : [...data.daily.map(row => row.periode), ...data.size_evolution.map(row => row.date)].sort();
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
    if (selectionKind() === "file") return [];
    return data.projects.filter(project => selectedProject === "all" || project.id === selectionId());
  }

  function selectedFile() {
    return selectionKind() === "file" ? data.files.find(file => file.id === selectionId()) : null;
  }

  function productionRows(kind) {
    if (selectionKind() !== "file") return kind === "week" ? data.weekly : kind === "month" ? data.monthly : data.daily;
    if (kind === "minute" || kind === "hour") {
      const buckets = new Map();
      for (const row of data.file_activity.filter(item => item.fichier === selectionId())) {
        const offset = row.timestamp.slice(19);
        const period = kind === "hour"
          ? `${row.timestamp.slice(0, 13)}:00:00${offset}`
          : `${row.timestamp.slice(0, 16)}:00${offset}`;
        const bucket = buckets.get(period) || { periode: period, projet: row.fichier, signes_reels: 0, dossiers: new Set() };
        bucket.signes_reels += Number(row.signes_reels) || 0;
        for (const path of row.chemins || []) bucket.dossiers.add(path);
        buckets.set(period, bucket);
      }
      return [...buckets.values()].map(row => ({ ...row, dossiers: [...row.dossiers].sort() }));
    }
    const rows = kind === "week" ? data.file_weekly : kind === "month" ? data.file_monthly : data.file_daily;
    return rows.map(row => ({ ...row, projet: row.fichier, dossiers: row.chemins || [] }));
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

  function temporalAxis(rows, field = "periode", precision = null, zoomName = null) {
    const values = rows.map(row => periodTimestamp(row[field])).filter(Number.isFinite).sort((a, b) => a - b);
    if (!values.length) return null;
    const min = values[0];
    const max = values.at(-1);
    const spanDays = Math.max(1, (max - min) / 86400000);
    const unit = precision === "minute" || precision === "hour"
      ? "hour"
      : precision === "day" && selectionKind() === "file"
        ? "day"
      : spanDays <= 100 ? "week" : spanDays <= 730 ? "month" : "year";
    const cursor = new Date(min);
    if (unit === "hour") {
      cursor.setUTCHours(cursor.getUTCHours() + 1, 0, 0, 0);
    } else if (unit === "day") {
      cursor.setUTCHours(12, 0, 0, 0);
      if (cursor.getTime() < min) cursor.setUTCDate(cursor.getUTCDate() + 1);
    } else if (unit === "week") {
      cursor.setUTCHours(12, 0, 0, 0);
      cursor.setUTCDate(cursor.getUTCDate() + ((8 - (cursor.getUTCDay() || 7)) % 7));
    } else if (unit === "month") {
      cursor.setUTCHours(12, 0, 0, 0);
      cursor.setUTCMonth(cursor.getUTCMonth() + 1, 1);
    } else {
      cursor.setUTCHours(12, 0, 0, 0);
      cursor.setUTCFullYear(cursor.getUTCFullYear() + 1, 0, 1);
    }
    const ticks = [];
    while (cursor.getTime() <= max) {
      ticks.push(cursor.getTime());
      if (unit === "hour") cursor.setUTCHours(cursor.getUTCHours() + 1);
      if (unit === "day") cursor.setUTCDate(cursor.getUTCDate() + 1);
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
    const label = value => unit === "hour"
        ? (() => {
          const date = new Date(value);
          const hour = Number(parisHourFormatter.format(date));
          const zoom = zoomName ? zooms[zoomName] : 1;
          const interval = zoom >= 4 ? 1 : zoom >= 2 ? 2 : 6;
          if (hour === 0) return new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "short", timeZone: "Europe/Paris" }).format(date);
          return hour % interval === 0 ? `${hour} h` : "";
        })()
      : unit === "day"
        ? new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "short", timeZone: "Europe/Paris" }).format(new Date(value))
        : unit === "week"
          ? weekLabel(value)
          : new Intl.DateTimeFormat("fr-FR", unit === "month" ? { month: "short", year: "numeric", timeZone: "UTC" } : { year: "numeric", timeZone: "UTC" }).format(new Date(value));
    return { min, max, ticks, label, unit };
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
            title: items => new Intl.DateTimeFormat("fr-FR", { dateStyle: "long", timeZone: "Europe/Paris" }).format(new Date(items[0].parsed.x))
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
          grid: {
            color: context => temporal.unit === "day" || isDayBoundary(context) ? "#9aa0a6" : "#e8eaed",
            borderDash: [],
            lineWidth: context => temporal.unit === "day" || isDayBoundary(context) ? 1.5 : 1
          },
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
    if (kind === "minute" || kind === "hour") return new Intl.DateTimeFormat("fr-FR", { dateStyle: "long", timeStyle: "short", timeZone: "Europe/Paris" }).format(new Date(period));
    if (kind === "week") return `Semaine ${period}`;
    if (kind === "month") return new Intl.DateTimeFormat("fr-FR", { month: "long", year: "numeric", timeZone: "UTC" }).format(new Date(`${period}-01T12:00:00Z`));
    return new Intl.DateTimeFormat("fr-FR", { dateStyle: "long", timeZone: "UTC" }).format(new Date(`${period}T12:00:00Z`));
  }

  function combinedProductionRows(rows) {
    const buckets = new Map();
    for (const row of rows) {
      const bucket = buckets.get(row.periode) || {
        periode: row.periode,
        signes_reels: 0,
        dossiers: new Set()
      };
      bucket.signes_reels += Number(row.signes_reels) || 0;
      for (const folder of row.dossiers || []) bucket.dossiers.add(folder);
      buckets.set(row.periode, bucket);
    }
    return [...buckets.values()].map(row => ({ ...row, dossiers: [...row.dossiers].sort() }));
  }

  function productionChart(canvas, rows, kind) {
    const file = selectedFile();
    const visibleIds = new Set(file ? [file.id] : visibleProjects().map(project => project.id));
    const visibleRows = rows.filter(row => visibleIds.has(row.projet));
    const series = selectedProject === "all"
      ? [{ id: "__all__", title: "Tous les projets", rows: combinedProductionRows(visibleRows) }]
      : (file ? [file] : visibleProjects()).map(project => ({
          id: project.id,
          title: project.title,
          rows: visibleRows.filter(row => row.projet === project.id)
        }));
    const temporal = temporalAxis(series.flatMap(item => item.rows), "periode", kind, "daily");
    const datasets = series.map(item => {
      const point = row => ({
        x: periodTimestamp(row.periode),
        y: row.signes_reels,
        chars: row.signes_reels,
        period: row.periode,
        folders: row.dossiers || []
      });
      return {
        label: item.title,
        data: item.rows.map(point),
        backgroundColor: color(item.id),
        borderRadius: 2,
        maxBarThickness: 32
      };
    });
    const options = commonOptions(true, temporal);
    if (temporal) {
      const padding = kind === "minute" ? 30000 : kind === "hour" ? 1800000 : kind === "week" ? 3.5 * 86400000 : kind === "month" ? 15 * 86400000 : 0.5 * 86400000;
      options.scales.x.min = temporal.min - padding;
      options.scales.x.max = temporal.max + padding;
    }
    options.plugins.tooltip.callbacks = {
      title: items => productionPeriodLabel(items[0].raw.period, kind),
      label: context => `Produits : ${formatter.format(context.raw.chars)} signes`,
      afterLabel: context => {
        if (selectionKind() === "file") return [];
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
    const series = selectedProject === "all"
      ? [{ id: "__all__", title: "Tous les projets", rows: combinedProductionRows(data.daily) }]
      : (selectedFile() ? [selectedFile()] : visibleProjects()).map(project => ({
          id: project.id,
          title: project.title,
          rows: productionRows("day").filter(row => row.projet === project.id)
        }));
    const datasets = series.map(item => {
      const allRows = item.rows.sort((a, b) => a.periode.localeCompare(b.periode));
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
        label: item.title,
        data: totals.map((total, index) => ({
          x: weekdayLabels[index],
          y: total,
          total,
          occurrences: occurrences[index],
          activeDays: activeDays[index],
          average: occurrences[index] ? Math.round(total / occurrences[index]) : 0
        })),
        backgroundColor: color(item.id),
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

  function fileActivityGrid(svg) {
    const rows = data.file_activity
      .filter(row => row.fichier === selectionId())
      .sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
    const buckets = new Map();
    for (const row of rows) {
      const day = row.timestamp.slice(0, 10);
      const hour = Number(row.timestamp.slice(11, 13));
      const minute = Number(row.timestamp.slice(14, 16));
      const slot = Math.floor((hour * 60 + minute) / activityCellMinutes);
      const key = `${day}:${slot}`;
      const bucket = buckets.get(key) || { day, slot, chars: 0 };
      bucket.chars += Number(row.signes_reels) || 0;
      buckets.set(key, bucket);
    }
    const days = [];
    if (rows.length) {
      const cursor = new Date(`${rows[0].timestamp.slice(0, 10)}T12:00:00Z`);
      const end = new Date(`${rows.at(-1).timestamp.slice(0, 10)}T12:00:00Z`);
      while (cursor <= end) {
        days.push(cursor.toISOString().slice(0, 10));
        cursor.setUTCDate(cursor.getUTCDate() + 1);
      }
    }
    const maximum = Math.max(1, ...[...buckets.values()].map(bucket => bucket.chars));
    const namespace = "http://www.w3.org/2000/svg";
    const layout = {
      60: { columns: 4, rows: 6 },
      30: { columns: 6, rows: 8 },
      15: { columns: 8, rows: 12 }
    }[activityCellMinutes];
    const columnsPerDay = layout.columns;
    const rowsPerColumn = layout.rows;
    const slotsPerDay = columnsPerDay * rowsPerColumn;
    const dayWidth = 88;
    const cellWidth = dayWidth / columnsPerDay;
    const cellHeight = cellWidth;
    const gridHeight = rowsPerColumn * cellHeight;
    const width = Math.max(1, days.length * dayWidth);
    const height = gridHeight + 26;
    let producedSoFar = 0;
    const cumulativeByKey = new Map();
    for (const day of days) {
      for (let slot = 0; slot < slotsPerDay; slot += 1) {
        const key = `${day}:${slot}`;
        const bucket = buckets.get(key);
        producedSoFar += bucket?.chars || 0;
        cumulativeByKey.set(key, producedSoFar);
      }
    }
    const cadence = writingCadence(rows);
    const writingMinutesAtEvent = new Map();
    let writingMinutes = 0;
    let previousTimestamp = null;
    for (const row of rows) {
      const timestamp = Date.parse(row.timestamp);
      const gap = previousTimestamp === null ? cadence : (timestamp - previousTimestamp) / 60000;
      if (cadence !== null) writingMinutes += previousTimestamp === null || gap > cadence * 4 ? cadence : gap;
      const day = row.timestamp.slice(0, 10);
      const minute = Number(row.timestamp.slice(11, 13)) * 60 + Number(row.timestamp.slice(14, 16));
      const slot = Math.floor(minute / activityCellMinutes);
      writingMinutesAtEvent.set(`${day}:${slot}`, writingMinutes);
      previousTimestamp = timestamp;
    }
    const writingMinutesByKey = new Map();
    let accumulatedWritingMinutes = 0;
    for (const day of days) {
      for (let slot = 0; slot < slotsPerDay; slot += 1) {
        const key = `${day}:${slot}`;
        if (writingMinutesAtEvent.has(key)) accumulatedWritingMinutes = writingMinutesAtEvent.get(key);
        writingMinutesByKey.set(key, accumulatedWritingMinutes);
      }
    }
    const stage = document.querySelector("#file-activity-stage");
    stage.style.width = `${width}px`;
    stage.style.height = "";
    svg.replaceChildren();
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("shape-rendering", "crispEdges");
    const detail = document.querySelector("#file-activity-detail");
    const sizeHistory = data.file_size_evolution
      .filter(row => row.fichier === selectionId() && !row.estime)
      .sort((a, b) => Date.parse(a.date) - Date.parse(b.date));
    detail.hidden = true;
    detail.textContent = "";

    days.forEach((day, dayIndex) => {
      for (let slot = 0; slot < slotsPerDay; slot += 1) {
        const bucket = buckets.get(`${day}:${slot}`);
        const chars = bucket?.chars || 0;
        const rectangle = document.createElementNS(namespace, "rect");
        rectangle.setAttribute("x", String(dayIndex * dayWidth + Math.floor(slot / rowsPerColumn) * cellWidth));
        rectangle.setAttribute("y", String((slot % rowsPerColumn) * cellHeight));
        rectangle.setAttribute("width", String(cellWidth));
        rectangle.setAttribute("height", String(cellHeight));
        const fill = chars ? `rgb(26 115 232 / ${0.22 + 0.78 * Math.sqrt(chars / maximum)})` : "#fff";
        rectangle.setAttribute("fill", fill);
        rectangle.setAttribute("data-fill", fill);
        rectangle.setAttribute("stroke", "#dadce0");
        rectangle.setAttribute("class", "activity-cell");
        rectangle.setAttribute("tabindex", "0");
        const tooltip = document.createElementNS(namespace, "title");
        const date = new Intl.DateTimeFormat("fr-FR", { dateStyle: "full", timeZone: "UTC" }).format(new Date(`${day}T12:00:00Z`));
        const start = slot * activityCellMinutes;
        const time = value => `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
        const speed = chars > 0 ? Math.round(chars * 60 / activityCellMinutes) : null;
        const documentSize = fileSizeAt(
          sizeHistory,
          parisTimestamp(day, start + activityCellMinutes)
        );
        const details = [
          `${date}, ${time(start)}–${time(start + activityCellMinutes)}`,
          `Texte produit sur la tranche : ${formatter.format(chars)} signes`,
          `Texte produit jusque-là : ${formatter.format(cumulativeByKey.get(`${day}:${slot}`) || 0)} signes`,
          `Temps passé estimé depuis le début : ${cadence === null ? "—" : durationLabel(writingMinutesByKey.get(`${day}:${slot}`) || 0)}`,
          `Taille du document à cet instant : ${formatter.format(documentSize)} signes`,
          `Vitesse sur cette tranche : ${speed === null ? "—" : `${formatter.format(speed)} signes/heure`}`
        ].join("\n");
        rectangle.setAttribute("aria-label", details);
        tooltip.textContent = details;
        rectangle.append(tooltip);
        rectangle.addEventListener("click", () => {
          svg.querySelectorAll(".activity-cell.is-selected").forEach(cell => {
            cell.classList.remove("is-selected");
            cell.setAttribute("fill", cell.dataset.fill);
          });
          rectangle.classList.add("is-selected");
          rectangle.setAttribute("fill", "#d93025");
          detail.textContent = details;
          detail.hidden = false;
        });
        rectangle.addEventListener("keydown", event => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          rectangle.dispatchEvent(new MouseEvent("click"));
        });
        svg.append(rectangle);
      }
      const label = document.createElementNS(namespace, "text");
      label.setAttribute("x", String(dayIndex * dayWidth + dayWidth / 2));
      label.setAttribute("y", String(gridHeight + 18));
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("fill", "#5f6368");
      label.setAttribute("font-size", "10");
      label.textContent = new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "short", timeZone: "UTC" }).format(new Date(`${day}T12:00:00Z`));
      svg.append(label);
    });
    const outline = document.createElementNS(namespace, "rect");
    outline.setAttribute("x", "1");
    outline.setAttribute("y", "1");
    outline.setAttribute("width", String(Math.max(0, width - 2)));
    outline.setAttribute("height", String(Math.max(0, gridHeight - 2)));
    outline.setAttribute("fill", "none");
    outline.setAttribute("stroke", "#9aa0a6");
    outline.setAttribute("stroke-width", "2");
    outline.setAttribute("pointer-events", "none");
    svg.append(outline);
    for (let day = 1; day < days.length; day += 1) {
      const separator = document.createElementNS(namespace, "line");
      separator.setAttribute("x1", String(day * dayWidth));
      separator.setAttribute("x2", String(day * dayWidth));
      separator.setAttribute("y1", "0");
      separator.setAttribute("y2", String(gridHeight));
      separator.setAttribute("stroke", "#9aa0a6");
      separator.setAttribute("stroke-width", "2");
      separator.setAttribute("pointer-events", "none");
      svg.append(separator);
    }
  }

  function filteredDaily() {
    return productionRows("day").filter(row => selectedProject === "all" || row.projet === selectionId());
  }

  function writingEstimate() {
    const rows = data.file_activity
      .filter(row => row.fichier === selectionId())
      .sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
    if (rows.length < 2) return null;
    const cadence = writingCadence(rows);
    if (!cadence) return null;
    const gaps = rows.slice(1).map((row, index) => (
      (Date.parse(row.timestamp) - Date.parse(rows[index].timestamp)) / 60000
    ));
    const sessionLimit = cadence * 4;
    const minutes = gaps.reduce(
      (total, gap) => total + (gap <= sessionLimit ? gap : cadence),
      cadence
    );
    const characters = rows.reduce((total, row) => total + (Number(row.signes_reels) || 0), 0);
    return { minutes, speed: minutes > 0 ? Math.round(characters * 60 / minutes) : null };
  }

  function writingCadence(rows) {
    const gaps = rows.slice(1).map((row, index) => (
      (Date.parse(row.timestamp) - Date.parse(rows[index].timestamp)) / 60000
    )).filter(value => value > 0);
    if (!gaps.length) return null;
    const ordered = gaps.sort((a, b) => a - b);
    const middle = Math.floor(ordered.length / 2);
    return ordered.length % 2
      ? ordered[middle]
      : (ordered[middle - 1] + ordered[middle]) / 2;
  }

  function durationLabel(minutes) {
    const rounded = Math.round(minutes);
    if (rounded < 60) return `${rounded} min`;
    const hours = Math.floor(rounded / 60);
    const remainder = rounded % 60;
    return remainder ? `${hours} h ${String(remainder).padStart(2, "0")}` : `${hours} h`;
  }

  function renderCards(rows) {
    document.querySelector("#total-chars").textContent = formatter.format(rows.reduce((sum, row) => sum + row.signes_reels, 0));
    const file = selectedFile();
    const finalSize = file
      ? Number(file.taille_actuelle) || 0
      : visibleProjects().reduce((total, project) => total + (Number(project.taille_actuelle) || 0), 0);
    document.querySelector("#final-size").textContent = formatter.format(finalSize);
    const estimate = file ? writingEstimate() : null;
    document.querySelector("#writing-time-stat").hidden = !file;
    document.querySelector("#writing-speed-stat").hidden = !file;
    document.querySelector("#writing-time").textContent = estimate ? durationLabel(estimate.minutes) : "—";
    document.querySelector("#writing-speed").textContent = estimate?.speed ? formatter.format(estimate.speed) : "—";
  }

  function savePreferences() {
    try {
      localStorage.setItem(storageKey, JSON.stringify({ project: selectedProject, productionGranularity, sizeGranularity, activityCellMinutes, zooms }));
    } catch (_) { /* Le dashboard reste utilisable si le stockage est désactivé. */ }
  }

  function updateActivityResolutionControls() {
    document.querySelector("#activity-resolution").textContent = activityCellMinutes === 60 ? "1 h" : `${activityCellMinutes} min`;
    const current = activityResolutions.indexOf(activityCellMinutes);
    document.querySelectorAll("[data-activity-resolution]").forEach(button => {
      button.disabled = button.dataset.activityResolution === "out"
        ? current === 0
        : current === activityResolutions.length - 1;
    });
  }

  function parisTimestamp(day, minuteOfDay) {
    const [year, month, date] = day.split("-").map(Number);
    const localAsUtc = Date.UTC(year, month - 1, date, 0, minuteOfDay);
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Europe/Paris",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23"
    }).formatToParts(new Date(localAsUtc));
    const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
    const representedAsUtc = Date.UTC(
      Number(values.year), Number(values.month) - 1, Number(values.day),
      Number(values.hour), Number(values.minute)
    );
    return localAsUtc - (representedAsUtc - localAsUtc);
  }

  function fileSizeAt(rows, timestamp) {
    let low = 0;
    let high = rows.length;
    while (low < high) {
      const middle = (low + high) >>> 1;
      if (Date.parse(rows[middle].date) <= timestamp) low = middle + 1;
      else high = middle;
    }
    return low ? Number(rows[low - 1].taille_signes) || 0 : 0;
  }

  function updateSelectionUrl() {
    const url = new URL(window.location.href);
    url.searchParams.delete("project");
    url.searchParams.delete("file");
    if (selectionKind() === "project") url.searchParams.set("project", selectionId());
    if (selectionKind() === "file") url.searchParams.set("file", selectionId());
    window.history.replaceState(null, "", url);
    document.title = selectedProject === "all" ? "Writing Log" : `${title(selectedProject)} — Writing Log`;
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
      charts[name]?.update("none");
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
    document.querySelectorAll("[data-activity-resolution]").forEach(button => {
      button.addEventListener("click", () => {
        const current = activityResolutions.indexOf(activityCellMinutes);
        const offset = button.dataset.activityResolution === "in" ? 1 : -1;
        const next = Math.max(0, Math.min(activityResolutions.length - 1, current + offset));
        if (next === current) return;
        activityCellMinutes = activityResolutions[next];
        savePreferences();
        updateActivityResolutionControls();
        render();
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
    const chart = Chart.getChart(canvas);
    const viewport = canvas.closest(".chart-scroll");
    const ratio = canvas.width / chart.width;
    const visibleWidth = viewport ? Math.min(viewport.clientWidth, chart.width) : chart.width;
    const axisWidth = viewport ? chart.chartArea.left : 0;
    const exported = document.createElement("canvas");
    exported.width = Math.round(visibleWidth * ratio);
    exported.height = canvas.height;
    const context = exported.getContext("2d");
    context.fillStyle = "#fff";
    context.fillRect(0, 0, exported.width, exported.height);
    if (!viewport) {
      context.drawImage(canvas, 0, 0);
    } else {
      const axisPixels = Math.round(axisWidth * ratio);
      context.drawImage(canvas, 0, 0, axisPixels, canvas.height, 0, 0, axisPixels, canvas.height);
      const plotWidth = exported.width - axisPixels;
      const sourceX = Math.round((viewport.scrollLeft + axisWidth) * ratio);
      context.drawImage(canvas, sourceX, 0, plotWidth, canvas.height, axisPixels, 0, plotWidth, canvas.height);
    }
    return exported.toDataURL("image/png");
  }

  function chartSvg(canvas) {
    const chart = Chart.getChart(canvas);
    const viewport = canvas.closest(".chart-scroll");
    const width = viewport ? Math.min(viewport.clientWidth, chart.width) : chart.width;
    const height = chart.height;
    const png = chartPng(canvas);
    return `<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><image width="${width}" height="${height}" href="${png}" xlink:href="${png}"/></svg>`;
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
    const svg = chartSvg(canvas);
    downloadBlob(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }), `${filename}.svg`);
  }

  function exportSvg(svg, format) {
    const heading = svg.closest(".panel")?.querySelector("h2")?.textContent || "graphique";
    const project = selectedProject === "all" ? "tous-les-projets" : title(selectedProject);
    const filename = `${safeFilename(project)}-${safeFilename(heading)}`;
    const exportVisual = svg.id === "file-activity-chart" ? activityExportSvg(svg) : svg;
    const width = Number(exportVisual.getAttribute("width"));
    const height = Number(exportVisual.getAttribute("height"));
    const source = new XMLSerializer().serializeToString(exportVisual);
    const blob = new Blob([source], { type: "image/svg+xml;charset=utf-8" });
    if (format === "svg") {
      downloadBlob(blob, `${filename}.svg`);
      return;
    }
    const url = URL.createObjectURL(blob);
    const image = new Image();
    image.onload = () => {
      const scale = 2;
      const canvas = document.createElement("canvas");
      canvas.width = width * scale;
      canvas.height = height * scale;
      const context = canvas.getContext("2d");
      context.scale(scale, scale);
      context.fillStyle = "#fff";
      context.fillRect(0, 0, width, height);
      context.drawImage(image, 0, 0, width, height);
      const link = document.createElement("a");
      link.href = canvas.toDataURL("image/png");
      link.download = `${filename}.png`;
      link.click();
      URL.revokeObjectURL(url);
    };
    image.src = url;
  }

  function activityExportSvg(svg) {
    const namespace = "http://www.w3.org/2000/svg";
    const panel = svg.closest(".panel");
    const detail = panel.querySelector("#file-activity-detail");
    const detailLines = detail.hidden ? [] : detail.textContent.split("\n");
    const viewport = svg.closest(".chart-scroll");
    const width = Number(svg.getAttribute("width"));
    const chartHeight = Number(svg.getAttribute("height"));
    const detailHeight = detailLines.length ? detailLines.length * 18 + 10 : 0;
    const height = chartHeight + detailHeight;
    const visibleWidth = viewport ? Math.min(viewport.clientWidth, width) : width;
    const scrollLeft = viewport ? Math.min(viewport.scrollLeft, Math.max(0, width - visibleWidth)) : 0;
    const root = document.createElementNS(namespace, "svg");
    root.setAttribute("xmlns", namespace);
    root.setAttribute("width", String(visibleWidth));
    root.setAttribute("height", String(height));
    root.setAttribute("viewBox", `0 0 ${visibleWidth} ${height}`);
    const background = document.createElementNS(namespace, "rect");
    background.setAttribute("width", "100%");
    background.setAttribute("height", "100%");
    background.setAttribute("fill", "#fff");
    root.append(background);
    const addText = (text, x, y, size, weight = "normal") => {
      const element = document.createElementNS(namespace, "text");
      element.setAttribute("x", String(x));
      element.setAttribute("y", String(y));
      element.setAttribute("fill", "#202124");
      element.setAttribute("font-family", "Arial, Helvetica, sans-serif");
      element.setAttribute("font-size", String(size));
      element.setAttribute("font-weight", weight);
      element.textContent = text;
      root.append(element);
    };
    const grid = svg.cloneNode(true);
    grid.setAttribute("x", "0");
    grid.setAttribute("y", "0");
    grid.setAttribute("width", String(visibleWidth));
    grid.setAttribute("viewBox", `${scrollLeft} 0 ${visibleWidth} ${chartHeight}`);
    root.append(grid);
    detailLines.forEach((line, index) => addText(line, 8, chartHeight + 18 * (index + 1), 12));
    return root;
  }

  function installDownloadButtons() {
    document.querySelectorAll(".panel").forEach(panel => {
      const visual = panel.querySelector("canvas, svg");
      const actions = panel.querySelector(".chart-actions");
      if (!visual) return;
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
          const currentVisual = panel.querySelector("canvas, svg");
          if (currentVisual instanceof SVGElement) exportSvg(currentVisual, format.toLowerCase());
          else exportChart(currentVisual, format.toLowerCase());
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

  function sizeBucket(date, granularity) {
    const day = date.slice(0, 10);
    return granularity === "minute"
      ? date.slice(0, 16)
      : granularity === "hour"
        ? date.slice(0, 13)
        : granularity === "year"
          ? day.slice(0, 4)
          : granularity === "month"
            ? day.slice(0, 7)
            : granularity === "week" ? isoWeek(day) : day;
  }

  function sizeRows(granularity) {
    const result = [];
    const file = selectedFile();
    const entities = file ? [file] : visibleProjects();
    const source = file
      ? data.file_size_evolution
          .filter(row => !row.estime && row.modifie)
          .map(row => ({ ...row, projet: row.fichier }))
      : data.size_evolution;
    for (const project of entities) {
      const rows = source.filter(row => row.projet === project.id).sort((a, b) => a.date.localeCompare(b.date));
      const buckets = new Map();
      for (const row of rows) {
        const bucket = sizeBucket(row.date, granularity);
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

  function combinedSizeRows(rows, granularity) {
    const byProject = new Map();
    for (const row of rows) {
      if (!byProject.has(row.projet)) byProject.set(row.projet, new Map());
      byProject.get(row.projet).set(sizeBucket(row.date, granularity), row);
    }
    const periods = [...new Set(rows.map(row => sizeBucket(row.date, granularity)))].sort();
    const current = new Map();
    const result = [];
    for (const period of periods) {
      const changed = [];
      for (const [project, projectRows] of byProject) {
        const row = projectRows.get(period);
        if (!row) continue;
        current.set(project, row);
        changed.push(row);
      }
      if (!current.size) continue;
      const activeRows = [...current.values()];
      const reference = changed.slice().sort((a, b) => a.date.localeCompare(b.date)).at(-1);
      result.push({
        date: reference.date,
        commit: "",
        source_commit: "",
        projet: "__all__",
        taille_signes: activeRows.reduce((sum, row) => sum + (Number(row.taille_signes) || 0), 0),
        taille_brute: activeRows.reduce((sum, row) => sum + (Number(row.taille_brute ?? row.taille_signes) || 0), 0),
        dossier: [...new Set(activeRows.map(row => row.dossier).filter(Boolean))].sort().join(", "),
        modifie: changed.some(row => row.modifie),
        estime: changed.some(row => row.estime),
        aggregate: true,
        project_count: activeRows.length
      });
    }
    return result;
  }

  function render() {
    Object.values(charts).forEach(chart => chart.destroy());
    const preciseProduction = selectionKind() === "file";
    document.querySelectorAll("[data-file-only]").forEach(option => {
      option.disabled = !preciseProduction;
      option.hidden = !preciseProduction;
    });
    if (!preciseProduction && ["minute", "hour"].includes(productionGranularity)) {
      productionGranularity = "day";
      document.querySelector("#production-granularity").value = productionGranularity;
      savePreferences();
    }
    if (!preciseProduction && ["minute", "hour"].includes(sizeGranularity)) {
      sizeGranularity = "day";
      document.querySelector("#size-granularity").value = sizeGranularity;
      savePreferences();
    }
    renderCards(filteredDaily());
    const activityPanel = document.querySelector("#file-activity-panel");
    activityPanel.hidden = selectionKind() !== "file";
    if (!activityPanel.hidden) {
      updateActivityResolutionControls();
      fileActivityGrid(document.querySelector("#file-activity-chart"));
    }
    const productionData = productionRows(productionGranularity);
    charts.daily = productionChart(document.querySelector("#daily-chart"), productionData, productionGranularity);
    charts.weekday = weekdayChart(document.querySelector("#weekday-chart"));
    const projectSizes = sizeRows(sizeGranularity);
    const sizeSeries = selectedProject === "all"
      ? [{ id: "__all__", title: "Tous les projets", rows: combinedSizeRows(projectSizes, sizeGranularity) }]
      : (selectedFile() ? [selectedFile()] : visibleProjects()).map(project => ({
          id: project.id,
          title: project.title,
          rows: projectSizes.filter(row => row.projet === project.id)
        }));
    const sizeTemporal = temporalAxis(sizeSeries.flatMap(item => item.rows), "date", sizeGranularity, "size");
    const sizeDatasets = sizeSeries.map(item => {
      return {
        label: item.title,
        data: item.rows.map((row, index) => {
          const previous = item.rows[index - 1];
          const delta = previous ? Number(row.taille_signes) - Number(previous.taille_signes) : null;
          const elapsedHours = previous
            ? (periodTimestamp(row.date) - periodTimestamp(previous.date)) / 3600000
            : 0;
          return {
            x: periodTimestamp(row.date),
            y: row.taille_signes,
            delta,
            speed: delta !== null && elapsedHours > 0 ? Math.round(delta / elapsedHours) : null,
            rawSize: row.taille_brute ?? row.taille_signes,
            timestamp: row.date,
            commit: row.commit || "",
            sourceCommit: row.source_commit || "",
            folder: row.dossier || "",
            estimated: Boolean(row.estime),
            touched: Boolean(row.modifie),
            aggregate: Boolean(row.aggregate),
            projectCount: Number(row.project_count) || 1
          };
        }),
        borderColor: color(item.id),
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
        dateStyle: "long", timeStyle: "medium", timeZone: "Europe/Paris"
      }).format(new Date(items[0].raw.timestamp)),
      label: context => `Taille : ${formatter.format(context.raw.y)} signes`,
      afterLabel: context => selectionKind() === "file"
        ? [
            `Signes : ${context.raw.delta === null ? "—" : context.raw.delta > 0 ? `+ ${formatter.format(context.raw.delta)}` : context.raw.delta < 0 ? `− ${formatter.format(Math.abs(context.raw.delta))}` : "0"}`,
            `Vitesse : ${context.raw.speed === null ? "—" : `${context.raw.speed > 0 ? "+ " : context.raw.speed < 0 ? "− " : ""}${formatter.format(Math.abs(context.raw.speed))} signes/heure`}`
          ]
        : context.raw.aggregate
        ? [
            `Cumul de ${context.raw.projectCount} projets`,
            `Dossiers analysés : ${context.raw.folder || "—"}`
          ]
        : context.raw.estimated
        ? [
            `Estimation quotidienne vers le commit : ${context.raw.sourceCommit.slice(0, 12)}`,
            `${selectionKind() === "file" ? "Fichier" : "Dossier"} analysé : ${context.raw.folder || "—"}`
          ]
        : context.raw.commit ? [
            `Commit : ${context.raw.commit.slice(0, 12)}`,
            `${selectionKind() === "file" ? "Fichier" : "Dossier"} analysé : ${context.raw.folder || "—"}`,
            context.raw.touched ? `${selectionKind() === "file" ? "Fichier" : "Projet"} modifié` : "Taille inchangée",
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
      data.files.forEach(file => color(file.id));
      try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
        if (saved?.project === "all" || data.projects.some(project => `project:${project.id}` === saved?.project) || data.files.some(file => `file:${file.id}` === saved?.project)) selectedProject = saved.project;
        else if (data.projects.some(project => project.id === saved?.project)) selectedProject = `project:${saved.project}`;
        if (["minute", "hour", "day", "week", "month"].includes(saved?.productionGranularity)) productionGranularity = saved.productionGranularity;
        if (["minute", "hour", "day", "week", "month", "year"].includes(saved?.sizeGranularity)) sizeGranularity = saved.sizeGranularity;
        if (activityResolutions.includes(saved?.activityCellMinutes)) activityCellMinutes = saved.activityCellMinutes;
        if (saved?.zooms) {
          for (const key of Object.keys(zooms)) {
            if (zoomLevels.includes(saved.zooms[key])) zooms[key] = saved.zooms[key];
          }
        }
      } catch (_) { /* Préférences absentes ou anciennes : conserver les valeurs par défaut. */ }
      const query = new URLSearchParams(window.location.search);
      if (query.has("project") && data.projects.some(project => project.id === query.get("project"))) selectedProject = `project:${query.get("project")}`;
      if (query.has("file") && data.files.some(file => file.id === query.get("file"))) selectedProject = `file:${query.get("file")}`;
      const projectFilter = document.querySelector("#project-filter");
      const projectsGroup = document.createElement("optgroup");
      projectsGroup.label = "Projets";
      data.projects.forEach(project => projectsGroup.append(new Option(project.title, `project:${project.id}`)));
      projectFilter.append(projectsGroup);
      if (data.files.length) {
        const filesGroup = document.createElement("optgroup");
        filesGroup.label = "Fichiers";
        data.files.forEach(file => filesGroup.append(new Option(file.title, `file:${file.id}`)));
        projectFilter.append(filesGroup);
      }
      projectFilter.value = selectedProject;
      projectFilter.addEventListener("change", event => { selectedProject = event.target.value; updateSelectionUrl(); savePreferences(); render(); });
      const granularity = document.querySelector("#production-granularity");
      granularity.value = productionGranularity;
      granularity.addEventListener("change", event => { productionGranularity = event.target.value; savePreferences(); render(); });
      const sizeGranularitySelect = document.querySelector("#size-granularity");
      sizeGranularitySelect.value = sizeGranularity;
      sizeGranularitySelect.addEventListener("change", event => { sizeGranularity = event.target.value; savePreferences(); render(); });
      document.querySelector("#last-update").textContent = new Intl.DateTimeFormat("fr-FR", { dateStyle: "long", timeStyle: "short" }).format(new Date(data.overview.derniere_mise_a_jour));
      updateSelectionUrl();
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
