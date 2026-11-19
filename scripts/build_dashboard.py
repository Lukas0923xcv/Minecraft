#!/usr/bin/env python3
"""Compile the perfdash dashboard mod (dashboard/) into a Fabric mod jar.

No Gradle/Loom needed: Minecraft 26.x ships unobfuscated, so the mod is
compiled with plain `javac` (JDK 25) against the exact classes the server runs:

  * the vanilla server jar (sha1 pinned in modpack.toml) -> inner server jar +
    its bundled libraries
  * Fabric Loader + Sponge Mixin jars (sha256 pinned in modpack.toml [dashboard])
  * the Fabric API modules it uses, taken from the pinned Fabric API jar

Called by build.py; can also be run alone:
    python scripts/build_dashboard.py --out dist/perfdash.jar
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import modrinth as mr  # noqa: E402

ROOT = mr.ROOT
SRC = ROOT / "dashboard" / "src" / "main"
FABRIC_API_MODULES = ("fabric-api-base-", "fabric-command-api-v2-", "fabric-lifecycle-events-v1-",
                      "fabric-networking-api-v1-")
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def maven_url(repo: str, coord: str) -> str:
    group, artifact, version = coord.split(":")
    return f"{repo.rstrip('/')}/{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.jar"


def fetch(url: str, algo: str, expected: str, cache: Path, name: str) -> Path:
    from build import fetch_verified  # reuse the same verify-or-fail logic
    fetch_verified(url, algo, expected, cache, name)
    return cache / name


def javac() -> str:
    for cand in (os.environ.get("JAVA_HOME", "") + "/bin/javac", shutil.which("javac") or ""):
        if cand and Path(cand).is_file():
            return cand
    raise SystemExit("javac (JDK 25) not found: install a JDK 25 or set JAVA_HOME")


def build(pack: dict, cache: Path, out_jar: Path) -> Path:
    mc = pack["minecraft"]
    dash = pack["dashboard"]
    work = Path(tempfile.mkdtemp(prefix="perfdash-build-"))
    try:
        cp: list[Path] = []

        # vanilla server: bundler jar -> inner server jar + libraries
        info_url = f"https://piston-data.mojang.com/v1/objects/{mc['server_sha1']}/server.jar"
        bundler = cache / f"minecraft-server-{mc['version']}.jar"
        cache.mkdir(parents=True, exist_ok=True)
        if not bundler.exists() or mr.sha1(bundler.read_bytes()) != mc["server_sha1"]:
            data = mr.download(info_url)
            if mr.sha1(data) != mc["server_sha1"]:
                raise SystemExit("HASH MISMATCH for vanilla server jar")
            bundler.write_bytes(data)
        with zipfile.ZipFile(bundler) as zf:
            for n in zf.namelist():
                if n.endswith(".jar") and (n.startswith("META-INF/versions/") or n.startswith("META-INF/libraries/")):
                    target = work / "cp" / n
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(n))
                    cp.append(target)

        # Fabric Loader + Mixin (pinned)
        for dep in dash["compile"]:
            cp.append(fetch(maven_url(dep["repo"], dep["coord"]), "sha256", dep["sha256"], cache,
                            dep["coord"].replace(":", "-") + ".jar"))

        # Fabric API modules, from the pinned (sha512-verified) Fabric API jar
        api = next(m for m in pack["mods"] if m["slug"] == "fabric-api")
        api_jar = fetch(api["url"], "sha512", api["sha512"], cache, api["filename"])
        with zipfile.ZipFile(api_jar) as zf:
            for n in zf.namelist():
                base = n.rsplit("/", 1)[-1]
                if n.startswith("META-INF/jars/") and base.startswith(FABRIC_API_MODULES):
                    target = work / "cp" / base
                    target.write_bytes(zf.read(n))
                    cp.append(target)

        sources = sorted(str(p) for p in (SRC / "java").rglob("*.java"))
        classes = work / "classes"
        cmd = [javac(), "--release", str(mc["java"]), "-proc:none", "-encoding", "UTF-8", "-nowarn",
               "-cp", os.pathsep.join(str(p) for p in cp), "-d", str(classes)] + sources
        subprocess.run(cmd, check=True)

        # jar: sorted entries, fixed timestamps
        entries: dict[str, bytes] = {}
        for p in classes.rglob("*.class"):
            entries[p.relative_to(classes).as_posix()] = p.read_bytes()
        for p in (SRC / "resources").rglob("*"):
            if p.is_file():
                data = p.read_bytes()
                if p.name == "fabric.mod.json":
                    data = (data.decode().replace("${version}", dash["version"])
                            .replace("${minecraft}", f"~{mc['version']}").encode())
                entries[p.relative_to(SRC / "resources").as_posix()] = data
        entries["META-INF/MANIFEST.MF"] = b"Manifest-Version: 1.0\r\n\r\n"
        out_jar.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_jar, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(entries):
                info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                info.create_system = 3
                zf.writestr(info, entries[name])
        return out_jar
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "dist" / "perfdash.jar")
    ap.add_argument("--cache", type=Path, default=ROOT / ".cache")
    args = ap.parse_args()
    jar = build(mr.load_modpack(), args.cache, args.out)
    print(f"built {jar} sha256={mr.sha256(jar.read_bytes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
