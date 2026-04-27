import atexit
import datetime
import logging
import os
import secrets
import sqlite3
import tempfile

logger = logging.getLogger("guidebook")

CERT_VALIDITY_DAYS = 3650  # ~10 years
CA_VALIDITY_DAYS = 7300  # ~20 years


def _generate_server_cert(
    ca_cert_pem: str | None = None, ca_key_pem: str | None = None
) -> tuple[str, str]:
    """Generate a server TLS certificate. If CA cert/key are provided, the
    server cert is signed by the CA. Otherwise it is self-signed.
    Returns (cert_pem, key_pem).
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "guidebook"),
        ]
    )

    if ca_cert_pem and ca_key_pem:
        ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode())
        ca_key = serialization.load_pem_private_key(ca_key_pem.encode(), password=None)
        issuer = ca_cert.subject
        signing_key = ca_key
    else:
        issuer = subject
        signing_key = key

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv6Address("::1")),
                ]
            ),
            critical=False,
        )
        .sign(signing_key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()

    return cert_pem, key_pem


def generate_ca_cert() -> tuple[str, str]:
    """Generate a CA certificate and key for signing client certs. Returns (cert_pem, key_pem)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "guidebook-ca"),
        ]
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=CA_VALIDITY_DAYS))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()

    return cert_pem, key_pem


def ensure_ca_cert(meta_db_path: str) -> tuple[str, str]:
    """Load or generate CA cert/key from the global database. Returns (cert_pem, key_pem)."""
    os.makedirs(os.path.dirname(meta_db_path), exist_ok=True)
    conn = sqlite3.connect(meta_db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings "
        "(id INTEGER NOT NULL PRIMARY KEY, key VARCHAR NOT NULL UNIQUE, value VARCHAR)"
    )

    row_cert = conn.execute(
        "SELECT value FROM settings WHERE key = 'ca_cert_pem'"
    ).fetchone()
    row_key = conn.execute(
        "SELECT value FROM settings WHERE key = 'ca_key_pem'"
    ).fetchone()

    if row_cert and row_key and row_cert[0] and row_key[0]:
        conn.close()
        logger.info("Loaded CA certificate from database")
        return row_cert[0], row_key[0]

    logger.info("Generating new CA certificate (valid %d days)", CA_VALIDITY_DAYS)
    cert_pem, key_pem = generate_ca_cert()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('ca_cert_pem', ?)",
        (cert_pem,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('ca_key_pem', ?)",
        (key_pem,),
    )
    conn.commit()
    conn.close()
    return cert_pem, key_pem


def generate_client_cert(
    ca_cert_pem: str, ca_key_pem: str, label: str
) -> tuple[bytes, str, str, str]:
    """Generate a client certificate signed by the CA, bundled as PKCS#12.

    Uses legacy 3DES+SHA1 encryption for maximum browser compatibility
    (Firefox's NSS library does not support AES-256-CBC PKCS#12 files).

    Returns (p12_bytes, password, serial_hex, fingerprint_sha256).
    """
    import subprocess

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode())
    ca_key = serialization.load_pem_private_key(ca_key_pem.encode(), password=None)

    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    now = datetime.datetime.now(datetime.timezone.utc)
    serial = x509.random_serial_number()
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(
                        NameOID.COMMON_NAME, f"guidebook-client-{label}"
                    ),
                ]
            )
        )
        .issuer_name(ca_cert.subject)
        .public_key(client_key.public_key())
        .serial_number(serial)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    fingerprint = client_cert.fingerprint(hashes.SHA256()).hex()
    serial_hex = format(serial, "x")

    # Build PKCS#12 with legacy 3DES+SHA1 via openssl for browser compatibility
    password = secrets.token_urlsafe(24)
    client_cert_pem = client_cert.public_bytes(serialization.Encoding.PEM)
    client_key_pem = client_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )

    cert_file = tempfile.NamedTemporaryFile(
        delete=False, suffix=".pem", prefix="guidebook_ccert_"
    )
    cert_file.write(client_cert_pem)
    cert_file.close()
    key_file = tempfile.NamedTemporaryFile(
        delete=False, suffix=".pem", prefix="guidebook_ckey_"
    )
    key_file.write(client_key_pem)
    key_file.close()
    ca_file = tempfile.NamedTemporaryFile(
        delete=False, suffix=".pem", prefix="guidebook_cacert_"
    )
    ca_file.write(ca_cert_pem.encode())
    ca_file.close()
    p12_file = tempfile.NamedTemporaryFile(
        delete=False, suffix=".p12", prefix="guidebook_p12_"
    )
    p12_file.close()

    try:
        result = subprocess.run(
            [
                "openssl",
                "pkcs12",
                "-export",
                "-inkey",
                key_file.name,
                "-in",
                cert_file.name,
                "-certfile",
                ca_file.name,
                "-out",
                p12_file.name,
                "-passout",
                f"pass:{password}",
                "-keypbe",
                "pbeWithSHA1And3-KeyTripleDES-CBC",
                "-certpbe",
                "pbeWithSHA1And3-KeyTripleDES-CBC",
                "-macalg",
                "sha1",
                "-name",
                f"guidebook-{label}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"openssl pkcs12 failed: {result.stderr}")

        with open(p12_file.name, "rb") as f:
            p12_bytes = f.read()
    finally:
        for path in (cert_file.name, key_file.name, ca_file.name, p12_file.name):
            try:
                os.unlink(path)
            except OSError:
                pass

    return p12_bytes, password, serial_hex, fingerprint


def generate_crl(
    ca_cert_pem: str,
    ca_key_pem: str,
    revoked_serials: list[tuple[int, datetime.datetime]],
) -> str:
    """Generate a PEM-encoded CRL containing revoked serial numbers.

    revoked_serials is a list of (serial_number_int, revocation_datetime).
    Returns CRL as PEM string.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode())
    ca_key = serialization.load_pem_private_key(ca_key_pem.encode(), password=None)

    now = datetime.datetime.now(datetime.timezone.utc)
    builder = x509.CertificateRevocationListBuilder()
    builder = builder.issuer_name(ca_cert.subject)
    builder = builder.last_update(now)
    builder = builder.next_update(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))

    for serial_int, revoked_at in revoked_serials:
        revoked_cert = (
            x509.RevokedCertificateBuilder()
            .serial_number(serial_int)
            .revocation_date(revoked_at)
            .build()
        )
        builder = builder.add_revoked_certificate(revoked_cert)

    crl = builder.sign(ca_key, hashes.SHA256())
    return crl.public_bytes(serialization.Encoding.PEM).decode()


def _load_ca_from_db(conn) -> tuple[str | None, str | None]:
    """Load CA cert/key from the database if they exist."""
    row_ca_cert = conn.execute(
        "SELECT value FROM settings WHERE key = 'ca_cert_pem'"
    ).fetchone()
    row_ca_key = conn.execute(
        "SELECT value FROM settings WHERE key = 'ca_key_pem'"
    ).fetchone()
    ca_cert = row_ca_cert[0] if row_ca_cert and row_ca_cert[0] else None
    ca_key = row_ca_key[0] if row_ca_key and row_ca_key[0] else None
    return ca_cert, ca_key


def _is_signed_by_ca(cert_pem: str, ca_cert_pem: str) -> bool:
    """Check if a certificate was issued by the given CA."""
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode())
    return cert.issuer == ca_cert.subject


def ensure_tls_cert(meta_db_path: str) -> tuple[str, str]:
    """Load or generate TLS cert/key from the global database. Returns (cert_pem, key_pem).

    If ``tls_mode`` is ``"acme"`` and an ACME cert exists, it is returned as-is.
    Otherwise, if a CA exists, the server cert is signed by the CA.  If the
    existing server cert is self-signed but a CA now exists, it is regenerated
    as CA-signed.
    """
    os.makedirs(os.path.dirname(meta_db_path), exist_ok=True)
    conn = sqlite3.connect(meta_db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings "
        "(id INTEGER NOT NULL PRIMARY KEY, key VARCHAR NOT NULL UNIQUE, value VARCHAR)"
    )

    # If in ACME mode, use the stored cert directly (don't regenerate)
    tls_mode_row = conn.execute(
        "SELECT value FROM settings WHERE key = 'tls_mode'"
    ).fetchone()
    tls_mode = tls_mode_row[0] if tls_mode_row and tls_mode_row[0] else "self-signed"

    ca_cert_pem, ca_key_pem = _load_ca_from_db(conn)

    row_cert = conn.execute(
        "SELECT value FROM settings WHERE key = 'tls_cert_pem'"
    ).fetchone()
    row_key = conn.execute(
        "SELECT value FROM settings WHERE key = 'tls_key_pem'"
    ).fetchone()

    if row_cert and row_key and row_cert[0] and row_key[0]:
        if tls_mode == "acme":
            conn.close()
            logger.info("Loaded ACME TLS certificate from database")
            return row_cert[0], row_key[0]
        # Check if we need to regenerate: CA exists but cert is not CA-signed
        if ca_cert_pem and not _is_signed_by_ca(row_cert[0], ca_cert_pem):
            logger.info("Regenerating server certificate (signing with CA)")
        else:
            conn.close()
            logger.info("Loaded TLS certificate from database")
            return row_cert[0], row_key[0]

    if ca_cert_pem and ca_key_pem:
        logger.info(
            "Generating CA-signed server certificate (valid %d days)",
            CERT_VALIDITY_DAYS,
        )
        cert_pem, key_pem = _generate_server_cert(ca_cert_pem, ca_key_pem)
    else:
        logger.info(
            "Generating self-signed server certificate (valid %d days)",
            CERT_VALIDITY_DAYS,
        )
        cert_pem, key_pem = _generate_server_cert()

    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('tls_cert_pem', ?)",
        (cert_pem,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('tls_key_pem', ?)",
        (key_pem,),
    )
    conn.commit()
    conn.close()
    return cert_pem, key_pem


def write_tls_temp_files(cert_pem: str, key_pem: str) -> tuple[str, str]:
    """Write cert/key PEM to temp files for uvicorn. Returns (certfile_path, keyfile_path)."""
    cert_file = tempfile.NamedTemporaryFile(
        delete=False, suffix=".pem", prefix="guidebook_cert_"
    )
    cert_file.write(cert_pem.encode())
    cert_file.close()

    key_file = tempfile.NamedTemporaryFile(
        delete=False, suffix=".pem", prefix="guidebook_key_"
    )
    key_file.write(key_pem.encode())
    key_file.close()

    # Restrict key file permissions
    try:
        os.chmod(key_file.name, 0o600)
    except OSError:
        pass

    def cleanup():
        for path in (cert_file.name, key_file.name):
            try:
                os.unlink(path)
            except OSError:
                pass

    atexit.register(cleanup)
    return cert_file.name, key_file.name


def write_ca_temp_file(ca_cert_pem: str) -> str:
    """Write CA cert PEM to a temp file for uvicorn ssl_ca_certs. Returns path."""
    ca_file = tempfile.NamedTemporaryFile(
        delete=False, suffix=".pem", prefix="guidebook_ca_"
    )
    ca_file.write(ca_cert_pem.encode())
    ca_file.close()

    def cleanup():
        try:
            os.unlink(ca_file.name)
        except OSError:
            pass

    atexit.register(cleanup)
    return ca_file.name


def write_crl_temp_file(crl_pem: str) -> str:
    """Write CRL PEM to a temp file. Returns path."""
    crl_file = tempfile.NamedTemporaryFile(
        delete=False, suffix=".pem", prefix="guidebook_crl_"
    )
    crl_file.write(crl_pem.encode())
    crl_file.close()

    def cleanup():
        try:
            os.unlink(crl_file.name)
        except OSError:
            pass

    atexit.register(cleanup)
    return crl_file.name
