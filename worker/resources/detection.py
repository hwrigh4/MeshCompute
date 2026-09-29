import os

import psutil

from common.schemas.workers import ResourceSnapshot, Telemetry


def detect_resources(cpu_limit: float, memory_limit_mb: int) -> tuple[ResourceSnapshot, Telemetry]:
    logical_cpus = psutil.cpu_count(logical=True)
    if not logical_cpus:
        raise RuntimeError("Cannot detect logical CPU capacity")
    memory = psutil.virtual_memory()
    total_mb = memory.total // (1024 * 1024)
    cpu_contributed = min(cpu_limit, logical_cpus)
    memory_contributed = min(memory_limit_mb, total_mb)
    resources = ResourceSnapshot(
        cpu_physical=logical_cpus,
        cpu_physical_cores=psutil.cpu_count(logical=False),
        cpu_contributed=cpu_contributed,
        cpu_allocatable=cpu_contributed,
        memory_physical_mb=total_mb,
        memory_contributed_mb=memory_contributed,
        memory_allocatable_mb=memory_contributed,
    )
    try:
        load_1m = os.getloadavg()[0]
    except (AttributeError, OSError):
        load_1m = None
    telemetry = Telemetry(
        cpu_usage_percent=psutil.cpu_percent(interval=None),
        load_1m=load_1m,
        memory_available_mb=memory.available // (1024 * 1024),
    )
    return resources, telemetry
