"""Optional loopback scrape target for worker-local events; no telemetry protocol."""
import os
from prometheus_client import CollectorRegistry, start_http_server
from common.metrics import counter, histogram

registry = CollectorRegistry()
executions = counter(registry, 'container_executions_total', 'Confirmed engine launches.', ['engine'])
execution_time = histogram(registry, 'container_execution_seconds', 'Confirmed launch through local cleanup, including shutdown grace.', ['engine'])
pulls = counter(registry, 'image_pull_total', 'Image acquisition pulls attempted; cached images excluded.', ['engine'])
pull_failures = counter(registry, 'image_pull_failures_total', 'Failed image acquisition pulls.', ['engine'])
reconciliations = counter(registry, 'reconciliation_runs_total', 'Local reconciliation passes, including internal bounded retries.')
reconciliation_failures = counter(registry, 'reconciliation_failures_total', 'Unresolved local reconciliation passes.')
reconciliation_time = histogram(registry, 'reconciliation_duration_seconds', 'Whole local reconciliation pass duration.')
orphans = counter(registry, 'reconciliation_orphans_removed_total', 'Confirmed owned orphan removals.', ['engine'])
missing = counter(registry, 'reconciliation_missing_workloads_total', 'Confirmed missing-workload reports.')


class MetricsUnavailable(RuntimeError):
    pass


def serve():
    value = os.environ.get('MESHCOMPUTE_WORKER_METRICS_PORT')
    if not value:
        return None
    try:
        port = int(value)
        if not 1024 <= port <= 65535:
            raise ValueError
        return start_http_server(port, addr='127.0.0.1', registry=registry)
    except (OSError, ValueError):
        raise MetricsUnavailable('Worker metrics listener unavailable; choose an unused port 1024..65535 in MESHCOMPUTE_WORKER_METRICS_PORT or unset it') from None
