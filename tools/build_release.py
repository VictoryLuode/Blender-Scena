"""Build an installable Scena ZIP after verifying it in clean Blender profiles."""

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("blender", help="Blender 5.3 executable")
    args = parser.parse_args()
    source = ROOT / "scena"
    with (source / "blender_manifest.toml").open("rb") as manifest:
        version = tomllib.load(manifest)["version"]
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    name = f"Scena-{version}.zip"
    with tempfile.TemporaryDirectory(dir=dist) as staging:
        package = Path(staging) / name
        subprocess.run([args.blender, "--background", "--factory-startup", "--offline-mode",
                        "--command", "extension", "build", "--source-dir", str(source),
                        "--output-filepath", str(package)], check=True)
        subprocess.run([sys.executable, str(ROOT / "tests" / "test_install.py"), args.blender,
                        str(package), "--preferences-ui"], check=True)
        package.replace(dist / name)
    print(dist / name)


if __name__ == "__main__":
    main()
