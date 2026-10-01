/** Left panel: the marketing context the agents work from. */

import { h, domainOf, formatDate, safeHref } from "./dom.js";

function section(...children) {
  return h("section", { class: "ctx-section" }, children);
}

function companySection(company) {
  const site = safeHref(company.website);
  return section(
    h("h3", { class: "ctx-name" }, company.name),
    h("p", { class: "ctx-meta" },
      company.industry ? h("span", {}, company.industry) : null,
      site ? h("a", { href: site, target: "_blank", rel: "noopener noreferrer" }, domainOf(site)) : null,
    ),
    h("p", { class: "ctx-about" }, company.description),
    company.target_audience?.length
      ? [h("h4", { class: "ctx-label" }, "Target audience"),
         h("ul", { class: "chips" }, company.target_audience.map((a) => h("li", {}, a)))]
      : null,
    company.products?.length
      ? [h("h4", { class: "ctx-label" }, "Products and services"),
         h("ul", { class: "ctx-list" }, company.products.map((p) => h("li", {}, p)))]
      : null,
  );
}

function competitorSection(competitors) {
  return section(
    h("h3", { class: "ctx-heading" }, "Competitors", h("span", { class: "ctx-count" }, competitors.length || "")),
    competitors.length
      ? h("ul", { class: "competitor-list" }, competitors.map((c) => {
          const site = safeHref(c.website);
          return h("li", {},
            h("span", { class: "competitor-name" }, c.name),
            site ? h("a", { class: "competitor-site", href: site, target: "_blank", rel: "noopener noreferrer" },
                     domainOf(site)) : null,
          );
        }))
      : h("p", { class: "ctx-empty" }, "None added. The Analysis Agent can find competitors on its own, or you can add them under Edit."),
  );
}

function researchSection(research) {
  const ran = Boolean(research?.last_research_at);
  return section(
    h("h3", { class: "ctx-heading" }, "Research"),
    h("dl", { class: "stats" },
      h("div", {}, h("dt", {}, "Articles analyzed"), h("dd", {}, research?.articles_analyzed ?? 0)),
      h("div", {}, h("dt", {}, "Keywords discovered"), h("dd", {}, research?.keywords_discovered ?? 0)),
      h("div", {}, h("dt", {}, "Last research"), h("dd", {}, ran ? formatDate(research.last_research_at) : "Never")),
    ),
    ran ? null : h("p", { class: "ctx-empty" }, "These numbers fill in after the Analysis Agent's first run."),
  );
}

function connectionsSection(health) {
  const rows = health
    ? [
        ["Language model", health.integrations.llm],
        ["Web search", health.integrations.search],
        ["Agent workflow", health.workflow_engine === "connected"],
      ]
    : null;
  return section(
    h("h3", { class: "ctx-heading" }, "Connections"),
    rows
      ? h("ul", { class: "connections" }, rows.map(([label, ok]) =>
          h("li", {}, label, h("span", { class: "connection-state", "data-ok": String(ok) },
            ok ? "Connected" : "Not set up"))))
      : h("p", { class: "ctx-empty" }, "Couldn't check the server's connections."),
  );
}

export function renderContext(body, context, health) {
  body.replaceChildren(
    companySection(context.company),
    competitorSection(context.competitors || []),
    researchSection(context.research),
    connectionsSection(health),
  );
}

export function renderContextError(body, message, onRetry) {
  body.replaceChildren(
    h("p", { class: "ctx-error", role: "alert" }, message),
    h("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: onRetry }, "Try again"),
  );
}
