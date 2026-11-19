package io.github.lukas0923xcv.perfdash;

import net.minecraft.ChatFormatting;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.MutableComponent;
import net.minecraft.network.chat.numbers.BlankFormat;
import net.minecraft.network.protocol.Packet;
import net.minecraft.network.protocol.game.ClientboundResetScorePacket;
import net.minecraft.network.protocol.game.ClientboundSetDisplayObjectivePacket;
import net.minecraft.network.protocol.game.ClientboundSetObjectivePacket;
import net.minecraft.network.protocol.game.ClientboundSetScorePacket;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.scores.DisplaySlot;
import net.minecraft.world.scores.Objective;
import net.minecraft.world.scores.Scoreboard;
import net.minecraft.world.scores.criteria.ObjectiveCriteria;

import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

/**
 * Draws the dashboard into one player's sidebar using plain vanilla scoreboard
 * packets that are sent only to that player. The server's real scoreboard is
 * never touched, so nobody else sees it and vanilla clients need nothing.
 */
final class Sidebar {
    static final String OBJECTIVE = "perfdash";
    static final int MAX_LINES = 15; // vanilla sidebar limit

    private static final Scoreboard DUMMY_BOARD = new Scoreboard();
    private static final String[] BAR = {"▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"};

    private Sidebar() {
    }

    private static Objective objective(Component title) {
        return new Objective(DUMMY_BOARD, OBJECTIVE, ObjectiveCriteria.DUMMY, title,
                ObjectiveCriteria.RenderType.INTEGER, false, BlankFormat.INSTANCE);
    }

    static void show(ServerPlayer player, List<Component> lines, Component title) {
        showPackets(lines, title).forEach(player.connection::send);
    }

    /** Sends the lines; removes leftover lines if the list got shorter. Returns the new line count. */
    static int update(ServerPlayer player, List<Component> lines, int previousCount, Component title) {
        updatePackets(lines, previousCount, title).forEach(player.connection::send);
        return Math.min(lines.size(), MAX_LINES);
    }

    static void hide(ServerPlayer player) {
        // Restore the server's own sidebar objective, if the server has one.
        Objective real = player.level().getServer().getScoreboard().getDisplayObjective(DisplaySlot.SIDEBAR);
        hidePackets(real).forEach(player.connection::send);
    }

    // Packet builders (separate so /dashboard selftest can round-trip them through the codecs)

    static List<Packet<?>> showPackets(List<Component> lines, Component title) {
        List<Packet<?>> out = new ArrayList<>();
        Objective obj = objective(title);
        out.add(new ClientboundSetObjectivePacket(obj, ClientboundSetObjectivePacket.METHOD_ADD));
        out.add(new ClientboundSetDisplayObjectivePacket(DisplaySlot.SIDEBAR, obj));
        out.addAll(updatePackets(lines, 0, title));
        return out;
    }

    static List<Packet<?>> updatePackets(List<Component> lines, int previousCount, Component title) {
        List<Packet<?>> out = new ArrayList<>();
        out.add(new ClientboundSetObjectivePacket(objective(title), ClientboundSetObjectivePacket.METHOD_CHANGE));
        int n = Math.min(lines.size(), MAX_LINES);
        for (int i = 0; i < n; i++) {
            // Sidebar sorts by score, highest first: line i gets score (MAX_LINES - i).
            out.add(new ClientboundSetScorePacket("l" + i, OBJECTIVE, MAX_LINES - i,
                    Optional.of(lines.get(i)), Optional.of(BlankFormat.INSTANCE)));
        }
        for (int i = n; i < previousCount; i++) {
            out.add(new ClientboundResetScorePacket("l" + i, OBJECTIVE));
        }
        return out;
    }

    static List<Packet<?>> hidePackets(Objective serverSidebar) {
        List<Packet<?>> out = new ArrayList<>();
        out.add(new ClientboundSetObjectivePacket(objective(Component.empty()), ClientboundSetObjectivePacket.METHOD_REMOVE));
        if (serverSidebar != null) {
            out.add(new ClientboundSetDisplayObjectivePacket(DisplaySlot.SIDEBAR, serverSidebar));
        }
        return out;
    }

    // ------------------------------------------------------------------ rendering

    static Component title() {
        return Component.literal("⚙ Server load ").withStyle(ChatFormatting.GOLD, ChatFormatting.BOLD)
                .append(Component.literal("(Shift+F)").withStyle(ChatFormatting.DARK_GRAY));
    }

    static List<Component> render(Sampler.Snapshot s, double tps, double mspt, double msptBudget,
                                  int entities, int chunks, int players) {
        List<Component> out = new ArrayList<>();

        out.add(Component.literal("TPS ").withStyle(ChatFormatting.GRAY)
                .append(Component.literal(String.format("%.1f", tps)).withStyle(tps >= 19.5 ? ChatFormatting.GREEN : tps >= 15 ? ChatFormatting.YELLOW : ChatFormatting.RED))
                .append(Component.literal("  MSPT ").withStyle(ChatFormatting.GRAY))
                .append(Component.literal(String.format("%.1f", mspt)).withStyle(colorFor(100 * mspt / msptBudget))));

        if (s.cpus() == 0) {
            out.add(Component.literal("sampling…").withStyle(ChatFormatting.DARK_GRAY));
            return out;
        }

        out.add(Component.literal("CPU ").withStyle(ChatFormatting.GRAY)
                .append(pct(s.systemPct())).append(Component.literal(" host · ").withStyle(ChatFormatting.DARK_GRAY))
                .append(pct(s.processPct() / Math.max(1, s.cpus())))
                .append(Component.literal(" server").withStyle(ChatFormatting.DARK_GRAY)));

        // ---- per core
        double[] cores = s.corePct();
        int fixedLines = 2 + 1 + Sampler.Workload.values().length + 1; // header lines + "per type" + workloads + footer
        int coreLines = MAX_LINES - fixedLines;
        if (cores.length > 0) {
            int perLine = Math.max(2, (cores.length + coreLines - 1) / coreLines);
            for (int start = 0; start < cores.length; start += perLine) {
                MutableComponent line = Component.empty();
                for (int c = start; c < Math.min(cores.length, start + perLine); c++) {
                    if (c > start) line.append(Component.literal("  "));
                    line.append(Component.literal(String.format("%2d ", c)).withStyle(ChatFormatting.DARK_GRAY));
                    if (perLine <= 2) line.append(bar(cores[c], 6)).append(Component.literal(" "));
                    line.append(pct(cores[c]));
                }
                out.add(line);
            }
        } else {
            out.add(Component.literal("per-core: n/a (not Linux)").withStyle(ChatFormatting.DARK_GRAY));
        }

        // ---- per workload type, in % of one core
        out.add(Component.literal("by type (100% = 1 core)").withStyle(ChatFormatting.DARK_AQUA));
        for (Sampler.Workload w : Sampler.Workload.values()) {
            double v = s.workloadPct().getOrDefault(w, 0.0);
            out.add(Component.literal(w.label + " ").withStyle(ChatFormatting.GRAY)
                    .append(Component.literal(String.format("%.0f%%", v)).withStyle(colorFor(v / Math.max(1, s.cpus()) * 100))));
        }

        out.add(Component.literal(String.format("%d entities · %d chunks · %d on", entities, chunks, players))
                .withStyle(ChatFormatting.DARK_GRAY));
        return out;
    }

    private static Component pct(double v) {
        if (v < 0) return Component.literal("–").withStyle(ChatFormatting.DARK_GRAY);
        return Component.literal(String.format("%3.0f%%", v)).withStyle(colorFor(v));
    }

    private static ChatFormatting colorFor(double pct) {
        if (pct >= 90) return ChatFormatting.RED;
        if (pct >= 60) return ChatFormatting.GOLD;
        if (pct >= 30) return ChatFormatting.YELLOW;
        return ChatFormatting.GREEN;
    }

    /** Horizontal bar of `width` cells with 1/8 sub-cell resolution. width 0 = no bar. */
    private static Component bar(double pct, int width) {
        if (width <= 0) return Component.empty();
        double cells = Math.max(0, Math.min(100, pct)) / 100.0 * width;
        int full = (int) cells;
        int eighths = (int) Math.round((cells - full) * 8);
        StringBuilder filled = new StringBuilder();
        filled.append("█".repeat(full));
        if (eighths > 0 && full < width) filled.append(BAR[eighths - 1]);
        int used = full + (eighths > 0 && full < width ? 1 : 0);
        return Component.literal(filled.toString()).withStyle(colorFor(pct))
                .append(Component.literal("·".repeat(Math.max(0, width - used))).withStyle(ChatFormatting.DARK_GRAY));
    }
}
