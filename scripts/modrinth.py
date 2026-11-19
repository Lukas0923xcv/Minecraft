"""Small shared helpers: HTTP with retries, Modrinth / Fabric / Mojang lookups,
and a deterministic writer for modpack.toml.

Only the Python standard library is used so the scripts run anywhere Python
3.11+ is installed (tomllib is stdlib since 3.11).
"""

from __future__ import annotations

import hashlib
import json
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MODRINTH = "https://api.modrinth.com/v2"
FABRIC_META = "https://meta.fabricmc.net/v2"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
USER_AGENT = "minecraft-server-pack-builder/1.0 (github.com/Lukas0923xcv/minecraft)"

# Modrinth version_type ordering: a mod with channel "alpha" accepts alpha, beta
# and release builds; channel "release" only accepts releases.
CHANNEL_RANK = {"release": 0, "beta": 1, "alpha": 2}

ROOT = Path(__file__).resolve().parent.parent
MODPACK = ROOT / "modpack.toml"


def _request(url: str, *, attempts: int = 4) -> bytes:
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # 4xx other than 429 will not get better by retrying
            if isinstance(exc, urllib.error.HTTPError) and exc.code != 429 and exc.code < 500:
                raise
            last = exc
            time.sleep(2 ** i)
    raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last}")


def get_json(url: str, params: dict | None = None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    return json.loads(_request(url))


def download(url: str) -> bytes:
    return _request(url)


def sha512(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


# --------------------------------------------------------------------------- lookups

def latest_mc_release() -> str:
    return get_json(MOJANG_MANIFEST)["latest"]["release"]


def mc_server_download(mc_version: str) -> dict:
    manifest = get_json(MOJANG_MANIFEST)
    entry = next(v for v in manifest["versions"] if v["id"] == mc_version)
    meta = get_json(entry["url"])
    return {
        "java": meta["javaVersion"]["majorVersion"],
        "server_sha1": meta["downloads"]["server"]["sha1"],
        "server_url": meta["downloads"]["server"]["url"],
    }


def latest_stable_fabric_loader() -> str:
    return next(v["version"] for v in get_json(f"{FABRIC_META}/versions/loader") if v["stable"])


def latest_stable_fabric_installer() -> str:
    return next(v["version"] for v in get_json(f"{FABRIC_META}/versions/installer") if v["stable"])


def fabric_launcher_url(mc: str, loader: str, installer: str) -> str:
    return f"{FABRIC_META}/versions/loader/{mc}/{loader}/{installer}/server/jar"


def fabric_compile_deps(mc: str, loader: str) -> list[dict]:
    """Fabric Loader + Sponge Mixin jars (with Fabric's published sha256) needed to
    compile the dashboard mod against this loader version."""
    meta = get_json(f"{FABRIC_META}/versions/loader/{mc}/{loader}/server/json")
    deps = []
    for lib in meta["libraries"]:
        group, artifact = lib["name"].split(":")[:2]
        if group == "net.fabricmc" and artifact in ("fabric-loader", "sponge-mixin"):
            g, a, v = lib["name"].split(":")
            url = f"{lib['url'].rstrip('/')}/{g.replace('.', '/')}/{a}/{v}/{a}-{v}.jar"
            sha = lib.get("sha256")
            if not sha:
                # meta has no sha256 for these: verify against Maven's .sha1, then pin sha256
                data = download(url)
                expected_sha1 = download(url + ".sha1").decode().split()[0].strip()
                if sha1(data) != expected_sha1:
                    raise RuntimeError(f"sha1 mismatch for {url}")
                sha = sha256(data)
            deps.append({"coord": lib["name"], "repo": lib["url"], "sha256": sha})
    if len(deps) != 2:
        raise RuntimeError(f"could not find fabric-loader and sponge-mixin in loader meta: {deps}")
    return sorted(deps, key=lambda d: d["coord"])


def newest_mod_version(slug: str, mc_version: str, channel: str) -> dict | None:
    """Newest Fabric build of `slug` for exactly `mc_version` that is at least as
    stable as `channel`. Returns None if there is none."""
    versions = get_json(
        f"{MODRINTH}/project/{slug}/version",
        {"loaders": json.dumps(["fabric"]), "game_versions": json.dumps([mc_version])},
    )
    allowed = CHANNEL_RANK[channel]
    versions = [v for v in versions if CHANNEL_RANK[v["version_type"]] <= allowed]
    if not versions:
        return None
    versions.sort(key=lambda v: v["date_published"], reverse=True)
    return versions[0]


def primary_file(version: dict) -> dict:
    files = [f for f in version["files"] if f.get("primary")] or version["files"]
    return files[0]


# --------------------------------------------------------------------------- modpack.toml

def load_modpack(path: Path = MODPACK) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _q(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))  # TOML basic strings are JSON compatible for our content


HEADER = """\
# modpack.toml: the single source of truth for this server pack.
#
# scripts/build.py turns this file into dist/ (downloads every file, verifies its
# hash, fails on mismatch). scripts/update.py rewrites the pinned versions and
# hashes below; prefer running it over editing pins by hand.
#
# Per-mod fields:
#   slug / project_id   Modrinth project
#   channel             least stable Modrinth build type update.py may pick
#                       (release | beta | alpha). The threading mods only publish
#                       alpha builds, so they are pinned to "alpha" on purpose.
#   version_id / version_number / filename / url / sha512   the pinned build
#   workload            what the mod does in this pack (used in release notes)
"""


def write_modpack(pack: dict, path: Path = MODPACK) -> None:
    out = [HEADER]
    out.append("[minecraft]")
    for key in ("version", "java", "server_sha1"):
        out.append(f"{key} = {_q(pack['minecraft'][key])}")
    out.append("")
    out.append("[fabric]")
    for key in ("loader", "installer", "launcher_sha256"):
        out.append(f"{key} = {_q(pack['fabric'][key])}")
    out.append("")
    out.append("[tuning]")
    out.append("# One knob for the worker pools of Async (entities) and C2ME (chunks + lighting).")
    out.append("#   0  = auto: C2ME uses max(1, cpus - 1), Async uses its own auto (~cpus / 1.3)")
    out.append("#   N  = pin both pools to N threads (recommended: physical cores - 1)")
    out.append("# build.py writes this into config/async.toml and config/c2me.toml.")
    out.append(f"worker_threads = {_q(pack['tuning']['worker_threads'])}")
    out.append("")
    out.append("[dashboard]")
    out.append("# perfdash: the in-game load dashboard built from dashboard/ by build_dashboard.py.")
    out.append("# compile = jars it is compiled against (not shipped); sha256 pinned after a Maven .sha1 check.")
    out.append(f"version = {_q(pack['dashboard']['version'])}")
    for dep in pack["dashboard"]["compile"]:
        out.append("")
        out.append("[[dashboard.compile]]")
        for key in ("coord", "repo", "sha256"):
            out.append(f"{key} = {_q(dep[key])}")
    for mod in pack["mods"]:
        out.append("")
        out.append("[[mods]]")
        for key in ("name", "slug", "project_id", "channel", "workload",
                    "version_id", "version_number", "filename", "url", "sha512"):
            out.append(f"{key} = {_q(mod[key])}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
