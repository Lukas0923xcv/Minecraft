package io.github.lukas0923xcv.perfdash.mixin;

import io.github.lukas0923xcv.perfdash.PerfDash;
import net.minecraft.network.protocol.game.ServerboundPlayerActionPacket;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.server.network.ServerGamePacketListenerImpl;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Key combination for vanilla clients: Sneak + "Swap item with offhand" (Shift+F
 * by default). The client sends the swap as a player-action packet, so the
 * server can see it without any client mod. While sneaking, the swap is
 * cancelled and the dashboard is toggled instead.
 */
@Mixin(ServerGamePacketListenerImpl.class)
public abstract class SwapHandsMixin {
    @Shadow
    public ServerPlayer player;

    @Inject(method = "handlePlayerAction", at = @At("HEAD"), cancellable = true)
    private void perfdash$toggleOnSneakSwap(ServerboundPlayerActionPacket packet, CallbackInfo ci) {
        if (packet.getAction() != ServerboundPlayerActionPacket.Action.SWAP_ITEM_WITH_OFFHAND) return;
        // Vanilla first receives the packet on a network thread and re-queues it
        // onto the server thread; only act on the server thread.
        if (!this.player.level().getServer().isSameThread()) return;
        if (!this.player.isShiftKeyDown()) return;
        ci.cancel();
        PerfDash.toggle(this.player);
    }
}
