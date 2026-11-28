"""Verify the packaged ZIP using an isolated Blender profile and two processes."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "tests" / "fixtures" / "GroupManager.blend"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("blender", help="Blender 5.3 executable")
    parser.add_argument("package", type=Path, help="Scena extension ZIP")
    parser.add_argument("--preferences-ui", action="store_true",
                        help="Open Blender Preferences and check persistence across two UI sessions")
    args = parser.parse_args()
    with zipfile.ZipFile(args.package) as archive:
        source = ROOT / "scena"
        expected = {path.relative_to(source).as_posix(): path for path in source.rglob("*")
                    if path.is_file() and (path.suffix in {".py", ".toml", ".ttf"} or path.name == "LICENSE")}
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        assert len(names) == len(set(names)), "Duplicate package entries"
        assert set(names) == set(expected), f"Package file mismatch: {sorted(set(names) ^ set(expected))}"
        for name, path in expected.items():
            assert archive.read(name) == path.read_bytes(), f"Stale package file: {name}"
    with tempfile.TemporaryDirectory(prefix="groupmanager-profile-") as temp:
        profile = Path(temp)
        (profile / "extensions" / "user_default").mkdir(parents=True)
        (profile / "config").mkdir()
        env = dict(os.environ, BLENDER_USER_RESOURCES=temp,
                   BLENDER_USER_CONFIG=str(profile / "config"),
                   BLENDER_USER_EXTENSIONS=str(profile / "extensions"))

        def run(*arguments):
            subprocess.run([args.blender, "--offline-mode", "--background", *arguments],
                           env=env, check=True, timeout=120)

        verify_profile = """import bpy, os
from pathlib import Path
repo = next(r for r in bpy.context.preferences.extensions.repos if r.module == 'user_default')
expected = Path(os.environ['BLENDER_USER_EXTENSIONS']) / 'user_default'
assert os.path.samefile(repo.directory, expected), (repo.directory, expected)
assert os.path.samefile(bpy.utils.user_resource('CONFIG'), os.environ['BLENDER_USER_CONFIG'])
"""
        run("--factory-startup", "--python-exit-code", "3", "--python-expr", verify_profile)
        run("--factory-startup", "--command", "extension", "install-file", "-r", "user_default",
            "--enable", str(args.package.resolve()))
        probe = """import bpy, importlib
name = 'bl_ext.user_default.scena'
assert name in bpy.context.preferences.addons
hosts = importlib.import_module(name + '.hosts')
for kind in ('NATIVE','NODES'):
    bpy.ops.object.select_all(action='DESELECT')
    bpy.ops.mesh.primitive_cube_add()
    assert bpy.ops.groupmanager.create(group_name=kind, kind=kind) == {'FINISHED'}
    assert hosts.host_type(bpy.context.active_object) == kind
print('Scena installed; both placement types passed')
"""
        run("--disable-autoexec", str(TEMPLATE), "--python-exit-code", "4",
            "--python-expr", probe)
        if args.preferences_ui:
            saved_scene = profile / "preferences.blend"
            for stage, scene in (("configure", TEMPLATE), ("restore", saved_scene)):
                subprocess.run([args.blender, "--offline-mode", "--disable-autoexec", "-p", "100", "100",
                                "1000", "720", str(scene), "--python-exit-code", "5", "--python",
                                str(ROOT / "tests" / "test_preferences_ui.py"), "--", stage, str(saved_scene)],
                               env=env, check=True, timeout=90)


if __name__ == "__main__":
    main()
