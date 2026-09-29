"""Correlation id per HTTP request (task C-14).

Takes the caller's `X-Request-ID` (a proxy or client may set one) or generates one, binds it to every log line
of the request — uvicorn's access line included — and returns it in the response header. Pure ASGI, so the
bound context is the one the route handlers run in.
"""

import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cdms.logs import bind

HEADER = "x-request-id"
# Accept only short, printable ids from callers: the value ends up in logs and in the response header.
_VALID = re.compile(r"[A-Za-z0-9._:-]{1,64}")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        given = next((v.decode("latin-1") for k, v in scope["headers"] if k == HEADER.encode()), "")
        request_id = given if _VALID.fullmatch(given) else uuid.uuid4().hex
        # For the 500 handler, which Starlette runs outside this middleware (ServerErrorMiddleware).
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (HEADER.encode(), request_id.encode())]
            await send(message)

        with bind(request_id=request_id):
            await self.app(scope, receive, send_with_id)
