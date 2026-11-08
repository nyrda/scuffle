#!/usr/bin/env python3
"""Stand in for GitHub's releases while a packaged Scuffle updates itself, for the packaging checks.

Usage: self_update_server.py PORT VERSION ASSET [--wrong-sum]

The checks build a Scuffle whose addresses point here. --wrong-sum publishes a checksum that does not fit.
"""
import hashlib
import http.server
import json
from pathlib import Path
import sys

port, version, asset = int(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
data = asset.read_bytes()
digest = '0' * 64 if '--wrong-sum' in sys.argv else hashlib.sha256(data).hexdigest()
name = asset.name
base = f'http://127.0.0.1:{port}/download/v{version}'
files = {f'/download/v{version}/{name}': data, f'/download/v{version}/SHA256SUMS': f'{digest}  {name}\n'.encode()}
files['/latest'] = json.dumps({
    'tag_name': f'v{version}', 'draft': False, 'prerelease': False,
    'html_url': f'https://github.com/nyrda/scuffle/releases/tag/v{version}',
    'assets': [{'name': name, 'size': len(data), 'browser_download_url': f'{base}/{name}'},
               {'name': 'SHA256SUMS', 'size': 100, 'browser_download_url': f'{base}/SHA256SUMS'}]}).encode()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = files.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.send_header('Content-Length', str(len(body or b'')))
        self.end_headers()
        self.wfile.write(body or b'')


http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
