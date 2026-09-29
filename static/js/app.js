// Africa ScholarBridge — app.js
// Small, dependency-free interactivity helpers. Bootstrap 5 JS (loaded via
// CDN in base.html) handles the navbar, accordions and dismissible alerts.

document.addEventListener("DOMContentLoaded", function () {
  // Auto-dismiss success/info alerts after a few seconds so they don't
  // clutter the page on longer sessions.
  document.querySelectorAll(".alert-success, .alert-info").forEach(function (alertEl) {
    setTimeout(function () {
      if (window.bootstrap && window.bootstrap.Alert) {
        window.bootstrap.Alert.getOrCreateInstance(alertEl).close();
      }
    }, 6000);
  });

  // Simple client-side confirmation for destructive-looking actions.
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      if (!confirm(el.getAttribute("data-confirm"))) {
        e.preventDefault();
      }
    });
  });
});
