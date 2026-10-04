"""Bound attempt mutation bodies before JSON decoding, including chunked requests."""
from starlette.responses import JSONResponse


class AttemptBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'POST' or '/attempts/' not in scope['path']:
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            # Allows JSON escaping of both 64 KiB tails; bounded before decoding.
            if len(body) > 800 * 1024:
                return await JSONResponse({'detail': 'Attempt report too large'}, status_code=413)(scope, receive, send)
            if not message.get('more_body', False):
                break
        async def buffered_receive():
            return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
        await self.app(scope, buffered_receive, send)
