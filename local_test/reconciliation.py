"""Phase 8 real-engine checks. Own controller, idle migrated local DB; never reset."""
import argparse
import asyncio
from contextlib import ExitStack
import os
import signal
import subprocess
import sys
from uuid import uuid4

import httpx
from sqlalchemy.exc import SQLAlchemyError

from common.schemas.assignments import WorkAssignment
from local_test.lab import Lab, LabError, STATE_DIR
from local_test.real_worker import acquire, lock_file
from local_test.execution import clean
from local_test.recovery import RecoveryLab, stop, until, SUCCESS
from local_test.provider import environment, control, running
from worker.config import WorkerSettings
from worker.executors.container import ContainerExecutor


def reconcile(h, w):
    result = subprocess.run([sys.executable, '-m', 'worker.main', 'reconcile'],
                            env=environment(h, w, credentials=True), timeout=35, capture_output=True, text=True)
    if result.returncode:
        raise LabError('Reconciliation CLI failed; inspect retained state (raw output omitted)')
    return result.stdout


def container(h, w, assignment, legacy=False):
    """Direct engine fixture using the production restricted creation policy."""
    async def create():
        settings = WorkerSettings(id=w['id'], token='fixture', container_engine=h.engine_name)
        backend = await ContainerExecutor(settings.podman_socket, settings.docker_socket, h.engine_name).select()
        backend.worker_id = None if legacy else settings.id
        try:
            a = WorkAssignment.model_validate(assignment)
            image = await backend.ensure_image(a.image)
            await backend.create(a, image)
            await backend.start()
        finally:
            await backend.client.aclose()
    h.orphans.add(assignment['attempt_id'])
    asyncio.run(create())


def exists(h, attempt):
    return h.api.get(f'/containers/meshcompute-{attempt["id"]}/json').status_code == 200


def claimed(h, w, name, maximum=2):
    hb = h.heartbeat(w)
    job = h.submit(name, seconds=50, maximum=maximum)
    assignment = h.lab.claim(w)
    stop(hb)
    return job, assignment, h.attempt(job)


def run(h):
    a, b = h.worker('reconcile-a'), h.worker('reconcile-b')
    bad_env = environment(h, a, credentials=True)
    bad_env['MESHCOMPUTE_WORKER_TOKEN'] = 'invalid-test-token'
    bad = subprocess.run([sys.executable, '-m', 'worker.main'], env=bad_env,
                         capture_output=True, text=True, timeout=10)
    assert bad.returncode != 0 and 'HTTP 401' in bad.stderr
    assert 'invalid-test-token' not in bad.stderr
    print('PASS invalid startup credentials fail clearly without endless retry or token output', flush=True)
    # A tiny unrelated provider fixture carries only a test-owned cleanup label.
    fixture = str(uuid4())
    r = h.api.post('/containers/create', json={
        'Image': 'localhost/meshcompute-sleep:dev', 'Entrypoint': ['python'],
        'Cmd': ['-c', 'import time; time.sleep(300)'], 'User': '65532:65532',
        'Labels': {'io.meshcompute.test.fixture': fixture},
        'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                       'SecurityOpt': ['no-new-privileges'], 'PidsLimit': 32,
                       'CpuPeriod': 100000, 'CpuQuota': 10000, 'Memory': 67108864,
                       'MemorySwap': 67108864, 'LogConfig': {'Type': 'none'}},
    })
    r.raise_for_status()
    unrelated = r.json()['Id']
    try:
        h.api.post(f'/containers/{unrelated}/start').raise_for_status()
        job = h.submit('hard-death', seconds=15)
        p = h.spawn(a, execute=True)
        first = running(h, job)  # Confirm actual engine execution before SIGKILL.
        h.orphans.add(first['id'])
        p.kill()
        assert p.wait(timeout=10) == -signal.SIGKILL
        h.backdate(first)
        lost = h.lost(job)
        assert exists(h, first)
        hb = h.heartbeat(a)  # Real startup reconciliation, no explicit reconcile call.
        until(lambda: not exists(h, first), 'Restart did not clean hard-death orphan')
        stop(hb)
        assert h.attempt(job) == lost
        h.finish(b, job, 2)
        assert h.api.get(f'/containers/{unrelated}/json').json()['State']['Running']
        print('PASS real SIGKILL -> LOST -> startup orphan cleanup -> second worker success; unrelated survives', flush=True)

        job, assignment, first = claimed(h, a, 'matching-and-missing')
        container(h, a, assignment)
        assert h.post(a, first, 'start', {'container_engine': h.actual}).status_code == 200
        baseline = h.attempt(job)
        assert 'active work remains' in reconcile(h, a)
        assert exists(h, first) and h.attempt(job) == baseline and h.reserved(first)
        assert h.api.get(f'/containers/meshcompute-{first["id"]}/json').json()['State']['Running']
        assert 'active work remains' in reconcile(h, a)
        assert exists(h, first) and h.attempt(job) == baseline
        payload = {'job_id': first['job_id'], 'container_engine': h.actual,
                   'lease_expires_at': baseline['lease_expires_at']}
        assert h.post(b, first, 'missing', payload).status_code == 403
        assert h.lab.client.post(h.route(a, first, 'missing'), json=payload).status_code == 401
        assert h.post(a, first, 'renew').status_code == 200
        assert h.post(a, first, 'missing', payload).status_code == 409
        h.remove_orphan(first['id'])  # Authoritative absence, independent of worker.
        reconcile(h, a)
        missing = h.attempt(job)
        assert missing['state'] == 'LOST' and missing['failure_reason'] == 'WORKLOAD_MISSING'
        assert not h.reserved(first) and h.job(job)['state'] == 'QUEUED'
        before = h.job(job)
        reconcile(h, a)
        assert h.attempt(job) == missing and h.job(job) == before
        # Exhaust retry via diagnostic lease expiry (no new execution needed).
        hb = h.heartbeat(b)
        h.lab.claim(b)
        second = h.attempt(job, 2)
        h.backdate(second)
        h.lost(job, 2, 'FAILED')
        stop(hb)
        print('PASS valid running container preserved, repeated reconciliation, authenticated/stale missing context, LOST/retry/release', flush=True)

        job, assignment, first = claimed(h, a, 'terminal-leftover', maximum=1)
        container(h, a, assignment, legacy=True)
        assert h.post(a, first, 'start', {'container_engine': h.actual}).status_code == 200
        assert h.post(a, first, 'result', SUCCESS | {'container_engine': h.actual}).status_code == 200
        terminal, completed = h.attempt(job), h.job(job)
        reconcile(h, a)
        assert not exists(h, first) and h.attempt(job) == terminal and h.job(job) == completed
        reconcile(h, a)
        assert h.attempt(job) == terminal
        print('PASS committed terminal controller result wins over running leftover; history/idempotency preserved (API fixture)', flush=True)

        assignment['attempt_id'], assignment['job_id'] = str(uuid4()), str(uuid4())
        container(h, a, assignment)
        unknown = {'id': assignment['attempt_id']}
        reconcile(h, b)
        assert exists(h, unknown), 'Another worker must not delete this identity’s container'
        reconcile(h, a)
        assert not exists(h, unknown)
        assert h.lab.client.get('/v1/attempts/' + unknown['id']).status_code == 404
        assert h.api.get(f'/containers/{unrelated}/json').json()['State']['Running']
        print('PASS unknown worker-labeled engine container cleaned; no retroactive attempt; unrelated untouched', flush=True)

        job, assignment, first = claimed(h, a, 'missing-exhausted', maximum=1)
        assert h.post(a, first, 'start', {'container_engine': h.actual}).status_code == 200
        reconcile(h, a)
        assert h.attempt(job)['failure_reason'] == 'WORKLOAD_MISSING'
        assert h.job(job)['state'] == 'FAILED' and h.job(job)['completed_at'] and not h.reserved(first)
        print('PASS missing workload exhausts max_attempts (assigned API fixture + real empty engine scan)', flush=True)

        job = h.submit('managed-workload-removed', seconds=40, maximum=1)
        p = h.spawn(a, execute=True)
        first = running(h, job)
        # Pause only our child while the engine fixture removes the container,
        # so its next observation is absence, not an intermediate stop exit code.
        p.send_signal(signal.SIGSTOP)
        try:
            h.remove_orphan(first['id'])
        finally:
            p.send_signal(signal.SIGCONT)
        assert p.wait(timeout=25) != 0
        missing = h.attempt(job)
        assert missing['state'] == 'LOST' and missing['failure_reason'] == 'WORKLOAD_MISSING'
        assert not h.reserved(first) and h.job(job)['state'] == 'FAILED'
        print('PASS external removal during real work-once reports missing; no generic FAILED result', flush=True)

        job, assignment, first = claimed(h, a, 'offline-orphan-reclaim', maximum=1)
        container(h, a, assignment)
        assert h.post(a, first, 'start', {'container_engine': h.actual}).status_code == 200
        baseline = h.attempt(job)
        env = environment(h, a, credentials=True)
        env.update(MESHCOMPUTE_WORKER_CONTAINER_ENGINE='podman', MESHCOMPUTE_WORKER_PODMAN_SOCKET='/nonexistent/meshcompute-test.sock')
        unavailable = subprocess.run([sys.executable, '-m', 'worker.main', 'reconcile'], env=env,
                                     capture_output=True, timeout=35)
        assert unavailable.returncode != 0 and h.attempt(job) == baseline and h.reserved(first)
        h.stop_controller()
        control(h, a, 'stop-all')  # No live execution session and no controller.
        assert not exists(h, first) and h.attempt(job) == baseline and h.reserved(first)
        h.backdate(first)
        h.start_controller()
        h.lost(job, state='FAILED')
        control(h, a, 'resume')
        print('PASS unavailable engine is not absence; offline stop-all reclaims labeled orphan without fake release', flush=True)

        # Heartbeat agent stays alive across an outage; reconnect cleans a new orphan.
        hb = h.heartbeat(a)
        h.stop_controller()
        import time
        time.sleep(6)
        assignment['attempt_id'], assignment['job_id'] = str(uuid4()), str(uuid4())
        container(h, a, assignment)
        unknown = {'id': assignment['attempt_id']}
        h.start_controller()
        until(lambda: not exists(h, unknown), 'Reconnect did not clean orphan', 35)
        stop(hb)
        print('PASS reconnect reconciliation without worker restart', flush=True)
        clean(h.lab)
    finally:
        response = h.api.get(f'/containers/{unrelated}/json')
        if response.status_code == 200:
            assert response.json()['Config']['Labels'].get('io.meshcompute.test.fixture') == fixture
            if response.json()['State']['Running']:
                h.api.post(f'/containers/{unrelated}/stop', params={'t': 1}).raise_for_status()
            h.api.delete(f'/containers/{unrelated}', params={'force': 'true', 'v': 'true'}).raise_for_status()
    print(f'Reconciliation validated ({h.actual}); retained results, no reset.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=['auto', 'podman', 'docker'], default=os.environ.get('MESHCOMPUTE_WORKER_CONTAINER_ENGINE', 'auto'))
    args = parser.parse_args()
    lab = None
    try:
        with ExitStack() as stack:
            for name in ('real-worker-test.lock', 'real-worker.lock'):
                if not acquire(stack.enter_context(lock_file(name))):
                    raise LabError('Stop helper agents/tests before test-reconciliation')
            lab = Lab(state_file=STATE_DIR / 'reconciliation-workers.json')
            h = RecoveryLab(lab, args.engine)
            try:
                h.start_controller()
                run(h)
            finally:
                h.close()
    except (LabError, AssertionError) as exc:
        raise SystemExit(f'FAIL {exc}; results retained, inspect make dev-state') from None
    except (SQLAlchemyError, httpx.HTTPError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f'Reconciliation checks failed ({type(exc).__name__}); inspect state; raw response omitted') from None
    except KeyboardInterrupt:
        raise SystemExit('Interrupted; owned children stopped; inspect state before rerunning') from None
    finally:
        if lab:
            lab.close()


if __name__ == '__main__':
    main()
