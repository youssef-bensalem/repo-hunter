# Changes

## Update 4
- **doctor mode**: Actions → repo-hunter → Run workflow → `doctor`. Checks the Discord webhook, GitHub API,
  each AI provider (with a real test request), image rendering and history, then posts a ✅/❌ report with
  the fix for each problem to Discord.
- **AI failure banner**: if captions fall back to the template, the day's first Discord message says why
  (the real error) and how to fix it.
- **Crash alerts**: if a run crashes, the error is posted to Discord.
- **Model auto-pick**: if the model name is wrong or retired, the bot asks the provider for its model list and
  picks a good one. Remove the `LLM_MODEL` variable to always let it choose.
- **Backup AI**: optional second provider, used when the first fails. Default is Groq's free tier: create a key
  at console.groq.com and add it as the `LLM2_API_KEY` secret. Nothing else needed.

## Update 5
- **No more game-port bait**: GTA 6 "ports", console emulators and game "recompilations" are skipped
  (copyright and malware risk). On the day this was built they were 4 of the top 5 picks. Turn off with the
  variable `SKIP_GAME_PORTS=false`.
- **Variety**: at most one repo per owner per day, no owner again within 3 days (`OWNER_COOLDOWN_DAYS`), and at
  most 2 repos of the same category per day (`MAX_PER_CATEGORY`).
- **"Alternative to X" boost**: repos that say they replace a product (Canva, Notion, IDM…) get +30% score
  (`ALTERNATIVE_BOOST`), and the AI is told to use it as the hook.
- **Labels in Discord**: each draft shows its category and what it replaces, e.g. `🏷️ Design & media · alternative to Canva`.

## Update 6
- **Install command on the image**: if the README has a short install line (`brew install x`, `pip install x`,
  `cargo install x`, `npx x`, `winget install x`…), it appears on the image as a terminal line. Skips dev-setup
  commands (`pip install -r requirements.txt`, `npm install`) and placeholders.
- **Backfill mode**: Actions → Run workflow → mode `backfill`, set days and count. Sends the best repos of that
  period (by stars, "alternative to X" boosted) to Discord with images, captions and logos, and marks them as
  sent so the daily run never repeats them.
- **Weekly thread cover**: the Sunday thread now comes with a cover image of the week's picks, numbered, for
  the first tweet.
- **Re-uploads skipped**: repos whose description says they are a mirror, translation or re-upload of another repo.
- **Fork-farm warning**: 🚩 when a repo has more than one fork for every two stars (often bot accounts).
- Workflow timeout raised to 30 minutes (backfill of 20 repos takes a few minutes).

## Update 7
- 🏆 marks the best pick of the day.
- Each draft has a **first reply** block (GitHub link, plus the website if the repo has one) ready to paste.
- `doctor` also checks whether Hacker News and Reddit are reachable (Reddit often blocks GitHub's servers;
  that's fine, GitHub search still works).
- If GitHub stops answering mid-run, the Discord header says so instead of "no repos passed the filters".
- The Sunday thread re-checks past picks against the current filters, so the GTA 6 repos sent on Nov 23-24
  (before the game-port filter existed) never make it into a thread.
- README rewritten for all modes and settings.
