"""Observe real child execution without granting it host-filesystem writes.

The fixture owns the marker files. A synchronous loopback event reports that the
child reached the original observation point, even inside a private /tmp mount.
"""
import atexit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from uuid import uuid4

_paths = {}
_server = None


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        token, _, mode = self.path.lstrip('/').partition('/')
        path = _paths.get(token)
        size = int(self.headers.get('Content-Length', 0))
        if path is None or mode not in {'append', 'replace'} or not 0 <= size <= 65536:
            self.send_error(400)
            return
        value = self.rfile.read(size)
        with path.open('ab' if mode == 'append' else 'wb') as stream:
            stream.write(value)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):
        pass


class ExecutionMarker:
    def __init__(self, path):
        global _server
        if _server is None:
            _server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
            thread = threading.Thread(target=_server.serve_forever, daemon=True)
            thread.start()
            def close():
                _server.shutdown()
                _server.server_close()
                thread.join(timeout=2)
            atexit.register(close)
        self.path = path
        self.token = uuid4().hex
        _paths[self.token] = path

    def __getattr__(self, name):
        return getattr(self.path, name)

    def source(self, expression, *, append=False):
        endpoint = f'http://127.0.0.1:{_server.server_port}/{self.token}/' + ('append' if append else 'replace')
        # No project source or verification behavior is intercepted or mocked.
        return ("__import__('urllib.request', fromlist=['']).urlopen("
                f"{endpoint!r}, data=({expression}).encode(), timeout=5).close()")

    def javascript_source(self, expression, *, append=False):
        endpoint = f'http://127.0.0.1:{_server.server_port}/{self.token}/' + ('append' if append else 'replace')
        return '''import('node:http').then(({request}) => new Promise((resolve, reject) => {
            const body = Buffer.from(String((EXPRESSION)), 'utf8');
            let req, response;
            let settled = false;
            const finish = (error) => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                response?.destroy();
                req?.destroy();
                if (error) reject(error);
                else resolve();
            };
            const timer = setTimeout(() => finish(new Error('Execution marker timeout')), 5000);
            try {
                req = request(ENDPOINT, {
                    method: 'POST', headers: {'Content-Length': body.length}
                }, (res) => {
                    response = res;
                    res.on('error', finish);
                    res.on('aborted', () => finish(new Error('Execution marker response aborted')));
                    res.on('end', () => finish(res.statusCode === 200 ? undefined :
                        new Error('Execution marker HTTP ' + res.statusCode)));
                    res.resume();
                });
                req.on('error', finish);
                req.end(body);
            } catch (error) {
                finish(error);
            }
        }))'''.replace('ENDPOINT', json.dumps(endpoint)).replace('EXPRESSION', expression)
