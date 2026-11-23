"""Step 4: send drafts to a Discord channel through a webhook."""
import time

import requests


def og_image(full_name, salt="1"):
    """GitHub's auto-generated preview card for any public repo (good post image)."""
    return f"https://opengraph.githubassets.com/{salt}/{full_name}"


def _code(text):
    # Code block = one-click copy on desktop, long-press copy on mobile.
    return "```\n" + text.replace("```", "'''") + "\n```"


def build_daily_messages(items, date):
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
            "image": {"url": og_image(it["full_name"], date)},
        }
        messages.append({"content": content[:2000], "embeds": [embed]})
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
                print(f"   [embed] {e['title']} | image: {e['image']['url']}")
            print()
            continue
        payload = {**msg, "allowed_mentions": {"parse": []}}  # never ping @everyone from README text
        for _ in range(5):
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
