# UnrarTool

A self-hosted Docker container for automatically extracting split RAR archives — built for Unraid but works anywhere. Features a sleek web UI with a file browser, job queue, watch folders, scheduled scanning, real-time progress, and full log visibility.

---

## Features

- **File Browser** — Navigate your mounted volumes with filter, sort, and multi-select support
- **Filter & Sort** — Filter by name, type (All / Folders / RAR Only / Files / Not Done), sort by name, date, size, or RAR count
- **Multi-Select** — Check multiple folders and queue or mark them all at once
- **Job Queue** — Real-time extraction progress bar and ETA driven directly from `unrar`'s stdout
- **Watch Folders** — Automatically detect and extract new RARs as they appear (filesystem events)
- **Scheduler** — Periodic background scan of all watch folders (configurable interval)
- **Smart Exclusions** — Auto-excluded after every successful extraction. Manual "✓ Mark Done" button always visible in the file browser
- **Force Re-extract** — Override any exclusion when you need to re-run
- **RAR5 + Split RAR** — Supports `.part01.rar` and legacy `.rar + .r00/.r01` formats
- **Password Support** — Per-folder RAR passwords for encrypted archives
- **Incomplete Archive Detection** — Skips and logs if parts are missing before attempting extraction
- **Dark / Light Mode** — Toggle in the sidebar
- **Live Logs** — Filterable log viewer, all activity recorded to SQLite
- **WebSocket Updates** — Progress bars and status badges update in real time

---

## Quick Start

### Docker Compose

```yaml
services:
  unrartool:
    image: hythamjurdi/unrartool:latest
    container_name: unrartool
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - /mnt/user/downloads:/data/downloads
      - /mnt/user/media:/data/media
      - /mnt/user/appdata/unrartool:/config
    environment:
      DATA_PATH: /data
      CONFIG_PATH: /config
```

Open `http://<your-server-ip>:8080`.

---

### Unraid Setup

1. In Unraid → Docker tab → **Add Container**
2. At the top where it says **Template**, paste:
   ```
   https://raw.githubusercontent.com/hythamjurdi/unrartool/main/unraid/unrartool.xml
   ```
3. Verify your paths and click **Apply**

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATA_PATH` | `/data` | Default root path for the file browser |
| `CONFIG_PATH` | `/config` | Where `unrartool.db` and settings are stored |
| `PORT` | `8080` | Port the web server listens on |
| `PUID` | `99` | User ID for file ownership |
| `PGID` | `100` | Group ID for file ownership |

---

## Settings (in-app)

| Setting | Default | Description |
|---|---|---|
| Scan Interval | 30 min | How often the scheduler re-scans watch folders |
| Max Concurrent Extractions | 1 | How many RARs to extract simultaneously |
| Default Post-Extraction Action | Keep | What to do with RAR files after successful extraction |
| Trash Folder | `/config/trash` | Destination when post-action is "Move to trash" |

---

## File Browser

### Filter Options
| Filter | Shows |
|---|---|
| All | Everything in the current folder |
| Folders | Directories only |
| RAR Only | Directories that contain at least one RAR set |
| Files | Non-directory files only |
| Not Done | Directories not yet marked as excluded |

### Sort Options
- Name A→Z / Z→A
- Newest First / Oldest First (by modified date)
- Size Large→Small / Small→Large
- Most RAR Sets first
- Type (Folders first, then files)

### Multi-Select
- Check any folder's checkbox to select it
- Use **Select All** at the top to select all visible folders at once
- A floating action bar appears at the bottom of the screen showing how many are selected
- **Queue Selected** — opens extraction settings once, applies to all selected folders
- **Mark Done** — marks all selected folders as excluded in one click
- **Clear** — deselects everything

---

## Exclusion System

| Source | When added |
|---|---|
| Auto | After every successful extraction (per RAR + per folder when all RARs done) |
| Manual | "✓ Mark Done" button in file browser, or Exclusions page |

- The **watcher** and **scheduler** both check the exclusion table before queuing
- Click **↺ Re-enable** on any excluded folder to clear it
- **Force Re-extract** in the queue modal or **Force Retry** on failed jobs bypasses exclusions

---

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/files/browse?path=…` | List directory contents |
| `GET` | `/api/jobs` | List jobs (`?status=…&limit=…`) |
| `POST` | `/api/jobs` | Queue a path (`force=true` to bypass exclusion) |
| `POST` | `/api/jobs/{id}/cancel` | Cancel a job |
| `POST` | `/api/jobs/{id}/retry?force=true` | Retry (force clears exclusion) |
| `DELETE` | `/api/jobs/{id}` | Delete a job from history |
| `GET` | `/api/folders` | List watch folders |
| `POST` | `/api/folders` | Add a watch folder |
| `PATCH` | `/api/folders/{id}` | Update a watch folder |
| `DELETE` | `/api/folders/{id}` | Remove a watch folder |
| `POST` | `/api/folders/{id}/scan` | Trigger immediate scan |
| `POST` | `/api/folders/scan-all` | Scan all watch folders now |
| `GET` | `/api/exclusions` | List exclusions |
| `POST` | `/api/exclusions` | Add exclusion manually |
| `DELETE` | `/api/exclusions/by-path?path=…` | Remove exclusion by path |
| `GET` | `/api/settings` | Get settings |
| `PUT` | `/api/settings` | Update settings |
| `GET` | `/api/logs` | Get log entries |
| `DELETE` | `/api/logs` | Clear logs |
| `POST` | `/api/webhook/sonarr` | Sonarr webhook receiver (`X-Api-Key` header required) |
| `POST` | `/api/webhook/radarr` | Radarr webhook receiver (`X-Api-Key` header required) |
| `POST` | `/api/webhook/lidarr` | Lidarr webhook receiver (`X-Api-Key` header required) |
| `POST` | `/api/webhook/readarr` | Readarr webhook receiver (`X-Api-Key` header required) |
| `GET` | `/api/webhooks/sources` | List webhook source status |
| `PATCH` | `/api/webhooks/sources/{source}?enabled=` | Enable/disable a source |
| `POST` | `/api/webhooks/sources/{source}/generate-key` | Generate new API key (returned once) |
| `DELETE` | `/api/webhooks/sources/{source}/key` | Revoke API key |
| `GET` | `/api/webhooks/enabled` | Get master webhook toggle |
| `PUT` | `/api/webhooks/enabled?enabled=` | Set master webhook toggle |

WebSocket at `ws://<host>/ws` — events: `new_job`, `job_update`, `job_progress`, `exclusion_added`, `exclusion_removed`.

---

## File Structure

```
unrartool/
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .gitignore
├── VERSION
├── requirements.txt
├── README.md
├── .github/workflows/
│   └── docker-publish.yml    # Auto-build + push to Docker Hub on every git push
├── unraid/
│   ├── unrartool.xml         # Unraid Community Applications template
│   └── icon.png              # Container icon
└── app/
    ├── main.py               # FastAPI app, WebSocket, lifespan
    ├── config.py             # Env var config
    ├── database.py           # SQLAlchemy engine + session helpers
    ├── models.py             # DB models
    ├── ws_manager.py         # WebSocket broadcast manager
    ├── services/
    │   ├── extractor.py      # RAR discovery, real-time progress parsing, extraction
    │   ├── queue_manager.py  # Async job queue, exclusion system
    │   ├── watcher.py        # watchdog filesystem monitor
    │   └── scheduler.py      # APScheduler periodic scanner
    ├── routers/
    │   ├── files.py          # File browser API
    │   ├── jobs.py           # Job CRUD + actions
    │   ├── folders.py        # Watch folders CRUD
    │   ├── exclusions.py     # Exclusion management
    │   ├── settings.py       # App settings
    │   └── logs.py           # Log access
    └── static/
        └── index.html        # Full single-file frontend (no CDN deps)
```

---

## Notes

- **No authentication** — designed for trusted LAN use. Place behind a reverse proxy (Nginx Proxy Manager / Traefik) with auth if you need external access.
- The SQLite database is stored at `$CONFIG_PATH/unrartool.db`. Back it up to preserve job history and settings.
- Extracting is done **in-place** — files are written to the same directory as the RAR set.
- Single uvicorn worker — intentional for SQLite + asyncio compatibility.

---

## Changelog

### v1.2.0
- **Webhook Integration** — Sonarr, Radarr, Lidarr, and Readarr can now notify UnrarTool the instant a download completes, triggering extraction immediately without any filesystem polling delay. Especially useful on SMB/NFS mounts where filesystem events are unreliable.
- **Security** — Per-source API keys (one per *arr app). Keys are generated with 256-bit entropy, stored as SHA256 hashes (never plaintext), transmitted via `X-Api-Key` header only (never in URLs), shown exactly once in the UI, and never written to logs. IP-based rate limiting blocks sources after 5 failed attempts for 5 minutes. Constant-time key comparison prevents timing attacks.
- **Optional toggle** — Webhooks are disabled by default. Enable in Settings → Webhook Integration. Each source (Sonarr/Radarr/Lidarr/Readarr) has its own enable toggle and key.
- **Webhook logs** — every hit (accepted and rejected) is recorded in the log viewer with source, IP, and event type
- **Test event support** — clicking "Test" in Sonarr/Radarr returns a success response without triggering extraction
- **Exclusions respected** — webhook-triggered folders are still skipped if marked as done

### v1.1.0
- **File Browser: Filter & Sort** — filter by name (search), type (All / Folders / RAR Only / Files / Not Done), sort by name, modified date, size, or RAR count
- **File Browser: Multi-Select** — checkboxes on every folder, select-all toggle, floating action bar with Queue Selected and Mark Done
- **Queue Selected** — opens extraction settings once and applies to all selected folders in one action
- **Mark Done (multi)** — mark multiple folders as excluded in a single click
- **README** — full rewrite with feature docs, filter/sort reference, changelog

### v1.0.1
- Bump version to test Docker Hub update detection pipeline

### v1.0.0
- Initial release
- File browser with always-visible Extract and Mark as Done buttons per folder
- Real-time progress bar driven directly from `unrar` stdout percentage output
- Job queue with ETA, cancel, retry, force-retry
- Watch folders with filesystem event detection (watchdog)
- APScheduler periodic background scan
- Automatic exclusion after every successful extraction
- Manual "Mark as Done" button in file browser
- Force Re-extract override
- Exclusions page showing all auto and manual exclusions
- Per-folder password support for encrypted RARs
- Dark / Light theme toggle
- Filterable log viewer
- OCI image labels for Unraid update detection
- GitHub Actions CI/CD — auto-build and push to Docker Hub on every `git push`
- Unraid Community Applications XML template + icon
