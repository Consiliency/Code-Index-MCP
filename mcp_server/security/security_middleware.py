"""FastAPI security middleware for authentication and authorization."""

import logging
import os
import re
from ipaddress import ip_address, ip_network
from typing import Awaitable, Callable, Dict, List, Literal, Optional, cast
from urllib.parse import unquote

from fastapi import Depends, FastAPI, HTTPException, Request, Response, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from starlette.routing import Match

from .auth_manager import (
    AuthManager,
)
from .models import (
    AccessRequest,
    Permission,
    SecurityConfig,
    TokenData,
    User,
    UserRole,
)

logger = logging.getLogger(__name__)
_bearer_scheme = HTTPBearer(auto_error=False)


def _client_ip(request: Request) -> str:
    """Use forwarded identity only through an explicitly trusted proxy chain."""
    peer = str(request.client.host) if request.client else "unknown"
    try:
        networks = [
            ip_network(value.strip())
            for value in os.getenv("MCP_TRUSTED_PROXIES", "").split(",")
            if value.strip()
        ]
        address = ip_address(peer)
        if not any(address in network for network in networks):
            return peer
        header = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
        if not header:
            return peer
        chain = [ip_address(value.strip()) for value in header.split(",")]
        for candidate in reversed(chain):
            if not any(address in network for network in networks):
                break
            address = candidate
        return str(address)
    except ValueError:
        return peer


class SecurityHeaders:
    """Security headers configuration."""

    @staticmethod
    def get_default_headers() -> Dict[str, str]:
        """Get default security headers."""
        return {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "X-XSS-Protection": "1; mode=block",
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "X-Permitted-Cross-Domain-Policies": "none",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Content-Security-Policy": "default-src 'self'",
            "X-Download-Options": "noopen",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limiting middleware."""

    def __init__(self, app: FastAPI, auth_manager: AuthManager):
        super().__init__(app)
        self.auth_manager = auth_manager

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Process request with rate limiting."""
        auth_manager = getattr(request.app.state, "auth_manager", None) or self.auth_manager
        if auth_manager is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "Authentication unavailable"},
            )
        client_ip = self._get_client_ip(request)

        # Check rate limiting
        if auth_manager.rate_limiter.is_rate_limited(client_ip):
            await auth_manager._log_security_event(
                "rate_limit_exceeded",
                ip_address=client_ip,
                user_agent=request.headers.get("user-agent"),
                severity="warning",
            )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded. Please try again later."},
            )

        response = await call_next(request)
        return response

    def _get_client_ip(self, request: Request) -> str:
        return _client_ip(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Security headers middleware."""

    def __init__(self, app: FastAPI, security_headers: Optional[Dict[str, str]] = None):
        super().__init__(app)
        self.security_headers = security_headers or SecurityHeaders.get_default_headers()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Add security headers to response."""
        response = await call_next(request)

        # Add security headers
        for header, value in self.security_headers.items():
            response.headers[header] = value

        return response


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Authentication middleware."""

    def __init__(
        self,
        app: FastAPI,
        auth_manager: Optional[AuthManager] = None,
        excluded_paths: Optional[List[str]] = None,
    ):
        super().__init__(app)
        self.auth_manager = auth_manager
        self.excluded_paths = excluded_paths or [
            "/docs",
            "/redoc",
            "/openapi.json",
            "/health",
            "/ready",
            "/liveness",
            "/api/v1/auth/login",
            "/api/v1/auth/register",
            "/api/v1/auth/refresh",
        ]

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Process request with authentication."""
        # Skip authentication for excluded paths
        if self._is_excluded_path(request.url.path):
            return await call_next(request)

        auth_manager = getattr(request.app.state, "auth_manager", None) or self.auth_manager
        if auth_manager is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "Authentication unavailable"},
            )

        # Extract and verify token
        token = self._extract_token(request)
        if not token:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Missing authentication token"},
            )

        token_data = await auth_manager.verify_token(token)
        if not token_data:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid or expired token"},
            )

        # Add user info to request state
        request.state.user_id = token_data.user_id
        request.state.username = token_data.username
        request.state.user_role = token_data.role
        request.state.user_permissions = token_data.permissions
        request.state.token_data = token_data

        # Log authentication
        await auth_manager._log_security_event(
            "request_authenticated",
            user_id=token_data.user_id,
            username=token_data.username,
            ip_address=self._get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )

        return await call_next(request)

    def _is_excluded_path(self, path: str) -> bool:
        """Check if path is excluded from authentication."""
        return any(
            path == excluded or path.startswith(excluded.rstrip("/") + "/")
            for excluded in self.excluded_paths
        )

    def _extract_token(self, request: Request) -> Optional[str]:
        """Extract Bearer token from Authorization header."""
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return None

        if not auth_header.startswith("Bearer "):
            return None

        return str(auth_header[7:])  # Remove "Bearer " prefix

    def _get_client_ip(self, request: Request) -> str:
        return _client_ip(request)


class AuthorizationMiddleware(BaseHTTPMiddleware):
    """Authorization middleware."""

    def __init__(self, app: FastAPI, auth_manager: Optional[AuthManager] = None):
        super().__init__(app)
        self.auth_manager = auth_manager

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Process request with authorization."""
        # Skip authorization if not authenticated
        if not hasattr(request.state, "user_id"):
            return await call_next(request)

        # Let the router preserve its 404/405 contract. A real route without an
        # access rule still reaches the default-deny branch below.
        if not any(route.matches(request.scope)[0] == Match.FULL for route in request.app.routes):
            return await call_next(request)

        auth_manager = getattr(request.app.state, "auth_manager", None) or self.auth_manager
        if auth_manager is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "Authentication unavailable"},
            )

        # Determine operation from HTTP method
        operation = self._get_operation_from_method(request.method)

        # Create access request
        access_request = AccessRequest(
            user_id=request.state.user_id,
            path=request.url.path,
            operation=operation,
            metadata={
                "method": request.method,
                "query_params": dict(request.query_params),
                "ip_address": self._get_client_ip(request),
                "user_agent": request.headers.get("user-agent"),
            },
        )

        # Check authorization
        is_authorized = await auth_manager.authorize_request(access_request)
        if not is_authorized:
            await auth_manager._log_security_event(
                "authorization_denied",
                user_id=request.state.user_id,
                username=request.state.username,
                ip_address=self._get_client_ip(request),
                details={
                    "path": request.url.path,
                    "method": request.method,
                    "operation": operation.value,
                },
                severity="warning",
            )
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Insufficient permissions"},
            )

        # Log successful authorization
        await auth_manager._log_security_event(
            "authorization_granted",
            user_id=request.state.user_id,
            username=request.state.username,
            ip_address=self._get_client_ip(request),
            details={
                "path": request.url.path,
                "method": request.method,
                "operation": operation.value,
            },
        )

        return await call_next(request)

    def _get_operation_from_method(self, method: str) -> Permission:
        """Map HTTP method to permission."""
        method_mapping = {
            "GET": Permission.READ,
            "HEAD": Permission.READ,
            "OPTIONS": Permission.READ,
            "POST": Permission.WRITE,
            "PUT": Permission.WRITE,
            "PATCH": Permission.WRITE,
            "DELETE": Permission.DELETE,
        }
        return method_mapping.get(method.upper(), Permission.READ)

    def _get_client_ip(self, request: Request) -> str:
        return _client_ip(request)


class RequestValidationMiddleware(BaseHTTPMiddleware):
    """Request validation and sanitization middleware."""

    def __init__(self, app: FastAPI, max_request_size: int = 10 * 1024 * 1024):  # 10MB
        super().__init__(app)
        self.max_request_size = max_request_size

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Validate and sanitize incoming requests."""
        # Check request size
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.max_request_size:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"detail": "Request too large"},
            )

        # Validate content type for POST/PUT requests
        if request.method in ["POST", "PUT", "PATCH"]:
            content_type = request.headers.get("content-type", "")
            if content_type and not self._is_valid_content_type(content_type):
                return JSONResponse(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    content={"detail": "Unsupported media type"},
                )

        # Check for suspicious patterns in URL path and query string (decode URL encoding)
        raw_path = request.scope.get("path", request.url.path)
        query = unquote(str(request.url.query)) if request.url.query else ""
        full_url_check = raw_path + "?" + query if query else raw_path
        if self._has_suspicious_patterns(full_url_check):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "Invalid request format"},
            )

        return await call_next(request)

    def _is_valid_content_type(self, content_type: str) -> bool:
        """Check if content type is allowed."""
        allowed_types = [
            "application/json",
            "application/x-www-form-urlencoded",
            "multipart/form-data",
            "text/plain",
        ]
        return any(content_type.startswith(allowed) for allowed in allowed_types)

    def _has_suspicious_patterns(self, path: str) -> bool:
        """Check for suspicious patterns in URL path."""
        suspicious_patterns = [
            "../",
            "..\\",
            "..",
            "%2e%2e",
            "%2f",
            "%5c",
            "<script",
            "</script>",
            "javascript:",
            "vbscript:",
            "onload=",
            "onerror=",
            "onclick=",
            "onmouseover=",
        ]
        path_lower = path.lower()
        return any(pattern in path_lower for pattern in suspicious_patterns)


# Dependency functions for FastAPI


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> TokenData:
    """Get current user from request state."""
    if hasattr(request.state, "token_data"):
        return cast(TokenData, request.state.token_data)

    auth_manager = cast(Optional[AuthManager], getattr(request.app.state, "auth_manager", None))
    if auth_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication unavailable",
        )

    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    token_data = await auth_manager.verify_token(credentials.credentials)
    if token_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    request.state.user_id = token_data.user_id
    request.state.username = token_data.username
    request.state.user_role = token_data.role
    request.state.user_permissions = token_data.permissions
    request.state.token_data = token_data
    return token_data


async def get_current_active_user(
    request: Request,
    token_data: TokenData = Security(get_current_user),
) -> User:
    """Get current active user."""
    user = User(
        id=token_data.user_id,
        username=token_data.username,
        hashed_password="",  # Not needed for verification
        role=token_data.role,
        permissions=token_data.permissions,
    )
    return user


def require_permission(permission: Permission) -> Callable[..., Awaitable[User]]:
    """Decorator to require specific permission."""

    async def permission_checker(
        current_user: User = Security(get_current_active_user),
    ) -> User:
        if permission not in current_user.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission.value}' required",
            )
        return current_user

    return permission_checker


def require_role(role: UserRole) -> Callable[..., Awaitable[User]]:
    """Decorator to require specific role or higher."""

    async def role_checker(current_user: User = Security(get_current_active_user)) -> User:
        role_hierarchy = [
            UserRole.GUEST,
            UserRole.READONLY,
            UserRole.USER,
            UserRole.ADMIN,
        ]

        try:
            user_level = role_hierarchy.index(current_user.role)
            required_level = role_hierarchy.index(role)
            if user_level < required_level:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Role '{role.value}' or higher required",
                )
        except ValueError:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid role")

        return current_user

    return role_checker


_SCOPE_TO_PERMISSION: Dict[str, Permission] = {
    "metrics": Permission.ADMIN,
    "admin": Permission.ADMIN,
    "tools": Permission.READ,
}


def require_auth(
    scope: Literal["metrics", "admin", "tools"],
) -> Callable[..., Awaitable[User]]:
    """FastAPI dep that delegates to require_permission after scope→permission mapping."""
    permission = _SCOPE_TO_PERMISSION[scope]
    return require_permission(permission)


_REDACTION_PATTERNS = [
    (re.compile(r"(Bearer\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(JWT_SECRET_KEY=)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(GITHUB_TOKEN=)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(MCP_CLIENT_SECRET=)\S+"), r"\1[REDACTED]"),
]


class SecretRedactionResponseMiddleware(BaseHTTPMiddleware):
    """Redact secrets from 4xx/5xx response bodies. Streaming bodies pass through."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        if not (400 <= response.status_code < 600):
            return response

        # Accumulate body; streaming bodies are passed through on failure
        chunks: list[bytes] = []
        try:
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        except Exception:
            return response

        body = b"".join(chunks).decode("utf-8", errors="replace")
        for pattern, replacement in _REDACTION_PATTERNS:
            body = pattern.sub(replacement, body)

        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )


class SecurityMiddlewareStack:
    """Complete security middleware stack for FastAPI applications."""

    def __init__(self, app: FastAPI, config: SecurityConfig, auth_manager: AuthManager):
        self.app = app
        self.config = config
        self.auth_manager = auth_manager

    def setup_middleware(self):
        """Set up all security middleware in the correct order."""
        # Add CORS middleware first
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=self.config.cors_origins,
            allow_credentials=True,
            allow_methods=self.config.cors_methods,
            allow_headers=self.config.cors_headers,
        )

        # Add security headers middleware
        self.app.add_middleware(
            SecurityHeadersMiddleware, security_headers=self.config.security_headers
        )

        # Add request validation middleware
        self.app.add_middleware(RequestValidationMiddleware)

        # Note: SecretRedactionResponseMiddleware is registered at module-level
        # in gateway.py so it applies in subprocess/uvicorn boot paths where
        # late middleware registration from the startup event is rejected by
        # FastAPI (see gateway.py "Skipping late middleware registration" log).

        # Add rate limiting middleware
        self.app.add_middleware(RateLimitMiddleware, auth_manager=self.auth_manager)

        # Add authorization middleware
        self.app.add_middleware(AuthorizationMiddleware, auth_manager=self.auth_manager)

        # Add authentication middleware last (executes first)
        self.app.add_middleware(AuthenticationMiddleware, auth_manager=self.auth_manager)

    def add_security_routes(self):
        """Add security-related routes."""

        @self.app.post("/api/v1/auth/login")
        async def login(credentials: dict):
            """Login endpoint."""
            # This would be implemented in the gateway

        @self.app.post("/api/v1/auth/logout")
        async def logout(current_user: User = Depends(get_current_active_user)):
            """Logout endpoint."""
            # This would be implemented in the gateway

        @self.app.post("/api/v1/auth/refresh")
        async def refresh_token(refresh_token: str):
            """Refresh token endpoint."""
            # This would be implemented in the gateway

        @self.app.get("/api/v1/auth/me")
        async def get_current_user_info(
            current_user: User = Depends(get_current_active_user),
        ):
            """Get current user information."""
            return {
                "id": current_user.id,
                "username": current_user.username,
                "role": current_user.role,
                "permissions": current_user.permissions,
            }

        @self.app.get("/api/v1/security/events")
        async def get_security_events(
            limit: int = 100, current_user: User = Depends(require_role(UserRole.ADMIN))
        ):
            """Get security events (admin only)."""
            events = await self.auth_manager.get_security_events(limit)
            return {"events": events}
