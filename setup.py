########################################################################################################################
# OctoPrint-NASBackup setup.py
########################################################################################################################

import re

plugin_identifier   = "nasbackup"
plugin_package      = "octoprint_nasbackup"
plugin_name         = "OctoPrint-NASBackup"

# Single source of truth — version lives in __init__.py
with open("octoprint_nasbackup/__init__.py", "r", encoding="utf-8") as _f:
    _m = re.search(r'^__plugin_version__\s*=\s*[\'"]([^\'"]+)[\'"]', _f.read(), re.MULTILINE)
    plugin_version = _m.group(1) if _m else "0.0.0"

plugin_description  = "Automated OctoPrint backups to a NAS over SMB — scheduled backups and GFS retention."
plugin_author       = "KrX3D"
plugin_author_email = ""
plugin_url          = "https://github.com/KrX3D/OctoPrint-NASBackup"
plugin_license      = "MIT"
plugin_requires     = []

########################################################################################################################

from setuptools import setup

try:
    import octoprint_setuptools
except ImportError:
    print(
        "Could not import OctoPrint's setuptools. Make sure you are running this with "
        "the same Python installation that OctoPrint is installed in."
    )
    import sys
    sys.exit(-1)

setup_parameters = octoprint_setuptools.create_plugin_setup_parameters(
    identifier  = plugin_identifier,
    package     = plugin_package,
    name        = plugin_name,
    version     = plugin_version,
    description = plugin_description,
    author      = plugin_author,
    mail        = plugin_author_email,
    url         = plugin_url,
    license     = plugin_license,
    requires    = plugin_requires,
)

if __name__ == "__main__":
    setup(**setup_parameters)