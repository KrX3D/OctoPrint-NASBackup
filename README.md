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
| **NAS Connection** | SMB credential setup, dependency status, connection testing |
| **Backup Options** | OctoPrint ZIP excludes, local ZIP keep count, server name |
| **System Files** | Extra files/dirs to tar and upload alongside the ZIP |
| **Retention** | GFS: how many daily/weekly/monthly/yearly snapshots to keep |
| **Status** | Last result, next scheduled run, manual trigger, live log |

---

## NAS Directory Layout

```
<share>/<subdir>/<server_name>/
  snapshots/
    2025-01-15_030000/
      octoprint-backup-....zip
      system_backup.tar.gz   (if enabled)
      backup.log             (if enabled)
      _backup_info.txt
    2025-01-08_030000/
      ...
  latest -> snapshots/2025-01-15_030000
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

