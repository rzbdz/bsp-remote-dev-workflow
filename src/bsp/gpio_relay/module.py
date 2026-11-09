"""gpio_relay module: header GPIOs as raw GPIO / relay / button channels."""

from __future__ import annotations

from typing import Any

from ..core.module import MODULES, Module, ModuleContext
from .backends import create_backend
from .board import BoardCatalog
from .commands import build_commands
from .pinmux import PinmuxInspector
from .service import ConfigStore, GpioRelayService


@MODULES.register("gpio_relay")
class GpioRelayModule(Module):
    name = "gpio_relay"
    title = "GPIO / Relay"

    def __init__(self, ctx: ModuleContext, config: dict[str, Any]):
        super().__init__(ctx, config)
        from ..core.site import layered

        board = BoardCatalog(layered("boards", (ctx.config_dir / config.get("site", "..")).resolve())).get(config["board"])
        backend = create_backend(
            config.get("backend", "sysfs"), config.get("backend_options"), timeout=float(config.get("hw_timeout_s", 3.0))
        )
        debugfs = board.pinctrl.get("debugfs") if config.get("pinmux", True) else None
        self.service = GpioRelayService(
            board,
            backend,
            ctx.bus,
            baseline=config.get("channels") or {},
            aliases=config.get("aliases") or {},
            store=ConfigStore(ctx.module_state_dir(self.name) / "channels.json"),
            alias_path=ctx.module_state_dir(self.name) / "aliases.json",
            protected=config.get("protected") or [],
            pinmux=PinmuxInspector(debugfs),
            max_pulse_ms=int(config.get("max_pulse_ms", 60_000)),
        )
        ctx.services["gpio_relay"] = self.service

    def commands(self):
        return build_commands(self.service)

    def startup(self) -> None:
        self.service.startup()

    def shutdown(self) -> None:
        self.service.shutdown()

    def status(self) -> dict[str, Any]:
        return {
            "board": self.service.board.id,
            "backend": self.service.backend.info(),
            "pinmux": self.service.pinmux.summary(),
        }
