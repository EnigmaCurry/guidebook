import atexit
import datetime
import logging
import os
import secrets
import sqlite3
import tempfile

logger = logging.getLogger("guidebook")

CERT_VALIDITY_DAYS = 3650  # ~10 years


def generate_self_signed_cert() -> tuple[str, str]:
    """Generate a self-signed TLS certificate. Returns (cert_pem, key_pem)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "guidebook"),
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
        .sign(key, hashes.SHA256())
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
        .not_valid_after(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))
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

    logger.info("Generating new CA certificate (valid %d days)", CERT_VALIDITY_DAYS)
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

    Returns (p12_bytes, password, serial_hex, fingerprint_sha256).
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import (
        pkcs12,
        BestAvailableEncryption,
    )
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

    password = secrets.token_urlsafe(24)
    p12_bytes = pkcs12.serialize_key_and_certificates(
        name=b"guidebook-client",
        key=client_key,
        cert=client_cert,
        cas=[ca_cert],
        encryption_algorithm=BestAvailableEncryption(password.encode()),
    )

    fingerprint = client_cert.fingerprint(hashes.SHA256()).hex()
    serial_hex = format(serial, "x")

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


def ensure_tls_cert(meta_db_path: str) -> tuple[str, str]:
    """Load or generate TLS cert/key from the global database. Returns (cert_pem, key_pem)."""
    os.makedirs(os.path.dirname(meta_db_path), exist_ok=True)
    conn = sqlite3.connect(meta_db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings "
        "(id INTEGER NOT NULL PRIMARY KEY, key VARCHAR NOT NULL UNIQUE, value VARCHAR)"
    )

    row_cert = conn.execute(
        "SELECT value FROM settings WHERE key = 'tls_cert_pem'"
    ).fetchone()
    row_key = conn.execute(
        "SELECT value FROM settings WHERE key = 'tls_key_pem'"
    ).fetchone()

    if row_cert and row_key and row_cert[0] and row_key[0]:
        conn.close()
        logger.info("Loaded TLS certificate from database")
        return row_cert[0], row_key[0]

    logger.info(
        "Generating new self-signed TLS certificate (valid %d days)", CERT_VALIDITY_DAYS
    )
    cert_pem, key_pem = generate_self_signed_cert()
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
