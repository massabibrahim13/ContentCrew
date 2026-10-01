import copy

from tests.conftest import VALID_CONTEXT


def test_empty_context_before_onboarding(client):
    data = client.get("/api/context").get_json()
    assert data["onboarded"] is False
    assert data["company"] is None
    assert data["competitors"] == []
    assert data["research"]["articles_analyzed"] == 0


def test_save_and_read_context(client, onboarded):
    assert onboarded["onboarded"] is True
    assert onboarded["company"]["website"] == "https://apimio.com"  # scheme added
    assert [c["name"] for c in onboarded["competitors"]] == ["Akeneo", "Salsify"]
    assert onboarded["competitors"][1]["website"] is None

    again = client.get("/api/context").get_json()
    assert again["company"]["target_audience"] == ["E-commerce brands", "Retail teams"]


def test_saving_replaces_competitors(client, onboarded):
    body = copy.deepcopy(VALID_CONTEXT)
    body["competitors"] = [{"name": "Plytix", "website": "plytix.com"}]
    data = client.put("/api/context", json=body).get_json()
    assert [c["name"] for c in data["competitors"]] == ["Plytix"]


def test_validation_reports_every_field(client):
    body = {
        "company": {"name": "", "website": "not a url", "description": "short",
                    "target_audience": []},
        "competitors": [{"name": ""}, {"name": "A", "website": "javascript:alert(1)"}],
    }
    response = client.put("/api/context", json=body)
    assert response.status_code == 422
    fields = response.get_json()["error"]["fields"]
    for key in ("company.name", "company.website", "company.description",
                "company.target_audience", "competitors.0.name", "competitors.1.website"):
        assert key in fields, key


def test_duplicate_competitors_rejected(client):
    body = copy.deepcopy(VALID_CONTEXT)
    body["competitors"] = [{"name": "Akeneo"}, {"name": "akeneo"}]
    response = client.put("/api/context", json=body)
    assert response.status_code == 422
    assert "competitors.1.name" in response.get_json()["error"]["fields"]


def test_too_many_competitors(client):
    body = copy.deepcopy(VALID_CONTEXT)
    body["competitors"] = [{"name": f"Competitor {i}"} for i in range(7)]
    response = client.put("/api/context", json=body)
    assert response.status_code == 422


def test_text_is_cleaned(client):
    body = copy.deepcopy(VALID_CONTEXT)
    body["company"]["name"] = "  Api\x00mio   Inc  "
    body["company"]["target_audience"] = ["Retail", " retail ", "Brands"]
    data = client.put("/api/context", json=body).get_json()
    assert data["company"]["name"] == "Apimio Inc"
    assert data["company"]["target_audience"] == ["Retail", "Brands"]


def test_requires_json(client):
    response = client.put("/api/context", data="name=x")
    assert response.status_code == 415
    assert response.get_json()["error"]["code"] == "unsupported_media_type"


def test_sample_company(client):
    data = client.get("/api/context/sample").get_json()
    assert data["company"]["name"] == "Apimio"
    # The sample must itself pass validation.
    assert client.put("/api/context", json=data).status_code == 200
