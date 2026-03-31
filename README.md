# OctoPrint-NASBackup

Automated OctoPrint backups to a NAS over SMB with scheduling, configurable GFS retention, and a full settings UI — no manual script editing required.

## Features

- **Scheduled backups** — daily, weekly, monthly, or manual
- **SMB-only transfer** — uses `smbclient` (no pre-mounted path required)
- **GFS retention** — keep *n* daily / weekly / monthly / yearly snapshots, old ones pruned automatically
- **OctoPrint ZIP backup** — triggers the built-in backup, configurable excludes (uploads, timelapse)
- **System files backup** — optionally tar and upload extra files (fstab, config.yaml, …)
- **Local ZIP pruning** — keeps the last *n* ZIPs on the SD card to reduce wear
- **"Idle only" guard** — skips backup if a print is in progress
- **Connection test** button in the settings UI
- **Live log viewer** inside OctoPrint settings
- **Enable / disable** switch without uninstalling

---

## Installation

Install via OctoPrint's Plugin Manager using this URL:

```
https://github.com/KrX3D/OctoPrint-NASBackup/archive/main.zip
```

Or clone and install manually:

```bash
cd ~/
git clone https://github.com/KrX3D/OctoPrint-NASBackup.git
~/oprint/bin/pip install -e OctoPrint-NASBackup
```

---

## SMB Dependency

The plugin uses `smbclient` on the OctoPrint host.

**Install smbclient manually (recommended):**
```bash
sudo apt install smbclient
```

If `smbclient` is missing, the NAS tab shows an install hint and the manual command to run.


## Settings

Open OctoPrint → Settings → **NAS Backup**.

| Tab | What it controls |
|-----|-----------------|
| **Schedule** | When to run (daily/weekly/monthly/disabled), time, idle-only guard |
| **NAS Connection** | SMB credential setup, server name mode, dependency status, connection testing |
| **Backup Options** | OctoPrint ZIP excludes, local ZIP keep count |
| **System Files** | Extra files/dirs to tar and upload alongside the ZIP |
| **Retention** | GFS: how many daily/weekly/monthly/yearly snapshots to keep |
| **Status** | Last result, next scheduled run, manual trigger, live log |

### Translations

The plugin now includes initial translations for:
- **English** (`en`) fallback
- **German** (`de`)

OctoPrint will use the active UI language automatically, falling back to English strings if a translation is missing.

> Translation files follow gettext/Babel layout under  
> `translations/<lang>/LC_MESSAGES/messages.po` (+ compiled `messages.mo` for runtime).
>
> This repository stores the editable `.po` files.  
> If your workflow cannot handle binary files in PRs, compile `.mo` files locally when building/releasing:
> ```bash
> msgfmt octoprint_nasbackup/translations/de/LC_MESSAGES/messages.po -o octoprint_nasbackup/translations/de/LC_MESSAGES/messages.mo
> msgfmt octoprint_nasbackup/translations/en/LC_MESSAGES/messages.po -o octoprint_nasbackup/translations/en/LC_MESSAGES/messages.mo
> ```

### Default option values

The plugin ships with these defaults (from `get_settings_defaults()`):

| Option | Default |
|---|---|
| `enabled` | `true` |
| `schedule_type` | `daily` |
| `schedule_time` | `03:00` |
| `schedule_day_of_week` | `0` (Monday) |
| `schedule_day_of_month` | `1` |
| `only_when_idle` | `true` |
| `backup_on_startup` | `false` |
| `startup_delay` | `10` seconds |
| `backup_on_startup_cold_boot` | `true` |
| `backup_on_startup_system_restart` | `true` |
| `backup_on_startup_octoprint_restart` | `true` |
| `transfer_mode` | `smbclient` |
| `smb_host` | `192.168.1.11` |
| `smb_share` | `backup` |
| `smb_subdir` | `OctoPrint` |
| `smb_username` | empty |
| `smb_password` | empty |
| `smb_domain` | empty |
| `smb_version` | `3.0` |
| `exclude_uploads` | `false` |
| `exclude_timelapse` | `true` |
| `local_keep_count` | `5` |
| `system_backup_enabled` | `false` |
| `system_backup_items` | `/etc/fstab`, `/etc/hostname`, `/etc/hosts`, `/home/pi/.octoprint/config.yaml`, `/etc/crontab` |
| `retention_enabled` | `true` |
| `keep_daily` | `7` |
| `keep_weekly` | `8` |
| `keep_monthly` | `12` |
| `keep_yearly` | `5` |
| `server_name_auto` | `"true"` |
| `server_name_manual` | `OctoPrint` |
| `copy_log_to_nas` | `false` |

---

## NAS Directory Layout

```
<share>/<subdir>/<server_name>/
  2025-01-15_030000/
    octoprint-backup-....zip
    system_backup.tar.gz   (if enabled)
    backup.log             (if enabled)
    _backup_info.txt
  2025-01-08_030000/
    ...
  latest -> 2025-01-15_030000
```

---

## Changelog

### 0.1.0
- Initial release

### 0.3.3
- Fix settings UI script filename so Knockout bindings and API actions load correctly.
- Improve log panel contrast/readability in the Status tab.

### 0.3.4
- Reworked the settings UI so schedule and retention controls are reliably visible.
- Simplified plugin behavior to SMB-only backups and improved SMB dependency messaging.

### 0.3.5
- Added smbclient dependency status + install command hint in the NAS tab.
- Fixed retention fields so they stay editable when pruning is enabled.
- Manual backup button is now clickable whenever no backup is currently running.

### 0.3.6
- Fixed wording/docs to consistently describe SMB-only behavior.
- Added app-context handling for backup ZIP creation in worker thread.
- Improved schedule form visibility (day fields always shown with usage hints).
- Disabled manual backup action when smbclient is missing and show a clear notification.

### 0.3.7
- Removed non-working smbclient auto-install action from the UI (manual install hint only).
- Added notification when a scheduled backup starts.
- Added early smbclient prerequisite check before backup steps.
- Improved OctoPrint backup trigger call to avoid permission wrapper identity errors.

### 0.3.8
- Added user notifications for backup status updates (success/failed/skipped).
- Test connection now reports via notification popups instead of inline result rows.

### 0.3.9
- Improved schedule button highlighting so selected type is visually clear.
- Reduced noisy blank spacer log lines in backup output.
- Made backup-plugin create call signature-compatible across OctoPrint versions.

### 0.3.10
- Switched backup ZIP triggering to use OctoPrint backup helper API (avoids request-context crashes).
- Retried backup helper call without excludes when helper signature does not support exclude arg.
- Reduced visual blank lines in log viewer rendering.

### 0.3.11
- Wait for asynchronous OctoPrint backup ZIP creation (up to 180s) before failing.
- Use trigger timestamp matching to detect fresh backup ZIPs more reliably.

### 0.3.12
- Store backups directly under the selected SMB subdirectory/server path (no forced `snapshots` folder).
- Write/upload `backup.log` at the end of the run so it contains complete logs.
- Improved SMB directory parsing for retention and snapshot counting.

### 0.3.13
- Startup backup delay default changed to 10s (still user-configurable).
- Added startup kind detection in logs (`system_boot` vs `octoprint_restart`, best effort).
- Improved schedule button selection persistence on page reload.

### 0.3.14
- Added startup backup event filters: cold boot, system restart, and OctoPrint restart.
- Added startup state persistence to classify restart type and avoid duplicate startup triggers.

### 0.3.15
- Startup backup options are always visible (enabled/disabled with main startup toggle).
- Added plugin version display in settings header.
- Improved SMB retention directory listing by switching to `cd <path>; ls` parsing.

### 0.3.16
- Startup delay and startup-type options are now always editable in UI.
- Fixed daily retention behavior to keep only the newest snapshot per day window.

### 0.3.17
- Snapshot log now uses backup ZIP base name (`octoprint-backup-YYYYMMDD-HHMMSS.log`).
- Added monthly root log append file (`nasbackup-YYYY-MM.log`) that survives snapshot pruning.
- Monthly root log entries now include clear separators between runs.

### 0.3.18
- Startup restart classification now consumes shutdown markers to reduce cold-boot misclassification after quick reboot cycles.
- Added first translation bundles (English/German) for startup labels and key notifications.

### 0.3.19
- Removed compiled `.mo` binaries from repository to avoid PR systems that reject binary diffs.
- Added translation compile workflow that uploads compiled catalogs as CI artifacts.
- Added README instructions to compile translation catalogs locally during build/release.
