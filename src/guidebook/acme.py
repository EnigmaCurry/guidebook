"""ACME protocol client for Let's Encrypt DNS-01 challenges via acme-dns.

Implements RFC 8555 (ACME) with DNS-01 validation using acme-dns as the
DNS provider.  No external ACME libraries required — uses ``cryptography``
for JWS/CSR and ``httpx`` for HTTP.
"""

import base64
import datetime
import hashlib
import json
import logging

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger("guidebook")

# Well-known ACME directory URLs
LE_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"

DEFAULT_ACME_DNS_SERVER = "https://auth.acme-dns.io"

# Timeout for HTTP requests (seconds)
_HTTP_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    """Base64url-encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwk_thumbprint(pub: rsa.RSAPublicKey) -> str:
    """Compute JWK thumbprint (RFC 7638) of an RSA public key."""
    nums = pub.public_numbers()
    jwk_obj = {
        "e": _b64url(nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big")),
        "kty": "RSA",
        "n": _b64url(nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")),
    }
    # Keys MUST be sorted for canonical JSON
    canonical = json.dumps(jwk_obj, sort_keys=True, separators=(",", ":"))
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def _jwk_public(pub: rsa.RSAPublicKey) -> dict:
    """Return the JWK dict for an RSA public key."""
    nums = pub.public_numbers()
    return {
        "e": _b64url(nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big")),
        "kty": "RSA",
        "n": _b64url(nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")),
    }


def _load_account_key(pem: str) -> rsa.RSAPrivateKey:
    return serialization.load_pem_private_key(pem.encode(), password=None)


def generate_account_key() -> str:
    """Generate a new RSA-2048 account key, returned as PEM string."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


# ---------------------------------------------------------------------------
# JWS signing (RFC 7515 — flattened JSON serialization for ACME)
# ---------------------------------------------------------------------------


def _sign_jws(
    url: str,
    payload: dict | str | None,
    account_key: rsa.RSAPrivateKey,
    nonce: str,
    *,
    kid: str | None = None,
) -> dict:
    """Build a JWS body for an ACME request.

    If *kid* is provided the protected header uses ``kid``; otherwise it
    embeds the full JWK (used only for newAccount).

    *payload* may be a dict (JSON-serialised), the empty string ``""`` for
    POST-as-GET, or ``None`` which is treated the same as ``""``.
    """
    protected: dict = {"alg": "RS256", "nonce": nonce, "url": url}
    if kid:
        protected["kid"] = kid
    else:
        protected["jwk"] = _jwk_public(account_key.public_key())

    protected_b64 = _b64url(json.dumps(protected).encode())

    if payload is None or payload == "":
        payload_b64 = ""
    else:
        payload_b64 = _b64url(json.dumps(payload).encode())

    sig_input = f"{protected_b64}.{payload_b64}".encode()
    signature = account_key.sign(sig_input, padding.PKCS1v15(), hashes.SHA256())

    return {
        "protected": protected_b64,
        "payload": payload_b64,
        "signature": _b64url(signature),
    }


# ---------------------------------------------------------------------------
# ACME directory & nonce management
# ---------------------------------------------------------------------------

_directory_cache: dict | None = None
_nonce: str | None = None


async def acme_directory(endpoint: str) -> dict:
    """Fetch (and cache) the ACME directory."""
    global _directory_cache
    if _directory_cache and _directory_cache.get("_endpoint") == endpoint:
        return _directory_cache
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        r = await c.get(endpoint)
        r.raise_for_status()
        d = r.json()
        d["_endpoint"] = endpoint
        _directory_cache = d
        return d


async def _get_nonce(directory: dict) -> str:
    """Get a fresh anti-replay nonce."""
    global _nonce
    if _nonce:
        n = _nonce
        _nonce = None
        return n
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        r = await c.head(directory["newNonce"])
        return r.headers["Replay-Nonce"]


def _save_nonce(response: httpx.Response) -> None:
    """Stash the Replay-Nonce from a response for the next request."""
    global _nonce
    n = response.headers.get("Replay-Nonce")
    if n:
        _nonce = n


# ---------------------------------------------------------------------------
# Signed ACME requests
# ---------------------------------------------------------------------------


async def _acme_post(
    url: str,
    payload: dict | str | None,
    account_key: rsa.RSAPrivateKey,
    directory: dict,
    *,
    kid: str | None = None,
) -> httpx.Response:
    """Send a signed POST to an ACME endpoint and return the response."""
    nonce = await _get_nonce(directory)
    body = _sign_jws(url, payload, account_key, nonce, kid=kid)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        r = await c.post(
            url,
            json=body,
            headers={"Content-Type": "application/jose+json"},
        )
    _save_nonce(r)
    return r


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------


async def acme_register_account(endpoint: str, account_key_pem: str) -> tuple[str, str]:
    """Register (or find existing) ACME account.

    Returns ``(account_url, account_key_pem)``.  The key PEM is returned
    unchanged so callers can store both together.
    """
    directory = await acme_directory(endpoint)
    key = _load_account_key(account_key_pem)
    payload = {"termsOfServiceAgreed": True}
    r = await _acme_post(directory["newAccount"], payload, key, directory)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"ACME newAccount failed ({r.status_code}): {r.text}")
    account_url = r.headers["Location"]
    logger.info("ACME account registered: %s", account_url)
    return account_url, account_key_pem


# ---------------------------------------------------------------------------
# Order + DNS-01 challenge
# ---------------------------------------------------------------------------


async def acme_new_order(
    endpoint: str,
    account_key_pem: str,
    account_url: str,
    domain: str,
) -> dict:
    """Create a new ACME order for *domain*.

    Returns the order dict including ``authorizations``, ``finalize``, and
    the order URL (injected as ``_url``).
    """
    directory = await acme_directory(endpoint)
    key = _load_account_key(account_key_pem)
    payload = {"identifiers": [{"type": "dns", "value": domain}]}
    r = await _acme_post(
        directory["newOrder"], payload, key, directory, kid=account_url
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"ACME newOrder failed ({r.status_code}): {r.text}")
    order = r.json()
    order["_url"] = r.headers.get("Location", "")
    return order


async def acme_get_dns01_challenge(
    endpoint: str,
    account_key_pem: str,
    account_url: str,
    auth_url: str,
) -> tuple[str, str, str]:
    """Fetch the DNS-01 challenge for an authorization.

    Returns ``(challenge_url, token, key_authorization_hash)`` where
    *key_authorization_hash* is the value to place in the TXT record.
    """
    directory = await acme_directory(endpoint)
    key = _load_account_key(account_key_pem)

    # POST-as-GET to fetch the authorization
    r = await _acme_post(auth_url, "", key, directory, kid=account_url)
    r.raise_for_status()
    auth = r.json()

    dns01 = None
    for ch in auth.get("challenges", []):
        if ch["type"] == "dns-01":
            dns01 = ch
            break
    if dns01 is None:
        raise RuntimeError("No dns-01 challenge found in authorization")

    token = dns01["token"]
    thumbprint = _jwk_thumbprint(key.public_key())
    key_auth = f"{token}.{thumbprint}"
    txt_value = _b64url(hashlib.sha256(key_auth.encode()).digest())

    return dns01["url"], token, txt_value


async def acme_respond_challenge(
    endpoint: str,
    account_key_pem: str,
    account_url: str,
    challenge_url: str,
) -> None:
    """Tell the ACME server to verify the DNS-01 challenge."""
    directory = await acme_directory(endpoint)
    key = _load_account_key(account_key_pem)
    r = await _acme_post(challenge_url, {}, key, directory, kid=account_url)
    if r.status_code not in (200, 202):
        raise RuntimeError(
            f"ACME challenge response failed ({r.status_code}): {r.text}"
        )


async def acme_poll_order(
    endpoint: str,
    account_key_pem: str,
    account_url: str,
    order_url: str,
    *,
    max_attempts: int = 30,
    interval: float = 5,
) -> dict:
    """Poll an order until it becomes ``ready`` or ``valid``, or fails."""
    import asyncio

    directory = await acme_directory(endpoint)
    key = _load_account_key(account_key_pem)
    for attempt in range(max_attempts):
        r = await _acme_post(order_url, "", key, directory, kid=account_url)
        r.raise_for_status()
        order = r.json()
        status = order.get("status")
        if status in ("ready", "valid"):
            order["_url"] = order_url
            return order
        if status == "invalid":
            raise RuntimeError(f"ACME order became invalid: {json.dumps(order)}")
        await asyncio.sleep(interval)
    raise RuntimeError("Timed out waiting for ACME order to become ready")


# ---------------------------------------------------------------------------
# CSR generation & finalization
# ---------------------------------------------------------------------------


def generate_csr(domain: str) -> tuple[str, str]:
    """Generate an RSA-2048 key and CSR for *domain*.

    Returns ``(csr_der_b64url, key_pem)``.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(domain)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    csr_der = csr.public_bytes(serialization.Encoding.DER)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return _b64url(csr_der), key_pem


async def acme_finalize_order(
    endpoint: str,
    account_key_pem: str,
    account_url: str,
    finalize_url: str,
    order_url: str,
    csr_b64url: str,
) -> str:
    """Finalize an ACME order by submitting the CSR.

    After submitting the CSR the order typically enters ``processing`` state.
    This function polls the order until it becomes ``valid`` and a certificate
    URL is available, then returns that URL.
    """
    directory = await acme_directory(endpoint)
    key = _load_account_key(account_key_pem)
    payload = {"csr": csr_b64url}
    r = await _acme_post(finalize_url, payload, key, directory, kid=account_url)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"ACME finalize failed ({r.status_code}): {r.text}")
    order = r.json()
    cert_url = order.get("certificate")
    if cert_url:
        return cert_url

    # Order is still processing — poll until valid
    order = await acme_poll_order(endpoint, account_key_pem, account_url, order_url)
    cert_url = order.get("certificate")
    if not cert_url:
        raise RuntimeError("No certificate URL in finalized order")
    return cert_url


async def acme_download_cert(
    endpoint: str,
    account_key_pem: str,
    account_url: str,
    cert_url: str,
) -> str:
    """Download the issued certificate chain as PEM."""
    directory = await acme_directory(endpoint)
    key = _load_account_key(account_key_pem)
    r = await _acme_post(cert_url, "", key, directory, kid=account_url)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------------
# acme-dns helpers
# ---------------------------------------------------------------------------


async def acmedns_register(server_url: str) -> dict:
    """Register a new acme-dns account.

    Returns the registration dict with keys: ``username``, ``password``,
    ``fulldomain``, ``subdomain``, ``allowfrom``.
    """
    url = server_url.rstrip("/") + "/register"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        r = await c.post(url, json={})
        r.raise_for_status()
        return r.json()


async def acmedns_update_txt(
    server_url: str,
    subdomain: str,
    username: str,
    password: str,
    txt: str,
) -> None:
    """Update the TXT record on acme-dns."""
    url = server_url.rstrip("/") + "/update"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        r = await c.post(
            url,
            json={"subdomain": subdomain, "txt": txt},
            headers={
                "X-Api-User": username,
                "X-Api-Key": password,
            },
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"acme-dns update failed ({r.status_code}): {r.text}")


# ---------------------------------------------------------------------------
# Full provisioning orchestration
# ---------------------------------------------------------------------------


async def acme_provision_cert(
    domain: str,
    endpoint: str,
    account_key_pem: str,
    account_url: str,
    acmedns_server: str,
    acmedns_subdomain: str,
    acmedns_username: str,
    acmedns_password: str,
    *,
    progress_callback=None,
) -> tuple[str, str]:
    """Run the complete ACME DNS-01 flow and return ``(cert_chain_pem, key_pem)``.

    *progress_callback*, if provided, is called with ``(stage: str, detail: str)``
    at each major step.  Stages: ``ordering``, ``challenge``, ``updating-dns``,
    ``validating``, ``finalizing``, ``downloading``, ``complete``.
    """
    import asyncio

    def _progress(stage: str, detail: str = "") -> None:
        if progress_callback:
            progress_callback(stage, detail)

    _progress("ordering", f"Creating order for {domain}")
    order = await acme_new_order(endpoint, account_key_pem, account_url, domain)

    auth_url = order["authorizations"][0]
    _progress("challenge", "Fetching DNS-01 challenge")
    challenge_url, _token, txt_value = await acme_get_dns01_challenge(
        endpoint, account_key_pem, account_url, auth_url
    )

    _progress("updating-dns", "Setting TXT record via acme-dns")
    await acmedns_update_txt(
        acmedns_server, acmedns_subdomain, acmedns_username, acmedns_password, txt_value
    )

    # Brief pause for DNS propagation
    await asyncio.sleep(3)

    _progress("validating", "Responding to challenge and waiting for validation")
    await acme_respond_challenge(endpoint, account_key_pem, account_url, challenge_url)

    order_url = order.get("_url") or order["authorizations"][0].rsplit("/", 2)[0]
    order = await acme_poll_order(endpoint, account_key_pem, account_url, order_url)

    _progress("finalizing", "Submitting CSR and waiting for certificate")
    csr_b64, key_pem = generate_csr(domain)
    cert_url = await acme_finalize_order(
        endpoint,
        account_key_pem,
        account_url,
        order["finalize"],
        order_url,
        csr_b64,
    )

    _progress("downloading", "Downloading certificate")
    cert_pem = await acme_download_cert(
        endpoint, account_key_pem, account_url, cert_url
    )

    _progress("complete", "Certificate provisioned successfully")
    return cert_pem, key_pem


# ---------------------------------------------------------------------------
# Certificate inspection helpers
# ---------------------------------------------------------------------------


def parse_cert_info(cert_pem: str) -> dict:
    """Extract useful fields from a PEM certificate.

    Returns dict with: ``issuer``, ``subject``, ``not_before``, ``not_after``,
    ``fingerprint_sha256``, ``serial``, ``san``.
    """
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    try:
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        san = san_ext.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        san = []

    return {
        "issuer": cert.issuer.rfc4514_string(),
        "subject": cert.subject.rfc4514_string(),
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": cert.not_valid_after_utc.isoformat(),
        "fingerprint_sha256": cert.fingerprint(hashes.SHA256()).hex(),
        "serial": format(cert.serial_number, "x"),
        "san": san,
    }


def check_needs_renewal(cert_pem: str) -> bool:
    """Return True if ≥ 2/3 of the certificate's validity period has elapsed."""
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    now = datetime.datetime.now(datetime.timezone.utc)
    total = (cert.not_valid_after_utc - cert.not_valid_before_utc).total_seconds()
    elapsed = (now - cert.not_valid_before_utc).total_seconds()
    if total <= 0:
        return True
    return elapsed >= (total * 2 / 3)


def next_renewal_time(cert_pem: str) -> str | None:
    """Return ISO timestamp when renewal should happen (2/3 of validity)."""
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    total = cert.not_valid_after_utc - cert.not_valid_before_utc
    renewal_at = cert.not_valid_before_utc + (total * 2 / 3)
    return renewal_at.isoformat()
