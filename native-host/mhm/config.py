"""Config domain services."""

from __future__ import annotations
import re
import json
import base64
import urllib.parse
import uuid as uuidlib
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .errors import HostError

MAX_PROFILE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class RealityProfile:
    server: str
    port: int
    uuid: str
    flow: str
    server_name: str
    public_key: str
    short_id: str
    fingerprint: str | None
    label: str | None


def _one(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name, [])
    return values[0] if values else default


def parse_vless_url(raw_url: str) -> RealityProfile:
    try:
        parsed = urllib.parse.urlsplit(raw_url.strip())
    except ValueError as exc:
        raise HostError(f"Invalid VLESS URL: {exc}") from exc

    if parsed.scheme.lower() != "vless":
        raise HostError("Only vless:// URLs are supported")
    if not parsed.username:
        raise HostError("VLESS URL is missing UUID")
    if not parsed.hostname:
        raise HostError("VLESS URL is missing server address")

    try:
        user_uuid = urllib.parse.unquote(parsed.username)
        uuidlib.UUID(user_uuid)
    except (ValueError, AttributeError) as exc:
        raise HostError("Invalid VLESS UUID") from exc

    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise HostError("Invalid VLESS server port") from exc
    if not (1 <= port <= 65535):
        raise HostError("Invalid VLESS server port")

    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    security = _one(query, "security").lower()
    if security != "reality":
        raise HostError("Only URLs with security=reality are supported")

    transport_type = _one(query, "type", "tcp").lower()
    if transport_type not in ("tcp", "none", ""):
        raise HostError(
            f"Only TCP transport is currently supported; received type={transport_type}"
        )

    server_name = _one(query, "sni") or _one(query, "serverName")
    public_key = _one(query, "pbk") or _one(query, "publicKey")
    short_id = _one(query, "sid") or _one(query, "shortId")
    flow = _one(query, "flow", "xtls-rprx-vision")
    fingerprint = _one(query, "fp") or None

    if not server_name:
        raise HostError("REALITY URL is missing sni")
    if not public_key:
        raise HostError("REALITY URL is missing pbk (public key)")
    if flow and flow != "xtls-rprx-vision":
        raise HostError(f"Unsupported flow: {flow}")
    if short_id and (
        len(short_id) > 16
        or len(short_id) % 2 != 0
        or any(c not in "0123456789abcdefABCDEF" for c in short_id)
    ):
        raise HostError(
            "sid must be an even-length hex string of at most 16 characters"
        )

    label = urllib.parse.unquote(parsed.fragment) if parsed.fragment else None
    return RealityProfile(
        server=parsed.hostname,
        port=port,
        uuid=user_uuid,
        flow=flow,
        server_name=server_name,
        public_key=public_key,
        short_id=short_id,
        fingerprint=fingerprint,
        label=label,
    )


def _validate_subscription_url(url: str, transport: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname:
        raise HostError("Invalid subscription URL")
    if transport == "direct":
        if parsed.scheme != "https":
            raise HostError("Direct subscription updates require an https:// URL")
    elif transport == "ssh":
        if parsed.scheme not in {"http", "https"}:
            raise HostError("Subscription over SSH must use an http:// or https:// URL")
    else:
        raise HostError("Unknown subscription update transport")
    if parsed.username or parsed.password or parsed.fragment:
        raise HostError(
            "Use HTTPS URLs without userinfo or fragments. Put authorization in HTTP headers."
        )
    try:
        parsed.port
    except ValueError as exc:
        raise HostError("Invalid subscription port") from exc
    return parsed


def checked_profile_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise HostError("Invalid profile ID", "INVALID_REQUEST")
    return value


def load_mihomo_yaml(text: str) -> dict[str, Any]:
    import yaml

    if len(text.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise HostError("Profile exceeds the 8 MB limit")
    try:
        # Limit aliases, nesting, duplicate keys and non-JSON values before the
        # engine sees the document. SafeLoader alone permits recursive aliases.
        nodes = yaml.compose(text, Loader=yaml.SafeLoader)
        budget = [100000]

        def inspect_node(node, ancestors, depth=0):
            budget[0] -= 1
            if budget[0] < 0 or depth > 40 or id(node) in ancestors:
                raise HostError("YAML expansion is recursive or too large")
            descendants = ancestors | {id(node)}
            if isinstance(node, yaml.MappingNode):
                for key, value in node.value:
                    inspect_node(key, descendants, depth + 1)
                    inspect_node(value, descendants, depth + 1)
            elif isinstance(node, yaml.SequenceNode):
                for value in node.value:
                    inspect_node(value, descendants, depth + 1)

        if nodes:
            inspect_node(nodes, set())

        class UniqueLoader(yaml.SafeLoader):
            pass

        def mapping(loader, node):
            explicit = set()
            for key_node, _ in node.value:
                if key_node.tag != "tag:yaml.org,2002:merge":
                    if key_node.value in explicit:
                        raise HostError("YAML keys must be unique")
                    explicit.add(key_node.value)
            loader.flatten_mapping(node)
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node)
                if not isinstance(key, str):
                    raise HostError("YAML keys must be unique strings")
                result[key] = loader.construct_object(value_node)
            return result

        UniqueLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping
        )
        document = yaml.load(text, Loader=UniqueLoader)
        json.dumps(document, allow_nan=False)
    except HostError:
        raise
    except Exception as exc:
        raise HostError(
            "Invalid mihomo YAML. Check the file format and indentation."
        ) from exc
    if not isinstance(document, dict):
        raise HostError("Expected a mihomo YAML profile")
    if not document.get("proxies") and not document.get("proxy-providers"):
        raise HostError("The profile contains no proxies or proxy providers")
    if document.get("proxies") and not isinstance(document["proxies"], list):
        raise HostError("proxies must be a list")
    # Provider-only subscriptions become a usable standalone profile.
    if not document.get("proxy-groups") and not document.get("rules"):
        names = [
            p.get("name") for p in document.get("proxies", []) if isinstance(p, dict)
        ]
        providers = list((document.get("proxy-providers") or {}).keys())
        if any(not isinstance(n, str) or not n for n in names):
            raise HostError("Every proxy needs a name")
        group = "mhm"
        while group in names:
            group += " Proxy"
        document["proxy-groups"] = [
            {
                "name": group,
                "type": "select",
                "proxies": names,
                **({"use": providers} if providers else {}),
            }
        ]
        document["rules"] = [f"MATCH,{group}"]
    return document


def legacy_mihomo_yaml(raw_url: str) -> str:
    import yaml

    p = parse_vless_url(raw_url)
    proxy = {
        "name": p.label or "Proxy",
        "type": "vless",
        "server": p.server,
        "port": p.port,
        "uuid": p.uuid,
        "tls": True,
        "servername": p.server_name,
        "flow": p.flow,
        "client-fingerprint": p.fingerprint or "chrome",
        "reality-opts": {"public-key": p.public_key, "short-id": p.short_id},
        "udp": True,
    }
    return yaml.safe_dump({"proxies": [proxy]}, allow_unicode=True, sort_keys=False)


def profile_document(text: str) -> dict[str, Any]:
    if text.strip().lower().startswith("vless://"):
        text = legacy_mihomo_yaml(text.strip())
    return load_mihomo_yaml(text)


def wireguard_proxy(text: str) -> dict[str, Any]:
    import configparser
    import ipaddress

    if len(text.encode("utf-8")) > 65536:
        raise HostError("WireGuard configuration exceeds 64 KB")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(text.lstrip("\ufeff"))
        if set(parser.sections()) != {"Interface", "Peer"}:
            raise HostError(
                "WireGuard configuration must contain exactly [Interface] and [Peer] sections. Import the original .conf file without Markdown fences."
            )
        interface, peer = parser["Interface"], parser["Peer"]
        for section in (interface, peer):
            if any(
                k.casefold() in {"preup", "postup", "predown", "postdown"}
                for k in section
            ):
                raise HostError(
                    "WireGuard lifecycle scripts are not supported. Remove PreUp, PostUp, PreDown and PostDown."
                )
        private_key, public_key = (
            interface["PrivateKey"].strip(),
            peer["PublicKey"].strip(),
        )
        for field, key in (("PrivateKey", private_key), ("PublicKey", public_key)):
            try:
                decoded = base64.b64decode(key, validate=True)
            except ValueError as exc:
                raise HostError(
                    f"WireGuard {field} is invalid Base64. Copy the complete 44-character key from the original configuration."
                ) from exc
            if len(decoded) != 32:
                raise HostError(
                    f"WireGuard {field} must encode exactly 32 bytes. Copy the complete key from the original configuration."
                )
        endpoint = urllib.parse.urlsplit("//" + peer["Endpoint"].strip())
        if not endpoint.hostname or not endpoint.port:
            raise HostError(
                "WireGuard Endpoint must include a hostname or IP address and port, for example vpn.example.org:51820."
            )
        addresses = [
            ipaddress.ip_interface(a.strip()).ip
            for a in interface["Address"].split(",")
        ]
        ipv4 = next((str(a) for a in addresses if a.version == 4), None)
        ipv6 = next((str(a) for a in addresses if a.version == 6), None)
        if not ipv4:
            raise HostError(
                "WireGuard Address must include an IPv4 interface address, for example 10.0.0.2/32."
            )
        allowed = [
            ipaddress.ip_network(a.strip(), strict=False)
            for a in peer["AllowedIPs"].split(",")
        ]
        if not allowed:
            raise HostError("WireGuard AllowedIPs must contain at least one IP network")
        dns = [
            str(ipaddress.ip_address(a.strip()))
            for a in interface.get("DNS", "1.1.1.1").split(",")
        ]
        result = {
            "name": "mhm WireGuard",
            "type": "wireguard",
            "server": endpoint.hostname,
            "port": endpoint.port,
            "ip": ipv4,
            "private-key": private_key,
            "public-key": public_key,
            "udp": True,
            "remote-dns-resolve": True,
            "dns": dns,
        }
        if ipv6:
            result["ipv6"] = ipv6
        if peer.get("PresharedKey"):
            key = peer["PresharedKey"].strip()
            try:
                valid_key = len(base64.b64decode(key, validate=True)) == 32
            except ValueError:
                valid_key = False
            if not valid_key:
                raise HostError(
                    "WireGuard PresharedKey must be a complete Base64 key encoding 32 bytes."
                )
            result["pre-shared-key"] = key
        mtu = int(interface.get("MTU", "1420"))
        if not 1280 <= mtu <= 9000:
            raise HostError("WireGuard MTU must be between 1280 and 9000.")
        result["mtu"] = mtu
        keepalive = int(peer.get("PersistentKeepalive", "0"))
        if not 0 <= keepalive <= 65535:
            raise HostError(
                "WireGuard PersistentKeepalive must be between 0 and 65535 seconds"
            )
        result["persistent-keepalive"] = keepalive
        remote_peer = {key: result.pop(key) for key in ("server", "port", "public-key")}
        if "pre-shared-key" in result:
            remote_peer["pre-shared-key"] = result.pop("pre-shared-key")
        remote_peer["allowed-ips"] = [str(network) for network in allowed]
        result["peers"] = [remote_peer]
        return result
    except HostError:
        raise
    except configparser.DuplicateSectionError as exc:
        raise HostError(
            "WireGuard configuration must contain exactly one [Interface] and one [Peer]. Multiple peers are not supported yet."
        ) from exc
    except configparser.DuplicateOptionError as exc:
        raise HostError(
            "WireGuard configuration contains a repeated field. Keep one value per field."
        ) from exc
    except configparser.Error as exc:
        raise HostError(
            "WireGuard configuration has invalid INI syntax. Import the original .conf file with real line breaks and no Markdown fences."
        ) from exc
    except KeyError as exc:
        field = exc.args[0] if exc.args else ""
        safe_fields = {
            "Interface",
            "Peer",
            "PrivateKey",
            "PublicKey",
            "Address",
            "Endpoint",
            "AllowedIPs",
        }
        if field not in safe_fields:
            field = "required field"
        raise HostError(
            f"WireGuard configuration is missing {field}. Import the complete original .conf file."
        ) from exc
    except Exception as exc:
        raise HostError(
            "WireGuard configuration contains an invalid address, endpoint, route or number. Check Address, Endpoint, AllowedIPs, DNS, MTU and PersistentKeepalive."
        ) from exc


def build_mihomo_config(
    document: dict[str, Any], directory: Path, port: int, api_port: int, secret: str
) -> dict[str, Any]:
    import copy

    config = copy.deepcopy(document)
    for key in (
        "listeners",
        "external-controller-unix",
        "external-controller-pipe",
        "external-controller-tls",
        "external-ui",
        "external-ui-url",
        "external-ui-name",
        "authentication",
        "users",
        "tunnels",
        "iptables",
        "interface-name",
        "routing-mark",
        "geox-url",
    ):
        config.pop(key, None)
    config.update(
        {
            "mixed-port": port,
            "port": 0,
            "socks-port": 0,
            "redir-port": 0,
            "tproxy-port": 0,
            "allow-lan": False,
            "bind-address": "127.0.0.1",
            "external-controller": f"127.0.0.1:{api_port}",
            "secret": secret,
            "external-controller-cors": {
                "allow-origins": [],
                "allow-private-network": False,
            },
            "tun": {"enable": False},
            "log-level": "silent",
            "profile": {"store-selected": False, "store-fake-ip": False},
            "geodata-mode": False,
            "geo-auto-update": False,
        }
    )
    dns = config.get("dns") or {}
    if not isinstance(dns, dict):
        raise HostError("dns must be a mapping")
    dns.pop("listen", None)
    config["dns"] = dns
    # Remote providers retain their semantics, but all files stay inside mhm.
    for section in ("proxy-providers", "rule-providers"):
        providers = config.get(section) or {}
        if not isinstance(providers, dict):
            raise HostError(f"{section} must be a mapping")
        for name, provider in providers.items():
            if not isinstance(provider, dict):
                raise HostError("Invalid provider")
            kind = provider.get("type", "http")
            if kind not in {"http", "inline"}:
                raise HostError(
                    "File providers require a self-contained profile. Use HTTPS or inline providers."
                )
            provider.pop("path", None)
            if kind == "http":
                _validate_subscription_url(str(provider.get("url") or ""), "direct")
                digest = hashlib.sha256(f"{section}/{name}".encode()).hexdigest()
                provider["path"] = str(directory / f"{digest}.yaml")

    # Files referenced by certificates/keys are outside the portable profile contract.
    def inspect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    key in {"certificate", "private-key"}
                    and isinstance(child, str)
                    and ("/" in child or "\\" in child)
                    and "-----BEGIN" not in child
                ):
                    # WireGuard keys are base64 and can contain '/'.
                    if value.get("type") != "wireguard":
                        raise HostError(
                            "Embed certificates and keys in the profile instead of referencing files"
                        )
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(config)
    return config
