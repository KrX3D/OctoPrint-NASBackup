# coding=utf-8
"""
OctoPrint-NASBackup  –  __init__.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Automated OctoPrint backups to a NAS:
  - APScheduler-based cron (daily / weekly / monthly)
  - Transfer via local path OR smbclient
  - GFS retention (daily / weekly / monthly / yearly)
  - System-files tar upload
  - In-memory log viewer exposed via SimpleApiPlugin
"""
from __future__ import absolute_import, unicode_literals

import datetime
import glob
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import traceback
from collections import deque

import flask
import octoprint.plugin

# APScheduler (bundled with OctoPrint)
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    _HAS_SCHEDULER = True
except ImportError:
    _HAS_SCHEDULER = False

MAX_LOG_ENTRIES = 300


# ─────────────────────────────────────────────────────────────────────────────
# Plugin class
# ─────────────────────────────────────────────────────────────────────────────

class NasBackupPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SimpleApiPlugin,
):

    # ──────────────────────────────────────────
    # Init
    # ──────────────────────────────────────────

    def __init__(self):
        self._scheduler      = None
        self._backup_running = False
        self._backup_lock    = threading.Lock()
        self._log_entries    = deque(maxlen=MAX_LOG_ENTRIES)
        self._last_status    = {
            "status":  "never",
            "message": "No backup has been run yet.",
            "time":    None,
        }

    # ──────────────────────────────────────────
    # SettingsPlugin
    # ──────────────────────────────────────────

    def get_settings_defaults(self):
        return dict(
            # Master switch
            enabled=True,

            # ── Schedule ──────────────────────────────
            schedule_type="daily",        # daily | weekly | monthly | disabled
            schedule_time="03:00",        # HH:MM (device local time)
            schedule_day_of_week=0,       # 0=Mon … 6=Sun (weekly only)
            schedule_day_of_month=1,      # 1–28 (monthly only)
            only_when_idle=True,

            # ── Transfer ──────────────────────────────
            transfer_mode="local",        # local | smbclient
            local_path="/mnt/octoprint_backup",

            # ── SMB ───────────────────────────────────
            smb_host="192.168.1.11",
            smb_share="backup",
            smb_subdir="OctoPrint",
            smb_username="",
            smb_password="",
            smb_domain="",
            smb_version="3.0",

            # ── OctoPrint backup options ───────────────
            exclude_uploads=False,
            exclude_timelapse=True,
            local_keep_count=5,           # how many ZIPs to keep on SD card

            # ── System files ──────────────────────────
            system_backup_enabled=False,
            system_backup_items=(
                "/etc/fstab\n"
                "/etc/hostname\n"
                "/etc/hosts\n"
                "/home/pi/.octoprint/config.yaml\n"
                "/etc/crontab"
            ),

            # ── GFS retention ─────────────────────────
            retention_enabled=True,
            keep_daily=7,
            keep_weekly=8,
            keep_monthly=12,
            keep_yearly=5,

            # ── Misc ──────────────────────────────────
            server_name_auto=True,
            server_name_manual="OctoPrint",
            copy_log_to_nas=False,
        )

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self._reschedule()
        self._plugin_log("Settings saved — schedule re-applied.")

    # ──────────────────────────────────────────
    # TemplatePlugin
    # ──────────────────────────────────────────

    def get_template_configs(self):
        return [
            dict(
                type="settings",
                name="NAS Backup",
                template="nasbackup_settings.jinja2",
                custom_bindings=True,
            )
        ]

    # ──────────────────────────────────────────
    # AssetPlugin
    # ──────────────────────────────────────────

    def get_assets(self):
        return dict(
            js=["js/nasbackup.js"],
            css=["css/nasbackup.css"],
        )

    # ──────────────────────────────────────────
    # StartupPlugin
    # ──────────────────────────────────────────

    def on_after_startup(self):
        self._plugin_log("NAS Backup plugin started (v{})".format(self._plugin_version))

        if not _HAS_SCHEDULER:
            self._plugin_log("WARNING: APScheduler not available — scheduled backups disabled.")
            return

        self._scheduler = BackgroundScheduler(daemon=True)
        self._scheduler.start()
        self._reschedule()

    # ──────────────────────────────────────────
    # ShutdownPlugin
    # ──────────────────────────────────────────

    def on_shutdown(self):
        if self._scheduler and self._scheduler.running:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass

    # ──────────────────────────────────────────
    # SimpleApiPlugin
    # ──────────────────────────────────────────

    def get_api_commands(self):
        return dict(
            trigger_backup=[],
            test_connection=[],
            clear_logs=[],
        )

    def on_api_command(self, command, data):
        if command == "trigger_backup":
            if self._backup_running:
                return flask.jsonify({"success": False, "message": "A backup is already running."}), 409
            t = threading.Thread(target=self._run_backup, name="nasbackup-thread", daemon=True)
            t.start()
            return flask.jsonify({"success": True, "message": "Backup started."})

        elif command == "test_connection":
            result = self._test_connection()
            return flask.jsonify(result)

        elif command == "clear_logs":
            self._log_entries.clear()
            return flask.jsonify({"success": True})

        return flask.abort(400)

    def on_api_get(self, request):
        next_run = None
        if self._scheduler:
            try:
                job = self._scheduler.get_job("nasbackup_job")
                if job and job.next_run_time:
                    next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        return flask.jsonify(dict(
            running=self._backup_running,
            last_status=self._last_status,
            next_run=next_run,
            logs=list(self._log_entries),
        ))

    # ──────────────────────────────────────────
    # Scheduling
    # ──────────────────────────────────────────

    def _reschedule(self):
        if not self._scheduler:
            return
        try:
            self._scheduler.remove_all_jobs()
        except Exception:
            pass

        if not self._settings.get_boolean(["enabled"]):
            self._plugin_log("Scheduling skipped — plugin disabled.")
            return

        schedule_type = self._settings.get(["schedule_type"])
        if schedule_type == "disabled":
            self._plugin_log("Schedule type is 'disabled' — manual trigger only.")
            return

        trigger = self._build_cron_trigger(schedule_type)
        if trigger is None:
            self._plugin_log("WARNING: Could not build cron trigger for type '{}'.".format(schedule_type))
            return

        self._scheduler.add_job(
            func=self._run_backup,
            trigger=trigger,
            id="nasbackup_job",
            replace_existing=True,
            max_instances=1,
        )
        self._plugin_log("Backup scheduled: type={} trigger={}".format(schedule_type, trigger))

    def _build_cron_trigger(self, schedule_type):
        time_str = self._settings.get(["schedule_time"]) or "03:00"
        try:
            hour, minute = map(int, time_str.split(":"))
        except Exception:
            hour, minute = 3, 0

        if schedule_type == "daily":
            return CronTrigger(hour=hour, minute=minute)

        elif schedule_type == "weekly":
            _days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            dow   = int(self._settings.get(["schedule_day_of_week"]) or 0)
            return CronTrigger(day_of_week=_days[dow % 7], hour=hour, minute=minute)

        elif schedule_type == "monthly":
            dom = int(self._settings.get(["schedule_day_of_month"]) or 1)
            dom = max(1, min(28, dom))
            return CronTrigger(day=dom, hour=hour, minute=minute)

        return None

    # ──────────────────────────────────────────
    # Core backup orchestration
    # ──────────────────────────────────────────

    def _run_backup(self):
        """
        Main backup routine — called by the scheduler or manual trigger.
        Always runs in a background thread.
        """
        with self._backup_lock:
            if self._backup_running:
                self._log("Backup already running — skipping.", "WARNING")
                return
            self._backup_running = True

        start_time = datetime.datetime.now()
        timestamp  = start_time.strftime("%Y-%m-%d_%H%M%S")

        try:
            self._log("=" * 60)
            self._log("NAS Backup started  [{} v{}]".format(
                socket.gethostname(), self._plugin_version))
            self._log("Timestamp : {}".format(timestamp))
            self._log("=" * 60)

            # Guard: idle check
            if self._settings.get_boolean(["only_when_idle"]):
                if self._printer.is_printing() or self._printer.is_paused():
                    self._log("Printer is busy and only_when_idle=true — skipping.")
                    self._set_status("skipped", "Skipped — printer busy.")
                    return

            server_name   = self._get_server_name()
            transfer_mode = self._settings.get(["transfer_mode"])
            self._log("Server name   : {}".format(server_name))
            self._log("Transfer mode : {}".format(transfer_mode))

            # ── Step 1: Create OctoPrint backup ZIP ──────────────────────
            self._log("")
            self._log("Step 1/4 — Creating OctoPrint backup ZIP…")
            zip_path = self._trigger_octoprint_backup()
            self._log("ZIP created : {} ({:.1f} MB)".format(
                os.path.basename(zip_path),
                os.path.getsize(zip_path) / 1_048_576,
            ))

            # ── Step 2: Transfer to NAS ───────────────────────────────────
            self._log("")
            self._log("Step 2/4 — Transferring to NAS…")
            if transfer_mode == "local":
                self._transfer_local(zip_path, server_name, timestamp)
            elif transfer_mode == "smbclient":
                self._transfer_smbclient(zip_path, server_name, timestamp)
            else:
                raise RuntimeError("Unknown transfer_mode: '{}'".format(transfer_mode))

            # ── Step 3: Prune local ZIPs ─────────────────────────────────
            self._log("")
            self._log("Step 3/4 — Pruning local OctoPrint ZIPs…")
            self._prune_local_zips()

            # ── Step 4: GFS retention ─────────────────────────────────────
            if self._settings.get_boolean(["retention_enabled"]):
                self._log("")
                self._log("Step 4/4 — Applying GFS retention on NAS…")
                self._apply_retention(server_name, transfer_mode)
            else:
                self._log("Step 4/4 — Retention disabled, skipping.")

            elapsed = int((datetime.datetime.now() - start_time).total_seconds())
            self._log("")
            self._log("=" * 60)
            self._log("Backup completed successfully in {}s.".format(elapsed))
            self._log("=" * 60)
            self._set_status("success", "Completed in {}s.".format(elapsed))

        except Exception as exc:
            self._log("", "ERROR")
            self._log("BACKUP FAILED: {}".format(exc), "ERROR")
            self._log(traceback.format_exc(), "DEBUG")
            self._set_status("failed", str(exc))

        finally:
            self._backup_running = False

    # ──────────────────────────────────────────
    # Step 1 — trigger OctoPrint built-in backup
    # ──────────────────────────────────────────

    def _trigger_octoprint_backup(self):
        """
        Call OctoPrint's bundled backup plugin and return the path of the new ZIP.
        Raises RuntimeError on failure.
        """
        backup_plugin = self._plugin_manager.get_plugin_info("backup")
        if not backup_plugin:
            raise RuntimeError(
                "OctoPrint backup plugin not found. "
                "Make sure the bundled 'backup' plugin is enabled."
            )

        excludes = []
        if self._settings.get_boolean(["exclude_uploads"]):
            excludes.append("uploads")
        if self._settings.get_boolean(["exclude_timelapse"]):
            excludes.append("timelapse")

        backup_dir = self._get_octoprint_backup_dir()
        os.makedirs(backup_dir, exist_ok=True)

        before = set(glob.glob(os.path.join(backup_dir, "*.zip")))

        # Call the backup plugin synchronously
        try:
            result = backup_plugin.implementation.create_backup(exclude=excludes)
        except Exception as exc:
            raise RuntimeError("OctoPrint backup plugin raised: {}".format(exc))

        # If create_backup returned a valid path, use it directly (newer API)
        if result and isinstance(result, str) and os.path.isfile(result):
            return result

        # Otherwise detect the newly created file
        after   = set(glob.glob(os.path.join(backup_dir, "*.zip")))
        new_zip = after - before
        if new_zip:
            return max(new_zip, key=os.path.getmtime)

        # Last resort: the newest ZIP if it was refreshed in-place
        all_zips = sorted(
            glob.glob(os.path.join(backup_dir, "*.zip")),
            key=os.path.getmtime, reverse=True
        )
        if all_zips:
            age = (datetime.datetime.now() -
                   datetime.datetime.fromtimestamp(os.path.getmtime(all_zips[0]))).total_seconds()
            if age < 120:
                return all_zips[0]

        raise RuntimeError(
            "No new backup ZIP detected after triggering OctoPrint backup. "
            "Check the OctoPrint log for errors."
        )

    # ──────────────────────────────────────────
    # Step 2a — local transfer
    # ──────────────────────────────────────────

    def _transfer_local(self, zip_path, server_name, timestamp):
        base     = self._settings.get(["local_path"])
        snap_dir = os.path.join(base, server_name, "snapshots", timestamp)
        os.makedirs(snap_dir, exist_ok=True)

        # Copy ZIP
        dest = os.path.join(snap_dir, os.path.basename(zip_path))
        shutil.copy2(zip_path, dest)
        self._log("  ZIP  → {}".format(dest))

        # Optional system files
        if self._settings.get_boolean(["system_backup_enabled"]):
            self._backup_system_files_local(snap_dir)

        # Optional log copy
        if self._settings.get_boolean(["copy_log_to_nas"]):
            self._write_log_file(os.path.join(snap_dir, "backup.log"))

        # Metadata
        self._write_metadata_file(
            os.path.join(snap_dir, "_backup_info.txt"),
            zip_path, timestamp, server_name
        )

        # Update "latest" symlink
        latest = os.path.join(base, server_name, "latest")
        if os.path.islink(latest) or os.path.exists(latest):
            try:
                os.remove(latest)
            except Exception:
                pass
        try:
            os.symlink(snap_dir, latest)
        except Exception as exc:
            self._log("  Could not update 'latest' symlink: {}".format(exc), "WARNING")

        self._log("  Transfer complete.")

    def _backup_system_files_local(self, snap_dir):
        items   = self._get_system_items()
        sys_dir = os.path.join(snap_dir, "system_files")
        copied  = 0
        for item in items:
            if not os.path.exists(item):
                self._log("  [system] Not found, skipping: {}".format(item), "WARNING")
                continue
            rel  = item.lstrip("/")
            dest = os.path.join(sys_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                if os.path.isdir(item):
                    try:
                        shutil.copytree(item, dest, dirs_exist_ok=True)
                    except TypeError:
                        # Python < 3.8 fallback
                        if os.path.exists(dest):
                            shutil.rmtree(dest)
                        shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
                copied += 1
            except Exception as exc:
                self._log("  [system] Failed to copy {}: {}".format(item, exc), "WARNING")
        self._log("  System files: {} item(s) copied.".format(copied))

    # ──────────────────────────────────────────
    # Step 2b — smbclient transfer
    # ──────────────────────────────────────────

    def _transfer_smbclient(self, zip_path, server_name, timestamp):
        subdir      = self._settings.get(["smb_subdir"]) or "OctoPrint"
        remote_snap = "{}/{}/snapshots/{}".format(subdir, server_name, timestamp)

        # Create remote directory tree
        self._smb_mkdir_p(remote_snap)

        # Upload ZIP
        remote_zip = "{}/{}".format(remote_snap, os.path.basename(zip_path))
        self._log("  Uploading ZIP → {}".format(remote_zip))
        rc, out, err = self._smb_exec("put \"{}\" \"{}\"".format(zip_path, remote_zip))
        if rc != 0:
            raise RuntimeError(
                "smbclient failed to upload ZIP (exit {}): {}".format(rc, err.strip())
            )
        self._log("  ZIP upload OK.")

        # Optional system files
        if self._settings.get_boolean(["system_backup_enabled"]):
            self._backup_system_files_smbclient(remote_snap)

        # Optional log
        if self._settings.get_boolean(["copy_log_to_nas"]):
            self._upload_temp_file_smbclient(
                lambda f: self._write_log_file(f),
                "{}/backup.log".format(remote_snap),
                suffix=".log",
            )

        # Metadata
        meta = self._build_metadata_text(zip_path, timestamp, server_name)
        self._upload_temp_file_smbclient(
            lambda f: open(f, "w").write(meta) or None,
            "{}/_backup_info.txt".format(remote_snap),
            suffix=".txt",
        )

        self._log("  Transfer complete.")

    def _backup_system_files_smbclient(self, remote_snap):
        items    = self._get_system_items()
        existing = [x for x in items if os.path.exists(x)]
        if not existing:
            self._log("  [system] No items found, skipping.", "WARNING")
            return

        fd, tar_path = tempfile.mkstemp(prefix="nasbackup_sys_", suffix=".tar.gz")
        os.close(fd)
        try:
            cmd    = ["tar", "-czf", tar_path] + existing
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                self._log(
                    "  [system] tar failed: {}".format(result.stderr.strip()), "WARNING"
                )
                return
            remote_tar = "{}/system_backup.tar.gz".format(remote_snap)
            rc, out, err = self._smb_exec(
                "put \"{}\" \"{}\"".format(tar_path, remote_tar)
            )
            if rc != 0:
                self._log(
                    "  [system] Upload failed (exit {}): {}".format(rc, err.strip()), "WARNING"
                )
            else:
                sz = os.path.getsize(tar_path) / 1_048_576
                self._log("  System files: {:.1f} MB uploaded.".format(sz))
        finally:
            try:
                os.unlink(tar_path)
            except Exception:
                pass

    def _upload_temp_file_smbclient(self, write_fn, remote_path, suffix=".tmp"):
        """Write content to a temp file via write_fn, then upload it, then delete."""
        fd, local = tempfile.mkstemp(prefix="nasbackup_", suffix=suffix)
        os.close(fd)
        try:
            write_fn(local)
            self._smb_exec("put \"{}\" \"{}\"".format(local, remote_path))
        finally:
            try:
                os.unlink(local)
            except Exception:
                pass

    # ──────────────────────────────────────────
    # Step 3 — prune local ZIPs
    # ──────────────────────────────────────────

    def _prune_local_zips(self):
        keep = int(self._settings.get(["local_keep_count"]) or 5)
        if keep <= 0:
            self._log("  local_keep_count=0 — skipping prune.")
            return

        backup_dir = self._get_octoprint_backup_dir()
        zips       = sorted(
            glob.glob(os.path.join(backup_dir, "*.zip")),
            key=os.path.getmtime, reverse=True
        )

        if len(zips) <= keep:
            self._log("  Local ZIPs: {} present, {} allowed — nothing to delete.".format(
                len(zips), keep
            ))
            return

        for old in zips[keep:]:
            try:
                os.unlink(old)
                self._log("  Deleted local ZIP: {}".format(os.path.basename(old)))
            except Exception as exc:
                self._log("  Could not delete {}: {}".format(old, exc), "WARNING")

    # ──────────────────────────────────────────
    # Step 4 — GFS retention
    # ──────────────────────────────────────────

    def _apply_retention(self, server_name, transfer_mode):
        if transfer_mode == "local":
            base      = self._settings.get(["local_path"])
            snap_base = os.path.join(base, server_name, "snapshots")
            if not os.path.isdir(snap_base):
                self._log("  Snapshot dir not found, skipping retention: {}".format(snap_base))
                return
            snapshots = [
                d for d in os.listdir(snap_base)
                if os.path.isdir(os.path.join(snap_base, d))
                and re.match(r"^\d{4}-\d{2}-\d{2}_\d{6}$", d)
            ]
            to_delete = self._gfs_calculate_deletions(snapshots)
            for snap in sorted(to_delete):
                path = os.path.join(snap_base, snap)
                self._log("  Pruning old snapshot: {}".format(snap))
                shutil.rmtree(path, ignore_errors=True)

        elif transfer_mode == "smbclient":
            subdir          = self._settings.get(["smb_subdir"]) or "OctoPrint"
            remote_snap_base = "{}/{}/snapshots".format(subdir, server_name)
            snapshots        = self._smb_list_subdirs(remote_snap_base)
            to_delete        = self._gfs_calculate_deletions(snapshots)
            for snap in sorted(to_delete):
                remote_path = "{}/{}".format(remote_snap_base, snap)
                self._log("  Pruning remote snapshot: {}".format(snap))
                rc, out, err = self._smb_exec("deltree \"{}\"".format(remote_path))
                if rc != 0:
                    self._log(
                        "    deltree failed (exit {}): {}".format(rc, err.strip()), "WARNING"
                    )

        self._log("  Retention applied.")

    def _gfs_calculate_deletions(self, snapshot_names):
        """
        Pure-Python GFS logic.
        Returns a set of snapshot names that should be deleted.
        """
        keep_daily   = max(0, int(self._settings.get(["keep_daily"])   or 7))
        keep_weekly  = max(0, int(self._settings.get(["keep_weekly"])  or 8))
        keep_monthly = max(0, int(self._settings.get(["keep_monthly"]) or 12))
        keep_yearly  = max(0, int(self._settings.get(["keep_yearly"])  or 5))

        now      = datetime.datetime.now()
        to_keep  = set()
        seen_wk  = set()
        seen_mo  = set()
        seen_yr  = set()

        # Parse snapshot names
        parsed = []
        for name in snapshot_names:
            m = re.match(
                r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(\d{2})$", name
            )
            if m:
                try:
                    dt = datetime.datetime(
                        int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)), int(m.group(6)),
                    )
                    parsed.append((dt, name))
                except ValueError:
                    to_keep.add(name)  # can't parse → keep
            else:
                to_keep.add(name)  # unknown format → keep

        parsed.sort(reverse=True)  # newest first

        for dt, name in parsed:
            age_days = (now - dt).days

            # Daily window
            if age_days < keep_daily:
                to_keep.add(name)
                continue

            # Weekly window — keep first (newest) seen per ISO week
            age_weeks = age_days // 7
            if age_weeks < keep_weekly:
                iso_y, iso_w, _ = dt.isocalendar()
                week_key = "{}-W{:02d}".format(iso_y, iso_w)
                if week_key not in seen_wk:
                    seen_wk.add(week_key)
                    to_keep.add(name)
                continue

            # Monthly window
            age_months = age_days // 30
            if age_months < keep_monthly:
                month_key = "{}-{:02d}".format(dt.year, dt.month)
                if month_key not in seen_mo:
                    seen_mo.add(month_key)
                    to_keep.add(name)
                continue

            # Yearly window
            age_years = age_days // 365
            if age_years < keep_yearly:
                year_key = str(dt.year)
                if year_key not in seen_yr:
                    seen_yr.add(year_key)
                    to_keep.add(name)
                continue

            # Falls outside all windows → delete (implicitly, by not being in to_keep)

        all_names = {name for _, name in parsed}
        return all_names - to_keep

    # ──────────────────────────────────────────
    # SMB helpers
    # ──────────────────────────────────────────

    def _smb_exec(self, command):
        """
        Run a smbclient command against the configured share.
        Returns (returncode, stdout, stderr).
        Credentials are written to a per-call temp file and deleted immediately.
        """
        host  = self._settings.get(["smb_host"])  or ""
        share = self._settings.get(["smb_share"]) or ""
        unc   = "//{}/{}".format(host.strip("/"), share.strip("/"))

        fd, cred_file = tempfile.mkstemp(prefix="nasbackup_creds_")
        try:
            os.close(fd)
            os.chmod(cred_file, 0o600)
            with open(cred_file, "w") as cf:
                cf.write("username={}\n".format(
                    self._settings.get(["smb_username"]) or "guest"
                ))
                cf.write("password={}\n".format(
                    self._settings.get(["smb_password"]) or ""
                ))
                domain = self._settings.get(["smb_domain"])
                if domain:
                    cf.write("domain={}\n".format(domain))

            version = self._settings.get(["smb_version"]) or "3.0"
            # smbclient -m accepts only "SMB2" or "SMB3", not "SMB30" etc.
            smb_proto = "SMB3" if version.startswith("3") else "SMB2"
            cmd = [
                "smbclient", unc,
                "-A", cred_file,
                "--option=client min protocol=SMB2",
                "-m", smb_proto,
                "-c", command,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
            return result.returncode, result.stdout, result.stderr

        except FileNotFoundError:
            return 127, "", "smbclient binary not found — install: sudo apt install smbclient"
        finally:
            try:
                os.unlink(cred_file)
            except Exception:
                pass

    def _smb_mkdir_p(self, path):
        """Create each component of a remote path (mkdir -p via sequential mkdir calls)."""
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        current = ""
        for part in parts:
            current = "{}/{}".format(current, part) if current else part
            # Ignore errors — directory may already exist
            self._smb_exec("mkdir \"{}\"".format(current))

    def _smb_list_subdirs(self, remote_path):
        """Return a list of sub-directory names inside a remote SMB path."""
        rc, out, err = self._smb_exec("ls \"{}\"".format(remote_path))
        dirs = []
        if rc != 0:
            return dirs
        for line in out.splitlines():
            # smbclient ls: "  name   D   size   date"
            m = re.match(r"^\s+(\S+)\s+D\s+", line)
            if m:
                name = m.group(1)
                if name not in (".", ".."):
                    dirs.append(name)
        return dirs

    # ──────────────────────────────────────────
    # Test connection
    # ──────────────────────────────────────────

    def _test_connection(self):
        mode = self._settings.get(["transfer_mode"])

        if mode == "local":
            path = self._settings.get(["local_path"])
            if not os.path.isdir(path):
                return {"success": False, "message": "Path does not exist: {}".format(path)}
            test = os.path.join(path, ".nasbackup_writetest")
            try:
                with open(test, "w") as f:
                    f.write("ok")
                os.unlink(test)
                return {"success": True, "message": "Path is accessible and writable: {}".format(path)}
            except Exception as exc:
                return {"success": False, "message": "Path not writable: {}".format(exc)}

        elif mode == "smbclient":
            if not shutil.which("smbclient"):
                return {
                    "success": False,
                    "message": "smbclient not found. Install it: sudo apt install smbclient"
                }
            rc, out, err = self._smb_exec("ls")
            if rc == 0:
                return {"success": True, "message": "SMB connection successful."}
            else:
                detail = (err or out or "unknown error").strip().splitlines()[0]
                return {"success": False, "message": "SMB failed: {}".format(detail)}

        return {"success": False, "message": "Unknown transfer mode: {}".format(mode)}

    # ──────────────────────────────────────────
    # Utility helpers
    # ──────────────────────────────────────────

    def _get_server_name(self):
        if self._settings.get_boolean(["server_name_auto"]):
            # Try OctoPrint appearance name
            try:
                name = self._settings.global_get(["appearance", "name"])
                if name and name.strip():
                    return self._sanitize_name(name.strip())
            except Exception:
                pass
            # Fall back to hostname
            try:
                return self._sanitize_name(socket.gethostname())
            except Exception:
                pass
            return "OctoPrint"
        else:
            name = self._settings.get(["server_name_manual"]) or "OctoPrint"
            return self._sanitize_name(name)

    @staticmethod
    def _sanitize_name(name):
        name = re.sub(r"\s+", "_", name)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        name = re.sub(r"_+", "_", name)
        name = name.strip("_")
        return name or "OctoPrint"

    def _get_octoprint_backup_dir(self):
        return os.path.join(
            self._settings.global_get_basefolder("data"),
            "backup",
        )

    def _get_system_items(self):
        raw = self._settings.get(["system_backup_items"]) or ""
        return [x.strip() for x in raw.splitlines() if x.strip()]

    def _write_log_file(self, path):
        with open(path, "w") as f:
            for entry in self._log_entries:
                f.write(entry + "\n")

    def _write_metadata_file(self, path, zip_path, timestamp, server_name):
        content = self._build_metadata_text(zip_path, timestamp, server_name)
        with open(path, "w") as f:
            f.write(content)

    def _build_metadata_text(self, zip_path, timestamp, server_name):
        try:
            zip_size = "{:.2f} MB".format(os.path.getsize(zip_path) / 1_048_576)
        except Exception:
            zip_size = "?"
        lines = [
            "OctoPrint-NASBackup metadata",
            "=" * 40,
            "Timestamp    : {}".format(timestamp),
            "Hostname     : {}".format(socket.gethostname()),
            "Server name  : {}".format(server_name),
            "Plugin ver   : {}".format(self._plugin_version),
            "ZIP file     : {}".format(os.path.basename(zip_path)),
            "ZIP size     : {}".format(zip_size),
            "Transfer mode: {}".format(self._settings.get(["transfer_mode"])),
            "Excludes     : uploads={} timelapse={}".format(
                self._settings.get_boolean(["exclude_uploads"]),
                self._settings.get_boolean(["exclude_timelapse"]),
            ),
            "Retention    : daily={} weekly={} monthly={} yearly={}".format(
                self._settings.get(["keep_daily"]),
                self._settings.get(["keep_weekly"]),
                self._settings.get(["keep_monthly"]),
                self._settings.get(["keep_yearly"]),
            ),
        ]
        return "\n".join(lines) + "\n"

    # ──────────────────────────────────────────
    # Logging helpers
    # ──────────────────────────────────────────

    def _log(self, message, level="INFO"):
        ts    = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = "[{}] [{}] {}".format(ts, level, message)
        self._log_entries.append(entry)

        if level == "ERROR":
            self._logger.error(message)
        elif level == "WARNING":
            self._logger.warning(message)
        elif level == "DEBUG":
            self._logger.debug(message)
        else:
            self._logger.info(message)

    def _plugin_log(self, message):
        """Log to OctoPrint's logger only (not in-memory buffer)."""
        self._logger.info(message)

    def _set_status(self, status, message):
        self._last_status = {
            "status":  status,
            "message": message,
            "time":    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Plugin registration
# ─────────────────────────────────────────────────────────────────────────────

__plugin_name__          = "NAS Backup"
__plugin_identifier__    = "nasbackup"
__plugin_pythoncompat__  = ">=3.7,<4"
__plugin_version__       = "0.1.0"
__plugin_description__   = (
    "Automated OctoPrint backups to a NAS — "
    "scheduled (daily/weekly/monthly), GFS retention, SMB or local path."
)
__plugin_author__        = "KrX3D"
__plugin_url__           = "https://github.com/KrX3D/OctoPrint-NASBackup"
__plugin_license__       = "MIT"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = NasBackupPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {}