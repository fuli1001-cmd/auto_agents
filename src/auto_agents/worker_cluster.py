from __future__ import annotations

from dataclasses import asdict, dataclass, field
import base64
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import threading
import time
from typing import Mapping, Optional


DISCOVERY_PORT = 47321
WORKER_API_PORT = 47322
CLUSTER_PROTOCOL_VERSION = 2
PAIRING_PREFIX = "aa-worker-v1."


def cluster_root() -> Path:
    override = os.environ.get("AUTO_AGENTS_CLUSTER_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".config" / "auto-agents" / "cluster").resolve()


@dataclass
class ClusterState:
    cluster_id: str
    node_id: str
    secret: str
    hostname: str
    api_port: int = WORKER_API_PORT
    peers: dict[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ClusterState":
        peers = payload.get("peers", {})
        return cls(
            cluster_id=str(payload.get("cluster_id", "")),
            node_id=str(payload.get("node_id", "")),
            secret=str(payload.get("secret", "")),
            hostname=str(payload.get("hostname", socket.gethostname())),
            api_port=int(payload.get("api_port", WORKER_API_PORT)),
            peers={
                str(key): dict(value)
                for key, value in peers.items()
                if isinstance(value, dict)
            }
            if isinstance(peers, dict)
            else {},
        )


@dataclass(frozen=True)
class DiscoveredWorker:
    worker_id: str
    hostname: str
    host: str
    port: int
    tls_fingerprint: str
    max_slots: int
    capabilities: tuple[str, ...]
    last_seen: float

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        fallback_host: str = "",
    ) -> "DiscoveredWorker":
        raw_capabilities = payload.get("capabilities", [])
        return cls(
            worker_id=str(payload.get("worker_id", "")),
            hostname=str(payload.get("hostname", "")),
            host=str(payload.get("host", fallback_host)),
            port=int(payload.get("port", WORKER_API_PORT)),
            tls_fingerprint=str(payload.get("tls_fingerprint", "")),
            max_slots=max(1, int(payload.get("max_slots", 1))),
            capabilities=tuple(
                sorted(
                    str(item).strip().lower()
                    for item in raw_capabilities
                    if str(item).strip()
                )
            )
            if isinstance(raw_capabilities, list)
            else (),
            last_seen=float(payload.get("last_seen", time.time())),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _state_path() -> Path:
    return cluster_root() / "cluster.json"


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load_cluster_state(*, required: bool = False) -> Optional[ClusterState]:
    path = _state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise RuntimeError(
                "this computer is not paired; run `auto-agents cluster init` "
                "or `auto-agents worker serve --join <code>`"
            )
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid cluster state: {error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError("invalid cluster state: root must be an object")
    state = ClusterState.from_dict(payload)
    if not state.cluster_id or not state.node_id or not state.secret:
        raise RuntimeError("invalid cluster state: required identity fields are missing")
    return state


def save_cluster_state(state: ClusterState) -> None:
    _write_json_atomic(_state_path(), asdict(state))


def certificate_paths() -> tuple[Path, Path]:
    root = cluster_root()
    return root / "node.crt", root / "node.key"


def _ensure_certificate(node_id: str) -> tuple[Path, Path]:
    certificate, private_key = certificate_paths()
    if certificate.is_file() and private_key.is_file():
        return certificate, private_key
    certificate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    process = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "3650",
            "-subj",
            f"/CN={node_id}",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip() or "failed to generate worker TLS certificate"
        )
    os.chmod(private_key, 0o600)
    os.chmod(certificate, 0o600)
    return certificate, private_key


def certificate_fingerprint(certificate: Optional[Path] = None) -> str:
    certificate = certificate or certificate_paths()[0]
    process = subprocess.run(
        ["openssl", "x509", "-in", str(certificate), "-outform", "DER"],
        capture_output=True,
    )
    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.decode("utf-8", errors="replace").strip()
            or "failed to read worker TLS certificate"
        )
    return hashlib.sha256(process.stdout).hexdigest()


def init_cluster(*, name: str = "") -> ClusterState:
    existing = load_cluster_state()
    if existing is not None:
        _ensure_certificate(existing.node_id)
        return existing
    node_id = secrets.token_hex(12)
    state = ClusterState(
        cluster_id=hashlib.sha256(
            f"{name}:{secrets.token_hex(32)}".encode("utf-8")
        ).hexdigest()[:24],
        node_id=node_id,
        secret=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        hostname=socket.gethostname(),
    )
    _ensure_certificate(node_id)
    save_cluster_state(state)
    return state


def _secret_bytes(state: ClusterState) -> bytes:
    try:
        return base64.urlsafe_b64decode(state.secret.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise RuntimeError("invalid cluster secret") from error


def canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sign_payload(state: ClusterState, payload: Mapping[str, object]) -> str:
    return hmac.new(
        _secret_bytes(state),
        canonical_json(payload),
        hashlib.sha256,
    ).hexdigest()


def verify_payload(
    state: ClusterState,
    payload: Mapping[str, object],
    signature: str,
) -> bool:
    return hmac.compare_digest(sign_payload(state, payload), str(signature))


def local_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _encode_invite(payload: Mapping[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(canonical_json(payload)).decode("ascii")
    return PAIRING_PREFIX + encoded.rstrip("=")


def decode_pairing_invite(code: str) -> dict[str, object]:
    if not code.startswith(PAIRING_PREFIX):
        raise ValueError("invalid pairing code prefix")
    encoded = code[len(PAIRING_PREFIX) :]
    encoded += "=" * (-len(encoded) % 4)
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid pairing code") from error
    if not isinstance(payload, dict):
        raise ValueError("invalid pairing code payload")
    if float(payload.get("expires_at", 0)) <= time.time():
        raise ValueError("pairing code has expired")
    return payload


def create_pairing_invite(
    *,
    host: str = "",
    port: int = WORKER_API_PORT,
    ttl_seconds: int = 600,
) -> str:
    state = load_cluster_state(required=True)
    assert state is not None
    _ensure_certificate(state.node_id)
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + max(30, int(ttl_seconds))
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    pairing_path = cluster_root() / "pairings" / f"{token_hash}.json"
    _write_json_atomic(
        pairing_path,
        {
            "token_hash": token_hash,
            "expires_at": expires_at,
            "used": False,
        },
    )
    return _encode_invite(
        {
            "host": host or local_lan_ip(),
            "port": int(port),
            "token": token,
            "expires_at": expires_at,
            "tls_fingerprint": certificate_fingerprint(),
        }
    )


def consume_pairing_token(token: str) -> ClusterState:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    path = cluster_root() / "pairings" / f"{token_hash}.json"
    consumed_path = path.with_suffix(f".used-{secrets.token_hex(6)}.json")
    try:
        os.replace(path, consumed_path)
        payload = json.loads(consumed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PermissionError("pairing token is unknown") from error
    if (
        not isinstance(payload, dict)
        or bool(payload.get("used"))
        or float(payload.get("expires_at", 0)) <= time.time()
    ):
        raise PermissionError("pairing token is expired or already used")
    payload["used"] = True
    payload["used_at"] = time.time()
    _write_json_atomic(consumed_path, payload)
    state = load_cluster_state(required=True)
    assert state is not None
    return state


def join_cluster(code: str, *, timeout_seconds: float = 10.0) -> ClusterState:
    if load_cluster_state() is not None:
        raise RuntimeError(
            "this computer is already paired; refusing to replace its cluster identity"
        )
    invite = decode_pairing_invite(code)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(
        str(invite["host"]),
        int(invite["port"]),
        timeout=timeout_seconds,
        context=context,
    )
    body = canonical_json({"token": str(invite["token"])})
    connection.connect()
    assert connection.sock is not None
    actual = hashlib.sha256(
        connection.sock.getpeercert(binary_form=True)
    ).hexdigest()
    if not hmac.compare_digest(actual, str(invite["tls_fingerprint"])):
        connection.close()
        raise RuntimeError("pairing server TLS fingerprint does not match the code")
    connection.request(
        "POST",
        "/v1/pair",
        body=body,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
    )
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    connection.close()
    if response.status != 200 or not isinstance(payload, dict):
        raise RuntimeError(str(payload.get("error", "pairing failed")))
    state = ClusterState.from_dict(payload)
    if not state.cluster_id or not state.node_id or not state.secret:
        raise RuntimeError("pairing server returned incomplete cluster state")
    inviter_id = state.node_id
    inviter_host = str(invite["host"])
    inviter_port = int(invite["port"])
    inviter_fingerprint = str(invite["tls_fingerprint"])
    state.node_id = secrets.token_hex(12)
    state.hostname = socket.gethostname()
    state.peers = {
        inviter_id: {
            "worker_id": inviter_id,
            "hostname": str(payload.get("hostname", inviter_host)),
            "host": inviter_host,
            "port": inviter_port,
            "tls_fingerprint": inviter_fingerprint,
            "max_slots": 1,
            "capabilities": [],
            "last_seen": time.time(),
        }
    }
    root = cluster_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    certificate, private_key = certificate_paths()
    certificate.unlink(missing_ok=True)
    private_key.unlink(missing_ok=True)
    _ensure_certificate(state.node_id)
    save_cluster_state(state)
    return state


def worker_advertisement(
    state: ClusterState,
    *,
    host: str,
    port: int,
    max_slots: int,
    capabilities: list[str],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "protocol_version": CLUSTER_PROTOCOL_VERSION,
        "cluster_id": state.cluster_id,
        "worker_id": state.node_id,
        "hostname": state.hostname,
        "host": host,
        "port": int(port),
        "tls_fingerprint": certificate_fingerprint(),
        "max_slots": max(1, int(max_slots)),
        "capabilities": sorted(set(capabilities)),
        "timestamp": time.time(),
    }
    payload["signature"] = sign_payload(state, payload)
    return payload


def _valid_advertisement(
    state: ClusterState,
    payload: Mapping[str, object],
) -> bool:
    signature = str(payload.get("signature", ""))
    unsigned = dict(payload)
    unsigned.pop("signature", None)
    return (
        payload.get("cluster_id") == state.cluster_id
        and int(payload.get("protocol_version", 0)) == CLUSTER_PROTOCOL_VERSION
        and abs(time.time() - float(payload.get("timestamp", 0))) <= 30
        and verify_payload(state, unsigned, signature)
    )


class DiscoveryResponder:
    def __init__(
        self,
        *,
        port: int,
        max_slots: int,
        capabilities: list[str],
    ) -> None:
        self.api_port = int(port)
        self.max_slots = max_slots
        self.capabilities = capabilities
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.socket: Optional[socket.socket] = None

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._serve,
            name="auto-agents-worker-discovery",
            daemon=True,
        )
        self.thread.start()

    def _serve(self) -> None:
        state = load_cluster_state(required=True)
        assert state is not None
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket = sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", DISCOVERY_PORT))
        sock.settimeout(0.5)
        while not self.stop_event.is_set():
            try:
                data, address = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                query = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(query, dict) or query.get("type") != "discover":
                continue
            signature = str(query.get("signature", ""))
            unsigned = dict(query)
            unsigned.pop("signature", None)
            if (
                query.get("cluster_id") != state.cluster_id
                or abs(time.time() - float(query.get("timestamp", 0))) > 30
                or not verify_payload(state, unsigned, signature)
            ):
                continue
            response = worker_advertisement(
                state,
                host=local_lan_ip(),
                port=self.api_port,
                max_slots=self.max_slots,
                capabilities=self.capabilities,
            )
            sock.sendto(canonical_json(response), address)
        sock.close()

    def close(self) -> None:
        self.stop_event.set()
        if self.socket is not None:
            self.socket.close()
        if self.thread is not None:
            self.thread.join(timeout=2)


def discover_workers(timeout_seconds: float = 1.5) -> list[DiscoveredWorker]:
    state = load_cluster_state()
    if state is None:
        return []
    query: dict[str, object] = {
        "type": "discover",
        "cluster_id": state.cluster_id,
        "timestamp": time.time(),
        "nonce": secrets.token_hex(12),
    }
    query["signature"] = sign_payload(state, query)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", 0))
    sock.settimeout(0.15)
    encoded = canonical_json(query)
    broadcast_addresses = {"255.255.255.255", "127.255.255.255"}
    try:
        process = subprocess.run(
            ["ip", "-j", "-4", "addr", "show", "up"],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=5,
        )
        interfaces = json.loads(process.stdout) if process.returncode == 0 else []
        if isinstance(interfaces, list):
            for interface in interfaces:
                if not isinstance(interface, dict):
                    continue
                for address in interface.get("addr_info", []):
                    if isinstance(address, dict) and address.get("broadcast"):
                        broadcast_addresses.add(str(address["broadcast"]))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    for address in sorted(broadcast_addresses):
        try:
            sock.sendto(encoded, (address, DISCOVERY_PORT))
        except OSError:
            pass
    found: dict[str, DiscoveredWorker] = {}
    deadline = time.monotonic() + max(0.05, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            data, address = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not _valid_advertisement(state, payload):
            continue
        payload["host"] = address[0]
        worker = DiscoveredWorker.from_dict(payload, fallback_host=address[0])
        if worker.worker_id and worker.worker_id != state.node_id:
            found[worker.worker_id] = worker
    sock.close()
    for node_id, peer in state.peers.items():
        if node_id == state.node_id or node_id in found:
            continue
        try:
            found[node_id] = DiscoveredWorker.from_dict(peer)
        except (TypeError, ValueError):
            continue
    now = time.time()
    for worker in found.values():
        state.peers[worker.worker_id] = {
            **worker.to_dict(),
            "last_seen": now,
        }
    save_cluster_state(state)
    return list(found.values())
