"""Sequential lease checks with real PostgreSQL, an owned controller and real workers.

Start PostgreSQL and migrate first. Stop other controllers/agents on this lab.
This command owns only its child controller/worker processes; it never resets data.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime
import os
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from controller.models.job_attempt import JobAttempt
from controller.models.worker import Worker
from controller.services.recovery import recover_one
from local_test.execution import clean
from local_test.lab import Lab, LabError, STATE_DIR
from local_test.real_worker import acquire, lock_file
from worker.config import WorkerSettings
from worker.executors.container import ContainerExecutor

STATE_FILE = STATE_DIR / 'recovery-workers.json'
SUCCESS = {'state': 'SUCCEEDED', 'container_engine': 'podman', 'exit_code': 0}
FAILURE = {'state': 'FAILED', 'failure_reason': 'ENGINE_UNAVAILABLE'}


def until(predicate, message, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise LabError(message)


def stop(process):
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            process.kill()  # Only a child created by this harness.
            process.wait(timeout=10)
            raise LabError('Owned child required SIGKILL; inspect retained attempts/containers')


class RecoveryLab:
    def __init__(self, lab, engine):
        self.lab, self.engine_name = lab, engine
        self.controller = None
        self.children = []
        self.orphans = set()
        parsed = urlsplit(lab.url)
        if parsed.scheme != 'http' or parsed.path not in ('', '/'):
            raise LabError('Recovery harness requires a plain loopback HTTP URL without a path')
        self.host, self.port = parsed.hostname, parsed.port or 80
        # Never terminate an existing controller. Restart testing requires ownership.
        with socket.socket(socket.AF_INET6 if ':' in self.host else socket.AF_INET) as probe:
            try:
                probe.bind((self.host, self.port))
            except OSError:
                raise LabError('Controller port is occupied. Stop your lab controller first; keep PostgreSQL running. No process stopped.') from None
        clean(lab)
        with lab.engine.connect() as connection:
            if connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one() != '0006':
                raise LabError('Run make dev-migrate before test-recovery')
        settings = WorkerSettings(id='00000000-0000-0000-0000-000000000000', token='probe', container_engine=engine)
        backend = asyncio.run(ContainerExecutor(settings.podman_socket, settings.docker_socket, engine).select())
        self.actual = backend.engine
        self.api = httpx.Client(transport=httpx.HTTPTransport(uds=backend.socket_path), base_url='http://engine', timeout=10, trust_env=False)
        asyncio.run(backend.client.aclose())
        for image in ('success', 'sleep'):
            if self.api.get(f'/images/localhost/meshcompute-{image}:dev/json').status_code != 200:
                self.api.close()
                raise LabError(f'Build images explicitly: make examples-build-{self.actual}')

    def start_controller(self):
        self.controller = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'controller.main:app',
                                           '--host', self.host, '--port', str(self.port), '--log-level', 'warning'])
        def ready():
            if self.controller.poll() is not None:
                raise LabError('Owned controller exited before becoming ready')
            try:
                return self.lab.client.get('/health').status_code == 200
            except httpx.RequestError:
                return False
        until(ready, 'Owned controller did not start')

    def stop_controller(self):
        stop(self.controller)
        self.controller = None

    def worker(self, alias):
        saved = self.lab.credentials()
        if alias not in saved:
            row = self.lab.request('POST', '/v1/workers/register', json={'name': 'recovery-' + alias, 'agent_version': '0.1.0'}).json()
            saved[alias] = {key: row[key] for key in ('id', 'token')}
            self.lab.save(saved)
        w = saved[alias]
        with Session(self.lab.engine) as session:
            if session.get(Worker, UUID(w['id'])) is None:
                raise LabError('Stale recovery credentials after reset; remove .meshcompute-lab/recovery-workers.json after inspecting configuration')
        return w

    def spawn(self, w, execute=False):
        env = os.environ.copy()
        env.setdefault("MESHCOMPUTE_WORKER_STATE_DIR", str(STATE_DIR / "provider-state"))
        env.update(MESHCOMPUTE_WORKER_CONTROLLER_URL=self.lab.url, MESHCOMPUTE_WORKER_ID=w['id'],
                   MESHCOMPUTE_WORKER_TOKEN=w['token'], MESHCOMPUTE_WORKER_CPU_LIMIT='1',
                   MESHCOMPUTE_WORKER_MEMORY_LIMIT_MB='256', MESHCOMPUTE_WORKER_CONTAINER_ENGINE=self.engine_name)
        p = subprocess.Popen([sys.executable, '-m', 'worker.main', *(['work-once'] if execute else [])], env=env)
        self.children.append(p)
        return p

    def heartbeat(self, w):
        previous = next((x['last_heartbeat'] for x in self.lab.snapshot()['workers'] if x['id'] == w['id']), None)
        p = self.spawn(w)
        until(lambda: any(x['id'] == w['id'] and x['state'] == 'HEALTHY'
                          and x['last_heartbeat'] != previous for x in self.lab.snapshot()['workers']), 'Real heartbeat not freshly healthy')
        return p

    def submit(self, name, seconds=None, maximum=2):
        clean(self.lab)
        return self.lab.request('POST', '/v1/jobs', json={
            'name': 'recovery-' + name, 'runtime': 'container',
            'image': f'localhost/meshcompute-{"sleep" if seconds else "success"}:dev',
            'command': ['python', '/app/main.py', *(['--seconds', str(seconds)] if seconds else [])],
            'resources': {'cpu': .5, 'memory_mb': 128}, 'timeout_seconds': 60, 'max_attempts': maximum,
        }).json()

    def attempt(self, job, number=1):
        return next((a for a in self.lab.snapshot()['attempts'] if a['job_id'] == job['id'] and a['attempt_number'] == number), None)

    def job(self, job):
        return next(j for j in self.lab.snapshot()['jobs'] if j['id'] == job['id'])

    def reserved(self, attempt):
        return any(str(a['job_attempt_id']) == attempt['id'] for a in self.lab.snapshot()['allocations'])

    def backdate(self, attempt):
        # Test-only DB fixture; no production time-travel API.
        with self.lab.engine.begin() as connection:
            connection.execute(text("UPDATE job_attempts SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=:id"), {'id': attempt['id']})

    def lost(self, job, number=1, state='QUEUED'):
        a = until(lambda: (a if (a := self.attempt(job, number)) and a['state'] == 'LOST' else None), 'Recovery did not mark LOST')
        assert a['failure_reason'] == 'LEASE_EXPIRED' and a['completed_at']
        assert not self.reserved(a) and self.job(job)['state'] == state
        worker = next(w for w in self.lab.snapshot()['workers'] if w['id'] == a['worker_id'])
        resources = worker['resources']
        assert resources['cpu_reserved'] == 0 and resources['memory_reserved_mb'] == 0
        assert resources['cpu_allocatable'] == resources['cpu_contributed']
        assert resources['memory_allocatable_mb'] == resources['memory_contributed_mb']
        return a

    def route(self, w, a, action):
        return f'/v1/workers/{w["id"]}/attempts/{a["id"]}/{action}'

    def post(self, w, a, action, payload=None):
        return self.lab.client.post(self.route(w, a, action), headers=self.lab.auth(w), **({'json': payload} if payload is not None else {}))

    def finish(self, w, job, number=1):
        p = self.spawn(w, execute=True)
        assert p.wait(timeout=75) == 0
        a = self.attempt(job, number)
        assert a['state'] == 'SUCCEEDED' and a['exit_code'] == 0 and a['container_engine'] == self.actual
        assert self.job(job)['state'] == 'SUCCEEDED' and not self.reserved(a)
        assert self.api.get(f'/containers/meshcompute-{a["id"]}/json').status_code == 404
        return a

    def remove_orphan(self, attempt_id):
        name = 'meshcompute-' + attempt_id
        response = self.api.get(f'/containers/{name}/json')
        if response.status_code == 404:
            return
        response.raise_for_status()
        assert response.json()['Config']['Labels'].get('io.meshcompute.attempt') == attempt_id
        if response.json()['State']['Running']:
            self.api.post(f'/containers/{name}/stop', params={'t': 2}).raise_for_status()
        self.api.delete(f'/containers/{name}', params={'force': 'true', 'v': 'true'}).raise_for_status()

    def close(self):
        try:
            for child in reversed(self.children):
                stop(child)
            for attempt_id in self.orphans:
                self.remove_orphan(attempt_id)
        finally:
            self.stop_controller()
            self.api.close()


def run(h):
    lab = h.lab
    a, b = h.worker('a'), h.worker('b')
    # Real CLI renews during a workload beyond the initial 30-second lease.
    job = h.submit('renewal', seconds=40)
    p = h.spawn(a, execute=True)
    first = until(lambda: (x if (x := h.attempt(job)) and x['state'] == 'RUNNING' else None), 'Workload did not start')
    initial = datetime.fromisoformat(first['lease_expires_at'])
    advanced = until(lambda: (x if (x := h.attempt(job)) and datetime.fromisoformat(x['lease_expires_at']) > initial else None), 'Lease did not advance', 20)
    assert advanced['state'] == 'RUNNING' and h.reserved(advanced)
    until(lambda: datetime.now(initial.tzinfo) >= initial, 'Initial deadline not reached', 35)
    live = h.attempt(job)
    assert live['state'] == 'RUNNING' and h.reserved(live)
    worker = until(lambda: next((w for w in lab.snapshot()['workers'] if w['id'] == a['id'] and datetime.fromisoformat(w['last_heartbeat']) > initial), None), 'Heartbeat did not advance', 7)
    assert worker['state'] == 'HEALTHY'
    assert p.wait(timeout=30) == 0
    assert h.attempt(job)['state'] == 'SUCCEEDED' and not h.reserved(live)
    print(f'PASS renewal beyond initial lease, heartbeat, terminal release ({h.actual})', flush=True)

    hb = h.heartbeat(a)
    job = h.submit('leased-expiry')
    assignment = lab.claim(a)
    first = h.attempt(job)
    assert assignment['lease_duration_seconds'] == 30 and assignment['renew_after_seconds'] == 10
    assert first['state'] == 'LEASED' and first['started_at'] is None and h.reserved(first)
    assert h.post(a, first, 'renew').status_code == 200
    assert h.post(b, first, 'renew').status_code == 403
    assert lab.client.post(h.route(a, first, 'renew')).status_code == 401
    assert h.post(a, first, 'result', FAILURE | {'failure_reason': 'LEASE_EXPIRED'}).status_code == 422
    assert h.post(a, first, 'result', {'state': 'LOST', 'failure_reason': 'LEASE_EXPIRED'}).status_code == 422
    h.backdate(first)
    lost = h.lost(job)
    assert lost['started_at'] is None and h.job(job)['started_at'] is None
    assert next(w for w in lab.snapshot()['workers'] if w['id'] == a['id'])['state'] == 'HEALTHY'
    before = h.job(job)
    assert h.post(a, lost, 'renew').status_code == 409
    assert h.post(a, lost, 'result', FAILURE).status_code == 409
    assert h.attempt(job) == lost and h.job(job) == before
    stop(hb)
    second = h.finish(b, job, 2)
    assert second['worker_id'] != first['worker_id'] and h.attempt(job)['state'] == 'LOST'
    assert h.post(a, lost, 'result', FAILURE).status_code == 409
    assert h.attempt(job, 2) == second
    print('PASS LEASED expiry, fresh heartbeat independence, authentication, stale renew/result, second-worker retry', flush=True)

    job = h.submit('hard-death', seconds=45)
    p = h.spawn(a, execute=True)
    first = until(lambda: (x if (x := h.attempt(job)) and x['state'] == 'RUNNING' else None), 'Hard-death workload did not start')
    name = 'meshcompute-' + first['id']
    until(lambda: (r.status_code == 200 and r.json()['State']['Running']) if (r := h.api.get(f'/containers/{name}/json')) else False, 'Container not running')
    until(lambda: h.attempt(job)['lease_expires_at'] > first['lease_expires_at'], 'No renewal before hard death', 20)
    h.orphans.add(first['id'])
    p.kill()
    assert p.wait(timeout=10) == -signal.SIGKILL
    h.backdate(first)
    h.lost(job)
    orphan = h.api.get(f'/containers/{name}/json')
    print(f'Observed hard-death orphan: exists={orphan.status_code == 200}; expected limitation', flush=True)
    h.remove_orphan(first['id'])  # Keep actual workloads sequential on small hosts.
    h.orphans.remove(first['id'])
    second = h.finish(b, job, 2)
    assert second['worker_id'] != first['worker_id']
    assert h.job(job)['started_at'] == first['started_at']
    print('PASS SIGKILL -> LOST -> different worker succeeds; test-owned orphan cleanup', flush=True)

    hb = h.heartbeat(a)
    job = h.submit('exhaustion', maximum=2)
    for number in (1, 2):
        assignment = lab.claim(a)
        assert assignment['attempt_id'] == h.attempt(job, number)['id']
        h.backdate(h.attempt(job, number))
        h.lost(job, number, 'QUEUED' if number == 1 else 'FAILED')
    assert h.job(job)['completed_at'] and lab.claim(a) is None
    assert len([x for x in lab.snapshot()['attempts'] if x['job_id'] == job['id']]) == 2
    # Deliberately inconsistent QUEUED fixture: the normal claim must repair it
    # rather than creating attempt 3. This is not a production requeue API.
    with lab.engine.begin() as connection:
        connection.execute(text("UPDATE jobs SET state='QUEUED', completed_at=NULL WHERE id=:id"), {'id': job['id']})
    assert lab.claim(a) is None and h.job(job)['state'] == 'FAILED'
    assert len([x for x in lab.snapshot()['attempts'] if x['job_id'] == job['id']]) == 2
    print('PASS max_attempts=2 exhausted, defensive claim bound, no third attempt', flush=True)

    # Own controller is stopped. Real ASGI endpoints + PostgreSQL run without a
    # lifespan/reaper solely to prove expiry rejection BEFORE persisted LOST.
    job = h.submit('restart-and-expiry')
    lab.claim(a)
    first = h.attempt(job)
    stop(hb)
    h.stop_controller()
    h.backdate(first)
    before = h.attempt(job)
    async def expired_api():
        from controller.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=lab.url) as client:
            for action, payload in [('renew', None), ('start', {'container_engine': h.actual}), ('result', FAILURE | {'container_engine': h.actual})]:
                response = await client.post(h.route(a, first, action), headers=lab.auth(a), **({'json': payload} if payload else {}))
                assert response.status_code == 409
    asyncio.run(expired_api())
    assert h.attempt(job) == before and h.reserved(first) and h.job(job)['state'] == 'RUNNING'
    time.sleep(6)  # Proves no other controller is recovering this supposedly idle lab.
    assert h.attempt(job) == before, 'Another controller is active; restart fixture is not isolated'
    h.start_controller()
    h.lost(job)
    h.finish(b, job, 2)
    print('PASS expired-before-reap start/renew/result rejection and automatic controller restart recovery', flush=True)

    hb = h.heartbeat(a)
    job = h.submit('running-expiry-authority', maximum=1)
    lab.claim(a)
    first = h.attempt(job)
    started = h.post(a, first, 'start', {'container_engine': h.actual}).json()
    assert h.post(a, first, 'start', {'container_engine': h.actual}).json() == started
    stop(hb)
    h.stop_controller()
    h.backdate(first)
    before = h.attempt(job)
    asyncio.run(expired_api())
    assert h.attempt(job) == before and h.reserved(first)
    h.start_controller()
    h.lost(job, state='FAILED')
    print('PASS expired RUNNING repeated-start/result/renew rejection before recovery (API fixture)', flush=True)

    hb = h.heartbeat(a)
    job = h.submit('completion-wins')
    lab.claim(a)
    first = h.attempt(job)
    assert h.post(a, first, 'start', {'container_engine': h.actual}).status_code == 200
    payload = SUCCESS | {'container_engine': h.actual}
    result = h.post(a, first, 'result', payload)
    assert result.status_code == 200
    h.backdate(first)  # Historical deadline only, terminal payload/timestamps unchanged.
    baseline = h.attempt(job)
    assert not recover_one(lab.engine, UUID(a['id']), UUID(first['id']))
    assert h.post(a, first, 'result', payload).json() == baseline
    assert h.post(a, first, 'renew').status_code == 409
    assert not h.reserved(first)
    print('PASS completion-before-recovery, identical terminal replay after historical expiry (API fixture)', flush=True)

    job = h.submit('competing-recovery', maximum=1)
    lab.claim(a)
    first = h.attempt(job)
    h.backdate(first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: recover_one(lab.engine, UUID(a['id']), UUID(first['id'])), range(2)))
    assert sum(results) <= 1  # The background process may also win.
    lost = h.lost(job, state='FAILED')
    assert h.post(a, first, 'result', FAILURE).status_code == 409
    assert h.attempt(job) == lost
    stop(hb)
    print('PASS competing recovery transactions and recovery-before-completion (DB/API fixture)', flush=True)

    # Connectivity outage: actual worker must stop locally on its monotonic
    # deadline and leave recovery to the controller, not report interruption.
    job = h.submit('connectivity-loss', seconds=55, maximum=1)
    p = h.spawn(a, execute=True)
    first = until(lambda: (x if (x := h.attempt(job)) and x['state'] == 'RUNNING' else None), 'Partition workload did not start')
    h.orphans.add(first['id'])
    h.stop_controller()
    assert p.wait(timeout=50) != 0
    assert h.attempt(job)['state'] == 'RUNNING' and h.reserved(first)
    assert h.api.get(f'/containers/meshcompute-{first["id"]}/json').status_code == 404
    h.orphans.remove(first['id'])
    h.start_controller()
    h.lost(job, state='FAILED')
    print('PASS connectivity loss: real container stopped, no stale terminal report, controller later releases allocation', flush=True)
    job = h.submit('renewal-rejected', seconds=40, maximum=1)
    p = h.spawn(a, execute=True)
    first = until(lambda: (x if (x := h.attempt(job)) and x['state'] == 'RUNNING' else None), 'Rejection workload did not start')
    h.orphans.add(first['id'])
    h.backdate(first)
    lost = h.lost(job, state='FAILED')
    assert p.wait(timeout=30) != 0
    assert h.attempt(job) == lost  # No WORKER_INTERRUPTED or stale completion.
    assert h.api.get(f'/containers/meshcompute-{first["id"]}/json').status_code == 404
    h.orphans.remove(first['id'])
    print('PASS explicit renewal rejection stops real workload without stale result', flush=True)
    clean(lab)
    print('Recovery validated; results retained. Provider controls have a separate suite; orphan reconciliation has a separate suite.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=['auto', 'podman', 'docker'], default=os.environ.get('MESHCOMPUTE_WORKER_CONTAINER_ENGINE', 'auto'))
    args = parser.parse_args()
    lab = h = None
    try:
        with ExitStack() as stack:
            for name in ('real-worker-test.lock', 'real-worker.lock'):
                if not acquire(stack.enter_context(lock_file(name))):
                    raise LabError('Stop helper agents/tests before test-recovery')
            lab = Lab(state_file=STATE_FILE)
            h = RecoveryLab(lab, args.engine)
            try:
                h.start_controller()
                run(h)
            finally:
                h.close()
    except (LabError, AssertionError) as exc:
        raise SystemExit(f'FAIL {exc}; results retained, inspect make dev-state') from None
    except (SQLAlchemyError, httpx.HTTPError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f'Recovery checks failed ({type(exc).__name__}); inspect local configuration/state; raw response omitted') from None
    except KeyboardInterrupt:
        raise SystemExit('Interrupted; owned children stopped; inspect state before rerunning') from None
    finally:
        if lab:
            lab.close()


if __name__ == '__main__':
    main()
