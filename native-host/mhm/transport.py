"""HTTPS subscription transport over direct TCP or a local SOCKS tunnel."""

from __future__ import annotations
import socket
import struct
import ssl
import http.client
import urllib.parse
from .errors import HostError
from .config import _validate_subscription_url, MAX_PROFILE_BYTES
from .tls import subscription_tls_context


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise HostError("SOCKS5 proxy closed the connection unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _open_socks5_tunnel(
    proxy_port: int, host: str, port: int, timeout: float = 12.0
) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2) != b"\x05\x00":
            raise HostError("Local SOCKS5 proxy rejected unauthenticated connection")

        import ipaddress

        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host_bytes = host.encode("idna")
            if len(host_bytes) > 255:
                raise HostError("Subscription hostname is too long for SOCKS5")
            destination = b"\x03" + bytes([len(host_bytes)]) + host_bytes
        else:
            destination = (
                b"\x01" if address.version == 4 else b"\x04"
            ) + address.packed
        request = b"\x05\x01\x00" + destination + struct.pack("!H", port)
        sock.sendall(request)
        head = _recv_exact(sock, 4)
        if head[0] != 5 or head[1] != 0:
            raise HostError(
                f"WireGuard could not reach the subscription server (SOCKS5 code {head[1]}). Check the tunnel handshake, internal DNS, AllowedIPs and server access."
            )
        atyp = head[3]
        if atyp == 1:
            _recv_exact(sock, 4)
        elif atyp == 3:
            _recv_exact(sock, _recv_exact(sock, 1)[0])
        elif atyp == 4:
            _recv_exact(sock, 16)
        else:
            raise HostError("SOCKS5 proxy returned an invalid address type")
        _recv_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


def download_subscription(
    url: str,
    custom_headers: dict,
    proxy_port: int | None = None,
    tls_options: dict | None = None,
):
    current = url
    original = urllib.parse.urlsplit(url)
    for _ in range(6):
        parsed = _validate_subscription_url(current, "direct")
        headers = {
            "User-Agent": "mihomo",
            "Accept": "application/yaml, text/yaml, text/plain",
            "Accept-Encoding": "identity",
        }
        if (parsed.hostname, parsed.port) == (original.hostname, original.port):
            headers.update(custom_headers)
        connection = None
        try:
            # Custom trust and verification bypass apply only to the selected
            # subscription origin, never to a different redirect destination.
            same_origin = (parsed.hostname, parsed.port or 443) == (
                original.hostname,
                original.port or 443,
            )
            context = subscription_tls_context(tls_options if same_origin else None)
            if proxy_port:
                raw = _open_socks5_tunnel(
                    proxy_port, parsed.hostname, parsed.port or 443, timeout=20
                )
                try:
                    tls = context.wrap_socket(raw, server_hostname=parsed.hostname)
                except Exception:
                    raw.close()
                    raise
                connection = http.client.HTTPSConnection(
                    parsed.hostname,
                    parsed.port or 443,
                    timeout=30,
                    context=context,
                )
                connection.sock = tls
            else:
                connection = http.client.HTTPSConnection(
                    parsed.hostname,
                    parsed.port or 443,
                    timeout=30,
                    context=context,
                )
            path = urllib.parse.urlunsplit(
                ("", "", parsed.path or "/", parsed.query, "")
            )
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise HostError("Subscription redirect has no destination")
                current = urllib.parse.urljoin(current, location)
                continue
            if response.status != 200:
                raise HostError(f"Subscription server returned HTTP {response.status}")
            body = response.read(MAX_PROFILE_BYTES + 1)
            if len(body) > MAX_PROFILE_BYTES:
                raise HostError("Subscription exceeds the 8 MB limit")
            return body, {k.lower(): v for k, v in response.getheaders()}
        except HostError:
            raise
        except ssl.SSLCertVerificationError as exc:
            raise HostError(
                "The subscription HTTPS certificate is not trusted or does not match its hostname. Install the issuing CA in your operating system's trusted certificate store and check the subscription hostname."
            ) from exc
        except ssl.SSLError as exc:
            raise HostError(
                "The subscription server could not establish a secure HTTPS connection. Check the HTTPS port and TLS configuration."
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise HostError(
                (
                    "Subscription connection timed out through WireGuard. Check the VPN handshake, internal DNS, AllowedIPs and HTTPS server access."
                    if proxy_port
                    else "The subscription HTTPS server did not respond in time."
                ),
                "TIMEOUT",
            ) from exc
        except socket.gaierror as exc:
            raise HostError(
                "The subscription hostname could not be resolved. Check its spelling and DNS configuration."
            ) from exc
        except ConnectionRefusedError as exc:
            raise HostError(
                "The subscription server refused the TCP connection. Check its HTTPS port and firewall."
            ) from exc
        except OSError as exc:
            raise HostError(
                "The subscription connection was interrupted. Check the WireGuard routes and HTTPS server availability."
            ) from exc
        except http.client.HTTPException as exc:
            raise HostError(
                "The subscription server returned an invalid HTTPS response."
            ) from exc
        finally:
            if connection:
                connection.close()
    raise HostError("Too many subscription redirects")
