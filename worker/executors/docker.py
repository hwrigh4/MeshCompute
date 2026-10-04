from worker.executors.compatible import CompatibleExecutor


class DockerExecutor(CompatibleExecutor):
    """Docker through its configured Unix API; no CLI context or host credentials."""
    engine = 'docker'
