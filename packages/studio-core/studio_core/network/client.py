"""Only the egress service opens IP sockets. Workers use a filesystem Unix socket."""
import base64
import hashlib
import http.client
import json
import socket
from dataclasses import dataclass


class NetworkError(RuntimeError):
    pass


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


@dataclass(frozen=True)
class NetworkResponse:
    status: int
    content_type: str
    body: bytes
    sha256: str


class NetworkClient:
    def __init__(self, config):
        self.config = config

    def get(self, url):
        request = json.dumps({'url': url}).encode()
        connection = UnixHTTPConnection(self.config.proxy_socket, self.config.network_timeout + 2)
        try:
            connection.request('POST', '/fetch', request, {'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(self.config.network_max_bytes * 2 + 8193)
            if len(raw) > self.config.network_max_bytes * 2 + 8192:
                raise NetworkError('Invalid proxy response size')
            value = json.loads(raw)
            if response.status != 200:
                raise NetworkError(value.get('error', 'PROXY_REJECTED'))
            body = base64.b64decode(value['body'], validate=True)
            digest = hashlib.sha256(body).hexdigest()
            if len(body) > self.config.network_max_bytes or digest != value['sha256']:
                raise NetworkError('Proxy response integrity failed')
            if not 200 <= value['status'] < 300:
                raise NetworkError('UPSTREAM_HTTP_' + str(value['status']))
            return NetworkResponse(value['status'], value['content_type'], body, digest)
        except (OSError, ValueError, KeyError, http.client.HTTPException) as exc:
            # Do not emit exception strings containing URLs or credentials.
            raise NetworkError(type(exc).__name__) from None
        finally:
            connection.close()
