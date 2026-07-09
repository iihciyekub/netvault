(function () {
  "use strict";

  const mainSelector = "#app-main";
  const htmlType = "text/html";
  let activeTooltipTarget = null;

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
      if (!submit.dataset.originalText) submit.dataset.originalText = submit.textContent.trim();
      submit.textContent = busy ? label : submit.dataset.originalText;
    }
  };

  const updateFromHtml = (html, url, push) => {
    const nextDoc = new DOMParser().parseFromString(html, htmlType);
    const nextMain = qs(mainSelector, nextDoc);
    if (!nextMain) {
      window.location.href = url;
      return;
    }

    const currentMain = qs(mainSelector);
    const nextTopbar = qs(".topbar", nextDoc);
    const currentTopbar = qs(".topbar");
    if (currentTopbar && nextTopbar) currentTopbar.innerHTML = nextTopbar.innerHTML;
    document.body.className = nextDoc.body.className;
    currentMain.innerHTML = nextMain.innerHTML;
    document.title = nextDoc.title || document.title;

    if (push && url !== window.location.href) history.pushState({}, "", url);
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    setLoading(false);
  };

  const fetchPage = async (url, options = {}, push = true) => {
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
      updateFromHtml(html, response.url || url, push);
    } catch {
      window.location.href = url;
    } finally {
      setLoading(false);
    }
  };

  const copyText = async (button) => {
    const text = button.getAttribute("data-copy") || "";
    const original = button.textContent;
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
      button.textContent = "Copied";
    } catch {
      button.textContent = "Failed";
    }
    window.setTimeout(() => {
      button.textContent = original;
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
    tooltip.classList.add("is-visible");
    positionTooltip(event);
  };

  const positionTooltip = (event) => {
    if (!activeTooltipTarget) return;
    const tooltip = qs(".heatmap-tooltip");
    if (!tooltip) return;
    const offset = 12;
    const rect = tooltip.getBoundingClientRect();
    let left = event.clientX + offset;
    let top = event.clientY + offset;
    if (left + rect.width + 8 > window.innerWidth) left = event.clientX - rect.width - offset;
    if (top + rect.height + 8 > window.innerHeight) top = event.clientY - rect.height - offset;
    tooltip.style.transform = `translate(${Math.max(8, left)}px, ${Math.max(8, top)}px)`;
  };

  const hideTooltip = () => {
    activeTooltipTarget = null;
    const tooltip = qs(".heatmap-tooltip");
    if (tooltip) tooltip.classList.remove("is-visible");
  };

  const updateFileSummary = (input) => {
    const summary = qs("#file-summary");
    if (!summary) return;
    const count = input.files.length;
    if (!count) summary.textContent = "Choose files";
    else if (count === 1) summary.textContent = input.files[0].name;
    else summary.textContent = `${count} files selected`;
  };

  const uploadWithProgress = (form) => {
    const xhr = new XMLHttpRequest();
    const body = new FormData(form);
    const progress = qs(".upload-progress", form);
    const progressBar = qs(".upload-progress-bar span", form);
    const progressText = qs(".upload-progress p", form);

    setFormBusy(form, true, "Uploading...");
    if (progress) progress.hidden = false;
    if (progressBar) progressBar.style.width = "0%";
    if (progressText) progressText.textContent = "Uploading 0%";

    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.round((event.loaded / event.total) * 100);
      if (progressBar) progressBar.style.width = `${percent}%`;
      if (progressText) progressText.textContent = percent >= 100 ? "Processing PDFs..." : `Uploading ${percent}%`;
    });

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 400) {
        if (progressBar) progressBar.style.width = "100%";
        if (progressText) progressText.textContent = "Done";
        updateFromHtml(xhr.responseText, xhr.responseURL || form.action, true);
      } else {
        if (progressText) progressText.textContent = "Upload failed";
        setFormBusy(form, false, "Upload");
      }
    });

    xhr.addEventListener("error", () => {
      if (progressText) progressText.textContent = "Network error";
      setFormBusy(form, false, "Upload");
    });

    xhr.open(form.method || "POST", form.action);
    xhr.setRequestHeader("X-Requested-With", "fetch");
    xhr.withCredentials = true;
    xhr.send(body);
  };

  document.addEventListener("click", (event) => {
    const copyButton = event.target.closest("[data-copy]");
    if (copyButton) {
      event.preventDefault();
      copyText(copyButton);
      return;
    }

    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (link.target || link.hasAttribute("download")) return;
    const url = sameOriginUrl(link.href);
    if (!url || !isWebPath(url) || isDownloadPath(url)) return;
    event.preventDefault();
    fetchPage(url.href);
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    const url = sameOriginUrl(form.action);
    if (!url || !isWebPath(url)) return;
    event.preventDefault();

    if (form.matches("[data-upload-form]")) {
      uploadWithProgress(form);
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

  document.addEventListener("dragenter", (event) => {
    const dropzone = event.target.closest("#dropzone");
    if (dropzone) dropzone.classList.add("is-dragging");
  });

  document.addEventListener("dragover", (event) => {
    const dropzone = event.target.closest("#dropzone");
    if (dropzone) dropzone.classList.add("is-dragging");
  });

  document.addEventListener("dragleave", (event) => {
    const dropzone = event.target.closest("#dropzone");
    if (dropzone) dropzone.classList.remove("is-dragging");
  });

  document.addEventListener("drop", (event) => {
    const dropzone = event.target.closest("#dropzone");
    if (dropzone) dropzone.classList.remove("is-dragging");
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

  window.addEventListener("popstate", () => {
    fetchPage(window.location.href, {}, false);
  });
})();
