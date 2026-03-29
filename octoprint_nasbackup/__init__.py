# coding=utf-8
"""
OctoPrint-NASBackup  -  __init__.py
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

MAX_LOG_ENTRIES = 400


class NasBackupPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SimpleApiPlugin,
):

    def __init__(self):
        self._schedule_thread  = None
        self._schedule_stop    = threading.Event()
        self._startup_timer    = None
        self._next_run         = None   # datetime or None
        self._backup_running   = False
        self._backup_lock      = threading.Lock()
        self._log_entries      = deque(maxlen=MAX_LOG_ENTRIES)
        self._last_status      = {
            "status":  "never",
            "message": "No backup has been run yet.",
            "time":    None,
        }

    # ── SettingsPlugin ────────────────────────────────────────────────────────

    def get_settings_defaults(self):
        return dict(
            enabled=True,
            # Schedule
            schedule_type="daily",
            schedule_time="03:00",
            schedule_day_of_week=0,
            schedule_day_of_month=1,
            only_when_idle=True,
            # Startup backup
            backup_on_startup=False,
            startup_delay=120,
            # Transfer
            transfer_mode="smbclient",
            local_path="/mnt/octoprint_backup",
            # SMB
            smb_host="192.168.1.11",
            smb_share="backup",
            smb_subdir="OctoPrint",
            smb_username="",
            smb_password="",
            smb_domain="",
            smb_version="3.0",
            # Backup options
            exclude_uploads=False,
            exclude_timelapse=True,
            local_keep_count=5,
            # System files
            system_backup_enabled=False,
            system_backup_items=(
                "/etc/fstab\n"
                "/etc/hostname\n"
                "/etc/hosts\n"
                "/home/pi/.octoprint/config.yaml\n"
                "/etc/crontab"
            ),
            # Retention
            retention_enabled=True,
            keep_daily=7,
            keep_weekly=8,
            keep_monthly=12,
            keep_yearly=5,
            # Misc
            # NOTE: stored as string "true"/"false" because KO radio buttons
            # send strings. Use _get_bool() helper, not get_boolean().
            server_name_auto="true",
            server_name_manual="OctoPrint",
            copy_log_to_nas=False,
        )

    def on_settings_save(self, data):
        old_enabled  = self._get_bool("enabled")
        old_stype    = self._settings.get(["schedule_type"])
        old_stime    = self._settings.get(["schedule_time"])

        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self._force_smb_mode()

        new_enabled  = self._get_bool("enabled")
        new_stype    = self._settings.get(["schedule_type"])
        new_stime    = self._settings.get(["schedule_time"])

        self._plugin_log(
            "Settings saved. enabled={} schedule_type={} schedule_time={} "
            "transfer_mode={} smb_host={} smb_share={} smb_subdir={}".format(
                new_enabled,
                new_stype,
                new_stime,
                self._settings.get(["transfer_mode"]),
                self._settings.get(["smb_host"]),
                self._settings.get(["smb_share"]),
                self._settings.get(["smb_subdir"]),
            )
        )

        if old_enabled != new_enabled or old_stype != new_stype or old_stime != new_stime:
            self._plugin_log("Schedule-relevant setting changed, rescheduling.")
            self._reschedule()

    # ── TemplatePlugin ────────────────────────────────────────────────────────

    def get_template_configs(self):
        return [dict(
            type="settings",
            name="NAS Backup",
            template="nasbackup_settings.jinja2",
            custom_bindings=True,
        )]

    # ── AssetPlugin ───────────────────────────────────────────────────────────

    def get_assets(self):
        return dict(js=["js/nasbackup.js"], css=["css/nasbackup.css"])

    # ── SoftwareUpdate (hook only) ────────────────────────────────────────────

    def get_update_information(self):
        return dict(
            nasbackup=dict(
                displayName=__plugin_name__,
                displayVersion=self._plugin_version,
                type="github_release",
                user="KrX3D",
                repo="OctoPrint-NASBackup",
                current=self._plugin_version,
                pip="https://github.com/KrX3D/OctoPrint-NASBackup/archive/{target}.zip",
            )
        )

    # ── StartupPlugin ─────────────────────────────────────────────────────────

    def on_after_startup(self):
        self._force_smb_mode()
        self._plugin_log(
            "NAS Backup plugin started (v{})".format(self._plugin_version)
        )

        enabled         = self._get_bool("enabled")
        backup_on_start = self._get_bool("backup_on_startup")
        delay           = int(self._settings.get(["startup_delay"]) or 120)

        self._plugin_log(
            "Startup config: enabled={} backup_on_startup={} startup_delay={}s "
            "server_name_auto={} transfer_mode={}".format(
                enabled,
                backup_on_start,
                delay,
                self._settings.get(["server_name_auto"]),
                self._settings.get(["transfer_mode"]),
            )
        )

        # Start the schedule thread
        self._reschedule()

        # Startup backup
        if enabled and backup_on_start:
            self._plugin_log("Startup backup armed — will fire in {}s.".format(delay))
            self._startup_timer = threading.Timer(
                delay, lambda: self._run_backup(source="startup")
            )
            self._startup_timer.daemon = True
            self._startup_timer.start()
        else:
            self._plugin_log(
                "Startup backup NOT armed (enabled={}, backup_on_startup={}).".format(
                    enabled, backup_on_start
                )
            )

    # ── ShutdownPlugin ────────────────────────────────────────────────────────

    def on_shutdown(self):
        self._schedule_stop.set()
        if self._startup_timer is not None:
            self._startup_timer.cancel()
            self._startup_timer = None

    # ── SimpleApiPlugin ───────────────────────────────────────────────────────

    def get_api_commands(self):
        return dict(
            trigger_backup=[],
            test_connection=[],
            clear_logs=[],
        )

    def on_api_command(self, command, data):
        self._plugin_log("API command received: {}".format(command))

        if command == "trigger_backup":
            if self._backup_running:
                self._plugin_log("Trigger rejected — backup already running.")
                return flask.jsonify({
                    "success": False,
                    "message": "A backup is already running."
                }), 409
            t = threading.Thread(
                target=lambda: self._run_backup(source="manual"),
                name="nasbackup-thread",
                daemon=True
            )
            t.start()
            return flask.jsonify({"success": True, "message": "Backup started."})

        elif command == "test_connection":
            result = self._test_connection()
            self._plugin_log("Test connection result: {}".format(result))
            return flask.jsonify(result)

        elif command == "clear_logs":
            self._log_entries.clear()
            return flask.jsonify({"success": True})
        return flask.abort(400)

    def on_api_get(self, request):
        next_run_str = None
        if self._next_run is not None:
            try:
                next_run_str = self._next_run.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        return flask.jsonify(dict(
            running=self._backup_running,
            last_status=self._last_status,
            next_run=next_run_str,
            startup_pending=(
                self._startup_timer is not None and self._startup_timer.is_alive()
            ),
            smbclient_installed=bool(shutil.which("smbclient")),
            smbclient_install_hint=self._suggest_install_command(),
            logs=list(self._log_entries),
        ))

    # ── Scheduling (pure Python, no APScheduler needed) ───────────────────────

    def _reschedule(self):
        """Stop existing schedule thread and start a new one with current settings."""
        # Signal old thread to stop
        self._schedule_stop.set()
        if self._schedule_thread and self._schedule_thread.is_alive():
            self._schedule_thread.join(timeout=2)

        self._schedule_stop.clear()
        self._next_run = None

        if not self._get_bool("enabled"):
            self._plugin_log("Scheduling skipped — plugin disabled.")
            return

        schedule_type = self._settings.get(["schedule_type"])
        if schedule_type == "disabled":
            self._plugin_log("Schedule type is disabled — manual trigger only.")
            return

        next_run = self._calc_next_run()
        if next_run is None:
            self._plugin_log(
                "WARNING: Could not calculate next run for type '{}'.".format(schedule_type)
            )
            return

        self._next_run = next_run
        self._plugin_log(
            "Backup scheduled: type={} next_run={}".format(
                schedule_type, next_run.strftime("%Y-%m-%d %H:%M:%S")
            )
        )

        self._schedule_thread = threading.Thread(
            target=self._schedule_loop, name="nasbackup-scheduler", daemon=True
        )
        self._schedule_thread.start()

    def _schedule_loop(self):
        """
        Runs in background thread. Sleeps until next_run, fires backup,
        recalculates next_run, repeats.
        """
        while not self._schedule_stop.is_set():
            now     = datetime.datetime.now()
            target  = self._next_run

            if target is None:
                break

            wait_seconds = (target - now).total_seconds()

            if wait_seconds > 0:
                # Sleep in 30s chunks so we can respond to stop events
                while wait_seconds > 0 and not self._schedule_stop.is_set():
                    chunk = min(30, wait_seconds)
                    self._schedule_stop.wait(chunk)
                    wait_seconds -= chunk

            if self._schedule_stop.is_set():
                break

            # Fire backup
            self._plugin_log("Scheduled backup firing now.")
            self._plugin_manager.send_plugin_message(
                self._identifier, {"event": "scheduled_backup_started"}
            )
            self._run_backup(source="scheduled")

            # Calculate next run
            next_run = self._calc_next_run()
            if next_run is None:
                break
            self._next_run = next_run
            self._plugin_log(
                "Next scheduled backup: {}".format(
                    next_run.strftime("%Y-%m-%d %H:%M:%S")
                )
            )

    def _calc_next_run(self):
        """Calculate the next datetime this backup should run."""
        schedule_type = self._settings.get(["schedule_type"])
        time_str      = self._settings.get(["schedule_time"]) or "03:00"

        try:
            hour, minute = map(int, time_str.split(":"))
        except Exception:
            self._plugin_log(
                "Invalid schedule_time '{}', defaulting to 03:00".format(time_str)
            )
            hour, minute = 3, 0

        now = datetime.datetime.now()

        if schedule_type == "daily":
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += datetime.timedelta(days=1)
            return candidate

        elif schedule_type == "weekly":
            dow = int(self._settings.get(["schedule_day_of_week"]) or 0)
            # dow: 0=Mon … 6=Sun, matches Python weekday()
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            days_ahead = (dow - now.weekday()) % 7
            if days_ahead == 0 and candidate <= now:
                days_ahead = 7
            return candidate + datetime.timedelta(days=days_ahead)

        elif schedule_type == "monthly":
            dom = max(1, min(28, int(self._settings.get(["schedule_day_of_month"]) or 1)))
            # Try this month first
            try:
                candidate = now.replace(
                    day=dom, hour=hour, minute=minute, second=0, microsecond=0
                )
            except ValueError:
                candidate = None

            if candidate is None or candidate <= now:
                # Move to next month
                if now.month == 12:
                    next_month = now.replace(year=now.year + 1, month=1)
                else:
                    next_month = now.replace(month=now.month + 1)
                try:
                    candidate = next_month.replace(
                        day=dom, hour=hour, minute=minute, second=0, microsecond=0
                    )
                except ValueError:
                    return None
            return candidate

        return None

    # ── Path variable resolution ──────────────────────────────────────────────

    def _resolve_vars(self, path):
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "octopi"
        return (path or "").replace("{hostname}", hostname)

    # ── Boolean helper ────────────────────────────────────────────────────────

    def _get_bool(self, key):
        """
        Safe boolean getter that handles both Python bool and string "true"/"false"
        (KO radio buttons send strings back; checkboxes send actual booleans).
        """
        val = self._settings.get([key])
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("true", "1", "yes")
        return bool(val)

    # ── Core backup orchestration ─────────────────────────────────────────────

    def _run_backup(self, source="manual"):
        acquired = self._backup_lock.acquire(blocking=False)
        if not acquired:
            self._plugin_log("Could not acquire backup lock — already running.")
            return
        if self._backup_running:
            self._backup_lock.release()
            return
        self._backup_running = True
        self._backup_lock.release()

        start_time = datetime.datetime.now()
        timestamp  = start_time.strftime("%Y-%m-%d_%H%M%S")

        try:
            self._log("=" * 60)
            self._log("NAS Backup started  [{} v{}]".format(
                socket.gethostname(), self._plugin_version))
            self._log("Trigger       : {}".format(source))
            self._log("Timestamp     : {}".format(timestamp))
            self._log("Plugin enabled: {}".format(self._get_bool("enabled")))
            self._log("Transfer mode : {}".format(self._settings.get(["transfer_mode"])))
            self._log("=" * 60)

            if not self._get_bool("enabled"):
                self._log("Plugin is disabled — aborting.", "WARNING")
                self._set_status("skipped", "Skipped — plugin disabled.")
                return

            if not shutil.which("smbclient"):
                hint = self._suggest_install_command()
                self._log("smbclient missing — cannot run backup.", "ERROR")
                self._set_status("failed", "smbclient missing. {}".format(hint))
                return

            if self._get_bool("only_when_idle"):
                printing = self._printer.is_printing()
                paused   = self._printer.is_paused()
                self._log("Idle check: printing={} paused={}".format(printing, paused))
                if printing or paused:
                    self._log("Printer is busy — skipping.")
                    self._set_status("skipped", "Skipped — printer busy.")
                    return

            server_name   = self._get_server_name()
            transfer_mode = self._get_transfer_mode()
            self._log("Server name   : {}".format(server_name))
            self._log("Transfer mode : {}".format(transfer_mode))

            # Step 1
            self._log("")
            self._log("Step 1/4 — Creating OctoPrint backup ZIP...")
            zip_path = self._trigger_octoprint_backup()
            self._log("ZIP created : {} ({:.1f} MB)".format(
                os.path.basename(zip_path),
                os.path.getsize(zip_path) / 1_048_576,
            ))

            # Step 2
            self._log("")
            self._log("Step 2/4 — Transferring to NAS...")
            if transfer_mode == "smbclient":
                self._transfer_smbclient(zip_path, server_name, timestamp)
            else:
                raise RuntimeError("Unknown transfer_mode: '{}'".format(transfer_mode))

            # Step 3
            self._log("")
            self._log("Step 3/4 — Pruning local OctoPrint ZIPs...")
            self._prune_local_zips()

            # Step 4
            if self._get_bool("retention_enabled"):
                self._log("")
                self._log("Step 4/4 — Applying GFS retention on NAS...")
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
            self._log("BACKUP FAILED: {}".format(exc), "ERROR")
            self._log(traceback.format_exc(), "DEBUG")
            self._set_status("failed", str(exc))
        finally:
            self._backup_running = False

    # ── Step 1 ────────────────────────────────────────────────────────────────

    def _trigger_octoprint_backup(self):
        backup_plugin = self._plugin_manager.get_plugin_info("backup")
        if not backup_plugin:
            raise RuntimeError(
                "OctoPrint backup plugin not found — make sure it is enabled."
            )

        excludes = []
        if self._get_bool("exclude_uploads"):
            excludes.append("uploads")
        if self._get_bool("exclude_timelapse"):
            excludes.append("timelapse")

        self._log("  Excludes: {}".format(excludes or "none"))

        backup_dir = self._get_octoprint_backup_dir()
        os.makedirs(backup_dir, exist_ok=True)
        before = set(glob.glob(os.path.join(backup_dir, "*.zip")))

        try:
            import inspect
            from octoprint.server import app as octoprint_app
            with octoprint_app.app_context():
                create_backup = inspect.unwrap(backup_plugin.implementation.create_backup)
                if getattr(create_backup, "__self__", None) is backup_plugin.implementation:
                    result = create_backup(exclude=excludes)
                else:
                    result = create_backup(backup_plugin.implementation, exclude=excludes)
        except Exception as exc:
            raise RuntimeError("OctoPrint backup plugin raised: {}".format(exc))

        if result and isinstance(result, str) and os.path.isfile(result):
            return result

        after   = set(glob.glob(os.path.join(backup_dir, "*.zip")))
        new_zip = after - before
        if new_zip:
            return max(new_zip, key=os.path.getmtime)

        all_zips = sorted(
            glob.glob(os.path.join(backup_dir, "*.zip")),
            key=os.path.getmtime, reverse=True
        )
        if all_zips:
            age = (datetime.datetime.now() -
                   datetime.datetime.fromtimestamp(
                       os.path.getmtime(all_zips[0]))).total_seconds()
            if age < 120:
                return all_zips[0]

        raise RuntimeError("No new backup ZIP detected after triggering OctoPrint backup.")

    # ── Step 2a ───────────────────────────────────────────────────────────────

    def _transfer_local(self, zip_path, server_name, timestamp):
        base     = self._resolve_vars(
            self._settings.get(["local_path"]) or "/mnt/octoprint_backup"
        )
        snap_dir = os.path.join(base, server_name, "snapshots", timestamp)
        self._log("  Local snap dir: {}".format(snap_dir))
        os.makedirs(snap_dir, exist_ok=True)

        dest = os.path.join(snap_dir, os.path.basename(zip_path))
        shutil.copy2(zip_path, dest)
        self._log("  ZIP -> {}".format(dest))

        if self._get_bool("system_backup_enabled"):
            self._backup_system_files_local(snap_dir)
        if self._get_bool("copy_log_to_nas"):
            self._write_log_file(os.path.join(snap_dir, "backup.log"))

        self._write_metadata_file(
            os.path.join(snap_dir, "_backup_info.txt"),
            zip_path, timestamp, server_name
        )

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

        self._log("  Local transfer complete.")

    def _backup_system_files_local(self, snap_dir):
        items   = self._get_system_items()
        sys_dir = os.path.join(snap_dir, "system_files")
        copied  = 0
        for item in items:
            if not os.path.exists(item):
                self._log("  [system] Not found: {}".format(item), "WARNING")
                continue
            rel  = item.lstrip("/")
            dest = os.path.join(sys_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                if os.path.isdir(item):
                    try:
                        shutil.copytree(item, dest, dirs_exist_ok=True)
                    except TypeError:
                        if os.path.exists(dest):
                            shutil.rmtree(dest)
                        shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
                copied += 1
            except Exception as exc:
                self._log("  [system] Failed {}: {}".format(item, exc), "WARNING")
        self._log("  System files: {} item(s) copied.".format(copied))

    # ── Step 2b ───────────────────────────────────────────────────────────────

    def _transfer_smbclient(self, zip_path, server_name, timestamp):
        subdir      = self._resolve_vars(
            self._settings.get(["smb_subdir"]) or "OctoPrint"
        )
        remote_snap = "{}/{}/snapshots/{}".format(subdir, server_name, timestamp)
        self._log("  Remote snap path: {}".format(remote_snap))

        self._smb_mkdir_p(remote_snap)

        remote_zip = "{}/{}".format(remote_snap, os.path.basename(zip_path))
        self._log("  Uploading ZIP -> {}".format(remote_zip))
        rc, out, err = self._smb_exec("put \"{}\" \"{}\"".format(zip_path, remote_zip))
        if rc != 0:
            raise RuntimeError(
                "smbclient upload failed (exit {}): {}".format(rc, err.strip())
            )
        self._log("  ZIP upload OK.")

        if self._get_bool("system_backup_enabled"):
            self._backup_system_files_smbclient(remote_snap)
        if self._get_bool("copy_log_to_nas"):
            self._upload_temp_file_smbclient(
                lambda f: self._write_log_file(f),
                "{}/backup.log".format(remote_snap),
                suffix=".log",
            )

        meta = self._build_metadata_text(zip_path, timestamp, server_name)
        self._upload_temp_file_smbclient(
            lambda f: open(f, "w").write(meta) or None,
            "{}/_backup_info.txt".format(remote_snap),
            suffix=".txt",
        )
        self._log("  SMB transfer complete.")

    def _backup_system_files_smbclient(self, remote_snap):
        items    = self._get_system_items()
        existing = [x for x in items if os.path.exists(x)]
        if not existing:
            self._log("  [system] No items found.", "WARNING")
            return
        fd, tar_path = tempfile.mkstemp(prefix="nasbackup_sys_", suffix=".tar.gz")
        os.close(fd)
        try:
            result = subprocess.run(
                ["tar", "-czf", tar_path] + existing,
                capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0:
                self._log("  [system] tar failed: {}".format(result.stderr.strip()), "WARNING")
                return
            remote_tar = "{}/system_backup.tar.gz".format(remote_snap)
            rc, out, err = self._smb_exec(
                "put \"{}\" \"{}\"".format(tar_path, remote_tar)
            )
            if rc != 0:
                self._log("  [system] Upload failed: {}".format(err.strip()), "WARNING")
            else:
                self._log("  System files: {:.1f} MB uploaded.".format(
                    os.path.getsize(tar_path) / 1_048_576
                ))
        finally:
            try:
                os.unlink(tar_path)
            except Exception:
                pass

    def _upload_temp_file_smbclient(self, write_fn, remote_path, suffix=".tmp"):
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

    # ── Step 3 ────────────────────────────────────────────────────────────────

    def _prune_local_zips(self):
        keep = int(self._settings.get(["local_keep_count"]) or 5)
        if keep <= 0:
            self._log("  local_keep_count=0 — skipping prune.")
            return
        backup_dir = self._get_octoprint_backup_dir()
        zips = sorted(
            glob.glob(os.path.join(backup_dir, "*.zip")),
            key=os.path.getmtime, reverse=True
        )
        self._log("  Local ZIPs: {} present, keeping last {}.".format(len(zips), keep))
        for old in zips[keep:]:
            try:
                os.unlink(old)
                self._log("  Deleted: {}".format(os.path.basename(old)))
            except Exception as exc:
                self._log("  Could not delete {}: {}".format(old, exc), "WARNING")

    # ── Step 4 ────────────────────────────────────────────────────────────────

    def _apply_retention(self, server_name, transfer_mode):
        if transfer_mode == "local":
            base      = self._resolve_vars(
                self._settings.get(["local_path"]) or "/mnt/octoprint_backup"
            )
            snap_base = os.path.join(base, server_name, "snapshots")
            if not os.path.isdir(snap_base):
                self._log("  Snapshot dir not found: {}".format(snap_base))
                return
            snapshots = [
                d for d in os.listdir(snap_base)
                if os.path.isdir(os.path.join(snap_base, d))
                and re.match(r"^\d{4}-\d{2}-\d{2}_\d{6}$", d)
            ]
            self._log("  Found {} snapshot(s) to evaluate.".format(len(snapshots)))
            to_delete = self._gfs_calculate_deletions(snapshots)
            self._log("  Will delete {} snapshot(s).".format(len(to_delete)))
            for snap in sorted(to_delete):
                self._log("  Pruning: {}".format(snap))
                shutil.rmtree(os.path.join(snap_base, snap), ignore_errors=True)

        elif transfer_mode == "smbclient":
            subdir           = self._resolve_vars(
                self._settings.get(["smb_subdir"]) or "OctoPrint"
            )
            remote_snap_base = "{}/{}/snapshots".format(subdir, server_name)
            snapshots        = self._smb_list_subdirs(remote_snap_base)
            self._log("  Found {} remote snapshot(s).".format(len(snapshots)))
            to_delete = self._gfs_calculate_deletions(snapshots)
            self._log("  Will delete {} remote snapshot(s).".format(len(to_delete)))
            for snap in sorted(to_delete):
                self._log("  Pruning remote: {}".format(snap))
                rc, out, err = self._smb_exec(
                    "deltree \"{}\"".format("{}/{}".format(remote_snap_base, snap))
                )
                if rc != 0:
                    self._log("    deltree failed: {}".format(err.strip()), "WARNING")

        self._log("  Retention applied.")

    def _gfs_calculate_deletions(self, snapshot_names):
        keep_daily   = max(0, int(self._settings.get(["keep_daily"])   or 7))
        keep_weekly  = max(0, int(self._settings.get(["keep_weekly"])  or 8))
        keep_monthly = max(0, int(self._settings.get(["keep_monthly"]) or 12))
        keep_yearly  = max(0, int(self._settings.get(["keep_yearly"])  or 5))

        now     = datetime.datetime.now()
        to_keep = set()
        seen_wk = set()
        seen_mo = set()
        seen_yr = set()
        parsed  = []

        for name in snapshot_names:
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(\d{2})$", name)
            if m:
                try:
                    parsed.append((datetime.datetime(
                        int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)), int(m.group(6)),
                    ), name))
                except ValueError:
                    to_keep.add(name)
            else:
                to_keep.add(name)

        parsed.sort(reverse=True)

        for dt, name in parsed:
            age_days = (now - dt).days
            if age_days < keep_daily:
                to_keep.add(name); continue
            age_weeks = age_days // 7
            if age_weeks < keep_weekly:
                key = "{}-W{:02d}".format(*dt.isocalendar()[:2])
                if key not in seen_wk:
                    seen_wk.add(key); to_keep.add(name)
                continue
            age_months = age_days // 30
            if age_months < keep_monthly:
                key = "{}-{:02d}".format(dt.year, dt.month)
                if key not in seen_mo:
                    seen_mo.add(key); to_keep.add(name)
                continue
            age_years = age_days // 365
            if age_years < keep_yearly:
                key = str(dt.year)
                if key not in seen_yr:
                    seen_yr.add(key); to_keep.add(name)
                continue

        return {name for _, name in parsed} - to_keep

    # ── SMB helpers ───────────────────────────────────────────────────────────

    def _smb_exec(self, command):
        host  = self._settings.get(["smb_host"])  or ""
        share = self._settings.get(["smb_share"]) or ""
        unc   = "//{}/{}".format(host.strip("/"), share.strip("/"))

        fd, cred_file = tempfile.mkstemp(prefix="nasbackup_creds_")
        try:
            os.close(fd)
            os.chmod(cred_file, 0o600)
            with open(cred_file, "w") as cf:
                cf.write("username={}\n".format(
                    self._settings.get(["smb_username"]) or "guest"))
                cf.write("password={}\n".format(
                    self._settings.get(["smb_password"]) or ""))
                domain = self._settings.get(["smb_domain"])
                if domain:
                    cf.write("domain={}\n".format(domain))

            smb_proto = "SMB3" if (
                self._settings.get(["smb_version"]) or "3.0"
            ).startswith("3") else "SMB2"

            result = subprocess.run(
                ["smbclient", unc, "-A", cred_file,
                 "--option=client min protocol=SMB2",
                 "-m", smb_proto, "-c", command],
                capture_output=True, text=True, timeout=120
            )
            return result.returncode, result.stdout, result.stderr
        except FileNotFoundError:
            return 127, "", "smbclient not found — install: sudo apt install smbclient"
        finally:
            try:
                os.unlink(cred_file)
            except Exception:
                pass

    def _smb_mkdir_p(self, path):
        parts   = [p for p in path.replace("\\", "/").split("/") if p]
        current = ""
        for part in parts:
            current = "{}/{}".format(current, part) if current else part
            self._smb_exec("mkdir \"{}\"".format(current))

    def _smb_list_subdirs(self, remote_path):
        rc, out, err = self._smb_exec("ls \"{}\"".format(remote_path))
        dirs = []
        if rc != 0:
            return dirs
        for line in out.splitlines():
            m = re.match(r"^\s+(\S+)\s+D\s+", line)
            if m and m.group(1) not in (".", ".."):
                dirs.append(m.group(1))
        return dirs

    # ── Test connection ───────────────────────────────────────────────────────

    def _test_connection(self):
        self._plugin_log("Test connection: mode=smbclient")
        if not shutil.which("smbclient"):
            return {
                "success": False,
                "message": (
                    "smbclient is required on the OctoPrint host. "
                    "Install with: {}".format(self._suggest_install_command())
                ),
            }
        self._plugin_log(
            "Test SMB: host={} share={}".format(
                self._settings.get(["smb_host"]),
                self._settings.get(["smb_share"]),
            )
        )
        rc, out, err = self._smb_exec("ls")
        self._plugin_log("SMB test result: rc={} err={}".format(rc, err.strip()[:100]))
        if rc == 0:
            return {"success": True, "message": "SMB connection successful."}
        detail = (err or out or "unknown error").strip().splitlines()[0]
        return {"success": False, "message": "SMB failed: {}".format(detail)}

    # ── Utility helpers ───────────────────────────────────────────────────────

    def _get_server_name(self):
        auto = self._get_bool("server_name_auto")
        self._log("  server_name_auto={} (raw value='{}')".format(
            auto, self._settings.get(["server_name_auto"])
        ))
        if auto:
            try:
                name = self._settings.global_get(["appearance", "name"])
                if name and name.strip():
                    self._log("  Using OctoPrint appearance name: {}".format(name.strip()))
                    return self._sanitize_name(name.strip())
            except Exception:
                pass
            try:
                h = socket.gethostname()
                self._log("  Using hostname: {}".format(h))
                return self._sanitize_name(h)
            except Exception:
                pass
            return "OctoPrint"
        name = self._settings.get(["server_name_manual"]) or "OctoPrint"
        self._log("  Using manual server name: {}".format(name))
        return self._sanitize_name(name)

    def _force_smb_mode(self):
        mode = self._settings.get(["transfer_mode"])
        if mode != "smbclient":
            self._settings.set(["transfer_mode"], "smbclient")
            self._settings.save()
            self._plugin_log("Migrated transfer_mode '{}' -> 'smbclient'.".format(mode))

    def _get_transfer_mode(self):
        # SMB-only plugin behavior.
        return "smbclient"

    def _suggest_install_command(self):
        if shutil.which("apt-get") or shutil.which("apt"):
            return "sudo apt install smbclient"
        if shutil.which("dnf"):
            return "sudo dnf install samba-client"
        if shutil.which("yum"):
            return "sudo yum install samba-client"
        if shutil.which("zypper"):
            return "sudo zypper install samba-client"
        if shutil.which("pacman"):
            return "sudo pacman -S smbclient"
        return "Install smbclient with your distro package manager"

    @staticmethod
    def _sanitize_name(name):
        name = re.sub(r"\s+", "_", name)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        name = re.sub(r"_+", "_", name).strip("_")
        return name or "OctoPrint"

    def _get_octoprint_backup_dir(self):
        return os.path.join(self._settings.global_get_basefolder("data"), "backup")

    def _get_system_items(self):
        raw = self._settings.get(["system_backup_items"]) or ""
        return [x.strip() for x in raw.splitlines() if x.strip()]

    def _write_log_file(self, path):
        with open(path, "w") as f:
            for entry in self._log_entries:
                f.write(entry + "\n")

    def _write_metadata_file(self, path, zip_path, timestamp, server_name):
        with open(path, "w") as f:
            f.write(self._build_metadata_text(zip_path, timestamp, server_name))

    def _build_metadata_text(self, zip_path, timestamp, server_name):
        try:
            zip_size = "{:.2f} MB".format(os.path.getsize(zip_path) / 1_048_576)
        except Exception:
            zip_size = "?"
        return "\n".join([
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
                self._get_bool("exclude_uploads"),
                self._get_bool("exclude_timelapse"),
            ),
            "Retention    : daily={} weekly={} monthly={} yearly={}".format(
                self._settings.get(["keep_daily"]),
                self._settings.get(["keep_weekly"]),
                self._settings.get(["keep_monthly"]),
                self._settings.get(["keep_yearly"]),
            ),
        ]) + "\n"

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, message, level="INFO"):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._log_entries.append("[{}] [{}] {}".format(ts, level, message))
        if level == "ERROR":
            self._logger.error(message)
        elif level == "WARNING":
            self._logger.warning(message)
        elif level == "DEBUG":
            self._logger.debug(message)
        else:
            self._logger.info(message)

    def _plugin_log(self, message):
        """Logs to octoprint.log only (not the in-UI log buffer)."""
        self._logger.info(message)

    def _set_status(self, status, message):
        self._last_status = {
            "status":  status,
            "message": message,
            "time":    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }


# ── Plugin registration ───────────────────────────────────────────────────────

__plugin_name__         = "NAS Backup"
__plugin_identifier__   = "nasbackup"
__plugin_pythoncompat__ = ">=3.7,<4"
__plugin_version__      = "0.3.7"
__plugin_description__  = (
    "Automated OctoPrint backups to a NAS over SMB - "
    "scheduled (daily/weekly/monthly), GFS retention."
)
__plugin_author__       = "KrX3D"
__plugin_url__          = "https://github.com/KrX3D/OctoPrint-NASBackup"
__plugin_license__      = "MIT"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = NasBackupPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config":
            __plugin_implementation__.get_update_information,
    }
