"""Bounded engine discovery and controller-authoritative cleanup.

Discovery never renews/adopts a workload. A surviving active container blocks new
claims until its owner finishes or its lease expires. Unknown legacy containers
without a worker label cannot be attributed safely and are left for inspection.
"""
import asyncio
import json
import logging
from pathlib import Path
from uuid import UUID

import httpx

from common.schemas.reconciliation import MAX_ATTEMPTS, ReconcileView
from worker.executors.container import engine_usable
from worker.executors.compatible import ExecutionError
from worker.executors.docker import DockerExecutor
from worker.executors.podman import PodmanExecutor
from worker.preemption.control import live_status

logger = logging.getLogger(__name__)
INTERVAL = 30
_CACHE = {}


class ReconciliationFailed(RuntimeError):
    pass


class ReconciliationRejected(ReconciliationFailed):
    """Invalid credentials/protocol need operator action, not an endless retry."""


def active_ids(settings):
    return sorted(_CACHE.get(str(settings.id), set()))[:MAX_ATTEMPTS]


def observed(settings, attempt_id, present):
    ids = _CACHE.setdefault(str(settings.id), set())
    if present:
        ids.add(str(attempt_id))
    else:
        ids.discard(str(attempt_id))


async def bounded_json(client, path, **kwargs):
    async with client.stream('GET', path, **kwargs) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=16384):
            data.extend(chunk)
            if len(data) > 2 * 1024 * 1024:
                raise ReconciliationFailed('Engine discovery exceeded its bound; no missing-workload inference')
        return json.loads(data)


async def discover(settings):
    """Returns complete scans only; never treat a failed scan as empty."""
    found, scanned = [], set()
    for engine, adapter, path in (
        ('podman', PodmanExecutor, settings.podman_socket),
        ('docker', DockerExecutor, settings.docker_socket),
    ):
        if settings.container_engine not in ('auto', engine):
            continue
        if not await engine_usable(path):
            if settings.container_engine == engine or Path(path).exists():
                raise ReconciliationFailed('Configured engine unavailable; cannot rule out old local execution')
            continue
        backend = adapter(path)
        try:
            rows = await bounded_json(backend.client, '/containers/json', params={
                'all': 'true', 'filters': json.dumps({'label': ['io.meshcompute.attempt']})})
            if not isinstance(rows, list) or len(rows) > MAX_ATTEMPTS:
                raise ReconciliationFailed('Too many labeled containers; inspect engine before claiming')
            for row in rows:
                labels = row.get('Labels') or {}
                worker = labels.get('io.meshcompute.worker')
                if worker is not None and worker != str(settings.id):
                    continue
                try:
                    attempt, job = str(UUID(labels['io.meshcompute.attempt'])), str(UUID(labels['io.meshcompute.job']))
                except (KeyError, ValueError, TypeError):
                    # Malformed labels are not authority to delete anything.
                    if worker == str(settings.id):
                        raise ReconciliationFailed('Invalid ownership labels for this worker; inspect manually') from None
                    continue
                info = await bounded_json(backend.client, f'/containers/{row["Id"]}/json')
                if info.get('Config', {}).get('Labels') != labels or info['Id'] != row['Id']:
                    raise ReconciliationFailed('Container changed during discovery; retry reconciliation')
                found.append(dict(engine=engine, path=path, adapter=adapter, id=info['Id'],
                                  attempt=attempt, job=job, worker=worker, running=info['State']['Running']))
            scanned.add(engine)
        finally:
            await backend.client.aclose()
    if len(found) > MAX_ATTEMPTS:
        raise ReconciliationFailed('Too many local attempts; inspect before claiming')
    return found, scanned


async def remove(container):
    backend = container['adapter'](container['path'])
    backend.container_id = container['id']
    backend.attempt_id = container['attempt']
    backend.job_id = container['job']
    backend.worker_id = container['worker']
    backend.create_attempted = True
    # cleanup re-inspects immutable engine ID and all expected ownership labels.
    # Discovery cannot establish ownership of attached persistent volumes.
    # Container tmpfs disappears on removal without deleting engine volumes.
    await backend.cleanup(remove_volumes=False)


async def reconcile(settings):
    # Engine socket activation/busy hosts can transiently fail a probe. Repeating
    # comparison/owned cleanup is safe; the actual claim is still sent only once.
    try:
        async with asyncio.timeout(25):
            for retry in range(3):
                try:
                    return await _reconcile(settings)
                except ReconciliationRejected:
                    raise
                except (ReconciliationFailed, ExecutionError, httpx.HTTPError, OSError,
                        ValueError, KeyError, TypeError, TimeoutError) as exc:
                    if retry == 2:
                        if isinstance(exc, ReconciliationFailed):
                            raise
                        raise ReconciliationFailed(f'Reconciliation unavailable ({type(exc).__name__}); no new claim authorized') from None
                    await asyncio.sleep(1)
    except TimeoutError:
        raise ReconciliationFailed('Reconciliation timed out; no new claim authorized') from None


async def _reconcile(settings):
    async with httpx.AsyncClient(
        base_url=str(settings.controller_url).rstrip('/') + '/',
        headers={'Authorization': f'Bearer {settings.token.get_secret_value()}'},
        timeout=5, trust_env=False, follow_redirects=False,
    ) as client:
        async def snapshot(ids):
            response = await client.post(f'v1/workers/{settings.id}/reconcile', json={'attempt_ids': ids})
            if response.status_code in (401, 403, 404, 422):
                raise ReconciliationRejected(f'Reconciliation rejected (HTTP {response.status_code}); check worker credentials and controller protocol')
            response.raise_for_status()
            return ReconcileView.model_validate(response.json())

        # Capture expected state BEFORE inspecting engine absence. Concurrent
        # renewals/starts invalidate this context at the missing endpoint.
        before = await snapshot([])
        found, scanned = await discover(settings)
        view = await snapshot([c['attempt'] for c in found])
        expected = {str(a.id): a for a in view.attempts}
        live = await live_status(settings)
        protected = live['attempt_id'] if live else None
        present, reporting, remaining = set(), set(), False
        for c in found:
            a = expected.get(c['attempt'])
            if c['worker'] is None and a is None:
                remaining = True
                logger.warning('Unattributed legacy labeled container left untouched; inspect ownership before claiming')
                continue
            if a is not None and str(a.job_id) != c['job']:
                raise ReconciliationFailed('Container job label disagrees with controller; inspect ownership')
            if a is not None and a.container_engine not in (None, c['engine']):
                raise ReconciliationFailed('Container engine disagrees with controller; inspect ownership')
            present.add(c['attempt'])
            if c['running']:
                reporting.add(c['attempt'])
            if a is None or not a.active:
                # The live executor owns its cleanup; lease keeper will stop it.
                # Never race its attached output/terminal reporting.
                if c['attempt'] != protected:
                    await remove(c)
                    reporting.discard(c['attempt'])
                    logger.info('Reconciled stale container for attempt %s', c['attempt'])
                else:
                    remaining = True
            else:
                remaining = True  # Discovery is not permission to execute again.
        _CACHE[str(settings.id)] = reporting
        for a in before.attempts:
            if not a.active:
                continue
            if (a.state != 'RUNNING' or str(a.id) in present
                    or a.container_engine not in scanned or str(a.id) == protected):
                continue
            # Recheck the local execution guard immediately before reporting.
            current = await live_status(settings)
            if current and current['attempt_id'] == str(a.id):
                continue
            response = await client.post(f'v1/workers/{settings.id}/attempts/{a.id}/missing', json={
                'job_id': str(a.job_id), 'container_engine': a.container_engine,
                'lease_expires_at': a.lease_expires_at.isoformat(),
            })
            if response.status_code != 409:
                response.raise_for_status()
                logger.info('Controller recorded missing workload %s', a.id)
        # Re-read after mutations: only controller release permits fresh claims.
        final = await snapshot([])
        return not final.attempts and not remaining and protected is None


async def report_absence(settings, assignment):
    """Execution observed an engine 404 after launch and completed local cleanup."""
    async with httpx.AsyncClient(
        base_url=str(settings.controller_url).rstrip('/') + '/',
        headers={'Authorization': f'Bearer {settings.token.get_secret_value()}'},
        timeout=5, trust_env=False, follow_redirects=False,
    ) as client:
        response = await client.post(f'v1/workers/{settings.id}/reconcile', json={'attempt_ids': []})
        response.raise_for_status()
        view = ReconcileView.model_validate(response.json())
        for a in view.attempts:
            if a.id == assignment.attempt_id and a.active and a.state == 'RUNNING':
                response = await client.post(f'v1/workers/{settings.id}/attempts/{a.id}/missing', json={
                    'job_id': str(a.job_id), 'container_engine': a.container_engine,
                    'lease_expires_at': a.lease_expires_at.isoformat(),
                })
                if response.status_code != 409:
                    response.raise_for_status()
                return


async def reclaim_local(settings):
    """Provider control needs no controller. Only this worker's explicit labels."""
    try:
        async with asyncio.timeout(30):
            found, scanned = await discover(settings)
            if not scanned:
                raise ReconciliationFailed('No engine inspected; local reclaim cannot be confirmed')
            for c in found:
                if c['worker'] == str(settings.id):
                    await remove(c)
                    observed(settings, c['attempt'], False)
    except (ExecutionError, httpx.HTTPError, OSError, ValueError, KeyError, TypeError, TimeoutError):
        raise ReconciliationFailed('Local reclaim not confirmed; inspect engine and owned containers') from None
