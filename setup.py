########################################################################################################################
# OctoPrint-NASBackup setup.py
########################################################################################################################

import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "octoprint_nasbackup"))
import _meta

########################################################################################################################

from setuptools import setup

try:
    import octoprint_setuptools
except ImportError:
    print(
        "Could not import OctoPrint's setuptools. Make sure you are running this with "
        "the same Python installation that OctoPrint is installed in."
    )
    sys.exit(-1)

setup_parameters = octoprint_setuptools.create_plugin_setup_parameters(
    identifier  = _meta.identifier,
    package     = "octoprint_nasbackup",
    name        = _meta.name,
    version     = _meta.version,
    description = _meta.description,
    author      = _meta.author,
    mail        = "",
    url         = _meta.url,
    license     = _meta.license,
    requires    = [],
)

if __name__ == "__main__":
    setup(**setup_parameters)
