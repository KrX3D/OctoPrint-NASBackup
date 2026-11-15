# coding=utf-8
"""Lets the tests import the plugin without OctoPrint or flask installed.

``octoprint_nasbackup/__init__.py`` subclasses OctoPrint's plugin mixins and
imports flask. Both are replaced with empty stand-ins here, so the plugin's own
logic can be tested on a plain Python install.
"""
import datetime
import logging
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_stubs():
    if "octoprint.plugin" not in sys.modules:
        plugin = types.ModuleType("octoprint.plugin")
        for name in (
            "SettingsPlugin",
            "TemplatePlugin",
            "AssetPlugin",
            "StartupPlugin",
            "ShutdownPlugin",
            "SimpleApiPlugin",
            "EventHandlerPlugin",
        ):
            setattr(plugin, name, type(name, (object,), {}))
        octoprint = types.ModuleType("octoprint")
        octoprint.plugin = plugin
        sys.modules["octoprint"] = octoprint
        sys.modules["octoprint.plugin"] = plugin
    if "flask" not in sys.modules:
        sys.modules["flask"] = types.ModuleType("flask")


_install_stubs()

import octoprint_nasbackup  # noqa: E402

# Fixed "now" for every test: Sunday 2026-11-15 12:00:00.
NOW = datetime.datetime(2026, 11, 15, 12, 0, 0)


class FrozenDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class FakeSettings(object):
    def __init__(self, values, data_dir):
        self.values = values
        self.data_dir = data_dir

    def get(self, path):
        return self.values.get(path[0])

    def global_get_basefolder(self, name):
        return self.data_dir


class FakePluginManager(object):
    def __init__(self):
        self.messages = []

    def send_plugin_message(self, identifier, data):
        self.messages.append((identifier, data))


@pytest.fixture
def frozen_now(monkeypatch):
    fake = types.SimpleNamespace(datetime=FrozenDatetime, timedelta=datetime.timedelta)
    monkeypatch.setattr(octoprint_nasbackup, "datetime", fake)
    return NOW


@pytest.fixture
def plugin(tmp_path, frozen_now):
    """A plugin instance with default settings and a temporary data folder."""
    p = octoprint_nasbackup.NasBackupPlugin()
    values = p.get_settings_defaults()
    p._settings = FakeSettings(values, str(tmp_path / "octoprint_data"))
    p._logger = logging.getLogger("test.nasbackup")
    p._plugin_version = "test"
    p._identifier = "nasbackup"
    p._plugin_manager = FakePluginManager()
    data_folder = str(tmp_path / "plugin_data")
    p.get_plugin_data_folder = lambda: data_folder
    return p
