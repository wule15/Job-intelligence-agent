"""
The Apify actors renamed their inputs, so the old names were ignored: the
LinkedIn actor returned its default US listing and the Indeed actor searched
the US site. These tests pin the current input names, the kept location, the
per-call cost limits and the monthly budget check, with a fake client so no
test spends Apify credit.
"""

from sources import apify


class _Actor:
    def __init__(self, client, name):
        self.client, self.name = client, name

    def call(self, **kwargs):
        self.client.calls.append((self.name, kwargs))
        return {"defaultDatasetId": self.name}


class _Dataset:
    def __init__(self, items):
        self.items = items

    def iterate_items(self):
        return iter(self.items)


class _User:
    def __init__(self, used):
        self.used = used

    def limits(self):
        return {"current": {"monthlyUsageUsd": self.used}}


class _Client:
    def __init__(self, items=None, used=0.5):
        self.calls, self.items, self.used = [], items or {}, used

    def actor(self, name):
        return _Actor(self, name)

    def dataset(self, name):
        return _Dataset(self.items.get(name, []))

    def user(self):
        return _User(self.used)


LINKEDIN = "bebity/linkedin-jobs-scraper"
INDEED = "misceres/indeed-scraper"


def test_linkedin_uses_current_input_names_and_keeps_location(monkeypatch):
    monkeypatch.setattr(apify.time, "sleep", lambda s: None)
    client = _Client({LINKEDIN: [{"title": "Sales Engineer", "companyName": "Acme",
                                  "jobUrl": "https://x/1", "location": "Munich, Bavaria, Germany"}]})
    jobs = apify.search_linkedin(["sales engineer"], client=client)
    name, kwargs = client.calls[0]
    run_input = kwargs["run_input"]
    assert run_input["titles"] == ["sales engineer"]
    assert run_input["locations"] and run_input["rows"] == apify.LINKEDIN_ROWS
    assert "queries" not in run_input and "count" not in run_input
    assert kwargs["max_items"] == apify.LINKEDIN_ROWS
    assert kwargs["max_total_charge_usd"] == apify.LINKEDIN_CALL_LIMIT_USD
    assert jobs[0]["location"] == "Munich, Bavaria, Germany"


def test_indeed_searches_a_european_site_and_tags_the_country(monkeypatch):
    monkeypatch.setattr(apify.time, "sleep", lambda s: None)
    client = _Client({INDEED: [{"positionName": "Application Engineer", "company": "Acme",
                                "url": "https://x/2", "location": "Rotterdam"}]})
    jobs = apify.search_indeed(["application engineer"], country="NL", client=client)
    run_input = client.calls[0][1]["run_input"]
    assert run_input == {"position": "application engineer", "country": "NL",
                         "maxItemsPerSearch": apify.INDEED_ROWS}
    assert "Netherlands" in jobs[0]["location"]


def test_country_rotates_daily(monkeypatch):
    monkeypatch.setenv("APIFY_INDEED_COUNTRIES", "GB,DE")
    assert [apify._indeed_country(d) for d in range(4)] == ["GB", "DE", "GB", "DE"]


def test_budget_cap_skips_apify(monkeypatch):
    client = _Client(used=4.6)
    monkeypatch.setattr(apify, "_client", lambda: client)
    monkeypatch.setenv("APIFY_MONTHLY_CAP_USD", "4.5")
    assert apify.ApifyJobSearcher().search_all(["a", "b", "c"]) == []
    assert client.calls == []


def test_normal_run_makes_three_linkedin_calls_and_one_indeed_call(monkeypatch):
    monkeypatch.setattr(apify.time, "sleep", lambda s: None)
    client = _Client(used=1.0)
    monkeypatch.setattr(apify, "_client", lambda: client)
    apify.ApifyJobSearcher().search_all(["a", "b", "c", "d"])
    names = [name for name, _ in client.calls]
    assert names.count(LINKEDIN) == 3 and names.count(INDEED) == 1
