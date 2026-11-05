"""Tls domain services."""

from __future__ import annotations
import ssl
import base64
from .errors import HostError


def normalize_subscription_ca(value: str) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    if not isinstance(value, str) or len(value.encode("utf-8")) > 256 * 1024:
        raise HostError("CA certificate must be smaller than 256 KB")
    if "PRIVATE KEY" in value:
        raise HostError("Import a public certificate, not a private key")
    try:
        if value.startswith("der:"):
            certificates = [
                x509.load_der_x509_certificate(
                    base64.b64decode(value[4:], validate=True)
                )
            ]
        else:
            certificates = x509.load_pem_x509_certificates(value.encode("utf-8"))
        if not certificates:
            raise ValueError()
        return "".join(
            certificate.public_bytes(Encoding.PEM).decode("ascii")
            for certificate in certificates
        )
    except Exception as exc:
        raise HostError(
            "Invalid CA certificate. Import a PEM or DER .crt, .cer or .pem file."
        ) from exc


def subscription_tls_context(options=None):
    options = options or {}
    mode = options.get("mode", "system")
    if mode == "insecure":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if mode not in {"system", "custom"}:
        raise HostError("Invalid HTTPS certificate verification mode")
    import truststore

    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if mode == "custom":
        certificate = normalize_subscription_ca(options.get("caCertificate", ""))
        context.load_verify_locations(cadata=certificate)
    return context
