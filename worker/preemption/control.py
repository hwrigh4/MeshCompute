"""Worker-local provider intent and live-session status. No credentials or recovery."""
import asyncio
import logging
from contextlib import contextmanager, suppress
import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from common.logging import event


class ControlError(RuntimeError):
    pass


class ProviderState(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    participation: Literal['HEALTHY', 'PAUSED', 'DRAINING'] = 'HEALTHY'
    cpu: float = Field(ge=0)
    memory_mb: int = Field(ge=0)
    revision: int = Field(default=0, ge=0)
    stop_sequence: int = Field(default=0, ge=0)


class ControlStore:
    def __init__(self, settings):
        self.settings = settings
        self.directory = Path(settings.state_dir).expanduser() / str(settings.id)
        if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
            raise ControlError('Refusing symlinked provider state directory')
        for path in (self.directory.parent, self.directory):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                raise ControlError('Provider state directory must belong to this user with mode 0700')
        with self.lock():
            if (self.directory / 'provider.json').is_symlink():
                raise ControlError('Refusing symlinked provider state')
            if not (self.directory / 'provider.json').exists():
                self.write(ProviderState(cpu=settings.cpu_limit, memory_mb=settings.memory_limit_mb))

    def open(self, name, flags):
        fd = os.open(self.directory / name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise ControlError('Provider state files require owner-only access (0600)')
        return fd

    @contextmanager
    def lock(self):
        fd = self.open('provider.lock', os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def read(self):
        try:
            with os.fdopen(self.open('provider.json', os.O_RDONLY)) as source:
                value = source.read(4097)
            if len(value) > 4096:
                raise ValueError()
            return ProviderState.model_validate_json(value)
        except (OSError, ValueError):
            raise ControlError('Cannot read provider state; inspect permissions/configuration') from None

    def write(self, state):
        fd, name = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as target:
                target.write(state.model_dump_json())
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, self.directory / 'provider.json')
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(name).unlink(missing_ok=True)

    def update(self, action, cpu=None, memory=None):
        with self.lock():
            state = self.read()
            values = state.model_dump()
            if action in ('pause', 'resume', 'drain', 'stop-all'):
                values['participation'] = {'pause': 'PAUSED', 'resume': 'HEALTHY', 'drain': 'DRAINING', 'stop-all': 'PAUSED'}[action]
            if action == 'stop-all':
                values['stop_sequence'] += 1
            if action == 'resources':
                values.update(cpu=cpu, memory_mb=memory)
            values['revision'] += 1
            state = ProviderState.model_validate(values)
            self.write(state)
            return state


def same_user(writer):
    peer = writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    return struct.unpack('3i', peer)[1] == os.getuid()


def address(settings):
    return f'\0meshcompute-control-{os.getuid()}-{settings.id}'


class LiveAttempt:
    """In-memory status only: never rediscover/adopt orphan containers on restart."""
    def __init__(self, settings, assignment):
        self.settings, self.assignment = settings, assignment
        self.preempted = False
        self.cleaned = False
        self.cleanup_done = asyncio.Event()
        self.engine = None
        self.server = None
        self.handlers = set()

    async def __aenter__(self):
        self.server = await asyncio.start_unix_server(self.handle, path=address(self.settings), limit=1024)
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        self.cleanup_done.set()  # Wake waiters even on failure, with cleaned=False.
        if self.handlers:
            done, pending = await asyncio.wait(self.handlers, timeout=2)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            if not same_user(writer):
                return
            async with asyncio.timeout(35):
                request = await reader.readline()
                if request == b'wait-clean\n':
                    await self.cleanup_done.wait()
                elif request != b'status\n':
                    return
            writer.write(json.dumps({'attempt_id': str(self.assignment.attempt_id),
                                     'container': 'meshcompute-' + str(self.assignment.attempt_id),
                                     'engine': self.engine, 'local_cleanup_confirmed': self.cleaned}).encode() + b'\n')
            await asyncio.wait_for(writer.drain(), 1)
        except (OSError, TimeoutError, ValueError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            self.handlers.discard(task)

    async def watch(self, store):
        baseline = self.assignment._provider_stop_sequence
        if baseline is None:
            baseline = store.read().stop_sequence
        while True:
            if store.read().stop_sequence != baseline and not self.cleaned:
                self.preempted = True
                return
            await asyncio.sleep(.1)


async def live_status(settings):
    try:
        async with asyncio.timeout(2):
            reader, writer = await asyncio.open_unix_connection(address(settings), limit=4096)
            try:
                if not same_user(writer):
                    raise ControlError('Local execution socket belongs to another user')
                writer.write(b'status\n')
                await writer.drain()
                return json.loads(await reader.readline())
            finally:
                writer.close()
                await writer.wait_closed()
    except (ConnectionRefusedError, FileNotFoundError):
        return None


async def command(settings, action, cpu=None, memory=None):
    store = ControlStore(settings)
    connection = None
    if action == 'stop-all':
        try:
            async with asyncio.timeout(2):
                connection = await asyncio.open_unix_connection(address(settings), limit=4096)
        except (ConnectionRefusedError, FileNotFoundError):
            pass
    try:
        state = store.read() if action == 'status' else store.update(action, cpu, memory)
        if action == 'stop-all' and connection:
            reader, writer = connection
            if not same_user(writer):
                raise ControlError('Local execution socket belongs to another user')
            writer.write(b'wait-clean\n')
            await writer.drain()
            async with asyncio.timeout(35):
                reply = json.loads(await reader.readline())
                if not reply.get('local_cleanup_confirmed'):
                    raise ControlError('Local cleanup was not confirmed')
    finally:
        if connection:
            connection[1].close()
            with suppress(OSError):
                await connection[1].wait_closed()
    if action != 'status':
        event(logging.getLogger(__name__), 'provider.control_applied', worker_id=settings.id, action=action, state=state.participation)
    if action == 'stop-all':
        from worker.agent.reconciliation import reclaim_local
        await reclaim_local(settings)
        event(logging.getLogger(__name__), 'provider.local_reclaim_confirmed', worker_id=settings.id, action=action)
        print('Participation PAUSED; live local attempt cleaned or no live execution session. '
              'Explicitly worker-labeled containers reclaimed; controller release is separate.')
    elif action == 'status':
        reachable = False
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False, follow_redirects=False) as client:
                reachable = (await client.get(str(settings.controller_url).rstrip('/') + '/health')).status_code == 200
        except httpx.HTTPError:
            pass
        print(json.dumps({'worker_id': str(settings.id), **state.model_dump(),
                          'configured_engine': settings.container_engine, 'controller_reachable': reachable,
                          'active_local_session': await live_status(settings)}, indent=2))
    else:
        print(f'Local {state.participation}: {state.cpu} CPU / {state.memory_mb} MiB. '
              'Live heartbeat will advertise changes; active work is unchanged.')
