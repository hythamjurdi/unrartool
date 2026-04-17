# UnrarTool

A self-hosted Docker container for automatically extracting split RAR archives. Built for Unraid but works with any Docker-compatible host via `docker-compose`. Features a sleek web UI with a file browser, job queue, watch folders, scheduled scanning, real-time progress, and webhook integration with Sonarr, Radarr, Lidarr, and Readarr.

---

## Features

- **File Browser** — Navigate mounted volumes with filter, sort, and multi-select support
- **Filter & Sort** — Filter by name, type (All / Folders / RAR Only / Files / Not Done), sort by name, date, size, or RAR count
- **Multi-Select** — Checkboxes on every folder; queue or mark multiple folders at once via a floating action bar
- **Real-time Progress** — Smooth progress bar and ETA driven directly from `unrar`'s stdout output
- **Job Queue** — Live status updates via WebSocket; cancel, retry, or force-retry any job
- **Watch Folders** — Filesystem event detection (watchdog) queues new RARs the instant they land
- **Scheduler** — Periodic background scan of all watch folders (configurable interval)
- **Smart Exclusions** — Auto-excluded after every successful extraction; manual "✓ Mark Done" button always visible in the file browser
- **Force Re-extract** — Override any exclusion when you need to re-run
- **Webhook Integration** — Optional direct integration with Sonarr, Radarr, Lidarr, and Readarr. UnrarTool connects to each app using its URL and API key, enabling instant extraction triggers and connection testing. Especially useful on SMB/NFS mounts where filesystem events are unreliable.
- **RAR5 + Split RAR** — Supports `.part01.rar` and legacy `.rar + .r00/.r01` formats
- **Password Support** — Per-folder RAR passwords for encrypted archives
- **Incomplete Archive Detection** — Skips and logs an error if parts are missing before attempting extraction
- **Dark / Light Mode** — Toggle in the sidebar
- **Live Log Viewer** — Filterable log viewer; all activity recorded to SQLite
- **Unraid Update Detection** — OCI image labels ensure Unraid shows an update badge whenever a new version is pushed to Docker Hub

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
      - /path/to/downloads:/data/downloads
      - /path/to/media:/data/media
      - /path/to/appdata/unrartool:/config
    environment:
      DATA_PATH: /data
      CONFIG_PATH: /config
      PORT: "8080"
```

Open `http://<your-server-ip>:8080`.

### Unraid Setup

1. In Unraid → Docker tab → **Add Container**
2. In the **Template** field, paste:
   ```
   https://raw.githubusercontent.com/hythamjurdi/unrartool/main/unraid/unrartool.xml
   ```
3. Adjust the volume paths to match your setup and click **Apply**

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATA_PATH` | `/data` | Root path shown in the file browser on first load |
| `CONFIG_PATH` | `/config` | Where `unrartool.db` and settings are stored |
| `PORT` | `8080` | Port the web server listens on |
| `PUID` | `99` | User ID for file ownership (Unraid: 99 = nobody) |
| `PGID` | `100` | Group ID for file ownership (Unraid: 100 = users) |

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

### Filters
| Filter | Shows |
|---|---|
| All | Everything in the current directory |
| Folders | Directories only |
| RAR Only | Directories containing at least one RAR set |
| Files | Non-directory files only |
| Not Done | Directories not yet excluded |

### Sort Options
Name A→Z / Z→A · Newest / Oldest · Size Large→Small / Small→Large · Most RAR Sets · Type (Folders first)

### Multi-Select
Check any folder's checkbox to select it. Use **Select All** to select all visible folders. A floating action bar appears at the bottom showing the count with **Queue Selected**, **Mark Done**, and **Clear** actions.

---

## Webhook Integration

UnrarTool can connect directly to your \*arr apps so they notify it the instant a download finishes — no filesystem polling delay, works reliably on SMB/NFS mounts.

### How it works
1. In UnrarTool → **Settings → Webhook Integration** → enable the master toggle
2. For each app, enter its **URL** and **API Key** and click **Save**
3. Click **Test Connection** to verify UnrarTool can reach the app
4. In the \*arr app, add a webhook pointing back to UnrarTool (instructions shown per-source in the UI)

### Default ports
| App | Default Port |
|---|---|
| Sonarr | 8989 |
| Radarr | 7878 |
| Lidarr | 8686 |
| Readarr | 8787 |

### Webhook URL format
```
http://YOUR_IP:8080/api/webhook/sonarr
http://YOUR_IP:8080/api/webhook/radarr
http://YOUR_IP:8080/api/webhook/lidarr
http://YOUR_IP:8080/api/webhook/readarr
```

### In each \*arr app
Go to **Settings → Connect → + → Webhook**:
- **URL**: as above (replace `YOUR_IP:8080` with your UnrarTool address)
- **Method**: POST
- **Trigger**: On Download
- **Header**: `X-Api-Key` = your \*arr API key (the same one saved in UnrarTool)

### Security
- API keys stored in the local SQLite database (same as all other tools in the \*arr ecosystem)
- Webhook validation uses SHA256 hashing and constant-time comparison to prevent timing attacks
- IP-based rate limiting: 5 failed auth attempts → 5 minute block per IP
- Auth failures always return an identical 401 — no information leakage
- Key values are never written to logs

---

## Exclusion System

| Source | When added |
|---|---|
| Auto | After every successful extraction (per RAR + per folder when all RARs done) |
| Manual | "✓ Mark Done" in file browser, or the Exclusions page |

- The **watcher**, **scheduler**, and **webhook handler** all respect exclusions
- Click **↺ Re-enable** on any excluded folder to clear it
- **Force Re-extract** in the queue modal or **Force Retry** on failed jobs bypasses exclusions

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/files/browse?path=…` | List directory contents |
| `GET` | `/api/jobs` | List jobs (`?status=…&limit=…`) |
| `POST` | `/api/jobs` | Queue a path (`force=true` bypasses exclusion) |
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
| `POST` | `/api/webhook/sonarr` | Sonarr webhook receiver (`X-Api-Key` header) |
| `POST` | `/api/webhook/radarr` | Radarr webhook receiver (`X-Api-Key` header) |
| `POST` | `/api/webhook/lidarr` | Lidarr webhook receiver (`X-Api-Key` header) |
| `POST` | `/api/webhook/readarr` | Readarr webhook receiver (`X-Api-Key` header) |
| `GET` | `/api/webhooks/sources` | List webhook source status |
| `PATCH` | `/api/webhooks/sources/{source}?enabled=` | Enable/disable a source |
| `POST` | `/api/webhooks/sources/{source}/save-key` | Save URL + API key |
| `DELETE` | `/api/webhooks/sources/{source}/key` | Clear credentials |
| `GET` | `/api/webhooks/sources/{source}/test` | Test connection to \*arr app |
| `GET` | `/api/webhooks/enabled` | Get master webhook toggle |
| `PUT` | `/api/webhooks/enabled?enabled=` | Set master webhook toggle |

WebSocket at `ws://<host>/ws` — events: `new_job`, `job_update`, `job_progress`, `exclusion_added`, `exclusion_removed`.

---

## Notes

- **No authentication** — designed for trusted LAN use. Place behind Nginx Proxy Manager or Traefik with auth if you need external access.
- The SQLite database lives at `$CONFIG_PATH/unrartool.db`. Back it up via Unraid's appdata backup to preserve job history and settings.
- Extraction is done **in-place** — files are written to the same directory as the RAR set.
- Runs as a single uvicorn worker — intentional for SQLite + asyncio compatibility.
- Works on any Docker host — not Unraid-specific.

---

## Changelog

### v1.2.1
- **Fix: webhook source cards not rendering** — added SQLite migration on startup to add `app_url` and `arr_api_key` columns to existing `webhook_sources` tables. Upgrading users with a pre-existing database were hitting a silent column-missing error that prevented the sources list from loading.
- **Fix: removed stale security description** from the webhook settings UI — the text referencing key generation was outdated after the UX change to paste-in keys

### v1.2.0
- **Webhook Integration** — Sonarr, Radarr, Lidarr, and Readarr can notify UnrarTool the instant a download completes. Enter each app's URL and API key in Settings → Webhook Integration. UnrarTool tests connectivity and uses the same credentials for incoming webhook validation.
- **Test Connection button** — per-source button calls the \*arr app's `/system/status` endpoint and reports the app name and version, or a clear error message
- **Clean UX** — URL + API key input fields per source; no key generation or copying required; instructions shown inline per source
- **Security** — webhook auth uses SHA256 hashing + constant-time comparison; IP rate limiting (5 failures → 5 min block); keys never logged; identical 401 for all auth failures
- **Optional** — disabled by default; each source enabled independently once credentials are saved
- **Test event support** — clicking Test in \*arr apps returns 200 OK without triggering extraction
- **Hit counter** — each source card shows total webhook hits received and the last received time
- **httpx** added to dependencies for async outbound HTTP calls
- README fully updated with webhook setup, default ports, and all current features

### v1.1.0
- **File Browser: Filter & Sort** — search by name; filter by All / Folders / RAR Only / Files / Not Done; sort by name, modified date, size, or RAR count
- **File Browser: Multi-Select** — checkboxes on every folder, select-all toggle, floating action bar with Queue Selected and Mark Done
- **Queue Selected** — one extraction settings dialog applies to all selected folders
- **Mark Done (multi)** — mark multiple folders excluded in a single click
- README updated with filter/sort reference

### v1.0.1
- Bump version to test Docker Hub → Unraid update detection pipeline

### v1.0.0
- Initial release
- File browser with always-visible Extract and Mark as Done buttons
- Real-time progress bar from `unrar` stdout percentage output
- Job queue with ETA, cancel, retry, force-retry
- Watch folders with filesystem event detection
- APScheduler periodic background scan
- Auto-exclusion after every successful extraction
- Manual Mark as Done in file browser
- Force Re-extract override
- Exclusions page
- Per-folder password support
- Dark / Light theme
- Filterable log viewer
- OCI image labels for Unraid update detection
- GitHub Actions CI/CD → Docker Hub on every `git push`
- Unraid Community Applications XML template + icon
