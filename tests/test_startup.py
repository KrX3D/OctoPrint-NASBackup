# coding=utf-8
"""Startup detection: cold boot vs. system restart vs. OctoPrint restart.

The kind decides which startup backup fires, so the state file written by
on_shutdown()/on_after_startup() is driven through real reboot sequences with a
fake boot_id and clock.
"""
import json
import os
import types

import pytest

import octoprint_nasbackup


class Machine(object):
    """Fake boot_id + wall clock, patched into the plugin module."""

    def __init__(self):
        self.boot_id = "boot-1"
        self.now = 1700000000.0

    def reboot(self, after=60):
        self.now += after
        self.boot_id = "boot-{}".format(int(self.boot_id.split("-")[1]) + 1)

    def wait(self, seconds):
        self.now += seconds


class FakeTimer(object):
    created = []

    def __init__(self, delay, fn):
        self.delay = delay
        self.fn = fn
        self.started = False
        self.cancelled = False
        FakeTimer.created.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def is_alive(self):
        return self.started and not self.cancelled


@pytest.fixture
def machine(plugin, monkeypatch):
    m = Machine()
    monkeypatch.setattr(plugin, "_get_boot_id", lambda: m.boot_id)
    monkeypatch.setattr(octoprint_nasbackup, "time", types.SimpleNamespace(time=lambda: m.now))
    FakeTimer.created = []
    monkeypatch.setattr(octoprint_nasbackup.threading, "Timer", FakeTimer)
    monkeypatch.setattr(plugin, "_reschedule", lambda: None)
    return m


def start(plugin):
    """Run on_after_startup() and return the startup kind it detected."""
    kind = plugin._detect_startup_kind()
    plugin.on_after_startup()
    return kind


def state(plugin):
    with open(os.path.join(plugin.get_plugin_data_folder(), "startup_state.json")) as f:
        return json.load(f)


def test_first_ever_start_is_cold_boot(plugin, machine):
    assert start(plugin) == "cold_boot"


def test_octoprint_restart_keeps_boot_id(plugin, machine):
    start(plugin)
    plugin.on_shutdown()
    machine.wait(20)
    assert start(plugin) == "octoprint_restart"


def test_octoprint_crash_restart_without_shutdown(plugin, machine):
    start(plugin)
    machine.wait(20)
    assert start(plugin) == "octoprint_restart"


def test_clean_reboot_is_system_restart(plugin, machine):
    start(plugin)
    plugin.on_shutdown()
    machine.reboot(after=90)
    assert start(plugin) == "system_restart"


def test_power_loss_is_cold_boot(plugin, machine):
    start(plugin)
    machine.wait(3600)
    machine.reboot(after=30)  # no on_shutdown() - power pulled
    assert start(plugin) == "cold_boot"


def test_shutdown_then_long_power_off_is_cold_boot(plugin, machine):
    start(plugin)
    plugin.on_shutdown()
    machine.reboot(after=901)
    assert start(plugin) == "cold_boot"


def test_shutdown_marker_is_consumed_by_next_start(plugin, machine):
    start(plugin)
    plugin.on_shutdown()
    machine.reboot(after=60)
    assert start(plugin) == "system_restart"
    assert state(plugin)["last_shutdown_ts"] == 0
    # A later power loss must not reuse the old shutdown marker.
    machine.reboot(after=60)
    assert start(plugin) == "cold_boot"


def test_shutdown_marker_from_an_older_boot_is_ignored(plugin, machine):
    folder = plugin.get_plugin_data_folder()
    os.makedirs(folder)
    with open(os.path.join(folder, "startup_state.json"), "w") as f:
        json.dump({
            "boot_id": "boot-1",
            "last_shutdown_ts": machine.now - 30,
            "last_shutdown_boot_id": "boot-0",
        }, f)
    machine.reboot(after=0)
    assert start(plugin) == "cold_boot"


def test_corrupt_state_file_is_cold_boot(plugin, machine):
    folder = plugin.get_plugin_data_folder()
    os.makedirs(folder)
    with open(os.path.join(folder, "startup_state.json"), "w") as f:
        f.write("{not json")
    assert start(plugin) == "cold_boot"
    assert state(plugin)["boot_id"] == machine.boot_id


def test_non_dict_state_file_is_cold_boot(plugin, machine):
    folder = plugin.get_plugin_data_folder()
    os.makedirs(folder)
    with open(os.path.join(folder, "startup_state.json"), "w") as f:
        json.dump(["boot-1"], f)
    assert start(plugin) == "cold_boot"


def test_unreadable_boot_id_never_claims_octoprint_restart(plugin, machine):
    machine.boot_id = None
    start(plugin)
    plugin.on_shutdown()
    machine.wait(20)
    assert start(plugin) == "cold_boot"


def test_garbage_shutdown_ts_is_unknown(plugin, machine):
    folder = plugin.get_plugin_data_folder()
    os.makedirs(folder)
    with open(os.path.join(folder, "startup_state.json"), "w") as f:
        json.dump({"boot_id": "boot-0", "last_shutdown_ts": "yesterday"}, f)
    assert plugin._detect_startup_kind() == "unknown"


# -- Arming the startup backup ---------------------------------------------------


@pytest.mark.parametrize("kind_setting", [
    "backup_on_startup_cold_boot",
    "backup_on_startup_system_restart",
    "backup_on_startup_octoprint_restart",
])
def test_startup_backup_follows_per_kind_switch(plugin, machine, kind_setting):
    values = plugin._settings.values
    values["backup_on_startup"] = True
    values["startup_delay"] = 42
    for key in ("cold_boot", "system_restart", "octoprint_restart"):
        values["backup_on_startup_" + key] = "backup_on_startup_" + key == kind_setting

    sequences = {
        "backup_on_startup_cold_boot": lambda: None,
        "backup_on_startup_system_restart": lambda: (
            start(plugin), plugin.on_shutdown(), machine.reboot(after=30)),
        "backup_on_startup_octoprint_restart": lambda: (
            start(plugin), plugin.on_shutdown(), machine.wait(5)),
    }
    sequences[kind_setting]()
    FakeTimer.created = []
    start(plugin)

    assert len(FakeTimer.created) == 1
    timer = FakeTimer.created[0]
    assert timer.started and timer.delay == 42


def test_startup_backup_not_armed_for_disabled_kind(plugin, machine):
    values = plugin._settings.values
    values["backup_on_startup"] = True
    values["backup_on_startup_octoprint_restart"] = "false"
    start(plugin)
    FakeTimer.created = []
    machine.wait(5)
    assert start(plugin) == "octoprint_restart"
    assert FakeTimer.created == []


def test_startup_backup_not_armed_when_master_switch_off(plugin, machine):
    plugin._settings.values["backup_on_startup"] = False
    start(plugin)
    assert FakeTimer.created == []


def test_startup_backup_not_armed_when_plugin_disabled(plugin, machine):
    plugin._settings.values.update(enabled=False, backup_on_startup=True)
    start(plugin)
    assert FakeTimer.created == []


def test_startup_backup_not_armed_for_unknown_kind(plugin, machine, monkeypatch):
    plugin._settings.values["backup_on_startup"] = True
    monkeypatch.setattr(plugin, "_detect_startup_kind", lambda: "unknown")
    plugin.on_after_startup()
    assert FakeTimer.created == []


def test_shutdown_cancels_pending_startup_backup(plugin, machine):
    plugin._settings.values["backup_on_startup"] = True
    start(plugin)
    timer = FakeTimer.created[0]
    plugin.on_shutdown()
    assert timer.cancelled
    assert plugin._startup_timer is None


def test_startup_timer_runs_backup_with_kind_as_source(plugin, machine, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "_run_backup", lambda source="manual": calls.append(source))
    plugin._settings.values["backup_on_startup"] = True
    start(plugin)
    FakeTimer.created[0].fn()
    assert calls == ["startup_cold_boot"]


# -- _get_bool -----------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False),
    ("true", True), ("True ", True), ("1", True), ("yes", True),
    ("false", False), ("0", False), ("", False), ("no", False),
    (None, False), (0, False), (1, True),
])
def test_get_bool(plugin, raw, expected):
    plugin._settings.values["x"] = raw
    assert plugin._get_bool("x") is expected
