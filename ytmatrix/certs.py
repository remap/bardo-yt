from __future__ import annotations

import ipaddress
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Hosts the certificate must cover. `localhost` is not optional and not
# interchangeable with the IP: YouTube refuses to embed into a page served from
# 127.0.0.1 (onError 150) while accepting the identical page at localhost.
CERT_HOSTS = ["localhost", "127.0.0.1", "::1"]

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def mkcert_ca_installed() -> bool:
    """True when mkcert exists AND its local CA is in the system trust store.

    Both halves matter. mkcert being on PATH says nothing about trust: until
    `mkcert -install` has run, its certificates draw exactly the same browser
    warning as a self-signed one.
    """
    if shutil.which("mkcert") is None:
        return False
    try:
        caroot = subprocess.run(
            ["mkcert", "-CAROOT"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return False
    return bool(caroot) and (Path(caroot) / "rootCA.pem").exists()


def ensure_cert(cert_path: Path, key_path: Path) -> str:
    """Produce a usable certificate, preferring one browsers already trust.

    Returns "mkcert" or "self-signed" so the caller can tell the user which
    they got, and what to do about it.
    """
    if cert_path.exists() and key_path.exists():
        return "existing"

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    if mkcert_ca_installed():
        try:
            subprocess.run(
                ["mkcert", "-cert-file", str(cert_path), "-key-file", str(key_path)] + CERT_HOSTS,
                capture_output=True,
                check=True,
                timeout=60,
            )
            return "mkcert"
        except (subprocess.SubprocessError, OSError):
            # Fall through: an untrusted cert still beats no server at all.
            pass

    ensure_self_signed_cert(cert_path, key_path)
    return "self-signed"


def ensure_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    if cert_path.exists() and key_path.exists():
        return

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    san = x509.SubjectAlternativeName(
        [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
