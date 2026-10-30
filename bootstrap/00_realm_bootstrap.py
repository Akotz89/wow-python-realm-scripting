"""Only Python file placed under ``Python.ScriptsPath``.

The engine recursively executes every ``.py`` below that path.  Keeping this
directory to one bootstrap prevents filesystem-order execution, duplicate
package imports, and accidental loading of tests or operator scratch files.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import azerothcore as ac

release_root = Path(os.environ.get("WOW_PYTHON_RELEASE_ROOT", "/srv/wow/python/current")).resolve(strict=True)
library_root = release_root / "lib"
manifest_path = release_root / "manifest.json"

if str(library_root) not in sys.path:
    sys.path.insert(0, str(library_root))

realm_loader = importlib.import_module("realm_scripts.loader")
realm_loader = importlib.reload(realm_loader)
report = realm_loader.load_manifest(ac, manifest_path)
print(
    f"Realm Python release {report.release} loaded "
    f"{report.binding_count} bindings from {len(report.features)} features"
)
