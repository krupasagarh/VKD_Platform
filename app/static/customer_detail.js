(function () {
  var page = document.querySelector(".cd-page");
  if (!page) return;

  function overlay(id) {
    return document.getElementById(id);
  }

  var ignoreBackdropUntil = 0;

  function openOverlay(id) {
    var el = overlay(id);
    if (!el) return;
    document.body.classList.remove("nav-open");
    document.documentElement.classList.remove("nav-open");
    el.classList.add("on");
    document.body.style.overflow = "hidden";
    ignoreBackdropUntil = Date.now() + 500;
    var focus = el.querySelector("input, select, textarea, button.btn-primary");
    if (focus) setTimeout(function () { focus.focus(); }, 30);
  }

  function closeOverlay(el) {
    if (!el) return;
    el.classList.remove("on");
    if (!document.querySelector(".cd-overlay.on")) {
      document.body.style.overflow = "";
    }
  }

  function closeAll() {
    document.querySelectorAll(".cd-overlay.on").forEach(closeOverlay);
  }

  document.addEventListener("click", function (e) {
    if (!e.target.closest(".menu-toggle")) return;
    if (!document.querySelector(".cd-overlay.on")) return;
    closeAll();
  }, true);

  document.addEventListener("click", function (e) {
    var openBtn = e.target.closest("[data-open]");
    if (openBtn) {
      e.preventDefault();
      var details = openBtn.closest("details");
      if (details) details.open = false;
      var connId = openBtn.getAttribute("data-conn");
      var connSel = connId && document.getElementById("collect-connection");
      if (connSel) {
        connSel.value = connId;
        connSel.dispatchEvent(new Event("change", { bubbles: true }));
      }
      openOverlay(openBtn.getAttribute("data-open"));
      return;
    }
    var closeBtn = e.target.closest("[data-close]");
    if (closeBtn) {
      e.preventDefault();
      closeOverlay(closeBtn.closest(".cd-overlay"));
    }
  });

  document.querySelectorAll(".cd-overlay").forEach(function (el) {
    el.addEventListener("click", function (e) {
      if (e.target !== el) return;
      if (Date.now() < ignoreBackdropUntil) return;
      closeOverlay(el);
    });
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeAll();
  });

  var VALID_TABS = { connections: 1, statement: 1, complaints: 1, jobs: 1, plan: 1 };
  var HASH_TAB = {
    connections: "connections",
    statement: "statement",
    "bills-receipts": "statement",
    "bix-history": "statement",
    complaints: "complaints",
    "job-history": "jobs",
    jobs: "jobs",
    "customer-plan": "plan",
    plan: "plan",
  };
  var HASH_OPEN = {
    collect: "collect",
    "change-due": "change-due",
    "add-stb": "add-stb",
  };

  function showTab(name) {
    if (!VALID_TABS[name] || !document.getElementById("tab-" + name)) name = "connections";
    page.querySelectorAll(".cd-tabs button").forEach(function (btn) {
      var on = btn.getAttribute("data-tab") === name;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    page.querySelectorAll(".cd-panel").forEach(function (panel) {
      panel.classList.toggle("on", panel.id === "tab-" + name);
    });
    var active = page.querySelector(".cd-tabs button.on");
    if (active && active.scrollIntoView) {
      active.scrollIntoView({ inline: "nearest", block: "nearest" });
    }
    return name;
  }

  function applyPayFilter(on) {
    var panel = document.getElementById("tab-statement");
    if (!panel) return;
    panel.classList.toggle("is-pay-filter", !!on);
    var note = panel.querySelector(".cd-pay-filter-note");
    if (note) note.hidden = !on;
  }

  function tabFromUrl() {
    var params = new URLSearchParams(location.search);
    if (params.has("bix")) return "statement";
    var tab = params.get("tab");
    if (tab && VALID_TABS[tab]) return tab;
    var hash = (location.hash || "").replace("#", "");
    if (HASH_TAB[hash]) return HASH_TAB[hash];
    return "connections";
  }

  function writeTabUrl(name, filter, push) {
    var url = new URL(location.href);
    url.searchParams.set("tab", name);
    if (filter) url.searchParams.set("filter", filter);
    else url.searchParams.delete("filter");
    var href = url.pathname + url.search;
    if (push && history.pushState) history.pushState({ tab: name }, "", href);
    else if (history.replaceState) history.replaceState({ tab: name }, "", href);
  }

  function applyFromUrl(opts) {
    var params = new URLSearchParams(location.search);
    var hash = (location.hash || "").replace("#", "");
    var tab = tabFromUrl();
    showTab(tab);
    applyPayFilter(params.get("filter") === "payments");
    if (HASH_OPEN[hash]) {
      if (hash === "add-stb") showTab("connections");
      openOverlay(HASH_OPEN[hash]);
    }
    if (opts && opts.migrate && !params.get("tab") && !HASH_OPEN[hash] && history.replaceState) {
      writeTabUrl(tab, params.get("filter") === "payments" ? "payments" : "", false);
    }
  }

  page.querySelectorAll(".cd-tabs button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var name = showTab(btn.getAttribute("data-tab"));
      writeTabUrl(name, "", true);
      applyPayFilter(false);
    });
  });

  page.querySelectorAll("[data-goto-tab]").forEach(function (el) {
    el.addEventListener("click", function () {
      var name = showTab(el.getAttribute("data-goto-tab"));
      var filter = el.getAttribute("data-filter") || "";
      writeTabUrl(name, filter, true);
      applyPayFilter(filter === "payments");
      var scroll = el.getAttribute("data-scroll");
      var target = scroll ? document.querySelector(scroll) : page.querySelector(".cd-tabs-card");
      if (target && target.scrollIntoView) {
        target.scrollIntoView({ behavior: "smooth", block: scroll ? "center" : "start" });
      }
    });
  });

  applyFromUrl({ migrate: true });
  window.addEventListener("hashchange", function () { applyFromUrl(); });
  window.addEventListener("popstate", function () { applyFromUrl(); });

  function currentTabPath(base) {
    try {
      var url = new URL(base || location.pathname, location.origin);
      if (url.pathname.indexOf("/customers/") !== 0) return base || location.pathname;
      var params = new URLSearchParams(location.search);
      var tab = params.get("tab") || tabFromUrl();
      if (tab) url.searchParams.set("tab", tab);
      if (params.get("filter")) url.searchParams.set("filter", params.get("filter"));
      return url.pathname + url.search;
    } catch (err) {
      return base || location.pathname;
    }
  }

  function markRefreshLoading() {
    var card = document.querySelector(".cd-status-card");
    if (card) card.classList.add("is-loading");
    var kpis = document.querySelector(".cd-kpis");
    if (kpis) kpis.classList.add("is-loading");
    var tabs = document.querySelector(".cd-tabs-card");
    if (tabs) tabs.classList.add("is-loading");
    document.querySelectorAll(".cd-refresh-form .cd-icon-btn").forEach(function (b) {
      b.classList.add("is-loading");
      b.disabled = true;
    });
  }

  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      var action = form.getAttribute("action") || "";
      var next = form.querySelector("input[name=next]");
      if (next && next.value) {
        next.value = currentTabPath(next.value);
      } else if (action.indexOf("/check-all") !== -1) {
        var hidden = form.querySelector("input[name=next]");
        if (!hidden) {
          hidden = document.createElement("input");
          hidden.type = "hidden";
          hidden.name = "next";
          form.appendChild(hidden);
        }
        hidden.value = currentTabPath(location.pathname);
      }
      var btn = e.submitter || form.querySelector("button[type=submit]");
      if (btn) {
        btn.classList.add("is-loading");
        btn.disabled = true;
      }
      if (form.classList.contains("cd-refresh-form") || action.indexOf("/check-all") !== -1) {
        markRefreshLoading();
      }
    });
  });

  var pasteWrap = document.getElementById("cd-paste-wrap");
  document.querySelectorAll(".cd-paste-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (!pasteWrap) return;
      var on = pasteWrap.style.display !== "none";
      pasteWrap.style.display = on ? "none" : "";
      if (!on) {
        var input = pasteWrap.querySelector("input");
        if (input) input.focus();
      }
    });
  });

  document.addEventListener("click", function (e) {
    var copyBtn = e.target.closest("[data-copy]");
    if (!copyBtn) return;
    e.preventDefault();
    var text = copyBtn.getAttribute("data-copy") || "";
    function copied() {
      copyBtn.classList.add("is-copied");
      copyBtn.setAttribute("title", "Copied");
      setTimeout(function () {
        copyBtn.classList.remove("is-copied");
        copyBtn.setAttribute("title", "Copy");
      }, 1400);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(copied).catch(function () {
        window.prompt("Copy this ID", text);
      });
    } else {
      window.prompt("Copy this ID", text);
    }
  });

  (function relativeTimes() {
    function fmtRel(raw) {
      if (!raw) return "";
      var parsed = Date.parse(String(raw).replace(" ", "T"));
      if (!isFinite(parsed)) return "";
      var secs = Math.round((Date.now() - parsed) / 1000);
      var future = secs < 0;
      secs = Math.abs(secs);
      var text;
      if (secs < 45) return future ? "in a moment" : "just now";
      if (secs < 3600) {
        var m = Math.max(1, Math.floor(secs / 60));
        text = m + " min";
      } else if (secs < 86400) {
        var h = Math.floor(secs / 3600);
        text = h + " hour" + (h === 1 ? "" : "s");
      } else {
        var d = Math.floor(secs / 86400);
        if (d >= 30) return "";
        text = d + " day" + (d === 1 ? "" : "s");
      }
      return future ? "in " + text : text + " ago";
    }
    document.querySelectorAll("[data-rel]").forEach(function (el) {
      var raw = el.getAttribute("data-rel");
      var text = fmtRel(raw);
      if (text) {
        var prefix = el.getAttribute("data-rel-prefix") || "";
        el.textContent = prefix + text;
      }
      if (!el.getAttribute("data-rel-fresh")) return;
      var parsed = Date.parse(String(raw || "").replace(" ", "T"));
      if (!isFinite(parsed)) return;
      var age = Date.now() - parsed;
      var wrap = el.closest(".cd-status-age") || el;
      wrap.classList.remove("is-stale-warn", "is-stale-bad");
      if (age > 7 * 86400000) wrap.classList.add("is-stale-bad");
      else if (age > 86400000) wrap.classList.add("is-stale-warn");
    });
  })();

  window.saveCustomerGps = function saveCustomerGps() {
    if (!navigator.geolocation) {
      alert("This phone cannot read GPS. Paste a Google Maps pin instead.");
      return;
    }
    var btn = document.getElementById("cd-gps-btn");
    if (btn) {
      btn.disabled = true;
      btn.classList.add("is-loading");
    }
    navigator.geolocation.getCurrentPosition(function (pos) {
      document.getElementById("geo-lat").value = pos.coords.latitude.toFixed(6);
      document.getElementById("geo-lng").value = pos.coords.longitude.toFixed(6);
      document.getElementById("geo-acc").value = pos.coords.accuracy || "";
      document.getElementById("geo-form").submit();
    }, function () {
      if (btn) {
        btn.disabled = false;
        btn.classList.remove("is-loading");
      }
      alert("Could not read GPS on this connection. Stand at the house, allow location, or paste a Google Maps link.");
    }, { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
  };

  (function collectDrawer() {
    var form = document.getElementById("collect-form");
    if (!form) return;
    var dataEl = document.getElementById("cd-collect-data");
    var data = { quotes: {}, netDue: 0, usual: 0, breakdown: [] };
    try {
      if (dataEl) data = JSON.parse(dataEl.textContent || "{}");
    } catch (err) {}
    var quotes = data.quotes || {};
    var netDue = Number(data.netDue || 0);
    var usual = Number(data.usual || 0);
    var breakdown = data.breakdown || [];
    var amount = form.querySelector("[name=amount]");
    var amountField = document.getElementById("collect-amount-field");
    var conn = document.getElementById("collect-connection");
    var renewRow = document.getElementById("collect-renew-row");
    var waRow = document.getElementById("collect-wa-row");
    var refRow = document.getElementById("collect-ref-row");
    var dateRow = document.getElementById("collect-date-row");
    var submit = document.getElementById("collect-submit");
    var hint = document.getElementById("collect-hint");
    var renewBox = document.getElementById("renew");
    var cashHint = hint ? hint.textContent : "";
    var coverDays = data.coverDays;
    var chipState = netDue > 0 ? "due" : "usual";

    function rupees(paise) {
      return String(Math.ceil(Number(paise || 0) / 100));
    }
    function modeValue() {
      var r = form.querySelector("[name=mode]:checked");
      return r ? r.value : "cash";
    }
    function formatInr(paise) {
      var n = Math.ceil(Number(paise || 0) / 100);
      return n.toLocaleString("en-IN", { maximumFractionDigits: 0 });
    }
    function currentPaise() {
      if (!amount) return 0;
      var raw = String(amount.value || "").replace(/,/g, "");
      var n = parseFloat(raw);
      if (!isFinite(n)) return 0;
      return Math.round(n * 100);
    }
    function updateSubmit() {
      if (!submit) return;
      if (modeValue() === "collect_later") {
        submit.textContent = "Renew and collect later";
        return;
      }
      submit.textContent = "Collect ₹" + formatInr(currentPaise());
    }
    function updateHint() {
      if (!hint) return;
      if (modeValue() === "collect_later") {
        hint.textContent = "Service will be renewed. They stay on follow-up until you collect.";
        return;
      }
      var paid = currentPaise();
      var left = netDue - paid;
      var parts = [];
      if (netDue > 0) {
        if (paid <= 0) {
          parts.push("Due now ₹" + rupees(netDue) + ".");
        } else if (left > 0) {
          parts.push("This leaves ₹" + rupees(left) + " still due.");
        } else if (left === 0) {
          parts.push("This clears the due.");
        } else {
          parts.push("This clears the due. Extra ₹" + rupees(-left) + " stays as advance.");
        }
      } else if (paid > 0) {
        parts.push("Nothing outstanding. This collects for the next cycle.");
      }
      if (renewBox && renewBox.checked) {
        parts.push("Portal renewal will be queued.");
      } else if (coverDays !== null && coverDays !== undefined && coverDays !== "" && Number(coverDays) >= 0) {
        parts.push("Cover is still active — leave renewal off unless they need a recharge.");
      }
      hint.textContent = parts.join(" ") || cashHint.trim();
    }
    function markChip(name) {
      chipState = name;
      form.querySelectorAll("[data-fill]").forEach(function (chip) {
        chip.classList.toggle("on", chip.getAttribute("data-fill") === name);
      });
    }
    function applyQuote() {
      if (!amount) return;
      var later = modeValue() === "collect_later";
      if (later) {
        updateSubmit();
        updateHint();
        return;
      }
      if (chipState === "custom") {
        updateSubmit();
        updateHint();
        return;
      }
      if (chipState === "due" && netDue > 0) {
        amount.value = rupees(netDue);
        updateSubmit();
        updateHint();
        return;
      }
      if (chipState === "usual" && usual > 0) {
        amount.value = rupees(usual);
        updateSubmit();
        updateHint();
        return;
      }
      var q = conn && quotes ? quotes[conn.value] : null;
      if (conn && conn.value && q && q.total && netDue <= 0) {
        amount.value = rupees(q.total);
        if (hint) {
          if (q.exclusive && q.gst) {
            hint.textContent = "This connection: plan ₹" + rupees(q.base) + " + " + q.rate + "% GST = ₹" + rupees(q.total) + ".";
          } else {
            hint.textContent = "This connection: ₹" + rupees(q.total) + ".";
          }
        }
      }
      updateSubmit();
      updateHint();
    }
    function syncMode() {
      var later = modeValue() === "collect_later";
      var cash = modeValue() === "cash";
      if (amount) amount.required = !later;
      if (amountField) amountField.style.display = later ? "none" : "";
      if (renewRow) renewRow.style.display = later ? "none" : "";
      if (waRow) waRow.style.display = later ? "none" : "";
      if (refRow) refRow.style.display = (!later && !cash) ? "" : "none";
      if (dateRow) dateRow.style.display = later ? "none" : "";
      if (conn) {
        var empty = conn.querySelector('option[value=""]');
        if (empty) empty.textContent = later ? "— pick the connection to renew —" : "— not tied to one connection —";
        if (later && !conn.value) {
          var usable = [].filter.call(conn.options, function (o) { return o.value; });
          if (usable.length === 1) conn.value = usable[0].value;
        }
      }
      applyQuote();
    }

    form.querySelectorAll("[name=mode]").forEach(function (el) {
      el.addEventListener("change", syncMode);
    });
    if (conn) conn.addEventListener("change", function () {
      if (chipState !== "custom" && chipState !== "due") chipState = "usual";
      applyQuote();
    });
    if (amount) {
      amount.addEventListener("input", function () {
        markChip("custom");
        updateSubmit();
        updateHint();
      });
    }
    if (renewBox) {
      renewBox.addEventListener("change", updateHint);
    }
    form.querySelectorAll("[data-fill]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        markChip(chip.getAttribute("data-fill"));
        if (chipState === "custom" && amount) amount.focus();
        applyQuote();
      });
    });

    var params = new URLSearchParams(location.search);
    var connId = params.get("connection_id");
    if (connId && conn) {
      conn.value = connId;
    } else if (conn && !conn.value) {
      var only = [].filter.call(conn.options, function (o) { return o.value; });
      if (only.length === 1) conn.value = only[0].value;
    }
    markChip(netDue > 0 ? "due" : "usual");
    syncMode();
  })();

  (function addConnection() {
    var sel = document.getElementById("new-conn-provider");
    var id = document.getElementById("new-conn-id");
    var label = document.getElementById("new-conn-id-label");
    var cardWrap = document.getElementById("new-conn-card-wrap");
    var plan = document.getElementById("new-conn-plan");
    var planSel = document.getElementById("new-conn-plan-select");
    var plansByProvider = {};
    var jsonEl = document.getElementById("plan-packages-json");
    try {
      plansByProvider = JSON.parse((jsonEl && jsonEl.textContent) || "{}");
    } catch (err) { plansByProvider = {}; }
    if (!sel || !id) return;
    function rebuildPlanSelect(p) {
      if (!planSel) return;
      planSel.innerHTML = '<option value="">— pick from catalog —</option>';
      (plansByProvider[p] || []).forEach(function (name) {
        var o = document.createElement("option");
        o.value = name;
        o.textContent = name;
        planSel.appendChild(o);
      });
      planSel.innerHTML += '<option value="__other__">Other (type below)…</option>';
    }
    function sync() {
      var p = sel.value;
      if (plan) plan.setAttribute("list", "plans-" + p);
      rebuildPlanSelect(p);
      if (p === "iptv" || p === "ott") {
        label.textContent = p === "ott" ? "OTT phone" : "IPTV phone";
        id.placeholder = p === "ott" ? "10-digit mobile on SmartPlay" : "10-digit mobile on ANT";
        if (!id.value && id.getAttribute("data-phone")) id.value = id.getAttribute("data-phone");
        if (cardWrap) cardWrap.style.display = "none";
      } else if (p === "railtel") {
        label.textContent = "Railtel login";
        id.placeholder = "ka.username";
        if (/^\d{10}$/.test((id.value || "").replace(/\D/g, "")) && (id.value || "").replace(/\D/g, "").length === 10) {
          id.value = "";
        }
        if (cardWrap) cardWrap.style.display = "";
      } else {
        label.textContent = "STB / login";
        id.placeholder = "N70130838231";
        if (cardWrap) cardWrap.style.display = "";
      }
    }
    if (planSel && plan) {
      planSel.addEventListener("change", function () {
        if (planSel.value === "__other__") {
          plan.value = "";
          plan.focus();
        } else if (planSel.value) {
          plan.value = planSel.value;
        }
      });
    }
    sel.addEventListener("change", sync);
    sync();
  })();
})();
