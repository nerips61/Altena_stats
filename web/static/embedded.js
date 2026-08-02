/**
 * Mode portail — détecte ?embedded=1 ou chargement dans une iframe.
 * Ajoute html[data-embedded="1"] et body.embedded pour masquer en-têtes dupliqués.
 */
(function () {
  const params = new URLSearchParams(window.location.search);
  const embedded =
    params.get("embedded") === "1" ||
    (window.self !== window.top && params.get("embedded") !== "0");

  if (!embedded) return;

  document.documentElement.dataset.embedded = "1";

  function apply() {
    document.body.classList.add("embedded");
  }

  if (document.body) apply();
  else document.addEventListener("DOMContentLoaded", apply);
})();
