/*
 * OctoPrint-NASBackup — nasbackup.js
 */
$(function () {

    function NasBackupViewModel(parameters) {
        var self = this;

        self.settingsViewModel   = parameters[0];
        self.loginStateViewModel = parameters[1];

        // Runtime observables — all safe defaults
        self.backupRunning  = ko.observable(false);
        self.startupPending = ko.observable(false);
        self.nextRun        = ko.observable(null);
        self.lastStatus     = ko.observable({
            status: "never", message: "No backup run yet.", time: null
        });
        self.logs           = ko.observableArray([]);
        self.statusPolling  = null;

        self.triggerBusy = ko.observable(false);
        self.testBusy    = ko.observable(false);
        self.smbclientInstalled   = ko.observable(true);
        self.smbclientInstallHint = ko.observable("sudo apt install smbclient");
        self.pluginVersion = ko.observable("?");

        // settings is set in onBeforeBinding — null until then.
        // NEVER call settings.xxx() directly in data-bind attributes.
        // Use the safe computed helpers below instead.
        self.settings = null;

        // ── Safe computed helpers ─────────────────────────────────────
        // These return a safe default if settings is not yet loaded.

        self.isEnabled = ko.computed(function () {
            try {
                var v = self.settings && self.settings.enabled();
                return v === true || v === "true" || v === 1 || v === "1";
            }
            catch (e) { return false; }
        });

        self.scheduleType = ko.computed(function () {
            try {
                var v = self.settings && self.settings.schedule_type();
                return (v || "daily").toString().trim();
            }
            catch (e) { return "daily"; }
        });

        self.transferMode = ko.computed(function () {
            try { return self.settings && self.settings.transfer_mode(); }
            catch (e) { return "local"; }
        });

        self.backupOnStartup = ko.computed(function () {
            try {
                var v = self.settings && self.settings.backup_on_startup();
                return v === true || v === "true" || v === 1 || v === "1";
            }
            catch (e) { return false; }
        });

        self.systemBackupEnabled = ko.computed(function () {
            try { return self.settings && self.settings.system_backup_enabled(); }
            catch (e) { return false; }
        });

        self.retentionEnabled = ko.computed(function () {
            try { return self.settings && self.settings.retention_enabled(); }
            catch (e) { return false; }
        });

        self.serverNameAuto = ko.computed(function () {
            try {
                var v = self.settings && self.settings.server_name_auto();
                return v === true || v === "true";
            }
            catch (e) { return true; }
        });

        // ── Computed for status display ───────────────────────────────
        self.statusClass = ko.computed(function () {
            return "nasbackup-status-" + (self.lastStatus().status || "never");
        });

        self.statusLabel = ko.computed(function () {
            var map = {
                success: "Success", failed: "Failed",
                running: "Running...", skipped: "Skipped", never: "Never run"
            };
            return map[self.lastStatus().status] || (self.lastStatus().status || "never");
        });

        // ── Lifecycle ─────────────────────────────────────────────────

        self.onBeforeBinding = function () {
            self.settings = self.settingsViewModel.settings.plugins.nasbackup;
            try {
                var cur = self.settings.schedule_type();
                if (!cur || !cur.toString().trim()) {
                    self.settings.schedule_type("daily");
                }
            } catch (e) {}
        };

        self.onSettingsShown = function () {
            self.testBusy(false);
            self.triggerBusy(false);
            self.refreshStatus();

            self.statusPolling = setInterval(function () {
                if (self.backupRunning() || self.startupPending()) {
                    self.refreshStatus();
                }
            }, 3000);
        };

        self.onSettingsHidden = function () {
            if (self.statusPolling) {
                clearInterval(self.statusPolling);
                self.statusPolling = null;
            }
        };

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "nasbackup" || !data || !data.event) { return; }
            if (data.event === "scheduled_backup_started") {
                new PNotify({
                    title: "NAS Backup",
                    text: "Scheduled backup started.",
                    type: "info",
                    hide: true
                });
                self.refreshStatus();
                return;
            }
            if (data.event === "backup_status") {
                var typeMap = {
                    success: "success",
                    failed: "error",
                    skipped: "notice",
                    running: "info",
                    never: "info"
                };
                new PNotify({
                    title: "NAS Backup",
                    text: (data.status || "status") + ": " + (data.message || ""),
                    type: typeMap[data.status] || "info",
                    hide: true
                });
                self.refreshStatus();
            }
        };

        // ── API ───────────────────────────────────────────────────────

        self.refreshStatus = function () {
            OctoPrint.get("api/plugin/nasbackup")
                .done(function (data) {
                    self.backupRunning(data.running === true);
                    self.startupPending(data.startup_pending === true);
                    self.lastStatus(data.last_status || {
                        status: "never", message: "", time: null
                    });
                    self.nextRun(data.next_run || null);
                    self.pluginVersion(data.plugin_version || "?");
                    self.smbclientInstalled(data.smbclient_installed === true);
                    self.smbclientInstallHint(data.smbclient_install_hint || "sudo apt install smbclient");
                    if (Array.isArray(data.logs)) {
                        self.logs(data.logs);
                        var el = document.getElementById("nasbackup_log_area");
                        if (el) { el.scrollTop = el.scrollHeight; }
                    }
                });
        };

        self.triggerBackup = function () {
            if (self.triggerBusy() || self.backupRunning()) { return; }
            if (!self.smbclientInstalled()) {
                new PNotify({
                    title: "NAS Backup",
                    text: "smbclient is not installed. " + self.smbclientInstallHint(),
                    type: "error"
                });
                return;
            }
            self.triggerBusy(true);
            OctoPrint.simpleApiCommand("nasbackup", "trigger_backup", {})
                .done(function (data) {
                    if (data.success) {
                        new PNotify({
                            title: "NAS Backup", text: "Backup started.",
                            type: "success", hide: true
                        });
                        self.backupRunning(true);
                        setTimeout(self.refreshStatus, 1000);
                    } else {
                        new PNotify({
                            title: "NAS Backup",
                            text: data.message || "Could not start backup.",
                            type: "error"
                        });
                    }
                })
                .fail(function () {
                    new PNotify({title: "NAS Backup", text: "Request failed.", type: "error"});
                })
                .always(function () { self.triggerBusy(false); });
        };

        self.testConnection = function () {
            if (self.testBusy()) { return; }
            self.testBusy(true);
            OctoPrint.simpleApiCommand("nasbackup", "test_connection", {})
                .done(function (data) {
                    new PNotify({
                        title: "NAS Backup",
                        text: data.message || "Connection test finished.",
                        type: data.success ? "success" : "error",
                        hide: true
                    });
                })
                .fail(function () {
                    new PNotify({
                        title: "NAS Backup",
                        text: "Request to OctoPrint failed.",
                        type: "error"
                    });
                })
                .always(function () { self.testBusy(false); });
        };

        self.clearLogs = function () {
            OctoPrint.simpleApiCommand("nasbackup", "clear_logs", {})
                .done(function () { self.logs([]); });
        };

        self.logLineClass = function (line) {
            if (line.indexOf("[ERROR]")   !== -1) { return "log-error"; }
            if (line.indexOf("[WARNING]") !== -1) { return "log-warning"; }
            if (line.indexOf("[DEBUG]")   !== -1) { return "log-debug"; }
            return "";
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct:    NasBackupViewModel,
        dependencies: ["settingsViewModel", "loginStateViewModel"],
        elements:     ["#settings_plugin_nasbackup"]
    });
});
