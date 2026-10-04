"""Sequential Phase 5 functional checks using real APIs, PostgreSQL and engines.

No reset, service startup, image build, or automatic claim retry. Terminal results
remain available, and successful runs may be repeated without resetting the lab.
"""
import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from common.schemas.attempts import AttemptView
from controller.models.job_attempt import JobAttempt
from local_test.lab import Lab, LabError, REAL_STATE_FILE
from local_test.real_worker import acquire, identity, lock_file
from worker.config import WorkerSettings
from worker.executors.container import ContainerExecutor

SCENARIOS = {
    'success': ('success', [], 10, 128, 'SUCCEEDED', 0, 'meshcompute example: success'),
    'failure': ('failure', [], 10, 128, 'FAILED', 7, 'intentional failure'),
    'timeout': ('sleep', ['--seconds', '10'], 1, 128, 'TIMED_OUT', None, None),
    'cpu': ('cpu-burn', ['--seconds', '6'], 15, 128, 'SUCCEEDED', 0, None),
    'memory': ('memory-hold', ['--mib', '16', '--seconds', '3'], 10, 128, 'SUCCEEDED', 0, None),
    'memory-over': ('memory-hold', ['--mib', '128', '--seconds', '1'], 10, 64, 'FAILED', None, None),
    'monte-carlo': ('monte-carlo', [], 10, 128, 'SUCCEEDED', 0, '"inside": 78432'),
}


KERNEL_PROBE = """import json, os
from pathlib import Path
p = Path('/sys/fs/cgroup')
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert os.getuid() == 65532 and os.getgid() == 65532
assert 'MESHCOMPUTE_WORKER_TOKEN' not in os.environ
assert int(status['CapEff'].strip(), 16) == 0 and int(status['CapBnd'].strip(), 16) == 0
assert status['NoNewPrivs'].strip() == '1' and status['Seccomp'].strip() == '2'
quota, period = map(int, (p/'cpu.max').read_text().split())
assert quota / period == .5
assert (p/'memory.max').read_text().strip() == '134217728'
assert (p/'pids.max').read_text().strip() == '64'
assert (p/'memory.swap.max').read_text().strip() == '0'
for path in ('/tmp/probe', '/mesh/work/probe'):
    Path(path).write_text('temporary')
try:
    Path('/app/should-not-write').write_text('bad')
except OSError:
    pass
else:
    raise AssertionError('root filesystem writable')
assert set(os.listdir('/sys/class/net')) == {'lo'}
print('kernel/security probe PASS')
"""
COMMANDS = {
    'kernel': ['python', '-c', KERNEL_PROBE],
    'output': ['python', '-c', "import os; os.write(1,b'o'*200000+b'OUT_END'); os.write(2,b'e'*200000+b'ERR_END')"],
    'argv': ['python', '-c', "import sys; assert sys.argv[1:] == ['a b', '', '$HOME;echo unsafe']; print('argv PASS')", 'a b', '', '$HOME;echo unsafe'],
}
SCENARIOS.update({
    'kernel': ('success', [], 10, 128, 'SUCCEEDED', 0, 'kernel/security probe PASS'),
    'output': ('success', [], 10, 128, 'SUCCEEDED', 0, 'OUT_END'),
    'argv': ('success', [], 10, 128, 'SUCCEEDED', 0, 'argv PASS'),
})


def clean(lab):
    snapshot = lab.snapshot()
    if any(j['state'] in ('QUEUED', 'RUNNING') for j in snapshot['jobs']) or snapshot['allocations']:
        raise LabError('Active jobs/reservations exist; no test submitted. Inspect make dev-state. Use a separate lab or explicitly reset only disposable data after stopping agents.')


def run_scenarios(lab, selected, engine):
    with lock_file('real-worker-test.lock') as test_fd, lock_file('real-worker.lock') as agent_fd:
        if not acquire(test_fd) or not acquire(agent_fd):
            raise LabError('Stop dev-real-worker and other helper tests first; work-once maintains its own heartbeat')
        clean(lab)
        settings = WorkerSettings(id='00000000-0000-0000-0000-000000000000', token='probe',
                                  container_engine=engine, cpu_limit=1, memory_limit_mb=256)
        selector = ContainerExecutor(settings.podman_socket, settings.docker_socket, engine)
        backend = asyncio.run(selector.select())
        actual_engine, socket_path = backend.engine, backend.socket_path
        asyncio.run(backend.client.aclose())
        with httpx.Client(transport=httpx.HTTPTransport(uds=socket_path), base_url='http://engine', timeout=5, trust_env=False) as api:
            for name in {SCENARIOS[s][0] for s in selected}:
                response = api.get('/images/' + f'localhost/meshcompute-{name}:dev' + '/json')
                if response.status_code != 200:
                    raise LabError(f'Missing {name} image; run make examples-build-{actual_engine}. No test job submitted.')
            w = identity(lab, register=True)
            env = os.environ.copy()
            env.update(MESHCOMPUTE_WORKER_CONTROLLER_URL=lab.url, MESHCOMPUTE_WORKER_ID=w['id'],
                       MESHCOMPUTE_WORKER_TOKEN=w['token'], MESHCOMPUTE_WORKER_CPU_LIMIT='1',
                       MESHCOMPUTE_WORKER_MEMORY_LIMIT_MB='256', MESHCOMPUTE_WORKER_CONTAINER_ENGINE=engine)
            for scenario in selected:
                clean(lab)
                image, args, timeout, memory, state, exit_code, marker = SCENARIOS[scenario]
                job = lab.request('POST', '/v1/jobs', json={
                    'name': 'execution-' + scenario, 'runtime': 'container',
                    'image': f'localhost/meshcompute-{image}:dev', 'command': COMMANDS.get(scenario, ['python', '/app/main.py', *args]),
                    'resources': {'cpu': 0.5, 'memory_mb': memory}, 'timeout_seconds': timeout, 'max_attempts': 3,
                }).json()
                # Refuse a concurrent submitter before invoking the single-claim CLI.
                if any(j['id'] != job['id'] and j['state'] in ('QUEUED', 'RUNNING') for j in lab.snapshot()['jobs']):
                    raise LabError('Queue changed; no claim made. Stop other submitters and inspect state.')
                process = subprocess.Popen([sys.executable, '-m', 'worker.main', 'work-once'], env=env)
                inspected = False
                attempt = None
                try:
                    deadline = time.monotonic() + 60
                    while process.poll() is None:
                        if time.monotonic() > deadline:
                            raise LabError('Worker deadline exceeded; inspect persisted state before rerunning')
                        with Session(lab.engine) as session:
                            row = session.scalar(select(JobAttempt).where(JobAttempt.job_id == UUID(job['id'])))
                            if row:
                                attempt = AttemptView.model_validate(row)
                        if attempt and not inspected and scenario in ('cpu', 'memory'):
                            response = api.get(f'/containers/meshcompute-{attempt.id}/json')
                            if response.status_code == 200 and response.json()['State']['Running']:
                                info = response.json()
                                host = info['HostConfig']
                                assert host['CpuQuota'] / host['CpuPeriod'] == .5
                                assert host['Memory'] == memory * 1024 * 1024
                                assert host['MemorySwap'] == host['Memory'] and host['PidsLimit'] == 64
                                assert info['Config']['User'] == '65532:65532'
                                assert host['ReadonlyRootfs'] and host['NetworkMode'] == 'none'
                                assert not host['Privileged'] and not host.get('Binds')
                                assert all(m['Type'] == 'tmpfs' for m in info.get('Mounts', []))
                                assert 'no-new-privileges' in host['SecurityOpt'] or 'no-new-privileges:true' in host['SecurityOpt']
                                headers = {'Authorization': 'Bearer ' + w['token']}
                                route = f'/v1/workers/{w["id"]}/attempts/{attempt.id}/start'
                                first = lab.request('POST', route, headers=headers, json={'container_engine': actual_engine}).json()
                                second = lab.request('POST', route, headers=headers, json={'container_engine': actual_engine}).json()
                                assert first['started_at'] == second['started_at']
                                inspected = True
                        time.sleep(.05)
                    if process.returncode != 0:
                        raise LabError(f'{scenario}: worker failed; inspect job {job["id"]}. No retry.')
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                        try:
                            process.wait(timeout=45)
                        except subprocess.TimeoutExpired:
                            raise LabError('Worker cleanup remains unresolved; inspect its process and attempt') from None
                with Session(lab.engine) as session:
                    rows = list(session.scalars(select(JobAttempt).where(JobAttempt.job_id == UUID(job['id']))))
                    assert len(rows) == 1  # max_attempts=3 must not trigger retries.
                    attempt_id = rows[0].id
                result = lab.request('GET', f'/v1/attempts/{attempt_id}').json()
                final_job = lab.request('GET', f'/v1/jobs/{job["id"]}').json()
                assert result['state'] == state, result['failure_reason']
                assert result['container_engine'] == actual_engine
                assert final_job['state'] == ('SUCCEEDED' if state == 'SUCCEEDED' else 'FAILED')
                assert result['started_at'] and result['completed_at'] and final_job['completed_at']
                if scenario == 'cpu':
                    from datetime import datetime
                    worker = next(x for x in lab.snapshot()['workers'] if str(x['id']) == w['id'])
                    assert datetime.fromisoformat(worker['last_heartbeat']) > datetime.fromisoformat(result['started_at'])
                if exit_code is not None:
                    assert result['exit_code'] == exit_code
                if marker:
                    assert marker in result['stdout_tail'] + result['stderr_tail']
                if scenario == 'output':
                    assert result['stdout_truncated'] and result['stderr_truncated']
                    assert result['stdout_tail'].endswith('OUT_END') and result['stderr_tail'].endswith('ERR_END')
                    assert len(result['stdout_tail'].encode()) == len(result['stderr_tail'].encode()) == 65536
                report = {key: result[key] for key in ('state', 'container_engine', 'exit_code', 'failure_reason', 'stdout_tail', 'stderr_tail', 'stdout_truncated', 'stderr_truncated')}
                route = f'/v1/workers/{w["id"]}/attempts/{attempt_id}/result'
                headers = {'Authorization': 'Bearer ' + w['token']}
                repeated = lab.request('POST', route, headers=headers, json=report).json()
                assert repeated == result
                conflict = report | {'state': 'FAILED' if state != 'FAILED' else 'TIMED_OUT', 'failure_reason': 'NONZERO_EXIT' if state != 'FAILED' else 'TIMEOUT', 'exit_code': 7}
                assert lab.client.post(route, headers=headers, json=conflict).status_code == 409
                assert lab.client.post(route, json=report).status_code == 401
                assert not lab.snapshot()['allocations']
                assert api.get(f'/containers/meshcompute-{attempt_id}/json').status_code == 404
                if scenario in ('cpu', 'memory'):
                    assert inspected, 'No running-container inspection obtained'
                print(f'PASS {scenario}: {state}, engine={actual_engine}, attempt={attempt_id}; allocation/container removed', flush=True)
    print('Execution validated; results retained. No retry, lease expiry, or recovery implemented.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=['auto', 'podman', 'docker'], default=os.environ.get('MESHCOMPUTE_WORKER_CONTAINER_ENGINE', 'auto'))
    parser.add_argument('--scenario', choices=['all', *SCENARIOS], default='all')
    args = parser.parse_args()
    lab = None
    try:
        lab = Lab(state_file=REAL_STATE_FILE)
        lab.request('GET', '/health')
        run_scenarios(lab, list(SCENARIOS) if args.scenario == 'all' else [args.scenario], args.engine)
    except (LabError, AssertionError) as exc:
        raise SystemExit(f'FAIL {exc}; results preserved, inspect make dev-state') from None
    except (SQLAlchemyError, httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(f'Execution checks failed ({type(exc).__name__}); inspect configuration/state; raw responses omitted') from None
    except KeyboardInterrupt:
        raise SystemExit('Interrupted; inspect state before another claim') from None
    finally:
        if lab:
            lab.close()


if __name__ == '__main__':
    main()
