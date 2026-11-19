package io.github.lukas0923xcv.perfdash;

import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.fabric.api.networking.v1.ServerPlayConnectionEvents;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.ChatFormatting;
import net.minecraft.commands.Commands;
import net.minecraft.network.chat.Component;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * perfdash: server-side, per-player performance dashboard.
 *
 * Any player toggles it with Sneak + Swap-offhand (Shift+F) or /dashboard.
 * It shows load per CPU core and CPU time per workload type (main tick,
 * Async entities, C2ME chunks + lighting, vanilla workers, network, GC/JIT).
 * Vanilla clients need nothing: it is drawn with scoreboard-sidebar packets.
 */
public final class PerfDash implements ModInitializer {
    public static final Logger LOGGER = LoggerFactory.getLogger("perfdash");

    private static final Map<UUID, Integer> VIEWERS = new ConcurrentHashMap<>(); // player -> lines currently shown
    private static Sampler sampler;
    private static Thread samplerThread;
    private static int refreshTicks = 20;
    private static boolean joinHint = true;

    private static long lastTickCount = -1;
    private static long lastTickWall;
    private static double tps = 20.0;

    @Override
    public void onInitialize() {
        loadConfig();

        ServerLifecycleEvents.SERVER_STARTED.register(server -> {
            sampler = new Sampler(1000);
            samplerThread = new Thread(sampler, "perfdash-sampler");
            samplerThread.setDaemon(true);
            samplerThread.setPriority(Thread.MIN_PRIORITY);
            samplerThread.start();
            LOGGER.info("perfdash ready: players toggle the dashboard with Sneak + Swap-offhand (Shift+F) or /dashboard");
            // Load the packet listener now (normally loaded on first join) so a broken
            // key-combo hook shows up at startup instead of when a player connects.
            try {
                Class<?> listener = Class.forName("net.minecraft.server.network.ServerGamePacketListenerImpl");
                boolean hooked = java.util.Arrays.stream(listener.getDeclaredMethods())
                        .anyMatch(m -> m.getName().contains("toggleOnSneakSwap"));
                if (hooked) {
                    LOGGER.info("perfdash key combo hook active (Sneak + Swap-offhand)");
                } else {
                    LOGGER.error("perfdash key combo hook NOT applied; only /dashboard will work");
                }
            } catch (ClassNotFoundException e) {
                LOGGER.error("perfdash: packet listener class not found", e);
            }
        });
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> {
            if (sampler != null) sampler.stop();
            if (samplerThread != null) samplerThread.interrupt();
            VIEWERS.clear();
        });

        ServerTickEvents.END_SERVER_TICK.register(PerfDash::onTick);

        ServerPlayConnectionEvents.JOIN.register((handler, sender, server) -> {
            if (joinHint) {
                handler.player.sendSystemMessage(Component.literal("[perfdash] ").withStyle(ChatFormatting.GOLD)
                        .append(Component.literal("Sneak + Swap-offhand (Shift+F) or /dashboard toggles the server load dashboard.")
                                .withStyle(ChatFormatting.GRAY)));
            }
        });
        ServerPlayConnectionEvents.DISCONNECT.register((handler, server) -> VIEWERS.remove(handler.player.getUUID()));

        CommandRegistrationCallback.EVENT.register((dispatcher, registryAccess, environment) ->
                dispatcher.register(Commands.literal("dashboard")
                        .executes(ctx -> {
                            toggle(ctx.getSource().getPlayerOrException());
                            return 1;
                        })
                        // Same panel as chat/console text (works from Crafty's console, used by CI).
                        .then(Commands.literal("print").executes(ctx -> {
                            for (Component line : snapshotLines(ctx.getSource().getServer())) {
                                ctx.getSource().sendSystemMessage(line);
                            }
                            return 1;
                        }))
                        .then(Commands.literal("selftest").executes(ctx -> {
                            String result = SelfTest.run(ctx.getSource().getServer(), snapshotLines(ctx.getSource().getServer()));
                            ctx.getSource().sendSystemMessage(Component.literal(result));
                            return 1;
                        }))));
    }

    private static List<Component> snapshotLines(MinecraftServer server) {
        double mspt = server.getAverageTickTimeNanos() / 1e6;
        double budget = server.tickRateManager().millisecondsPerTick();
        int entities = 0;
        int chunks = 0;
        for (ServerLevel level : server.getAllLevels()) {
            for (var ignored : level.getAllEntities()) entities++;
            chunks += level.getChunkSource().getLoadedChunksCount();
        }
        return Sidebar.render(sampler == null ? Sampler.Snapshot.EMPTY : sampler.latest(), tps, mspt, budget,
                entities, chunks, server.getPlayerList().getPlayerCount());
    }

    /** Called on the server thread. */
    public static void toggle(ServerPlayer player) {
        if (VIEWERS.remove(player.getUUID()) != null) {
            Sidebar.hide(player);
        } else {
            VIEWERS.put(player.getUUID(), 0);
            Sidebar.show(player, List.of(Component.literal("sampling…").withStyle(ChatFormatting.DARK_GRAY)), Sidebar.title());
            VIEWERS.put(player.getUUID(), 1);
        }
    }

    private static void onTick(MinecraftServer server) {
        if (server.getTickCount() % refreshTicks != 0) return;

        long now = System.nanoTime();
        long ticks = server.getTickCount();
        float tickRate = server.tickRateManager().tickrate();
        if (lastTickCount >= 0 && now > lastTickWall) {
            double measured = (ticks - lastTickCount) / ((now - lastTickWall) / 1e9);
            tps = Math.min(tickRate, measured);
        }
        lastTickCount = ticks;
        lastTickWall = now;

        if (VIEWERS.isEmpty() || sampler == null) return;

        List<Component> lines = snapshotLines(server);
        Component title = Sidebar.title();

        for (Map.Entry<UUID, Integer> e : VIEWERS.entrySet()) {
            ServerPlayer p = server.getPlayerList().getPlayer(e.getKey());
            if (p == null) {
                VIEWERS.remove(e.getKey());
                continue;
            }
            e.setValue(Sidebar.update(p, lines, e.getValue(), title));
        }
    }

    private static void loadConfig() {
        Path path = FabricLoader.getInstance().getConfigDir().resolve("perfdash.properties");
        Properties p = new Properties();
        try {
            if (Files.isRegularFile(path)) {
                try (InputStream in = Files.newInputStream(path)) {
                    p.load(in);
                }
            }
            refreshTicks = Math.max(5, Integer.parseInt(p.getProperty("refreshTicks", "20").trim()));
            joinHint = Boolean.parseBoolean(p.getProperty("joinHint", "true").trim());
            if (!Files.isRegularFile(path)) {
                p.setProperty("refreshTicks", String.valueOf(refreshTicks));
                p.setProperty("joinHint", String.valueOf(joinHint));
                try (OutputStream out = Files.newOutputStream(path)) {
                    p.store(out, "perfdash: refreshTicks = dashboard update interval in ticks (min 5); joinHint = tell players the key combo on join");
                }
            }
        } catch (Exception e) {
            LOGGER.warn("perfdash: could not read {}, using defaults ({})", path, e.toString());
        }
    }
}
