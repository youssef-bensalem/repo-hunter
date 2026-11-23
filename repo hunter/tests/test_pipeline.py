"""Run with:  pip install pytest && python -m pytest -q"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import collect
import config
import draft
import main
import notify
import score

NOW = dt.datetime(2026, 11, 23, 6, 0, tzinfo=dt.timezone.utc)


def gh_item(name, stars=500, created="2026-11-20T00:00:00Z", pushed="2026-11-22T00:00:00Z",
            desc="A fast local-first note taking app", license_="MIT", **kw):
    owner = name.split("/")[0]
    return {"full_name": name, "html_url": f"https://github.com/{name}", "owner": {"login": owner},
            "description": desc, "stargazers_count": stars, "forks_count": 10, "language": "Python",
            "topics": kw.get("topics", []), "license": {"spdx_id": license_} if license_ else None,
            "homepage": "", "created_at": created, "pushed_at": pushed,
            "archived": kw.get("archived", False), "fork": kw.get("fork", False)}


class FakeResp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text or json.dumps(data or {})

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    for mod in (collect, draft, notify, main):
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)


# ---------- collect ----------

def test_extract_repo_names():
    text = ("see https://github.com/foo/bar.git and github.com/Foo/Bar, https://www.github.com/a-b/c.d. "
            "not https://gist.github.com/x/123 nor https://github.com/topics/python or github.com/sponsors/x")
    assert collect.extract_repo_names(text) == ["foo/bar", "a-b/c.d"]


def test_collect_merges_sources(monkeypatch):
    class S:
        def get(self, url, params=None, timeout=None, headers=None):
            if url.endswith("/search/repositories"):
                return FakeResp(data={"items": [gh_item("a/one")]})
            if url.endswith("/repos/b/two"):
                return FakeResp(data=gh_item("b/two", created="2024-01-01T00:00:00Z"))
            return FakeResp(404)
    monkeypatch.setattr(collect, "names_from_hacker_news", lambda: [("a/one", "hn"), ("b/two", "hn"), ("gone/x", "hn")])
    monkeypatch.setattr(collect, "names_from_reddit", lambda: [("B/Two", "r/selfhosted")])
    pool = {r["full_name"]: r for r in collect.collect(S(), now=NOW)}
    assert set(pool) == {"a/one", "b/two"}
    assert pool["a/one"]["sources"] == ["github", "hn"]
    assert pool["b/two"]["sources"] == ["hn", "r/selfhosted"]


def test_network_failures_are_warnings(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("blocked")
    monkeypatch.setattr(collect.requests, "get", boom)
    assert collect.names_from_hacker_news() == []
    assert collect.names_from_reddit(["x"]) == []


# ---------- score ----------

def repo(name, **kw):
    r = collect.normalize(gh_item(name, **kw), kw.pop("source", "github"))
    return r


def test_filters():
    seen = {"old/seen": "2026-11-01"}
    cases = {
        "old/seen": None, "x/archived": dict(archived=True), "x/forked": dict(fork=True),
        "x/nolicense": dict(license_=None), "x/stale": dict(pushed="2026-09-01T00:00:00Z"),
        "x/awesome-python": {}, "x/game-cheats": dict(desc="Free cheats and aimbot"),
        "x/nodesc": dict(desc=""),
    }
    reasons = {n: score.reject_reason(repo(n, **(kw or {})), seen, NOW) for n, kw in cases.items()}
    assert reasons == {"old/seen": "already sent", "x/archived": "archived", "x/forked": "fork",
                       "x/nolicense": "no license", "x/stale": "inactive", "x/awesome-python": "excluded name/topic",
                       "x/game-cheats": "spam/malware keywords", "x/nodesc": "no description"}
    assert score.reject_reason(repo("x/good", desc="An awesome CLI"), seen, NOW) is None  # 'awesome' in desc is fine


def test_ranking_prefers_velocity_and_community():
    fast = repo("x/fast", stars=300, created="2026-11-22T06:00:00Z")      # 300 stars in 1 day
    slow = repo("x/slow", stars=1000, created="2026-11-13T06:00:00Z")     # 100/day
    old_hn = repo("x/oldhn", stars=3000, created="2022-01-01T00:00:00Z")  # old, but on HN now
    old_hn["sources"] = ["hn"]
    ranked = score.rank([slow, fast, old_hn], {}, now=NOW)
    assert [r["full_name"] for r in ranked] == ["x/fast", "x/oldhn", "x/slow"]
    assert ranked[1]["score"] == 150.0  # 3000 / 30 days * 1.5


# ---------- draft ----------

def test_x_length():
    assert draft.x_length("hello") == 5
    assert draft.x_length("→ a") == 4
    assert draft.x_length("see https://example.com/very/long/path/here") == 4 + 23
    assert draft.x_length("🚀") == 2


def test_clean_readme():
    md = ('<!-- hi --><p align="center"><img src="x.png"></p>\n'
          '[![CI](https://b.svg)](https://ci) ![logo](l.png)\n\n\n\n# Tool\nDoes things.')
    assert draft.clean_readme(md) == "# Tool\nDoes things."


def test_fallback_post_fits():
    r = repo("x/tool", desc="word " * 120, stars=12345)
    text = draft.fallback_post(r, "maker")
    assert draft.x_length(text) <= 280 and "by @maker" in text and "12,345 stars" in text


def test_draft_uses_llm_and_retries_when_too_long(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    replies = iter(["x" * 400, '"Short post\n\n→ feature\n#opensource"'])
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json))
        return FakeResp(data={"choices": [{"message": {"content": next(replies)}}]})
    monkeypatch.setattr(draft.requests, "post", fake_post)
    out = draft.draft_post(repo("x/tool"), "readme", "maker")
    assert out == {"text": "Short post\n\n→ feature\n#opensource", "chars": 34, "used_llm": True, "warning": None}
    assert len(calls) == 2 and calls[0][0].endswith("/chat/completions")
    assert 'by @maker' in calls[0][1]["messages"][0]["content"]


def test_draft_falls_back_without_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    out = draft.draft_post(repo("x/tool"), "readme")
    assert out["used_llm"] is False and "template draft" in out["warning"] and out["text"].startswith("tool:")


def test_llm_retries_on_429(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    seq = iter([FakeResp(429, {}), FakeResp(data={"choices": [{"message": {"content": "ok"}}]})])
    monkeypatch.setattr(draft.requests, "post", lambda *a, **k: next(seq))
    assert draft.llm_chat([]) == "ok"


def test_thread_parsing(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    monkeypatch.setattr(draft, "llm_chat", lambda *a, **k: "Hook 🧵\n---\nOne\n---\nFollow me")
    assert draft.draft_thread([{"full_name": "a/b", "url": "u", "description": "d"}]) == ["Hook 🧵", "One", "Follow me"]


# ---------- notify ----------

def test_discord_messages_and_send(monkeypatch):
    item = {**repo("x/tool"), "score": 12.5, "text": "Post ```text```", "chars": 20, "warning": None}
    msgs = notify.build_daily_messages([item], "2026-11-23")
    assert len(msgs) == 2
    assert "```\nPost '''text'''\n```" in msgs[1]["content"]
    assert msgs[1]["embeds"][0]["image"]["url"] == "https://opengraph.githubassets.com/2026-11-23/x/tool"

    sent, seq = [], iter([FakeResp(429, {"retry_after": 0.1}), FakeResp(204, {}), FakeResp(204, {})])

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return next(seq)
    monkeypatch.setattr(notify.requests, "post", fake_post)
    notify.send("https://discord/webhook", msgs)
    assert len(sent) == 3 and sent[0]["allowed_mentions"] == {"parse": []}


# ---------- main (end to end with fakes) ----------

def test_daily_then_weekly(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "TOP_N", 2)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    items = [gh_item("a/one", stars=900), gh_item("b/two", stars=600), gh_item("c/short", stars=800),
             gh_item("d/three", stars=100)]

    class S:
        def get(self, url, params=None, timeout=None, headers=None):
            if url.endswith("/search/repositories"):
                return FakeResp(data={"items": items})
            if url.endswith("/readme"):
                return FakeResp(text="tiny" if "c/short" in url else "# Readme\n" + "content " * 100)
            if "/users/" in url:
                return FakeResp(data={"twitter_username": "aone" if url.endswith("/a") else None})
            return FakeResp(404)
    monkeypatch.setattr(main, "gh_session", lambda token: S())
    monkeypatch.setattr(collect, "names_from_hacker_news", lambda: [])
    monkeypatch.setattr(collect, "names_from_reddit", lambda: [])
    sent = []
    monkeypatch.setattr(main, "send", lambda url, msgs, dry_run=False: sent.append(msgs))

    picks = main.run_daily(now=NOW)
    assert [p["full_name"] for p in picks] == ["a/one", "b/two"]  # c/short skipped (README)
    assert "by @aone" in picks[0]["text"]
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert seen == {"a/one": "2026-11-23", "b/two": "2026-11-23"}

    # second run the same day: already-sent repos are not repeated
    picks2 = main.run_daily(now=NOW)
    assert [p["full_name"] for p in picks2] == ["d/three"]

    tweets = main.run_weekly(now=NOW)
    assert tweets[0].startswith("3 open-source tools") and len(tweets) == 5


def test_dry_run_changes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "collect", lambda s, now=None: [])
    main.run_daily(dry_run=True, now=NOW)
    assert not (tmp_path / "seen.json").exists()


def test_requires_webhook(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    with pytest.raises(SystemExit):
        main.main(["daily"])


def test_stops_after_repeated_readme_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    candidates = [collect.normalize(gh_item(f"o/r{i}", stars=1000 - i), "github") for i in range(20)]
    monkeypatch.setattr(main, "collect", lambda s, now=None: candidates)
    calls = []
    monkeypatch.setattr(main, "fetch_readme", lambda s, name: calls.append(name))  # returns None = failure
    assert main.run_daily(dry_run=True, now=NOW) == []
    assert len(calls) == 5


def test_english_filter_and_suspicious_flag():
    zh = repo("x/zh", desc="白嫖与资讯资源聚合雷达 radar")
    fr = repo("x/fr", desc="Un éditeur rapide et léger")
    assert score.reject_reason(zh, {}, NOW) == "non-English description"
    assert score.reject_reason(fr, {}, NOW) is None
    hot = repo("x/hot", stars=26000, created="2026-11-20T06:00:00Z")
    ok = repo("x/ok", stars=900, created="2026-11-20T06:00:00Z")
    flags = {r["full_name"]: r["flag"] for r in score.rank([hot, ok], {}, now=NOW)}
    assert flags["x/hot"] and flags["x/ok"] is None
    item = {**score.rank([hot], {}, now=NOW)[0], "text": "t", "chars": 1, "warning": None}
    assert "🚩" in notify.build_daily_messages([item], "d")[1]["content"]
