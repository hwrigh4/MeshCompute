"""Phase 7 local-control checks; reuse the recovery lab's owned controller lifecycle."""
import argparse
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from sqlalchemy.exc import SQLAlchemyError

from local_test.lab import Lab, LabError, STATE_DIR
from local_test.real_worker import acquire, lock_file
from local_test.recovery import RecoveryLab, stop, until
from worker.agent.claim import claim_once
from worker.agent.execution import execute_assignment
from worker.config import WorkerSettings
from worker.executors.container import ContainerExecutor


def environment(h, w, credentials=False):
    env = os.environ.copy()
    env.update(MESHCOMPUTE_WORKER_ID=w['id'], MESHCOMPUTE_WORKER_CONTROLLER_URL=h.lab.url,
               MESHCOMPUTE_WORKER_CPU_LIMIT='1', MESHCOMPUTE_WORKER_MEMORY_LIMIT_MB='256',
               MESHCOMPUTE_WORKER_CONTAINER_ENGINE=h.engine_name)
    env.setdefault('MESHCOMPUTE_WORKER_STATE_DIR', str(STATE_DIR / 'provider-state'))
    env.pop('MESHCOMPUTE_WORKER_TOKEN', None)
    if credentials:
        env['MESHCOMPUTE_WORKER_TOKEN'] = w['token']
    return env


def control(h, w, action, *args):
    result = subprocess.run([sys.executable, '-m', 'worker.main', action, *args],
                            env=environment(h, w), capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise LabError(f'Local {action} failed; inspect state (raw output omitted)')
    return json.loads(result.stdout) if action == 'status' else result.stdout


def worker(h, w):
    return next(x for x in h.lab.snapshot()['workers'] if x['id'] == w['id'])


def running(h, job, number=1):
    def inspect():
        a = h.attempt(job, number)
        if not a or a['state'] != 'RUNNING':
            return None
        r = h.api.get(f'/containers/meshcompute-{a["id"]}/json')
        return a if r.status_code == 200 and r.json()['State']['Running'] else None
    return until(inspect, 'Real workload did not start')


def queued(h, name):
    # Deliberate second queue entry, to test eligibility while another job runs.
    return h.lab.request('POST', '/v1/jobs', json={
        'name': name, 'runtime': 'container', 'image': 'localhost/meshcompute-success:dev',
        'command': ['python', '/app/main.py'], 'resources': {'cpu': .5, 'memory_mb': 128},
        'timeout_seconds': 20, 'max_attempts': 1,
    }).json()


def run(h):
    a, b = h.worker('provider-a'), h.worker('provider-b')
    for w in (a, b):
        control(h, w, 'resources', '--cpu', '1', '--memory', '256')
        control(h, w, 'resume')
    hb = h.heartbeat(a)
    job = h.submit('provider-stop-before-launch', maximum=1)
    async def claimed_stop():
        settings = WorkerSettings(id=a['id'], token=a['token'], controller_url=h.lab.url,
                                  cpu_limit=1, memory_limit_mb=256, container_engine=h.engine_name,
                                  state_dir=environment(h, a)['MESHCOMPUTE_WORKER_STATE_DIR'])
        assignment = await claim_once(settings)
        assert str(assignment.job_id) == job['id']
        # Fixture timing only: a real claim has returned, but execution has not
        # begun. Stop then resume must not erase the intervening reclaim request.
        await asyncio.to_thread(control, h, a, 'stop-all')
        await asyncio.to_thread(control, h, a, 'resume')
        executor = ContainerExecutor(settings.podman_socket, settings.docker_socket, h.engine_name)
        return await execute_assignment(settings, executor, assignment)
    result = asyncio.run(claimed_stop())
    assert result.state == 'PREEMPTED' and result.started_at is None
    assert h.job(job)['started_at'] is None and not h.reserved(h.attempt(job))
    assert h.api.get(f'/containers/meshcompute-{result.id}/json').status_code == 404
    print('PASS claimed-before-launch stop request survives resume; real worker reports LEASED -> PREEMPTED', flush=True)
    control(h, a, 'pause')
    until(lambda: worker(h, a)['state'] == 'PAUSED', 'Pause not advertised')
    stop(hb)
    last = worker(h, a)['last_heartbeat']
    hb = h.spawn(a)  # Restart must preserve PAUSED, not become HEALTHY.
    until(lambda: worker(h, a)['last_heartbeat'] != last, 'Restart sent no fresh heartbeat')
    job = h.submit('provider-pause')
    until(lambda: worker(h, a)['state'] == 'PAUSED', 'Pause did not survive restart')
    assert h.lab.claim(a) is None
    diagnostic = subprocess.run([sys.executable, '-m', 'worker.main', 'claim'], env=environment(h, a, True), capture_output=True, text=True, timeout=10)
    assert diagnostic.returncode == 0 and 'No work' in diagnostic.stdout
    assert not h.attempt(job)
    status = control(h, a, 'status')
    assert status['participation'] == 'PAUSED' and status['controller_reachable']
    root = Path(environment(h, a)['MESHCOMPUTE_WORKER_STATE_DIR']) / a['id']
    assert root.stat().st_mode & 0o777 == 0o700
    raw = (root / 'provider.json').read_text()
    assert 'token' not in raw and a['token'] not in raw
    assert (root / 'provider.json').stat().st_mode & 0o777 == 0o600
    control(h, a, 'resume')
    until(lambda: worker(h, a)['state'] == 'HEALTHY', 'Resume not advertised')
    stop(hb)
    h.finish(a, job)
    print('PASS pause/resume, local claim gate, persisted restart state, credential-free protected storage', flush=True)

    job = h.submit('provider-drain', seconds=8, maximum=1)
    p = h.spawn(a, execute=True)
    active = running(h, job)
    control(h, a, 'drain')
    until(lambda: worker(h, a)['state'] == 'DRAINING', 'Drain not advertised')
    next_job = queued(h, 'after-drain')
    assert h.lab.claim(a) is None and h.reserved(active)
    assert p.wait(timeout=30) == 0 and h.attempt(job)['state'] == 'SUCCEEDED'
    assert control(h, a, 'status')['participation'] == 'DRAINING'
    assert h.spawn(a, execute=True).wait(timeout=15) == 0
    assert h.job(next_job)['state'] == 'QUEUED'
    h.finish(b, next_job)
    control(h, a, 'resume')
    print('PASS drain blocks new claims while existing real workload completes; state remains DRAINING', flush=True)

    job = h.submit('provider-reduction', seconds=8, maximum=1)
    p = h.spawn(a, execute=True)
    active = running(h, job)
    control(h, a, 'resources', '--cpu', '.25', '--memory', '64')
    resources = until(lambda: (r if (r := worker(h, a)['resources'])['cpu_contributed'] == .25 else None), 'Contribution not updated')
    assert resources['cpu_reserved'] == .5 and resources['memory_reserved_mb'] == 128
    assert resources['cpu_allocatable'] == resources['memory_allocatable_mb'] == 0
    assert h.attempt(job)['state'] == 'RUNNING'
    next_job = queued(h, 'after-reduction')
    assert h.lab.claim(a) is None
    assert p.wait(timeout=30) == 0 and h.attempt(job)['state'] == 'SUCCEEDED'
    assert h.spawn(a, execute=True).wait(timeout=15) == 0
    assert h.job(next_job)['state'] == 'QUEUED'
    control(h, a, 'resources', '--cpu', '1', '--memory', '256')
    h.finish(a, next_job)
    print('PASS reduced contributions floor capacity at zero, preserve existing execution, reject oversized claims', flush=True)

    # A real provider container has NO MeshCompute ownership label. It stays
    # running throughout stop-all; harness cleanup uses its own unique label.
    label = uuid4().hex
    response = h.api.post('/containers/create', params={'name': 'meshcompute-provider-fixture-' + label}, json={
        'Image': 'localhost/meshcompute-sleep:dev', 'Entrypoint': ['python'], 'Cmd': ['/app/main.py', '--seconds', '60'],
        'User': '65532:65532', 'Labels': {'io.meshcompute.test.provider': label},
        'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                       'SecurityOpt': ['no-new-privileges'], 'PidsLimit': 64, 'CpuPeriod': 100000,
                       'CpuQuota': 10000, 'Memory': 67108864, 'MemorySwap': 67108864},
    })
    response.raise_for_status()
    unrelated = response.json()['Id']
    try:
        h.api.post(f'/containers/{unrelated}/start').raise_for_status()
        job = h.submit('provider-online-stop', seconds=15, maximum=2)
        p = h.spawn(a, execute=True)
        first = running(h, job)
        status = control(h, a, 'status')
        assert status['active_local_session']['attempt_id'] == first['id']
        started = time.monotonic()
        control(h, a, 'stop-all')
        assert time.monotonic() - started < 10, 'Local reclaim unexpectedly slow'
        assert h.api.get(f'/containers/meshcompute-{first["id"]}/json').status_code == 404
        assert h.api.get(f'/containers/{unrelated}/json').json()['State']['Running']
        assert p.wait(timeout=30) == 0
        result = h.attempt(job)
        assert result['state'] == 'PREEMPTED' and result['failure_reason'] == 'PROVIDER_PREEMPTED'
        assert h.job(job)['state'] == 'QUEUED' and not h.reserved(first)
        payload = {key: result[key] for key in ('state', 'container_engine', 'exit_code', 'failure_reason', 'stdout_tail', 'stderr_tail', 'stdout_truncated', 'stderr_truncated')}
        assert h.post(a, first, 'result', payload).json() == result
        assert h.post(b, first, 'result', payload).status_code == 403
        assert h.lab.client.post(h.route(a, first, 'result'), json=payload).status_code == 401
        assert h.post(a, first, 'result', payload | {'state': 'FAILED'}).status_code == 422
        assert h.post(a, first, 'result', payload | {'state': 'FAILED', 'failure_reason': 'WORKER_INTERRUPTED'}).status_code == 409
        assert h.post(a, first, 'renew').status_code == 409
        # Remove our fixture before the retry so only the deliberately tiny
        # ownership-check overlap above runs concurrently.
    finally:
        info = h.api.get(f'/containers/{unrelated}/json')
        if info.status_code == 200:
            assert info.json()['Config']['Labels'].get('io.meshcompute.test.provider') == label
            h.api.post(f'/containers/{unrelated}/stop', params={'t': 2}).raise_for_status()
            h.api.delete(f'/containers/{unrelated}', params={'force': 'true', 'v': 'true'}).raise_for_status()
    second = h.finish(b, job, 2)
    assert second['worker_id'] != first['worker_id'] and h.job(job)['started_at'] == first['started_at']
    assert h.post(a, first, 'result', payload).json() == result
    assert h.job(job)['state'] == 'SUCCEEDED' and h.attempt(job, 2) == second
    print(f'PASS online stop-all: real {h.actual} cleanup, PREEMPTED/auth/idempotency, unrelated container preserved, second-worker retry', flush=True)

    job = h.submit('provider-exhaustion', seconds=15, maximum=2)
    for number in (1, 2):
        control(h, a, 'resume')
        p = h.spawn(a, execute=True)
        first = running(h, job, number)
        control(h, a, 'stop-all')
        assert p.wait(timeout=30) == 0
        assert h.attempt(job, number)['state'] == 'PREEMPTED' and not h.reserved(first)
        assert h.job(job)['state'] == ('QUEUED' if number == 1 else 'FAILED')
    control(h, a, 'resume')
    assert h.spawn(a, execute=True).wait(timeout=15) == 0
    assert len([x for x in h.lab.snapshot()['attempts'] if x['job_id'] == job['id']]) == 2
    print('PASS repeated PREEMPTED attempts exhaust max_attempts, no attempt 3', flush=True)

    job = h.submit('provider-offline-stop', seconds=50, maximum=1)
    p = h.spawn(a, execute=True)
    first = running(h, job)
    h.orphans.add(first['id'])
    h.stop_controller()
    started = time.monotonic()
    control(h, a, 'stop-all')
    assert time.monotonic() - started < 10, 'Offline reclaim waited for controller/lease'
    assert h.api.get(f'/containers/meshcompute-{first["id"]}/json').status_code == 404
    assert h.attempt(job)['state'] == 'RUNNING' and h.reserved(first)
    assert not control(h, a, 'status')['controller_reachable']
    assert p.wait(timeout=30) != 0  # Report unavailable; no fake server release.
    h.orphans.remove(first['id'])
    assert h.attempt(job)['state'] == 'RUNNING' and h.reserved(first)
    h.backdate(first)  # Test fixture speeds only controller lease recovery.
    h.start_controller()
    lost = h.lost(job, state='FAILED')
    assert h.post(a, first, 'result', {'state': 'PREEMPTED', 'container_engine': h.actual, 'failure_reason': 'PROVIDER_PREEMPTED'}).status_code == 409
    assert h.attempt(job) == lost
    print('PASS offline stop-all reclaims locally before reporting; reservation remains until LOST recovery', flush=True)
    print('Provider controls validated; results retained. No orphan discovery/reconciliation.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=['auto', 'podman', 'docker'], default=os.environ.get('MESHCOMPUTE_WORKER_CONTAINER_ENGINE', 'auto'))
    args = parser.parse_args()
    lab = None
    try:
        with ExitStack() as stack:
            for name in ('real-worker-test.lock', 'real-worker.lock'):
                if not acquire(stack.enter_context(lock_file(name))):
                    raise LabError('Stop helper agents/tests before test-provider')
            lab = Lab(state_file=STATE_DIR / 'provider-workers.json')
            h = RecoveryLab(lab, args.engine)
            try:
                h.start_controller()
                run(h)
            finally:
                h.close()
    except (LabError, AssertionError) as exc:
        raise SystemExit(f'FAIL {exc}; provider results retained, inspect make dev-state') from None
    except (SQLAlchemyError, httpx.HTTPError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f'Provider checks failed ({type(exc).__name__}); inspect configuration/state; raw output omitted') from None
    except KeyboardInterrupt:
        raise SystemExit('Interrupted; owned children stopped; inspect state') from None
    finally:
        if lab:
            lab.close()


if __name__ == '__main__':
    main()
