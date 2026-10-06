(function () {
  function copyText(text, btn) {
    if (!text) return;
    var done = function () {
      if (!btn) return;
      var prev = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () {
        btn.textContent = prev;
      }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () {
        fallbackCopy(text);
        done();
      });
    } else {
      fallbackCopy(text);
      done();
    }
  }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } catch (e) {}
    document.body.removeChild(ta);
  }

  function initQr(canvasId, uri, fallbackImgId) {
    var canvas = document.getElementById(canvasId);
    if (!canvas || !uri) return;
    var size = Math.min(240, Math.max(180, window.innerWidth - 120));
    if (typeof QRCode !== "undefined") {
      QRCode.toCanvas(
        canvas,
        uri,
        {
          width: size,
          margin: 1,
          color: { dark: "#0a0a0a", light: "#ffffff" },
        },
        function (err) {
          if (err && fallbackImgId) {
            var img = document.getElementById(fallbackImgId);
            if (img) img.hidden = false;
            canvas.hidden = true;
          }
        }
      );
    } else if (fallbackImgId) {
      var imgEl = document.getElementById(fallbackImgId);
      if (imgEl) imgEl.hidden = false;
      canvas.hidden = true;
    }
  }

  function digitsPhone(raw) {
    var d = String(raw || "").replace(/\D/g, "");
    if (d.indexOf("00") === 0) d = d.slice(2);
    if (d.indexOf("91") === 0 && d.length >= 12) d = d.slice(-10);
    else if (d.charAt(0) === "0" && d.length === 11) d = d.slice(1);
    if (d.length > 10) d = d.slice(-10);
    return d;
  }

  function bindLookupFields() {
    var phone = document.getElementById("phone");
    if (phone) {
      function tidyPhone() {
        var n = digitsPhone(phone.value);
        if (n) phone.value = n;
      }
      phone.addEventListener("paste", function () {
        setTimeout(tidyPhone, 0);
      });
      phone.addEventListener("blur", tidyPhone);
      if (phone.form) phone.form.addEventListener("submit", tidyPhone);
    }
    var sid = document.getElementById("service_id");
    if (sid) {
      sid.setAttribute("autocapitalize", "none");
      sid.setAttribute("autocorrect", "off");
      sid.setAttribute("spellcheck", "false");
      sid.addEventListener("input", function () {
        var v = sid.value;
        var next = v.replace(/\. +/g, ".").replace(/ /g, "");
        if (next === v) return;
        var pos = sid.selectionStart;
        var delta = v.length - next.length;
        sid.value = next;
        if (typeof pos === "number") {
          sid.setSelectionRange(Math.max(0, pos - delta), Math.max(0, pos - delta));
        }
      });
    }
  }

  document.addEventListener("click", function (ev) {
    var t = ev.target;
    if (t && t.matches("[data-copy]")) {
      ev.preventDefault();
      copyText(t.getAttribute("data-copy") || "", t);
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    bindLookupFields();
    var root = document.getElementById("pay-qr-root");
    if (root) {
      initQr(
        root.getAttribute("data-canvas") || "pay-qr-canvas",
        root.getAttribute("data-uri") || "",
        root.getAttribute("data-fallback") || "pay-qr-fallback"
      );
    }
  });

  window.vkPayCopy = copyText;
  window.vkPayInitQr = initQr;
})();
