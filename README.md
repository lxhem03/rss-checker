# RSS Torrent Bot

A Pyrogram-based Telegram bot that monitors Nyaa (or any) RSS feeds,
downloads new anime episodes via torrent, and uploads them directly to
your Telegram PM — fully renamed, with duration mapped and a screenshot thumbnail.

---

## Features

| Feature | Detail |
|---|---|
| `/download` | Magnet link / direct .torrent URL / reply to .torrent file |
| `/rssfeed` | Add an RSS feed — bot auto-downloads new episodes |
| `/feeds` | List your active feeds |
| `/removefeed` | Remove a feed |
| `/status` | View ongoing downloads |
| `/cancel` | Cancel a running download |
| Parallel downloads | Configurable via `MAX_PARALLEL_DOWNLOADS` |
| Parallel uploads | Multiple Pyrogram workers via `WORKERS` |
| Duration mapping | ffprobe → mediainfo fallback → seek bar always works |
| Thumbnail | Random screenshot from 10–80 % of video |
| Rename | `{Title} - S01E01.mkv` via regex + anitopy fallback |
| Dedup | MongoDB prevents re-downloading the same episode |
| Access control | Only `AUTH_USERS` in `AUTH_GROUPS` can use commands |

---

## Quick Start (local)

```bash
# 1. Clone and enter
git clone <repo> && cd rss_bot

# 2. Create virtual environment
python -m venv .venv && source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and fill env
cp .env.example .env   # rename the .txt version first
# edit .env with your credentials

# 5. Run
python main.py
```

---

## Deploy to Heroku (Docker stack)

> Heroku Container Registry is used because the bot needs ffmpeg + libtorrent,
> which are not available on standard buildpacks.

```bash
# 1. Login
heroku login
heroku container:login

# 2. Create app (if not done)
heroku create your-app-name

# 3. Set stack to container
heroku stack:set container -a your-app-name

# Rename heroku.yml.txt → heroku.yml
# Rename Dockerfile.txt → Dockerfile
# (done before pushing)

# 4. Set all config vars
heroku config:set API_ID=...        -a your-app-name
heroku config:set API_HASH=...      -a your-app-name
heroku config:set BOT_TOKEN=...     -a your-app-name
heroku config:set AUTH_USERS=...    -a your-app-name
heroku config:set AUTH_GROUPS=...   -a your-app-name
heroku config:set MONGO_URI=...     -a your-app-name
heroku config:set MONGO_DB=rss_bot  -a your-app-name
heroku config:set WORKERS=4         -a your-app-name
heroku config:set MAX_PARALLEL_DOWNLOADS=3 -a your-app-name
heroku config:set RSS_CHECK_INTERVAL=120   -a your-app-name

# 5. Push and release
git push heroku main
heroku ps:scale worker=1 -a your-app-name

# 6. Tail logs
heroku logs --tail -a your-app-name
```

---

## File rename format

```
{title} - S{season:02d}E{episode:02d}.mkv
# e.g.  Oregairu - S02E03.mkv

# No season detected:
{title} - E{episode:02d}.mkv
```

Season/episode is extracted using the regex list in `config.py`, with
anitopy as fallback.

---

## Adding a Nyaa RSS feed

```
/rssfeed https://nyaa.si/?page=rss&q=oregairu&c=0_0&f=0 -title Oregairu
```

The bot will:
1. Save the feed URL + title to MongoDB.
2. Poll every 2 minutes (default).
3. On new entry → download → rename → screenshot → upload to your PM.

---

## Environment variables

See `.env.example` for full list and descriptions.

---

## MongoDB collections

| Collection | Purpose |
|---|---|
| `rss_feeds` | Saved feeds per user, seen GUIDs |
| `downloads` | Completed downloads for deduplication |
