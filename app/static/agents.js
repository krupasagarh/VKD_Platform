(function () {
  var page = document.querySelector(".ag-page");
  if (!page) return;

  var OPEN_KEY = "vk_ag_panel";

  var saving = false;

  function overlays() {
    return document.querySelectorAll(".ag-overlay");
  }

  function openOverlay(id, opts) {
    opts = opts || {};
    var el = document.getElementById(id);
    if (!el) return;
    closeMenus();
    overlays().forEach(function (other) {
      if (other !== el) other.classList.remove("on");
    });
    el.hidden = false;
    el.classList.add("on");
    document.body.style.overflow = "hidden";
    if (opts.resetPw) revealPassword(el);
    var focus = el.querySelector(".ag-pw input, input[name=name], button.btn-primary");
    if (focus) setTimeout(function () { focus.focus(); }, 30);
  }

  function overlayDirty(el) {
    if (!el) return false;
    if (el.querySelector(".js-track.is-dirty")) return true;
    var head = el.querySelector(".js-head-active");
    if (head && head.getAttribute("data-orig") === "1" && !head.checked) return true;
    if (head && head.getAttribute("data-orig") !== "1" && head.checked) return true;
    return false;
  }

  function closeOverlay(el, force) {
    if (!el || !el.classList.contains("on")) return;
    if (!force && overlayDirty(el)) {
      if (!confirm("You have unsaved changes. Close anyway?")) return;
    }
    el.classList.remove("on");
    el.hidden = true;
    if (!document.querySelector(".ag-overlay.on")) {
      document.body.style.overflow = "";
    }
  }

  function closeMenus() {
    document.querySelectorAll(".ag-menu.open").forEach(function (menu) {
      menu.classList.remove("open");
      menu.classList.remove("open-up");
      var list = menu.querySelector(".ag-menu-list");
      if (list) list.hidden = true;
    });
  }

  function placeMenu(menu) {
    var btn = menu.querySelector(".ag-menu-btn");
    var list = menu.querySelector(".ag-menu-list");
    if (!btn || !list) return;
    menu.classList.remove("open-up");
    list.hidden = false;
    var row = menu.closest("tr");
    var lastRow = row && row.parentNode && row === row.parentNode.lastElementChild;
    var btnRect = btn.getBoundingClientRect();
    var need = (list.offsetHeight || 96) + 8;
    if (lastRow || btnRect.bottom + need > window.innerHeight) {
      menu.classList.add("open-up");
    }
  }

  function revealPassword(overlay) {
    var wrap = overlay.querySelector(".ag-pw");
    if (!wrap) return;
    wrap.hidden = false;
    var input = wrap.querySelector("input");
    if (input) input.focus();
    var card = wrap.closest(".js-track");
    if (card) syncDirty(card);
  }

  function sectionState(card) {
    var parts = [];
    card.querySelectorAll("input, select, textarea").forEach(function (el) {
      if (el.type === "hidden") return;
      if (el.disabled) return;
      if (el.type === "checkbox" || el.type === "radio") {
        parts.push((el.name || el.className) + "=" + (el.checked ? el.value || "1" : ""));
      } else {
        parts.push((el.name || el.className) + "=" + (el.value || ""));
      }
    });
    var overlay = card.closest(".ag-overlay");
    if (overlay && card.closest('[data-section="details"], .ag-new-form')) {
      var head = overlay.querySelector(".js-head-active");
      if (head) parts.push("head-active=" + (head.checked ? "1" : "0"));
    }
    return parts.join("&");
  }

  function snapshot(card) {
    card.setAttribute("data-snap", sectionState(card));
    card.classList.remove("is-dirty");
    var save = card.querySelector(".js-save");
    if (save) save.disabled = true;
  }

  function syncDirty(card) {
    var snap = card.getAttribute("data-snap");
    if (snap == null) return;
    var dirty = sectionState(card) !== snap;
    card.classList.toggle("is-dirty", dirty);
    var save = card.querySelector(".js-save");
    if (save) save.disabled = !dirty;
  }

  function permCount(card) {
    var boxes = card.querySelectorAll('input[name="perm"]');
    if (!boxes.length) return;
    var c = 0;
    boxes.forEach(function (b) { if (b.checked) c += 1; });
    var el = card.querySelector(".js-perm-count");
    if (el) el.textContent = c + " of " + boxes.length + " allowed";
    var note = card.querySelector(".js-admin-note");
    var admin = card.querySelector(".js-admin-perm");
    if (note) note.hidden = !(admin && admin.checked);
  }

  function areaCount(card) {
    var boxes = card.querySelectorAll('input[name="area"]');
    var el = card.querySelector(".js-area-count");
    if (!el || !boxes.length) return;
    var c = 0;
    boxes.forEach(function (b) { if (b.checked) c += 1; });
    el.textContent = c + " of " + boxes.length + " selected";
    var all = card.querySelector(".js-all-areas");
    var list = card.querySelector(".js-area-list");
    if (all) {
      all.checked = c === boxes.length && boxes.length > 0;
      all.indeterminate = c > 0 && c < boxes.length;
    }
    if (list && all) list.classList.toggle("is-locked", all.checked && !all.indeterminate);
  }

  function setAreas(card, checked) {
    card.querySelectorAll('input[name="area"]').forEach(function (b) {
      b.checked = checked;
    });
    areaCount(card);
    syncDirty(card);
  }

  function syncActiveCopies(overlay, form) {
    if (!overlay || !form) return;
    var head = overlay.querySelector(".js-head-active");
    if (!head) return;
    form.querySelectorAll('input[name="active"]').forEach(function (el) {
      if (el.classList.contains("js-head-active")) return;
      if (el.disabled) return;
      el.remove();
    });
    if (head.disabled) {
      var keep = document.createElement("input");
      keep.type = "hidden";
      keep.name = "active";
      keep.value = "1";
      form.appendChild(keep);
      return;
    }
    if (head.checked) {
      var hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "active";
      hidden.value = "1";
      hidden.className = "js-active-copy";
      form.appendChild(hidden);
    }
  }

  function bindCard(card) {
    snapshot(card);
    permCount(card);
    areaCount(card);
    card.addEventListener("input", function () {
      permCount(card);
      areaCount(card);
      syncDirty(card);
    });
    card.addEventListener("change", function () {
      permCount(card);
      areaCount(card);
      syncDirty(card);
    });
  }

  document.querySelectorAll(".js-head-active").forEach(function (el) {
    el.setAttribute("data-orig", el.checked ? "1" : "0");
  });

  document.querySelectorAll(".js-track").forEach(bindCard);

  page.addEventListener("click", function (e) {
    var stop = e.target.closest("[data-stop-open]");
    var menuBtn = e.target.closest(".ag-menu-btn");
    if (menuBtn) {
      e.preventDefault();
      e.stopPropagation();
      var menu = menuBtn.closest(".ag-menu");
      var open = menu.classList.contains("open");
      closeMenus();
      if (!open) {
        menu.classList.add("open");
        placeMenu(menu);
      }
      return;
    }

    var openBtn = e.target.closest("[data-open]");
    if (openBtn) {
      if (e.target.closest("a[href^='tel']")) return;
      if (e.target.closest("[data-stop-open]")) return;
      e.preventDefault();
      openOverlay(openBtn.getAttribute("data-open"), {
        resetPw: openBtn.hasAttribute("data-reset-pw"),
      });
      return;
    }

    var closeBtn = e.target.closest("[data-close]");
    if (closeBtn) {
      e.preventDefault();
      closeOverlay(closeBtn.closest(".ag-overlay"));
      return;
    }

    var groupAll = e.target.closest(".js-group-all");
    if (groupAll) {
      e.preventDefault();
      var group = groupAll.closest(".ag-perm-group");
      if (group) {
        group.querySelectorAll('input[name="perm"]').forEach(function (b) { b.checked = true; });
        var card = groupAll.closest(".js-track") || groupAll.closest(".ag-card");
        permCount(card);
        if (card && card.classList.contains("js-track")) syncDirty(card);
      }
      return;
    }

    var areasAll = e.target.closest(".js-areas-all");
    if (areasAll) {
      e.preventDefault();
      var cardAll = areasAll.closest(".ag-card");
      var visible = cardAll.querySelectorAll('.js-area-list .check:not([hidden]) input[name="area"]');
      visible.forEach(function (b) { b.checked = true; });
      areaCount(cardAll);
      if (cardAll.classList.contains("js-track")) syncDirty(cardAll);
      return;
    }

    var areasClear = e.target.closest(".js-areas-clear");
    if (areasClear) {
      e.preventDefault();
      var cardClear = areasClear.closest(".ag-card");
      var allToggle = cardClear.querySelector(".js-all-areas");
      if (allToggle) allToggle.checked = false;
      var list = cardClear.querySelector(".js-area-list");
      if (list) list.classList.remove("is-locked");
      var visibleClear = cardClear.querySelectorAll('.js-area-list .check:not([hidden]) input[name="area"]');
      visibleClear.forEach(function (b) { b.checked = false; });
      areaCount(cardClear);
      if (cardClear.classList.contains("js-track")) syncDirty(cardClear);
      return;
    }

    var resetPw = e.target.closest(".js-reset-pw");
    if (resetPw) {
      e.preventDefault();
      revealPassword(resetPw.closest(".ag-overlay"));
      return;
    }

    if (!stop) closeMenus();
  });

  document.addEventListener("click", function (e) {
    if (!e.target.closest(".ag-menu")) closeMenus();
  });

  overlays().forEach(function (el) {
    el.addEventListener("click", function (e) {
      if (e.target === el) closeOverlay(el);
    });
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var open = document.querySelector(".ag-overlay.on");
    if (open) closeOverlay(open);
  });

  page.addEventListener("change", function (e) {
    var allAreas = e.target.closest(".js-all-areas");
    if (allAreas) {
      var card = allAreas.closest(".ag-card");
      var list = card.querySelector(".js-area-list");
      if (allAreas.checked) {
        setAreas(card, true);
        if (list) list.classList.add("is-locked");
      } else {
        if (list) list.classList.remove("is-locked");
        areaCount(card);
        if (card.classList.contains("js-track")) syncDirty(card);
      }
    }

    var head = e.target.closest(".js-head-active");
    if (head) {
      var label = head.parentNode.querySelector("span");
      if (label) label.textContent = head.checked ? "Active" : "Disabled";
      var overlay = head.closest(".ag-overlay");
      var details = overlay && overlay.querySelector('[data-section="details"] .js-track');
      if (details) syncDirty(details);
    }

    var role = e.target.closest(".js-role");
    if (role) {
      var overlay = role.closest(".ag-overlay");
      var chip = overlay && overlay.querySelector(".js-role-chip");
      if (chip) chip.textContent = role.value === "admin" ? "Admin" : "Collector";
    }
  });

  page.addEventListener("input", function (e) {
    var searchAreas = e.target.closest(".js-area-search");
    if (searchAreas) {
      var q = (searchAreas.value || "").trim().toLowerCase();
      var list = searchAreas.closest(".ag-card").querySelector(".js-area-list");
      if (list) {
        list.querySelectorAll(".check[data-area]").forEach(function (row) {
          var match = !q || (row.getAttribute("data-area") || "").indexOf(q) !== -1;
          row.hidden = !match;
        });
      }
    }

    var searchStaff = e.target.closest(".js-ag-search");
    if (searchStaff) {
      var needle = (searchStaff.value || "").trim().toLowerCase();
      function matchRow(node) {
        if (!needle) return true;
        return (node.getAttribute("data-name") || "").indexOf(needle) !== -1
          || (node.getAttribute("data-username") || "").indexOf(needle) !== -1
          || (node.getAttribute("data-phone") || "").indexOf(needle) !== -1
          || (node.getAttribute("data-role") || "").indexOf(needle) !== -1;
      }
      page.querySelectorAll(".ag-row").forEach(function (row) {
        row.hidden = !matchRow(row);
      });
      page.querySelectorAll(".ag-mcard").forEach(function (card) {
        card.hidden = !matchRow(card);
      });
    }
  });

  page.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.classList.contains("ag-form") && !form.classList.contains("ag-new-form") && !form.classList.contains("ag-status-form")) {
      return;
    }
    var overlay = form.closest(".ag-overlay");
    if (overlay) {
      syncActiveCopies(overlay, form);
      try { sessionStorage.setItem(OPEN_KEY, overlay.id); } catch (err) {}
    }
    var track = form.querySelector(".js-track");
    if (track) track.classList.remove("is-dirty");
    saving = true;
    var btn = form.querySelector(".js-save, .ag-create-foot .btn, button[type=submit]");
    if (btn && !form.classList.contains("ag-status-form")) {
      btn.disabled = false;
      btn.classList.add("is-loading");
      if (btn.classList.contains("js-save") || form.classList.contains("ag-new-form")) {
        btn.textContent = form.classList.contains("ag-new-form") ? "Creating…" : "Saving…";
      }
    }
  });

  window.addEventListener("beforeunload", function (e) {
    if (saving) return;
    var open = document.querySelector(".ag-overlay.on");
    if (open && overlayDirty(open)) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  try {
    var reopen = sessionStorage.getItem(OPEN_KEY);
    if (reopen) {
      sessionStorage.removeItem(OPEN_KEY);
      openOverlay(reopen);
    }
  } catch (err) {}
})();
