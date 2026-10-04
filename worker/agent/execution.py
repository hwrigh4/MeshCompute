"""One assignment, local execution, and bounded idempotent reporting."""
import asyncio
from contextlib import suppress
import logging
import os
import socket

import httpx

from common.schemas.attempts import AttemptResult, AttemptStart, AttemptView
from common.schemas.states import AttemptState
from worker.agent.claim import claim_once
from worker.agent.loop import run_agent
from worker.executors.compatible import ExecutionError

logger = logging.getLogger(__name__)


class ReportingFailed(RuntimeError):
    pass


async def report(settings, assignment, action, payload):
    """Only idempotent mutations may retry; never claims or container starts."""
    async with httpx.AsyncClient(
        base_url=str(settings.controller_url).rstrip('/') + '/',
        headers={'Authorization': f'Bearer {settings.token.get_secret_value()}'},
        timeout=5, trust_env=False, follow_redirects=False,
    ) as client:
        for retry in range(3):
            try:
                response = await client.post(
                    f'v1/workers/{settings.id}/attempts/{assignment.attempt_id}/{action}',
                    json=payload.model_dump(mode='json'),
                )
                if response.status_code not in (408, 429) and response.status_code < 500:
                    if response.status_code != 200:
                        raise ReportingFailed(f'{action} rejected (HTTP {response.status_code}); inspect attempt {assignment.attempt_id}')
                    view = AttemptView.model_validate(response.json())
                    if view.id != assignment.attempt_id or view.job_id != assignment.job_id or view.worker_id != settings.id:
                        raise ValueError('Mismatched response')
                    expected = {'state': AttemptState.RUNNING, **payload.model_dump()} if action == 'start' else payload.model_dump()
                    if not all(getattr(view, key) == value for key, value in expected.items()):
                        raise ValueError('Mismatched response')
                    return view
            except (httpx.RequestError, ValueError):
                pass
            if retry < 2:
                await asyncio.sleep(retry + 1)
    raise ReportingFailed(f'{action} outcome unresolved for attempt {assignment.attempt_id}; inspect controller state. No new claim or assumed release.')


async def execute_assignment(settings, executor, assignment):
    backend = None
    output = None
    result = AttemptResult(state=AttemptState.FAILED, failure_reason='ENGINE_UNAVAILABLE')
    interrupted = False
    try:
        backend = await executor.select()
        result.container_engine = backend.engine
        image = await backend.ensure_image(assignment.image)
        # Create (without launching) allows policy validation before start reporting.
        await backend.create(assignment, image)
        output = await backend.logs()
        await report(settings, assignment, 'start', AttemptStart(container_engine=backend.engine))
        try:
            async with asyncio.timeout(assignment.timeout_seconds):
                await backend.start()
                result.exit_code = await backend.wait()
            result.state = AttemptState.SUCCEEDED if result.exit_code == 0 else AttemptState.FAILED
            result.failure_reason = None if result.exit_code == 0 else 'NONZERO_EXIT'
        except TimeoutError:
            await backend.terminate()
            result.exit_code = (await backend.inspect())['State']['ExitCode']
            result.state = AttemptState.TIMED_OUT
            result.failure_reason = 'TIMEOUT'
        await output.finish()
    except asyncio.CancelledError:
        interrupted = True
        result.state, result.failure_reason = AttemptState.FAILED, 'WORKER_INTERRUPTED'
    except ExecutionError as exc:
        result.state, result.failure_reason = AttemptState.FAILED, exc.reason
    except (httpx.HTTPError, OSError, KeyError, ValueError, TypeError, TimeoutError):
        result.state, result.failure_reason = AttemptState.FAILED, 'CONTAINER_START_FAILED'
    finally:
        # Also removes partially created containers when start reporting fails.
        # Never report resource release if we cannot confirm local cleanup.
        if backend:
            try:
                await backend.cleanup()
            except (ExecutionError, httpx.HTTPError, OSError, TimeoutError):
                raise ReportingFailed(f'Cleanup unresolved for attempt {assignment.attempt_id}; inspect its labeled container and controller reservation') from None
    if output:
        result.stdout_tail = output.stdout.text()
        result.stderr_tail = output.stderr.text()
        result.stdout_truncated = output.stdout.truncated
        result.stderr_truncated = output.stderr.truncated
    view = await report(settings, assignment, 'result', result)
    logger.info('Attempt %s: %s (%s); controller recorded result and released allocation', view.id, view.state, view.failure_reason or 'exit 0')
    if interrupted:
        raise asyncio.CancelledError
    return view


async def work_once(settings, executor):
    # Linux abstract socket is an atomic process-lifetime lock, no credential or
    # runtime files. A second executor for this identity on this host must stop.
    lock = socket.socket(socket.AF_UNIX)
    try:
        try:
            lock.bind(f'\0meshcompute-work-{os.getuid()}-{settings.id}')
        except OSError:
            raise ReportingFailed('Another work-once process owns this local worker identity') from None
        ready = asyncio.Event()
        heartbeat = asyncio.create_task(run_agent(settings, executor, ready=ready))
        async def work():
            try:
                await asyncio.wait_for(ready.wait(), 30)
            except TimeoutError:
                raise ReportingFailed('No confirmed heartbeat within 30 seconds; no claim made') from None
            assignment = await claim_once(settings)
            if assignment is None:
                logger.info('No work (204)')
                return None
            logger.info('Claimed job %s / attempt %s', assignment.job_id, assignment.attempt_id)
            return await execute_assignment(settings, executor, assignment)
        task = asyncio.create_task(work())
        try:
            done, _ = await asyncio.wait((task, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                await heartbeat
            return await task
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
    finally:
        lock.close()
