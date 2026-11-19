#!/usr/bin/env python3
"""Check Modrinth / Fabric / Mojang for newer compatible versions and rewrite
modpack.toml with new pins and hashes.

    python scripts/update.py                  # newest builds for the pinned MC version
    python scripts/update.py --mc-version 26.4  # move the whole pack to another MC version
    python scripts/update.py --check-only     # report, do not write

Rules:
* Only builds for exactly the pinned Minecraft version are considered.
* A newer Minecraft release is never adopted automatically. It is reported, with
  the list of mods that do not have a build for it yet.
* --mc-version refuses to write anything if any mod lacks a build for the
  target version (no silent downgrade, no partial pack).
* Every new file is downloaded and its sha512 checked against Modrinth's.

Exit codes: 0 = ok (changes or not), 2 = a mod has no build for the target version.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import modrinth as mr  # noqa: E402

# Used only when modpack.toml does not exist yet (first run).
BOOTSTRAP_MODS = [
    ("Fabric API", "fabric-api", "release", "Dependency of Async, Chunky and spark"),
    ("Async", "async", "alpha", "Parallel entity ticking (+ optional parallel mob spawning / random ticks)"),
    ("C2ME", "c2me-fabric", "alpha", "Parallel chunk loading, generation, IO and saving; also hosts ScalableLux's lighting tasks"),
    ("ScalableLux", "scalablelux", "alpha", "Parallel lighting engine (Starlight-based)"),
    ("Lithium", "lithium", "release", "General game-logic / tick optimizations (single-threaded)"),
    ("FerriteCore", "ferrite-core", "release", "Memory usage reduction"),
    ("Very Many Players", "vmp-fabric", "alpha", "Networking, player chunk tracking and entity tracking for many players"),
    ("spark", "spark", "release", "Profiler (/spark tps, /spark profiler)"),
    ("Chunky", "chunky", "release", "World pre-generation (/chunky)"),
]


def bootstrap(mc_version: str) -> dict:
    mods = []
    for name, slug, channel, workload in BOOTSTRAP_MODS:
        project = mr.get_json(f"{mr.MODRINTH}/project/{slug}")
        mods.append({
            "name": name, "slug": slug, "project_id": project["id"], "channel": channel,
            "workload": workload, "version_id": "", "version_number": "",
            "filename": "", "url": "", "sha512": "",
        })
    return {
        "minecraft": {"version": mc_version, "java": 0, "server_sha1": ""},
        "fabric": {"loader": "", "installer": "", "launcher_sha256": ""},
        "tuning": {"worker_threads": 0},
        "mods": mods,
    }


def missing_mods(mods: list[dict], mc_version: str) -> list[str]:
    return [m["name"] for m in mods if mr.newest_mod_version(m["slug"], mc_version, m["channel"]) is None]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mc-version", help="move the pack to this Minecraft version")
    ap.add_argument("--check-only", action="store_true", help="report only, do not write modpack.toml")
    ap.add_argument("--summary", type=Path, help="write a Markdown change summary here (for PR bodies)")
    args = ap.parse_args()

    if mr.MODPACK.exists():
        pack = mr.load_modpack()
    else:
        target = args.mc_version or mr.latest_mc_release()
        print(f"modpack.toml not found, bootstrapping for Minecraft {target}")
        pack = bootstrap(target)

    old_mc = pack["minecraft"]["version"]
    mc = args.mc_version or old_mc
    changes: list[str] = []
    notes: list[str] = []

    # ---- every mod must have a build for the target version, otherwise stop
    missing = missing_mods(pack["mods"], mc)
    if missing:
        print(f"ERROR: no Fabric build for Minecraft {mc} yet: {', '.join(missing)}", file=sys.stderr)
        print("Nothing was changed. Stay on the current version or wait for these mods.", file=sys.stderr)
        if args.summary:
            args.summary.write_text(f"Cannot move to Minecraft {mc}: missing builds for {', '.join(missing)}.\n")
        return 2

    # ---- Minecraft + Java
    server = mr.mc_server_download(mc)
    if mc != old_mc:
        changes.append(f"Minecraft {old_mc} -> {mc}")
    pack["minecraft"].update(version=mc, java=server["java"], server_sha1=server["server_sha1"])

    # ---- Fabric loader / installer (+ hash of the generated launcher jar, which is reproducible)
    loader, installer = mr.latest_stable_fabric_loader(), mr.latest_stable_fabric_installer()
    fab = pack["fabric"]
    if (loader, installer) != (fab["loader"], fab["installer"]) or mc != old_mc or not fab["launcher_sha256"]:
        if loader != fab["loader"]:
            changes.append(f"Fabric Loader {fab['loader'] or '-'} -> {loader}")
        if installer != fab["installer"]:
            changes.append(f"Fabric installer {fab['installer'] or '-'} -> {installer}")
        jar = mr.download(mr.fabric_launcher_url(mc, loader, installer))
        fab.update(loader=loader, installer=installer, launcher_sha256=mr.sha256(jar))

    # ---- mods
    for mod in pack["mods"]:
        newest = mr.newest_mod_version(mod["slug"], mc, mod["channel"])
        if newest["id"] == mod["version_id"]:
            continue
        f = mr.primary_file(newest)
        data = mr.download(f["url"])
        if mr.sha512(data) != f["hashes"]["sha512"]:
            raise SystemExit(f"sha512 mismatch while downloading {f['url']}")
        changes.append(f"{mod['name']} {mod['version_number'] or '-'} -> {newest['version_number']} ({newest['version_type']})")
        mod.update(version_id=newest["id"], version_number=newest["version_number"],
                   filename=f["filename"], url=f["url"], sha512=f["hashes"]["sha512"])

    # ---- tell (but never act) about a newer Minecraft release
    latest = mr.latest_mc_release()
    if latest != mc:
        blockers = missing_mods(pack["mods"], latest)
        if blockers:
            notes.append(f"Minecraft {latest} is out, but these mods have no build yet: {', '.join(blockers)}.")
        else:
            notes.append(f"Minecraft {latest} is out and every mod has a build for it. "
                         f"Run `python scripts/update.py --mc-version {latest}` to move the pack (read the changelogs first).")

    for line in changes or ["No updates."]:
        print(line)
    for line in notes:
        print("NOTE:", line)

    if args.summary:
        body = ["## Pinned version updates", ""]
        body += [f"- {c}" for c in changes] or ["- none"]
        if notes:
            body += ["", "## Notes", ""] + [f"- {n}" for n in notes]
        body += ["", "Read each mod's changelog on Modrinth before merging; the CI smoke test must pass."]
        args.summary.write_text("\n".join(body) + "\n")

    if not args.check_only:
        mr.write_modpack(pack)
    return 0


if __name__ == "__main__":
    sys.exit(main())
