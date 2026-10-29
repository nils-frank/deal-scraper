"""The deal service over HTTP (`deals serve`): one Starlette app on one port.

- GET /api/find?q=&when=&stores=&limit=  the find_offers answer, with the German speech text
- GET /api/watchlist?when=               the watchlist_deals answer
- GET /api/status                        the deals://status answer
- /mcp                                   the MCP server (`mcp_server.py`)
- GET /healthz                           store reachability and the age of the last watchlist run

Every path except /healthz needs `Authorization: Bearer <DEAL_SERVICE_TOKEN>`.
The service is plain HTTP on the LAN; the token is the access control.
"""
from __future__ import annotations

import hmac
import logging
import sys

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from deals import mcp_server, query
from deals.service import ConfigError, DealService, InvalidRequest, ServiceError, Settings

log = logging.getLogger(__name__)

OPEN_PATHS = {"/healthz"}


class BearerAuth:
    """Answers 401 to every request without the service token, except OPEN_PATHS.
    Plain ASGI, so the MCP endpoint's streamed responses pass through untouched."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        await response(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        scheme, _, credentials = Headers(scope=scope).get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(credentials.strip().encode(), self.token)


def _answer(call, *args) -> JSONResponse:
    try:
        return JSONResponse(call(*args))
    except ServiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


def _limit(request: Request) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return query.DEFAULT_LIMIT
    try:
        return int(raw)
    except ValueError:
        raise InvalidRequest("limit must be a number") from None


def create_app(service: DealService) -> Starlette:
    mcp_app = mcp_server.build_server(service).streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # The SDK's Host/Origin check guards a localhost server against DNS
        # rebinding; this one serves the LAN under whatever name or address it
        # is reached by, and a rebinding page cannot send the bearer token.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    # Sync endpoints run in Starlette's thread pool: Marktguru and SQLite calls block.
    def find(request: Request) -> JSONResponse:
        params = request.query_params
        stores = [s.strip() for value in params.getlist("stores") for s in value.split(",") if s.strip()]
        return _answer(lambda: service.find_offers(params.get("q", ""), params.get("when", "now"), stores, _limit(request)))

    def watchlist(request: Request) -> JSONResponse:
        return _answer(service.watchlist_deals, request.query_params.get("when", "now"))

    def status(request: Request) -> JSONResponse:
        return _answer(service.status)

    def healthz(request: Request) -> JSONResponse:
        ok, body = service.health()
        return JSONResponse(body, status_code=200 if ok else 503)

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/api/find", find),
            Route("/api/watchlist", watchlist),
            Route("/api/status", status),
            *mcp_app.routes,
        ],
        middleware=[Middleware(BearerAuth, token=service.settings.token)],
        # Runs the MCP session manager, which serves /mcp.
        lifespan=lambda app: mcp_app.router.lifespan_context(mcp_app),
    )


def serve(host: str, port: int) -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"deals serve: {exc}", file=sys.stderr)
        return 2
    service = DealService(settings)
    service.migrate()
    log.info("Serving on %s:%d, store %s", host, port, settings.db_path)
    uvicorn.run(create_app(service), host=host, port=port, log_level="info")
    return 0
