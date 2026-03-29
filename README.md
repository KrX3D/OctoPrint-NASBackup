# OctoPrint-NASBackup

Automated OctoPrint backups to a NAS with scheduling, configurable GFS retention, and a full settings UI — no manual script editing required.

## Features

- **Scheduled backups** — daily, weekly, monthly, or manual
- **Two transfer modes** — local path (fstab/NFS mount) or SMB via `smbclient` (no root needed)
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

## Transfer Modes

### Local Path (recommended for fstab/NFS mounts)

Mount your NAS share via `/etc/fstab` or a systemd `.mount` unit, then point the plugin at the local mount point.  
No extra packages or permissions needed.

**Example `/etc/fstab` entry (CIFS):**
```
//192.168.1.11/backup  /mnt/octoprint_backup  cifs  credentials=/root/.smb-octoprint,vers=3.0,iocharset=utf8,uid=pi,gid=pi,file_mode=0640,dir_mode=0750,nofail  0  0
```

### SMB via smbclient

The plugin calls `smbclient` directly — no mount, no root permissions required.

**Install smbclient:**
```bash
sudo apt install smbclient
```

Use the **Test Connection** button in settings to verify before the first scheduled backup.

---

## Settings

Open OctoPrint → Settings → **NAS Backup**.

| Tab | What it controls |
|-----|-----------------|
| **Schedule** | When to run (daily/weekly/monthly/disabled), time, idle-only guard |
| **NAS Connection** | Transfer mode, local path or SMB credentials |
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

