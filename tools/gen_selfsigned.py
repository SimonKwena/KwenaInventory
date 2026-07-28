"""Generate a self-signed TLS certificate for local/development use.

The certificate is NOT trusted by browsers (it's self-signed), so visitors to
the public site will see a "Not secure" warning. Use it only as a stopgap until
you install a free Let's Encrypt certificate via certbot.

Output:
    certs/selfsigned-cert.pem
    certs/selfsigned-key.pem
"""
import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "certs"
OUT_DIR.mkdir(exist_ok=True)

CERT_PATH = OUT_DIR / "selfsigned-cert.pem"
KEY_PATH = OUT_DIR / "selfsigned-key.pem"

DNS_NAMES = ["kwenamusic.co.za", "www.kwenamusic.co.za", "localhost"]
IP_ADDRESSES = [ipaddress.IPv4Address("127.0.0.1")]

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

subject = issuer = x509.Name(
    [
        x509.NameAttribute(NameOID.COUNTRY_NAME, "ZA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Kwena Music"),
        x509.NameAttribute(NameOID.COMMON_NAME, "kwenamusic.co.za"),
    ]
)

san = [x509.DNSName(n) for n in DNS_NAMES] + [x509.IPAddress(ip) for ip in IP_ADDRESSES]

cert = (
    x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(issuer)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
    .not_valid_after(
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=365)
    )
    .add_extension(x509.SubjectAlternativeName(san), critical=False)
    .sign(key, hashes.SHA256())
)

KEY_PATH.write_bytes(
    key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
)
CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

print(f"Wrote {CERT_PATH}")
print(f"Wrote {KEY_PATH}")
