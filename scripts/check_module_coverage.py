"""Fail unless every module of the integration reaches the given coverage (default 95 %, branches included).

Usage: python scripts/check_module_coverage.py [percent] [coverage.json]
"""

import json
import sys

threshold = float(sys.argv[1]) if len(sys.argv) > 1 else 95.0
path = sys.argv[2] if len(sys.argv) > 2 else "coverage.json"
with open(path) as fh:
    files = json.load(fh)["files"]

low = {
    name: info["summary"]["percent_covered"]
    for name, info in files.items()
    if info["summary"]["percent_covered"] < threshold
}
for name, info in sorted(files.items()):
    print(f"{info['summary']['percent_covered']:6.1f} %  {name}")
if low:
    print(f"\nBelow {threshold:.0f} %: " + ", ".join(f"{n} ({p:.1f} %)" for n, p in sorted(low.items())))
    sys.exit(1)
print(f"\nEvery module is at {threshold:.0f} % or more.")
