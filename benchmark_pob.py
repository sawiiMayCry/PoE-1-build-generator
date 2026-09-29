"""Compare cold imports with a warmed persistent worker (no network/publishing)."""
import argparse
import json
import time
from pathlib import Path

from pob_engine import _cold_calculate_with_pob, calculate_with_pob, close_worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    root = Path(__file__).resolve().parent
    xml = args.xml.read_text(encoding="utf-8")
    start = time.perf_counter()
    cold = _cold_calculate_with_pob(xml, root, root / "data")
    cold_seconds = time.perf_counter() - start
    calculate_with_pob(xml, root, root / "data")
    start = time.perf_counter()
    for _ in range(args.iterations):
        warm = calculate_with_pob(xml, root, root / "data")
        if (cold["passives"] != warm["passives"] or
                any(warm["stats"].get(key) != value for key, value in cold["stats"].items())):
            raise RuntimeError("Cold and persistent calculation outputs differ")
    seconds = time.perf_counter() - start
    print(json.dumps({"coldSeconds": cold_seconds, "warmSecondsPerCalc": seconds / args.iterations,
                      "warmCalcsPerSecond": args.iterations / seconds, "identicalOutputs": True}, indent=2))
    close_worker()


if __name__ == "__main__":
    main()
