"""Small allowlisted event records; no request bodies, credentials or output."""
from datetime import datetime, timezone
import json
import logging
import os

FIELDS = {'worker_id', 'job_id', 'attempt_id', 'attempt_number', 'engine', 'state',
          'failure_reason', 'outcome', 'action', 'duration_seconds', 'error_type', 'count'}


class EventFormatter(logging.Formatter):
    def format(self, record):
        data = {'timestamp': datetime.now(timezone.utc).isoformat(), 'level': record.levelname,
                'component': record.name.split('.')[0], 'event': record.getMessage()}
        data.update({key: str(getattr(record, key)) for key in sorted(FIELDS) if hasattr(record, key)})
        if os.environ.get('MESHCOMPUTE_LOG_FORMAT', 'console') == 'json':
            return json.dumps(data, ensure_ascii=True)
        context = ' '.join(f'{k}={json.dumps(v)}' for k, v in data.items()
                           if k not in ('timestamp', 'level', 'event'))
        return f'{data["level"]} {data["event"]} {context}'


def configure_logging():
    level = logging.DEBUG if os.environ.get('MESHCOMPUTE_LOG_LEVEL') == 'DEBUG' else logging.INFO
    for name in ('controller', 'worker'):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = False
        if not any(getattr(h, '_meshcompute', False) for h in logger.handlers):
            handler = logging.StreamHandler()
            handler._meshcompute = True
            handler.setFormatter(EventFormatter())
            logger.addHandler(handler)
    # Uvicorn access logs include raw paths/query strings. Lifecycle events below
    # are sufficient for this phase; never enable raw request logging by default.
    logging.getLogger('uvicorn.access').disabled = True
    logging.getLogger('httpx').setLevel(logging.WARNING)


def event(logger, name, level=logging.INFO, **fields):
    logger.log(level, name, extra={k: v for k, v in fields.items() if k in FIELDS and v is not None})


def attempt_context(attempt):
    return dict(worker_id=attempt.worker_id, job_id=attempt.job_id, attempt_id=attempt.id,
                attempt_number=attempt.attempt_number, engine=attempt.container_engine,
                state=attempt.state, failure_reason=attempt.failure_reason)
