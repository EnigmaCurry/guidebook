import ipaddress

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

_PROXY_HEADERS = frozenset(
    {
        b"x-forwarded-for",
        b"x-forwarded-proto",
        b"x-forwarded-host",
        b"x-forwarded-port",
        b"x-real-ip",
        b"forwarded",
    }
)


class TrustedProxyMiddleware(ProxyHeadersMiddleware):
    """Extends uvicorn's ProxyHeadersMiddleware with CIDR-based trust."""

    def __init__(self, app, trusted_networks):
        # Pass trusted_hosts="*" so the parent always processes headers;
        # we override the trust check ourselves.
        super().__init__(app, trusted_hosts="*")
        self.trusted_networks = [
            ipaddress.ip_network(n, strict=False)
            if not isinstance(n, (ipaddress.IPv4Network, ipaddress.IPv6Network))
            else n
            for n in trusted_networks
        ]

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            if client:
                trusted = False
                try:
                    addr = ipaddress.ip_address(client[0])
                    trusted = any(addr in net for net in self.trusted_networks)
                except ValueError:
                    pass
                if not trusted:
                    # Reject untrusted clients that send proxy headers
                    headers = scope.get("headers", [])
                    if any(name in _PROXY_HEADERS for name, _ in headers):
                        from starlette.responses import Response

                        response = Response(
                            "Proxy headers not allowed from untrusted source",
                            status_code=400,
                        )
                        return await response(scope, receive, send)
                    return await self.app(scope, receive, send)
        return await super().__call__(scope, receive, send)
