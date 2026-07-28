"""Generate a self-signed development TLS certificate.

Used by run_https.py so the Django dev server can be reached over HTTPS on the
LAN. Browsers only expose the camera (getUserMedia) in a secure context, so a
phone accessing the app at http://192.168.1.39 cannot open the camera. Serving
over HTTPS fixes that for local development.

Usage:
    python tools/gen_cert.py [extra-host ...]
"""
import datetime
import ipaddress
import re
import socket
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

BASE_DIR = Path(__file__).resolve().parent.parent
CERT_DIR = BASE_DIR / "certs"
CERT_PATH = CERT_DIR / "dev-cert.pem"
KEY_PATH = CERT_DIR / "dev-key.pem"

DEFAULT_HOSTS = ["192.168.1.39", "localhost", "127.0.0.1"]


def _local_ip():
    """Best-effort detection of this machine's LAN IPv4 address.

    First tries a UDP probe (needs a default route); falls back to enumerating
    local interfaces via `ipconfig` so it still works on a LAN-only machine
    with no outbound internet.
    """
    candidates = []

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()

    import subprocess

    try:
        out = subprocess.run(
            ["ipconfig"], capture_output=True, text=True
        ).stdout
        for m in re.findall(r"IPv4 Address[ .]*: ([0-9.]+)", out):
            candidates.append(m)
    except Exception:
        pass

    for ip in candidates:
        if ip and ip != "127.0.0.1":
            return ip
    return None


def _san_for(host):
    host = host.split(":")[0].strip()
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def generate(hosts=None):
    hosts = list(dict.fromkeys(hosts or DEFAULT_HOSTS))
    local_ip = _local_ip()
    if local_ip and local_ip not in hosts:
        hosts.append(local_ip)
    sans = [_san_for(h) for h in hosts]

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "ZA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Kwena Music Dev"),
            x509.NameAttribute(NameOID.COMMON_NAME, hosts[0].split(":")[0]),
        ]
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )

    CERT_DIR.mkdir(exist_ok=True)
    with open(KEY_PATH, "wb") as fh:
        fh.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    with open(CERT_PATH, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))

    return CERT_PATH, KEY_PATH


if __name__ == "__main__":
    hosts = sys.argv[1:] or DEFAULT_HOSTS
    cert_file, key_file = generate(hosts)
    print("Wrote certificate:")
    print(f"  {cert_file}")
    print(f"  {key_file}")
