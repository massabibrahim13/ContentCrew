/**
 * Light or dark mode.
 *
 * Loaded as a plain script in each page's <head> (not a module, not deferred),
 * so the theme is set before anything is drawn and the page never flashes the
 * wrong colours. The visitor's choice is remembered in this browser; until they
 * choose, the app follows the system setting. Buttons marked
 * [data-theme-toggle] switch between the two.
 */
(() => {
  const KEY = "contentcrew-theme";
  const root = document.documentElement;
  const system = window.matchMedia("(prefers-color-scheme: dark)");

  const chosen = () => {
    try {
      const value = window.localStorage.getItem(KEY);
      return value === "dark" || value === "light" ? value : null;
    } catch {
      return null;           // storage blocked (private mode): just follow the system
    }
  };

  const apply = (theme) => {
    root.dataset.theme = theme;
    for (const button of document.querySelectorAll("[data-theme-toggle]")) {
      button.setAttribute("aria-pressed", String(theme === "dark"));
      button.title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    }
  };

  apply(chosen() || (system.matches ? "dark" : "light"));

  system.addEventListener("change", (e) => {
    if (!chosen()) apply(e.matches ? "dark" : "light");
  });

  document.addEventListener("click", (e) => {
    if (!(e.target instanceof Element) || !e.target.closest("[data-theme-toggle]")) return;
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    try {
      window.localStorage.setItem(KEY, next);
    } catch {
      /* not remembered, but still switched for this page */
    }
    apply(next);
  });

  // The buttons exist once the page has loaded: give them the right state.
  document.addEventListener("DOMContentLoaded", () => apply(root.dataset.theme));
})();
