# Multi-core Fabric server for Crafty Controller

A ready-to-import **Minecraft Java 26.3** server (Fabric, Java 25) that spreads
work over all CPU cores **by type of workload**, not by world region:

| Workload | Mod | Runs on |
|---|---|---|
| Entity ticking (mobs, villagers, animals, projectiles, ...) | **Async** | Async's worker pool (parallel per entity) |
| Chunk loading, world generation, chunk IO and saving | **C2ME** | C2ME global executor |
| Lighting updates | **ScalableLux** | the **same** C2ME executor (see below) |
| General tick optimizations (single-threaded) | Lithium | main thread (makes it cheaper) |
| Memory | FerriteCore | — |
| Networking, chunk sending, entity tracking for many players | Very Many Players (VMP) | Netty IO threads + off-thread tracking |
| Profiling | spark | — |
| Pre-generation | Chunky | drives C2ME |
| Library | Fabric API | — |

Everything is **server-side only**, so vanilla 26.3 clients can join without installing anything.
Region threading (Folia and similar) is **not** used: a lag spike in one chunk
does not lock a region to a single core; each pool takes work from the whole world.

Pinned versions (Modrinth version IDs and sha512 for each file) are in [`modpack.toml`](modpack.toml).
Each [release](https://github.com/Lukas0923xcv/minecraft/releases) lists them again.

> **Stability.** Async, C2ME, ScalableLux and VMP are only published as **alpha**
> builds on Modrinth (that's normal for these mods). Async calls itself experimental.
> Keep backups, for example with Crafty's backup schedule.

---

## What is not parallel, and why

Some work cannot be safely parallelized with any existing mod. It stays on the
main thread:

* **Redstone, block entities (hoppers, furnaces, pistons...), scheduled block and fluid ticks.**
  Their results depend on a strict update order: redstone timing, hopper
  item order, and piston and observer chains. Running them in parallel would change behavior and break
  contraptions or duplicate/delete items. No maintained Fabric mod does this safely, and
  this pack does not try to.
* **Player actions, commands, chunk tickets, world border, raids, the game loop itself.**
* Inside Async: **TNT, dropped items, XP orbs** (Async's default `synchronizedEntities`)
  and **minecarts** (forced by Async) tick on the main thread.
* **Random ticks** (crop growth, leaf decay...) and **mob spawning** *can* run in parallel
  through Async, but both are **off by default** (experimental). See "Thread tuning" below.

So a world full of mobs or a burst of chunk generation spreads over all cores.
A huge redstone/hopper build still costs main-thread time. Check it with `/spark profiler`.

---

## Install in Crafty Controller 4

1. Download `minecraft-server-26.3-<tag>.zip` from the
   [latest release](https://github.com/Lukas0923xcv/minecraft/releases/latest).
   Optionally check it against `SHA256SUMS`:
   `sha256sum -c SHA256SUMS --ignore-missing`.
2. Crafty → **Servers → Create New Server → Minecraft Java** → choose **Import from zip** (the zip-upload import).
   * Upload the zip. The files sit at the **root** of the zip (`server.jar`,
     `mods/`, `config/`, `server.properties`, `eula.txt`). If Crafty asks for a root folder, pick the top level.
   * **Server JAR / executable:** `server.jar`
   * **Min / Max memory:** your heap size (for example 8 GB / 8 GB, see `jvm-flags.txt`)
   * **Port:** the zip has its own `server.properties` with `server-port=25565`, and
     Crafty keeps that file on import. To use another port, change `server-port` in
     Crafty's file editor after the import.
3. Open the new server → **Config** tab → **Server Execution Command**, and replace it with
   the one-line command from [`jvm-flags.txt`](jvm-flags.txt) (change `8G` to your heap size). The
   Crafty Docker image ships Java 25 at `/usr/lib/jvm/java-25-openjdk-amd64/bin/java`.
   A plain `java` there may be an older version, so use the full path. Save.
4. Click **Start**. Crafty asks you to **accept the Minecraft EULA**
   ([aka.ms/MinecraftEULA](https://aka.ms/MinecraftEULA)) and writes `eula=true`.
   * On first start, the Fabric launcher downloads the vanilla 26.3 server jar from Mojang
     (it is not in the zip, because Mojang's EULA forbids redistributing it) and the Fabric libraries,
     then checks them. The host needs internet access for that once.
5. The whitelist is **on** (vanilla default). In the Crafty terminal, run `whitelist add <you>`
   and `op <you>`.
6. Check that everything runs:
   `async stats` (Async pool and thread count), `spark tps`, and in `logs/latest.log`:
   `Initialized Pool with N threads`, `Global Executor Parallelism: N configured`,
   `[ScalableLux] Lighting scaling is enabled in externally managed mode`.
7. Optional: pre-generate the world with `chunky radius 3000` and then `chunky start`.

To update an existing server to a new release, stop it, back it up, replace `mods/` and
`server.jar` with the ones from the new zip, and compare `config/` (keep your own edits).
Don't overwrite the world.

---

## Thread tuning

Target hosts have 4 to 8 cores. The goal is that **cores go where the load is**:

* Each parallel pool may use up to **about cores − 1** threads, so a single busy workload
  can use almost the whole CPU while one core stays free for the main thread, GC and networking.
* The pools **overlap on purpose** (3 + 3 threads on 4 cores). Entity ticking happens
  every tick, inside the tick, while the main thread waits for it. Chunk and lighting work comes in bursts
  (exploration, `/chunky`). The OS scheduler gives cores to whichever pool has work.
  C2ME's workers also run at reduced priority (`SCHED_BATCH`), so when both pools want the same core,
  the tick (main thread + Async) wins.
* There are only **two** pools to size, not three: with C2ME installed, ScalableLux's
  lighting runs on C2ME's executor (C2ME's `c2me-threading-lighting` module merges the
  pools; the log says "externally managed mode"). `scalablelux.properties` is then ignored.

| Logical CPUs | Async (auto) | C2ME + lighting (shipped expression) | Recommended pin (`worker_threads`) |
|---|---|---|---|
| 4 | 3 | 3 | 3 |
| 6 | 4 | 5 | 5 |
| 8 | 6 | 7 | 7 |
| 8 cores / 16 threads (SMT) | 12 | 15 | 7 to 11 (test with spark) |

### The one place to change it

[`modpack.toml`](modpack.toml) → `[tuning] worker_threads`:

* `0` (default) = auto. C2ME uses `max(1, min(cpus - 1, (mem_gb - 0.5) * 3))`; Async uses its own
  auto value of `floor(cpus / 1.3)`. The same zip then fits any host.
* `N` = both pools get exactly N threads. `build.py` writes N into the two files below.
  You can also build once with `python scripts/build.py --worker-threads 7`.

### Changing it on a running server (per file)

| File | Setting | Notes |
|---|---|---|
| `config/async.toml` | `maxThreads = -1` (auto) or `N` | Then run `async config reload`, or restart. Clamped to the cpu count. |
| `config/c2me.toml` | `globalExecutorParallelism = "default"` (uses the expression) or `N` | A plain number overrides `defaultGlobalExecutorParallelismExpression`. Restart needed. This also sizes **lighting**. |
| `config/scalablelux.properties` | `parallelism` | **Ignored** while C2ME is installed. |
| `config/vmp.properties` | none | Netty event loops size themselves from the cpu count. |

Each file explains the reasoning in its header comments. Async, VMP and Minecraft re-save their
files on start and drop custom comments. That's why the explanations are also here.

### Experimental Async options (off by default)

```
async config setAsyncEntitySpawn true     # parallel mob spawning (not with Carpet's lagFreeSpawning)
async config setAsyncRandomTicks true     # parallel random ticks: experimental, back up first
```

Both commands save to `config/async.toml`. Measure before and after with `spark tps` and `async stats entity 10 200`.

### Lithium

`config/lithium.properties` sets only `mixin.alloc.chunk_random=false`. Async needs it, and Async
already applies it automatically through its `lithium:options` metadata (the log says
`1 override(s) found`). It's in the file too, so it stays off. No other Lithium option has to be disabled
for these versions of Async, C2ME or ScalableLux: Async ships its own locking for Lithium's
entity-movement trackers, block-change trackers and item lists.

### Expected warnings

On every start, the log shows two `Cancelled mixin com.ishland.c2me...` warnings. Async deliberately cancels
two of C2ME's thread-safety **detectors**, because Async's off-thread entity ticking would trip them.
They are not errors, and the CI smoke test allows exactly these two.

---

## server.properties

Only performance-related values differ from vanilla:

* `simulation-distance=6`: entity, block and redstone ticking is the expensive per-chunk work
  and, apart from entities, stays on the main thread. 6 gives 13×13 ticking chunks per player,
  instead of 21×21 at 10.
* `view-distance=10`: chunks beyond the simulation distance are only loaded, lit and sent.
  That's C2ME and ScalableLux work on worker threads, so it's cheap for the main thread.
* `sync-chunk-writes=true` (vanilla default) is kept for safety on crashes and power loss.
* `difficulty=normal` and the MOTD are cosmetic. `management-server-secret` is not shipped:
  the server generates its own.

## JVM flags

See [`jvm-flags.txt`](jvm-flags.txt): Aikar-style **G1** plus `-XX:+UseCompactObjectHeaders` on Java 25,
with the reasons for choosing it over generational ZGC on 4 to 8 cores, RAM sizing, and the exact Crafty command.

---

## Compatibility notes

Don't add these to this pack:

* **Other entity-threading mods** (for example TickWeave or C3H6N6O6) next to Async: they conflict.
* Mods that Async declares incompatible: **Moonrise**, **Open Parties and Claims**, **Accelerated Recoiling**.
* Mods that C2ME breaks or conflicts with: Tic-Tacs, OptiFabric, DynView, BetterChunkLoading.
  Don't add Starlight or Phosphor either: ScalableLux replaces them.
* Carpet's `lagFreeSpawning` rule together with Async's async spawning.

Extra mods I looked at and left out: none clearly helped without overlapping these or
changing vanilla behavior. ServerCore changes gameplay mechanics, and Krypton overlaps with VMP's
networking changes and is untested with it. Add mods one at a time, watch the log, and run the
smoke test (below).

---

## Repository layout

```
modpack.toml              single source of truth: MC/Java/Fabric versions, mods + pins + sha512, thread knob
server/                   copied into the zip: config/, server.properties, eula.txt (eula=false)
scripts/build.py          modpack.toml -> dist/<zip>, SHA256SUMS, release-notes.md (fails on any hash mismatch)
scripts/update.py         finds newer compatible builds on Modrinth/Fabric and rewrites modpack.toml
scripts/smoke_test.py     boots the zip headless and checks it (used by CI)
jvm-flags.txt             JVM flags + Crafty command
.github/workflows/        build.yml (CI + releases), update-check.yml (weekly update PR)
```

## Build locally

Needs Python 3.11+ (standard library only) and, for the smoke test, Java 25.

```bash
python scripts/build.py --tag dev
```

```bash
python scripts/smoke_test.py dist/minecraft-server-26.3-dev.zip --load-test
```

The build is reproducible: same commit, same zip (sorted entries, fixed timestamps, pinned hashes).

## Updating

* **Automatic:** `update-check.yml` runs every Monday. If any mod, Fabric Loader or installer has a newer build
  **for the pinned Minecraft version**, it rewrites `modpack.toml`, builds and boot-tests the result, and
  opens a PR (`automation/mod-updates`) that lists every change. Read the mods' changelogs, merge, then tag a release.
* **Manual:** `python scripts/update.py`, then review `git diff modpack.toml`.
* **New Minecraft version:** never automatic. The weekly PR (or `update.py`) reports when a new
  release is out and which mods still lack a build for it. Once every mod has one, run
  `python scripts/update.py --mc-version <new>`. It refuses to change anything while a mod is
  missing, so there's no silent downgrade and no partial pack. Then check that the config keys still exist,
  and that `server/server.properties` still matches the defaults of the new version.
* **Release:** `git tag v1.2.0 && git push --tags`. CI builds, runs the smoke test, and publishes the release.

## CI checks

`build.yml` runs on every push, PR and tag:

1. **build**: downloads every file and verifies it against `modpack.toml` (sha512 for mods, sha256
   for the Fabric launcher), builds the zip, and checks `SHA256SUMS`.
2. **smoke-test** (Java 25): starts the server headless in a temp dir with `eula=true` and the flags from
   `jvm-flags.txt`, waits for `Done`, runs `async stats` and `spark tps` over stdin, then runs a
   **load test**: summons 3000 mobs into 64 force-loaded chunks, then pre-generates about 3700 chunks
   with Chunky. MSPT and chunks/s go to the job summary. It then sends `stop`. The job fails on a crash,
   a non-zero exit, a missing mod, any ERROR/FATAL log line, any incompatibility, mixin-failure or
   threading-violation warning, or missing pool start-up lines.
3. **release** (tags `v*` only): attaches the zip and `SHA256SUMS` and uses the version table as release notes.
