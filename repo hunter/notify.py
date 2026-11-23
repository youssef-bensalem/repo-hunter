"""Step 4: send drafts to a Discord channel through a webhook."""
import json
import os
import tempfile
import time

import requests

import card


def og_image(full_name, salt="1"):
    """GitHub's auto-generated preview card for any public repo (good post image)."""
    return f"https://opengraph.githubassets.com/{salt}/{full_name}"


def _code(text):
    # Code block = one-click copy on desktop, long-press copy on mobile.
    return "```\n" + text.replace("```", "'''") + "\n```"


def _make_card(item, card_dir, n):
    try:
        return card.render_card(item, os.path.join(card_dir, f"{n}-{item['full_name'].replace('/', '-')}.png"))
    except Exception as e:  # a broken image must never stop the drafts from being sent
        print(f"[warn] post image failed for {item['full_name']}: {e}")
        return None


def build_daily_messages(items, date, card_dir=None):
    card_dir = card_dir or tempfile.mkdtemp(prefix="cards-")
    header = (f"## 📦 Repo picks for {date}\n"
              "Pick 1–2. Open the repo before posting, save the image, put the GitHub link in the first reply.")
    if not items:
        header += "\n\nNo new repos passed the filters today."
    messages = [{"content": header}]

    for n, it in enumerate(items, 1):
        content = (f"**{n}. {it['full_name']}** · ⭐ {it['stars']:,} · score {it['score']} · {it['chars']}/280\n"
                   f"{_code(it['text'])}\n"
                   f"Reply link: <{it['url']}>")
        if it.get("flag"):
            content += f"\n🚩 {it['flag']}"
        if it.get("warning"):
            content += f"\n⚠️ {it['warning']}"
        embed = {
            "title": it["full_name"][:256],
            "url": it["url"],
            "description": it["description"][:500],
            "color": 0x2EA043,
            "fields": [
                {"name": "Language", "value": it.get("language") or "—", "inline": True},
                {"name": "License", "value": it.get("license") or "—", "inline": True},
                {"name": "Created", "value": it["created_at"][:10], "inline": True},
                {"name": "Found via", "value": ", ".join(it["sources"])[:1024], "inline": True},
            ],
        }
        msg = {"content": content[:2000], "embeds": [embed]}
        path = _make_card(it, card_dir, n)
        if path:
            msg["file"] = path      # attached as a full-size PNG, ready to save and post
        else:
            embed["image"] = {"url": og_image(it["full_name"], date)}
        messages.append(msg)
    return messages


def build_weekly_messages(tweets, date):
    if not tweets:
        return [{"content": f"## 🧵 Weekly thread for {date}\nNo picks were sent this week, so there is no thread."}]
    messages = [{"content": f"## 🧵 Weekly thread for {date}\nPost these in order as one thread."}]
    for i, tweet in enumerate(tweets, 1):
        messages.append({"content": f"**{i}/{len(tweets)}**\n{_code(tweet)}"[:2000]})
    return messages


def send(webhook_url, messages, dry_run=False):
    for msg in messages:
        if dry_run:
            print(msg["content"])
            for e in msg.get("embeds", []):
                print(f"   [embed] {e['title']}" + (f" | image: {e['image']['url']}" if "image" in e else ""))
            if msg.get("file"):
                print(f"   [image] {msg['file']}")
            print()
            continue
        path = msg.get("file")
        payload = {k: v for k, v in msg.items() if k != "file"}
        payload["allowed_mentions"] = {"parse": []}  # never ping @everyone from README text
        for _ in range(5):
            if path:
                with open(path, "rb") as fh:
                    r = requests.post(webhook_url, data={"payload_json": json.dumps(payload)},
                                      files={"files[0]": (os.path.basename(path), fh, "image/png")}, timeout=60)
            else:
                r = requests.post(webhook_url, json=payload, timeout=30)
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                time.sleep(wait + 0.5)
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError("Discord kept rate-limiting the webhook; gave up")
        time.sleep(1)
