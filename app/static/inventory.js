(function () {
  function esc(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function bindUseForm(form) {
    var kind = form.querySelector(".js-inv-kind");
    var custWrap = form.querySelector(".js-inv-cust");
    var q = form.querySelector(".js-cust-q");
    var box = form.querySelector(".js-cust-suggest");
    var meta = form.querySelector(".js-cust-meta");
    var idInput = form.querySelector('input[name="customer_id"]');
    if (!kind || !custWrap) return;

    function showCust(on) {
      custWrap.hidden = !on;
      if (!on && idInput) idInput.value = "";
    }
    showCust(kind.value === "faulty");
    kind.addEventListener("change", function () {
      showCust(kind.value === "faulty");
    });
    if (!q || !box || !idInput) return;

    var timer = 0;
    q.addEventListener("input", function () {
      idInput.value = "";
      if (meta) {
        meta.classList.add("is-empty");
        meta.innerHTML = "<span>Pick a customer from the list</span>";
      }
      clearTimeout(timer);
      var text = q.value.trim();
      if (text.length < 2) {
        box.hidden = true;
        box.innerHTML = "";
        return;
      }
      timer = setTimeout(function () {
        fetch("/inventory/search-customers?q=" + encodeURIComponent(text), { credentials: "same-origin" })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            var list = (data && data.customers) || [];
            if (!list.length) {
              box.innerHTML = "<div class=\"settle-suggest-empty\">No matching customer</div>";
              box.hidden = false;
              return;
            }
            box.innerHTML = list.map(function (item, i) {
              var labels = (item.provider_labels || []).join(" · ") || "—";
              return "<button type=\"button\" class=\"settle-suggest-item\" data-i=\"" + i + "\">"
                + "<strong>" + esc(item.name) + "</strong>"
                + "<span>" + esc(item.area || "No area") + " · " + esc(labels) + "</span>"
                + "</button>";
            }).join("");
            box.hidden = false;
            box.querySelectorAll(".settle-suggest-item").forEach(function (btn) {
              btn.onclick = function () {
                var item = list[parseInt(btn.getAttribute("data-i"), 10)];
                idInput.value = item.id;
                q.value = item.name;
                if (meta) {
                  meta.classList.remove("is-empty");
                  meta.innerHTML = "<span>" + esc(item.area || "No area") + "</span>";
                }
                box.hidden = true;
                box.innerHTML = "";
              };
            });
          })
          .catch(function () {
            box.innerHTML = "<div class=\"settle-suggest-empty\">Could not search</div>";
            box.hidden = false;
          });
      }, 180);
    });
    document.addEventListener("click", function (ev) {
      if (!form.contains(ev.target)) box.hidden = true;
    });
  }

  document.querySelectorAll(".js-inv-use").forEach(bindUseForm);
})();
