# RARUnpacker

A self-hosted Docker container for automatically extracting split RAR archives — built for Unraid but works anywhere. Features a sleek web UI with a file browser, job queue, watch folders, scheduled scanning, real-time progress, and full log visibility.

---

## Features

- **File Browser** — Navigate your mounted volumes, see which folders contain RAR sets, queue them directly
- **Job Queue** — Real-time extraction progress, ETA, per-job post-action (keep / delete / move to trash)
- **Watch Folders** — Automatically detect and extract new RARs as they appear (via filesystem events)
- **Scheduler** — Periodic background scan of all watch folders (configurable interval)
- **Handles Split RARs** — Supports both `archive.part01.rar` and legacy `archive.rar + .r00 .r01` formats
- **RAR5 Support** — Uses the official `unrar` binary (not the hobbled `unrar-free`)
- **Password Support** — Per-folder RAR passwords
- **Incomplete Archive Detection** — Skips and logs an error if parts are missing before attempting extraction
- **Mark as Extracted** — Flag a watch folder so the scheduler skips it permanently
- **Dark / Light Mode** — Toggle in the sidebar
- **Live Logs** — Filterable log viewer, all activity recorded to SQLite
- **WebSocket Updates** — Progress bars and status badges update in real time without polling

---

## Quick Start

### Docker Compose

```yaml
services:
  rarunpacker:
    build: .
    container_name: rarunpacker
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - /mnt/user/downloads:/data/downloads
      - /mnt/user/media:/data/media
      - /mnt/user/appdata/rarunpacker:/config
    environment:
      DATA_PATH: /data
      CONFIG_PATH: /config
```

Then:

```bash
docker compose up -d --build
```

Open `http://<your-server-ip>:8080` in your browser.

---

### Unraid Setup

1. In Unraid, go to **Community Applications** and search for a custom container, or use the **Add Container** button in the Docker tab.
2. Set the repository to your built image or use `ghcr.io/youruser/rarunpacker:latest`.
3. Add the following volume mappings:

| Container Path | Host Path | Description |
|---|---|---|
| `/data` | `/mnt/user/downloads` | Your downloads root (add more as needed) |
| `/config` | `/mnt/user/appdata/rarunpacker` | Persistent config + database |

4. Add environment variables:

| Variable | Value | Description |
|---|---|---|
| `DATA_PATH` | `/data` | Root shown in file browser |
| `CONFIG_PATH` | `/config` | Config/database location |
| `PORT` | `8080` | Web UI port |

5. Set **WebUI** to `http://[IP]:[PORT:8080]`
6. Click **Apply**.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATA_PATH` | `/data` | Default root path for the file browser |
| `CONFIG_PATH` | `/config` | Where `rarunpacker.db` and settings are stored |
| `PORT` | `8080` | Port the web server listens on |

---

## Settings (in-app)

| Setting | Default | Description |
|---|---|---|
| Scan Interval | 30 min | How often the scheduler re-scans watch folders |
| Max Concurrent Extractions | 1 | How many RARs to extract simultaneously |
| Default Post-Extraction Action | Keep | What to do with RAR files after successful extraction |
| Trash Folder | `/config/trash` | Destination when post-action is "Move to trash" |

---

## Extraction Logic

### RAR Set Detection

The tool only queues the **first part** of each set:

| Format | First part detected |
|---|---|
| `movie.rar` + `movie.r00`, `movie.r01`… | `movie.rar` |
| `movie.part1.rar`, `movie.part2.rar`… | `movie.part1.rar` |
| `movie.part01.rar`, `movie.part02.rar`… | `movie.part01.rar` |
| Single `movie.rar` | `movie.rar` |

### Incomplete Archives

Before extraction begins, the tool runs `unrar l` to verify all volumes are present. If any part is missing, the job is marked **Failed** with the missing filename logged — no partial extraction is attempted.

### Progress Estimation

Progress is calculated by comparing the size of files written to the output folder vs the total declared uncompressed size in the RAR headers. This is more reliable than parsing `unrar`'s stdout, which varies between versions.

### Post-Extraction Actions (per job)

- **Keep** — Leave all `.rar` / `.r00` etc. files in place
- **Delete** — Remove all parts of the set immediately
- **Move to trash** — Move all parts to the configured trash folder

---

## Watch Folder Automation

When you add a **Watch Folder**, the container:

1. **Immediately** registers a filesystem watcher (via `watchdog`) on that path
2. When a new `.rar` file lands, waits **15 seconds** for the file to stabilise (size unchanged), then queues it
3. **Also** scans the folder on the configured schedule (default every 30 min) to catch anything missed

### Marking as Extracted

Click **Mark Extracted** on any watch folder to tell the scheduler to permanently skip it. Useful for archive folders you've already processed. You can unmark at any time.

---

## API

The backend exposes a REST API. All endpoints are prefixed with `/api`.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/files/browse?path=…` | List directory contents |
| `GET` | `/api/jobs` | List jobs (`?status=…&limit=…`) |
| `POST` | `/api/jobs` | Queue a path for extraction |
| `POST` | `/api/jobs/{id}/cancel` | Cancel a job |
| `POST` | `/api/jobs/{id}/retry` | Retry a failed/cancelled job |
| `DELETE` | `/api/jobs/{id}` | Delete a job from history |
| `GET` | `/api/folders` | List watch folders |
| `POST` | `/api/folders` | Add a watch folder |
| `PATCH` | `/api/folders/{id}` | Update a watch folder |
| `DELETE` | `/api/folders/{id}` | Remove a watch folder |
| `POST` | `/api/folders/{id}/scan` | Trigger immediate scan |
| `POST` | `/api/folders/{id}/mark-extracted` | Toggle extracted flag |
| `POST` | `/api/folders/scan-all` | Scan all watch folders now |
| `GET` | `/api/settings` | Get settings |
| `PUT` | `/api/settings` | Update settings |
| `GET` | `/api/logs` | Get log entries |
| `DELETE` | `/api/logs` | Clear logs |

WebSocket available at `ws://<host>/ws` — receives `new_job`, `job_update`, and `job_progress` events.

---

## File Structure

```
rarunpacker/
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── requirements.txt
├── README.md
└── app/
    ├── main.py               # FastAPI app, WebSocket endpoint, lifespan
    ├── config.py             # Env var config
    ├── database.py           # SQLAlchemy engine + session helpers
    ├── models.py             # DB models (Job, WatchedFolder, LogEntry, AppSetting)
    ├── ws_manager.py         # WebSocket broadcast manager
    ├── services/
    │   ├── extractor.py      # RAR discovery, integrity check, extraction, post-actions
    │   ├── queue_manager.py  # Async job queue with concurrency control
    │   ├── watcher.py        # watchdog filesystem monitor
    │   └── scheduler.py      # APScheduler periodic scanner
    ├── routers/
    │   ├── files.py          # File browser API
    │   ├── jobs.py           # Job CRUD + actions
    │   ├── folders.py        # Watch folders CRUD
    │   ├── settings.py       # App settings
    │   └── logs.py           # Log access
    └── static/
        └── index.html        # Full single-file frontend (vanilla JS, no CDN deps)
```

---

## Notes

- **No authentication** — designed for trusted LAN use. Place behind a reverse proxy (e.g. Nginx Proxy Manager / Traefik) with auth if you need external access.
- The SQLite database is stored at `$CONFIG_PATH/rarunpacker.db`. Back up this file to preserve your job history and settings.
- Extracting is always done **in-place** — files are written to the same directory as the RAR set.
- The container uses a single uvicorn worker. This is intentional — SQLite + asyncio works best single-process.
