# coding=utf-8
"""Upload checks: the NAS copy must match the local ZIP before retention runs.

smbclient is never called; its results are faked at the _smb_exec /
_smb_remote_size / _smb_stream_sha256 level.
"""
import hashlib
import zipfile

import pytest


@pytest.fixture
def good_zip(tmp_path):
    path = tmp_path / "octoprint-backup-20261115-030000.zip"
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("basedir/config.yaml", "serial:\n  port: AUTO\n" * 50)
        zf.writestr("basedir/data/notes.txt", "hello" * 200)
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Nas(object):
    """Fakes what the NAS reports back for the uploaded file."""

    def __init__(self, plugin, size, digest, read_bytes, err=""):
        self.size = size
        self.digest = digest
        self.read_bytes = read_bytes
        self.err = err
        self.size_calls = []
        self.stream_calls = []
        plugin._smb_remote_size = self.remote_size
        plugin._smb_stream_sha256 = self.stream

    def remote_size(self, remote_path):
        self.size_calls.append(remote_path)
        return self.size

    def stream(self, remote_path, timeout=600):
        self.stream_calls.append((remote_path, timeout))
        return self.digest, self.read_bytes, self.err


def nas_matching(plugin, path):
    size = path.stat().st_size
    return Nas(plugin, size, sha(path), size)


def test_matching_copy_passes(plugin, good_zip):
    nas = nas_matching(plugin, good_zip)
    plugin._verify_remote_zip(str(good_zip), "OctoPrint/host/snap/b.zip")
    assert nas.size_calls == ["OctoPrint/host/snap/b.zip"]
    assert [c[0] for c in nas.stream_calls] == ["OctoPrint/host/snap/b.zip"]
    log = "\n".join(plugin._log_entries)
    assert "Verify size  : OK" in log
    assert "Verify data  : OK" in log
    assert "Verify ZIP   : OK (2 entries" in log


def test_both_checks_disabled_skips_nas(plugin, good_zip):
    plugin._settings.values.update(verify_upload_size=False, verify_upload_zip="false")
    nas = Nas(plugin, None, None, 0)
    plugin._verify_remote_zip(str(good_zip), "r.zip")
    assert nas.size_calls == [] and nas.stream_calls == []


def test_size_only(plugin, good_zip):
    plugin._settings.values["verify_upload_zip"] = False
    nas = Nas(plugin, good_zip.stat().st_size, None, 0)
    plugin._verify_remote_zip(str(good_zip), "r.zip")
    assert nas.stream_calls == []


def test_data_only(plugin, good_zip):
    plugin._settings.values["verify_upload_size"] = False
    nas = nas_matching(plugin, good_zip)
    plugin._verify_remote_zip(str(good_zip), "r.zip")
    assert nas.size_calls == []
    assert len(nas.stream_calls) == 1


@pytest.mark.parametrize("size,message", [
    (None, "could not read size"),
    (0, "size on NAS 0 bytes"),
    (-1, "size on NAS -1 bytes"),
])
def test_size_mismatch_fails(plugin, good_zip, size, message):
    Nas(plugin, size, sha(good_zip), good_zip.stat().st_size)
    with pytest.raises(RuntimeError, match=message):
        plugin._verify_remote_zip(str(good_zip), "r.zip")


def test_truncated_upload_fails_on_size(plugin, good_zip):
    Nas(plugin, good_zip.stat().st_size - 1, sha(good_zip), good_zip.stat().st_size)
    with pytest.raises(RuntimeError, match="size on NAS"):
        plugin._verify_remote_zip(str(good_zip), "r.zip")


def test_read_back_failure_fails(plugin, good_zip):
    Nas(plugin, good_zip.stat().st_size, None, 0, err="exit 1: NT_STATUS_ACCESS_DENIED")
    with pytest.raises(RuntimeError, match="could not read back r.zip: exit 1: NT_STATUS_ACCESS_DENIED"):
        plugin._verify_remote_zip(str(good_zip), "r.zip")


def test_checksum_mismatch_fails(plugin, good_zip):
    size = good_zip.stat().st_size
    Nas(plugin, size, "0" * 64, size)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        plugin._verify_remote_zip(str(good_zip), "r.zip")


def test_short_read_back_fails_even_if_hash_matches(plugin, good_zip):
    size = good_zip.stat().st_size
    Nas(plugin, size, sha(good_zip), size - 10)
    with pytest.raises(RuntimeError, match="of {} bytes read back".format(size)):
        plugin._verify_remote_zip(str(good_zip), "r.zip")


def test_corrupt_zip_member_fails(plugin, tmp_path):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("a.txt", "A" * 1000)
    data = bytearray(path.read_bytes())
    data[data.index(b"AAAA")] = ord("B")  # flip a byte inside the stored member
    path.write_bytes(bytes(data))
    nas_matching(plugin, path)
    with pytest.raises(RuntimeError, match="ZIP member 'a.txt' is corrupt"):
        plugin._verify_remote_zip(str(path), "r.zip")


def test_not_a_zip_fails(plugin, tmp_path):
    path = tmp_path / "notazip.zip"
    path.write_bytes(b"this is not a zip file at all")
    nas_matching(plugin, path)
    with pytest.raises(RuntimeError, match="ZIP is unreadable"):
        plugin._verify_remote_zip(str(path), "r.zip")


def test_sha256_file(tmp_path):
    from octoprint_nasbackup import NasBackupPlugin

    path = tmp_path / "f.bin"
    path.write_bytes(b"x" * (3 * 1_048_576 + 7))
    assert NasBackupPlugin._sha256_file(str(path)) == sha(path)


# -- _smb_remote_size parsing --------------------------------------------------

ALLINFO = """\
altname: BACKUP~1.ZIP
create_time:    Sun Nov 15 03:00:01 AM 2026 CET
access_time:    Sun Nov 15 03:00:01 AM 2026 CET
write_time:     Sun Nov 15 03:00:09 AM 2026 CET
change_time:    Sun Nov 15 03:00:09 AM 2026 CET
attributes: A (20)
stream: [::$DATA], 52428812 bytes
"""

LS = """\
  octoprint-backup.zip                A 52428812  Sun Nov 15 03:00:09 2026

\t\t976762584 blocks of size 1024. 123456 blocks available
"""


class FakeExec(object):
    def __init__(self, responses):
        self.responses = responses
        self.commands = []

    def __call__(self, command, timeout=120):
        self.commands.append(command)
        verb = command.split(" ", 1)[0]
        return self.responses.get(verb, (1, "", "NT_STATUS_OBJECT_NAME_NOT_FOUND"))


def test_remote_size_from_allinfo(plugin):
    fake = FakeExec({"allinfo": (0, ALLINFO, "")})
    plugin._smb_exec = fake
    assert plugin._smb_remote_size("a/b/octoprint-backup.zip") == 52428812
    assert fake.commands == ['allinfo "a/b/octoprint-backup.zip"']


def test_remote_size_falls_back_to_ls(plugin):
    fake = FakeExec({"allinfo": (1, "", "not supported"), "ls": (0, LS, "")})
    plugin._smb_exec = fake
    assert plugin._smb_remote_size("a/b/octoprint-backup.zip") == 52428812
    assert fake.commands[-1] == 'ls "a/b/octoprint-backup.zip"'


def test_remote_size_ls_without_matching_line(plugin):
    fake = FakeExec({"ls": (0, "  other.zip   A 1  Sun Nov 15 2026\n", "")})
    plugin._smb_exec = fake
    assert plugin._smb_remote_size("a/b/octoprint-backup.zip") is None


def test_remote_size_missing_file(plugin):
    plugin._smb_exec = FakeExec({})
    assert plugin._smb_remote_size("a/b/octoprint-backup.zip") is None


# -- Failed verification must not reach retention ------------------------------


@pytest.fixture
def backup_run(plugin, good_zip, monkeypatch):
    """Drive _run_backup() with everything outside the plugin faked."""
    import octoprint_nasbackup

    steps = []
    monkeypatch.setattr(octoprint_nasbackup.shutil, "which", lambda name: "/usr/bin/" + name)
    plugin._settings.values["only_when_idle"] = False
    plugin._get_server_name = lambda: "host"
    plugin._trigger_octoprint_backup = lambda: str(good_zip)
    plugin._prune_local_zips = lambda: steps.append("prune")
    plugin._apply_retention = lambda server, mode: steps.append(("retention", server, mode))
    plugin._copy_log_to_destination = lambda dest, include_snapshot_log=False: steps.append("log")
    plugin._backup_system_files_smbclient = lambda remote: steps.append("system")
    plugin._upload_temp_file_smbclient = lambda fn, remote, suffix=".tmp": steps.append(("meta", remote))
    plugin._smb_mkdir_p = lambda path: None
    plugin._smb_exec = lambda command, timeout=120: (0, "", "")
    return steps


def last_status(plugin):
    return plugin._plugin_manager.messages[-1][1]["status"]


def test_verified_backup_runs_retention(plugin, good_zip, backup_run):
    nas_matching(plugin, good_zip)
    plugin._run_backup(source="test")
    assert last_status(plugin) == "success"
    assert "prune" in backup_run
    assert ("retention", "host", "smbclient") in backup_run


@pytest.mark.parametrize("break_nas", [
    lambda p, z: Nas(p, None, None, 0),
    lambda p, z: Nas(p, z.stat().st_size, "0" * 64, z.stat().st_size),
    lambda p, z: Nas(p, z.stat().st_size, None, 0, err="timeout"),
])
def test_failed_verification_skips_prune_and_retention(plugin, good_zip, backup_run, break_nas):
    break_nas(plugin, good_zip)
    plugin._run_backup(source="test")
    assert last_status(plugin) == "failed"
    assert backup_run == []
    assert plugin._backup_running is False


def test_failed_upload_skips_verification_and_retention(plugin, good_zip, backup_run):
    nas = nas_matching(plugin, good_zip)
    plugin._smb_exec = lambda command, timeout=120: (1, "", "NT_STATUS_DISK_FULL")
    plugin._run_backup(source="test")
    assert last_status(plugin) == "failed"
    assert "NT_STATUS_DISK_FULL" in plugin._last_status["message"]
    assert nas.size_calls == [] and nas.stream_calls == []
    assert backup_run == []


def test_retention_disabled_is_respected(plugin, good_zip, backup_run):
    plugin._settings.values["retention_enabled"] = "false"
    nas_matching(plugin, good_zip)
    plugin._run_backup(source="test")
    assert last_status(plugin) == "success"
    assert not [s for s in backup_run if isinstance(s, tuple) and s[0] == "retention"]
