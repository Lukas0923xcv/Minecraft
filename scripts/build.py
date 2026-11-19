#!/usr/bin/env python3
"""Resolve modpack.toml into a ready-to-import server zip.

    python scripts/build.py --tag v1.0.0

Output in dist/:
    minecraft-server-<mc>-<tag>.zip   files at the zip ROOT (what Crafty's
                                      "Import from zip" expects):
                                        server.jar  (Fabric server launcher)
                                        mods/ config/ server.properties eula.txt
    SHA256SUMS                        sha256 of the zip and of every file in it
    release-notes.md                  MC / loader / mod versions

Reproducibility: every download is checked against the hash pinned in
modpack.toml and the build aborts on any mismatch. Zip entries are sorted and
get a fixed timestamp and permissions, so the same commit gives a
byte-identical zip.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import modrinth as mr  # noqa: E402

ROOT = mr.ROOT
SERVER_DIR = ROOT / "server"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


class HashMismatch(SystemExit):
    pass


def fetch_verified(url: str, algo: str, expected: str, cache: Path, name: str) -> bytes:
    """Download (or reuse from cache) and verify. Cached files are re-verified."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / name
    hasher = {"sha512": mr.sha512, "sha256": mr.sha256}[algo]
    if target.exists():
        data = target.read_bytes()
        if hasher(data) == expected:
            return data
        target.unlink()  # stale/corrupt cache entry, download again
    data = mr.download(url)
    actual = hasher(data)
    if actual != expected:
        raise HashMismatch(f"HASH MISMATCH for {name}\n  url:      {url}\n  expected: {algo}:{expected}\n  actual:   {algo}:{actual}")
    target.write_bytes(data)
    return data


def apply_tuning(config_dir: Path, threads: int) -> None:
    """Write the single worker_threads knob into the two pool settings."""
    if threads == 0:
        return
    if threads < 1:
        raise SystemExit("tuning.worker_threads must be 0 (auto) or a positive integer")
    edits = {
        "async.toml": (r"^maxThreads\s*=.*$", f"maxThreads = {threads}"),
        "c2me.toml": (r"^globalExecutorParallelism\s*=.*$", f"globalExecutorParallelism = {threads}"),
    }
    for filename, (pattern, replacement) in edits.items():
        path = config_dir / filename
        text = path.read_text(encoding="utf-8")
        new, n = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
        if n != 1:
            raise SystemExit(f"could not find the thread setting in {path}")
        path.write_text(new, encoding="utf-8")


def write_zip(src: Path, dest: Path) -> None:
    files = sorted(p for p in src.rglob("*") if p.is_file())
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(src).as_posix(), date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3  # unix, so the permissions above are honoured
            zf.writestr(info, path.read_bytes())


def release_notes(pack: dict, tag: str, zip_name: str) -> str:
    mc, fab = pack["minecraft"], pack["fabric"]
    lines = [
        f"# Fabric multi-core server {tag}",
        "",
        f"| Component | Version |",
        f"|---|---|",
        f"| Minecraft | **{mc['version']}** |",
        f"| Java | {mc['java']} |",
        f"| Fabric Loader | {fab['loader']} |",
        f"| Fabric installer (server launcher) | {fab['installer']} |",
        "",
        "## Mods",
        "",
        "| Mod | Version | Modrinth version ID | Role |",
        "|---|---|---|---|",
    ]
    for m in pack["mods"]:
        lines.append(f"| [{m['name']}](https://modrinth.com/mod/{m['slug']}) | {m['version_number']} | "
                     f"[`{m['version_id']}`](https://modrinth.com/mod/{m['slug']}/version/{m['version_id']}) | {m['workload']} |")
    lines.append(f"| perfdash (this repo, `dashboard/`) | {pack['dashboard']['version']} | built from source | "
                 "In-game load dashboard: Sneak + Swap-offhand (Shift+F) or `/dashboard` |")
    threads = pack["tuning"]["worker_threads"]
    lines += [
        "",
        f"Worker threads: {'auto (C2ME: cpus - 1, Async: ~cpus / 1.3)' if threads == 0 else threads}",
        "",
        "## Install",
        "",
        f"Crafty Controller 4: *Create server* -> *Import from zip* -> upload `{zip_name}`, "
        "server jar `server.jar`. Then set the start command from `jvm-flags.txt` in the repo "
        "and accept the EULA on first start. Full steps: see README.",
        "",
        "The vanilla Minecraft server jar is **not** included (Mojang EULA); the Fabric launcher "
        "downloads it from Mojang on first start and checks it.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME", "dev"),
                    help="release tag used in the zip name (default: $GITHUB_REF_NAME or 'dev')")
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    ap.add_argument("--cache", type=Path, default=ROOT / ".cache")
    ap.add_argument("--worker-threads", type=int, help="override tuning.worker_threads for this build")
    args = ap.parse_args()

    pack = mr.load_modpack()
    mc, fab = pack["minecraft"], pack["fabric"]
    threads = pack["tuning"]["worker_threads"] if args.worker_threads is None else args.worker_threads
    pack["tuning"]["worker_threads"] = threads
    tag = re.sub(r"[^A-Za-z0-9._+-]", "_", args.tag)
    base = f"minecraft-server-{mc['version']}-{tag}"

    if args.out.exists():
        shutil.rmtree(args.out)
    stage = args.out / base
    (stage / "mods").mkdir(parents=True)

    # Fabric server launcher (meta.fabricmc.net generates it reproducibly; hash pinned)
    url = mr.fabric_launcher_url(mc["version"], fab["loader"], fab["installer"])
    launcher_name = f"fabric-server-mc.{mc['version']}-loader.{fab['loader']}-launcher.{fab['installer']}.jar"
    print(f"launcher  {launcher_name}")
    (stage / "server.jar").write_bytes(fetch_verified(url, "sha256", fab["launcher_sha256"], args.cache, launcher_name))

    for mod in pack["mods"]:
        print(f"mod       {mod['filename']}")
        data = fetch_verified(mod["url"], "sha512", mod["sha512"], args.cache, mod["filename"])
        (stage / "mods" / mod["filename"]).write_bytes(data)

    # In-game dashboard, compiled from dashboard/ (needs JDK 25)
    import build_dashboard
    dash_jar = stage / "mods" / f"perfdash-{pack['dashboard']['version']}+mc{mc['version']}.jar"
    print(f"mod       {dash_jar.name} (built from dashboard/)")
    build_dashboard.build(pack, args.cache, dash_jar)

    shutil.copytree(SERVER_DIR / "config", stage / "config")
    shutil.copy2(SERVER_DIR / "server.properties", stage / "server.properties")
    shutil.copy2(SERVER_DIR / "eula.txt", stage / "eula.txt")
    apply_tuning(stage / "config", threads)

    zip_path = args.out / f"{base}.zip"
    write_zip(stage, zip_path)

    sums = [f"{mr.sha256(zip_path.read_bytes())}  {zip_path.name}"]
    for p in sorted(x for x in stage.rglob("*") if x.is_file()):
        sums.append(f"{mr.sha256(p.read_bytes())}  {base}/{p.relative_to(stage).as_posix()}")
    (args.out / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    (args.out / "release-notes.md").write_text(release_notes(pack, tag, zip_path.name))

    print(f"\nbuilt {zip_path.relative_to(ROOT)}  ({zip_path.stat().st_size / 1e6:.1f} MB)")
    print(sums[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
