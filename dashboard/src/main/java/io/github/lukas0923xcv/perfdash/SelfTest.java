package io.github.lukas0923xcv.perfdash;

import io.netty.buffer.Unpooled;
import net.minecraft.network.FriendlyByteBuf;
import net.minecraft.network.RegistryFriendlyByteBuf;
import net.minecraft.network.chat.Component;
import net.minecraft.network.protocol.Packet;
import net.minecraft.network.protocol.game.ClientboundResetScorePacket;
import net.minecraft.network.protocol.game.ClientboundSetDisplayObjectivePacket;
import net.minecraft.network.protocol.game.ClientboundSetObjectivePacket;
import net.minecraft.network.protocol.game.ClientboundSetScorePacket;
import net.minecraft.server.MinecraftServer;

import java.util.ArrayList;
import java.util.List;

/**
 * /dashboard selftest: builds every packet the sidebar would send (show, an
 * update that shrinks, hide) for the current real panel and round-trips each
 * one through the game's own network codecs. This checks the packets can be
 * serialized for a vanilla client without needing a client (used by CI).
 */
final class SelfTest {
    private SelfTest() {
    }

    static String run(MinecraftServer server, List<Component> panel) {
        List<Packet<?>> packets = new ArrayList<>();
        packets.addAll(Sidebar.showPackets(panel, Sidebar.title()));
        packets.addAll(Sidebar.updatePackets(panel.subList(0, Math.max(1, panel.size() - 3)), panel.size(), Sidebar.title()));
        packets.addAll(Sidebar.hidePackets(null));

        int scores = 0;
        for (Packet<?> packet : packets) {
            RegistryFriendlyByteBuf buf = new RegistryFriendlyByteBuf(Unpooled.buffer(), server.registryAccess());
            Object back;
            switch (packet) {
                case ClientboundSetObjectivePacket p -> {
                    ClientboundSetObjectivePacket.STREAM_CODEC.encode(buf, p);
                    ClientboundSetObjectivePacket d = ClientboundSetObjectivePacket.STREAM_CODEC.decode(buf);
                    if (!d.getObjectiveName().equals(p.getObjectiveName()) || d.getMethod() != p.getMethod()) {
                        throw new IllegalStateException("objective packet changed in round trip");
                    }
                    back = d;
                }
                case ClientboundSetDisplayObjectivePacket p -> {
                    ClientboundSetDisplayObjectivePacket.STREAM_CODEC.encode((FriendlyByteBuf) buf, p);
                    back = ClientboundSetDisplayObjectivePacket.STREAM_CODEC.decode(buf);
                }
                case ClientboundSetScorePacket p -> {
                    ClientboundSetScorePacket.STREAM_CODEC.encode(buf, p);
                    ClientboundSetScorePacket d = ClientboundSetScorePacket.STREAM_CODEC.decode(buf);
                    if (!d.equals(p)) throw new IllegalStateException("score packet changed in round trip: " + p);
                    scores++;
                    back = d;
                }
                case ClientboundResetScorePacket p -> {
                    ClientboundResetScorePacket.STREAM_CODEC.encode((FriendlyByteBuf) buf, p);
                    back = ClientboundResetScorePacket.STREAM_CODEC.decode(buf);
                }
                default -> throw new IllegalStateException("unexpected packet " + packet);
            }
            if (back == null || buf.readableBytes() != 0) {
                throw new IllegalStateException("codec left " + buf.readableBytes() + " bytes for " + packet);
            }
            buf.release();
        }
        return "perfdash selftest OK: " + packets.size() + " sidebar packets (" + scores + " lines) round-tripped";
    }
}
