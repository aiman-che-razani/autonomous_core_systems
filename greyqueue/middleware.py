import asyncio
import json
import logging
import time
import uuid

from starlette.responses import JSONResponse

log = logging.getLogger("greyqueue.requests")


class BoundRequests:
    def __init__(self, app, require_tls=False):
        self.app, self.require_tls = app, require_tls

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        if self.require_tls and scope["scheme"] != "https" and scope["path"] != "/health":
            return await JSONResponse({"detail": "HTTPS required"}, 426)(scope, receive, send)
        messages, total = [], 0
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    total += len(message.get("body", b""))
                    if total > 131072:
                        return await JSONResponse({"detail": "Request too large"}, 413)(
                            scope, receive, send
                        )
                    messages.append(message)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await JSONResponse({"detail": "Request body timeout"}, 408)(scope, receive, send)

        async def replay():
            return messages.pop(0) if messages else await receive()

        async def traced(message):
            if message["type"] == "http.response.start":
                policy = b"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
                if scope["path"] in {"/docs", "/redoc"}:
                    policy = b"default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; frame-ancestors 'none'"
                message["headers"] += [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-store"),
                    (
                        b"content-security-policy",
                        policy,
                    ),
                ]
                log.info(
                    json.dumps(
                        {
                            "event": "http_request",
                            "request_id": request_id,
                            "method": scope["method"],
                            "path": scope["path"],
                            "status": message["status"],
                            "duration_ms": round((time.monotonic() - started) * 1000, 2),
                        }
                    )
                )
            await send(message)

        await self.app(scope, replay, traced)
