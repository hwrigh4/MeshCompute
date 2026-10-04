"""Post-claim fixtures for execution.py's four Phase 5.1 checks.

Uses real heartbeats, one real claim, and the production execution coordinator.
Only fixture setup and assertions live here; no result or engine behavior is mocked.
"""
import asyncio
from contextlib import suppress
import json
from uuid import uuid4

import httpx

from local_test.lab import LabError
from worker.agent.claim import claim_once
from worker.agent.execution import execute_assignment
from worker.agent.loop import run_agent
from worker.config import WorkerSettings
from worker.executors.container import ContainerExecutor

FAILURE_REASONS = {
    'image-failure': 'IMAGE_PULL_FAILED',
    'policy-rejection': 'SECURITY_POLICY_UNSUPPORTED',
    'name-collision': 'CONTAINER_CREATE_FAILED',
}
SCENARIOS = (*FAILURE_REASONS, 'oversized-result')


def assigned_state(lab, assignment):
    snapshot = lab.snapshot()
    return {
        'job': next(j for j in snapshot['jobs'] if j['id'] == str(assignment.job_id)),
        'attempt': next(a for a in snapshot['attempts'] if a['id'] == str(assignment.attempt_id)),
        'allocations': [a for a in snapshot['allocations'] if str(a['job_attempt_id']) == str(assignment.attempt_id)],
    }


async def run_assigned(lab, scenario, job_id):
    settings = WorkerSettings()
    if str(settings.controller_url).rstrip('/') != lab.url:
        raise LabError('Fixture and worker controller URLs must match')
    executor = ContainerExecutor(settings.podman_socket, settings.docker_socket, settings.container_engine)
    ready = asyncio.Event()
    heartbeat = asyncio.create_task(run_agent(settings, executor, ready=ready))
    try:
        await asyncio.wait_for(ready.wait(), 30)
        snapshot = lab.snapshot()
        if (any(j['id'] != str(job_id) and j['state'] in ('QUEUED', 'RUNNING') for j in snapshot['jobs'])
                or snapshot['allocations']):
            raise LabError('Queue/reservations changed; no fixture claim made')
        assignment = await claim_once(settings)  # Never retry an ambiguous claim.
        assert assignment is not None and assignment.job_id == job_id
        before = assigned_state(lab, assignment)
        assert before['attempt']['state'] == 'LEASED' and before['job']['state'] == 'RUNNING'
        assert before['attempt']['worker_id'] == str(settings.id)
        assert before['attempt']['started_at'] is None and before['job']['started_at'] is None
        assert before['attempt']['completed_at'] is None and before['job']['completed_at'] is None
        assert len(before['allocations']) == 1
        assert before['allocations'][0]['cpu_reserved'] == .5
        assert before['allocations'][0]['memory_reserved_mb'] == 128
        print(f'Fixture {scenario}: real LEASED attempt {assignment.attempt_id}; allocation present', flush=True)

        backend = await executor.select()
        socket_path = backend.socket_path
        await backend.client.aclose()
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket_path),
                                     base_url='http://engine', timeout=10, trust_env=False) as api:
            name = f'meshcompute-{assignment.attempt_id}'
            fixture_id = None
            fixture_create_sent = False
            fixture_label = 'io.meshcompute.local-test.fixture'
            fixture_token = uuid4().hex  # Ownership marker, not a worker credential.
            try:
                if scenario == 'image-failure':
                    response = await api.get(f'/images/{assignment.image}/json')
                    assert response.status_code == 404
                elif scenario == 'policy-rejection':
                    response = await api.get(f'/images/{assignment.image}/json')
                    response.raise_for_status()
                    assert '/data' in response.json()['Config']['Volumes']
                elif scenario == 'oversized-result':
                    # Valid terminal-report JSON except for the oversized tail;
                    # 413 (not Pydantic's 422) proves the body guard runs first.
                    body = json.dumps({'state': 'FAILED', 'failure_reason': 'IMAGE_PULL_FAILED',
                                       'stdout_tail': 'x' * (801 * 1024)}).encode()
                    headers = {'Authorization': 'Bearer ' + settings.token.get_secret_value(),
                               'Content-Type': 'application/json'}
                    response = lab.client.post(
                        f'/v1/workers/{settings.id}/attempts/{assignment.attempt_id}/result',
                        headers=headers, content=body,
                    )
                    assert response.status_code == 413
                    assert assigned_state(lab, assignment) == before
                    print('PASS oversized-result guard: HTTP 413; job, attempt, timestamps and allocation unchanged', flush=True)
                elif scenario == 'name-collision':
                    assert (await api.get(f'/containers/{name}/json')).status_code == 404
                    # Direct fixture creation, never started. No MeshCompute attempt
                    # label, no volume image, and no production cleanup authority.
                    fixture_create_sent = True
                    response = await api.post('/containers/create', params={'name': name}, json={
                        'Image': assignment.image, 'User': '65532:65532',
                        'Labels': {fixture_label: fixture_token},
                        'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True,
                                       'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges'],
                                       'CpuPeriod': 100000, 'CpuQuota': 50000,
                                       'Memory': 134217728, 'MemorySwap': 134217728, 'PidsLimit': 64},
                    })
                    response.raise_for_status()
                    fixture_id = response.json()['Id']
                    original = (await api.get(f'/containers/{fixture_id}/json')).json()
                    assert original['Config']['Labels'].get('io.meshcompute.attempt') != str(assignment.attempt_id)
                    assert not original['State']['Running']
                    assert assigned_state(lab, assignment) == before

                result = await execute_assignment(settings, executor, assignment)
                after = assigned_state(lab, assignment)
                assert after['attempt']['state'] == result.state and not after['allocations']
                if scenario in FAILURE_REASONS:
                    assert result.state == 'FAILED' and result.failure_reason == FAILURE_REASONS[scenario]
                    assert after['job']['state'] == 'FAILED'
                    assert result.started_at is None and after['job']['started_at'] is None
                    assert result.exit_code is None and not result.stdout_tail and not result.stderr_tail
                if fixture_id:
                    response = await api.get(f'/containers/{name}/json')
                    response.raise_for_status()
                    preserved = response.json()
                    assert preserved['Id'] == fixture_id
                    assert preserved['Config'] == original['Config'] and preserved['State'] == original['State']
                    print('PASS name-collision ownership: fixture unchanged after valid FAILED report and allocation release', flush=True)
                else:
                    assert (await api.get(f'/containers/{name}/json')).status_code == 404
            finally:
                if fixture_create_sent and fixture_id is None:
                    # A lost create response may still leave our fixture behind.
                    response = await api.get(f'/containers/{name}/json')
                    if response.status_code != 404:
                        response.raise_for_status()
                        candidate = response.json()
                        if candidate['Config'].get('Labels', {}).get(fixture_label) == fixture_token:
                            fixture_id = candidate['Id']
                if fixture_id:
                    # Independent test-owned cleanup, by ID AND unique fixture label.
                    response = await api.get(f'/containers/{fixture_id}/json')
                    response.raise_for_status()
                    assert response.json()['Config']['Labels'].get(fixture_label) == fixture_token
                    response = await api.delete(f'/containers/{fixture_id}', params={'force': 'true', 'v': 'true'})
                    response.raise_for_status()
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
