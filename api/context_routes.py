"""
Marketing context: the company profile and competitors every agent works from.

GET  /api/context          current context (company is null until onboarding is done)
PUT  /api/context          replace the context (onboarding and "Edit context")
GET  /api/context/sample   the sample company used in class
"""

from __future__ import annotations

import json
import logging

from flask import Blueprint, jsonify

from api import deps
from api.errors import ApiError, ValidationError
from api.validation import Validator, json_body
from config import BASE_DIR

log = logging.getLogger(__name__)
bp = Blueprint("context", __name__)

MAX_COMPETITORS = 6
SAMPLE_PATH = BASE_DIR / "data" / "sample_company.json"


@bp.get("/context")
def get_context():
    return jsonify(deps.company_repo().get_context())


@bp.put("/context")
def save_context():
    data = json_body()
    company_in = data.get("company")
    if not isinstance(company_in, dict):
        raise ValidationError({"company": "Company details are missing."})
    competitors_in = data.get("competitors", [])

    v = Validator()
    company = {
        "name": v.text(company_in.get("name"), "company.name", "the company name",
                       required=True, min_len=2, max_len=80),
        "website": v.url(company_in.get("website"), "company.website"),
        "industry": v.text(company_in.get("industry"), "company.industry", "the industry", max_len=80),
        "description": v.text(company_in.get("description"), "company.description",
                              "a description", required=True, min_len=20, max_len=600, multiline=True),
        "target_audience": v.string_list(company_in.get("target_audience"), "company.target_audience",
                                         "target audience", min_items=1, max_items=8, item_max=60),
        "products": v.string_list(company_in.get("products"), "company.products",
                                  "products and services", max_items=10, item_max=80),
    }

    competitors: list[dict] = []
    if not isinstance(competitors_in, list):
        v.fail("competitors", "Competitors must be a list.")
    elif len(competitors_in) > MAX_COMPETITORS:
        v.fail("competitors", f"Add at most {MAX_COMPETITORS} competitors.")
    else:
        seen: set[str] = set()
        for i, item in enumerate(competitors_in):
            if not isinstance(item, dict):
                v.fail(f"competitors.{i}.name", "Enter the competitor's name.")
                continue
            name = v.text(item.get("name"), f"competitors.{i}.name", "the competitor's name",
                          required=True, max_len=80)
            website = v.url(item.get("website"), f"competitors.{i}.website")
            if name and name.lower() in seen:
                v.fail(f"competitors.{i}.name", "This competitor is already in the list.")
            elif name:
                seen.add(name.lower())
                competitors.append({"name": name, "website": website})

    v.raise_if_errors()
    context = deps.company_repo().save_context(company, competitors)
    log.info("Marketing context saved for %s (%d competitors)", company["name"], len(competitors))
    return jsonify(context)


@bp.get("/context/sample")
def sample_context():
    try:
        with open(SAMPLE_PATH, encoding="utf-8") as fh:
            return jsonify(json.load(fh))
    except (OSError, ValueError):
        log.exception("Could not read %s", SAMPLE_PATH)
        raise ApiError(500, "sample_unavailable", "The sample company file couldn't be read.")
