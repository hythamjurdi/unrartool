# UnrarTool

A self-hosted Docker container for automatically extracting split RAR archives. Built for Unraid but works anywhere Docker runs.

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
      - /path/to/appdata/unrartool:/config
    environment:
      DATA_PATH: /data
      CONFIG_PATH: /config
```

Open `http://<your-server-ip>:8080`.

### Unraid

In Unraid → Docker → Add Container → paste into the Template field:
```
https://raw.githubusercontent.com/hythamjurdi/unrartool/main/unraid/unrartool.xml
```
Adjust volume paths and click Apply.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATA_PATH` | `/data` | Root path shown in the file browser |
| `CONFIG_PATH` | `/config` | Where the database and settings are stored |
| `PORT` | `8080` | Web UI port |
| `PUID` | `99` | User ID for file ownership |
| `PGID` | `100` | Group ID for file ownership |

---

## How It Works

### File Browser
Browse your mounted folders. Every folder has three action buttons:
- **Extract** — queue it immediately (opens a dialog for password and post-action)
- **✓ Mark Done** — exclude it from all automation permanently
- **Watch** — add it as a watch folder

Use the search bar, type filters (All / Folders / RAR Only / Not Done), and sort options to navigate large libraries. Check multiple folders and use the floating **Queue Selected** bar to batch-queue them at once.

### Sources Page
The Sources page is split into two tabs:

**Watch Folders** — add any folder and UnrarTool monitors it in real time (recursive). New RARs are queued the moment they appear. A periodic scheduler also re-scans all watch folders on a configurable interval (default 30 min) to catch anything missed.

***arr Webhooks** — optionally connect Sonarr, Radarr, Lidarr, or Readarr. When a download finishes, the app sends UnrarTool an instant notification rather than waiting for the next filesystem scan. Useful on SMB/NFS mounts where filesystem events are unreliable.

To set up: go to **Sources → *arr Webhooks**, enable the toggle, then for each app enter its URL and paste in its API key (found in that app under Settings → General → Security). Hit **Test Connection** to verify, then add UnrarTool as a webhook in that app pointing to `http://UNRARTOOL_IP:8080/api/webhook/sonarr` (or radarr/lidarr/readarr). The UI shows the exact URL and steps inline per source.

### Exclusions
After a successful extraction, the folder is automatically excluded so it's never re-extracted. The exclusion appears in the **Exclusions** page and as a **✓ Done** badge in the file browser. Click **↺ Re-enable** at any time to clear it, or use **Force Re-extract** when queueing to override it.

### Source Badges
Every job in the queue, history, and dashboard shows a coloured badge showing how it was triggered:

| Badge | Colour | Meaning |
|---|---|---|
| Manual | Purple | Queued via File Browser |
| Watcher | Blue | Triggered by filesystem event |
| Scheduler | Grey | Triggered by periodic scan |
| Sonarr | Cyan | Sonarr webhook |
| Radarr | Gold | Radarr webhook |
| Lidarr | Teal | Lidarr webhook |
| Readarr | Orange | Readarr webhook |

### Settings
Found under **Settings** in the sidebar:

| Setting | Default | Description |
|---|---|---|
| Scan Interval | 30 min | How often the scheduler re-scans watch folders |
| Max Concurrent Extractions | 1 | Recommended to keep at 1 |
| Default Post-Extraction Action | Keep | Keep / Delete / Move to trash |
| Trash Folder | `/config/trash` | Used when post-action is Move to trash |

---

## Notes

- No authentication — designed for trusted LAN use. Put it behind a reverse proxy (Nginx Proxy Manager, Traefik) if you need external access.
- Extraction is always in-place — files land in the same folder as the RAR.
- Watch folders scan recursively — adding `/data/downloads` catches RARs in all subdirectories.
- The database lives at `$CONFIG_PATH/unrartool.db` — include it in your Unraid appdata backup.

---

## Changelog

### v1.3.5
- **App-specific trigger instructions** — Radarr source card now correctly shows "On File Import only (Radarr has no Import Complete)" while Sonarr shows "On File Import + On Import Complete"

### v1.3.4
- **Webhook URL instruction clarified** — now explicitly says the URL points to UnrarTool itself (not Sonarr/Radarr), uses `UNRAID_IP:UNRARTOOL_PORT` as the placeholder so it's clear the port is UnrarTool's port, not the *arr app's port

### v1.3.3
- **Webhook setup instructions** — step 2 in each source card now shows every field exactly as it appears in Sonarr/Radarr: Webhook URL, Method (POST), which triggers to check (On File Import / On Import Complete only), that Username/Password should be left empty, and that the API key goes in Headers → Key: `X-Api-Key` / Value: your API key

### v1.3.2
- **Webhook status indicator** — source cards in Sources → *arr Webhooks now show 4 distinct states: Not configured (grey) · Disabled (grey) · Waiting — webhook not set up in app yet (amber) · Active — receiving webhooks (green with glow). The border of each card changes colour to match. "Active" only turns green once real webhook hits have been received, not just when credentials are saved.
- **Clearer 2-step setup flow** — source cards now explicitly show Step 1 (connect UnrarTool to the app) and Step 2 (add UnrarTool as a webhook in the app), with a "✓ Done" or "▶ Action required" marker on Step 2 based on whether hits have been received
- **Dashboard webhook cards** — same 4-state logic applied to the dashboard sources widget; amber "Waiting" cards replace the misleading green for configured-but-not-yet-firing sources

### v1.3.1
- **Fix: auto-defer for mid-download extraction attempts** — when the filesystem watcher fires while qBittorrent (or any downloader) is still writing a RAR, unrar would fail with "is not RAR archive". UnrarTool now detects this specific class of error and automatically retries the job after 5 minutes, up to 6 times (30 min total), before giving up with a real failure. Jobs show as `pending` while waiting, not `failed`.
- **Improved watcher stabilisation** — increased from 15 seconds to a double-check: wait 30 seconds after the last filesystem event, then compare file sizes 15 seconds apart. Only queues when the size is confirmed stable, avoiding false triggers on large files still downloading.

### v1.3.0
- **Sources page** — "Watch Folders" nav item renamed to "Sources"; watch folder management and *arr webhook configuration merged into one page with a tab bar
- **Source status cards** — live status cards at the top of the Sources page showing each scanning method's state, last trigger time, and hit count
- **Dashboard sources widget** — card grid on the dashboard showing all active sources with status and last triggered time
- **Coloured source badges** — every job now shows a coloured badge identifying how it was triggered; *arr sources use each app's actual brand colour

### v1.2.1
- Fixed webhook source cards not rendering after upgrade (missing DB columns)
- Removed stale text from settings UI

### v1.2.0
- **Webhook integration** — optional Sonarr, Radarr, Lidarr, Readarr support via Sources → *arr Webhooks tab
- **Test Connection** — verify connectivity to each *arr app from within UnrarTool
- Per-source enable/disable, hit counter, last received timestamp

### v1.1.0
- File browser filter, sort, and multi-select with floating action bar
- Batch queue and batch mark-done for multiple folders

### v1.0.0
- Initial release: file browser, job queue, watch folders, scheduler, real-time progress, exclusion system, dark/light theme, log viewer, Unraid template, GitHub Actions CI/CD
