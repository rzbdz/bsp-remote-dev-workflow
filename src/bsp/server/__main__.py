"""`bsp-hostd --config <site>/hosts/<host>.yaml`"""

from __future__ import annotations

import argparse
import logging


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    from .app import create_app
    from .config import HostConfig

    ap = argparse.ArgumentParser(prog="bsp-hostd", description="bsp host controller daemon")
    ap.add_argument("--config", "-c", required=True, help="host config YAML")
    ap.add_argument("--bind", help="override server.bind")
    ap.add_argument("--port", type=int, help="override server.port")
    ap.add_argument("--log-level", default="info")
    a = ap.parse_args(argv)

    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = HostConfig.load(a.config)
    app = create_app(cfg)
    uvicorn.run(app, host=a.bind or cfg.bind, port=a.port or cfg.port, log_level=a.log_level, access_log=False)


if __name__ == "__main__":
    main()
