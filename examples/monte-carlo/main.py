import argparse
import json
import random

parser = argparse.ArgumentParser()
parser.add_argument("--samples", type=int, default=100_000)
parser.add_argument("--seed", type=int, default=42)
args = parser.parse_args()
if not 1 <= args.samples <= 1_000_000:
    parser.error("--samples must be between 1 and 1000000")
rng = random.Random(args.seed)
inside = 0
for _ in range(args.samples):
    x, y = rng.random(), rng.random()
    inside += x * x + y * y <= 1
print(json.dumps({"seed": args.seed, "samples": args.samples, "inside": inside,
                  "pi_estimate": 4 * inside / args.samples}, sort_keys=True), flush=True)
