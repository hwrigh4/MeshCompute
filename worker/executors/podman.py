from worker.executors.compatible import CompatibleExecutor


class PodmanExecutor(CompatibleExecutor):
    """Rootless Podman through its configured Docker-compatible Unix API."""
    engine = 'podman'

    async def capabilities_dropped(self, info):
        # Podman's compatible inspect expands ALL into its default capability
        # list. Its native inspect exposes the actual empty capability sets.
        response = await self.request('GET', f'/v4.0.0/libpod/containers/{self.container_id}/json', 'SECURITY_POLICY_UNSUPPORTED')
        native = response.json()
        return all(key in native and native[key] in (None, []) for key in ('EffectiveCaps', 'BoundingCaps'))
