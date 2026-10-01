import argparse
import time

parser = argparse.ArgumentParser()
parser.add_argument("--mib", type=int, choices=range(1, 257), default=16, metavar="1..256")
parser.add_argument("--seconds", type=int, choices=range(1, 61), default=2, metavar="1..60")
args = parser.parse_args()
memory = bytearray(args.mib * 1024 * 1024)
# Touch each page and retain the allocation for the requested interval.
for offset in range(0, len(memory), 4096):
    memory[offset] = 1
print(f"holding {args.mib} MiB for {args.seconds}s", flush=True)
time.sleep(args.seconds)
print(f"memory-hold complete: bytes={len(memory)}", flush=True)
