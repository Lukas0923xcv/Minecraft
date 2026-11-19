package io.github.lukas0923xcv.perfdash;

import java.lang.management.ManagementFactory;
import java.lang.management.ThreadInfo;
import java.lang.management.ThreadMXBean;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.EnumMap;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Background sampler (own daemon thread, never the server thread).
 *
 * Every second it measures:
 *  - load per logical CPU core, from /proc/stat (Linux; the whole host, so you
 *    also see load from other processes on the machine)
 *  - CPU time per workload, from the JVM's per-thread CPU clocks, grouped by
 *    thread name (each pool in this pack names its threads distinctly)
 *  - JVM-internal CPU (GC, JIT): process CPU time minus the sum of all Java threads
 *
 * Results are published as an immutable {@link Snapshot}; readers never block.
 */
final class Sampler implements Runnable {

    /** Workload groups shown on the dashboard. Order = display order. */
    enum Workload {
        MAIN("Main tick"),
        ENTITIES("Entities"),
        CHUNKS("Chunks+Light"),
        VANILLA("Vanilla workers"),
        NETWORK("Network"),
        JVM("GC / JIT"),
        OTHER("Other");

        final String label;

        Workload(String label) {
            this.label = label;
        }
    }

    static Workload classify(String threadName) {
        if (threadName.equals("Server thread")) return Workload.MAIN;
        if (threadName.startsWith("Async-Tick")) return Workload.ENTITIES;                 // Async
        if (threadName.startsWith("c2me") || threadName.startsWith("C2ME")
                || threadName.startsWith("scalablelux")) return Workload.CHUNKS;           // C2ME (+ ScalableLux lighting)
        if (threadName.startsWith("Worker-Main") || threadName.startsWith("IO-Worker")
                || threadName.startsWith("Download-")) return Workload.VANILLA;            // vanilla Util executors
        if (threadName.contains("Netty") || threadName.contains("netty")) return Workload.NETWORK;
        return Workload.OTHER;
    }

    /** Immutable result of one sample. Percentages are "% of one core" (200 = two full cores). */
    record Snapshot(double[] corePct, double systemPct, double processPct,
                    Map<Workload, Double> workloadPct, int cpus, long timeMillis) {
        static final Snapshot EMPTY = new Snapshot(new double[0], -1, -1, Map.of(), 0, 0);
    }

    private final ThreadMXBean threads = ManagementFactory.getThreadMXBean();
    private final com.sun.management.OperatingSystemMXBean os =
            ManagementFactory.getPlatformMXBean(com.sun.management.OperatingSystemMXBean.class);
    private final Path procStat = Path.of("/proc/stat");
    private final long intervalMillis;
    private volatile Snapshot latest = Snapshot.EMPTY;
    private volatile boolean running = true;

    private long[][] prevCoreTicks;          // [core][total, idle]
    private final Map<Long, Long> prevThreadCpu = new HashMap<>();
    private long prevProcessCpu = -1;
    private long prevWall = -1;

    Sampler(long intervalMillis) {
        this.intervalMillis = intervalMillis;
        if (threads.isThreadCpuTimeSupported() && !threads.isThreadCpuTimeEnabled()) {
            threads.setThreadCpuTimeEnabled(true);
        }
    }

    Snapshot latest() {
        return latest;
    }

    void stop() {
        running = false;
    }

    @Override
    public void run() {
        while (running) {
            try {
                sample();
            } catch (Throwable t) {
                PerfDash.LOGGER.debug("perfdash sample failed", t);
            }
            try {
                Thread.sleep(intervalMillis);
            } catch (InterruptedException e) {
                return;
            }
        }
    }

    private void sample() {
        long wall = System.nanoTime();
        double[] cores = sampleCores();

        // ---- per-thread CPU, grouped
        Map<Workload, Long> deltaNs = new EnumMap<>(Workload.class);
        long sumThreads = 0;
        Map<Long, Long> seen = new HashMap<>();
        if (threads.isThreadCpuTimeSupported()) {
            long[] ids = threads.getAllThreadIds();
            ThreadInfo[] infos = threads.getThreadInfo(ids);
            for (int i = 0; i < ids.length; i++) {
                if (infos[i] == null) continue;
                long cpu = threads.getThreadCpuTime(ids[i]);
                if (cpu < 0) continue;
                seen.put(ids[i], cpu);
                Long before = prevThreadCpu.get(ids[i]);
                long d = before == null ? 0 : Math.max(0, cpu - before);
                sumThreads += d;
                deltaNs.merge(classify(infos[i].getThreadName()), d, Long::sum);
            }
        }
        prevThreadCpu.clear();
        prevThreadCpu.putAll(seen);

        long processCpu = os.getProcessCpuTime();
        Map<Workload, Double> pct = new EnumMap<>(Workload.class);
        double processPct = -1;
        if (prevWall > 0) {
            double wallNs = wall - prevWall;
            for (Workload w : Workload.values()) {
                if (w == Workload.JVM) continue;
                pct.put(w, 100.0 * deltaNs.getOrDefault(w, 0L) / wallNs);
            }
            if (processCpu >= 0 && prevProcessCpu >= 0) {
                long proc = processCpu - prevProcessCpu;
                processPct = 100.0 * proc / wallNs;
                pct.put(Workload.JVM, Math.max(0, 100.0 * (proc - sumThreads) / wallNs));
            }
        }
        prevWall = wall;
        prevProcessCpu = processCpu;

        double system = cores.length > 0 ? avg(cores) : os.getCpuLoad() * 100.0;
        int cpus = Runtime.getRuntime().availableProcessors();
        if (!pct.isEmpty()) {
            latest = new Snapshot(cores, system, processPct, Map.copyOf(pct), cpus, System.currentTimeMillis());
        }
    }

    /** Per-core busy % from /proc/stat deltas. Empty array if not on Linux. */
    private double[] sampleCores() {
        List<long[]> now = new ArrayList<>();
        try {
            if (!Files.isReadable(procStat)) return new double[0];
            for (String line : Files.readAllLines(procStat)) {
                if (!line.startsWith("cpu") || line.startsWith("cpu ")) continue;
                String[] f = line.trim().split("\\s+");
                if (f.length < 5) continue;
                long total = 0;
                for (int i = 1; i < f.length && i <= 8; i++) total += Long.parseLong(f[i]); // user..steal
                long idle = Long.parseLong(f[4]) + (f.length > 5 ? Long.parseLong(f[5]) : 0);   // idle + iowait
                now.add(new long[]{total, idle});
            }
        } catch (Exception e) {
            return new double[0];
        }
        long[][] cur = now.toArray(new long[0][]);
        double[] out = new double[cur.length];
        if (prevCoreTicks != null && prevCoreTicks.length == cur.length) {
            for (int c = 0; c < cur.length; c++) {
                long dt = cur[c][0] - prevCoreTicks[c][0];
                long di = cur[c][1] - prevCoreTicks[c][1];
                out[c] = dt <= 0 ? 0 : 100.0 * (dt - di) / dt;
            }
        }
        prevCoreTicks = cur;
        return out;
    }

    private static double avg(double[] v) {
        double s = 0;
        for (double d : v) s += d;
        return v.length == 0 ? 0 : s / v.length;
    }
}
