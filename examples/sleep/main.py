import argparse
import time

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=int, choices=range(1, 61), default=2, metavar="1..60")
args = parser.parse_args()
print(f"sleeping {args.seconds}s", flush=True)
time.sleep(args.seconds)
print("sleep complete", flush=True)
