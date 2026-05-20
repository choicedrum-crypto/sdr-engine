/* Module 6 — Review UI keyboard + word-count behavior.
   Keyboard shortcuts per A9:
     s = Send (submit the form)
     k = Skip (POST /queue/<id>/skip)
     e = Focus the email body textarea
   Enter inside the textarea also submits (Cmd/Ctrl+Enter standard).
   Word count helper: turns red only outside [100, 250] per the
   spec's relaxed warning threshold. */

(function () {
  "use strict";

  // ─── Helper: POST without a form (for skip / retry buttons) ─────
  window.postTo = function (url) {
    fetch(url, { method: "POST", headers: { Accept: "application/json" } })
      .then(function (r) {
        if (r.ok || r.status === 502) {
          window.location.reload();
        } else {
          r.text().then(function (t) { alert("Action failed (" + r.status + "): " + t); });
        }
      })
      .catch(function (err) { alert("Network error: " + err); });
  };

  // ─── Keyboard shortcuts ─────────────────────────────────────────
  document.addEventListener("keydown", function (ev) {
    // Skip when user is typing in a text field — those keys mean characters,
    // not commands. The Cmd/Ctrl+Enter submit is the one exception.
    var inField = ev.target.matches("input, textarea, select");
    var meta = ev.metaKey || ev.ctrlKey;

    if (inField && ev.key === "Enter" && meta) {
      ev.preventDefault();
      var form = document.querySelector(".email-form");
      if (form) form.submit();
      return;
    }
    if (inField) return;

    if (ev.key === "s") {
      ev.preventDefault();
      var form = document.querySelector(".email-form");
      if (form) form.submit();
    } else if (ev.key === "k") {
      ev.preventDefault();
      var skipBtn = document.querySelector('[data-shortcut="k"]');
      if (skipBtn) skipBtn.click();
    } else if (ev.key === "e") {
      ev.preventDefault();
      var body = document.getElementById("body");
      if (body) {
        body.focus();
        body.setSelectionRange(body.value.length, body.value.length);
      }
    }
  });

  // ─── Live word count on the email body ──────────────────────────
  var body = document.getElementById("body");
  var counter = document.getElementById("word-count");
  if (body && counter) {
    function updateCount() {
      var words = body.value.trim().split(/\s+/).filter(Boolean).length;
      counter.textContent = words + " words";
      // Warn only OUTSIDE [100, 250] — wider than [150, 200] so the warning
      // doesn't fire on near-target drafts and SDR overrides it as noise.
      if (words < 100 || words > 250) {
        counter.classList.add("out-of-range");
      } else {
        counter.classList.remove("out-of-range");
      }
    }
    body.addEventListener("input", updateCount);
    updateCount();
  }
})();
