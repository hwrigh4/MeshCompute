"""Shared local Docker-compatible API transport and fixed execution policy.

Engine errors never include response bodies. Attach output is drained in bounded
chunks, with engine logging disabled so noisy workloads cannot fill host log disks.
"""
import asyncio
from contextlib import suppress
from decimal import Decimal
from fractions import Fraction
import json
from urllib.parse import quote

import httpx

from common.schemas.attempts import LOG_LIMIT


class ExecutionError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class Tail:
    def __init__(self):
        self.data = bytearray()
        self.truncated = False

    def add(self, chunk):
        self.truncated |= len(self.data) + len(chunk) > LOG_LIMIT
        self.data.extend(chunk)
        if len(self.data) > LOG_LIMIT:
            del self.data[:-LOG_LIMIT]

    def text(self):
        # PostgreSQL text rejects NUL. Invalid UTF-8 is replaced, then the encoded
        # tail is bounded again (replacement characters can expand the byte size).
        text = self.data.decode('utf-8', errors='replace').replace('\x00', '\ufffd')
        encoded = text.encode('utf-8')
        self.truncated |= len(encoded) > LOG_LIMIT
        return encoded[-LOG_LIMIT:].decode('utf-8', errors='ignore')


class AttachedOutput:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.stdout, self.stderr = Tail(), Tail()
        self.task = asyncio.create_task(self.drain())

    async def drain(self):
        # Tty=false uses Docker's 8-byte multiplex header; never allocate a
        # frame-sized buffer based on an untrusted frame length.
        while True:
            try:
                header = await self.reader.readexactly(8)
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    raise ExecutionError('RESULT_CAPTURE_FAILED') from None
                return
            if header[0] not in (1, 2) or header[1:4] != b'\0\0\0':
                raise ExecutionError('RESULT_CAPTURE_FAILED')
            remaining = int.from_bytes(header[4:], 'big')
            tail = self.stdout if header[0] == 1 else self.stderr
            while remaining:
                chunk = await self.reader.readexactly(min(16384, remaining))
                tail.add(chunk)
                remaining -= len(chunk)

    async def finish(self):
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 10)
        except (TimeoutError, OSError, asyncio.IncompleteReadError):
            raise ExecutionError('RESULT_CAPTURE_FAILED') from None

    async def close(self):
        self.task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await self.task
        self.writer.close()
        with suppress(OSError, TimeoutError):
            await asyncio.wait_for(self.writer.wait_closed(), 2)


class CompatibleExecutor:
    engine: str

    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url='http://engine', timeout=10, trust_env=False,
        )
        self.container_id = None
        self.attempt_id = None
        self.job_id = None
        self.worker_id = None
        self.name = None
        self.create_attempted = False
        self.output = None
        self.started = False

    async def request(self, method, path, reason, **kwargs):
        try:
            response = await self.client.request(method, path, **kwargs)
            response.raise_for_status()
            return response
        except (httpx.HTTPError, OSError, ValueError):
            raise ExecutionError(reason) from None

    async def ensure_image(self, image):
        path = '/images/' + quote(image, safe='') + '/json'
        try:
            response = await self.client.get(path)
            if response.status_code == 404:
                # Stream pull progress with a bounded line buffer and total deadline.
                async with asyncio.timeout(120):
                    async with self.client.stream('POST', '/images/create', params={'fromImage': image}, timeout=30) as pull:
                        pull.raise_for_status()
                        pending = bytearray()
                        async for chunk in pull.aiter_bytes(chunk_size=4096):
                            pending.extend(chunk)
                            while b'\n' in pending:
                                line, _, rest = pending.partition(b'\n')
                                pending = bytearray(rest)
                                if line and any(json.loads(line).get(key) for key in ('error', 'errorDetail')):
                                    raise ExecutionError('IMAGE_PULL_FAILED')
                            if len(pending) > 65536:
                                raise ExecutionError('IMAGE_PULL_FAILED')
                        if pending and json.loads(pending).get('error'):
                            raise ExecutionError('IMAGE_PULL_FAILED')
                response = await self.client.get(path)
            response.raise_for_status()
            info = response.json()
            # Image-declared volumes otherwise create unbounded anonymous storage.
            if info.get('Config', {}).get('Volumes'):
                raise ExecutionError('SECURITY_POLICY_UNSUPPORTED')
            return info['Id']  # Pin this local image; don't race a retag before create.
        except (httpx.HTTPError, OSError, TimeoutError, ValueError, KeyError):
            raise ExecutionError('IMAGE_PULL_FAILED') from None

    async def create(self, assignment, image_id):
        if not assignment.command or not assignment.command[0]:
            raise ExecutionError('CONTAINER_CREATE_FAILED')
        self.attempt_id = str(assignment.attempt_id)
        self.job_id = str(assignment.job_id)
        self.name = 'meshcompute-' + self.attempt_id
        existing = await self.client.get(f'/containers/{self.name}/json')
        if existing.status_code != 404:
            # Never adopt/re-execute a pre-existing name, even with a matching label.
            raise ExecutionError('CONTAINER_CREATE_FAILED')
        quota = Decimal(str(assignment.resources.cpu)) * 100000
        if quota != quota.to_integral_value() or quota < 1000:
            raise ExecutionError('SECURITY_POLICY_UNSUPPORTED')
        memory = assignment.resources.memory_mb * 1024 * 1024
        config = {
            'Image': image_id, 'Entrypoint': assignment.command[:1], 'Cmd': assignment.command[1:],
            'User': '65532:65532', 'WorkingDir': '/mesh/work', 'Tty': False,
            'AttachStdout': True, 'AttachStderr': True, 'OpenStdin': False,
            'Healthcheck': {'Test': ['NONE']},
            'Labels': {'io.meshcompute.attempt': self.attempt_id, 'io.meshcompute.job': str(assignment.job_id)},
            'HostConfig': {
                'Privileged': False, 'NetworkMode': 'none', 'ReadonlyRootfs': True,
                'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges'],
                'PidsLimit': 64, 'CpuPeriod': 100000, 'CpuQuota': int(quota),
                'Memory': memory, 'MemorySwap': memory, 'ShmSize': 16 * 1024 * 1024,
                'IpcMode': 'private', 'RestartPolicy': {'Name': 'no'},
                'Tmpfs': {'/tmp': 'rw,nosuid,nodev,noexec,size=16m,mode=1777',
                          '/mesh/work': 'rw,nosuid,nodev,noexec,size=16m,mode=1777'},
                'LogConfig': {'Type': 'none'},
            },
        }
        if self.worker_id is not None:
            config['Labels']['io.meshcompute.worker'] = str(self.worker_id)
        self.create_attempted = True
        response = await self.request('POST', '/containers/create', 'CONTAINER_CREATE_FAILED', params={'name': self.name}, json=config)
        try:
            self.container_id = response.json()['Id']
            await self.verify_policy(assignment)
        except (KeyError, ValueError, TypeError):
            raise ExecutionError('SECURITY_POLICY_UNSUPPORTED') from None

    def owned(self, info):
        labels = info.get('Config', {}).get('Labels') or {}
        return (labels.get('io.meshcompute.attempt') == self.attempt_id
                and (self.job_id is None or labels.get('io.meshcompute.job') == self.job_id)
                and (self.worker_id is None or labels.get('io.meshcompute.worker') == str(self.worker_id)))

    async def inspect(self):
        response = await self.client.get(f'/containers/{self.container_id}/json')
        if response.status_code == 404 and self.started:
            raise ExecutionError('WORKLOAD_MISSING')
        if response.status_code != 200:
            raise ExecutionError('CONTAINER_START_FAILED')
        info = response.json()
        if not self.owned(info):
            raise ExecutionError('CONTAINER_CLEANUP_FAILED')
        return info

    async def capabilities_dropped(self, info):
        return 'ALL' in [x.upper() for x in info['HostConfig'].get('CapDrop', [])]

    async def verify_policy(self, assignment):
        info = await self.inspect()
        cfg, host = info['Config'], info['HostConfig']
        quota, period = host.get('CpuQuota', 0), host.get('CpuPeriod', 0)
        memory = assignment.resources.memory_mb * 1024 * 1024
        mounts = info.get('Mounts', [])
        valid = (
            cfg.get('User') == '65532:65532' and not cfg.get('Tty')
            and not host.get('Privileged') and host.get('NetworkMode') == 'none'
            and host.get('ReadonlyRootfs') and await self.capabilities_dropped(info)
            and any(x in ('no-new-privileges', 'no-new-privileges:true') for x in host.get('SecurityOpt', []))
            and host.get('PidsLimit') == 64 and quota > 0 and period > 0
            and Fraction(quota, period) == Fraction(str(assignment.resources.cpu))
            and host.get('Memory') == memory and host.get('MemorySwap') == memory
            and host.get('PidMode', '') not in ('host',) and host.get('IpcMode', '') != 'host'
            and not host.get('Binds') and not host.get('Devices')
            and all(m.get('Type') == 'tmpfs' and m.get('Destination') in ('/tmp', '/mesh/work') for m in mounts)
            and set(host.get('Tmpfs', {})) == {'/tmp', '/mesh/work'}
            and host.get('LogConfig', {}).get('Type') == 'none'
        )
        if not valid:
            raise ExecutionError('SECURITY_POLICY_UNSUPPORTED')

    async def logs(self):
        # Attach BEFORE start: even short-lived workloads cannot lose early output.
        # Hijacked HTTP upgrade is raw multiplexed data, not HTTP chunk framing.
        writer = None
        try:
            async with asyncio.timeout(10):
                reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=16384)
                path = f'/containers/{self.container_id}/attach?stream=1&stdout=1&stderr=1&logs=0'
                writer.write((f'POST {path} HTTP/1.1\r\nHost: engine\r\nConnection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 0\r\n\r\n').encode())
                await writer.drain()
                headers = await reader.readuntil(b'\r\n\r\n')
                if headers.split(b'\r\n', 1)[0].split()[1] != b'101':
                    raise ExecutionError('RESULT_CAPTURE_FAILED')
                self.output = AttachedOutput(reader, writer)
                return self.output
        except asyncio.CancelledError:
            if writer:
                writer.close()
            raise
        except (OSError, TimeoutError, ValueError, IndexError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ExecutionError):
            if writer:
                writer.close()
            raise ExecutionError('RESULT_CAPTURE_FAILED') from None

    async def start(self):
        await self.inspect()
        await self.request('POST', f'/containers/{self.container_id}/start', 'CONTAINER_START_FAILED')
        self.started = True

    async def wait(self):
        # Polling avoids unbounded /wait HTTP timeouts and allows local deadlines.
        while True:
            info = await self.inspect()
            state = info['State']
            if not state['Running']:
                if state.get('Error'):
                    raise ExecutionError('CONTAINER_START_FAILED')
                return int(state['ExitCode'])
            await asyncio.sleep(0.2)

    async def stop(self):
        await self.inspect()
        await self.request('POST', f'/containers/{self.container_id}/stop', 'CONTAINER_CLEANUP_FAILED', params={'t': 2})

    async def kill(self):
        await self.inspect()
        await self.request('POST', f'/containers/{self.container_id}/kill', 'CONTAINER_CLEANUP_FAILED')

    async def terminate(self):
        with suppress(ExecutionError):
            await self.stop()
        if (await self.inspect())['State']['Running']:
            await self.kill()
        async with asyncio.timeout(5):
            await self.wait()

    async def cleanup(self, *, remove_volumes=True):
        try:
            if self.create_attempted:
                # Also handles a create committed with its response lost. The UUID
                # name alone is never sufficient authority to remove a container.
                response = await self.client.get(f'/containers/{self.container_id or self.name}/json')
                if response.status_code != 404:
                    response.raise_for_status()
                    info = response.json()
                    if not self.owned(info):
                        raise ExecutionError('CONTAINER_CLEANUP_FAILED')
                    self.container_id = info['Id']
                    if info['State']['Running']:
                        # Podman's forced removal otherwise uses its default stop
                        # grace, which can exceed the HTTP deadline. Stop with our
                        # bounded grace first, retaining force removal as fallback.
                        with suppress(ExecutionError, TimeoutError):
                            await self.terminate()
                    if self.output:
                        with suppress(ExecutionError):
                            await self.output.finish()
                    await self.request('DELETE', f'/containers/{self.container_id}', 'CONTAINER_CLEANUP_FAILED', params={'force': 'true', 'v': str(remove_volumes).lower()})
        except (httpx.HTTPError, OSError, ValueError, KeyError):
            raise ExecutionError('CONTAINER_CLEANUP_FAILED') from None
        finally:
            if self.output:
                await self.output.close()
            await self.client.aclose()
