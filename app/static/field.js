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

  function pingLoop() {
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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      watchForms();
      pingLoop();
    });
  } else {
    watchForms();
    pingLoop();
  }
})();
