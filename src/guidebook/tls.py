import atexit
import datetime
import logging
import os
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

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "guidebook"),
    ])

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
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                x509.IPAddress(ipaddress.IPv6Address("::1")),
            ]),
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

    logger.info("Generating new self-signed TLS certificate (valid %d days)", CERT_VALIDITY_DAYS)
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
