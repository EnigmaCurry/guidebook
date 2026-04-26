import ipaddress

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


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
                try:
                    addr = ipaddress.ip_address(client[0])
                except ValueError:
                    # Unrecognised address — don't trust proxy headers
                    return await self.app(scope, receive, send)
                if not any(addr in net for net in self.trusted_networks):
                    # Client is not a trusted proxy — skip header processing
                    return await self.app(scope, receive, send)
        return await super().__call__(scope, receive, send)
