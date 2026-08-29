"""Bearer-token auth for the HTTP transport.

Two options, selected in `mcp_server.main`:

- **`StaticBearerMiddleware` (default).** A plain ASGI middleware that rejects any
  request without `Authorization: Bearer <MCP_AUTH_TOKEN>`. It answers a missing/bad
  token with `401` + a bare `WWW-Authenticate: Bearer` and nothing else -- no OAuth
  resource-metadata pointer -- so an MCP client can't be nudged into OAuth / dynamic
  client registration discovery (which this server does not implement). This is what
  n8n's "Bearer Token" credential expects.

- **`StaticTokenVerifier` (opt-in, `MCP_NATIVE_AUTH=1`).** Plugs the same shared
  secret into mcp's native `RequireAuthMiddleware`. That path advertises
  `.well-known/oauth-protected-resource`; kept only as a fallback.
"""
from __future__ import annotations

import hmac

from mcp.server.auth.provider import AccessToken, TokenVerifier
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_EXEMPT_PATHS = ("/healthz",)


class StaticBearerMiddleware:
    """Require `Authorization: Bearer <token>` on every HTTP request except `exempt_paths`."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        exempt_paths: tuple[str, ...] = DEFAULT_EXEMPT_PATHS,
    ) -> None:
        if not token:
            raise ValueError("StaticBearerMiddleware requires a non-empty token")
        self._app = app
        self._token = token
        self._exempt = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self._exempt:
            await self._app(scope, receive, send)
            return

        presented = ""
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                raw = value.decode("latin-1")
                if raw[:7].lower() == "bearer ":
                    presented = raw[7:].strip()
                break

        if not presented or not hmac.compare_digest(presented, self._token):
            response = PlainTextResponse(
                "Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


class StaticTokenVerifier(TokenVerifier):
    """mcp-native verifier for a single shared secret (used only with `MCP_NATIVE_AUTH=1`)."""

    def __init__(
        self,
        token: str,
        *,
        client_id: str = "knesset-agent",
        scopes: list[str] | None = None,
    ) -> None:
        if not token:
            raise ValueError("StaticTokenVerifier requires a non-empty token")
        self._token = token
        self._client_id = client_id
        self._scopes = scopes or []

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not hmac.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id=self._client_id,
            scopes=list(self._scopes),
            expires_at=None,
        )
