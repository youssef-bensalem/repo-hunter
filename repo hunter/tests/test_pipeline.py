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


def test_fallback_post_is_a_short_caption():
    r = repo("x/tool", desc="Turns any docs site into one Markdown file. Also does many other things " * 5)
    text = draft.fallback_post(r, "maker")
    first, credit = text.split("\n")
    assert first == "tool: Turns any docs site into one Markdown file." and credit == "by @maker"
    long = draft.fallback_post(repo("x/tool", desc="word " * 80))
    assert draft.x_length(long) <= 120 and long.endswith("…") and "\n" not in long

def test_draft_uses_llm_and_retries_when_too_long(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    replies = iter(["x" * 400, 'Post A: "Short post\n\n→ feature"\n---\nOption B) Other angle\n\n→ thing'])
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json))
        return FakeResp(data={"choices": [{"message": {"content": next(replies)}}]})
    monkeypatch.setattr(draft.requests, "post", fake_post)
    out = draft.draft_post(repo("x/tool"), "readme", "maker")
    assert out == {"text": "Short post\n\n→ feature", "alt": "Other angle\n\n→ thing", "chars": 22,
                   "used_llm": True, "warning": None}
    assert len(calls) == 2 and calls[0][0].endswith("/chat/completions")
    assert 'by @maker' in calls[0][1]["messages"][0]["content"]


def test_draft_falls_back_without_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    out = draft.draft_post(repo("x/tool"), "readme")
    assert out["used_llm"] is False and "template draft" in out["warning"] and out["text"].startswith("tool:")
    assert out["alt"] is None


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

def test_discord_messages_and_send(monkeypatch, tmp_path):
    item = {**repo("x/tool"), "score": 12.5, "text": "Post ```text```", "chars": 20, "warning": None}
    msgs = notify.build_daily_messages([item], "2026-11-23", card_dir=str(tmp_path))
    assert len(msgs) == 2
    assert "```\nPost '''text'''\n```" in msgs[1]["content"]
    assert msgs[1]["file"].endswith("1-x-tool.png") and "image" not in msgs[1]["embeds"][0]

    sent, seq = [], iter([FakeResp(204, {}), FakeResp(429, {"retry_after": 0.1}), FakeResp(204, {})])

    def fake_post(url, json=None, data=None, files=None, timeout=None):
        if files:
            name, fh, mime = files["files[0]"]
            sent.append(("file", __import__("json").loads(data["payload_json"]), name, fh.read()[:8], mime))
        else:
            sent.append(("json", json))
        return next(seq)
    monkeypatch.setattr(notify.requests, "post", fake_post)
    notify.send("https://discord/webhook", msgs)
    assert sent[0] == ("json", {"content": msgs[0]["content"], "allowed_mentions": {"parse": []}})
    assert len(sent) == 3 and sent[1] == sent[2]  # 429 retried with the same upload
    kind, payload, name, head, mime = sent[2]
    assert (kind, name, head, mime) == ("file", "1-x-tool.png", b"\x89PNG\r\n\x1a\n", "image/png")
    assert "file" not in payload and payload["allowed_mentions"] == {"parse": []}


def test_card_failure_falls_back_to_github_preview(monkeypatch, tmp_path):
    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(notify.card, "render_card", broken)
    item = {**repo("x/tool"), "score": 1, "text": "t", "chars": 1, "warning": None}
    msg = notify.build_daily_messages([item], "2026-11-23", card_dir=str(tmp_path))[1]
    assert "file" not in msg
    assert msg["embeds"][0]["image"]["url"] == "https://opengraph.githubassets.com/2026-11-23/x/tool"


# ---------- card ----------

def test_card_renders_long_and_messy_input(tmp_path):
    from PIL import Image
    import card
    r = repo("some-very-long-organisation-name/an-extremely-long-repository-name-that-cannot-fit-on-one-line",
             desc=":rocket: 🚀 Blazing fast " + "tool that does many useful things " * 12, stars=1234567)
    path = card.render_card(r, str(tmp_path / "c.png"), handle="repohunterdev")
    img = Image.open(path)
    assert img.size == (1600, 900) and img.format == "PNG"


def test_clean_text_and_wrap():
    import card
    from PIL import Image, ImageDraw
    assert card.clean_text(":rocket: Fast 🚀 CLI ✨ tool‍") == "Fast CLI tool"
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    f = card.font("Regular", 40)
    lines = card.wrap(d, "word " * 200, f, 800, 4)
    assert len(lines) == 4 and lines[-1].endswith("…")
    assert all(d.textlength(l, font=f) <= 800 for l in lines)
    assert card.wrap(d, "short text", f, 800, 4) == ["short text"]


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


def test_two_options_shown_in_discord(tmp_path):
    item = {**repo("x/tool"), "score": 1, "text": "Option one", "alt": "Option two", "chars": 10, "warning": None}
    content = notify.build_daily_messages([item], "d", card_dir=str(tmp_path))[1]["content"]
    assert "Option A · 10/280\n```\nOption one\n```" in content and "Option B · 10/280\n```\nOption two\n```" in content


# ---------- logos ----------

README = '''<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./docs/logo-dark.svg">
    <img alt="Tool logo" src="docs/logo-light.png" width="200">
  </picture>
</p>
[![CI](https://github.com/o/r/actions/workflows/ci.yml/badge.svg)](x) ![stars](https://img.shields.io/github/stars/o/r)
![screenshot](assets/screenshot.png)
<img src="https://github.com/o/r/blob/main/assets/icon.png">
''' + "x" * 6000 + "![late logo](late-logo.png)"


def test_find_logo_urls():
    import card
    org = repo("o/r"); org.update(owner_type="Organization", owner_avatar="https://avatars.githubusercontent.com/u/1?v=4")
    assert card.find_logo_urls(README, org) == [
        "https://raw.githubusercontent.com/o/r/HEAD/docs/logo-dark.svg",
        "https://raw.githubusercontent.com/o/r/HEAD/docs/logo-light.png",
        "https://raw.githubusercontent.com/o/r/main/assets/icon.png",
        "https://avatars.githubusercontent.com/u/1?v=4&s=400",
    ]
    user = repo("o/r"); user.update(owner_type="User", owner_avatar="https://avatars.githubusercontent.com/u/2?v=4")
    assert card.find_logo_urls("no images here", user) == []


def _png_bytes(color, size=(120, 120), mode="RGBA"):
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


class FakeStream:
    def __init__(self, status, data, ctype="image/png"):
        import io
        self.status_code, self.raw, self.headers = status, io.BytesIO(data), {"content-type": ctype}
        self.raw.read = (lambda f: (lambda n, decode_content=True: f(n)))(self.raw.read)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


def test_load_logo_tries_candidates_in_order(monkeypatch):
    import card
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><circle cx="20" cy="20" r="18" fill="#000"/></svg>'
    responses = {"https://a/missing.png": FakeStream(404, b""),
                 "https://a/broken.png": FakeStream(200, b"not an image"),
                 "https://a/logo.svg": FakeStream(200, svg, "image/svg+xml")}
    monkeypatch.setattr(card.requests, "get", lambda url, **k: responses[url])
    img = card.load_logo(list(responses))
    assert img is not None and img.mode == "RGBA" and img.height > 400   # svg rasterised at 512, padding trimmed


def test_logo_tile_contrast():
    import card
    from PIL import Image
    dark = Image.new("RGBA", (100, 100), (0, 0, 0, 0)); dark.paste((10, 10, 10, 255), (20, 20, 80, 80))
    light = Image.new("RGBA", (100, 100), (0, 0, 0, 0)); light.paste((250, 250, 250, 255), (20, 20, 80, 80))
    assert card.logo_tile(dark).getpixel((5, 60))[:3] == (230, 237, 243)    # dark logo -> light tile
    assert card.logo_tile(light).getpixel((5, 60))[:3] == (22, 27, 34)      # light logo -> dark tile
    wide = card.logo_tile(Image.new("RGBA", (1000, 100), (200, 0, 0, 255)))  # opaque, wide
    assert wide.width <= 360 and wide.height <= 176 and wide.getpixel((0, 0))[3] == 0   # rounded corner


def test_card_with_logo(tmp_path, monkeypatch):
    import card
    from PIL import Image
    monkeypatch.setattr(card.requests, "get", lambda url, **k: FakeStream(200, _png_bytes((255, 0, 0, 255))))
    r = {**repo("o/a-repo-with-a-pretty-long-name-indeed"), "logo_urls": ["https://a/logo.png"]}
    img = Image.open(card.render_card(r, str(tmp_path / "c.png"), handle=""))
    assert img.size == (1600, 900) and img.getpixel((1460, 150)) == (255, 0, 0)   # logo drawn top right


def test_short_description_and_noassertion():
    assert score.reject_reason(repo("x/chat-demo", desc="Chat Demo."), {}, NOW) == "description too short"
    r = repo("x/tool", desc="A tool that does useful things")
    r["license"] = "NOASSERTION"
    assert "NOASSERTION" not in draft.fallback_post(r)


def test_caption_prompt_and_retry_threshold(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    sent, replies = [], iter(["Hook one that is fine.\nrepo does X.\n---\n" + "y" * 170,
                              "Short hook.\nrepo does X.\nby @maker\n---\nOther angle.\nby @maker"])

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return FakeResp(data={"choices": [{"message": {"content": next(replies)}}]})
    monkeypatch.setattr(draft.requests, "post", fake_post)
    out = draft.draft_post(repo("x/tool"), "readme", "maker")
    assert len(sent) == 2                                   # 170 chars > 160 -> one shorter rewrite
    assert "120 characters" in sent[0]["messages"][0]["content"]
    assert '"by @maker"' in sent[0]["messages"][0]["content"]
    assert out["text"] == "Short hook.\nrepo does X.\nby @maker" and out["alt"] == "Other angle.\nby @maker"


# ---------- LLM resilience ----------

GEMINI_MODELS = ["models/gemini-2.5-flash", "models/gemini-2.5-flash-lite", "models/gemini-2.5-pro",
                 "models/gemini-3-flash-preview", "models/text-embedding-004", "models/gemini-2.5-flash-preview-tts",
                 "models/imagen-4.0-generate-001", "models/gemini-flash-latest"]
GROQ_MODELS = ["llama-3.1-8b-instant", "whisper-large-v3", "meta-llama/llama-guard-4-12b",
               "llama-3.3-70b-versatile", "openai/gpt-oss-120b"]


def test_pick_model():
    assert draft.pick_model(GEMINI_MODELS) == "gemini-flash-latest"
    assert draft.pick_model([m for m in GEMINI_MODELS if "latest" not in m]) == "gemini-2.5-flash"
    assert draft.pick_model(GROQ_MODELS) == "openai/gpt-oss-120b"
    assert draft.pick_model(["text-embedding-004"]) is None


def _llm_env(monkeypatch, backup=False):
    monkeypatch.setattr(config, "LLM_API_KEY", "main-key")
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://main/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "gemini-old")
    monkeypatch.setattr(config, "LLM2_API_KEY", "backup-key" if backup else "")
    monkeypatch.setattr(config, "LLM2_BASE_URL", "https://backup/v1")
    monkeypatch.setattr(config, "LLM2_MODEL", "")
    monkeypatch.setattr(draft, "_picked", {})


def _ok(text):
    return FakeResp(data={"choices": [{"message": {"content": text}}]})


def test_unknown_model_is_replaced_automatically(monkeypatch):
    _llm_env(monkeypatch)
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json["model"])
        if json["model"] == "gemini-old":
            return FakeResp(404, {"error": {"message": "models/gemini-old is not found for API version v1beta"}})
        return _ok("hello")
    monkeypatch.setattr(draft.requests, "post", fake_post)
    monkeypatch.setattr(draft.requests, "get", lambda url, headers=None, timeout=None:
                        FakeResp(data={"data": [{"id": m} for m in GEMINI_MODELS]}))
    assert draft.llm_chat([{"role": "user", "content": "hi"}]) == "hello"
    assert calls == ["gemini-old", "gemini-flash-latest"]
    assert draft.llm_chat([{"role": "user", "content": "again"}]) == "hello"   # remembered for the run
    assert calls[-1] == "gemini-flash-latest"


def test_backup_provider_takes_over(monkeypatch):
    _llm_env(monkeypatch, backup=True)
    seen = []

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.append((url, headers["Authorization"], json["model"]))
        if url.startswith("https://main"):
            return FakeResp(400, {"error": {"message": "API key not valid. Please pass a valid API key.",
                                            "status": "INVALID_ARGUMENT", "reason": "API_KEY_INVALID"}})
        return _ok("from groq")
    monkeypatch.setattr(draft.requests, "post", fake_post)
    monkeypatch.setattr(draft.requests, "get", lambda url, headers=None, timeout=None:
                        FakeResp(data={"data": [{"id": m} for m in GROQ_MODELS]}))
    assert draft.llm_chat([{"role": "user", "content": "hi"}]) == "from groq"
    assert seen[0][:2] == ("https://main/v1/chat/completions", "Bearer main-key")   # key error: no model re-pick
    assert seen[-1] == ("https://backup/v1/chat/completions", "Bearer backup-key", "openai/gpt-oss-120b")
    assert len(seen) == 2


def test_all_providers_fail_records_error(monkeypatch):
    _llm_env(monkeypatch)
    monkeypatch.setattr(draft.requests, "post", lambda *a, **k: FakeResp(400, {"error": {"message": "API key not valid"}}))
    with pytest.raises(draft.LLMError):
        draft.llm_chat([])
    assert "main: HTTP 400" in draft.LAST_ERROR and "API key" in draft.explain(draft.LAST_ERROR)


def test_explain_hints():
    assert "LLM_API_KEY secret" in draft.explain("LLM_API_KEY not set")
    assert "new key" in draft.explain('HTTP 400: {"reason": "API_KEY_INVALID"}')
    assert "Free-tier limit" in draft.explain("HTTP 429: RESOURCE_EXHAUSTED")
    assert "model name" in draft.explain("HTTP 404: model not found")
    assert "region" in draft.explain("HTTP 400: User location is not supported for the API use. FAILED_PRECONDITION")


# ---------- diagnostics ----------

def test_llm_failure_banner(monkeypatch, tmp_path):
    monkeypatch.setattr(draft, "LAST_ERROR", 'main: HTTP 400: {"reason": "API_KEY_INVALID"}')
    picks = [{"used_llm": False}, {"used_llm": True}]
    notice = main.llm_notice(picks)
    assert "1/2 repos" in notice and "API_KEY_INVALID" in notice and "new key" in notice
    assert main.llm_notice([{"used_llm": True}]) is None
    msgs = notify.build_daily_messages([], "d", card_dir=str(tmp_path), notice=notice)
    assert notice in msgs[0]["content"]


def test_doctor_report(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord/webhook")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    _llm_env(monkeypatch, backup=True)

    class S:
        def get(self, url, timeout=None):
            return FakeResp(data={"resources": {"core": {"remaining": 990, "limit": 1000}}})
    monkeypatch.setattr(main, "gh_session", lambda token: S())

    def fake_post(url, json=None, headers=None, timeout=None):
        if url.startswith("https://main"):
            return FakeResp(400, {"error": {"message": "API key not valid", "reason": "API_KEY_INVALID"}})
        return _ok("OK")
    monkeypatch.setattr(draft.requests, "post", fake_post)
    monkeypatch.setattr(draft.requests, "get", lambda url, headers=None, timeout=None:
                        FakeResp(data={"data": [{"id": m} for m in GROQ_MODELS]}))
    sent = []
    monkeypatch.setattr(main, "send", lambda url, msgs, dry_run=False: sent.append((url, msgs, dry_run)))

    def net(url, headers=None, timeout=None):     # main and draft share the requests module
        if "reddit" in url:
            raise main.requests.ConnectionError("403 blocked")
        if url.endswith("/models"):
            return FakeResp(data={"data": [{"id": m} for m in GROQ_MODELS]})
        return FakeResp(data={"hits": []})
    monkeypatch.setattr(main.requests, "get", net)
    problems = main.run_doctor()
    report = sent[0][1][0]["content"]
    assert problems == 0 and sent
    assert "⚠️ Main AI failed" in report and "Fix: The API key is wrong" in report and "Covered" in report
    assert "✅ Backup AI works: model `openai/gpt-oss-120b`" in report
    assert "✅ Post images render" in report and "✅ GitHub API works (WITHOUT token: 990/1000" in report
    assert "All good" in report
    assert "✅ Hacker News reachable" in report and "ℹ️ Reddit not reachable" in report


def test_crash_alert_posts_and_reraises(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord/webhook")
    posted = []
    monkeypatch.setattr(main.requests, "post", lambda url, json=None, timeout=None: posted.append(json))

    def boom(dry_run=False):
        raise RuntimeError("something broke")
    monkeypatch.setattr(main, "run_daily", boom)
    with pytest.raises(RuntimeError):
        main.main(["daily"])
    assert "repo-hunter crashed" in posted[0]["content"] and "something broke" in posted[0]["content"]
    assert posted[0]["allowed_mentions"] == {"parse": []}


# ---------- categories, alternatives, variety (real descriptions) ----------

def _r(name, desc, topics=()):
    r = repo(name, desc=desc)
    r["topics"] = list(topics)
    return r


def test_alternative_to_real_descriptions():
    cases = {
        "Private AI meeting notes that stay on your computer. An alternative to Granola, Otter and Fireflies.":
            "Granola, Otter and Fireflies",
        "Open source canva alternative": "Canva",
        "Free, open-source IDM alternative for Windows, macOS and Linux": "IDM",
        "A small, fast container runtime for macOS, Windows and Linux with free Ngrok alternative": "Ngrok",
        "A free alternative for Windows users who need fast downloads": None,
        "An alternative way to browse files": None,
        "Fast JSON parser": None,
    }
    for desc, want in cases.items():
        assert score.alternative_to(_r("x/y", desc)) == want, desc
    assert score.alternative_to(_r("x/y", "Download manager", ["idm-alternative"])) == "IDM"
    assert score.alternative_to(_r("x/y", "Meeting notes", ["granola-alternative"])) == "Granola"


def test_categorize_real_descriptions():
    assert score.categorize(_r("x/zb", "Free, offline checks for known mercenary spyware on Windows, Mac, Linux, "
                                       "iPhone and Android")) == "Security"
    assert score.categorize(_r("x/kiri", "A small, fast container runtime for macOS, Windows and Linux")) == "Dev tool"
    assert score.categorize(_r("x/wordcraft", "an open-source, clean-room word processor in pure Rust")) == "Productivity"
    assert score.categorize(_r("x/net-merge", "Use all your internet connections at once on Windows, macOS, "
                                              "Linux and Android")) == "Desktop app"
    assert score.categorize(_r("x/unslop", "A Claude skill that turns vibecoded UI into a designed interface")) == "AI"
    assert score.categorize(_r("x/thing", "Something else entirely")) == "Other"


def test_variety_rules():
    a1 = {**_r("alice/one", "x y z"), "category": "AI"}
    a2 = {**_r("Alice/two", "x y z"), "category": "Dev tool"}
    b = {**_r("bob/b", "x y z"), "category": "AI"}
    c = {**_r("carol/c", "x y z"), "category": "AI"}
    assert score.variety_block(a2, [a1], set()) == "same owner as another pick today"     # case-insensitive
    assert score.variety_block(c, [a1, b], set(), max_per_category=2) == "already 2 AI picks today"
    assert score.variety_block(c, [a1], {"carol"}) == "owner featured in the last few days"
    assert score.variety_block(c, [a1], set()) is None
    hist = [{"full_name": "carol/old", "date": "2026-11-21"}, {"full_name": "dave/old", "date": "2026-11-10"}]
    assert score.recent_owners(hist, now=NOW, days=3) == {"carol"}


def test_alternative_boost_and_labels(tmp_path):
    plain = repo("x/plain", stars=1000, desc="A fast markdown notes app for everyone")
    alt = repo("y/alt", stars=900, desc="A fast notes app, an alternative to Notion")
    ranked = score.rank([plain, alt], {}, now=NOW)
    assert [r["full_name"] for r in ranked] == ["y/alt", "x/plain"]        # 900 * 1.3 beats 1000
    assert ranked[0]["replaces"] == "Notion" and ranked[0]["category"] == "Productivity"
    item = {**ranked[0], "text": "t", "chars": 1, "warning": None}
    content = notify.build_daily_messages([item], "d", card_dir=str(tmp_path))[1]["content"]
    assert "🏷️ Productivity · alternative to Notion" in content


def test_game_port_filter():
    bad = ["An attempt at running GTA VI on PC", "VERY experimental Nintendo Switch 2 emulator",
           "Static recompilation of The Legend of Zelda: Tears of the Kingdom to native Windows",
           "Xbox Series compatibility layer for Windows - runs console game code natively",
           "Native PC port of P.T. (runs from your own PS4 game files)",
           "Play your own Switch games on iPhone/iPad, one-click macOS app"]
    good = ["20 AI testers on 20 different phones in parallel using Android emulators",
            "Run real Linux kernel natively on iOS with an emulator", "Emulate macOS on iOS and iPadOS",
            "A tiny game engine for making pixel art games in Rust"]
    for d in bad:
        assert score.reject_reason(repo("x/y", desc=d), {}, NOW) == "game port/emulator (copyright & malware risk)", d
    for d in good:
        assert score.reject_reason(repo("x/y", desc=d), {}, NOW) is None, d


# ---------- install command ----------

def test_find_install():
    import card
    r = repo("acme/ripdocs")
    readme = """# ripdocs
## Development
```bash
git clone https://github.com/acme/ripdocs && cd ripdocs
pip install -r requirements.txt
pip install -e .
npm install
```
## Install
```sh
$ pip install ripdocs
brew install acme/tap/ripdocs
```
Or with Docker: `docker run --rm -it -v $(pwd):/data ghcr.io/acme/ripdocs:latest --very-long-flag`
"""
    assert card.find_install(readme, r) == "brew install acme/tap/ripdocs"    # brew preferred, names repo
    assert card.find_install("```\npip install ripdocs\n```", r) == "pip install ripdocs"
    assert card.find_install("```\ncargo install <crate-name>\n```", r) is None           # placeholder
    assert card.find_install("```\nnpm install\npip install -r requirements.txt\n```", r) is None
    assert card.find_install("```\ncurl -fsSL https://x.sh | sh\n```", r) is None        # not offered
    assert card.find_install("```bash\n  npx -y ripdocs init\n```", r) == "npx -y ripdocs init"
    assert card.find_install("> winget install -e --id Acme.RipDocs", r) == "winget install -e --id Acme.RipDocs"
    assert card.find_install("```\ngo install github.com/acme/ripdocs@latest\n```", r) == \
        "go install github.com/acme/ripdocs@latest"
    # a command for another tool (no repo name) loses to one that names the repo
    assert card.find_install("```\nbrew install ffmpeg\nnpm i -g ripdocs\n```", r) == "npm i -g ripdocs"
    assert card.find_install("", r) is None


def test_card_with_install_line(tmp_path):
    import card
    from PIL import Image
    r = {**repo("acme/ripdocs", desc="word " * 100), "install": "brew install acme/tap/ripdocs"}
    img = Image.open(card.render_card(r, str(tmp_path / "c.png"), handle=""))
    assert img.size == (1600, 900)
    # the install box sits between the description and the chips row
    box_row = [img.getpixel((card.PAD + 5, y)) for y in range(560, 740)]
    assert (17, 22, 29) in box_row


def test_reupload_and_fork_ratio():
    up = repo("x/artex", desc="ARTEX - AI pentest system (English translation; upstream: Autumn-27/ARTEX)")
    assert score.reject_reason(up, {}, NOW) == "re-upload or mirror of another repo"
    assert score.reject_reason(repo("x/m", desc="A read-only mirror of the Linux kernel tree"), {}, NOW) == \
        "re-upload or mirror of another repo"
    assert score.reject_reason(repo("x/ok", desc="Upstream-first tooling for package maintainers"), {}, NOW) is None
    farm = repo("x/farm", stars=11780, desc="Plug-in media player for everyone", created="2026-01-01T00:00:00Z")
    farm["forks"] = 9137
    normal = repo("x/normal", stars=18505, desc="An open source word processor", created="2026-01-01T00:00:00Z")
    normal["forks"] = 5142
    flags = {r["full_name"]: r["flag"] for r in score.rank([farm, normal], {}, now=NOW)}
    assert "9,137 forks for 11,780 stars" in flags["x/farm"] and flags["x/normal"] is None


def test_backfill(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM2_API_KEY", "")
    (tmp_path / "seen.json").write_text('{"old/sent": "2026-11-01"}')
    items = [gh_item("old/sent", stars=90000, desc="Already sent before to everyone"),
             gh_item("a/big", stars=50000, desc="A big tool for developers everywhere"),
             gh_item("a/second", stars=40000, desc="Same owner second tool here"),
             gh_item("b/alt", stars=30000, desc="A fast open source alternative to Notion"),
             gh_item("c/small", stars=35000, desc="Another useful tool for builders")]
    calls = []

    class S:
        def get(self, url, params=None, timeout=None, headers=None):
            if url.endswith("/search/repositories"):
                calls.append(params)
                return FakeResp(data={"items": items})
            if url.endswith("/readme"):
                return FakeResp(text="# Readme\n" + "content " * 100 + "\n```\npip install thing\n```")
            if "/users/" in url:
                return FakeResp(data={"twitter_username": None})
            return FakeResp(404)
    monkeypatch.setattr(main, "gh_session", lambda token: S())
    sent = []
    monkeypatch.setattr(main, "send", lambda url, msgs, dry_run=False: sent.append(msgs))
    picks = main.run_backfill(now=NOW, days=50, count=3)
    # seen skipped, one per owner, the Notion alternative boosted (30000*1.3=39000 > 35000)
    assert [p["full_name"] for p in picks] == ["a/big", "b/alt", "c/small"]
    assert calls[0]["q"].startswith("created:>2026-10-04 stars:>400") and calls[0]["per_page"] == 50
    assert "📚 Backfill: the 3 best repos of the last 50 days" in sent[0][0]["content"]
    assert picks[0]["install"] == "pip install thing"
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert set(seen) == {"old/sent", "a/big", "b/alt", "c/small"}
    assert not (tmp_path / "history.json").exists() or json.loads((tmp_path / "history.json").read_text()) == []


def test_search_pages(monkeypatch):
    pages = []

    class S:
        def get(self, url, params=None, timeout=None):
            pages.append(params["page"])
            n = 100 if params["page"] < 2 else 30
            return FakeResp(data={"items": [gh_item(f"o/r{params['page']}-{i}") for i in range(n)]})
    out = collect.search_github(S(), days_back=50, min_stars=400, topics=[], now=NOW, pages=3)
    assert pages == [1, 2] and len(out) == 130


def test_weekly_cover_attached(tmp_path):
    from PIL import Image
    items = [{"full_name": f"o{i}/repo{i}", "description": "A useful tool for people", "stars": 100 * i,
              "language": "Go"} for i in range(1, 6)]
    msgs = notify.build_weekly_messages(["t1", "t2"], "2026-11-29", items=items, card_dir=str(tmp_path))
    assert Image.open(msgs[0]["file"]).size == (1600, 900) and "cover image" in msgs[0]["content"]
    assert [m["content"].split("\n")[0] for m in msgs[1:]] == ["**1/2**", "**2/2**"]
    assert "file" not in notify.build_weekly_messages(["t"], "d")[0]        # no items: no cover


def test_cover_layouts(tmp_path):
    import card
    from PIL import Image
    for n in (1, 2, 4, 5, 6):
        items = [{"full_name": f"o/r{i}", "description": "d e f", "stars": 1} for i in range(n)]
        assert Image.open(card.render_cover(items, str(tmp_path / f"{n}.png"), handle="h")).size == (1600, 900)


def test_github_outage_notice(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "STOPPED_EARLY", False)
    candidates = [collect.normalize(gh_item(f"o{i}/r{i}", stars=1000 - i), "github") for i in range(8)]
    monkeypatch.setattr(main, "collect", lambda s, now=None: candidates)
    monkeypatch.setattr(main, "fetch_readme", lambda s, n: None)
    sent = []
    monkeypatch.setattr(main, "send", lambda url, msgs, dry_run=False: sent.append(msgs))
    main.run_daily(dry_run=True, now=NOW)
    header = sent[0][0]["content"]
    assert "GitHub stopped answering" in header and "No new repos passed" not in header


def test_best_pick_and_reply_block(tmp_path):
    a = {**repo("x/a"), "score": 9, "text": "t", "chars": 1, "warning": None, "homepage": "https://a.dev"}
    b = {**repo("y/b"), "score": 5, "text": "t", "chars": 1, "warning": None}
    msgs = notify.build_daily_messages([a, b], "d", card_dir=str(tmp_path))
    assert "🏆 **best pick today**" in msgs[1]["content"] and "best pick" not in msgs[2]["content"]
    assert "First reply (paste under your post):\n```\nhttps://github.com/x/a\nWebsite: https://a.dev\n```" in msgs[1]["content"]
    assert "```\nhttps://github.com/y/b\n```" in msgs[2]["content"]


def test_weekly_skips_picks_that_todays_filters_reject(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM2_API_KEY", "")
    hist = [{"date": "2026-11-22", "full_name": "x/leonida", "url": "u", "stars": 30481, "score": 9000,
             "description": "Leonida Is A Native PC Recompilation Of The Xbox Version Of Grand Theft Auto 6"},
            {"date": "2026-11-22", "full_name": "y/tool", "url": "u", "stars": 500, "score": 10,
             "description": "A useful command line tool for developers"}]
    (tmp_path / "history.json").write_text(json.dumps(hist))
    monkeypatch.setattr(main, "send", lambda url, msgs, dry_run=False: None)
    tweets = main.run_weekly(now=NOW)
    assert any("tool" in t for t in tweets) and not any("leonida" in t.lower() for t in tweets)


# ---------- overload handling (real Gemini 503 from the doctor report) ----------

GEMINI_503 = ('[{\n  "error": {\n    "code": 503,\n    "message": "This model is currently experiencing high demand. '
              'Spikes in demand are usually temporary. Please try again later.",\n    "status": "UNAVAILABLE"\n  }\n}\n]')


def test_explain_overload_is_not_a_model_name_problem():
    hint = draft.explain(f"gave up after 3 tries (HTTP 503: {GEMINI_503})")
    assert "overloaded" in hint and "model name" not in hint and "key works" in hint
    assert "model name is wrong" in draft.explain('HTTP 404: {"message": "models/gemini-x is not found for API version"}')
    assert "model name is wrong" not in draft.explain("HTTP 400: model output was blocked")


def test_busy_model_switches_to_another(monkeypatch):
    _llm_env(monkeypatch)
    monkeypatch.setattr(config, "LLM_MODEL", "gemini-flash-latest")
    used = []

    def fake_post(url, json=None, headers=None, timeout=None):
        used.append(json["model"])
        if json["model"] == "gemini-flash-latest":
            return FakeResp(503, None, text=GEMINI_503)
        return _ok("caption")
    monkeypatch.setattr(draft.requests, "post", fake_post)
    monkeypatch.setattr(draft.requests, "get", lambda url, headers=None, timeout=None:
                        FakeResp(data={"data": [{"id": m} for m in GEMINI_MODELS]}))
    assert draft.llm_chat([{"role": "user", "content": "hi"}]) == "caption"
    assert used == ["gemini-flash-latest"] * 3 + ["gemini-2.5-flash"]
    assert draft.llm_chat([]) == "caption" and used[-1] == "gemini-2.5-flash"     # sticks for the run


def test_all_models_busy_gives_clear_error(monkeypatch):
    _llm_env(monkeypatch)
    monkeypatch.setattr(config, "LLM_MODEL", "gemini-flash-latest")
    monkeypatch.setattr(draft.requests, "post", lambda *a, **k: FakeResp(503, None, text=GEMINI_503))
    monkeypatch.setattr(draft.requests, "get", lambda url, headers=None, timeout=None:
                        FakeResp(data={"data": [{"id": m} for m in GEMINI_MODELS]}))
    with pytest.raises(draft.LLMError) as e:
        draft.llm_chat([])
    msg = str(e.value)
    assert "HTTP 503" in msg and "also tried gemini-2.5-flash, gemini-3-flash-preview" in msg
    assert "overloaded" in draft.explain(draft.LAST_ERROR)


def test_doctor_mode_exits_green(monkeypatch, capsys):
    monkeypatch.setattr(main, "run_doctor", lambda dry_run=False: 3)
    assert main.main(["doctor"]) is None          # no SystemExit(1) even with problems


def test_failed_provider_is_skipped_for_rest_of_run(monkeypatch):
    _llm_env(monkeypatch, backup=True)
    monkeypatch.setattr(draft, "_down", {})
    monkeypatch.setattr(config, "LLM_MODEL", "gemini-flash-latest")
    hits = {"main": 0, "backup": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        if url.startswith("https://main"):
            hits["main"] += 1
            return FakeResp(503, None, text=GEMINI_503)
        hits["backup"] += 1
        return _ok("from groq")
    monkeypatch.setattr(draft.requests, "post", fake_post)
    monkeypatch.setattr(draft.requests, "get", lambda url, headers=None, timeout=None:
                        FakeResp(data={"data": [{"id": m} for m in (GEMINI_MODELS if "main" in url else GROQ_MODELS)]}))
    assert draft.llm_chat([]) == "from groq"
    first = hits["main"]
    assert first == 9                                   # 3 models x 3 tries, once
    for _ in range(4):
        assert draft.llm_chat([]) == "from groq"
    assert hits["main"] == first and hits["backup"] == 5   # Gemini not retried for the next captions


def test_doctor_warns_instead_of_failing_when_backup_covers(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    _llm_env(monkeypatch, backup=True)
    monkeypatch.setattr(config, "LLM_MODEL", "gemini-flash-latest")

    class S:
        def get(self, url, timeout=None):
            return FakeResp(data={"resources": {"core": {"remaining": 1, "limit": 1}}})
    monkeypatch.setattr(main, "gh_session", lambda token: S())

    def net(url, headers=None, timeout=None):
        if url.endswith("/models"):
            return FakeResp(data={"data": [{"id": m} for m in (GEMINI_MODELS if "main" in url else GROQ_MODELS)]})
        return FakeResp(data={})
    monkeypatch.setattr(main.requests, "get", net)
    monkeypatch.setattr(draft.requests, "post", lambda url, json=None, headers=None, timeout=None:
                        FakeResp(503, None, text=GEMINI_503) if url.startswith("https://main") else _ok("OK"))
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a))))
    problems = main.run_doctor(dry_run=True)
    report = "\n".join(out)
    assert "⚠️ Main AI failed" in report and "Covered: the other AI" in report and "✅ Backup AI works" in report
    assert "also tried gemini-2.5-flash, gemini-3-flash-preview" in report        # no longer cut off
    assert "❌ Main AI" not in report
    assert problems == 1    # the missing Discord webhook in this test; the AI is not counted
