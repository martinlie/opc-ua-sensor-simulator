#OPC UA plant simulation server - Nordvik Refinery, for stress testing OPC UA clients
#Based on the simple OPC-UA server by Flo Pachinger / flopach, Cisco Systems, July 2020
#Script based on the server example https://github.com/FreeOpcUa/python-opcua
#LGPL-3.0 License

import argparse
import asyncio
import logging

from simulator.plant import DEFAULT_CONFIG, load_plant
from simulator.server import serve


def parse_args():
    p = argparse.ArgumentParser(description="OPC UA server simulating a large process plant.")
    p.add_argument("--endpoint", default="opc.tcp://127.0.0.1:4840/opcua/", help="server endpoint URL")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="plant definition YAML")
    p.add_argument("--trains", type=int, default=1,
                   help="parallel trains per unit; 1 gives ~1k tags, 10 ~10k, 100 ~100k")
    p.add_argument("--fast-ms", type=float, default=1000, help="update period for fast tags (ms)")
    p.add_argument("--slow-s", type=float, default=10, help="update period for slow tags (s)")
    p.add_argument("--lab-s", type=float, default=3600, help="lab sample interval (s)")
    p.add_argument("--faults", type=float, default=0.01,
                   help="share of sensor and lab tags in a fault at any time (0 disables)")
    p.add_argument("--fault-duration", type=float, default=600, help="mean fault duration (s)")
    p.add_argument("--seed", type=int, default=42, help="random seed, same seed gives the same plant")
    p.add_argument("--export-tags", metavar="CSV", help="write the tag list (NodeIds, ranges, units) to a CSV file")
    p.add_argument("--no-metadata", action="store_true",
                   help="skip EURange/EngineeringUnits properties (fewer nodes, faster start)")
    p.add_argument("--stats-s", type=float, default=10, help="throughput log interval (s)")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    plant = load_plant(args.config, trains=args.trains, seed=args.seed)
    try:
        asyncio.run(serve(plant, args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
