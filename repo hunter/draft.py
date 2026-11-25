"""Step 3: turn a repo into a ready-to-post X draft (and the weekly thread)."""
import re
import time

import requests

import config


CAPTION_RETRY_CHARS = 160   # ask for a shorter rewrite above this


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


# ---------- LLM providers ----------

def providers():
    """The main LLM and, if configured, the backup one (tried in this order)."""
    out = []
    for label, key, base, model in (("main", config.LLM_API_KEY, config.LLM_BASE_URL, config.LLM_MODEL),
                                    ("backup", config.LLM2_API_KEY, config.LLM2_BASE_URL, config.LLM2_MODEL)):
        if key:
            out.append({"label": label, "key": key, "base": base.rstrip("/"), "model": model})
    return out


_picked = {}          # base url -> model chosen automatically this run
LAST_ERROR = None     # last LLM error of this run, for the Discord banner and the doctor report

_SKIP_MODEL = re.compile(r"embed|tts|audio|image|imagen|veo|live|guard|whisper|transcri|moderation|search|realtime|"
                         r"robotics|computer-use|deep-research|aqa|learnlm|gemma-3n|nano", re.I)


def _model_rank(model_id):
    nums = tuple(int(n) for n in re.findall(r"\d+", model_id)[:3])
    return (0 if re.search(r"preview|exp", model_id, re.I) else 1,
            1 if "latest" in model_id else 0, nums)


def pick_model(ids):
    """Choose a good, cheap chat model from a /models list."""
    ids = [i.removeprefix("models/") for i in ids if not _SKIP_MODEL.search(i)]
    for pattern in (r"flash(?!.*lite)", r"flash", r"gpt-oss-120b|llama-?3\.3-70b|llama.*70b|qwen.*32b|kimi",
                    r"llama|qwen|gpt-oss|mistral|gemma", r"."):
        found = [i for i in ids if re.search(pattern, i, re.I)]
        if found:
            return max(found, key=_model_rank)
    return None


def list_models(p):
    r = requests.get(f"{p['base']}/models", headers={"Authorization": f"Bearer {p['key']}"}, timeout=30)
    r.raise_for_status()
    return [m.get("id", "") for m in r.json().get("data", [])]


def explain(error):
    """Plain-English fix for an LLM error."""
    e = str(error)
    if "not set" in e:
        return "Add the LLM_API_KEY secret: repo Settings → Secrets and variables → Actions."
    if re.search(r"API.?KEY.?INVALID|API key not valid|invalid.?api.?key|HTTP 401|HTTP 403|PERMISSION_DENIED", e, re.I):
        return ("The API key is wrong or disabled. Create a new key (aistudio.google.com → Get API key), "
                "then replace the LLM_API_KEY secret. Paste only the key, no spaces or quotes.")
    if re.search(r"location|region|FAILED_PRECONDITION", e, re.I):
        return "This provider refuses requests from GitHub's servers' region. Add a backup provider (LLM2_API_KEY)."
    if re.search(r"HTTP 429|RESOURCE_EXHAUSTED|quota|rate", e, re.I):
        return "Free-tier limit reached (resets daily). Lower TOP_N or add a backup provider (LLM2_API_KEY)."
    if re.search(r"HTTP 404|not found|does not exist|model", e, re.I):
        return "The model name is wrong. Remove the LLM_MODEL variable to let the bot pick one automatically."
    if re.search(r"HTTP 400", e):
        return "The provider rejected the request. Check LLM_BASE_URL matches the key's provider."
    return "Network or provider problem; it usually fixes itself. Run the doctor mode if it keeps happening."


def _post(p, body):
    return requests.post(f"{p['base']}/chat/completions", json=body,
                         headers={"Authorization": f"Bearer {p['key']}"}, timeout=90)


def _chat_one(p, messages, max_tokens, temperature):
    model = _picked.get(p["base"]) or p["model"]
    if not model:
        model = _picked[p["base"]] = pick_model(list_models(p))
    body = {"model": model, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens}  # generous: thinking models use tokens too
    last, repicked = "unknown error", False
    for attempt in range(3):
        try:
            r = _post(p, body)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code in (400, 404) and not repicked and re.search(r"model", r.text, re.I) \
                and not re.search(r"API.?KEY|api key", r.text, re.I):
            repicked = True             # unknown model name: pick one from the provider's own list
            try:
                new = pick_model(list_models(p))
            except (requests.RequestException, ValueError):
                new = None
            if new and new != model:
                print(f"[info] model {model!r} not available on {p['label']} provider, using {new!r}")
                model = body["model"] = _picked[p["base"]] = new
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


def llm_chat(messages, max_tokens=2048, temperature=0.7):
    """Ask the main LLM; if it fails, the backup one. Any OpenAI-compatible API works."""
    global LAST_ERROR
    ps = providers()
    if not ps:
        LAST_ERROR = "LLM_API_KEY not set"
        raise LLMError(LAST_ERROR)
    errors = []
    for p in ps:
        try:
            return _chat_one(p, messages, max_tokens, temperature)
        except (LLMError, requests.RequestException, ValueError) as e:
            errors.append(f"{p['label']}: {e}")
    LAST_ERROR = " | ".join(errors)
    raise LLMError(LAST_ERROR)


# ---------- single post ----------

POST_RULES = """You write captions for an X (Twitter) account about {niche}.
Each post is an image that already shows the repo name, description, stars, language and license.
The caption sits above the image. Its only job is to make a developer stop scrolling.

Write TWO captions for the GitHub repo below, separated by a line containing only ---

Each caption:
- 1 or 2 short lines, at most 120 characters in total. Short beats complete.
- Line 1 is the hook: a pain everyone knows, a surprising result, or the paid thing it replaces.
- Line 2 (optional) names the repo and the payoff in plain words.
- Caption 1 leads with what you get. Caption 2 takes another angle: the pain it kills,
  what it replaces, or "X, but Y".
- Talk like a developer talking to a friend: simple words, concrete, a bit dry.
- {credit}
- No hashtags, emoji, links, markdown or quotes. Never start with "Introducing" or a question
  like "Tired of...?".
- Only facts from the description or README. Never invent numbers, stars or benchmarks.
- Never use: game-changer, revolutionary, insane, mind-blowing, unleash, supercharge, seamless,
  powerful, cutting-edge, elevate, effortless, "look no further", "you need this".

Examples of the style (invented repos, do not reuse their wording):

Stop pasting docs into ChatGPT page by page.
ripdocs turns a whole docs site into one Markdown file.
---
Your AI can finally read the entire docs.
ripdocs crawls the site and hands you one clean file.

Reply with the two captions only."""


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
            f"{'It says it replaces: ' + repo['replaces'] + ' (a strong hook)' + chr(10) if repo.get('replaces') else ''}"
            f"Homepage: {repo.get('homepage') or 'n/a'}\n"
            f"Install command (shown on the image): {repo.get('install') or 'none'}\n\n"
            f"README (excerpt):\n{readme or '(no README)'}")


def fallback_post(repo, handle=None):
    """Short template caption used when no LLM is configured or the LLM fails."""
    name = repo["full_name"].split("/", 1)[1]
    desc = repo["description"].strip().rstrip(".")
    first = re.split(r"(?<=[.!?])\s", desc)[0].rstrip(".")   # first sentence only
    words = first.split()
    line = f"{name}: {first}."
    while x_length(line) > 120 and len(words) > 3:
        words = words[:-1]
        line = f"{name}: {' '.join(words)}…"
    return line + (f"\nby @{handle}" if handle else "")


def draft_post(repo, readme, handle=None):
    credit = (f'Add "by @{handle}" (the maintainer) as an extra last line; it does not count toward the 120.' if handle
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
        if any(x_length(v) > CAPTION_RETRY_CHARS for v in variants):
            lengths = ", ".join(str(x_length(v)) for v in variants)
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"Those are {lengths} characters. Make both shorter: at most "
                                                     "120 characters each plus the credit line, same rules, "
                                                     "same --- separator."}]
            variants = _split_variants(llm_chat(messages)) or variants
        used_llm = True
    except LLMError as e:
        print(f"[warn] LLM draft failed for {repo['full_name']}: {e}")
        variants, used_llm = [fallback_post(repo, handle)], False
        warning = "template draft: the AI failed (reason at the top of today's messages)"

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
