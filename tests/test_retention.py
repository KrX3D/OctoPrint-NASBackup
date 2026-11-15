# coding=utf-8
"""GFS retention: which NAS snapshots get deleted.

A mistake here deletes good backups, so the selection logic is checked against
hand-picked calendars, and the SMB/local pruning is checked to delete exactly
what the selection returned and nothing else.

"Now" is frozen at Sunday 2026-11-15 12:00:00 (see conftest.py).
"""
import datetime

import pytest

from conftest import NOW


def snap(days_ago, hour=3, minute=0):
    dt = NOW - datetime.timedelta(days=days_ago)
    dt = dt.replace(hour=hour, minute=minute, second=0)
    return dt.strftime("%Y-%m-%d_%H%M%S")


def at(y, m, d, hour=3):
    return datetime.datetime(y, m, d, hour).strftime("%Y-%m-%d_%H%M%S")


def kept(plugin, names):
    return set(names) - plugin._gfs_calculate_deletions(names)


# -- _gfs_calculate_deletions --------------------------------------------------


def test_empty_list_deletes_nothing(plugin):
    assert plugin._gfs_calculate_deletions([]) == set()


def test_last_week_of_dailies_all_kept(plugin):
    names = [snap(d) for d in range(7)]
    assert plugin._gfs_calculate_deletions(names) == set()


def test_only_newest_snapshot_per_day_kept_in_daily_window(plugin):
    names = [snap(0, hour=3), snap(0, hour=11), snap(1, hour=3), snap(1, hour=20)]
    assert plugin._gfs_calculate_deletions(names) == {snap(0, hour=3), snap(1, hour=3)}


def test_todays_backup_always_kept(plugin):
    names = [snap(d) for d in range(2000)] + [snap(0, hour=11, minute=59)]
    assert snap(0, hour=11, minute=59) in kept(plugin, names)


def test_unparseable_names_are_never_deleted(plugin):
    junk = [
        "notes",
        "2026-11-15",
        "2026-11-15_0300",
        "2026-13-01_030000",  # month 13
        "2026-02-30_030000",  # no such day
        "2010-01-01_030000_old",
    ]
    names = junk + [snap(d) for d in range(3000)]
    deleted = plugin._gfs_calculate_deletions(names)
    assert not deleted & set(junk)


def test_weekly_window_keeps_newest_per_iso_week(plugin):
    # Daily window is ages 0-6, weekly covers ages 7-55 (8 weeks from now).
    names = [snap(d) for d in range(7, 56)]
    keep = kept(plugin, names)
    weeks = {}
    for name in names:
        dt = datetime.datetime.strptime(name, "%Y-%m-%d_%H%M%S")
        weeks.setdefault(dt.isocalendar()[:2], []).append(name)
    assert keep == {max(group) for group in weeks.values()}


def test_monthly_window_keeps_newest_per_calendar_month(plugin):
    # Monthly covers ages 56-359 (age // 30 < 12).
    names = [snap(d) for d in range(56, 360)]
    keep = kept(plugin, names)
    months = {}
    for name in names:
        months.setdefault(name[:7], []).append(name)
    assert keep == {max(group) for group in months.values()}


def test_yearly_window_keeps_newest_per_year(plugin):
    # Yearly covers ages 360-1824 (age // 365 < 5).
    names = [snap(d) for d in range(360, 1825)]
    keep = kept(plugin, names)
    years = {}
    for name in names:
        years.setdefault(name[:4], []).append(name)
    assert keep == {max(group) for group in years.values()}


def test_snapshots_older_than_yearly_window_deleted(plugin):
    old = [snap(1825), snap(2500), at(2015, 6, 1)]
    assert plugin._gfs_calculate_deletions(old + [snap(0)]) == set(old)


def test_three_years_of_dailies(plugin):
    names = [snap(d) for d in range(3 * 365)]
    keep = kept(plugin, names)
    # 7 daily + one per ISO week in ages 7-55 + one per month in ages 56-359
    # + one per year in ages 360-1094.
    assert {snap(d) for d in range(7)} <= keep
    assert len(keep) == 7 + 7 + 11 + 3
    assert max(names) in keep
    # Nothing is ever kept twice for the same calendar day.
    assert len({n[:10] for n in keep}) == len(keep)


def test_sparse_history_is_kept(plugin):
    # Backups only happened now and then: every one is the newest of its
    # bucket and must survive.
    names = [snap(0), snap(10), snap(40), snap(100), snap(200), snap(400), snap(1000)]
    assert plugin._gfs_calculate_deletions(names) == set()


def test_future_dated_snapshot_counts_as_today(plugin):
    # NAS/Pi clock skew: a snapshot "from tomorrow" must not be deleted.
    future = (NOW + datetime.timedelta(days=1)).strftime("%Y-%m-%d_%H%M%S")
    assert future in kept(plugin, [future, snap(0), snap(1)])


def test_input_order_does_not_matter(plugin):
    names = [snap(d) for d in range(400)]
    shuffled = names[::3] + names[1::3] + names[2::3]
    assert plugin._gfs_calculate_deletions(names) == plugin._gfs_calculate_deletions(shuffled)


def test_settings_as_strings_from_the_ui(plugin):
    plugin._settings.values.update(keep_daily="3", keep_weekly="8")
    names = [snap(0), snap(1), snap(2), snap(3), snap(4)]
    # Ages 3 and 4 (Thu/Wed) leave the daily tier and share ISO week 46 with
    # the dailies; the weekly tier keeps only the newer one.
    assert plugin._gfs_calculate_deletions(names) == {snap(4)}


def test_daily_tier_does_not_use_up_the_weekly_slot(plugin):
    plugin._settings.values.update(keep_daily=2)
    names = [snap(0), snap(1), snap(2, hour=1), snap(2, hour=5)]
    # Age 2 is in the same ISO week as ages 0/1, but those were kept by the
    # daily tier, so the weekly tier still keeps the newest of age 2.
    assert plugin._gfs_calculate_deletions(names) == {snap(2, hour=1)}


@pytest.mark.xfail(
    strict=True,
    reason="keep_* = 0 falls back to the default (int(0 or 7)), although the UI allows 0",
)
def test_zero_disables_daily_tier(plugin):
    plugin._settings.values.update(keep_daily=0)
    # Without dailies both fall into the weekly tier (ISO week 46): keep only
    # the newer one.
    assert plugin._gfs_calculate_deletions([snap(0), snap(1)]) == {snap(1)}


# -- _apply_retention (SMB) ----------------------------------------------------

SMB_LS = """\
  .                                   D        0  Sun Nov 15 12:00:00 2026
  ..                                  D        0  Sun Nov 15 12:00:00 2026
{dirs}
  _readme.txt                         A      120  Sun Nov 15 12:00:00 2026
  manual-copy                         D        0  Sun Nov 15 12:00:00 2026

\t\t976762584 blocks of size 1024. 123456 blocks available
"""


def ls_line(name):
    return "  {:<35} D        0  Sun Nov 15 12:00:00 2026".format(name)


class FakeSmb(object):
    def __init__(self, ls_out, ls_rc=0, deltree_rc=0):
        self.commands = []
        self.ls_out = ls_out
        self.ls_rc = ls_rc
        self.deltree_rc = deltree_rc

    def __call__(self, command, timeout=120):
        self.commands.append(command)
        if command.startswith("cd "):
            return self.ls_rc, self.ls_out, "" if self.ls_rc == 0 else "NT_STATUS_OBJECT_NAME_NOT_FOUND"
        if command.startswith("deltree "):
            return self.deltree_rc, "", "" if self.deltree_rc == 0 else "NT_STATUS_ACCESS_DENIED"
        raise AssertionError("unexpected smbclient command: " + command)


def test_smb_list_subdirs_parses_smbclient_ls(plugin):
    names = [snap(0), snap(1)]
    fake = FakeSmb(SMB_LS.format(dirs="\n".join(ls_line(n) for n in names)))
    plugin._smb_exec = fake
    assert plugin._smb_list_subdirs("OctoPrint/host") == names + ["manual-copy"]
    assert fake.commands == ['cd "OctoPrint/host"; ls']


def test_smb_retention_deletes_exactly_the_calculated_set(plugin):
    names = [snap(d) for d in range(60)]
    fake = FakeSmb(SMB_LS.format(dirs="\n".join(ls_line(n) for n in names)))
    plugin._smb_exec = fake
    expected = plugin._gfs_calculate_deletions(names + ["manual-copy"])
    assert expected and "manual-copy" not in expected

    plugin._apply_retention("host", "smbclient")

    deltrees = [c for c in fake.commands if c.startswith("deltree ")]
    assert deltrees == ['deltree "OctoPrint/host/{}"'.format(n) for n in sorted(expected)]


def test_smb_retention_uses_configured_subdir(plugin):
    plugin._settings.values["smb_subdir"] = "Backups/Pi"
    fake = FakeSmb(SMB_LS.format(dirs="\n".join(ls_line(n) for n in [snap(0, 3), snap(0, 4)])))
    plugin._smb_exec = fake
    plugin._apply_retention("host", "smbclient")
    assert fake.commands == [
        'cd "Backups/Pi/host"; ls',
        'deltree "Backups/Pi/host/{}"'.format(snap(0, 3)),
    ]


def test_smb_retention_listing_failure_deletes_nothing(plugin):
    fake = FakeSmb("", ls_rc=1)
    plugin._smb_exec = fake
    plugin._apply_retention("host", "smbclient")
    assert fake.commands == ['cd "OctoPrint/host"; ls']


def test_smb_retention_deltree_failure_is_logged_not_raised(plugin):
    fake = FakeSmb(SMB_LS.format(dirs="\n".join(ls_line(n) for n in [snap(0, 3), snap(0, 4)])),
                   deltree_rc=1)
    plugin._smb_exec = fake
    plugin._apply_retention("host", "smbclient")
    assert any("deltree failed" in e for e in plugin._log_entries)


# -- _apply_retention (local) --------------------------------------------------


def test_local_retention_removes_only_selected_snapshot_dirs(plugin, tmp_path):
    base = tmp_path / "nas"
    host = base / "host"
    names = [snap(0, 3), snap(0, 4), snap(400), snap(3000)]
    for n in names:
        (host / n).mkdir(parents=True)
        (host / n / "backup.zip").write_bytes(b"zip")
    (host / "manual-copy").mkdir()
    (host / "2001-01-01_000000.zip").write_bytes(b"a file, not a snapshot dir")
    plugin._settings.values["local_path"] = str(base)

    plugin._apply_retention("host", "local")

    left = sorted(p.name for p in host.iterdir())
    assert left == sorted(["2001-01-01_000000.zip", "manual-copy", snap(0, 4), snap(400)])
