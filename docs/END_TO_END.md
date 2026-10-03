# ContentCrew end to end

What happens, step by step, from **"Write me a blog about Agentic AI."** to
**"Blog published successfully."** Every line below is a real event the app
records; `GET /api/sessions/<id>/trace` returns the same list for any chat.

## 1. The request

| Step | Who | What you see |
|---|---|---|
| `POST /api/chat` | Flask | Your message appears; a session and a fresh LangGraph thread (`workflow_id`) are created |
| Context loaded | Runner | "Loaded marketing context for Apimio: 3 audiences, 3 competitors" |
| `create_plan` | Supervisor | The 7-step plan appears; top bar: *Supervisor → Planning* |
| `route_task` | Supervisor | "Assigned research to the Analysis Agent" |

## 2. Research (Analysis Agent)

| Node | Tools | Top bar |
|---|---|---|
| `prepare_research` | | Analysis Agent → Research Planning |
| `discover_competitors` | | Analysis Agent → Competitor Discovery (uses your saved competitors, or searches) |
| `search_competitor_content` | Google Search (Tavily), up to 4 queries | Analysis Agent → Web Search |
| `scrape_content` | Web Scraper, up to 5 pages; searches once more if fewer than 3 sites | Analysis Agent → Web Scraping |
| `analyze_competitors` | Competitor Analysis | Analysis Agent → Competitor Analysis |
| `analyze_keywords` | Keyword Analysis | Analysis Agent → Keyword Analysis |
| `identify_content_gaps` | | Analysis Agent → Content Gaps |
| `create_research_summary` | | Analysis Agent → Research Summary |

## 3. Human approval #1

The graph **pauses** inside `request_research_approval` (LangGraph `interrupt()`;
the state is saved to `data/checkpoints.db`). Top bar: *Research approval required*.
The card shows the counts and the summary; **Review strategy** opens the rest.

- **Approve and continue** → `POST /api/approval/research {decision: "approve"}` → the graph resumes.
- **Request changes** → `decision: "modify"` with your feedback → back to `prepare_research`.
- **Cancel request** (click twice) → `decision: "reject"` → the Supervisor ends the run: *Request cancelled*.

## 4. Writing (Generation Agent)

| Node | Tools | Top bar |
|---|---|---|
| `route_task` | | "Research approved. Assigned writing to the Generation Agent" |
| `prepare_generation` | | Generation Agent → Reading the Research |
| `build_outline` | Google Search, at most one lookup | Generation Agent → Outline |
| `generate_blog` | | Generation Agent → Writing |
| `optimize_blog` | SEO Analysis (measures, fixes, measures again) | Generation Agent → SEO Optimization |
| `prepare_blog_review` | | Generation Agent → Saving the Draft; the editor opens on the right |

## 5. Human approval #2

The graph pauses in `request_content_approval`. Top bar: *Content approval required*.
Only the newest draft can be edited or published; older versions open read-only.

- **Edit** → `PATCH /api/blog/<id>` (accepted only while the current draft is in review).
- **Regenerate**, or type a change in the chat → `decision: "regenerate"` → `plan_revision` decides:
  only the named sections (`revise_blog`), a new draft (`generate_blog`) or a new outline (`build_outline`).
  The editor locks ("Agents working") until the new version is ready. The research isn't redone.
- **Publish** (click twice) → `POST /api/approval/content {decision: "approve"}`.

## 6. Publishing

| Step | What happens |
|---|---|
| `route_task` | "Draft approved. Publishing the post" |
| `publish_blog` | The Publish Blog tool sends **`POST /blog {session_id, blog_id}`** over HTTP |
| `POST /blog` | The backend checks: approval recorded, workflow at the publish step, current draft, not empty → **201** |
| Done | "Published: …" with **View post**; top bar and timeline: **Blog published successfully** |

If the blog API can't be reached, the step fails with **Retry**. If it refuses
(for example, an older draft), it fails without Retry and says why.

## Checked by the tests

`tests/test_integration.py` runs this flow through the API, including a real HTTP
`POST /blog` to a running server, Cancel request, regenerate-then-publish, a blog API
outage with Retry, and approval/graph mismatches. The other test files cover each
agent, tool and approval in detail (134 tests in total).
