(function () {
  "use strict";

  const mainSelector = "#app-main";
  const htmlType = "text/html";
  let activeTooltipTarget = null;
  const pageCache = new Map();
  const prefetching = new Set();
  const journalRowsCache = new WeakMap();
  const journalPinsStorageKey = "netvault:pinned-journals:v1";
  const maxCachedPages = 10;
  const pageCacheTtlMs = 30000;
  const clientHashMaxBytes = 32 * 1024 * 1024;
  const activeUploadRequests = new Set();
  let journalFilterTimer = null;

  const qs = (selector, root = document) => root.querySelector(selector);
  const qsa = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const sameOriginUrl = (value) => {
    try {
      return new URL(value, window.location.href);
    } catch {
      return null;
    }
  };

  const isWebPath = (url) => url.origin === window.location.origin && /\/web(?:\/|$)/.test(url.pathname);
  const isDownloadPath = (url) => /\/web\/pdfs\/download(?:\/|$)/.test(url.pathname);

  const setLoading = (loading) => {
    document.documentElement.classList.toggle("is-loading", loading);
    document.body.setAttribute("aria-busy", loading ? "true" : "false");
  };

  const setFormBusy = (form, busy, label) => {
    form.toggleAttribute("aria-busy", busy);
    qsa("button, input, textarea, select", form).forEach((control) => {
      if (control.type !== "hidden") control.disabled = busy;
    });
    const submit = qs("[type='submit']", form);
    if (submit) {
      const labelNode = qs("[data-button-label]", submit);
      if (labelNode) {
        if (!labelNode.dataset.originalText) labelNode.dataset.originalText = labelNode.textContent.trim();
        labelNode.textContent = busy ? label : labelNode.dataset.originalText;
      } else {
        if (!submit.dataset.originalText) submit.dataset.originalText = submit.textContent.trim();
        submit.textContent = busy ? label : submit.dataset.originalText;
      }
    }
  };

  const cacheKey = (url) => {
    const parsed = sameOriginUrl(url);
    return parsed ? parsed.href : url;
  };

  const shouldCachePage = (options = {}) => {
    const method = String(options.method || "GET").toUpperCase();
    return method === "GET" && !options.body;
  };

  const rememberPage = (url, state) => {
    if (!state) return;
    const key = cacheKey(url);
    pageCache.delete(key);
    pageCache.set(key, { ...state, cachedAt: Date.now() });
    while (pageCache.size > maxCachedPages) pageCache.delete(pageCache.keys().next().value);
  };

  const parsePageState = (html) => {
    const nextDoc = new DOMParser().parseFromString(html, htmlType);
    const nextMain = qs(mainSelector, nextDoc);
    if (!nextMain) return null;
    const nextTopbar = qs(".topbar", nextDoc);
    return {
      bodyClass: nextDoc.body.className,
      mainHTML: nextMain.innerHTML,
      title: nextDoc.title || document.title,
      topbarHTML: nextTopbar ? nextTopbar.innerHTML : null,
    };
  };

  const snapshotPage = () => {
    const currentMain = qs(mainSelector);
    const currentTopbar = qs(".topbar");
    if (!currentMain) return null;
    return {
      bodyClass: document.body.className,
      mainHTML: currentMain.innerHTML,
      title: document.title,
      topbarHTML: currentTopbar ? currentTopbar.innerHTML : null,
    };
  };

  const applyPageState = (state, url, push) => {
    const currentMain = qs(mainSelector);
    const currentTopbar = qs(".topbar");
    if (!currentMain) {
      window.location.href = url;
      return;
    }
    if (currentTopbar && state.topbarHTML !== null) currentTopbar.innerHTML = state.topbarHTML;
    document.body.className = state.bodyClass;
    currentMain.innerHTML = state.mainHTML;
    document.title = state.title;

    if (push && url !== window.location.href) history.pushState({}, "", url);
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    setLoading(false);
    initJournalPins(currentMain);
    currentMain.focus({ preventScroll: true });
  };

  const updateFromHtml = (html, url, push, cache = false) => {
    const state = parsePageState(html);
    if (!state) {
      window.location.href = url;
      return;
    }
    if (cache) rememberPage(url, state);
    applyPageState(state, url, push);
  };

  const fetchPage = async (url, options = {}, push = true) => {
    const useCache = shouldCachePage(options);
    const key = cacheKey(url);
    if (useCache && pageCache.has(key)) {
      const cached = pageCache.get(key);
      if (Date.now() - cached.cachedAt <= pageCacheTtlMs) {
        applyPageState(cached, url, push);
        return;
      }
      pageCache.delete(key);
    }

    setLoading(true);
    try {
      const response = await fetch(url, {
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch", ...(options.headers || {}) },
        ...options,
      });
      const contentType = response.headers.get("content-type") || "";
      if (!contentType.includes(htmlType)) {
        window.location.href = response.url || url;
        return;
      }
      const html = await response.text();
      if (!useCache) pageCache.clear();
      updateFromHtml(html, response.url || url, push, useCache);
    } catch {
      window.location.href = url;
    } finally {
      setLoading(false);
    }
  };

  const prefetchPage = async (href) => {
    const url = sameOriginUrl(href);
    if (!url || !isWebPath(url) || isDownloadPath(url)) return;
    const key = url.href;
    if (pageCache.has(key) || prefetching.has(key)) return;
    prefetching.add(key);
    try {
      const response = await fetch(key, {
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
      });
      const contentType = response.headers.get("content-type") || "";
      if (response.ok && contentType.includes(htmlType)) {
        const state = parsePageState(await response.text());
        rememberPage(response.url || key, state);
      }
    } catch {
      // Prefetch is only an acceleration hint; failed prefetches should stay silent.
    } finally {
      prefetching.delete(key);
    }
  };

  const markLinkActive = (link) => {
    const group = link.closest(".primary-nav, .utility-nav, .filter-tabs");
    if (!group) return;
    qsa("a.active", group).forEach((item) => item.classList.remove("active"));
    link.classList.add("active");
  };

  const copyText = async (button) => {
    const text = button.getAttribute("data-copy") || "";
    const label = qs("[data-copy-label]", button) || button;
    const original = label.textContent;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.left = "-9999px";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        textarea.remove();
      }
      label.textContent = "Copied";
    } catch {
      label.textContent = "Failed";
    }
    window.setTimeout(() => {
      label.textContent = original;
    }, 1000);
  };

  const showTooltip = (target, event) => {
    const text = target.getAttribute("data-tip");
    if (!text) return;
    activeTooltipTarget = target;
    let tooltip = qs(".heatmap-tooltip");
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "heatmap-tooltip";
      tooltip.setAttribute("role", "tooltip");
      document.body.appendChild(tooltip);
    }
    tooltip.textContent = text;
    tooltip.classList.toggle("has-icon", target.classList.contains("heat-cell"));
    tooltip.classList.add("is-visible");
    positionTooltip(event);
  };

  const positionTooltip = (event) => {
    if (!activeTooltipTarget) return;
    const tooltip = qs(".heatmap-tooltip");
    if (!tooltip) return;
    const offset = 12;
    const rect = tooltip.getBoundingClientRect();
    const targetRect = activeTooltipTarget.getBoundingClientRect();
    const clientX = Number.isFinite(event.clientX) ? event.clientX : targetRect.left + targetRect.width / 2;
    const clientY = Number.isFinite(event.clientY) ? event.clientY : targetRect.bottom;
    let left = clientX + offset;
    let top = clientY + offset;
    if (left + rect.width + 8 > window.innerWidth) left = clientX - rect.width - offset;
    if (top + rect.height + 8 > window.innerHeight) top = clientY - rect.height - offset;
    tooltip.style.transform = `translate(${Math.max(8, left)}px, ${Math.max(8, top)}px)`;
  };

  const hideTooltip = () => {
    activeTooltipTarget = null;
    const tooltip = qs(".heatmap-tooltip");
    if (tooltip) tooltip.classList.remove("is-visible");
  };

  const normalizedJournalText = (value) => value.normalize("NFKC").toLocaleLowerCase();

  const journalRows = (heatmap) => {
    if (journalRowsCache.has(heatmap)) return journalRowsCache.get(heatmap);
    const elementsByRow = new Map();
    qsa("[data-journal-row]", heatmap).forEach((element) => {
      const rowId = element.dataset.journalRow;
      if (!elementsByRow.has(rowId)) elementsByRow.set(rowId, []);
      elementsByRow.get(rowId).push(element);
    });
    const rows = qsa(".heat-journal[data-journal-name]", heatmap).map((label) => ({
      name: normalizedJournalText(label.dataset.journalName || label.textContent || ""),
      label: label.dataset.journalName || label.textContent || "",
      elements: elementsByRow.get(label.dataset.journalRow) || [label],
    }));
    journalRowsCache.set(heatmap, rows);
    return rows;
  };

  const loadJournalPins = () => {
    try {
      const value = JSON.parse(window.localStorage.getItem(journalPinsStorageKey) || "[]");
      if (!Array.isArray(value)) return [];
      return value.filter((item) => typeof item === "string" && item.trim()).map((item) => item.trim());
    } catch {
      return [];
    }
  };

  const saveJournalPins = (pins) => {
    try {
      window.localStorage.setItem(journalPinsStorageKey, JSON.stringify(pins));
    } catch {
      // Filtering still works for this page when storage is unavailable.
    }
  };

  const renderJournalPins = (panel) => {
    const clearButton = qs("[data-journal-pin-clear]", panel);
    const pins = loadJournalPins();
    if (clearButton) clearButton.hidden = pins.length === 0;
  };

  const availableJournalNames = (panel) =>
    qsa("#journal-pin-options option", panel).map((option) => ({
      label: option.value,
      name: normalizedJournalText(option.value),
    }));

  const reloadDashboardForPins = (panel, push = false) => {
    const url = sameOriginUrl(window.location.href);
    if (!url || !/\/web$/.test(url.pathname)) return;
    const pins = loadJournalPins();
    const desired = pins.map(normalizedJournalText).sort();
    const current = url.searchParams.getAll("pin").map(normalizedJournalText).sort();
    if (desired.length === current.length && desired.every((name, index) => name === current[index])) return;
    url.searchParams.delete("pin");
    pins.forEach((name) => url.searchParams.append("pin", name));
    fetchPage(url.href, {}, push);
  };

  const applyJournalFilters = (panel) => {
    if (!panel || !panel.isConnected) return;
    const heatmap = qs(".journal-heatmap", panel);
    if (!heatmap) return;
    const input = qs("[data-journal-filter]", panel);
    const query = normalizedJournalText(input ? input.value.trim() : "");
    const rows = journalRows(heatmap);
    const pinnedNames = new Set(loadJournalPins().map(normalizedJournalText));
    let visibleCount = 0;
    rows.forEach((row) => {
      const visible = (!query || row.name.includes(query)) && (!pinnedNames.size || pinnedNames.has(row.name));
      row.elements.forEach((element) => {
        element.hidden = !visible;
      });
      if (visible) visibleCount += 1;
    });
    const status = qs("[data-journal-filter-status], .heatmap-filter-status", panel);
    if (status) {
      const noun = rows.length === 1 ? "journal" : "journals";
      status.textContent = query || pinnedNames.size ? `${visibleCount} of ${rows.length} ${noun}` : `${rows.length} ${noun}`;
    }
    const noMatches = qs("[data-journal-no-matches]", panel);
    if (noMatches) noMatches.hidden = visibleCount !== 0;
    const scroll = heatmap.closest(".heatmap-scroll");
    if (scroll) scroll.hidden = visibleCount === 0;
  };

  const scheduleJournalFilter = (input) => {
    window.clearTimeout(journalFilterTimer);
    journalFilterTimer = window.setTimeout(() => applyJournalFilters(input.closest(".heatmap-panel")), 300);
  };

  const flushJournalFilter = (input) => {
    window.clearTimeout(journalFilterTimer);
    applyJournalFilters(input.closest(".heatmap-panel"));
  };

  const addJournalPin = (input) => {
    const panel = input.closest(".heatmap-panel");
    const heatmap = qs(".journal-heatmap", panel);
    const feedback = qs("[data-journal-pin-feedback]", panel);
    const requested = normalizedJournalText(input.value.trim());
    if (!heatmap || !requested) return;
    const rows = journalRows(heatmap);
    let match = rows.find((row) => row.name === requested);
    if (!match) {
      const partialMatches = rows.filter((row) => row.name.includes(requested));
      if (partialMatches.length === 1) match = partialMatches[0];
    }
    if (!match) {
      const available = availableJournalNames(panel);
      match = available.find((row) => row.name === requested);
      if (!match) {
        const partialMatches = available.filter((row) => row.name.includes(requested));
        if (partialMatches.length === 1) match = partialMatches[0];
      }
      if (!match) {
        if (feedback) feedback.textContent = "Choose an exact journal name from the list.";
        return;
      }
    }
    const pins = loadJournalPins();
    if (!pins.some((name) => normalizedJournalText(name) === match.name)) pins.push(match.label);
    saveJournalPins(pins);
    input.value = "";
    if (feedback) feedback.textContent = "Journal pinned.";
    renderJournalPins(panel);
    const rowExists = rows.some((row) => row.name === match.name);
    if (rowExists) applyJournalFilters(panel);
    else reloadDashboardForPins(panel, true);
  };

  const initJournalPins = (root = document) => {
    qsa("[data-journal-pin-panel]", root).forEach((pinPanel) => {
      const panel = pinPanel.closest(".heatmap-panel");
      renderJournalPins(panel);
      applyJournalFilters(panel);
      const rowNames = new Set(journalRows(qs(".journal-heatmap", panel)).map((row) => row.name));
      const missingPin = loadJournalPins().some(
        (name) => !rowNames.has(normalizedJournalText(name))
      );
      if (missingPin) reloadDashboardForPins(panel);
    });
  };

  const updateFileSummary = (input) => {
    const summary = qs("#file-summary");
    if (!summary) return;
    const count = input.files.length;
    if (!count) summary.textContent = "Choose files";
    else if (count === 1) summary.textContent = input.files[0].name;
    else summary.textContent = `${count} files selected`;
  };

  const toHex = (buffer) =>
    Array.from(new Uint8Array(buffer))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");

  const fileSha256 = async (file) => {
    if (file.size > clientHashMaxBytes) return null;
    const buffer = await file.arrayBuffer();
    return toHex(await crypto.subtle.digest("SHA-256", buffer));
  };

  const hasPdfHeader = async (file) => {
    const header = new Uint8Array(await file.slice(0, 5).arrayBuffer());
    return header[0] === 0x25 && header[1] === 0x50 && header[2] === 0x44 && header[3] === 0x46 && header[4] === 0x2d;
  };

  const resultDetail = (row) => {
    if (!row.ok) return row.error || "";
    return row.doi || "";
  };

  const renderUploadRows = (rows) => {
    if (!rows.length) return;
    qsa("[data-client-upload-results]").forEach((node) => node.remove());
    let table = qs(".results");
    if (!table) {
      const heading = document.createElement("h2");
      heading.textContent = "Results";
      heading.setAttribute("data-client-upload-results", "");
      table = document.createElement("table");
      table.className = "results";
      table.setAttribute("data-client-upload-results", "");
      table.innerHTML = "<thead><tr><th>File</th><th>Status</th><th>DOI / Error</th></tr></thead><tbody></tbody>";
      const form = qs("[data-upload-form]");
      form.insertAdjacentElement("afterend", table);
      form.insertAdjacentElement("afterend", heading);
    }
    const tbody = qs("tbody", table);
    rows
      .slice()
      .reverse()
      .forEach((row) => {
        const tr = document.createElement("tr");
        const file = document.createElement("td");
        const status = document.createElement("td");
        const detail = document.createElement("td");
        file.textContent = row.filename || "";
        status.textContent = row.ok ? row.status || "uploaded" : "failed";
        detail.textContent = resultDetail(row);
        tr.append(file, status, detail);
        tbody.prepend(tr);
      });
  };

  const checkExistingPdfs = async (form, hashes, csrfToken) => {
    const url = form.getAttribute("data-precheck-url");
    if (!url || !hashes.length) return {};
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
        "X-Requested-With": "fetch",
      },
      body: JSON.stringify({ sha256: hashes }),
    });
    if (!response.ok) throw new Error("Server check failed");
    const payload = await response.json();
    return payload.existing || {};
  };

  const uploadOneFile = (url, file, values, onProgress) =>
    new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      const body = new FormData();
      body.append("csrf_token", values.csrfToken);
      body.append("doi", values.manualDoi);
      if (values.noCrossref) body.append("no_crossref", "true");
      body.append("file", file, file.name);
      let settled = false;
      const finish = (row) => {
        if (settled) return;
        settled = true;
        activeUploadRequests.delete(xhr);
        resolve(row);
      };
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) onProgress(event.loaded, event.total);
      });
      xhr.addEventListener("load", () => {
        let payload = {};
        try {
          payload = JSON.parse(xhr.responseText || "{}");
        } catch {
          payload = {};
        }
        if (xhr.status >= 200 && xhr.status < 300) finish(payload);
        else finish({ filename: file.name, ok: false, error: payload.detail || `HTTP ${xhr.status}` });
      });
      xhr.addEventListener("error", () => finish({ filename: file.name, ok: false, error: "Network error" }));
      xhr.addEventListener("abort", () => finish({ filename: file.name, ok: false, error: "Cancelled" }));
      xhr.open("POST", url);
      xhr.setRequestHeader("X-Requested-With", "fetch");
      if (values.sha256) xhr.setRequestHeader("Idempotency-Key", values.sha256);
      xhr.withCredentials = true;
      activeUploadRequests.add(xhr);
      xhr.send(body);
    });

  const uploadWithProgress = async (form) => {
    delete form.dataset.uploadCancelled;
    const progress = qs(".upload-progress", form);
    const progressBar = qs(".upload-progress-bar span", form);
    const progressText = qs(".upload-progress p", form);
    const fileInput = qs("input[type='file']", form);
    const selectedFiles = fileInput ? Array.from(fileInput.files) : [];
    const originalBody = new FormData(form);
    const csrfToken = originalBody.get("csrf_token") || "";
    const manualDoi = String(originalBody.get("doi") || "").trim();
    const noCrossref = Boolean(originalBody.get("no_crossref"));
    const uploadUrl = form.getAttribute("data-file-upload-url");
    const cancelButton = qs("[data-upload-cancel]", form);
    const preRows = [];

    setFormBusy(form, true, "Checking...");
    if (cancelButton) {
      cancelButton.hidden = false;
      cancelButton.disabled = false;
    }
    if (progress) progress.hidden = false;
    if (progressBar) progressBar.style.width = "0%";
    if (progressText) progressText.textContent = "Hashing PDFs...";

    if (manualDoi && selectedFiles.length !== 1) {
      renderUploadRows([{ filename: "Upload", ok: false, error: "Manual DOI can only be used with one file." }]);
      if (progressText) progressText.textContent = "Upload stopped";
      setFormBusy(form, false, "Upload");
      return;
    }

    const hashedFiles = [];
    for (const [index, file] of selectedFiles.entries()) {
      if (!file.name.toLowerCase().endsWith(".pdf") || !(await hasPdfHeader(file))) {
        preRows.push({ filename: file.name, ok: false, error: "invalid PDF file: expected %PDF- header" });
        continue;
      }
      const sha256 = await fileSha256(file);
      hashedFiles.push({ file, sha256 });
      if (progressBar) progressBar.style.width = `${Math.round(((index + 1) / Math.max(1, selectedFiles.length)) * 35)}%`;
      if (progressText) progressText.textContent = `Hashing ${index + 1}/${selectedFiles.length} PDFs`;
    }

    if (progressText) progressText.textContent = "Checking server...";
    const existing = await checkExistingPdfs(
      form,
      hashedFiles.map((entry) => entry.sha256).filter(Boolean),
      csrfToken
    );
    const newFiles = [];
    hashedFiles.forEach((entry) => {
      const pdf = entry.sha256 ? existing[entry.sha256] : null;
      if (pdf) {
        preRows.push({ filename: entry.file.name, ok: true, status: "existing", doi: pdf.doi });
      } else {
        newFiles.push(entry);
      }
    });

    if (!newFiles.length) {
      if (progressBar) progressBar.style.width = "100%";
      if (progressText) progressText.textContent = "Done";
      renderUploadRows(preRows);
      setFormBusy(form, false, "Upload");
      if (cancelButton) cancelButton.hidden = true;
      return;
    }
    if (!uploadUrl) throw new Error("Upload endpoint is unavailable");
    if (progressText) progressText.textContent = `Uploading ${newFiles.length} PDF${newFiles.length === 1 ? "" : "s"}...`;
    const transfers = new Map(newFiles.map((entry) => [entry.file, { loaded: 0, total: entry.file.size || 1 }]));
    const results = new Array(newFiles.length);
    let nextIndex = 0;
    const reportProgress = (file, loaded, total) => {
      transfers.set(file, { loaded, total });
      const totals = Array.from(transfers.values()).reduce(
        (sum, item) => ({ loaded: sum.loaded + item.loaded, total: sum.total + item.total }),
        { loaded: 0, total: 0 }
      );
      const percent = totals.total ? Math.round((totals.loaded / totals.total) * 100) : 0;
      if (progressBar) progressBar.style.width = `${35 + Math.round(percent * 0.65)}%`;
      if (progressText) progressText.textContent = percent >= 100 ? "Processing metadata..." : `Uploading ${percent}%`;
    };
    const worker = async () => {
      while (nextIndex < newFiles.length && form.dataset.uploadCancelled !== "true") {
        const index = nextIndex++;
        const entry = newFiles[index];
        const file = entry.file;
        results[index] = await uploadOneFile(
          uploadUrl,
          file,
          { csrfToken, manualDoi, noCrossref, sha256: entry.sha256 },
          (loaded, total) => reportProgress(file, loaded, total)
        );
      }
    };
    await Promise.all(Array.from({ length: Math.min(2, newFiles.length) }, () => worker()));
    newFiles.forEach((entry, index) => {
      if (!results[index]) results[index] = { filename: entry.file.name, ok: false, error: "Cancelled" };
    });
    if (progressBar) progressBar.style.width = "100%";
    if (progressText) progressText.textContent = results.some((row) => !row.ok) ? "Completed with errors" : "Done";
    pageCache.clear();
    renderUploadRows([...preRows, ...results]);
    setFormBusy(form, false, "Upload");
    if (cancelButton) cancelButton.hidden = true;
  };

  document.addEventListener("click", (event) => {
    const utilityToggle = event.target.closest("[data-utility-toggle]");
    if (utilityToggle) {
      const nav = qs(`#${utilityToggle.getAttribute("aria-controls")}`);
      const expanded = utilityToggle.getAttribute("aria-expanded") === "true";
      utilityToggle.setAttribute("aria-expanded", String(!expanded));
      if (nav) nav.classList.toggle("is-open", !expanded);
      return;
    }

    const cancelUpload = event.target.closest("[data-upload-cancel]");
    if (cancelUpload) {
      const form = cancelUpload.closest("form");
      if (form) form.dataset.uploadCancelled = "true";
      activeUploadRequests.forEach((xhr) => xhr.abort());
      return;
    }

    const pinAdd = event.target.closest("[data-journal-pin-add]");
    if (pinAdd) {
      const input = qs("[data-journal-pin-input]", pinAdd.closest(".heatmap-panel"));
      if (input) addJournalPin(input);
      return;
    }

    const pinClear = event.target.closest("[data-journal-pin-clear]");
    if (pinClear) {
      saveJournalPins([]);
      const panel = pinClear.closest(".heatmap-panel");
      const feedback = qs("[data-journal-pin-feedback]", panel);
      if (feedback) feedback.textContent = "Pins cleared.";
      renderJournalPins(panel);
      applyJournalFilters(panel);
      reloadDashboardForPins(panel, true);
      return;
    }

    const copyButton = event.target.closest("[data-copy]");
    if (copyButton) {
      event.preventDefault();
      copyText(copyButton);
      return;
    }

    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (link.target || link.hasAttribute("download") || link.hasAttribute("data-no-pjax")) return;
    const url = sameOriginUrl(link.href);
    if (!url || !isWebPath(url) || isDownloadPath(url)) return;
    event.preventDefault();
    markLinkActive(link);
    fetchPage(url.href);
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest(".topbar-right")) return;
    const toggle = qs("[data-utility-toggle]");
    const nav = qs(".utility-nav");
    if (toggle) toggle.setAttribute("aria-expanded", "false");
    if (nav) nav.classList.remove("is-open");
  });

  document.addEventListener("pointerover", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || link.target || link.hasAttribute("download") || link.hasAttribute("data-no-pjax")) return;
    if (!link.closest(".topbar, .filter-tabs")) return;
    prefetchPage(link.href);
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    const url = sameOriginUrl(form.action);
    if (form.hasAttribute("data-native-submit")) return;
    if (!url || !isWebPath(url)) return;
    event.preventDefault();

    if (form.matches("[data-upload-form]")) {
      uploadWithProgress(form).catch(() => {
        const progressText = qs(".upload-progress p", form);
        if (progressText) progressText.textContent = "Upload check failed";
        setFormBusy(form, false, "Upload");
        const cancelButton = qs("[data-upload-cancel]", form);
        if (cancelButton) cancelButton.hidden = true;
      });
      return;
    }

    const method = (form.method || "GET").toUpperCase();
    const body = new FormData(form);
    setFormBusy(form, true, method === "GET" ? "Searching..." : "Loading...");
    if (method === "GET") {
      const params = new URLSearchParams(body);
      fetchPage(`${url.origin}${url.pathname}?${params.toString()}`);
    } else {
      fetchPage(url.href, { method, body }).finally(() => setFormBusy(form, false, "Submit"));
    }
  });

  document.addEventListener("change", (event) => {
    if (event.target.matches("#file-input")) updateFileSummary(event.target);
  });

  document.addEventListener("input", (event) => {
    if (event.target.matches("[data-journal-filter]")) scheduleJournalFilter(event.target);
  });

  document.addEventListener("search", (event) => {
    if (event.target.matches("[data-journal-filter]")) flushJournalFilter(event.target);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.matches("[data-journal-pin-input]")) {
      event.preventDefault();
      addJournalPin(event.target);
      return;
    }
    if (event.key === "Enter" && event.target.matches("[data-journal-filter]")) {
      event.preventDefault();
      flushJournalFilter(event.target);
    }
  });

  document.addEventListener("focusout", (event) => {
    if (event.target.matches("[data-journal-filter]")) flushJournalFilter(event.target);
  });

  document.addEventListener("dragenter", (event) => {
    const dropzone = event.target.closest("#dropzone");
    if (dropzone) dropzone.classList.add("is-dragging");
  });

  document.addEventListener("dragover", (event) => {
    const dropzone = event.target.closest("#dropzone");
    if (dropzone) {
      event.preventDefault();
      dropzone.classList.add("is-dragging");
    }
  });

  document.addEventListener("dragleave", (event) => {
    const dropzone = event.target.closest("#dropzone");
    if (dropzone) dropzone.classList.remove("is-dragging");
  });

  document.addEventListener("drop", (event) => {
    const dropzone = event.target.closest("#dropzone");
    if (dropzone) {
      event.preventDefault();
      dropzone.classList.remove("is-dragging");
      const input = qs("input[type='file']", dropzone);
      if (input && event.dataTransfer && event.dataTransfer.files.length) {
        input.files = event.dataTransfer.files;
        updateFileSummary(input);
      }
    }
  });

  document.addEventListener("pointerover", (event) => {
    const target = event.target.closest(".journal-heatmap [data-tip]");
    if (!target || target === activeTooltipTarget) return;
    showTooltip(target, event);
  });

  document.addEventListener("pointermove", positionTooltip);

  document.addEventListener("pointerout", (event) => {
    if (!activeTooltipTarget) return;
    const next = event.relatedTarget;
    if (next && activeTooltipTarget.contains(next)) return;
    hideTooltip();
  });

  document.addEventListener("focusin", (event) => {
    const target = event.target.closest(".journal-heatmap [data-tip]");
    if (target) showTooltip(target, event);
  });

  document.addEventListener("focusout", (event) => {
    if (event.target.closest(".journal-heatmap [data-tip]")) hideTooltip();
  });

  window.addEventListener("popstate", () => {
    fetchPage(window.location.href, {}, false);
  });

  rememberPage(window.location.href, snapshotPage());
  initJournalPins();
})();
