import argparse
import time

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=int, choices=range(1, 61), default=1, metavar="1..60")
args = parser.parse_args()
deadline = time.monotonic() + args.seconds
iterations = 0
value = 1
while time.monotonic() < deadline:
    for _ in range(1000):
        value = (value * 1664525 + 1013904223) & 0xFFFFFFFF
    iterations += 1000
print(f"cpu-burn complete: iterations={iterations} checksum={value}", flush=True)
