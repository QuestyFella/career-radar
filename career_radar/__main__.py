"""``python -m career_radar``: run the web app (with the built-in scheduler) or a one-off job."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(prog="career-radar", description="Self-hosted job radar for any region and every career level")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the web app and the scheduler (default)")
    serve.add_argument("--host", default=os.environ.get("CAREER_RADAR_HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("CAREER_RADAR_PORT", "8765")))
    sub.add_parser("collect", help="collect jobs from every enabled source once")
    discovery = sub.add_parser("discover", help="verify new boards found in leads and collected jobs once")
    discovery.add_argument("--reverify", action="store_true", help="re-check every discovered board now")
    sub.add_parser("scout", help="ask the configured LLM for new employer boards once")
    sub.add_parser("status", help="print discovery and source counts")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("CAREER_RADAR_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command in (None, "serve"):
        import uvicorn
        host = getattr(args, "host", os.environ.get("CAREER_RADAR_HOST", "0.0.0.0"))
        port = getattr(args, "port", int(os.environ.get("CAREER_RADAR_PORT", "8765")))
        uvicorn.run("career_radar.app:app", host=host, port=port, proxy_headers=False, log_level="info")
        return 0

    from . import config, core, discover, services
    config.seed_data_dir()
    core.init_db(config.db_path())
    if args.command == "collect":
        ok, summary = services.run_collection_job()
    elif args.command == "discover":
        ok, summary = services.run_discovery_job(reverify=args.reverify)
    elif args.command == "scout":
        ok, summary = services.run_scout_job()
    else:
        print(json.dumps(discover.status(), indent=2))
        return 0
    print(summary)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
