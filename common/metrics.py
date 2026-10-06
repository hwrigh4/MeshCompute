"""Process-local collectors. Labels are constants/enums, never identity strings."""
from prometheus_client import Counter, Histogram

BUCKETS = (.01, .05, .1, .5, 1, 5, 10, 30, 60, 120, 300, 900, 3600)


def counter(registry, name, help, labels=()):
    return Counter('meshcompute_' + name, help, labels, registry=registry)


def histogram(registry, name, help, labels=()):
    return Histogram('meshcompute_' + name, help, labels, registry=registry, buckets=BUCKETS)
