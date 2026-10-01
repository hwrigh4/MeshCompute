import sys

print("meshcompute example: intentional failure", file=sys.stderr, flush=True)
raise SystemExit(7)
