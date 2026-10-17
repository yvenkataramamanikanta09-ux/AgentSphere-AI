"""Transport and request-size controls before authentication body parsing."""

import ipaddress
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from agentsphere.security import check_origin


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        protected = request.url.path.startswith(("/api/v1/auth/", "/api/v1/workspaces"))
        if not protected:
            return await call_next(request)
        settings = request.app.state.settings
        origin = urlsplit(settings.app_origin)
        try:
            if request.url.netloc != origin.netloc or request.url.scheme != origin.scheme:
                raise HTTPException(403, "Use the configured application origin.")
            if settings.allow_insecure_local_http:
                try:
                    loopback = (
                        request.client and ipaddress.ip_address(request.client.host).is_loopback
                    )
                except ValueError:
                    loopback = False
                if not loopback:
                    raise HTTPException(403, "HTTP development access is loopback-only.")
            check_origin(request)
            if request.url.path.startswith("/api/v1/auth/") and request.method == "POST":
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 8192:
                        raise HTTPException(413, "Authentication request is too large.")
                # BaseHTTPMiddleware's cached Request forwards the body to FastAPI.
                request._body = bytes(body)
        except HTTPException as error:
            return JSONResponse(
                {"detail": error.detail},
                status_code=error.status_code,
                headers={"Cache-Control": "no-store"},
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
