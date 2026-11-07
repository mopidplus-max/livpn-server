/* LiVPN UI — progressive enhancements. No dependencies. */
(function () {
  "use strict";

  var root = document.documentElement;
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── Theme ───────────────────────────────────────────────────────────── */

  var metaTheme = document.getElementById("meta-theme");
  var themeToggle = document.getElementById("theme-toggle");

  function syncThemeMeta() {
    if (!metaTheme) return;
    metaTheme.setAttribute("content", root.dataset.theme === "light" ? "#f4f6fd" : "#070b14");
  }

  function setTheme(theme) {
    root.dataset.theme = theme;
    try { localStorage.setItem("livpn-theme", theme); } catch (e) {}
    syncThemeMeta();
    if (themeToggle) {
      themeToggle.setAttribute("aria-label", theme === "light" ? "Включить тёмную тему" : "Включить светлую тему");
    }
  }

  syncThemeMeta();
  if (themeToggle) {
    setTheme(root.dataset.theme === "light" ? "light" : "dark");
    themeToggle.addEventListener("click", function () {
      setTheme(root.dataset.theme === "light" ? "dark" : "light");
    });
  }

  /* ── Toasts ──────────────────────────────────────────────────────────── */

  var stack = document.getElementById("toasts");
  var TOAST_ICONS = { success: "#i-check", error: "#i-x", info: "#i-spark" };

  function toast(message, type) {
    if (!stack) return;
    var kind = type || "success";
    var node = document.createElement("div");
    node.className = "toast toast--" + kind;
    node.innerHTML =
      '<span class="toast-icon"><svg class="icon" aria-hidden="true"><use href="' +
      (TOAST_ICONS[kind] || TOAST_ICONS.info) +
      '"></use></svg></span><span class="toast-text"></span>';
    node.querySelector(".toast-text").textContent = message;
    stack.appendChild(node);
    window.setTimeout(function () {
      node.classList.add("is-out");
      window.setTimeout(function () { node.remove(); }, 240);
    }, 3400);
  }

  /* ── Clipboard ───────────────────────────────────────────────────────── */

  function fallbackCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    area.remove();
    return ok;
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(
        function () { return true; },
        function () { return fallbackCopy(text); }
      );
    }
    return Promise.resolve(fallbackCopy(text));
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-copy]");
    if (!trigger) return;
    if (trigger.tagName === "A") event.preventDefault();
    var value = trigger.getAttribute("data-copy") || "";
    if (!value) return;
    copyText(value).then(function (ok) {
      toast(ok ? (trigger.getAttribute("data-copy-label") || "Скопировано") : "Не удалось скопировать", ok ? "success" : "error");
    });
  });

  /* ── Share ───────────────────────────────────────────────────────────── */

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-share]");
    if (!trigger) return;
    var url = trigger.getAttribute("data-share") || location.href;
    var title = trigger.getAttribute("data-share-title") || document.title;
    if (navigator.share) {
      navigator.share({ title: title, text: "Моя подписка LiVPN", url: url }).catch(function () {});
      return;
    }
    copyText(url).then(function (ok) {
      toast(ok ? "Ссылка скопирована" : "Не удалось скопировать", ok ? "success" : "error");
    });
  });

  /* ── QR dialog ───────────────────────────────────────────────────────── */

  var dialog = document.getElementById("qr-dialog");
  if (dialog) {
    var qrImage = document.getElementById("qr-image");
    var qrTitle = document.getElementById("qr-title");

    document.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-qr]");
      if (!trigger) return;
      event.preventDefault();
      var src = trigger.getAttribute("data-qr");
      if (!src) return;
      if (qrImage) {
        qrImage.src = src;
        qrImage.alt = "QR-код: " + (trigger.getAttribute("data-qr-title") || "LiVPN");
      }
      if (qrTitle) qrTitle.textContent = trigger.getAttribute("data-qr-title") || "QR-код";
      if (typeof dialog.showModal === "function") dialog.showModal();
      else window.open(src, "_blank", "noopener");
    });

    dialog.querySelectorAll("[data-close-dialog]").forEach(function (button) {
      button.addEventListener("click", function () { dialog.close(); });
    });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("close", function () {
      if (qrImage) qrImage.removeAttribute("src");
    });
  }

  /* ── Header on scroll ────────────────────────────────────────────────── */

  var header = document.querySelector("[data-header]");
  if (header) {
    var onScroll = function () {
      header.classList.toggle("is-scrolled", window.scrollY > 8);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  /* ── Reveal on scroll ────────────────────────────────────────────────── */

  var revealItems = Array.prototype.slice.call(document.querySelectorAll(".reveal"));
  if (revealItems.length) {
    if (!("IntersectionObserver" in window) || reduceMotion) {
      revealItems.forEach(function (item) { item.classList.add("is-visible"); });
    } else {
      var revealObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("is-visible");
          revealObserver.unobserve(entry.target);
        });
      }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
      revealItems.forEach(function (item) { revealObserver.observe(item); });
    }
  }

  /* ── Animated counters ───────────────────────────────────────────────── */

  function animateCounter(element) {
    var target = parseInt(element.getAttribute("data-count") || "0", 10);
    if (!isFinite(target) || target <= 0) {
      element.textContent = "0";
      return;
    }
    if (reduceMotion) {
      element.textContent = target.toLocaleString("ru-RU");
      return;
    }
    var start = performance.now();
    var duration = 900;
    function frame(now) {
      var progress = Math.min(1, (now - start) / duration);
      var eased = 1 - Math.pow(1 - progress, 3);
      element.textContent = Math.round(target * eased).toLocaleString("ru-RU");
      if (progress < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  var counters = Array.prototype.slice.call(document.querySelectorAll("[data-count]"));
  if (counters.length) {
    if (!("IntersectionObserver" in window)) {
      counters.forEach(animateCounter);
    } else {
      var counterObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          animateCounter(entry.target);
          counterObserver.unobserve(entry.target);
        });
      }, { threshold: 0.4 });
      counters.forEach(function (item) { counterObserver.observe(item); });
    }
  }

  /* ── Countdown to auto refresh ───────────────────────────────────────── */

  function formatDuration(seconds) {
    if (seconds <= 0) return "скоро";
    var days = Math.floor(seconds / 86400);
    var hours = Math.floor((seconds % 86400) / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    if (days > 0) return "через " + days + " д " + hours + " ч";
    if (hours > 0) return "через " + hours + " ч " + minutes + " мин";
    if (minutes > 0) return "через " + minutes + " мин";
    return "меньше минуты";
  }

  var countdown = document.querySelector("[data-countdown]");
  if (countdown) {
    var remaining = parseInt(countdown.getAttribute("data-countdown") || "0", 10);
    if (!isFinite(remaining)) remaining = 0;
    var renderCountdown = function () {
      countdown.textContent = formatDuration(remaining);
    };
    renderCountdown();
    window.setInterval(function () {
      remaining = Math.max(0, remaining - 1);
      renderCountdown();
    }, 1000);
  }

  /* ── Landing: open dashboard by link/token ───────────────────────────── */

  function tokenFromValue(raw) {
    var value = (raw || "").trim();
    if (!value) return "";
    var nested = value.match(/\/(?:s|sub)\/([a-z0-9]{8,64})(?:[?#/]|$)/i);
    if (nested) return nested[1].toLowerCase();
    if (/^[a-z0-9]{8,64}$/i.test(value)) return value.toLowerCase();
    return "";
  }

  Array.prototype.slice.call(document.querySelectorAll("#open-sub-form, #open-sub-form-2")).forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var input = form.querySelector("input");
      if (!input) return;
      var token = tokenFromValue(input.value);
      if (!token) {
        var looksLikeServer = /^[a-z][a-z0-9+.-]*:\/\//i.test(input.value.trim()) && !/\/(?:s|sub)\//i.test(input.value);
        toast(
          looksLikeServer
            ? "Это ссылка на сервер. Нужна ссылка подписки вида /sub/…"
            : "Не похоже на ссылку подписки. Проверьте адрес или токен.",
          "error"
        );
        input.focus();
        return;
      }
      window.location.href = "/s/" + token;
    });
  });

  /* ── Node search and filters ─────────────────────────────────────────── */

  var nodeGrid = document.getElementById("node-grid");
  var searchInput = document.getElementById("node-search");
  var filterCount = document.getElementById("filter-count");
  var emptyState = document.getElementById("node-empty");
  var filterButtons = Array.prototype.slice.call(document.querySelectorAll("[data-filter]"));
  var nodeCards = nodeGrid
    ? Array.prototype.slice.call(nodeGrid.querySelectorAll(".node-card"))
    : [];
  var activeFilter = "all";

  function applyFilters() {
    if (!nodeCards.length) return;
    var query = (searchInput ? searchInput.value : "").trim().toLowerCase();
    var shown = 0;
    nodeCards.forEach(function (card) {
      var transport = card.getAttribute("data-transport") || "other";
      var haystack = card.getAttribute("data-search") || "";
      var matchesFilter = activeFilter === "all" || transport === activeFilter;
      var matchesQuery = !query || haystack.indexOf(query) !== -1;
      var visible = matchesFilter && matchesQuery;
      card.hidden = !visible;
      if (visible) shown += 1;
    });
    if (filterCount) filterCount.textContent = "Показано " + shown + " из " + nodeCards.length;
    if (emptyState) emptyState.hidden = shown !== 0;
    if (nodeGrid) nodeGrid.hidden = shown === 0;
  }

  if (nodeCards.length) {
    filterButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        activeFilter = button.getAttribute("data-filter") || "all";
        filterButtons.forEach(function (other) {
          other.classList.toggle("is-active", other === button);
        });
        applyFilters();
      });
    });
    if (searchInput) searchInput.addEventListener("input", applyFilters);
    var resetButton = document.getElementById("reset-filter");
    if (resetButton) {
      resetButton.addEventListener("click", function () {
        if (searchInput) searchInput.value = "";
        activeFilter = "all";
        filterButtons.forEach(function (other) {
          other.classList.toggle("is-active", other.getAttribute("data-filter") === "all");
        });
        applyFilters();
      });
    }
  }

  /* ── Manual refresh ──────────────────────────────────────────────────── */

  function formatRetry(seconds) {
    var value = Math.max(1, parseInt(seconds, 10) || 0);
    var minutes = Math.ceil(value / 60);
    if (minutes <= 1) return "минуту";
    if (minutes < 60) return minutes + " мин";
    return Math.ceil(minutes / 60) + " ч";
  }

  Array.prototype.slice.call(document.querySelectorAll('[data-action="refresh"]')).forEach(function (button) {
    button.addEventListener("click", function () {
      var token = button.getAttribute("data-token");
      if (!token) return;
      var buttons = Array.prototype.slice.call(document.querySelectorAll('[data-action="refresh"]'));
      var label = button.querySelector("span");
      var previous = label ? label.textContent : "";
      buttons.forEach(function (item) { item.disabled = true; });
      button.classList.add("is-busy");
      if (label) label.textContent = "Обновляем…";

      fetch("/api/refresh/" + encodeURIComponent(token), {
        method: "POST",
        headers: { Accept: "application/json" },
        credentials: "same-origin"
      })
        .then(function (response) {
          return response.json().catch(function () { return {}; }).then(function (data) {
            return { status: response.status, data: data };
          });
        })
        .then(function (result) {
          var data = result.data || {};
          if (result.status === 200 || result.status === 202) {
            toast(data.pending ? "Обновление уже выполняется…" : "Подписка обновлена", data.pending ? "info" : "success");
            window.setTimeout(function () { window.location.reload(); }, 1100);
            return;
          }
          if (result.status === 429 && data.error === "cooldown") {
            toast("Уже обновлено. Повторите через " + formatRetry(data.retry_after), "info");
          } else {
            toast(data.message || "Не удалось обновить. Попробуйте позже.", "error");
          }
          buttons.forEach(function (item) { item.disabled = false; });
          button.classList.remove("is-busy");
          if (label) label.textContent = previous;
        })
        .catch(function () {
          toast("Нет связи с сервером. Проверьте интернет.", "error");
          buttons.forEach(function (item) { item.disabled = false; });
          button.classList.remove("is-busy");
          if (label) label.textContent = previous;
        });
    });
  });

  /* ── Smooth scroll buttons ───────────────────────────────────────────── */

  document.querySelectorAll("[data-scroll]").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = document.querySelector(button.getAttribute("data-scroll"));
      if (target) target.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    });
  });

  /* ── PWA install ─────────────────────────────────────────────────────── */

  var installButton = document.getElementById("install-btn");
  var deferredPrompt = null;

  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferredPrompt = event;
    if (installButton) installButton.hidden = false;
  });

  if (installButton) {
    installButton.addEventListener("click", function () {
      if (!deferredPrompt) return;
      deferredPrompt.prompt();
      deferredPrompt.userChoice.finally(function () {
        deferredPrompt = null;
        installButton.hidden = true;
      });
    });
  }

  window.addEventListener("appinstalled", function () {
    if (installButton) installButton.hidden = true;
    toast("LiVPN добавлен на главный экран", "success");
  });

  /* ── Service worker ──────────────────────────────────────────────────── */

  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1")) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    });
  }
})();
