(function () {
  "use strict";

  const mainSelector = "#app-main";
  const htmlType = "text/html";
  const pageCache = new Map();
  const prefetching = new Set();
  const journalRowsCache = new WeakMap();
  const journalSortStorageKey = "netvault:heatmap-sort:v1";
  const journalSortValues = new Set(["name-asc", "name-desc", "total-desc", "total-asc"]);
  const maxCachedPages = 10;
  const pageCacheTtlMs = 30000;
  const clientHashMaxBytes = 32 * 1024 * 1024;
  const activeUploadRequests = new Set();
  let journalFilterTimer = null;
  let journalMenuTarget = null;
  let journalLimitTimer = null;
  let journalLimitRequest = null;

  const qs = (selector, root = document) => root.querySelector(selector);
  const qsa = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const applyTheme = theme => {
    const dark = theme === "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    qsa("[data-theme-toggle]").forEach(button => {
      button.setAttribute("aria-pressed", String(dark));
      button.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
      const icon = qs("i", button);
      if (icon) icon.className = "fa-solid fa-fw " + (dark ? "fa-sun" : "fa-moon");
    });
  };
  applyTheme(document.documentElement.dataset.theme);
  document.addEventListener("click", event => {
    if (!event.target.closest("[data-theme-toggle]")) return;
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(theme);
    try { localStorage.setItem("netvault:theme:v1", theme); } catch { /* Keep this page's theme. */ }
  });

  const closeFilterMenus = (except = null) => {
    qsa("details.filter-menu[open]").forEach((menu) => {
      if (menu !== except) menu.removeAttribute("open");
    });
  };

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
    closeJournalMenu();
    window.clearTimeout(journalFilterTimer);
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
    const sidebar = qs(".app-sidebar");
    if (sidebar) {
      const targetPath = sameOriginUrl(url)?.pathname;
      qsa("a.sidebar-icon", sidebar).forEach((item) => {
        const active = sameOriginUrl(item.href)?.pathname === targetPath;
        item.classList.toggle("active", active);
        if (active) item.setAttribute("aria-current", "page");
        else item.removeAttribute("aria-current");
      });
    }

    if (push && url !== window.location.href) history.pushState({}, "", url);
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    setLoading(false);
    initHeatmap(currentMain);
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
    const group = link.closest(".app-sidebar, .primary-nav, .utility-nav, .filter-tabs");
    if (!group) return;
    qsa("a.active", group).forEach((item) => item.classList.remove("active"));
    link.classList.add("active");
    if (group.matches(".app-sidebar")) {
      qsa("a[aria-current]", group).forEach((item) => item.removeAttribute("aria-current"));
      link.setAttribute("aria-current", "page");
    }
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

  const doiBatchDraftKey = "netvault:doi-batch-draft:v1";
  const doiBatchMax = 500;
  // Align with the backend's DOI patterns. Extraction is explicitly opt-in.
  const doiBatchPattern = /\b10\.\d{4,9}\/[-._;()/:a-z0-9+%=&]+/gi;
  let doiBatchSubmitting = false;

  const normalizeBatchDoi = (value) => {
    let doi = String(value || "").trim().normalize("NFKC");
    doi = doi.replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "");
    doi = doi.replace(/^doi:\s*/i, "");
    try { doi = decodeURIComponent(doi); } catch { /* Keep original suffix. */ }
    doi = doi.trim().replace(/[\s.,;:\]}>}'"]+$/g, "").toLowerCase();
    while (doi.endsWith(")") && (doi.match(/\)/g) || []).length > (doi.match(/\(/g) || []).length)
      doi = doi.slice(0, -1);
    return /^10\.\d{4,9}\/\S+$/i.test(doi) && doi.length <= 255 ? doi : "";
  };

  const readDoiBatch = (dialog) => {
    const textarea = qs("[data-doi-batch-input]", dialog);
    const extract = qs("[data-doi-batch-regex]", dialog)?.checked;
    const raw = textarea?.value || "";
    const candidates = extract
      ? (raw.match(doiBatchPattern) || [])
      : raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    const dois = [], seen = new Set();
    let invalid = 0;
    for (const candidate of candidates) {
      const doi = normalizeBatchDoi(candidate);
      if (!doi) { invalid++; continue; }
      if (!seen.has(doi)) { seen.add(doi); dois.push(doi); }
    }
    return { dois, invalid, extract, raw };
  };

  const doiBatchDialog = () => qs("[data-doi-batch-dialog]");
  const persistDoiBatch = (dialog) => {
    const textarea = qs("[data-doi-batch-input]", dialog);
    const extract = qs("[data-doi-batch-regex]", dialog);
    if (!textarea || !extract) return;
    try {
      sessionStorage.setItem(doiBatchDraftKey, JSON.stringify({
        text: textarea.value.slice(0, 120000),
        extract: extract.checked,
      }));
    } catch { /* The DOI search works without storage. */ }
  };
  const restoreDoiBatch = (dialog) => {
    if (dialog.dataset.doiBatchRestored === "true") return;
    dialog.dataset.doiBatchRestored = "true";
    try {
      const saved = JSON.parse(sessionStorage.getItem(doiBatchDraftKey) || "{}");
      const input = qs("[data-doi-batch-input]", dialog);
      const regex = qs("[data-doi-batch-regex]", dialog);
      if (typeof saved.text === "string" && input) input.value = saved.text;
      if (typeof saved.extract === "boolean" && regex) regex.checked = saved.extract;
    } catch { /* Preserve an empty draft. */ }
  };
  const updateDoiBatchCount = (dialog) => {
    if (!dialog) return { dois: [], invalid: 0 };
    const parsed = readDoiBatch(dialog);
    const count = qs("[data-doi-batch-count]", dialog);
    const button = qs("[data-doi-batch-search]", dialog);
    if (count) count.textContent = parsed.dois.length.toLocaleString("en-US") + " DOIs";
    if (button) button.disabled = doiBatchSubmitting || parsed.dois.length === 0 || parsed.dois.length > doiBatchMax;
    return parsed;
  };
  const doiBatchStatus = (dialog, text = "") => {
    const status = qs("[data-doi-batch-status]", dialog);
    if (status) status.textContent = text;
  };
  const formatDoiBatch = (dialog) => {
    const input = qs("[data-doi-batch-input]", dialog);
    if (!input) return;
    const parsed = readDoiBatch(dialog);
    if (!parsed.extract && parsed.invalid) {
      doiBatchStatus(dialog, parsed.invalid + " invalid line(s). Enable regex for pasted text.");
      updateDoiBatchCount(dialog);
      return;
    }
    input.value = parsed.dois.join("\n");
    doiBatchStatus(dialog, parsed.dois.length ? "" : "No DOIs found.");
    updateDoiBatchCount(dialog);
    persistDoiBatch(dialog);
  };
  const openDoiBatch = (status = "") => {
    const dialog = doiBatchDialog();
    if (!dialog) return;
    restoreDoiBatch(dialog);
    updateDoiBatchCount(dialog);
    doiBatchStatus(dialog, status);
    if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
    else if (!dialog.open) dialog.setAttribute("open", "");
    qs("[data-doi-batch-input]", dialog)?.focus({ preventScroll: true });
  };

  const submitDoiBatch = async () => {
    const dialog = doiBatchDialog();
    if (!dialog || doiBatchSubmitting) return;
    restoreDoiBatch(dialog);
    const parsed = readDoiBatch(dialog);
    if (!parsed.extract && parsed.invalid) {
      openDoiBatch(parsed.invalid + " invalid DOI line(s). Check the entries or enable regex.");
      return;
    }
    if (!parsed.dois.length) {
      openDoiBatch("Enter at least one DOI.");
      return;
    }
    if (parsed.dois.length > doiBatchMax) {
      openDoiBatch("Maximum " + doiBatchMax + " DOIs per search.");
      return;
    }
    const input = qs("[data-doi-batch-input]", dialog);
    if (input) input.value = parsed.dois.join("\n");
    persistDoiBatch(dialog);
    updateDoiBatchCount(dialog);
    // Open the tab during the direct click, before awaiting the POST. Opening
    // after an asynchronous response would be blocked by many browsers.
    const resultTab = window.open("about:blank", "_blank");
    if (!resultTab) {
      openDoiBatch("Please allow pop-ups for NetVault to open DOI results in a new tab.");
      return;
    }
    try {
      resultTab.opener = null;
      const theme = getComputedStyle(document.documentElement);
      const bg = theme.getPropertyValue("--bg").trim() || "#15191f";
      const fg = theme.getPropertyValue("--fg").trim() || "#e9edf3";
      resultTab.document.title = "Searching DOIs - NetVault";
      resultTab.document.documentElement.style.backgroundColor = bg;
      resultTab.document.body.style.cssText = "padding:24px;font:14px system-ui,sans-serif;";
      resultTab.document.body.style.color = fg;
      resultTab.document.body.textContent = "Preparing DOI search results…";
    } catch { /* The new tab will still navigate to the results. */ }
    doiBatchSubmitting = true;
    updateDoiBatchCount(dialog);
    doiBatchStatus(dialog, "Searching…");
    try {
      const response = await fetch(dialog.dataset.batchUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": qs("[data-doi-batch-csrf]", dialog)?.value || "",
          "X-Requested-With": "fetch",
        },
        body: JSON.stringify({ dois: parsed.dois }),
      });
      const result = await response.json();
      if (!response.ok) {
        const detail = result.detail;
        throw new Error(typeof detail === "string" ? detail : "Search failed (HTTP " + response.status + ").");
      }
      const target = sameOriginUrl(result.url);
      if (!target || !isWebPath(target)) throw new Error("Invalid batch search destination.");
      if (resultTab.closed) throw new Error("The DOI results tab was closed before the search finished.");
      resultTab.location.replace(target.href);
      if (dialog.open && typeof dialog.close === "function") dialog.close();
      doiBatchStatus(dialog);
    } catch (error) {
      if (!resultTab.closed) resultTab.close();
      openDoiBatch(error.message || "Could not search the DOI list.");
    } finally {
      doiBatchSubmitting = false;
      if (dialog.isConnected) updateDoiBatchCount(dialog);
    }
  };

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-doi-batch-open]")) { openDoiBatch(); return; }
    if (event.target.closest("[data-doi-batch-search]")) {
      submitDoiBatch();
      return;
    }
    if (event.target.closest("[data-doi-batch-close]")) {
      const dialog = doiBatchDialog();
      if (dialog?.open && typeof dialog.close === "function") dialog.close();
      return;
    }
    if (event.target.closest("[data-doi-batch-format]")) {
      const dialog = doiBatchDialog();
      if (dialog) formatDoiBatch(dialog);
    }
  });
  document.addEventListener("change", (event) => {
    if (!event.target.matches?.("[data-doi-batch-regex]")) return;
    const dialog = event.target.closest("[data-doi-batch-dialog]");
    // Checking the box extracts DOI patterns and immediately rewrites the input.
    if (dialog) formatDoiBatch(dialog);
  });
  document.addEventListener("input", (event) => {
    if (!event.target.matches?.("[data-doi-batch-input]")) return;
    const dialog = event.target.closest("[data-doi-batch-dialog]");
    doiBatchStatus(dialog);
    updateDoiBatchCount(dialog);
    persistDoiBatch(dialog);
    if (event.inputType === "insertFromPaste" && qs("[data-doi-batch-regex]", dialog)?.checked) {
      window.setTimeout(() => { if (dialog?.isConnected) formatDoiBatch(dialog); }, 0);
    }
  });
  document.addEventListener("focusout", (event) => {
    if (!event.target.matches?.("[data-doi-batch-input]")) return;
    const dialog = event.target.closest("[data-doi-batch-dialog]");
    if (qs("[data-doi-batch-regex]", dialog)?.checked) formatDoiBatch(dialog);
  });

  const journalNameCollator = new Intl.Collator("en", { sensitivity: "base", numeric: true });
  const journalListNames = (textarea) => {
    const seen = new Set();
    return String(textarea ? textarea.value : "")
      .split(/\r?\n/)
      .map((name) => name.trim().toLocaleUpperCase("en-US"))
      .filter((name) => {
        const key = normalizedJournalText(name);
        if (!key || seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .sort(journalNameCollator.compare);
  };

  const updateJournalListCount = (dialog) => {
    const textarea = qs("[data-journal-list-textarea]", dialog);
    const count = journalListNames(textarea).length;
    const output = qs("[data-journal-list-count]", dialog);
    if (output) output.textContent = `${count} journal${count === 1 ? "" : "s"}`;
  };

  const normalizeJournalListTextarea = (textarea) => {
    if (!textarea) return;
    const sorted = journalListNames(textarea).join("\n");
    if (textarea.value !== sorted) textarea.value = sorted;
    updateJournalListCount(textarea.closest("[data-journal-list-dialog]"));
  };

  // Native datalist popovers ignore theme colors and can overflow dialogs.
  const journalPickerCache = new WeakMap();
  const journalPickerOptions = (dialog) => {
    if (!journalPickerCache.has(dialog)) {
      journalPickerCache.set(dialog, qsa("#journal-list-options option", dialog)
        .map((option) => ({ label: option.value, search: normalizedJournalText(option.value) }))
        .sort((a, b) => journalNameCollator.compare(a.label, b.label)));
    }
    return journalPickerCache.get(dialog);
  };

  const closeJournalSuggestions = (input) => {
    if (!input) return;
    const list = qs("[data-journal-list-suggestions]", input.closest("[data-journal-list-dialog]"));
    if (list) {
      list.hidden = true;
      list.replaceChildren();
    }
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  };

  const renderJournalSuggestions = (input) => {
    const dialog = input.closest("[data-journal-list-dialog]");
    const list = dialog ? qs("[data-journal-list-suggestions]", dialog) : null;
    const picker = dialog ? qs("[data-custom-journal-picker]", dialog) : null;
    if (!list || !picker || picker.hidden) return;
    const query = normalizedJournalText(input.value.trim());
    const matching = journalPickerOptions(dialog)
      .filter((option) => option.search.includes(query)).slice(0, 10);
    if (!matching.length) {
      closeJournalSuggestions(input);
      return;
    }
    const items = matching.map((option, index) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "journal-list-suggestion";
      item.id = "journal-list-suggestion-" + index;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", "false");
      item.dataset.journalSuggestion = option.label;
      item.textContent = option.label;
      return item;
    });
    list.replaceChildren(...items);
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    input.removeAttribute("aria-activedescendant");
  };

  const chooseJournalSuggestion = (input, item) => {
    input.value = item.dataset.journalSuggestion || "";
    input.focus();
    closeJournalSuggestions(input);
  };

  const addJournalListSelection = (input) => {
    const dialog = input.closest("[data-journal-list-dialog]");
    const textarea = qs("[data-journal-list-textarea]", dialog);
    const feedback = qs("[data-journal-list-feedback]", dialog);
    const requested = normalizedJournalText(input.value.trim());
    if (!dialog || !textarea || !requested) return;
    const options = qsa("#journal-list-options option", dialog).map((option) => ({
      label: option.value,
      name: normalizedJournalText(option.value),
    }));
    let match = options.find((option) => option.name === requested);
    if (!match) {
      const partialMatches = options.filter((option) => option.name.includes(requested));
      if (partialMatches.length === 1) match = partialMatches[0];
    }
    if (!match) {
      if (feedback) feedback.textContent = "Choose a journal name from the list.";
      return;
    }
    const names = journalListNames(textarea);
    if (names.some((name) => normalizedJournalText(name) === match.name)) {
      if (feedback) feedback.textContent = `${match.label} is already in this list.`;
    } else {
      textarea.value = [...names, match.label].join("\n");
      normalizeJournalListTextarea(textarea);
      if (feedback) feedback.textContent = `Added ${match.label}. Save the list to apply changes.`;
      updateJournalListCount(dialog);
    }
    input.value = "";
    input.focus();
    closeJournalSuggestions(input);
  };

  let journalListOpenSequence = 0;
  const journalListPayloadCache = new Map();
  const journalListPayloadPending = new Map();
  const journalListCacheTtlMs = 30000;

  const loadJournalListPayload = (endpoint) => {
    const entry = journalListPayloadCache.get(endpoint);
    if (entry && Date.now() - entry.storedAt < journalListCacheTtlMs)
      return Promise.resolve(entry.payload);
    if (journalListPayloadPending.has(endpoint))
      return journalListPayloadPending.get(endpoint);
    const pending = (async () => {
      const response = await fetch(endpoint, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { "X-Requested-With": "fetch" },
      });
      if (!response.ok) throw new Error(await journalListError(response));
      const payload = await response.json();
      journalListPayloadCache.delete(endpoint);
      journalListPayloadCache.set(endpoint, { payload, storedAt: Date.now() });
      while (journalListPayloadCache.size > 20)
        journalListPayloadCache.delete(journalListPayloadCache.keys().next().value);
      return payload;
    })();
    journalListPayloadPending.set(endpoint, pending);
    pending.finally(() => journalListPayloadPending.delete(endpoint)).catch(() => {});
    return pending;
  };

  const closeJournalListEditor = (dialog) => {
    if (!dialog) return;
    journalListOpenSequence++;
    closeJournalSuggestions(qs("[data-journal-list-picker-input]", dialog));
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else dialog.removeAttribute("open");
  };

  const journalListError = async (response) => {
    try {
      const payload = await response.json();
      return payload.detail || `HTTP ${response.status}`;
    } catch {
      return `HTTP ${response.status}`;
    }
  };

  const populateJournalListEditor = (dialog, payload) => {
    const title = qs("[data-journal-list-title]", dialog);
    const textarea = qs("[data-journal-list-textarea]", dialog);
    const source = qs("[data-journal-list-source]", dialog);
    const reset = qs("[data-journal-list-reset]", dialog);
    const resetLabel = qs("[data-journal-list-reset-label]", dialog);
    const feedback = qs("[data-journal-list-feedback]", dialog);
    const keyInput = qs("input[name='filter_key']", dialog);
    const nameField = qs("[data-custom-list-name-field]", dialog);
    const nameInput = qs("[data-custom-list-name]", dialog);
    const picker = qs("[data-custom-journal-picker]", dialog);
    const pickerInput = qs("[data-journal-list-picker-input]", dialog);
    if (title) {
      title.textContent = payload.custom
        ? `Edit ${payload.label || "custom list"}`
        : `${payload.label || "Journal"} list`;
    }
    if (textarea) {
      textarea.value = Array.isArray(payload.journals) ? payload.journals.join("\n") : "";
      textarea.disabled = false;
      normalizeJournalListTextarea(textarea);
    }
    if (keyInput) keyInput.value = payload.key || "";
    if (source) {
      source.hidden = !payload.source_url;
      source.href = payload.source_url || "#";
      source.textContent = payload.source ? `Default: ${payload.source}` : "Default source";
    }
    if (nameField) nameField.hidden = !payload.custom;
    if (nameInput) nameInput.value = payload.custom ? (payload.label || "Custom list") : "";
    if (picker) picker.hidden = !payload.custom;
    if (pickerInput) {
      pickerInput.value = "";
      closeJournalSuggestions(pickerInput);
    }
    if (reset) reset.hidden = !(payload.can_reset || payload.can_delete);
    if (resetLabel) resetLabel.textContent = payload.can_delete ? "Delete list" : "Reset default";
    if (feedback) feedback.textContent = "";
    updateJournalListCount(dialog);
  };

  const openJournalListEditor = async (tab) => {
    const dialog = qs("[data-journal-list-dialog]");
    const endpoint = tab?.getAttribute("data-journal-list-url") || "";
    if (!dialog || !endpoint) return;
    const sequence = ++journalListOpenSequence;
    const trigger = qs("[data-journal-list-edit-trigger]", tab);
    if (trigger) trigger.setAttribute("aria-busy", "true");
    try {
      // Avoid opening an empty modal, loading text, and then resizing it.
      // Nearby hover/focus prefetch and the short-lived snapshot speed up repeats.
      const payload = await loadJournalListPayload(endpoint);
      if (sequence !== journalListOpenSequence || !dialog.isConnected || !tab.isConnected)
        return;
      dialog.dataset.endpoint = endpoint;
      populateJournalListEditor(dialog, payload);
      const saveButton = qs("[data-journal-list-form] button[type='submit']", dialog);
      if (saveButton) saveButton.disabled = false;
      if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
      else if (!dialog.open) dialog.setAttribute("open", "");
      const textarea = qs("[data-journal-list-textarea]", dialog);
      if (textarea) {
        textarea.focus({ preventScroll: true });
        textarea.setSelectionRange(0, 0);
        textarea.scrollTop = 0;
      }
    } catch (error) {
      if (sequence !== journalListOpenSequence || !dialog.isConnected) return;
      // Errors still need a clear recovery path, but never show an empty editor first.
      dialog.dataset.endpoint = endpoint;
      const title = qs("[data-journal-list-title]", dialog);
      const textarea = qs("[data-journal-list-textarea]", dialog);
      const feedback = qs("[data-journal-list-feedback]", dialog);
      if (title) title.textContent = "Unable to load journal list";
      if (textarea) { textarea.value = ""; textarea.disabled = true; }
      const picker = qs("[data-custom-journal-picker]", dialog);
      if (picker) picker.hidden = true;
      if (feedback) feedback.textContent = error.message || "Could not load the journal list.";
      const saveButton = qs("[data-journal-list-form] button[type='submit']", dialog);
      if (saveButton) saveButton.disabled = true;
      if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
      else if (!dialog.open) dialog.setAttribute("open", "");
      qs("[data-journal-list-close]", dialog)?.focus({ preventScroll: true });
    } finally {
      if (trigger?.isConnected) trigger.removeAttribute("aria-busy");
    }
  };

  // Warm the editor only on deliberate intent; do not fetch all lists on page load.
  document.addEventListener("pointerover", (event) => {
    const trigger = event.target.closest?.("[data-journal-list-edit-trigger]");
    if (!trigger || trigger.contains(event.relatedTarget)) return;
    const endpoint = trigger.closest("[data-journal-list-edit]")?.getAttribute("data-journal-list-url");
    if (endpoint) loadJournalListPayload(endpoint).catch(() => {});
  });
  document.addEventListener("focusin", (event) => {
    const trigger = event.target.closest?.("[data-journal-list-edit-trigger]");
    const endpoint = trigger?.closest("[data-journal-list-edit]")?.getAttribute("data-journal-list-url");
    if (endpoint) loadJournalListPayload(endpoint).catch(() => {});
  });

  const saveJournalListEditor = async (form) => {
    const dialog = form.closest("[data-journal-list-dialog]");
    const endpoint = dialog ? dialog.dataset.endpoint : "";
    const textarea = qs("[data-journal-list-textarea]", form);
    const feedback = qs("[data-journal-list-feedback]", form);
    const csrfToken = qs("input[name='csrf_token']", form)?.value || "";
    const nameInput = qs("[data-custom-list-name]", form);
    if (!dialog || !endpoint || !textarea) return;
    normalizeJournalListTextarea(textarea);
    setFormBusy(form, true, "Saving...");
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "X-Requested-With": "fetch",
        },
        body: JSON.stringify({
          journals: journalListNames(textarea),
          name: nameInput && !nameInput.closest("[hidden]") ? nameInput.value.trim() : undefined,
        }),
      });
      if (!response.ok) throw new Error(await journalListError(response));
      const payload = await response.json();
      journalListPayloadCache.delete(endpoint);
      pageCache.clear();
      closeJournalListEditor(dialog);
      const current = sameOriginUrl(window.location.href);
      if (payload.deleted && current) current.searchParams.set("filter", "custom");
      fetchPage(current ? current.href : window.location.href, {}, false);
    } catch (error) {
      if (feedback) feedback.textContent = error.message || "Could not save the journal list.";
    } finally {
      if (form.isConnected) setFormBusy(form, false, "Save list");
    }
  };

  const createCustomJournalList = async (button) => {
    const endpoint = button.getAttribute("data-endpoint") || "";
    const csrfToken = qs("input[name='csrf_token']", qs("[data-journal-list-form]"))?.value || "";
    const name = window.prompt("Name this journal list:", "Custom list");
    if (!endpoint || name === null || !name.trim()) return;
    button.disabled = true;
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "X-Requested-With": "fetch",
        },
        body: JSON.stringify({ name: name.trim() }),
      });
      if (!response.ok) throw new Error(await journalListError(response));
      const payload = await response.json();
      pageCache.clear();
      fetchPage(`${window.location.pathname}?filter=${encodeURIComponent(payload.key)}`, {}, true);
    } catch (error) {
      window.alert(error.message || "Could not create the journal list.");
      button.disabled = false;
    }
  };

  const resetJournalListEditor = async (button) => {
    const dialog = button.closest("[data-journal-list-dialog]");
    const form = button.closest("form");
    const endpoint = dialog ? dialog.dataset.endpoint : "";
    const feedback = qs("[data-journal-list-feedback]", dialog);
    const csrfToken = qs("input[name='csrf_token']", form)?.value || "";
    if (!dialog || !endpoint) return;
    button.disabled = true;
    try {
      const response = await fetch(endpoint, {
        method: "DELETE",
        credentials: "same-origin",
        headers: {
          "X-CSRF-Token": csrfToken,
          "X-Requested-With": "fetch",
        },
      });
      if (!response.ok) throw new Error(await journalListError(response));
      const payload = await response.json();
      journalListPayloadCache.delete(endpoint);
      pageCache.clear();
      closeJournalListEditor(dialog);
      const current = sameOriginUrl(window.location.href);
      if (payload.deleted && current) current.searchParams.set("filter", "custom");
      fetchPage(current ? current.href : window.location.href, {}, false);
    } catch (error) {
      if (feedback) feedback.textContent = error.message || "Could not reset the journal list.";
      button.disabled = false;
    }
  };

  const normalizedJournalText = (value) => value.normalize("NFKC").toLocaleLowerCase();
  const journalStopWords = new Set([
    "a", "an", "the", "and", "or", "of", "for", "in", "on", "at", "to", "by", "with", "from", "as",
  ]);
  const journalInitials = (value) => (normalizedJournalText(value).match(/[\p{L}\p{N}]+/gu) || [])
    .filter((word) => !journalStopWords.has(word))
    .map((word) => Array.from(word)[0]).join("");

  // Keep the full journal dataset, but create DOM cells only for visible rows.
  const journalRows = (heatmap) => {
    if (journalRowsCache.has(heatmap)) return journalRowsCache.get(heatmap);
    const dataNode = qs("[data-journal-heatmap-data]", heatmap.closest(".heatmap-panel"));
    const years = qsa(".heat-year span", heatmap).map((node) => node.textContent.trim());
    let payload = [];
    try {
      payload = dataNode ? JSON.parse(dataNode.textContent || "[]") : [];
    } catch {
      payload = [];
    }
    const rows = payload.map(([label, total, cells], index) => ({
      id: index,
      label,
      name: normalizedJournalText(label),
      initials: journalInitials(label),
      total: Number(total),
      counts: new Map(cells.map(([yearIndex, count, level]) => [years.length - 1 - yearIndex, [count, level]])),
      elements: null,
    }));
    const result = { rows, years };
    journalRowsCache.set(heatmap, result);
    return result;
  };

  const renderJournalRow = (row, years, searchUrl) => {
    if (row.elements) return row.elements;
    const elements = [];
    const label = document.createElement("div");
    label.className = "heat-journal";
    label.tabIndex = 0;
    label.setAttribute("aria-haspopup", "menu");
    label.dataset.journalRow = String(row.id);
    label.dataset.journalName = row.label;
    label.dataset.journalTotal = String(row.total);
    label.setAttribute("role", "button");
    const name = document.createElement("span");
    name.className = "heat-journal-name";
    name.textContent = row.label;
    label.append(name);
    years.forEach((year, index) => {
      const data = row.counts.get(index);
      // A button keeps the original click navigation without triggering the
      // browser's unstyleable white link-preview bar on hover.
      const cell = document.createElement(data ? "button" : "span");
      cell.className = "heat-cell level-" + (data ? data[1] : 0);
      cell.dataset.journalRow = String(row.id);
      const count = data ? data[0] : 0;
      cell.dataset.heatYear = String(year);
      cell.dataset.heatCount = String(count);
      if (data) {
        const params = new URLSearchParams({ journal: row.label, year });
        cell.type = "button";
        cell.dataset.pdfUrl = searchUrl + "?" + params.toString();
        cell.setAttribute("aria-label", row.label + " " + year + " " + count.toLocaleString("en-US") + " PDFs");
      } else {
        cell.setAttribute("aria-hidden", "true");
      }
      elements.push(cell);
    });
    const total = document.createElement("div");
    total.className = "heat-total";
    total.dataset.journalRow = String(row.id);
    total.textContent = row.total.toLocaleString("en-US");
    elements.push(total);
    elements.push(label);
    row.elements = elements;
    return elements;
  };

  // One floating, theme-aware tooltip for the entire heatmap. It lives outside
  // the scrolling year viewport so it never gets clipped or shifts the grid.
  let heatTooltip = null;
  let heatTooltipCell = null;
  let heatTooltipFrame = 0;
  let heatPointerX = 0;
  let heatPointerY = 0;
  const ensureHeatTooltip = () => {
    if (heatTooltip?.isConnected) return heatTooltip;
    heatTooltip = document.createElement("div");
    heatTooltip.className = "heat-cell-tooltip";
    heatTooltip.setAttribute("role", "tooltip");
    heatTooltip.id = "heat-cell-tooltip";
    heatTooltip.hidden = true;
    const yearNode = document.createElement("span");
    yearNode.className = "heat-cell-tooltip-year";
    const countNode = document.createElement("span");
    countNode.className = "heat-cell-tooltip-count";
    heatTooltip.append(yearNode, countNode);
    document.body.append(heatTooltip);
    return heatTooltip;
  };
  const placeHeatTooltip = (x, y) => {
    if (!heatTooltip || heatTooltip.hidden) return;
    const width = heatTooltip.offsetWidth;
    const height = heatTooltip.offsetHeight;
    const margin = 10;
    const xOffset = 14, yOffset = 18;
    // Prefer the lower right of the pointer; flip within the viewport at edges.
    const left = x + xOffset + width <= innerWidth - margin
      ? x + xOffset : Math.max(margin, x - width - xOffset);
    const top = y + yOffset + height <= innerHeight - margin
      ? y + yOffset : Math.max(margin, y - height - yOffset);
    heatTooltip.style.left = Math.min(left, innerWidth - width - margin) + "px";
    heatTooltip.style.top = Math.min(top, innerHeight - height - margin) + "px";
  };
  const moveHeatTooltip = (x, y) => {
    heatPointerX = x;
    heatPointerY = y;
    if (heatTooltipFrame) return;
    heatTooltipFrame = requestAnimationFrame(() => {
      heatTooltipFrame = 0;
      placeHeatTooltip(heatPointerX, heatPointerY);
    });
  };
  const showHeatTooltip = (cell, x, y) => {
    if (!cell?.isConnected) return;
    const tooltip = ensureHeatTooltip();
    const year = cell.dataset.heatYear || "";
    const count = Number(cell.dataset.heatCount || 0).toLocaleString("en-US");
    tooltip.firstElementChild.textContent = year;
    tooltip.lastElementChild.textContent = count + " PDFs";
    if (heatTooltipCell !== cell) {
      heatTooltipCell?.removeAttribute("aria-describedby");
      heatTooltipCell = cell;
      if (cell.tagName === "BUTTON") cell.setAttribute("aria-describedby", tooltip.id);
    }
    tooltip.hidden = false;
    moveHeatTooltip(x, y);
  };
  const hideHeatTooltip = () => {
    if (heatTooltipFrame) cancelAnimationFrame(heatTooltipFrame);
    heatTooltipFrame = 0;
    heatTooltipCell?.removeAttribute("aria-describedby");
    heatTooltipCell = null;
    if (heatTooltip) heatTooltip.hidden = true;
  };
  const heatCellAt = (target) => target?.closest?.(".journal-heatmap .heat-cell[data-heat-year]");
  document.addEventListener("pointerover", (event) => {
    if (event.pointerType === "touch") return;
    const cell = heatCellAt(event.target);
    if (cell && cell !== heatCellAt(event.relatedTarget))
      showHeatTooltip(cell, event.clientX, event.clientY);
  });
  document.addEventListener("pointermove", (event) => {
    if (event.pointerType === "touch") return;
    const cell = heatCellAt(event.target);
    if (cell && cell !== heatTooltipCell) showHeatTooltip(cell, event.clientX, event.clientY);
    else if (cell) moveHeatTooltip(event.clientX, event.clientY);
    else if (heatTooltipCell) hideHeatTooltip();
  }, { passive: true });
  document.addEventListener("pointerout", (event) => {
    if (heatCellAt(event.target) && !heatCellAt(event.relatedTarget)) hideHeatTooltip();
  });
  document.addEventListener("focusin", (event) => {
    const cell = heatCellAt(event.target);
    if (!cell || cell.tagName !== "BUTTON") return;
    const rect = cell.getBoundingClientRect();
    showHeatTooltip(cell, rect.right, rect.bottom);
  });
  document.addEventListener("focusout", (event) => {
    if (heatCellAt(event.target)) hideHeatTooltip();
  });
  document.addEventListener("scroll", () => {
    if (heatTooltipCell) hideHeatTooltip();
  }, { capture: true, passive: true });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && heatTooltipCell) hideHeatTooltip();
  });

  // Buttons preserve same-origin PJAX navigation without exposing a native
  // URL preview on hover; modifier-click still supports opening a new tab.
  document.addEventListener("click", (event) => {
    const cell = event.target.closest?.(".journal-heatmap button.heat-cell[data-pdf-url]");
    if (!cell) return;
    const url = sameOriginUrl(cell.dataset.pdfUrl);
    if (!url || !isWebPath(url)) return;
    hideHeatTooltip();
    if (event.metaKey || event.ctrlKey || event.shiftKey) {
      window.open(url.href, "_blank", "noopener,noreferrer");
    } else {
      fetchPage(url.href);
    }
  });

  document.addEventListener("auxclick", (event) => {
    if (event.button !== 1) return;
    const cell = event.target.closest?.(".journal-heatmap button.heat-cell[data-pdf-url]");
    if (!cell) return;
    const url = sameOriginUrl(cell.dataset.pdfUrl);
    if (!url || !isWebPath(url)) return;
    event.preventDefault();
    hideHeatTooltip();
    window.open(url.href, "_blank", "noopener,noreferrer");
  });

  const sortJournalRows = (select) => {
    applyJournalFilters(select.closest(".heatmap-panel"));
  };

  const loadJournalSort = () => {
    try {
      const value = window.localStorage.getItem(journalSortStorageKey) || "";
      return journalSortValues.has(value) ? value : "total-desc";
    } catch {
      return "total-desc";
    }
  };

  const saveJournalSort = (value) => {
    if (!journalSortValues.has(value)) return;
    try {
      window.localStorage.setItem(journalSortStorageKey, value);
    } catch {
      // Sorting still works for this page when storage is unavailable.
    }
  };

  // Use the visible heatmap's natural content width rather than stretching
  // its year scroller to the full dashboard maximum on shorter journal lists.
  const fitDashboardWidth = (panel) => {
    const page = panel?.closest(".page");
    const frame = qs(".heatmap-scroll", panel);
    const yearGrid = qs(".journal-heatmap", panel);
    const fixed = qs("[data-heatmap-fixed-body]", panel);
    if (!page || !frame || !yearGrid || !fixed) return;
    const mobile = window.matchMedia("(max-width: 760px)").matches;
    const intrinsicLabelWidth = Math.max(0, ...qsa(".heat-journal-name", fixed)
      .map((name) => name.scrollWidth));
    // On small screens retain the established compact fixed-column sizing.
    const nameWidth = mobile ? null : Math.min(540,
      Math.max(190, Math.ceil(intrinsicLabelWidth + 20)));
    if (nameWidth === null) frame.style.removeProperty("--heat-journal-width");
    else frame.style.setProperty("--heat-journal-width", nameWidth + "px");
    const computed = getComputedStyle(frame);
    const totalWidth = parseFloat(computed.getPropertyValue("--heat-total-width")) || 60;
    const fixedNameWidth = nameWidth ?? (parseFloat(computed.getPropertyValue("--heat-journal-width")) || 180);
    const naturalWidth = yearGrid.scrollWidth + totalWidth + fixedNameWidth + 12;
    const preferredWidth = Math.min(1320, Math.max(640, Math.ceil(naturalWidth)));
    page.style.setProperty("--dashboard-content-width", preferredWidth + "px");
  };

  const applyJournalFilters = (panel) => {
    if (!panel || !panel.isConnected) return;
    const heatmap = qs(".journal-heatmap", panel);
    if (!heatmap) return;
    const input = qs("[data-journal-filter]", panel);
    const query = normalizedJournalText(input ? input.value.trim() : "");
    // Journal initialisms omit function words (for example IJIR).
    // Accept spaced/dotted initialism input as well as compact uppercase initials.
    const initialsQuery = (query.match(/[\p{L}\p{N}]+/gu) || []).join("");
    const isInitialsQuery = /^[\p{L}\p{N}]{2,}$/u.test(initialsQuery);
    const { rows, years } = journalRows(heatmap);
    const sortSelect = qs("[data-journal-sort]", panel);
    const sortValue = sortSelect?.value || "total-desc";
    qsa("[data-journal-sort-toggle]", panel).forEach((button) => {
      const selected = sortValue.startsWith(button.dataset.journalSortToggle + "-");
      button.setAttribute("aria-pressed", String(selected));
      button.title = selected
        ? ("Sorted " + (sortValue.endsWith("desc") ? "descending" : "ascending") + "; click to reverse")
        : ("Sort by " + (button.dataset.journalSortToggle === "total" ? "total" : "journal name"));
    });
    const [field, direction] = sortValue.split("-");
    const multiplier = direction === "desc" ? -1 : 1;
    const sorted = [...rows].sort((left, right) => {
      if (field === "total" && left.total !== right.total) {
        return (left.total - right.total) * multiplier;
      }
      const nameOrder = left.label.localeCompare(right.label, undefined, {
        numeric: true, sensitivity: "base",
      });
      return field === "name" ? nameOrder * multiplier : nameOrder;
    });
    const matches = sorted.filter((row) => !query || row.name.includes(query)
      || (isInitialsQuery && row.initials.includes(initialsQuery)));
    const limit = Math.max(1, Math.min(200, Number(qs("[data-journal-limit]", panel)?.value || 20)));
    const pageCount = Math.max(1, Math.ceil(matches.length / limit));
    const requestedPage = Number(panel.dataset.journalPage || 1);
    const page = Number.isInteger(requestedPage) ? Math.min(pageCount, Math.max(1, requestedPage)) : 1;
    panel.dataset.journalPage = String(page);
    const offset = (page - 1) * limit;
    const shown = matches.slice(offset, offset + limit);
    const pager = qs("[data-journal-pagination]", panel);
    if (pager) {
      const start = matches.length ? offset + 1 : 0;
      const end = offset + shown.length;
      const range = qs("[data-journal-page-range]", pager);
      const number = qs("[data-journal-page-input]", pager);
      const total = qs("[data-journal-page-total]", pager);
      const prev = qs("[data-journal-page-prev]", pager);
      const next = qs("[data-journal-page-next]", pager);
      if (range) range.textContent = `Showing ${start}–${end} of ${matches.length} journals`;
      if (number) {
        number.max = String(pageCount);
        number.value = String(page);
      }
      if (total) total.textContent = `of ${pageCount}`;
      if (prev) prev.disabled = page <= 1;
      if (next) next.disabled = page >= pageCount;
      pager.hidden = matches.length === 0;
    }
    const fixedBody = qs("[data-heatmap-fixed-body]", panel);
    if (!fixedBody) return;
    const yearNodes = document.createDocumentFragment();
    const fixedNodes = document.createDocumentFragment();
    qsa(".heat-year", heatmap).forEach((node) => yearNodes.append(node));
    shown.forEach((row) => renderJournalRow(row, years, panel.dataset.pdfSearchUrl)
      .forEach((node, index) => (index < years.length ? yearNodes : fixedNodes).append(node)));
    heatmap.replaceChildren(yearNodes);
    fixedBody.replaceChildren(fixedNodes);
    fitDashboardWidth(panel);

    const status = qs("[data-journal-filter-status], .heatmap-filter-status", panel);
    if (status) {
      const noun = rows.length === 1 ? "journal" : "journals";
      status.textContent = query ? matches.length + " of " + rows.length + " " + noun : rows.length + " " + noun;
    }
    const suggestion = qs("[data-journal-suggestion]", panel);
    if (suggestion) {
      const first = query ? matches[0] : null;
      suggestion.textContent = first ? "Tab to complete: " + first.label : "";
      suggestion.hidden = !first;
    }
    const noMatches = qs("[data-journal-no-matches]", panel);
    if (noMatches) noMatches.hidden = shown.length !== 0;
    const frame = qs(".heatmap-scroll", panel);
    const scroll = qs("[data-heatmap-years-viewport]", panel);
    if (frame) frame.hidden = shown.length === 0;
    if (scroll && frame) {
      const syncFixedRows = () => {
        const fixed = qs("[data-heatmap-fixed-body]", panel);
        if (fixed) fixed.style.transform = "translate3d(0, " + (-scroll.scrollTop) + "px, 0)";
      };
      syncFixedRows();
      if (!scroll.dataset.yearScrollReady) {
        scroll.dataset.yearScrollReady = "true";
        // Oldest years at left, newest at right. Only year cells scroll horizontally.
        const alignLatest = () => {
          if (!scroll.isConnected || !scroll.clientWidth) return;
          scroll.scrollLeft = Math.max(0, scroll.scrollWidth - scroll.clientWidth);
        };
        requestAnimationFrame(alignLatest);
        const observer = new ResizeObserver(() => {
          if (scroll.dataset.userYearScroll !== "true") alignLatest();
          syncFixedRows();
        });
        observer.observe(scroll);
        scroll.addEventListener("scroll", syncFixedRows, { passive: true });
        const fixedViewport = qs("[data-heatmap-fixed-viewport]", panel);
        if (fixedViewport) {
          fixedViewport.addEventListener("wheel", (event) => {
            if (event.deltaY === 0 && event.deltaX === 0) return;
            scroll.scrollTop += event.deltaY;
            if (event.deltaX !== 0) {
              scroll.scrollLeft += event.deltaX;
              scroll.dataset.userYearScroll = "true";
            }
            event.preventDefault();
          }, { passive: false });
        }
        const userScrolled = () => { scroll.dataset.userYearScroll = "true"; };
        scroll.addEventListener("pointerdown", userScrolled, { passive: true });
        scroll.addEventListener("touchstart", userScrolled, { passive: true });
        scroll.addEventListener("wheel", (event) => {
          if (Math.abs(event.deltaX) > 0 || event.shiftKey) userScrolled();
        }, { passive: true });
        scroll.addEventListener("keydown", (event) => {
          if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) userScrolled();
        });
      }
    }
  };

  const goToJournalPage = (panel, requestedPage) => {
    if (!panel || !Number.isInteger(requestedPage)) return;
    panel.dataset.journalPage = String(Math.max(1, requestedPage));
    closeJournalMenu();
    applyJournalFilters(panel);
    const scroll = qs("[data-heatmap-years-viewport]", panel);
    if (scroll) scroll.scrollTop = 0;
  };

  const scheduleJournalFilter = (input) => {
    input.closest(".heatmap-panel").dataset.journalPage = "1";
    window.clearTimeout(journalFilterTimer);
    if (input.dataset.composing === "true") return;
    journalFilterTimer = window.setTimeout(() => applyJournalFilters(input.closest(".heatmap-panel")), 200);
  };

  const flushJournalFilter = (input) => {
    input.closest(".heatmap-panel").dataset.journalPage = "1";
    window.clearTimeout(journalFilterTimer);
    if (input.dataset.composing === "true") return;
    applyJournalFilters(input.closest(".heatmap-panel"));
  };

  const closeJournalMenu = (restoreFocus = false) => {
    const menu = qs("[data-journal-context-menu]");
    if (menu) menu.hidden = true;
    if (restoreFocus && journalMenuTarget?.isConnected) journalMenuTarget.focus({ preventScroll: true });
    journalMenuTarget = null;
  };

  const openJournalMenu = (target, event) => {
    const menu = qs("[data-journal-context-menu]");
    const link = qs("[data-journal-wos-link]", menu);
    const name = (target.dataset.journalName || "").replace(/\s+/g, " ").trim();
    if (!menu || !link || !name) return;
    closeJournalMenu();
    const queryJson = JSON.stringify([{ rowBoolean: null, rowField: "SO", rowText: name }]);
    link.href = `https://www.webofscience.com/wos/woscc/general-summary?queryJson=${encodeURIComponent(queryJson)}`;
    journalMenuTarget = target;
    menu.dataset.journalName = name;
    qs("[data-journal-copy]", menu).setAttribute("data-copy", name);
    menu.hidden = false;
    const rect = target.getBoundingClientRect();
    const keyboard = event.type === "keydown" || (!event.clientX && !event.clientY);
    const x = keyboard ? rect.left : event.clientX;
    const y = keyboard ? rect.bottom : event.clientY;
    menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8))}px`;
    menu.dataset.anchorX = String(x);
    menu.dataset.anchorY = String(y);
    qs("[data-journal-copy]", menu).focus({ preventScroll: true });
  };

  let rankSequence = 0;
  let rankResult = null;
  let rankDrag = null;
  const rankPositionStorageKey = "netvault:rank-position:v1";
  const rankCacheStorageKey = "netvault:journal-ranks:v1";
  const rankCacheTtlMs = 24 * 60 * 60 * 1000;
  const rankCache = new Map();
  const rankPending = new Map();
  let rankPosition = null;
  const validRankResult = (data) => data && typeof data.journal_name === "string"
    && Number.isFinite(data.queried_at) && data.queried_at > 0
    && data.queried_at * 1000 <= Date.now() + 300000
    && Array.isArray(data.rows) && data.rows.length <= 500
    && data.rows.every(row => row && ["key", "name", "rank", "source"].every(
      field => typeof row[field] === "string" && row[field].length <= 4000));
  try {
    const position = JSON.parse(localStorage.getItem(rankPositionStorageKey));
    if (position && Number.isFinite(position.x) && Number.isFinite(position.y)) rankPosition = position;
    const entries = JSON.parse(localStorage.getItem(rankCacheStorageKey) || "[]");
    if (Array.isArray(entries)) entries.slice(-100).forEach(entry => {
      if (Array.isArray(entry) && typeof entry[0] === "string" && validRankResult(entry[1]))
        rankCache.set(entry[0], entry[1]);
    });
  } catch { /* Memory caching still works when browser storage is unavailable. */ }
  const rankIdentity = (url, name) => url + "\0" + name.trim().replace(/\s+/g, " ").toLowerCase();
  const cacheRanks = (key, data) => {
    const previous = rankCache.get(key);
    if (previous && previous.queried_at > data.queried_at) return;
    rankCache.delete(key);
    rankCache.set(key, data);
    while (rankCache.size > 100) rankCache.delete(rankCache.keys().next().value);
    try { localStorage.setItem(rankCacheStorageKey, JSON.stringify([...rankCache])); } catch { /* Keep the memory cache. */ }
  };
  const clampRankPanel = (panel, x, y) => {
    const top = Math.max(8, Math.min(y, innerHeight - 160));
    panel.style.maxHeight = `${Math.max(0, innerHeight - top - 8)}px`;
    panel.style.left = `${Math.max(8, Math.min(x, innerWidth - panel.offsetWidth - 8))}px`;
    panel.style.top = `${top}px`;
  };
  const rememberRankPosition = () => {
    if (!rankDrag?.moved) { rankDrag = null; return; }
    rankPosition = {x: parseFloat(rankDrag.panel.style.left), y: parseFloat(rankDrag.panel.style.top)};
    try { localStorage.setItem(rankPositionStorageKey, JSON.stringify(rankPosition)); } catch { /* Keep the position for this page. */ }
    rankDrag = null;
  };
  // These dataset names are UI labels, so keep them in English.
  // Normalize lookup keys for previously cached ranking responses as well.
  const rankDatasetLabels = new Map(Object.entries({
    swufe: "Southwestern University of Finance and Economics",
    cufe: "Central University of Finance and Economics",
    uibe: "University of International Business and Economics",
    sdufe: "Shandong University of Finance and Economics",
    xdu: "Xidian University",
    swjtu: "Southwest Jiaotong University",
    ruc: "Renmin University of China",
    xmu: "Xiamen University",
    sjtu: "Shanghai Jiao Tong University",
    fdu: "Fudan University",
    hhu: "Hohai University",
    pku: "Peking University Core Journals",
    scu: "Sichuan University",
    sciUpTop: "CAS Upgraded Top Quartile",
    xrWarn: "Xinrui Academic Warning List",
    cqu: "Chongqing University",
    nju: "Nanjing University",
    xju: "Xinjiang University",
    cug: "China University of Geosciences",
    ccf: "China Computer Federation",
    cju: "Yangtze University (not China Jiliang University)",
    zju: "Zhejiang University",
    zhongguokejihexin: "Chinese Science and Technology Core Journals",
    fms: "FMS",
    utd24: "UTD24",
    eii: "Engineering Index (EI)",
    cssci: "CSSCI (Nanjing University Core Journals)",
    sciUpSmall: "CAS Upgraded Subject Quartile",
    cpu: "China Pharmaceutical University",
    xrTop: "Xinrui Academic Top Tier",
    sciif: "SCI Impact Factor (JCR)",
    sci: "SCI Quartile (JCR)",
    ssci: "SSCI Quartile (JCR)",
    jci: "JCI (JCR)",
    sciif5: "SCI Five-Year Impact Factor (JCR)",
    sciwarn: "CAS Warning List",
    sciBase: "CAS Basic Quartile",
    sciUp: "CAS Upgraded Quartile",
    ajg: "ABS Academic Journal Guide",
    ft50: "FT50",
    cscd: "Chinese Science Citation Database (CSCD)",
    ahci: "A&HCI",
    esi: "ESI Subject Classification",
    xr: "Xinrui Academic",
    xrSmall: "Xinrui Academic Subject Quartile",
  }).map(([code, label]) => [code.toLowerCase(), label]));
  const rankDisplayGroups = [
    { title: "Journal Rankings and Core Indexes", codes: ["utd24", "ft50", "ajg", "cssci", "pku", "fms", "zhongguokejihexin", "cscd", "ccf", "eii", "ahci", "esi"] },
    { title: "CAS Quartiles", codes: ["sciUp", "sciUpTop", "sciUpSmall", "sciBase", "sciwarn"] },
    { title: "JCR Metrics and Quartiles", codes: ["sci", "ssci", "sciif", "sciif5", "jci"] },
    { title: "Xinrui Academic", codes: ["xr", "xrTop", "xrSmall", "xrWarn"] },
    { title: "Other Universities and Institutions", codes: [] },
  ];
  const rankDatasetOrder = new Map();
  rankDisplayGroups.forEach((group, category) => group.codes.forEach((code, order) =>
    rankDatasetOrder.set(code.toLowerCase(), [category, order])));
  const rankFallbackCategory = rankDisplayGroups.length - 1;
  const rankRowPresentation = (row) => {
    const abbreviation = String(row.abbreviation || row.key || "").trim().toLowerCase();
    const known = rankDatasetLabels.has(abbreviation);
    const [category, order] = rankDatasetOrder.get(abbreviation) || [rankFallbackCategory, 1000];
    return { row, category, order, label: known ? rankDatasetLabels.get(abbreviation) : row.name };
  };

  const renderRanks = () => {
    const panel = qs("[data-rank-panel]");
    if (!panel || !rankResult) return;
    const selectedOnly = qs("[data-rank-selected]", panel)?.getAttribute?.("aria-pressed") === "true";
    const rows = rankResult.rows
      .filter(row => !selectedOnly || row.selected)
      .map(rankRowPresentation)
      .sort((left, right) => left.category - right.category
        || left.order - right.order
        || left.label.localeCompare(right.label, "zh-CN", { numeric: true }));
    const body = qs("[data-rank-rows]", panel);
    body.replaceChildren();
    let category = -1;
    for (const entry of rows) {
      if (category !== entry.category) {
        category = entry.category;
        const tr = document.createElement("tr"), th = document.createElement("th");
        tr.className = "rank-group";
        th.colSpan = 2;
        th.textContent = rankDisplayGroups[category].title;
        tr.append(th);
        body.append(tr);
      }
      const tr = document.createElement("tr"), th = document.createElement("th"), td = document.createElement("td");
      th.scope = "row";
      th.textContent = entry.label;
      td.textContent = entry.row.rank;
      tr.append(th, td);
      body.append(tr);
    }
    qs("[data-rank-table]", panel).hidden = !rows.length;
    // Show status text only when action or missing data requires attention.
    qs("[data-rank-status]", panel).textContent = rows.length ? ""
      : (selectedOnly ? "No selected datasets available." : "No rankings available.");
    clampRankPanel(panel, parseFloat(panel.style.left), parseFloat(panel.style.top));
  };
  const loadRanks = async (refresh = false) => {
    const panel = qs("[data-rank-panel]");
    if (!panel?.dataset.journalName) return;
    const sequence = ++rankSequence;
    const name = panel.dataset.journalName, key = rankIdentity(panel.dataset.rankUrl, name);
    const cached = rankCache.get(key);
    const current = () => sequence === rankSequence && panel.isConnected;
    rankResult = cached || null;
    panel.removeAttribute("aria-busy");
    qs("[data-rank-refresh]", panel).disabled = false;
    if (cached) {
      renderRanks();
      // Fresh results need no network round-trip. Older results stay visible during revalidation.
      if (!refresh && Date.now() - cached.queried_at * 1000 < rankCacheTtlMs) return;
    } else {
      qs("[data-rank-table]", panel).hidden = true;
      qs("[data-rank-status]", panel).textContent = "Fetching rankings…";
    }
    panel.setAttribute("aria-busy", "true");
    qs("[data-rank-refresh]", panel).disabled = true;
    try {
      let pending = refresh ? null : rankPending.get(key);
      if (!pending) {
        pending = (async () => {
          const url = new URL(panel.dataset.rankUrl, location.origin);
          url.search = new URLSearchParams({name, refresh: String(refresh)});
          const response = await fetch(url, {credentials: "same-origin", cache: "no-store"});
          if (!response.headers.get("content-type")?.includes("application/json")) throw Error("Please sign in again.");
          const data = await response.json();
          if (!response.ok) throw Error(typeof data.detail === "string" ? data.detail : "Unable to look up this journal.");
          if (!validRankResult(data) || rankIdentity(panel.dataset.rankUrl, data.journal_name) !== key)
            throw Error("Invalid journal ranking response.");
          cacheRanks(key, data);
          return data;
        })();
        rankPending.set(key, pending);
      }
      let data;
      try { data = await pending; }
      finally { if (rankPending.get(key) === pending) rankPending.delete(key); }
      if (!current()) return;
      rankResult = rankCache.get(key) || data; renderRanks();
    } catch (error) {
      if (current()) qs("[data-rank-status]", panel).textContent = error.message + (cached ? " Showing saved rankings." : "");
    } finally {
      if (current()) {
        panel.removeAttribute("aria-busy"); qs("[data-rank-refresh]", panel).disabled = false;
      }
    }
  };
  const openRankPanel = () => {
    const menu = qs("[data-journal-context-menu]"), panel = qs("[data-rank-panel]");
    if (!menu || !panel) return;
    panel.dataset.journalName = menu.dataset.journalName;
    panel.dataset.rankUrl = menu.dataset.rankUrl;
    qs("[data-rank-title]", panel).textContent = menu.dataset.journalName;
    qs("[data-rank-table]", panel).hidden = true;
    panel.hidden = false;
    clampRankPanel(panel, rankPosition?.x ?? Number(menu.dataset.anchorX) + 12,
      rankPosition?.y ?? Number(menu.dataset.anchorY) + 12);
    closeJournalMenu();
    qs("[data-rank-close]", panel).focus({preventScroll: true});
    loadRanks();
  };
  document.addEventListener("click", event => {
    if (event.target.closest("[data-rank-close]")) { qs("[data-rank-panel]").hidden = true; rankSequence++; }
    if (event.target.closest("[data-rank-refresh]")) loadRanks(true);
    const selected = event.target.closest("[data-rank-selected]");
    if (selected) {
      const pressed = selected.getAttribute("aria-pressed") !== "true";
      selected.setAttribute("aria-pressed", String(pressed));
      renderRanks();
    }
  });
  document.addEventListener("pointerdown", event => {
    const header = event.target.closest("[data-rank-drag]");
    if (!header || event.target.closest("button") || event.button !== 0) return;
    const panel = header.closest("[data-rank-panel]"), rect = panel.getBoundingClientRect();
    rankDrag = {panel, x: event.clientX - rect.left, y: event.clientY - rect.top};
    header.setPointerCapture(event.pointerId); event.preventDefault();
  });
  document.addEventListener("pointermove", event => {
    if (rankDrag) {
      rankDrag.moved = true;
      clampRankPanel(rankDrag.panel, event.clientX - rankDrag.x, event.clientY - rankDrag.y);
    }
  });
  document.addEventListener("pointerup", rememberRankPosition);
  document.addEventListener("pointercancel", rememberRankPosition);
  window.addEventListener("resize", () => {
    const panel = qs("[data-rank-panel]");
    if (panel && !panel.hidden) clampRankPanel(panel, parseFloat(panel.style.left), parseFloat(panel.style.top));
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      const panel = qs("[data-rank-panel]");
      if (panel) { panel.hidden = true; rankSequence++; }
    }
    const menu = event.target.closest("[data-journal-context-menu]");
    if (menu && ["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      const items = qsa("[role=menuitem]", menu), index = items.indexOf(document.activeElement);
      items[(index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
    }
  });

  const journalLimitValue = (input) => {
    const value = Number(input.value);
    return Number.isInteger(value) && value >= 1 && value <= 200 ? value : null;
  };

  const saveJournalLimit = async (input, revision) => {
    if (!input.isConnected || revision !== input.dataset.saveRevision) return;
    const value = journalLimitValue(input);
    const form = input.closest("[data-journal-limit-form]");
    const status = qs("[data-journal-limit-status]", form);
    if (!form || value === null) {
      input.setAttribute("aria-invalid", "true");
      if (status) status.textContent = "Enter 1–200";
      return;
    }
    input.removeAttribute("aria-invalid");
    if (String(value) === input.dataset.savedValue) {
      if (status) status.textContent = "";
      return;
    }
    if (journalLimitRequest) journalLimitRequest.abort();
    const controller = new AbortController();
    journalLimitRequest = controller;
    if (status) status.textContent = "Saving...";
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (revision !== input.dataset.saveRevision) return;
      input.dataset.savedValue = String(payload.journal_limit);
      if (status) status.textContent = "Saved";
      pageCache.clear();
      // Paging is entirely client-side; saving the limit needs no full-page fetch.
    } catch (error) {
      if (error.name !== "AbortError" && revision === input.dataset.saveRevision) {
        if (status) status.textContent = "Could not save";
      }
    } finally {
      if (journalLimitRequest === controller) journalLimitRequest = null;
    }
  };

  const scheduleJournalLimit = (input) => {
    window.clearTimeout(journalLimitTimer);
    if (journalLimitRequest) journalLimitRequest.abort();
    const revision = String(Number(input.dataset.saveRevision || 0) + 1);
    input.dataset.saveRevision = revision;
    input.closest(".heatmap-panel").dataset.journalPage = "1";
    applyJournalFilters(input.closest(".heatmap-panel"));
    journalLimitTimer = window.setTimeout(() => saveJournalLimit(input, revision), 600);
  };

  const flushJournalLimit = (input) => {
    window.clearTimeout(journalLimitTimer);
    if (journalLimitRequest) journalLimitRequest.abort();
    const revision = String(Number(input.dataset.saveRevision || 0) + 1);
    input.dataset.saveRevision = revision;
    input.closest(".heatmap-panel").dataset.journalPage = "1";
    applyJournalFilters(input.closest(".heatmap-panel"));
    saveJournalLimit(input, revision);
  };

  const initHeatmap = (root = document) => {
    qsa(".heatmap-panel", root).forEach((panel) => {
      const sortSelect = qs("[data-journal-sort]", panel);
      if (sortSelect) {
        sortSelect.value = loadJournalSort();
      }
      applyJournalFilters(panel);
    });
  };

  window.addEventListener("resize", () => {
    qsa(".heatmap-panel").forEach(fitDashboardWidth);
  });
  if (document.fonts?.ready) {
    document.fonts.ready.then(() => qsa(".heatmap-panel").forEach(fitDashboardWidth));
  }

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

  const checkExistingPdfs = async (form, hashes, csrfToken, manualDoi) => {
    const url = form.getAttribute("data-precheck-url");
    const existing = {};
    const existingDoi = {};
    if (!url) return { existing, existingDoi };
    for (let offset = 0; offset < Math.max(1, hashes.length); offset += 500) {
      const response = await fetch(url, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken,
                   "X-Requested-With": "fetch" },
        body: JSON.stringify({ sha256: hashes.slice(offset, offset + 500), doi: manualDoi ? [manualDoi] : [] }),
      });
      if (!response.ok) throw new Error("Server check failed");
      const payload = await response.json();
      Object.assign(existing, payload.existing || {});
      Object.assign(existingDoi, payload.existing_doi || {});
    }
    return { existing, existingDoi };
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

    const stopUpload = (message) => {
      renderUploadRows([{ filename: "Selection", ok: false, error: message }]);
      if (progressText) progressText.textContent = "Upload stopped";
      setFormBusy(form, false, "Upload");
      if (cancelButton) cancelButton.hidden = true;
    };
    const maxFiles = Number(form.dataset.maxFiles);
    const maxBatchBytes = Number(form.dataset.maxBatchBytes);
    if (selectedFiles.length > maxFiles) {
      stopUpload(`Upload at most ${maxFiles} files at once.`); return;
    }
    if (selectedFiles.reduce((total, file) => total + file.size, 0) > maxBatchBytes) {
      stopUpload("Upload batch exceeds the configured size limit."); return;
    }
    if (manualDoi && selectedFiles.length !== 1) {
      stopUpload("Manual DOI can only be used with one file."); return;
    }

    const hashedFiles = [];
    for (const [index, file] of selectedFiles.entries()) {
      if (form.dataset.uploadCancelled === "true") { stopUpload("Cancelled"); return; }
      if (file.size > Number(form.dataset.maxFileBytes)) {
        preRows.push({ filename: file.name, ok: false, error: "PDF exceeds the configured upload limit." });
        continue;
      }
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
      csrfToken, manualDoi
    );
    const newFiles = [];
    hashedFiles.forEach((entry) => {
      const pdf = entry.sha256 ? existing.existing[entry.sha256] : null;
      if (pdf && manualDoi && !Object.values(existing.existingDoi).some((item) => item.id === pdf.id)) {
        preRows.push({ filename: entry.file.name, ok: false, error: `This PDF is already linked to DOI ${pdf.doi}. Ask an administrator to correct a wrong DOI identity.` });
        return;
      }
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

  // Never inject PDF metadata as HTML: document fields are controlled by the PDF author.
  const renderPdfMetaFields = (root, rows) => {
    root.replaceChildren();
    rows.forEach(([name, value]) => {
      if (value === null || value === undefined || value === "") return;
      const row = document.createElement("div");
      const label = document.createElement("dt");
      const content = document.createElement("dd");
      label.textContent = name;
      content.textContent = String(value);
      row.append(label, content);
      root.append(row);
    });
    if (!root.children.length) {
      const empty = document.createElement("p");
      empty.className = "pdf-meta-empty";
      empty.textContent = "No metadata fields found.";
      root.append(empty);
    }
  };

  const openPdfMeta = async (button) => {
    const dialog = qs("[data-pdf-meta-dialog]");
    if (!dialog) return;
    const url = sameOriginUrl(button.dataset.metaUrl);
    const status = qs("[data-pdf-meta-status]", dialog);
    const sections = qs("[data-pdf-meta-sections]", dialog);
    const filename = qs("[data-pdf-meta-filename]", dialog);
    if (!url || url.origin !== window.location.origin || !/\/web\/pdfs\/metadata$/.test(url.pathname)) return;
    filename.textContent = "";
    sections.hidden = true;
    status.textContent = "Loading PDF metadata...";
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch(url.href, { credentials: "same-origin", cache: "no-store" });
      if (!response.ok || !(response.headers.get("content-type") || "").includes("application/json")) {
        let message = response.status === 401 || response.redirected
          ? "Session expired. Sign in and try again." : "Unable to read PDF metadata.";
        if (response.headers.get("content-type")?.includes("application/json")) {
          const payload = await response.json();
          if (typeof payload.detail === "string") message = payload.detail;
        }
        throw new Error(message);
      }
      const metadata = await response.json();
      if (!dialog.open) return;
      filename.textContent = `${metadata.filename} · PDF ID ${metadata.pdf_id}`;
      const vault = metadata.vault;
      renderPdfMetaFields(qs("[data-pdf-meta-vault]", dialog), [
        ["DOI", vault.doi], ["DOI source", vault.doi_source],
        ["Title", vault.title], ["Authors", vault.authors],
        ["Journal", vault.journal], ["Publisher", vault.publisher],
        ["Year", vault.year], ["Metadata status", vault.metadata_status],
        ["Size (bytes)", vault.size], ["SHA-256", vault.sha256],
      ]);
      const embedded = metadata.document;
      const messages = {
        ok: `Embedded DOI: ${embedded.doi}`,
        "no-doi": "No DOI found in PDF Document Info.",
        conflict: `Conflicting embedded DOIs: ${embedded.dois.join(", ")}`,
        encrypted: "PDF is encrypted; Document Info cannot be read.",
        unavailable: "PDF Document Info could not be read.",
      };
      qs("[data-pdf-meta-document-status]", dialog).textContent = messages[embedded.status] || "";
      renderPdfMetaFields(qs("[data-pdf-meta-document]", dialog), [
        ["Pages", embedded.page_count],
        ...Object.entries(embedded.fields || {}).map(([key, value]) => [key.replace(/^\//, ""), value]),
      ]);
      sections.hidden = false;
      status.textContent = "";
    } catch (error) {
      if (dialog.open) status.textContent = error.message || "Unable to read PDF metadata.";
    }
  };

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-pdf-meta]");
    if (trigger) {
      event.preventDefault();
      openPdfMeta(trigger);
      return;
    }
    if (event.target.closest("[data-pdf-meta-close]")) {
      qs("[data-pdf-meta-dialog]")?.close();
    }
  });

  const doiCorrectionDialog = () => qs("[data-doi-dialog]");

  const setDoiCorrectionStatus = (dialog, text, state = "") => {
    const result = qs("[data-doi-result]", dialog);
    if (!result) return;
    result.hidden = !text;
    result.textContent = text || "";
    result.dataset.state = state;
  };

  const requestDoiCorrection = async (url, body) => {
    const response = await fetch(url, {
      method: "POST",
      body,
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    if (!response.ok) {
      let detail = response.statusText || "Request failed";
      try {
        const payload = await response.json();
        detail = payload.detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    return response.json();
  };

  const loadDoiCorrectionHistory = async (dialog) => {
    const target = qs("[data-doi-history]", dialog);
    const url = dialog.dataset.historyUrl;
    if (!target || !url) return;
    target.textContent = "Loading...";
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) throw new Error("Unable to load history");
      const rows = await response.json();
      if (!rows.length) {
        target.textContent = "No previous DOI corrections.";
        return;
      }
      target.textContent = rows
        .map((row) => `${row.old_doi} → ${row.new_doi}\n${row.reason} · ${row.corrected_by}`)
        .join("\n\n");
    } catch (error) {
      target.textContent = error.message || "Unable to load history";
    }
  };

  const openDoiCorrection = (button) => {
    const dialog = doiCorrectionDialog();
    if (!dialog) return;
    dialog.dataset.previewUrl = button.dataset.previewUrl || "";
    dialog.dataset.applyUrl = button.dataset.applyUrl || "";
    dialog.dataset.historyUrl = button.dataset.historyUrl || "";
    dialog.dataset.verified = "false";
    const current = button.dataset.currentDoi || "";
    const sha = button.dataset.sha256 || "";
    const title = button.dataset.pdfTitle || "PDF";
    const currentLabel = qs("[data-doi-current]", dialog);
    const titleLabel = qs("[data-doi-paper-title]", dialog);
    const shaLabel = qs("[data-doi-sha-label]", dialog);
    const shaInput = qs("[data-doi-sha]", dialog);
    const currentInput = qs("[data-doi-current-hidden]", dialog);
    const doiInput = qs("[data-doi-input]", dialog);
    const reasonInput = qs("[data-doi-reason]", dialog);
    const apply = qs("[data-doi-apply]", dialog);
    const override = qs("[data-doi-override]", dialog);
    const overrideRow = qs("[data-doi-override-row]", dialog);
    if (currentLabel) currentLabel.textContent = current;
    if (titleLabel) titleLabel.textContent = title;
    if (shaLabel) shaLabel.textContent = `SHA-256: ${sha}`;
    if (shaInput) shaInput.value = sha;
    if (currentInput) currentInput.value = current;
    if (doiInput) doiInput.value = "";
    if (reasonInput) reasonInput.value = "";
    if (apply) apply.disabled = true;
    if (override) override.checked = false;
    if (overrideRow) overrideRow.hidden = true;
    setDoiCorrectionStatus(dialog, "");
    loadDoiCorrectionHistory(dialog);
    dialog.showModal();
    if (doiInput) doiInput.focus();
  };

  const verifyDoiCorrection = async (dialog) => {
    const form = qs("[data-doi-form]", dialog);
    const doiInput = qs("[data-doi-input]", dialog);
    const reasonInput = qs("[data-doi-reason]", dialog);
    const apply = qs("[data-doi-apply]", dialog);
    const overrideRow = qs("[data-doi-override-row]", dialog);
    const override = qs("[data-doi-override]", dialog);
    if (!form || !doiInput || !reasonInput) return;
    if (!doiInput.value.trim() || !reasonInput.value.trim()) {
      setDoiCorrectionStatus(dialog, "New DOI and reason are required.", "error");
      return;
    }
    if (apply) apply.disabled = true;
    dialog.dataset.verified = "false";
    setDoiCorrectionStatus(dialog, "Verifying DOI and PDF metadata...", "loading");
    const body = new FormData();
    body.set("csrf_token", new FormData(form).get("csrf_token") || "");
    body.set("doi", doiInput.value.trim());
    body.set("reason", reasonInput.value.trim());
    try {
      const result = await requestDoiCorrection(dialog.dataset.previewUrl, body);
      const score = result.title_match_score == null
        ? "unavailable"
        : `${Math.round(Number(result.title_match_score) * 100)}%`;
      setDoiCorrectionStatus(
        dialog,
        [
          `Canonical DOI: ${result.new_doi}`,
          `Metadata: ${result.metadata_provider || "crossref"}`,
          `Title: ${result.title || "-"}`,
          `Journal: ${result.container_title || "-"}`,
          `Year: ${result.published_year || "-"}`,
          `PDF title match: ${score}`,
        ].join("\n"),
        result.requires_title_override ? "warning" : "ok"
      );
      dialog.dataset.verified = "true";
      dialog.dataset.previewSha = result.sha256 || "";
      dialog.dataset.previewCurrentDoi = result.previous_doi || "";
      if (overrideRow) overrideRow.hidden = !result.requires_title_override;
      if (override) override.checked = false;
      if (apply) apply.disabled = Boolean(result.requires_title_override);
    } catch (error) {
      if (overrideRow) overrideRow.hidden = true;
      setDoiCorrectionStatus(dialog, error.message || "DOI verification failed.", "error");
    }
  };

  const applyDoiCorrection = async (dialog) => {
    if (dialog.dataset.verified !== "true") return;
    const form = qs("[data-doi-form]", dialog);
    const doiInput = qs("[data-doi-input]", dialog);
    const reasonInput = qs("[data-doi-reason]", dialog);
    const override = qs("[data-doi-override]", dialog);
    const apply = qs("[data-doi-apply]", dialog);
    if (!form || !doiInput || !reasonInput) return;
    const body = new FormData();
    body.set("csrf_token", new FormData(form).get("csrf_token") || "");
    body.set("doi", doiInput.value.trim());
    body.set("reason", reasonInput.value.trim());
    body.set("expected_sha256", dialog.dataset.previewSha || "");
    body.set("expected_current_doi", dialog.dataset.previewCurrentDoi || "");
    if (override?.checked) body.set("allow_title_mismatch", "true");
    if (apply) apply.disabled = true;
    setDoiCorrectionStatus(dialog, "Applying correction...", "loading");
    try {
      const result = await requestDoiCorrection(dialog.dataset.applyUrl, body);
      setDoiCorrectionStatus(
        dialog,
        `Corrected: ${result.previous_doi} → ${result.new_doi}`,
        "ok"
      );
      pageCache.clear();
      dialog.close();
      fetchPage(window.location.href, {}, false);
    } catch (error) {
      if (apply) apply.disabled = false;
      setDoiCorrectionStatus(dialog, error.message || "DOI correction failed.", "error");
    }
  };

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-doi-correct]");
    if (trigger) {
      event.preventDefault();
      openDoiCorrection(trigger);
      return;
    }
    const close = event.target.closest("[data-doi-close]");
    if (close) {
      doiCorrectionDialog()?.close();
      return;
    }
    const verify = event.target.closest("[data-doi-verify]");
    if (verify) {
      verifyDoiCorrection(verify.closest("[data-doi-dialog]"));
      return;
    }
    const apply = event.target.closest("[data-doi-apply]");
    if (apply) {
      applyDoiCorrection(apply.closest("[data-doi-dialog]"));
    }
  });

  document.addEventListener("change", (event) => {
    const override = event.target.closest("[data-doi-override]");
    if (!override) return;
    const dialog = override.closest("[data-doi-dialog]");
    const apply = qs("[data-doi-apply]", dialog);
    if (apply) apply.disabled = !override.checked;
  });

  document.addEventListener("input", (event) => {
    const target = event.target;
    if (target.matches?.("[data-journal-filter]")) {
      scheduleJournalFilter(target);
    } else if (target.matches?.("[data-journal-list-picker-input]")) {
      renderJournalSuggestions(target);
    } else if (target.matches?.("[data-journal-list-textarea]") && !event.isComposing) {
      const start = target.selectionStart, end = target.selectionEnd;
      const original = target.value;
      const uppercase = original.toLocaleUpperCase("en-US");
      if (original !== uppercase) {
        const newStart = original.slice(0, start).toLocaleUpperCase("en-US").length;
        const newEnd = original.slice(0, end).toLocaleUpperCase("en-US").length;
        target.value = uppercase;
        target.setSelectionRange(newStart, newEnd);
      }
      updateJournalListCount(target.closest("[data-journal-list-dialog]"));
    }
  });

  document.addEventListener("search", (event) => {
    if (event.target.matches?.("[data-journal-filter]")) scheduleJournalFilter(event.target);
  }, true);

  document.addEventListener("compositionend", (event) => {
    if (!event.target.matches?.("[data-journal-list-textarea]")) return;
    const input = event.target;
    input.value = input.value.toLocaleUpperCase("en-US");
    updateJournalListCount(input.closest("[data-journal-list-dialog]"));
  });

  document.addEventListener("focusin", (event) => {
    if (event.target.matches?.("[data-journal-list-picker-input]")) {
      renderJournalSuggestions(event.target);
    }
  });
  document.addEventListener("focusout", (event) => {
    const target = event.target;
    if (target.matches?.("[data-journal-list-textarea]")) {
      normalizeJournalListTextarea(target);
    } else if (target.matches?.("[data-journal-list-picker-input]")) {
      window.setTimeout(() => {
        if (!target.isConnected) return;
        const control = target.closest(".journal-list-picker-control");
        if (!control?.contains(document.activeElement)) closeJournalSuggestions(target);
      }, 0);
    }
  });

  document.addEventListener("keydown", (event) => {
    const input = event.target.closest?.("[data-journal-list-picker-input]");
    if (!input) return;
    const list = qs("[data-journal-list-suggestions]", input.closest("[data-journal-list-dialog]"));
    if (!list) return;
    if (event.key === "Escape" && !list.hidden) {
      event.preventDefault();
      event.stopPropagation();
      closeJournalSuggestions(input);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (list.hidden) renderJournalSuggestions(input);
      const options = qsa("[data-journal-suggestion]", list);
      if (!options.length) return;
      const active = options.findIndex((option) => option.classList.contains("is-active"));
      const next = active === -1
        ? (event.key === "ArrowDown" ? 0 : options.length - 1)
        : (active + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
      options.forEach((option, index) => {
        const isActive = index === next;
        option.classList.toggle("is-active", isActive);
        option.setAttribute("aria-selected", String(isActive));
      });
      input.setAttribute("aria-activedescendant", options[next].id);
      options[next].scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter") {
      event.preventDefault();
      const active = qs(".journal-list-suggestion.is-active", list);
      if (!list.hidden && active) chooseJournalSuggestion(input, active);
      else addJournalListSelection(input);
    }
  });

  document.addEventListener("click", (event) => {
    const suggestion = event.target.closest("[data-journal-suggestion]");
    if (suggestion) {
      const input = qs("[data-journal-list-picker-input]", suggestion.closest("[data-journal-list-dialog]"));
      if (input) chooseJournalSuggestion(input, suggestion);
      return;
    }
    if (!event.target.closest(".journal-list-picker-control")) {
      closeJournalSuggestions(qs("[data-journal-list-picker-input]"));
    }
    const filterMenu = event.target.closest("details.filter-menu");
    const filterSummary = event.target.closest("details.filter-menu > summary");
    if (filterSummary) closeFilterMenus(filterMenu);
    else if (!filterMenu || event.target.closest(".filter-menu-popover a, .filter-menu-popover button")) {
      closeFilterMenus();
    }

    const editTrigger = event.target.closest("[data-journal-list-edit-trigger]");
    if (editTrigger) {
      event.preventDefault();
      openJournalListEditor(editTrigger.closest("[data-journal-list-edit]"));
      return;
    }

    const editorClose = event.target.closest("[data-journal-list-close]");
    if (editorClose) {
      closeJournalListEditor(editorClose.closest("[data-journal-list-dialog]"));
      return;
    }

    const editorReset = event.target.closest("[data-journal-list-reset]");
    if (editorReset) {
      resetJournalListEditor(editorReset);
      return;
    }

    const customListCreate = event.target.closest("[data-custom-list-create]");
    if (customListCreate) {
      createCustomJournalList(customListCreate);
      return;
    }

    const journalListAdd = event.target.closest("[data-journal-list-add]");
    if (journalListAdd) {
      const input = qs("[data-journal-list-picker-input]", journalListAdd.closest("[data-journal-list-dialog]"));
      if (input) addJournalListSelection(input);
      return;
    }

    const sortHeading = event.target.closest("[data-journal-sort-toggle]");
    if (sortHeading) {
      const panel = sortHeading.closest(".heatmap-panel");
      const select = qs("[data-journal-sort]", panel);
      if (!select) return;
      const field = sortHeading.dataset.journalSortToggle;
      select.value = field + "-" + (select.value === field + "-desc" ? "asc" : "desc");
      saveJournalSort(select.value);
      sortJournalRows(select);
      return;
    }

    const pagePrev = event.target.closest("[data-journal-page-prev]");
    const pageNext = event.target.closest("[data-journal-page-next]");
    if (pagePrev || pageNext) {
      const panel = (pagePrev || pageNext).closest(".heatmap-panel");
      const currentPage = Number(panel.dataset.journalPage || 1);
      goToJournalPage(panel, currentPage + (pageNext ? 1 : -1));
      return;
    }

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

    const copyButton = event.target.closest("[data-copy]");
    if (copyButton) {
      event.preventDefault();
      copyText(copyButton);
      return;
    }

    const journal = event.target.closest(".heat-journal[data-journal-name]");
    if (journal) { event.preventDefault(); openJournalMenu(journal, event); return; }
    if (event.target.closest("[data-journal-rank]")) { openRankPanel(); return; }
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (link.target || link.hasAttribute("download") || link.hasAttribute("data-no-pjax")) return;
    const url = sameOriginUrl(link.href);
    if (!url || !isWebPath(url) || isDownloadPath(url)) return;
    event.preventDefault();
    markLinkActive(link);
    fetchPage(url.href);
  });

  document.addEventListener("contextmenu", (event) => {
    const journal = event.target.closest(".heat-journal[data-journal-name]");
    if (journal) {
      event.preventDefault();
      closeFilterMenus();
      openJournalMenu(journal, event);
      return;
    }
    closeJournalMenu();
    const tab = event.target.closest("[data-journal-list-edit]");
    if (!tab) return;
    event.preventDefault();
    closeFilterMenus();
    openJournalListEditor(tab);
  });

  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest("[data-journal-context-menu]")) closeJournalMenu();
  });
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-journal-wos-link]")) closeJournalMenu();
  });
  document.addEventListener("scroll", () => closeJournalMenu(), true);
  window.addEventListener("resize", () => closeJournalMenu());
  document.addEventListener("compositionstart", (event) => {
    if (event.target.matches("[data-journal-filter]")) {
      event.target.dataset.composing = "true";
      window.clearTimeout(journalFilterTimer);
    }
  });
  document.addEventListener("compositionend", (event) => {
    if (event.target.matches("[data-journal-filter]")) {
      delete event.target.dataset.composing;
      scheduleJournalFilter(event.target);
    }
  });

  // Keep filter popovers inside the viewport even when tabs wrap on small screens.
  const alignFilterPopover = (menu) => {
    const popup = qs(".filter-menu-popover", menu);
    if (!popup) return;
    popup.style.transform = "none";
    const rect = popup.getBoundingClientRect();
    const margin = 12;
    let shift = Math.min(0, window.innerWidth - margin - rect.right);
    if (rect.left + shift < margin) shift += margin - (rect.left + shift);
    popup.style.transform = "translateX(" + shift + "px)";
  };
  document.addEventListener("toggle", (event) => {
    const menu = event.target;
    if (menu.matches?.("details.filter-menu") && menu.open) {
      closeFilterMenus(menu);
      window.requestAnimationFrame(() => {
        if (menu.open) alignFilterPopover(menu);
      });
    }
  }, true);
  window.addEventListener("resize", () => {
    qsa("details.filter-menu[open]").forEach(alignFilterPopover);
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest(".topbar-right")) return;
    const toggle = qs("[data-utility-toggle]");
    const nav = qs(".utility-nav");
    if (toggle) toggle.setAttribute("aria-expanded", "false");
    if (nav) nav.classList.remove("is-open");
  });

  window.addEventListener("popstate", () => {
    fetchPage(window.location.href, {}, false);
  });

  let dashboardRefreshPending = false;
  let dashboardLastFullRefresh = Date.now();
  const refreshDashboardCounts = async () => {
    const panel = qs(".heatmap-panel");
    if (!panel || dashboardRefreshPending || document.hidden || qs("dialog[open]")) return;
    dashboardRefreshPending = true;
    const url = window.location.href;
    try {
      // Most checks transfer only a tiny revision token instead of the whole dashboard.
      const revisionResponse = await fetch(panel.dataset.dashboardRevisionUrl, {
        credentials: "same-origin", cache: "no-store",
      });
      if (!revisionResponse.ok || !revisionResponse.headers.get("content-type")?.includes("application/json")) {
        throw new Error("Revision check unavailable");
      }
      const { revision } = await revisionResponse.json();
      const fallbackDue = Date.now() - dashboardLastFullRefresh >= 10 * 60 * 1000;
      if (!fallbackDue && revision === panel.dataset.dashboardRevision) return;
      const currentMap = qs("#journal-year-heatmap", panel);
      // A previously empty vault can gain journals; fetch the page normally then.
      if (!currentMap) {
        if (url !== window.location.href) return;
        pageCache.delete(cacheKey(url));
        await fetchPage(url, {}, false);
        dashboardLastFullRefresh = Date.now();
        return;
      }
      // Keep existing controls and the current journal page while refreshing.
      if (currentMap.contains(document.activeElement)) return;
      const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
      if (!response.ok || response.url !== url) throw new Error("Refresh unavailable");
      const next = new DOMParser().parseFromString(await response.text(), htmlType);
      if (url !== window.location.href || !panel.isConnected) return;
      const updatedPanel = qs(".heatmap-panel", next);
      const updatedMap = qs("#journal-year-heatmap", updatedPanel);
      const updatedData = qs("[data-journal-heatmap-data]", updatedPanel);
      const currentData = qs("[data-journal-heatmap-data]", panel);
      if (!updatedMap || !updatedData || !currentData) return;
      const scroll = currentMap.closest("[data-heatmap-years-viewport]");
      const left = scroll.scrollLeft;
      const top = scroll.scrollTop;
      currentData.replaceWith(updatedData);
      currentMap.replaceWith(updatedMap);
      for (const selector of [".summary-tags", ".heatmap-filter-pdf-count"]) {
        const current = qs(selector);
        const replacement = qs(selector, next);
        if (current && replacement) current.replaceWith(replacement);
      }
      panel.dataset.dashboardRevision = updatedPanel.dataset.dashboardRevision;
      applyJournalFilters(panel);
      scroll.scrollLeft = left;
      scroll.scrollTop = top;
      dashboardLastFullRefresh = Date.now();
      pageCache.delete(cacheKey(url));
    } catch {
      // Keep the current dashboard usable if the connection is temporarily unavailable.
    } finally {
      dashboardRefreshPending = false;
    }
  };
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshDashboardCounts();
  });
  window.setInterval(refreshDashboardCounts, 30000);

  rememberPage(window.location.href, snapshotPage());
  initHeatmap();
})();
