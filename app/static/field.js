(function () {
  function postPing(lat, lng, accuracy, source) {
    var body = new URLSearchParams();
    body.set("lat", lat);
    body.set("lng", lng);
    if (accuracy) body.set("accuracy", accuracy);
    body.set("source", source || "ping");
    return fetch("/field/ping", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
      credentials: "same-origin",
    });
  }

  function fillForm(form, pos) {
    var lat = form.querySelector("[name=lat]");
    var lng = form.querySelector("[name=lng]");
    var acc = form.querySelector("[name=accuracy]");
    if (lat) lat.value = pos.coords.latitude.toFixed(6);
    if (lng) lng.value = pos.coords.longitude.toFixed(6);
    if (acc) acc.value = pos.coords.accuracy || "";
  }

  function watchForms() {
    document.querySelectorAll("form.js-geo").forEach(function (form) {
      if (!form.querySelector("[name=lat]")) {
        ["lat", "lng", "accuracy"].forEach(function (name) {
          var input = document.createElement("input");
          input.type = "hidden";
          input.name = name;
          form.appendChild(input);
        });
      }
      if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(
          function (pos) { fillForm(form, pos); },
          function () {},
          { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 }
        );
      }
      form.addEventListener("submit", function () {
        if (!navigator.geolocation) return;
        var lat = form.querySelector("[name=lat]");
        if (lat && lat.value) return;
        navigator.geolocation.getCurrentPosition(
          function (pos) { fillForm(form, pos); },
          function () {},
          { enableHighAccuracy: true, timeout: 4000, maximumAge: 30000 }
        );
      });
    });
  }

  function dutyState() {
    return document.body.getAttribute("data-field-duty") || "";
  }

  function showDutyPrompt() {
    var existing = document.getElementById("field-duty-prompt");
    if (existing) {
      existing.hidden = false;
      return existing;
    }
    var box = document.createElement("div");
    box.id = "field-duty-prompt";
    box.className = "field-duty-prompt";
    box.innerHTML =
      "<div class=\"field-duty-prompt-card\">" +
      "<strong>Field login is off</strong>" +
      "<p>Turn it on first.</p>" +
      "<form class=\"js-geo field-duty-form\" method=\"post\" action=\"/field/duty\">" +
      "<input type=\"hidden\" name=\"on\" value=\"1\">" +
      "<input type=\"hidden\" name=\"next\" value=\"" + (location.pathname + location.search).replace(/"/g, "") + "\">" +
      "<button class=\"btn btn-primary\" type=\"submit\">Turn on</button>" +
      "<button class=\"btn\" type=\"button\" data-close-duty>Not now</button>" +
      "</form></div>";
    document.body.appendChild(box);
    box.addEventListener("click", function (event) {
      if (event.target === box || event.target.getAttribute("data-close-duty") !== null) {
        box.hidden = true;
      }
    });
    watchForms();
    return box;
  }

  var DUTY_FREE_HREF = /^\/(logout|v2\/switch|v2\/classic)(\/|\?|$)/;
  var ACTION_SELECTOR = "a, button, summary, label, select, input, textarea, [role=button], [onclick]";

  function isDutyFree(el) {
    if (el.closest(".field-duty-form, #field-duty-prompt")) return true;
    if (el.tagName === "A") {
      var href = el.getAttribute("href") || "";
      if (DUTY_FREE_HREF.test(href)) return true;
    }
    return false;
  }

  function guardWorkForms() {
    if (dutyState() !== "off") return;
    document.addEventListener("click", function (event) {
      var el = event.target.closest && event.target.closest(ACTION_SELECTOR);
      if (!el || isDutyFree(el)) return;
      event.preventDefault();
      event.stopPropagation();
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
      showDutyPrompt();
    }, true);
    document.addEventListener("focusin", function (event) {
      var el = event.target;
      if (!el.matches || !el.matches("input, select, textarea") || isDutyFree(el)) return;
      el.blur();
    }, true);
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form || form.tagName !== "FORM") return;
      if (form.classList.contains("field-duty-form")) return;
      event.preventDefault();
      showDutyPrompt();
    }, true);
  }

  function pingLoop() {
    if (dutyState() === "off") return;
    if (!navigator.geolocation) return;
    function send() {
      navigator.geolocation.getCurrentPosition(
        function (pos) {
          postPing(
            pos.coords.latitude.toFixed(6),
            pos.coords.longitude.toFixed(6),
            pos.coords.accuracy || "",
            "ping"
          );
        },
        function () {},
        { enableHighAccuracy: true, timeout: 20000, maximumAge: 60000 }
      );
    }
    send();
    setInterval(send, 180000);
  }

  function copyFallback(text) {
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy");
        resolve();
      } catch (err) {
        reject(err);
      } finally {
        document.body.removeChild(area);
      }
    });
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () {
        return copyFallback(text);
      });
    }
    return copyFallback(text);
  }

  function bindCopyIds() {
    document.addEventListener("click", function (event) {
      var btn = event.target.closest(".copy-id");
      if (!btn) return;
      event.preventDefault();
      var value = (btn.getAttribute("data-copy") || btn.textContent || "").trim();
      if (!value) return;
      copyText(value).then(function () {
        var previous = btn.textContent;
        btn.classList.add("copied");
        btn.textContent = "Copied";
        setTimeout(function () {
          btn.classList.remove("copied");
          btn.textContent = previous;
        }, 1200);
      }).catch(function () {});
    });
  }

  function bindCustomerGpsButtons() {
    document.addEventListener("click", function (event) {
      var btn = event.target.closest(".vk-gps-btn");
      if (!btn) return;
      event.preventDefault();
      var customerId = btn.getAttribute("data-customer-id");
      if (!customerId) return;
      if (!navigator.geolocation) {
        alert("This phone cannot read GPS. Open Tag on map, or save the pin from the customer page.");
        return;
      }
      btn.disabled = true;
      var previous = btn.textContent;
      btn.textContent = "Reading…";
      navigator.geolocation.getCurrentPosition(
        function (pos) {
          var form = document.createElement("form");
          form.method = "post";
          form.action = "/customers/" + customerId + "/location";
          form.style.display = "none";
          ["lat", "lng", "accuracy"].forEach(function (name) {
            var input = document.createElement("input");
            input.type = "hidden";
            input.name = name;
            form.appendChild(input);
          });
          form.querySelector("[name=lat]").value = pos.coords.latitude.toFixed(6);
          form.querySelector("[name=lng]").value = pos.coords.longitude.toFixed(6);
          form.querySelector("[name=accuracy]").value = pos.coords.accuracy || "";
          var next = btn.getAttribute("data-next");
          if (next) {
            var nextInput = document.createElement("input");
            nextInput.type = "hidden";
            nextInput.name = "next";
            nextInput.value = next;
            form.appendChild(nextInput);
          }
          document.body.appendChild(form);
          form.submit();
        },
        function () {
          btn.disabled = false;
          btn.textContent = previous;
          alert("Could not read GPS. Stand at the house, allow location, or use Tag on map.");
        },
        { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 }
      );
    });
  }

  window.vkShareGps = function (formId) {
    var form = document.getElementById(formId);
    if (!form) return;
    if (!navigator.geolocation) {
      alert("This phone cannot read GPS. Paste a Google Maps pin instead.");
      return;
    }
    navigator.geolocation.getCurrentPosition(
      function (pos) {
        fillForm(form, pos);
        form.submit();
      },
      function () {
        alert("Could not read GPS. Allow location, or paste a Google Maps link.");
      },
      { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 }
    );
  };

  function bindDetailsDismiss() {
    var selectors = ["details.wa-menu", "details.area-setter"];
    function closeOpen(except) {
      selectors.forEach(function (sel) {
        document.querySelectorAll(sel + "[open]").forEach(function (details) {
          if (except && details === except) return;
          details.removeAttribute("open");
        });
      });
    }
    function isInsideMenu(details, target) {
      if (!target) return false;
      var list = details.querySelector(".wa-menu-list, .area-setter-form");
      var toggle = details.querySelector("summary");
      if (toggle && (target === toggle || toggle.contains(target))) return true;
      if (list && (target === list || list.contains(target))) return true;
      return false;
    }
    document.addEventListener("pointerdown", function (event) {
      selectors.forEach(function (sel) {
        document.querySelectorAll(sel + "[open]").forEach(function (details) {
          if (!isInsideMenu(details, event.target)) {
            details.removeAttribute("open");
          }
        });
      });
    }, true);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeOpen(null);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      watchForms();
      pingLoop();
      guardWorkForms();
      bindCopyIds();
      bindCustomerGpsButtons();
      bindDetailsDismiss();
    });
  } else {
    watchForms();
    pingLoop();
    guardWorkForms();
    bindCopyIds();
    bindCustomerGpsButtons();
    bindDetailsDismiss();
  }
})();
