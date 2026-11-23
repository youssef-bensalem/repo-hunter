"""Step 3: turn a repo into a ready-to-post X draft (and the weekly thread)."""
import re
import time

import requests

import config


class LLMError(Exception):
    pass


# ---------- helpers ----------

_URL_RE = re.compile(r"https?://\S+")


def _weight(ch):
    c = ord(ch)
    return 1 if (c <= 4351 or 8192 <= c <= 8205 or 8208 <= c <= 8223 or 8242 <= c <= 8247) else 2


def x_length(text):
    """Character count the way X counts it: links = 23, emoji/CJK/arrows = 2."""
    return sum(_weight(c) for c in _URL_RE.sub("x" * 23, text))


def clean_readme(text, limit=6000):
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)       # images and badges
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)           # links left empty after removing badges
    text = re.sub(r"<[^>]+>", "", text)                    # raw HTML tags
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def _tidy(text):
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def llm_chat(messages, max_tokens=2048, temperature=0.7):
    """Call any OpenAI-compatible chat endpoint (Gemini, Groq, OpenRouter, Ollama...)."""
    if not config.LLM_API_KEY:
        raise LLMError("LLM_API_KEY not set")
    url = f"{config.LLM_BASE_URL}/chat/completions"
    body = {"model": config.LLM_MODEL, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}  # generous: thinking models use tokens too
    last = "unknown error"
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, headers={"Authorization": f"Bearer {config.LLM_API_KEY}"}, timeout=90)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {r.status_code}"
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            content = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise LLMError(f"unexpected response: {r.text[:300]}")
        if not content or not content.strip():
            raise LLMError("empty response")
        return content.strip()
    raise LLMError(f"gave up after 3 tries ({last})")


# ---------- single post ----------

POST_RULES = """You write posts for an X (Twitter) account about {niche}.
The voice: a developer telling a friend about a tool they just found. Specific, plain, a bit dry. Never marketing.

Write TWO different posts about the GitHub repo below, separated by a line containing only ---

Each post:
- Line 1, the hook (max 80 characters). Lead with the concrete outcome or the pain it removes, and name the repo.
  Post 1 states what it does for you. Post 2 uses a different angle: what it replaces, a "X, but Y" comparison,
  or the annoying problem it kills.
- A blank line, then exactly 3 lines starting with "→ ", each one concrete capability (max 55 characters each).
  Prefer specifics from the README: platforms, languages, "runs offline", "no account", "one binary", formats.
- If the README shows a one-line install command of max 40 characters (brew/pip/npm/cargo/docker), you may add
  it as the last line, e.g. "brew install foo". Otherwise skip it.
- {credit}
- No hashtags, no emoji, no links, no markdown, no quotes around the post. Max 250 characters in total.
- Only state facts found in the description or README. Never invent features, numbers, stars or benchmarks.
- Never use: game-changer, revolutionary, insane, mind-blowing, unleash, supercharge, seamless, powerful,
  cutting-edge, elevate, effortless, "look no further", "you need this".

Two examples of the format (invented repos, do not reuse their wording):

ripdocs turns any docs website into one clean Markdown file.

→ Follows links, strips nav and ads
→ Keeps code blocks and tables intact
→ Output ready to paste into an LLM
pip install ripdocs
---
Stop copy-pasting docs into ChatGPT page by page. ripdocs grabs them all.

→ Crawls a whole docs site in one command
→ Strips menus, footers and cookie banners
→ Saves one tidy Markdown file

Reply with the two posts only."""


def _split_variants(raw):
    parts = []
    for part in re.split(r"^\s*-{3,}\s*$", raw, flags=re.M):
        part = _tidy(re.sub(r"^\s*(post|option|variant)?\s*[AB12][:.)]\s+", "", part.strip(), flags=re.I))
        if part:
            parts.append(part)
    return parts[:2]


def _repo_brief(repo, readme):
    return (f"Repo: {repo['full_name']}\n"
            f"Description: {repo['description']}\n"
            f"Language: {repo.get('language') or 'n/a'} | Stars: {repo['stars']} | License: {repo.get('license')}\n"
            f"Topics: {', '.join(repo.get('topics') or []) or 'n/a'}\n"
            f"Homepage: {repo.get('homepage') or 'n/a'}\n\n"
            f"README (excerpt):\n{readme or '(no README)'}")


def fallback_post(repo, handle=None):
    """Template draft used when no LLM is configured or the LLM fails."""
    name = repo["full_name"].split("/", 1)[1]
    desc = repo["description"].rstrip(".")
    lic = repo.get("license") if repo.get("license") not in (None, "NOASSERTION") else "custom"
    tail = [f"→ {repo.get('language') or 'Open source'} · {lic} license",
            f"→ {repo['stars']:,} stars on GitHub"]
    if handle:
        tail.append(f"by @{handle}")

    def build(d):
        return "\n".join([f"{name}: {d}", ""] + tail)

    text = build(desc + ".")
    words = desc.split()
    while x_length(text) > config.POST_MAX_CHARS and len(words) > 3:
        words = words[:-1]
        text = build(" ".join(words) + "…")
    return text


def draft_post(repo, readme, handle=None):
    credit = (f'End the post with "by @{handle}" on its own line (the maintainer).' if handle
              else "Do not mention or tag any person or account.")
    messages = [
        {"role": "system", "content": POST_RULES.format(niche=config.NICHE, credit=credit)},
        {"role": "user", "content": _repo_brief(repo, readme)},
    ]
    warning = None
    try:
        raw = llm_chat(messages)
        variants = _split_variants(raw)
        if not variants:
            raise LLMError("empty draft")
        if any(x_length(v) > config.POST_MAX_CHARS for v in variants):
            lengths = ", ".join(str(x_length(v)) for v in variants)
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"Those are {lengths} characters. Rewrite both under 240 "
                                                     "characters with the same rules and the --- separator."}]
            variants = _split_variants(llm_chat(messages)) or variants
        used_llm = True
    except LLMError as e:
        print(f"[warn] LLM draft failed for {repo['full_name']}: {e}")
        variants, used_llm = [fallback_post(repo, handle)], False
        warning = f"template draft (LLM: {e})"

    text, alt = variants[0], (variants[1] if len(variants) > 1 else None)
    chars = x_length(text)
    too_long = [label for label, v in (("A", text), ("B", alt)) if v and x_length(v) > config.POST_MAX_CHARS]
    if too_long:
        warning = (warning + "; " if warning else "") + f"option {'/'.join(too_long)} over 280 chars, trim before posting"
    return {"text": text, "alt": alt, "chars": chars, "used_llm": used_llm, "warning": warning}


# ---------- weekly thread ----------

THREAD_RULES = """You write X (Twitter) threads about {niche}.
Write a thread presenting the {n} repos below. Rules:
- Tweet 1: a hook like "{n} open-source tools I found this week 🧵" (adapt the wording, max 200 characters).
- Then exactly one tweet per repo, in the given order: repo name, what it does, why it's useful.
  Max 250 characters each. End each repo tweet with the repo URL.
- Last tweet: a short line inviting people to follow for one useful repo a day (max 150 characters).
- Plain text, no markdown. Only facts from the information given.
- Separate tweets with a line containing only ---
Reply with the thread only."""


def fallback_thread(items):
    tweets = [f"{len(items)} open-source tools I found this week 🧵"]
    for i, it in enumerate(items, 1):
        name = it["full_name"].split("/", 1)[1]
        tweets.append(f"{i}. {name}\n\n{it['description']}\n\n{it['url']}")
    tweets.append("Follow for one useful open-source repo every day.")
    return tweets


def draft_thread(items):
    brief = "\n\n".join(
        f"{i}. {it['full_name']} ({it['url']})\nDescription: {it['description']}\nDaily post draft: {it.get('text', '')}"
        for i, it in enumerate(items, 1))
    messages = [{"role": "system", "content": THREAD_RULES.format(niche=config.NICHE, n=len(items))},
                {"role": "user", "content": brief}]
    try:
        raw = _tidy(llm_chat(messages, max_tokens=4096))
    except LLMError as e:
        print(f"[warn] LLM thread failed: {e}")
        return fallback_thread(items)
    tweets = [t.strip() for t in re.split(r"^\s*---\s*$", raw, flags=re.M) if t.strip()]
    return tweets if len(tweets) >= 2 else fallback_thread(items)
