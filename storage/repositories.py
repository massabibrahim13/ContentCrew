"""
Repositories: the only place that writes SQL.

Routes and services call these methods and get plain dicts back, so nothing
else in the app needs to know how the data is stored.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from storage.database import Database, utc_now


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Company context
# ---------------------------------------------------------------------------

class CompanyRepository:
    def __init__(self, db: Database):
        self.db = db

    def get_context(self) -> dict:
        """Company profile + competitors + research totals, as one dict."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM company_profile WHERE id = 1").fetchone()
            competitors = conn.execute(
                "SELECT id, name, website FROM competitors ORDER BY position, id"
            ).fetchall()
            research = conn.execute(
                """
                SELECT COALESCE(SUM(articles_analyzed), 0)   AS articles_analyzed,
                       COALESCE(SUM(keywords_discovered), 0) AS keywords_discovered,
                       MAX(created_at)                       AS last_research_at
                FROM research_runs
                """
            ).fetchone()

        company = None
        if row:
            company = {
                "name": row["name"],
                "website": row["website"],
                "industry": row["industry"],
                "description": row["description"],
                "target_audience": _loads(row["target_audience"], []),
                "products": _loads(row["products"], []),
                "updated_at": row["updated_at"],
            }

        return {
            "onboarded": company is not None,
            "company": company,
            "competitors": [dict(c) for c in competitors],
            "research": dict(research),
        }

    def is_onboarded(self) -> bool:
        with self.db.connect() as conn:
            return conn.execute("SELECT 1 FROM company_profile WHERE id = 1").fetchone() is not None

    def save_context(self, company: dict, competitors: list[dict]) -> dict:
        """Replace the whole marketing context in one transaction."""
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO company_profile
                    (id, name, website, industry, description, target_audience, products, updated_at)
                VALUES (1, :name, :website, :industry, :description, :target_audience, :products, :updated_at)
                ON CONFLICT (id) DO UPDATE SET
                    name = excluded.name,
                    website = excluded.website,
                    industry = excluded.industry,
                    description = excluded.description,
                    target_audience = excluded.target_audience,
                    products = excluded.products,
                    updated_at = excluded.updated_at
                """,
                {
                    "name": company["name"],
                    "website": company.get("website"),
                    "industry": company.get("industry"),
                    "description": company["description"],
                    "target_audience": json.dumps(company.get("target_audience", [])),
                    "products": json.dumps(company.get("products", [])),
                    "updated_at": now,
                },
            )
            conn.execute("DELETE FROM competitors")
            conn.executemany(
                "INSERT INTO competitors (name, website, position) VALUES (?, ?, ?)",
                [(c["name"], c.get("website"), i) for i, c in enumerate(competitors)],
            )
        return self.get_context()


# ---------------------------------------------------------------------------
# Workflow sessions
# ---------------------------------------------------------------------------

SESSION_FIELDS = {
    "title", "status", "graph_thread_id", "current_agent", "current_node", "approval_required",
    "research_approved", "content_approved", "blog_status", "publish_status",
    "last_error",
}


class SessionRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, title: str, status: str) -> dict:
        session_id = uuid.uuid4().hex
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, title, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, title, status, now, now),
            )
        return self.get(session_id)

    def get(self, session_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._to_dict(row) if row else None

    def update(self, session_id: str, **changes) -> dict | None:
        unknown = set(changes) - SESSION_FIELDS
        if unknown:
            raise ValueError(f"Unknown session fields: {sorted(unknown)}")
        if "last_error" in changes and isinstance(changes["last_error"], dict):
            changes["last_error"] = json.dumps(changes["last_error"])
        for flag in ("research_approved", "content_approved"):
            if flag in changes:
                changes[flag] = 1 if changes[flag] else 0
        if changes:
            changes["updated_at"] = utc_now()
            assignments = ", ".join(f"{name} = :{name}" for name in changes)
            with self.db.connect() as conn:
                conn.execute(
                    f"UPDATE sessions SET {assignments} WHERE id = :session_id",
                    {**changes, "session_id": session_id},
                )
        return self.get(session_id)

    def ids_with_status(self, statuses: list[str]) -> list[str]:
        marks = ", ".join("?" for _ in statuses)
        with self.db.connect() as conn:
            rows = conn.execute(f"SELECT id FROM sessions WHERE status IN ({marks})", statuses).fetchall()
        return [r["id"] for r in rows]

    @staticmethod
    def _to_dict(row) -> dict:
        data = dict(row)
        data["research_approved"] = bool(data["research_approved"])
        data["content_approved"] = bool(data["content_approved"])
        data["last_error"] = _loads(data["last_error"], None)
        return data


# ---------------------------------------------------------------------------
# Timeline events
# ---------------------------------------------------------------------------

class EventRepository:
    def __init__(self, db: Database):
        self.db = db

    def add(self, session_id: str, event: dict) -> dict:
        created_at = utc_now()
        payload = {k: v for k, v in event.items() if k != "type"}
        with self.db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO events (session_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
                (session_id, event["type"], json.dumps(payload), created_at),
            )
            event_id = cursor.lastrowid
        return {"id": event_id, "session_id": session_id, "type": event["type"],
                "created_at": created_at, **payload}

    def list_after(self, session_id: str, after_id: int = 0, limit: int = 500) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, type, payload, created_at FROM events
                WHERE session_id = ? AND id > ?
                ORDER BY id LIMIT ?
                """,
                (session_id, after_id, limit),
            ).fetchall()
        return [
            {"id": r["id"], "session_id": r["session_id"], "type": r["type"],
             "created_at": r["created_at"], **_loads(r["payload"], {})}
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Blogs
# ---------------------------------------------------------------------------

def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:80] or "post"


class BlogRepository:
    def __init__(self, db: Database):
        self.db = db

    def create_draft(self, session_id: str | None, title: str, content: str) -> dict:
        blog_id = uuid.uuid4().hex
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO blogs (id, session_id, title, content, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'draft', ?, ?)
                """,
                (blog_id, session_id, title, content, now, now),
            )
        return self.get(blog_id)

    def get(self, blog_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM blogs WHERE id = ?", (blog_id,)).fetchone()
        return dict(row) if row else None

    def get_published_by_slug(self, slug: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM blogs WHERE slug = ? AND status = 'published'", (slug,)).fetchone()
        return dict(row) if row else None

    def update_draft(self, blog_id: str, title: str | None, content: str | None) -> dict | None:
        changes = {k: v for k, v in {"title": title, "content": content}.items() if v is not None}
        if changes:
            changes["updated_at"] = utc_now()
            assignments = ", ".join(f"{name} = :{name}" for name in changes)
            with self.db.connect() as conn:
                conn.execute(
                    f"UPDATE blogs SET {assignments} WHERE id = :blog_id AND status = 'draft'",
                    {**changes, "blog_id": blog_id},
                )
        return self.get(blog_id)

    def mark_published(self, blog_id: str) -> dict | None:
        blog = self.get(blog_id)
        if not blog:
            return None
        now = utc_now()
        slug = f"{slugify(blog['title'])}-{blog_id[:6]}"
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE blogs SET status = 'published', slug = ?, published_at = ?, updated_at = ?
                WHERE id = ? AND status = 'draft'
                """,
                (slug, now, now, blog_id),
            )
        return self.get(blog_id)

    def list(self, status: str | None = None, limit: int = 50) -> list[dict]:
        query = "SELECT id, session_id, title, status, slug, created_at, published_at FROM blogs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]


# ---------------------------------------------------------------------------
# Research runs (feeds the "Research" numbers in the left panel)
# ---------------------------------------------------------------------------

class ResearchRepository:
    def __init__(self, db: Database):
        self.db = db

    def add_run(self, session_id: str, articles_analyzed: int, keywords_discovered: int) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO research_runs (session_id, articles_analyzed, keywords_discovered, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, articles_analyzed, keywords_discovered, utc_now()),
            )
