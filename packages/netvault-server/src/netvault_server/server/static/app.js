(function () {
  "use strict";

  const mainSelector = "#app-main";
  const htmlType = "text/html";
  const pageCache = new Map();
  const prefetching = new Set();
  const journalRowsCache = new WeakMap();
  const journalSortStorageKey = "netvault:heatmap-sort:v2";
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
      // The left-hand journal collections are ordinary expandable groups,
      // not transient floating menus. Do not collapse them on outside clicks.
      if (menu.closest("[data-journal-library]")) return;
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
  const isDashboardRoot = (url) => url && /\/web\/?$/.test(url.pathname) && !url.searchParams.has("filter");
  let dashboardPreferenceQueue = Promise.resolve();
  let lastDashboardPreference = null;

  const rememberDashboardFilter = (value) => {
    const url = sameOriginUrl(value);
    if (!url || !/\/web\/?$/.test(url.pathname) || !url.searchParams.has("filter")) return;
    const filter = url.searchParams.get("filter");
    const panel = qs(".heatmap-panel");
    const token = qs("[data-journal-list-form] input[name='csrf_token']")?.value;
    if (!token || !panel || panel.dataset.dashboardFilter !== filter || filter === lastDashboardPreference) return;
    lastDashboardPreference = filter;
    dashboardPreferenceQueue = dashboardPreferenceQueue.catch(() => {}).then(async () => {
      const response = await fetch(panel.dataset.dashboardPreferenceUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
        body: new URLSearchParams({ filter, csrf_token: token }),
      });
      if (!response.ok) throw new Error("Could not remember the selected journal list.");
      const status = qs("[data-dashboard-preference-status]");
      if (status) status.textContent = "";
    }).catch(() => {
      lastDashboardPreference = null; // Allow a later navigation to retry.
      const status = qs("[data-dashboard-preference-status]");
      if (status) status.textContent = "Could not save the selected journal list. Please try again.";
    });
  };

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
    window.NetVaultI18n?.translate(currentMain);
    rememberDashboardFilter(url);
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
    const requested = sameOriginUrl(url);
    if (isDashboardRoot(requested)) await dashboardPreferenceQueue;
    // /web without an explicit filter depends on the per-user preference.
    // Caching or prefetching it can resurrect an obsolete UTD24 page.
    const useCache = shouldCachePage(options) && !isDashboardRoot(requested);
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
    if (!url || !isWebPath(url) || isDownloadPath(url) || isDashboardRoot(url)) return;
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
  let doiBatchRevision = 0;

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
    const check = qs("[data-doi-batch-check]", dialog);
    if (count) count.textContent = parsed.dois.length.toLocaleString("en-US") + " DOIs";
    if (button) button.disabled = doiBatchSubmitting || parsed.dois.length === 0 || parsed.dois.length > doiBatchMax;
    if (check) check.disabled = doiBatchSubmitting || parsed.dois.length === 0 || parsed.dois.length > doiBatchMax;
    return parsed;
  };
  const resetDoiBatchAvailability = (dialog) => {
    doiBatchRevision++;
    const result = qs("[data-doi-batch-availability]", dialog);
    const download = qs("[data-doi-batch-download]", dialog);
    const ids = qs("[data-doi-batch-download-ids]");
    if (result) result.hidden = true;
    if (download) { download.hidden = true; download.disabled = true; }
    if (ids) ids.replaceChildren();
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

  const checkDoiBatchAvailability = async () => {
    const dialog = doiBatchDialog();
    if (!dialog || doiBatchSubmitting) return;
    restoreDoiBatch(dialog);
    const parsed = readDoiBatch(dialog);
    if (!parsed.extract && parsed.invalid) {
      doiBatchStatus(dialog, parsed.invalid + " invalid DOI line(s). Check the entries or enable regex.");
      return;
    }
    if (!parsed.dois.length || parsed.dois.length > doiBatchMax) {
      doiBatchStatus(dialog, "Enter between 1 and " + doiBatchMax + " DOIs.");
      return;
    }
    resetDoiBatchAvailability(dialog);
    const revision = doiBatchRevision;
    persistDoiBatch(dialog);
    doiBatchSubmitting = true;
    updateDoiBatchCount(dialog);
    doiBatchStatus(dialog, "Checking server availability…");
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
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Availability check failed.");
      if (revision !== doiBatchRevision || !dialog.isConnected) return;
      const box = qs("[data-doi-batch-availability]", dialog);
      qs("[data-doi-batch-available]", dialog).textContent = Number(result.available_count).toLocaleString("en-US");
      qs("[data-doi-batch-requested]", dialog).textContent = Number(result.count).toLocaleString("en-US");
      const missing = result.count - result.available_count;
      const detail = qs("[data-doi-batch-detail]", dialog);
      detail.textContent = (missing ? missing + " unavailable (including " + result.storage_missing_count + " missing stored files). " : "All DOIs matched. ")
        + (result.zip_error || "Ready to download as ZIP.");
      box.hidden = false;
      const ids = qs("[data-doi-batch-download-ids]");
      const download = qs("[data-doi-batch-download]", dialog);
      if (result.zip_ready && Array.isArray(result.pdf_ids)) {
        const fragment = document.createDocumentFragment();
        result.pdf_ids.forEach((id) => {
          const input = document.createElement("input");
          input.type = "hidden";
          input.name = "pdf_ids";
          input.value = String(id);
          fragment.append(input);
        });
        ids.replaceChildren(fragment);
        download.disabled = false;
        download.hidden = false;
      }
      doiBatchStatus(dialog);
    } catch (error) {
      if (revision === doiBatchRevision) doiBatchStatus(dialog, error.message || "Could not check the DOI list.");
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
    if (event.target.closest("[data-doi-batch-check]")) {
      checkDoiBatchAvailability();
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
    if (dialog) { resetDoiBatchAvailability(dialog); formatDoiBatch(dialog); }
  });
  document.addEventListener("input", (event) => {
    if (!event.target.matches?.("[data-doi-batch-input]")) return;
    const dialog = event.target.closest("[data-doi-batch-dialog]");
    resetDoiBatchAvailability(dialog);
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

  const loadJournalListPayload = (endpoint, fresh = false) => {
    const entry = journalListPayloadCache.get(endpoint);
    if (!fresh && entry && Date.now() - entry.storedAt < journalListCacheTtlMs)
      return Promise.resolve(entry.payload);
    if (!fresh && journalListPayloadPending.has(endpoint))
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
    const metaField = qs("[data-custom-list-meta-field]", dialog);
    const categoryInput = qs("[data-custom-list-category]", dialog);
    const kindInput = qs("[data-custom-list-kind]", dialog);
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
    dialog.dataset.revision = payload.updated_at || "";
    if (source) {
      source.hidden = !payload.source_url;
      source.href = payload.source_url || "#";
      source.textContent = payload.source ? `Default: ${payload.source}` : "Default source";
    }
    if (nameField) nameField.hidden = !payload.custom;
    if (nameInput) nameInput.value = payload.custom ? (payload.label || "Custom list") : "";
    if (metaField) metaField.hidden = !payload.custom;
    if (categoryInput) categoryInput.value = payload.category || "";
    if (kindInput) kindInput.value = payload.kind || "";
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
    const saveButton = qs("[data-journal-list-form] button[type='submit']", dialog);
    const cached = journalListPayloadCache.get(endpoint);
    const hasCached = cached && Date.now() - cached.storedAt < journalListCacheTtlMs;
    dialog.dataset.endpoint = endpoint;
    if (hasCached) {
      populateJournalListEditor(dialog, cached.payload);
      if (saveButton) saveButton.disabled = false;
    } else {
      const title = qs("[data-journal-list-title]", dialog);
      const textarea = qs("[data-journal-list-textarea]", dialog);
      const feedback = qs("[data-journal-list-feedback]", dialog);
      if (title) title.textContent = "Loading list…";
      if (textarea) { textarea.value = ""; textarea.disabled = true; }
      if (feedback) feedback.textContent = "";
      if (saveButton) saveButton.disabled = true;
    }
    // Show immediately; previously this awaited a forced GET on every click.
    if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
    else if (!dialog.open) dialog.setAttribute("open", "");
    if (hasCached) qs("[data-journal-list-textarea]", dialog)?.focus({ preventScroll: true });
    try {
      if (hasCached) return;
      const payload = await loadJournalListPayload(endpoint);
      if (sequence !== journalListOpenSequence || !dialog.isConnected || !tab.isConnected)
        return;
      populateJournalListEditor(dialog, payload);
      if (saveButton) saveButton.disabled = false;
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
      if (saveButton) saveButton.disabled = true;
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
    const categoryInput = qs("[data-custom-list-category]", form);
    const kindInput = qs("[data-custom-list-kind]", form);
    if (!dialog || !textarea) return;
    if (!endpoint) {
      if (feedback) feedback.textContent = "Unable to save: the journal list was not loaded.";
      return;
    }
    normalizeJournalListTextarea(textarea);
    const requestedNames = journalListNames(textarea);
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
          journals: requestedNames,
          name: nameInput && !nameInput.closest("[hidden]") ? nameInput.value.trim() : undefined,
          category: categoryInput && !categoryInput.closest("[hidden]") ? categoryInput.value.trim() : undefined,
          kind: kindInput && !kindInput.closest("[hidden]") ? kindInput.value.trim() : undefined,
          expected_updated_at: dialog.dataset.revision || null,
        }),
      });
      if (!response.ok) throw new Error(await journalListError(response));
      const payload = await response.json();
      const savedNames = Array.isArray(payload.journals) ? payload.journals : [];
      const received = new Set(savedNames.map((name) => normalizedJournalText(name)));
      dialog.dataset.revision = payload.updated_at || "";
      journalListPayloadCache.delete(endpoint);
      if (savedNames.length !== requestedNames.length
          || requestedNames.some((name) => !received.has(normalizedJournalText(name)))) {
        if (feedback) feedback.textContent =
          "The server retained " + savedNames.length + " of " + requestedNames.length
          + " journal names. Review your list or export a backup before closing.";
        return;
      }
      pageCache.clear();
      closeJournalListEditor(dialog);
      if (payload.key?.startsWith("custom_")) placeCustomJournalEntry(payload);
      const activeFilter = qs(".heatmap-panel")?.dataset.dashboardFilter;
      showJournalLibraryNotice("Saved " + savedNames.length
        + " journal" + (savedNames.length === 1 ? "" : "s") + " to "
        + (payload.label || "the journal list") + ".");
      if (activeFilter === payload.key) {
        // Refresh the active chart after letting the success state paint.
        // Saving an inactive list needs no full dashboard request at all.
        const current = sameOriginUrl(window.location.href);
        window.setTimeout(() => {
          void fetchPage(current?.href || window.location.href, {}, false);
        }, 0);
      }
    } catch (error) {
      if (feedback) feedback.textContent = error.message || "Could not save the journal list.";
    } finally {
      if (form.isConnected) setFormBusy(form, false, "Save list");
    }
  };

  // The editor is replaced on PJAX navigation. Delegating form submission
  // guarantees that Save list (and Enter from a field) performs the JSON POST,
  // not the browser's default GET of the dashboard.
  document.addEventListener("submit", (event) => {
    const createForm = event.target.closest?.("[data-journal-create-form]");
    if (createForm) {
      event.preventDefault();
      if (!qs("[data-journal-create-submit]", createForm)?.disabled)
        void createCustomJournalList(createForm);
      return;
    }
    const form = event.target.closest?.("[data-journal-list-form]");
    if (!form) return;
    event.preventDefault();
    if (form.hasAttribute("aria-busy")) return;
    void saveJournalListEditor(form);
  });

  const exportJournalList = (button) => {
    const dialog = button.closest("[data-journal-list-dialog]");
    const textarea = qs("[data-journal-list-textarea]", dialog);
    if (!textarea) return;
    const names = journalListNames(textarea);
    const title = qs("[data-custom-list-name]", dialog)?.value
      || qs("[data-journal-list-title]", dialog)?.textContent || "journal-list";
    const safeName = title.trim().replace(/[^a-z0-9_-]+/gi, "-").slice(0, 60) || "journal-list";
    const lineBreak = String.fromCharCode(10);
    const blob = new Blob([names.join(lineBreak) + (names.length ? lineBreak : "")], {type: "text/plain;charset=utf-8"});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "netvault-" + safeName + ".txt";
    link.style.display = "none";
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const showJournalLibraryNotice = (message) => {
    const notice = qs("[data-journal-library-notice]");
    if (!notice) return;
    notice.textContent = message;
    notice.hidden = !message;
    window.clearTimeout(showJournalLibraryNotice.timer);
    if (message) showJournalLibraryNotice.timer = window.setTimeout(() => {
      if (notice.isConnected) notice.hidden = true;
    }, 4000);
  };

  const placeCustomJournalEntry = (payload) => {
    const library = qs("[data-journal-library]");
    const menu = qs(".custom-list-menu", library);
    if (!menu || !payload.key || !payload.key.startsWith("custom_")) return;
    const category = payload.category || "";
    let group = qsa("[data-journal-custom-category]", menu)
      .find((node) => node.dataset.journalCustomCategory === category);
    if (!group) {
      group = document.createElement("div");
      group.className = "journal-custom-category-group";
      group.dataset.journalCustomCategory = category;
      if (category) {
        const title = document.createElement("div");
        title.className = "journal-custom-category-label";
        title.textContent = category;
        group.append(title);
      }
      if (category) menu.append(group);
      else menu.prepend(group);
    }
    let row = qsa(".journal-custom-entry", menu)
      .find((node) => node.dataset.listKey === payload.key);
    const oldGroup = row?.closest("[data-journal-custom-category]");
    if (!row) {
      row = document.createElement("div");
      row.className = "journal-custom-entry";
      row.dataset.listKey = payload.key;
      const link = document.createElement("a");
      link.dataset.journalListEdit = "";
      const currentUrl = new URL(qs(".filter-tabs > a", library)?.href || window.location.href);
      currentUrl.searchParams.set("filter", payload.key);
      link.href = currentUrl.href;
      const label = document.createElement("span");
      link.append(label);
      const edit = document.createElement("span");
      edit.className = "filter-edit-mark";
      edit.dataset.journalListEditTrigger = "";
      edit.setAttribute("aria-label", "Edit " + payload.label + " journal list");
      edit.innerHTML = '<i class="fa-solid fa-ellipsis" aria-hidden="true"></i>';
      link.append(edit);
      row.append(link);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "journal-custom-delete";
      remove.dataset.journalCustomDelete = "";
      remove.innerHTML = '<i class="fa-solid fa-trash-can" aria-hidden="true"></i>';
      row.append(remove);
    }
    const link = qs("a", row);
    const endpoint = (qs("[data-custom-list-create]")?.dataset.endpoint || "")
      + "/" + encodeURIComponent(payload.key);
    link.dataset.journalListUrl = endpoint;
    link.title = (payload.kind ? payload.kind + " · " : "")
      + "Click to filter; right-click to edit " + payload.label;
    link.dataset.listKind = payload.kind || "";
    qs("span", link).textContent = payload.label || "Custom list";
    const deleteButton = qs("[data-journal-custom-delete]", row);
    if (deleteButton) {
      deleteButton.dataset.endpoint = endpoint;
      deleteButton.dataset.listKey = payload.key;
      deleteButton.dataset.listName = payload.label || "";
      deleteButton.setAttribute("aria-label", "Delete " + payload.label + " journal list");
      deleteButton.title = "Delete list";
    }
    group.append(row);
    if (oldGroup && oldGroup !== group && !qs(".journal-custom-entry", oldGroup))
      oldGroup.remove();
    const customSection = qs('[data-journal-list-group="custom"]', library);
    if (customSection) customSection.open = true;
    return row;
  };

  const openCreateJournalDialog = (button) => {
    const dialog = qs("[data-journal-create-dialog]");
    if (!dialog || !button.dataset.endpoint) return;
    const form = qs("[data-journal-create-form]", dialog);
    form.reset();
    form.dataset.endpoint = button.dataset.endpoint;
    qs("[data-journal-create-feedback]", dialog).textContent = "";
    qs("[data-journal-create-submit]", dialog).disabled = false;
    // Opening is synchronous: no prompt(), GET, chart calculation or request.
    if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
    else if (!dialog.open) dialog.setAttribute("open", "");
    qs("[data-create-list-name]", dialog)?.focus({ preventScroll: true });
  };

  const createCustomJournalList = async (form) => {
    const dialog = form.closest("[data-journal-create-dialog]");
    const csrfToken = qs("input[name='csrf_token']", qs("[data-journal-list-form]"))?.value || "";
    const feedback = qs("[data-journal-create-feedback]", dialog);
    const submit = qs("[data-journal-create-submit]", dialog);
    const endpoint = form.dataset.endpoint || "";
    if (!endpoint || !csrfToken || !form.reportValidity()) return;
    const fields = new FormData(form);
    const journals = String(fields.get("journals") || "")
      .split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (journals.length > 2000) {
      feedback.textContent = "Use at most 2,000 journal names.";
      return;
    }
    submit.disabled = true;
    submit.textContent = "Creating…";
    feedback.textContent = "Saving to your account…";
    try {
      const response = await fetch(endpoint, {
        method: "POST", credentials: "same-origin",
        headers: {
          "Content-Type": "application/json", "X-CSRF-Token": csrfToken,
          "X-Requested-With": "fetch",
        },
        body: JSON.stringify({
          name: String(fields.get("name") || "").trim(),
          category: String(fields.get("category") || "").trim(),
          kind: String(fields.get("kind") || "").trim(),
          journals,
        }),
      });
      if (!response.ok) throw new Error(await journalListError(response));
      const payload = await response.json();
      pageCache.clear();
      const row = placeCustomJournalEntry(payload);
      const editUrl = qs("a[data-journal-list-url]", row)?.dataset.journalListUrl;
      if (editUrl) journalListPayloadCache.set(editUrl, {payload, storedAt: Date.now()});
      dialog.close();
      showJournalLibraryNotice((payload.label || "List") + " created. Select it to view.");
      // No full dashboard fetch on creation: the user can keep comparing
      // their current chart, and choose the new list whenever they want.
      // Refreshing a 130k-document chart here used to block the interaction.
    } catch (error) {
      feedback.textContent = error.message || "Could not create the journal list.";
    } finally {
      if (submit.isConnected) {
        submit.disabled = false;
        submit.textContent = "Create list";
      }
    }
  };

  const deleteCustomJournalList = async (button) => {
    const endpoint = button.dataset.endpoint || "";
    const key = button.dataset.listKey || "";
    const name = button.dataset.listName || "this list";
    const token = qs("[data-journal-list-form] input[name='csrf_token']")?.value || "";
    // Only user-owned custom lists are deletable. Built-in filters must not
    // expose this control, and the server also enforces that restriction.
    if (!endpoint || !key.startsWith("custom_") || !token
        || !window.confirm('Delete custom journal list "' + name + '"? This cannot be undone.')) return;
    button.disabled = true;
    try {
      const response = await fetch(endpoint, {
        method: "DELETE", credentials: "same-origin",
        headers: {"X-CSRF-Token": token, "X-Requested-With": "fetch"},
      });
      if (!response.ok) throw new Error(await journalListError(response));
      const payload = await response.json();
      if (!payload.deleted) throw new Error("The journal list was not deleted.");
      journalListPayloadCache.delete(endpoint);
      pageCache.clear();
      const current = sameOriginUrl(window.location.href);
      if (current && current.searchParams.get("filter") === key)
        current.searchParams.set("filter", "all");
      await fetchPage(current ? current.href : window.location.href, {}, false);
    } catch (error) {
      window.alert(error.message || "Could not delete the journal list.");
      if (button.isConnected) button.disabled = false;
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
      if (payload.deleted && current) current.searchParams.set("filter", "all");
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
    // Do not set a title attribute: the browser's native hover tooltip
    // obscures neighbouring journal rows and duplicates the visible name.
    if (row.total === 0) label.classList.add("journal-no-pdfs");
    label.tabIndex = 0;
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
    total.dataset.journalName = row.label;
    total.textContent = row.total.toLocaleString("en-US");
    total.title = row.label + ": " + total.textContent + " PDFs";
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
    const panel = select.closest(".heatmap-panel");
    applyJournalFilters(panel);
    saveJournalView(panel);
  };

  const journalViewStorageKey = (panel) => {
    const userId = document.body?.dataset.userId || "";
    const filter = panel?.dataset.dashboardFilter || "";
    return userId && filter ? "netvault:dashboard-view:v1:" + userId + ":" + filter : null;
  };
  const loadJournalView = (panel) => {
    const key = journalViewStorageKey(panel);
    if (!key) return {};
    try {
      const saved = JSON.parse(localStorage.getItem(key) || "{}");
      return saved && typeof saved === "object" ? saved : {};
    } catch {
      return {};
    }
  };
  const saveJournalView = (panel) => {
    const key = journalViewStorageKey(panel);
    if (!key || !panel?.isConnected) return;
    const search = qs("[data-journal-filter]", panel)?.value || "";
    const page = Number(panel.dataset.journalPage || 1);
    const scroll = qs("[data-heatmap-years-viewport]", panel);
    const previous = loadJournalView(panel);
    try {
      localStorage.setItem(key, JSON.stringify({
        search: search.slice(0, 300),
        page: Number.isInteger(page) && page > 0 ? Math.min(page, 10000) : 1,
        yearScrollLeft: scroll?.dataset.userYearScroll === "true"
          ? Math.max(0, Math.round(scroll.scrollLeft))
          : previous.yearScrollLeft,
      }));
    } catch { /* Browsers without storage can still use the current page. */ }
  };

  const loadJournalSort = () => {
    try {
      const key = journalSortStorageKey + ":" + (document.body?.dataset.userId || "");
      const value = window.localStorage.getItem(key)
        || window.localStorage.getItem("netvault:heatmap-sort:v1") || "";
      return journalSortValues.has(value) ? value : "total-desc";
    } catch {
      return "total-desc";
    }
  };

  const saveJournalSort = (value) => {
    if (!journalSortValues.has(value)) return;
    try {
      const key = journalSortStorageKey + ":" + (document.body?.dataset.userId || "");
      window.localStorage.setItem(key, value);
    } catch {
      // Sorting still works for this page when storage is unavailable.
    }
  };

  // Preserve a centered unit: fixed-width years | Total | Journal.
  // When space is scarce only the year viewport scrolls; the Total gap
  // stays fixed and the journal name remains visible beside it.
  const fitDashboardWidth = (panel) => {
    const frame = qs(".heatmap-scroll", panel);
    const yearGrid = qs(".journal-heatmap", panel);
    const fixed = qs("[data-heatmap-fixed-body]", panel);
    if (!panel || !frame || !yearGrid || !fixed) return;
    frame.style.removeProperty("--heat-journal-width");
    const computed = getComputedStyle(frame);
    const totalWidth = parseFloat(computed.getPropertyValue("--heat-total-width")) || 60;
    // Custom property values such as clamp(190px, 20vw, 320px) are returned
    // as an unresolved string: parseFloat(clamp(...)) would produce NaN and
    // incorrectly truncate the years by ~100px even on a wide monitor.
    const journalWidth = qs(".heatmap-fixed-heading .heat-corner", panel)
      ?.getBoundingClientRect().width || 220;
    const scrollbarRail = parseFloat(computed.getPropertyValue("--heat-scrollbar-rail")) || 16;
    const naturalWidth = yearGrid.scrollWidth + totalWidth + journalWidth + scrollbarRail;
    panel.style.setProperty("--dashboard-table-width", Math.ceil(naturalWidth) + "px");
    const workspace = panel.closest("[data-rank-workspace]");
    if (workspace) {
      // Ranking details are always exactly 380 CSS px. The viewport may
      // scroll horizontally if even the minimum center pane cannot fit.
      workspace.style.removeProperty("--rank-current-width");
      workspace.dataset.rankLayout = "docked";
    }
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
    qsa("[data-journal-sort-toggle]", panel.closest(".page")).forEach((button) => {
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
      .forEach((node, index) => {
        if (index < years.length) yearNodes.append(node);
        else if (index === years.length) fixedNodes.append(node);
        else fixedNodes.append(node);
      }));
    heatmap.replaceChildren(yearNodes);
    fixedBody.replaceChildren(fixedNodes);
    // When search, sort, or pagination rebuilds the fixed journal column,
    // keep the highlight on the journal currently shown in the rank panel.
    highlightRankJournal(qs("[data-rank-panel]")?.dataset.journalName);
    fitDashboardWidth(panel);
    // Preload the rankings for the CURRENT visible page, not just the first
    // dashboard page. Subsequent mouse hovers then reuse local memory.
    sharedRankSeed = primeSharedRankCache(panel);

    const status = qs("[data-journal-filter-status], .heatmap-filter-status", panel);
    if (status) {
      const noun = rows.length === 1 ? "journal" : "journals";
      status.textContent = query ? matches.length + "/" + rows.length : String(rows.length);
      status.title = query ? matches.length + " of " + rows.length + " " + noun : rows.length + " " + noun;
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
      const horizontal = qs("[data-heatmap-scrollbar-x]", frame);
      const vertical = qs("[data-heatmap-scrollbar-y]", frame);
      const horizontalSpacer = qs("[data-heatmap-scrollbar-x-spacer]", frame);
      const verticalSpacer = qs("[data-heatmap-scrollbar-y-spacer]", frame);
      const syncFixedRows = () => {
        const fixed = qs("[data-heatmap-fixed-body]", panel);
        if (fixed) fixed.style.transform = "translate3d(0, " + (-scroll.scrollTop) + "px, 0)";
      };
      const syncScrollbarPositions = () => {
        if (horizontal && horizontal.scrollLeft !== scroll.scrollLeft) horizontal.scrollLeft = scroll.scrollLeft;
        if (vertical && vertical.scrollTop !== scroll.scrollTop) vertical.scrollTop = scroll.scrollTop;
      };
      const resizeScrollbarRails = () => {
        // Match scroll ranges exactly. The rails are separate from the grid,
        // so macOS overlay scrollbars never cover year squares.
        if (horizontalSpacer) horizontalSpacer.style.width = scroll.scrollWidth + "px";
        if (vertical && verticalSpacer) {
          verticalSpacer.style.height = Math.max(1, vertical.clientHeight
            + scroll.scrollHeight - scroll.clientHeight) + "px";
        }
        syncScrollbarPositions();
      };
      syncFixedRows();
      resizeScrollbarRails();
      if (!scroll.dataset.yearScrollReady) {
        scroll.dataset.yearScrollReady = "true";
        // Oldest years at left, newest at right. Only year cells scroll horizontally.
        const alignLatest = () => {
          if (!scroll.isConnected || !scroll.clientWidth) return;
          scroll.scrollLeft = Math.max(0, scroll.scrollWidth - scroll.clientWidth);
        };
        requestAnimationFrame(() => {
          resizeScrollbarRails();
          const savedLeft = Number(panel.dataset.savedYearScroll);
          if (panel.dataset.savedYearScroll !== undefined && Number.isFinite(savedLeft) && savedLeft >= 0) {
            scroll.dataset.userYearScroll = "true";
            scroll.scrollLeft = Math.min(savedLeft, Math.max(0, scroll.scrollWidth - scroll.clientWidth));
          } else {
            alignLatest();
          }
          syncScrollbarPositions();
        });
        const observer = new ResizeObserver(() => {
          resizeScrollbarRails();
          if (scroll.dataset.userYearScroll !== "true") alignLatest();
          syncFixedRows();
          syncScrollbarPositions();
        });
        observer.observe(scroll);
        let scrollSaveTimer = null;
        scroll.addEventListener("scroll", () => {
          syncFixedRows();
          syncScrollbarPositions();
          if (scroll.dataset.userYearScroll === "true") {
            window.clearTimeout(scrollSaveTimer);
            scrollSaveTimer = window.setTimeout(() => saveJournalView(panel), 200);
          }
        }, { passive: true });
        if (horizontal) horizontal.addEventListener("scroll", () => {
          if (Math.abs(horizontal.scrollLeft - scroll.scrollLeft) < 1) return;
          scroll.dataset.userYearScroll = "true";
          scroll.scrollLeft = horizontal.scrollLeft;
        }, { passive: true });
        if (vertical) vertical.addEventListener("scroll", () => {
          if (Math.abs(vertical.scrollTop - scroll.scrollTop) < 1) return;
          scroll.scrollTop = vertical.scrollTop;
        }, { passive: true });
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
    saveJournalView(panel);
    const scroll = qs("[data-heatmap-years-viewport]", panel);
    if (scroll) scroll.scrollTop = 0;
  };

  const scheduleJournalFilter = (input) => {
    input.closest(".heatmap-panel").dataset.journalPage = "1";
    saveJournalView(input.closest(".heatmap-panel"));
    window.clearTimeout(journalFilterTimer);
    if (input.dataset.composing === "true") return;
    journalFilterTimer = window.setTimeout(() => applyJournalFilters(input.closest(".heatmap-panel")), 200);
  };

  const flushJournalFilter = (input) => {
    input.closest(".heatmap-panel").dataset.journalPage = "1";
    window.clearTimeout(journalFilterTimer);
    if (input.dataset.composing === "true") return;
    applyJournalFilters(input.closest(".heatmap-panel"));
    saveJournalView(input.closest(".heatmap-panel"));
  };

  const closeJournalMenu = (restoreFocus = false) => {
    const menu = qs("[data-journal-context-menu]");
    if (menu) menu.hidden = true;
    if (restoreFocus && journalMenuTarget?.isConnected) journalMenuTarget.focus({ preventScroll: true });
    journalMenuTarget = null;
  };

  const journalWosUrl = (name) => {
    const queryJson = JSON.stringify([{ rowBoolean: null, rowField: "SO", rowText: name }]);
    return "https://www.webofscience.com/wos/woscc/general-summary?queryJson=" + encodeURIComponent(queryJson);
  };
  const journalNetvaultUrl = (name, base) => {
    // Reuse the prefix-aware authenticated PDF search URL.
    const searchUrl = new URL(base, window.location.origin);
    searchUrl.searchParams.set("journal", name);
    return searchUrl.href;
  };
  const openJournalMenu = (target, event) => {
    const menu = qs("[data-journal-context-menu]");
    const link = qs("[data-journal-wos-link]", menu);
    const netvaultSearch = qs("[data-journal-netvault-search]", menu);
    const name = (target.dataset.journalName || "").replace(/\s+/g, " ").trim();
    if (!menu || !link || !netvaultSearch || !name) return;
    closeJournalMenu();
    link.href = journalWosUrl(name);
    netvaultSearch.href = journalNetvaultUrl(name, menu.dataset.searchUrl);
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
  let highlightedRankJournals = [];
  const highlightRankJournal = (name, journalNode = null) => {
    // The left navigator, central journal name and Total are separate nodes.
    // Keep the same current-journal cue on all of them without touching grid
    // geometry or the year cells' existing density / hover tooltip.
    const current = qsa(
      ".heat-journal[data-journal-name], .heatmap-fixed-body .heat-total[data-journal-name]",
      qs(".page") || document,
    ).filter((entry) => entry.dataset.journalName === name);
    if (journalNode?.isConnected && journalNode.dataset.journalName === name
        && !current.includes(journalNode)) current.push(journalNode);
    highlightedRankJournals.forEach((entry) => {
      if (!current.includes(entry)) entry.removeAttribute("data-rank-current");
    });
    current.forEach((entry) => entry.setAttribute("data-rank-current", ""));
    highlightedRankJournals = current;
  };
  const rankCacheStorageKey = "netvault:journal-ranks:v1";
  // Completed lookups stay cached until the user explicitly requests Retry.
  // Provider timestamps describe the data source, not cache expiration.
  const rankCache = new Map();
  const rankPending = new Map();
  const rankKnownMissing = new Set();
  // Reuse actual table rows on repeated hovers: once rendered, switching
  // between cached journals just moves prebuilt nodes into the visible tbody.
  const rankRenderedRows = new Map();
  const validRankResult = (data) => data && typeof data.journal_name === "string"
    && Number.isFinite(data.queried_at) && data.queried_at > 0
    && data.queried_at * 1000 <= Date.now() + 300000
    && Array.isArray(data.rows) && data.rows.length <= 500
    && data.rows.every(row => row && ["key", "name", "rank", "source"].every(
      field => typeof row[field] === "string" && row[field].length <= 4000));
  try {
    const entries = JSON.parse(localStorage.getItem(rankCacheStorageKey) || "[]");
    if (Array.isArray(entries)) entries.slice(-300).forEach(entry => {
      if (Array.isArray(entry) && typeof entry[0] === "string" && validRankResult(entry[1]))
        rankCache.set(entry[0], entry[1]);
    });
  } catch { /* Memory caching still works when browser storage is unavailable. */ }
  const rankIdentity = (url, name) => url + "\0" + name.trim().replace(/\s+/g, " ").toLowerCase();
  const persistRankCache = () => {
    try { localStorage.setItem(rankCacheStorageKey, JSON.stringify([...rankCache])); }
    catch {
      // Browser quota limits vary; retain memory cache if storage is full.
      try { localStorage.setItem(rankCacheStorageKey, JSON.stringify([...rankCache].slice(-100))); }
      catch { /* Retain memory cache. */ }
    }
  };
  const cacheRanks = (key, data, persist = true) => {
    const previous = rankCache.get(key);
    // A valid server response revalidates this browser entry even when the
    // provider's original timestamp has not changed.
    const entry = previous && previous.queried_at > data.queried_at ? previous : data;
    rankCache.delete(key);
    rankCache.set(key, { ...entry, received_at: Date.now() });
    rankKnownMissing.delete(key);
    rankRenderedRows.delete(key + "\0all");
    rankRenderedRows.delete(key + "\0selected");
    while (rankCache.size > 300) {
      const evicted = rankCache.keys().next().value;
      rankCache.delete(evicted);
      rankRenderedRows.delete(evicted + "\0all");
      rankRenderedRows.delete(evicted + "\0selected");
    }
    if (persist) persistRankCache();
  };
  const invalidateRankCache = () => {
    rankCache.clear();
    rankRenderedRows.clear();
    rankKnownMissing.clear();
    try { localStorage.removeItem(rankCacheStorageKey); } catch { /* Optional browser storage. */ }
  };
  // Display the existing easyScholar dataset's Chinese labels, including for
  // older browser cache entries whose server-provided names were overridden.
  const rankDatasetLabels = new Map(Object.entries({
    swufe: "西南财经大学",
    cufe: "中央财经大学",
    uibe: "对外经济贸易大学",
    sdufe: "山东财经大学",
    xdu: "西安电子科技大学",
    swjtu: "西南交通大学",
    ruc: "中国人民大学",
    xmu: "厦门大学",
    sjtu: "上海交通大学",
    fdu: "复旦大学",
    hhu: "河海大学",
    pku: "北大核心",
    scu: "四川大学",
    sciUpTop: "中科院升级版Top分区",
    xrWarn: "新锐学术预警",
    cqu: "重庆大学",
    nju: "南京大学",
    xju: "新疆大学",
    cug: "中国地质大学",
    ccf: "中国计算机学会",
    cju: "长江大学（不是计量大学）",
    zju: "浙江大学",
    zhongguokejihexin: "中国科技核心期刊",
    fms: "FMS",
    utd24: "UTD24",
    eii: "EI检索",
    cssci: "南大核心",
    sciUpSmall: "中科院升级版小类分区",
    cpu: "中国药科大学",
    xrTop: "新锐学术Top分区",
    sciif: "SCI影响因子-JCR",
    sci: "SCI分区-JCR",
    ssci: "SSCI分区-JCR",
    jci: "JCI指数-JCR",
    sciif5: "SCI五年影响因子-JCR",
    sciwarn: "中科院预警",
    sciBase: "SCI基础版分区-中科院",
    sciUp: "SCI升级版分区-中科院",
    ajg: "ABS学术期刊指南",
    ft50: "FT50",
    cscd: "中国科学引文数据库",
    ahci: "A&HCI",
    esi: "ESI学科分类",
    xr: "新锐学术",
    xrSmall: "新锐学术小类分区",
  }).map(([code, label]) => [code.toLowerCase(), label]));
  const rankDisplayGroups = [
    { title: "期刊榜单与核心目录", codes: ["utd24", "ft50", "ajg", "cssci", "pku", "fms", "zhongguokejihexin", "cscd", "ccf", "eii", "ahci", "esi"] },
    { title: "中科院分区", codes: ["sciUp", "sciUpTop", "sciUpSmall", "sciBase", "sciwarn"] },
    { title: "JCR指标与分区", codes: ["sci", "ssci", "sciif", "sciif5", "jci"] },
    { title: "新锐学术", codes: ["xr", "xrTop", "xrSmall", "xrWarn"] },
    { title: "其他高校与机构", codes: [] },
  ];
  const rankDatasetOrder = new Map();
  rankDisplayGroups.forEach((group, category) => group.codes.forEach((code, order) =>
    rankDatasetOrder.set(code.toLowerCase(), [category, order])));
  // A shared collator avoids hundreds of expensive localeCompare collator
  // allocations during the first hovered ranking table render.
  const rankLabelCollator = new Intl.Collator("zh-CN", { numeric: true });
  const rankFallbackCategory = rankDisplayGroups.length - 1;
  const rankRowPresentation = (row) => {
    const abbreviation = String(row.abbreviation || row.key || "").trim().toLowerCase();
    const known = rankDatasetLabels.has(abbreviation);
    const [category, order] = rankDatasetOrder.get(abbreviation) || [rankFallbackCategory, 1000];
    return { row, category, order, label: known ? rankDatasetLabels.get(abbreviation) : row.name };
  };

  const rankImportantPattern = /(\u4e2d\u79d1\u9662(?:\u5347\u7ea7\u7248|\u57fa\u7840\u7248)?|ESCI|SCIE|SSCI|SCI|Q[1-4]|TOP|[1-4]\u533a|[\u4e00\u4e8c\u4e09\u56db]\u533a)/gi;
  const rankImportantKind = (token) => {
    if (/^(ESCI|SCIE|SSCI|SCI)$/i.test(token)) return "index";
    if (/^Q[12]$/i.test(token)) return "quartile";
    if (/^Q[34]$/i.test(token)) return "secondary";
    if (/^TOP$/i.test(token)) return "top";
    if (/^\u4e2d\u79d1\u9662/.test(token)) return "cas";
    if (/^[1\u4e00]\u533a$/i.test(token)) return "zone";
    return "secondary";
  };
  // EasyScholar's real result codes: utd24="UTD24", ft50="FT50",
  // ajg="4*" or "4". Do not highlight other AJG grades or the left navigator.
  const rankResultTier = (row) => {
    const code = String(row.abbreviation || row.key || "").trim().toLowerCase();
    const value = String(row.rank || "").trim();
    if (code === "utd24" && /^UTD24$/i.test(value)) return "utd24";
    if (code === "ft50" && /^FT50$/i.test(value)) return "ft50";
    if (code === "ajg" && /^4[ ]*[*★☆]$/.test(value)) return "abs4star";
    if (code === "ajg" && value === "4") return "abs4";
    return "";
  };
  const renderRankHighlights = (cell, raw) => {
    for (const part of String(raw).split(rankImportantPattern)) {
      if (!part) continue;
      if (!/^(\u4e2d\u79d1\u9662(?:\u5347\u7ea7\u7248|\u57fa\u7840\u7248)?|ESCI|SCIE|SSCI|SCI|Q[1-4]|TOP|[1-4]\u533a|[\u4e00\u4e8c\u4e09\u56db]\u533a)$/i.test(part)) {
        cell.append(part);
        continue;
      }
      const strong = document.createElement("strong");
      strong.className = "rank-emphasis rank-emphasis--" + rankImportantKind(part);
      strong.textContent = part;
      cell.append(strong);
    }
  };
  // Four icon actions follow the journal currently shown in the ranking
  // panel. A stale hover/menu selection must never be used as the target.
  const syncRankActions = (panel, name) => {
    const copy = qs("[data-rank-copy]", panel);
    const wos = qs("[data-rank-wos-link]", panel);
    const search = qs("[data-rank-netvault-search]", panel);
    if (!copy || !wos || !search) return; // Standalone benchmark fixtures.
    copy.setAttribute("data-copy", name);
    copy.disabled = false;
    qs("[data-rank-refresh]", panel).disabled = false;
    wos.href = journalWosUrl(name);
    search.href = journalNetvaultUrl(name, search.dataset.searchUrl);
    for (const link of [wos, search]) {
      link.removeAttribute("aria-disabled");
      link.removeAttribute("tabindex");
    }
  };
  const journalDetailsCache = new Map();
  const journalDetailsPending = new Map();
  let journalDetailsSequence = 0;
  const loadJournalDetails = async (panel, name, refresh = false) => {
    const section = qs("[data-rank-journal-details]", panel);
    if (!section || !panel.dataset.journalDetailsUrl) return;
    const sequence = ++journalDetailsSequence;
    const fields = qs("[data-rank-details-fields]", section);
    const status = qs("[data-rank-details-status]", section);
    const year = qs("[data-rank-jcr-year]", section);
    section.hidden = false;
    fields.hidden = true;
    year.textContent = "";
    status.textContent = "Loading journal details…";
    const key = rankIdentity(panel.dataset.journalDetailsUrl, name);
    try {
      let data;
      if (!refresh && journalDetailsCache.has(key)) data = journalDetailsCache.get(key);
      else {
        let pending = !refresh && journalDetailsPending.get(key);
        if (!pending) {
          const url = new URL(panel.dataset.journalDetailsUrl, location.origin);
          url.searchParams.set("name", name);
          pending = (async () => {
            const response = await fetch(url, {credentials: "same-origin", cache: "no-store"});
            if (!response.ok) throw Error("Unable to load journal details.");
            const result = await response.json();
            if (!Object.hasOwn(result, "journal")) throw Error("Invalid journal details.");
            if (journalDetailsPending.get(key) === pending) {
              journalDetailsCache.set(key, result.journal);
              if (journalDetailsCache.size > 300) journalDetailsCache.delete(journalDetailsCache.keys().next().value);
            }
            return result.journal;
          })();
          journalDetailsPending.set(key, pending);
        }
        try { data = await pending; }
        finally { if (journalDetailsPending.get(key) === pending) journalDetailsPending.delete(key); }
      }
      if (sequence !== journalDetailsSequence || !panel.isConnected) return;
      if (!data) { status.textContent = "No matching journal in the JCR catalogue."; return; }
      year.textContent = "JCR " + data.year;
      for (const field of ["journal_name", "publisher", "issn", "eissn", "categories", "indexes"]) {
        const cell = qs('[data-rank-detail="' + field + '"]', section);
        const raw = data[field];
        const value = Array.isArray(raw) ? raw.join("; ") : String(raw || "");
        cell.replaceChildren();
        if (["issn", "eissn"].includes(field) && /^\d{4}-\d{3}[\dX]$/i.test(value)) {
          const link = document.createElement("a");
          link.textContent = value;
          link.href = "https://www.webofscience.com/wos/woscc/general-summary?queryJson="
            + encodeURIComponent(JSON.stringify([{rowText: "IS=(" + value + ")"}]));
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.setAttribute("data-no-pjax", "");
          cell.append(link);
        } else cell.textContent = value || "—";
      }
      fields.hidden = false;
      status.textContent = "";
    } catch {
      if (sequence === journalDetailsSequence && panel.isConnected)
        status.textContent = "Unable to load journal details.";
    }
  };
  const renderRanks = () => {
    const panel = qs("[data-rank-panel]");
    if (!panel || !rankResult) return;
    const name = panel.dataset.journalName, url = panel.dataset.rankUrl;
    const cacheKey = rankIdentity(url, name) + "\0all";
    const body = qs("[data-rank-rows]", panel);
    let rendered = rankRenderedRows.get(cacheKey);
    if (!rendered) {
      const rows = rankResult.rows
        .map(rankRowPresentation)
        .sort((left, right) => left.category - right.category
          || left.order - right.order
          || rankLabelCollator.compare(left.label, right.label));
      const children = [];
      let category = -1;
      for (const entry of rows) {
        if (category !== entry.category) {
          category = entry.category;
          const tr = document.createElement("tr"), th = document.createElement("th");
          tr.className = "rank-group";
          th.colSpan = 2;
          th.textContent = rankDisplayGroups[category].title;
          tr.append(th);
          children.push(tr);
        }
        const tr = document.createElement("tr"), th = document.createElement("th"), td = document.createElement("td");
        th.scope = "row";
        renderRankHighlights(th, entry.label);
        th.title = entry.label;
        renderRankHighlights(td, entry.row.rank);
        const tier = rankResultTier(entry.row);
        if (tier) td.className = "rank-result-tier rank-result-tier--" + tier;
        td.title = entry.row.rank;
        tr.append(th, td);
        children.push(tr);
      }
      rendered = { children, hasRows: rows.length > 0 };
      rankRenderedRows.set(cacheKey, rendered);
      while (rankRenderedRows.size > 120) rankRenderedRows.delete(rankRenderedRows.keys().next().value);
    }
    // The cached nodes are detached from the previous table on a switch;
    // no sorting, highlighting or per-cell element construction is repeated.
    body.replaceChildren(...rendered.children);
    qs("[data-rank-table]", panel).hidden = !rendered.hasRows;
    // Show status text only when action or missing data requires attention.
    qs("[data-rank-status]", panel).textContent = rendered.hasRows ? ""
      : "No rankings available.";
  };
  // One lookup path serves both the embedded drawer and opportunistic cache
  // warming, sharing in-flight HTTP requests for the same journal.
  const fetchRankResult = (name, rankUrl, refresh = false) => {
    const key = rankIdentity(rankUrl, name);
    if (!refresh && rankPending.has(key)) return rankPending.get(key);
    const pending = (async () => {
      const url = new URL(rankUrl, location.origin);
      url.search = new URLSearchParams({ name, refresh: String(refresh) });
      const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
      if (!response.headers.get("content-type")?.includes("application/json")) throw Error("Please sign in again.");
      const data = await response.json();
      if (!response.ok) throw Error(typeof data.detail === "string" ? data.detail : "Unable to look up this journal.");
      if (!validRankResult(data) || rankIdentity(rankUrl, data.journal_name) !== key)
        throw Error("Invalid journal ranking response.");
      cacheRanks(key, data);
      return data;
    })();
    if (!refresh) rankPending.set(key, pending);
    return pending.finally(() => {
      if (rankPending.get(key) === pending) rankPending.delete(key);
    });
  };
  let sharedRankSeed = null;
  const primeSharedRankCache = (heatmapPanel) => {
    const rankUrl = qs("[data-journal-context-menu]")?.dataset.rankUrl;
    const csrfToken = qs("[data-journal-list-form] input[name='csrf_token']")?.value;
    const heatmap = qs(".journal-heatmap", heatmapPanel);
    if (!rankUrl || !csrfToken || !heatmap || !heatmapPanel.dataset.rankCachedUrl) return null;
    const names = [...new Map(journalRows(heatmap).rows
      .map(row => [rankIdentity(rankUrl, row.label), row.label])).entries()]
      .filter(([key]) => !rankCache.has(key) && !rankKnownMissing.has(key))
      .map(([, name]) => name);
    if (!names.length) return null;
    // The page can contain up to 200 journals. Load every visible journal
    // from shared PostgreSQL in batches of 60 (the API limit), without any
    // calls to the external provider or per-item UI updates.
    const batches = [];
    for (let index = 0; index < names.length; index += 60) batches.push(names.slice(index, index + 60));
    let added = false;
    return Promise.all(batches.map(async (batch) => {
      try {
        const response = await fetch(heatmapPanel.dataset.rankCachedUrl, {
          method: "POST", credentials: "same-origin", cache: "no-store",
          headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
          body: JSON.stringify({names: batch}),
        });
        if (!response.ok) throw Error("Unable to read shared rankings.");
        const {ranks} = await response.json();
        if (!Array.isArray(ranks)) return;
        for (const result of ranks) {
          if (validRankResult(result)) {
            const key = rankIdentity(rankUrl, result.journal_name);
            if (!rankCache.has(key)) {
              cacheRanks(key, result, false);
              added = true;
            }
          }
        }
        // An absent ranking is remembered within this page session so a
        // search or pagination change does not repeat the same DB check.
        for (const name of batch) {
          const key = rankIdentity(rankUrl, name);
          if (!rankCache.has(key)) rankKnownMissing.add(key);
        }
      } catch { /* Hover falls back to the existing server cache API. */ }
    })).then(() => { if (added) persistRankCache(); });
  };
  const loadRanks = async (refresh = false) => {
    const panel = qs("[data-rank-panel]");
    if (!panel?.dataset.journalName) return;
    const sequence = ++rankSequence;
    const name = panel.dataset.journalName, key = rankIdentity(panel.dataset.rankUrl, name);
    const current = () => sequence === rankSequence && panel.isConnected;
    let cached = rankCache.get(key);
    rankResult = cached || null;

    panel.removeAttribute("aria-busy");
    qs("[data-rank-refresh]", panel).disabled = false;
    if (cached) {
      // Synchronous hot path: pointerover -> cached table rows -> DOM.
      // Neither a timer, a Promise, nor any HTTP request precedes this.
      renderRanks();
      // Never revalidate on hover or revisit, regardless of source age.
      // An explicit Retry is the only way to replace a successful lookup.
      if (!refresh) return;
    } else {
      qs("[data-rank-table]", panel).hidden = true;
      qs("[data-rank-status]", panel).textContent =
        sharedRankSeed && !refresh ? "Loading saved rankings…" : "Fetching rankings…";
    }
    if (!refresh && !cached && sharedRankSeed) {
      await sharedRankSeed;
      if (!current()) return;
      cached = rankCache.get(key);
      if (cached) {
        rankResult = cached;
        renderRanks();
        return;
      }
    }
    panel.setAttribute("aria-busy", "true");
    qs("[data-rank-refresh]", panel).disabled = true;
    try {
      const data = await fetchRankResult(name, panel.dataset.rankUrl, refresh);
      if (!current()) return;
      rankResult = rankCache.get(key) || data;
      renderRanks();
    } catch (error) {
      if (current()) qs("[data-rank-status]", panel).textContent = error.message + (cached ? " Showing saved rankings." : "");
    } finally {
      if (current()) {
        panel.removeAttribute("aria-busy"); qs("[data-rank-refresh]", panel).disabled = false;
      }
    }
  };
  const showRankPanel = (name, url, hover = false, journalNode = null) => {
    const panel = qs("[data-rank-panel]");
    if (!panel || !name || !url) return;
    highlightRankJournal(name, journalNode);
    if (hover && panel.dataset.journalName === name) return;
    panel.dataset.journalName = name;
    panel.dataset.rankUrl = url;
    panel.dataset.rankOpenMode = hover ? "hover" : "manual";
    qs("[data-rank-title]", panel).textContent = name;
    syncRankActions(panel, name);
    void loadJournalDetails(panel, name);
    if (!hover) qs("[data-rank-refresh]", panel)?.focus({preventScroll:true});
    void loadRanks();
  };
  const openRankPanel = () => {
    const menu = qs("[data-journal-context-menu]");
    if (!menu) return;
    showRankPanel(menu.dataset.journalName, menu.dataset.rankUrl);
    closeJournalMenu();
  };
  document.addEventListener("pointerover", event => {
    // Hover lookup is always enabled. Ignore old saved toggle preferences:
    // pointerover must synchronously switch the permanent ranking panel.
    if (event.pointerType !== "mouse" || event.buttons) return;
    const journal = event.target.closest?.(".heat-journal[data-journal-name]");
    const statistic = journal ? null : event.target.closest?.(
      ".journal-heatmap .heat-cell[data-journal-row], .heatmap-fixed-body .heat-total[data-journal-row]",
    );
    if (!journal && !statistic) return;
    if (journal && event.relatedTarget && journal.contains(event.relatedTarget)) return;
    const menu = qs("[data-journal-context-menu]");
    if (!menu || !menu.hidden) return;
    // Row IDs originate from the shared heatmap data and survive sorting,
    // searching and pagination. Reading the in-memory row avoids repeating
    // large journal names on every year-cell element (including empty cells).
    const heatmap = statistic && qs(".journal-heatmap", statistic.closest(".heatmap-panel"));
    const rowId = statistic ? Number(statistic.dataset.journalRow) : -1;
    const row = heatmap && Number.isInteger(rowId) && rowId >= 0
      ? journalRows(heatmap).rows[rowId] : null;
    const name = (journal?.dataset.journalName || statistic?.dataset.journalName
      || row?.label || "").replace(/\s+/g, " ").trim();
    const panel = qs("[data-rank-panel]");
    if (!name || (panel && panel.dataset.journalName === name)) return;
    // Immediate in the pointerover call stack; rapid passes across several
    // rows always show the latest name, with no 120ms hover delay.
    showRankPanel(name, menu.dataset.rankUrl, true, journal);
  });
  document.addEventListener("click", event => {
    if (event.target.closest("[data-rank-refresh]")) {
      loadRanks(true);
      const panel = qs("[data-rank-panel]");
      if (panel?.dataset.journalName) void loadJournalDetails(panel, panel.dataset.journalName, true);
    }
  });
  let rankBatchTimer = null;
  let lastCompletedRankJob = null;
  let explicitlyRefreshedBatch = null;
  const setRankBatchMessage = (panel, message, retry = false, brief = "Ranks") => {
    const banner = qs("[data-rank-batch-banner]", panel);
    if (!banner) return;
    banner.hidden = false;
    banner.title = message;
    qs("[data-rank-batch-message]", banner).textContent = message;
    qs("[data-rank-batch-brief]", banner).textContent = brief;
    qs("[data-rank-batch-retry]", banner).hidden = !retry;
  };
  const showRankBatchProgress = (panel, job) => {
    if (!panel?.isConnected || !job) return;
    const done = job.status !== "queued" && job.status !== "running";
    const progress = job.processed + "/" + job.total + " journals ("
      + job.fetched + " fetched, " + job.cached + " cached, "
      + job.failed + " failed)";
    const message = done
      ? "Ranking check " + (job.status === "completed" ? "completed" : "finished with errors")
        + ": " + progress + (job.last_error ? ". Last error: " + job.last_error : "")
      : "Checking list rankings in the background: " + progress;
    const brief = job.processed + "/" + job.total + (job.failed ? "!" : "");
    setRankBatchMessage(panel, message, done, brief);
    qs("[data-rank-batch-banner]", panel).dataset.rankState = job.status;
    if (done && lastCompletedRankJob !== job.id) {
      lastCompletedRankJob = job.id;
    }
    if (done && explicitlyRefreshedBatch === job.id) {
      explicitlyRefreshedBatch = null;
      // Only an explicit list-wide Retry invalidates the browser rankings.
      // A completed background job seen on page load must not clear or
      // prefetch journals that the user has already looked up.
      invalidateRankCache();
    }
  };
  const pollRankBatch = async (panel) => {
    window.clearTimeout(rankBatchTimer);
    if (!panel?.isConnected || panel.dataset.dashboardFilter === "all") return;
    const filter = panel.dataset.dashboardFilter;
    try {
      const url = new URL(panel.dataset.rankBatchStatusUrl, location.origin);
      url.searchParams.set("filter", filter);
      const response = await fetch(url, {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) throw new Error("Unable to check list ranking progress.");
      const {job} = await response.json();
      if (!panel.isConnected || panel.dataset.dashboardFilter !== filter) return;
      if (job) {
        showRankBatchProgress(panel, job);
        if (job.status === "queued" || job.status === "running")
          rankBatchTimer = window.setTimeout(() => pollRankBatch(panel), 2000);
      }
    } catch (error) {
      if (panel.isConnected) {
        setRankBatchMessage(panel, error.message + " Retrying status check...");
        rankBatchTimer = window.setTimeout(() => pollRankBatch(panel), 5000);
      }
    }
  };
  const startRankBatch = async (refresh = false) => {
    const panel = qs(".heatmap-panel");
    if (!panel) return;
    closeJournalMenu();
    const filter = panel.dataset.dashboardFilter || "";
    if (!filter || filter === "all") {
      setRankBatchMessage(panel, "Select a journal list before fetching rankings.");
      return;
    }
    const token = qs("[data-journal-list-form] input[name='csrf_token']")?.value || "";
    if (!token) {
      setRankBatchMessage(panel, "Please sign in again before requesting rankings.");
      return;
    }
    setRankBatchMessage(panel, "Starting background journal ranking check...", false, "Ranks…");
    try {
      const response = await fetch(panel.dataset.rankBatchUrl, {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": token},
        body: JSON.stringify({filter, refresh}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Unable to start the ranking check.");
      if (!panel.isConnected) return;
      if (refresh) explicitlyRefreshedBatch = data.id;
      showRankBatchProgress(panel, data);
      void pollRankBatch(panel);
    } catch (error) {
      if (panel.isConnected) setRankBatchMessage(panel, error.message, true);
    }
  };
  document.addEventListener("click", event => {
    if (event.target.closest("[data-journal-rank-batch]")) {
      void startRankBatch(false);
      return;
    }
    if (event.target.closest("[data-rank-batch-retry]")) {
      void startRankBatch(true);
    }
  });
  document.addEventListener("keydown", event => {
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
    saveJournalView(input.closest(".heatmap-panel"));
    journalLimitTimer = window.setTimeout(() => saveJournalLimit(input, revision), 600);
  };

  const flushJournalLimit = (input) => {
    window.clearTimeout(journalLimitTimer);
    if (journalLimitRequest) journalLimitRequest.abort();
    const revision = String(Number(input.dataset.saveRevision || 0) + 1);
    input.dataset.saveRevision = revision;
    input.closest(".heatmap-panel").dataset.journalPage = "1";
    applyJournalFilters(input.closest(".heatmap-panel"));
    saveJournalView(input.closest(".heatmap-panel"));
    saveJournalLimit(input, revision);
  };

  const journalCollectionsScrollKey = "netvault:journal-collections-scroll:v1";
  const journalGroupPreferenceKey = "netvault:journal-group-open:v1:";
  const initJournalLibrary = (root = document) => {
    const library = qs("[data-journal-library]", root);
    // Drop the obsolete book icon after PJAX navigation as well as on load.
    qs("[data-journal-library-rail-toggle]")?.remove();
    if (!library) {
      delete document.body.dataset.journalLibraryOpen;
      return;
    }
    // The navigator is permanently visible; there is no collapse control.
    document.body.dataset.journalLibraryOpen = "true";
    // PJAX list switches replace the sidebar DOM; restore the previous
    // scroll position so someone comparing adjacent ABS/custom lists does
    // not have to find their place again after every click.
    const collections = qs(".journal-library-collections", library);
    if (collections) {
      try {
        collections.scrollTop = Math.max(0, Number(sessionStorage.getItem(journalCollectionsScrollKey)) || 0);
      } catch { /* Storage may be disabled; navigation still works. */ }
      collections.addEventListener("scroll", () => {
        try { sessionStorage.setItem(journalCollectionsScrollKey, String(collections.scrollTop)); }
        catch { /* Optional tab-local preference. */ }
      }, { passive: true });
    }
    qsa("details[data-journal-list-group]", library).forEach((group) => {
      try {
        const previous = localStorage.getItem(journalGroupPreferenceKey + group.dataset.journalListGroup);
        if (previous !== null) group.open = previous !== "false";
      } catch { /* Native details still work when localStorage is unavailable. */ }
    });
  };
  document.addEventListener("click", event => {
    if (event.target.closest?.("[data-journal-create-cancel]")) {
      qs("[data-journal-create-dialog]")?.close();
      return;
    }
  });
  const initHeatmap = (root = document) => {
    initJournalLibrary(root);
    qsa(".heatmap-panel", root).forEach((panel) => {
      if (panel.dataset.rankBatchStatusUrl) void pollRankBatch(panel);
      const view = loadJournalView(panel);
      const search = qs("[data-journal-filter]", panel);
      if (search && typeof view.search === "string") search.value = view.search.slice(0, 300);
      if (Number.isInteger(view.page) && view.page > 0 && view.page <= 10000)
        panel.dataset.journalPage = String(view.page);
      if (Number.isFinite(view.yearScrollLeft) && view.yearScrollLeft >= 0)
        panel.dataset.savedYearScroll = String(view.yearScrollLeft);
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

  // The ZIP endpoint already validates the authenticated IDs, CSRF token and
  // archive limits. Form-associated checkboxes submit only checked PDFs from
  // the current search page; nothing is copied into hidden inputs by JS.
  const pdfSelectionCheckboxes = () => qsa(
    "[data-pdf-select-item] [data-pdf-select-checkbox]", qs(mainSelector) || document
  );
  const updatePdfSelection = () => {
    const toolbar = qs("[data-pdf-selection-toolbar]");
    if (!toolbar) return;
    const checkboxes = pdfSelectionCheckboxes();
    const selected = checkboxes.filter((checkbox) => checkbox.checked).length;
    const allSelected = checkboxes.length > 0 && selected === checkboxes.length;
    const toggle = qs("[data-pdf-select-toggle]", toolbar);
    const label = qs("[data-pdf-select-toggle-label]", toolbar);
    const count = qs("[data-pdf-selected-count]", toolbar);
    const download = qs("[data-pdf-selected-zip]", toolbar);
    if (toggle) toggle.setAttribute("aria-pressed", String(allSelected));
    if (label) label.textContent = allSelected ? "Deselect all" : "Select all";
    if (count) count.textContent = selected + " selected";
    if (download) download.disabled = selected === 0;
    checkboxes.forEach((checkbox) => {
      checkbox.closest("[data-pdf-select-item]")?.classList.toggle("is-selected", checkbox.checked);
    });
  };
  document.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-pdf-select-toggle]");
    if (!toggle) return;
    const checkboxes = pdfSelectionCheckboxes();
    const selectAll = checkboxes.some((checkbox) => !checkbox.checked);
    checkboxes.forEach((checkbox) => { checkbox.checked = selectAll; });
    updatePdfSelection();
  });
  document.addEventListener("change", (event) => {
    if (event.target.matches?.("[data-pdf-select-checkbox]")) updatePdfSelection();
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

    const listExport = event.target.closest("[data-journal-list-export]");
    if (listExport) {
      exportJournalList(listExport);
      return;
    }

    const customListCreate = event.target.closest("[data-custom-list-create]");
    if (customListCreate) {
      openCreateJournalDialog(customListCreate);
      return;
    }

    const customListDelete = event.target.closest("[data-journal-custom-delete]");
    if (customListDelete) {
      event.preventDefault();
      void deleteCustomJournalList(customListDelete);
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
      const panel = sortHeading.closest(".heatmap-panel") || qs(".heatmap-panel");
      const select = qs("[data-journal-sort]", panel);
      if (!select) return;
      const field = sortHeading.dataset.journalSortToggle;
      const direction = select.value === field + "-asc" ? "desc"
        : select.value === field + "-desc" ? "asc"
        : field === "name" ? "asc" : "desc";
      select.value = field + "-" + direction;
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
    if (journal) {
      event.preventDefault();
      if (journal.closest("[data-journal-library-items]")) {
        if (event.target.closest("[data-journal-more]")) openJournalMenu(journal, event);
        else {
          const menu = qs("[data-journal-context-menu]");
          if (menu) showRankPanel(journal.dataset.journalName, menu.dataset.rankUrl, true, journal);
        }
      } else {
        // Regular click selects a journal; its four frequent actions live in
        // the permanent ranking toolbar. Right-click still offers bulk tools.
        const url = qs("[data-journal-context-menu]")?.dataset.rankUrl;
        if (url) showRankPanel(journal.dataset.journalName, url, false, journal);
      }
      return;
    }
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

  document.addEventListener("keydown", event => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const journal = event.target.closest?.(".heatmap-fixed-body .heat-journal[data-journal-name]");
    if (!journal) return;
    event.preventDefault();
    const url = qs("[data-journal-context-menu]")?.dataset.rankUrl;
    if (url) showRankPanel(journal.dataset.journalName, url, false, journal);
  });

  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest("[data-journal-context-menu]")) closeJournalMenu();
  });
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-journal-wos-link], [data-journal-netvault-search]")) closeJournalMenu();
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
    if (menu.matches?.("details[data-journal-list-group]")) {
      try { localStorage.setItem(journalGroupPreferenceKey + menu.dataset.journalListGroup, String(menu.open)); }
      catch { /* Optional per-device preference. */ }
      return;
    }
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
  rememberDashboardFilter(window.location.href);
})();
