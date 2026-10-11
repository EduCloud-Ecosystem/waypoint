"""Authenticated, browser-mediated handoff of explicitly selected saved files."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

MAX_FILES = 32
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 500
STATIC = Path(__file__).parent / 'static'


def cairn_origin(value, allow_loopback=False):
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in value) or '\\' in value:
        raise ValueError('invalid Cairn origin')
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/') or parsed.scheme not in ('https', 'http')):
        raise ValueError('invalid Cairn origin')
    if parsed.scheme == 'http' and not (allow_loopback and parsed.hostname in ('127.0.0.1', 'localhost', '::1')):
        raise ValueError('Cairn requires HTTPS outside a synthetic loopback test')
    if not re.fullmatch(r'[a-zA-Z0-9.-]+|::1', parsed.hostname):
        raise ValueError('invalid Cairn host')
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('invalid Cairn port')
    host = '[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname
    suffix = '' if port is None or port == (443 if parsed.scheme == 'https' else 80) else ':' + str(port)
    return parsed.scheme + '://' + host + suffix


def safe_path(value, directory=False):
    if directory and value == '':
        return []
    if not isinstance(value, str) or not 1 <= len(value) <= 240 or not value.isascii():
        raise ValueError('unsupported saved file path')
    pieces = value.split('/')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*', part) for part in pieces):
        raise ValueError('unsupported saved file path')
    return pieces


def open_root(root):
    """Open every absolute directory component without following symlinks."""
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('contents root must be absolute')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_beneath(root_fd, pieces, directory=False):
    fd = os.dup(root_fd)
    try:
        for index, part in enumerate(pieces):
            is_dir = index < len(pieces) - 1 or directory
            flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if is_dir else os.O_NONBLOCK)
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def list_saved(root, directory=''):
    pieces = safe_path(directory, directory=True)
    root_fd = open_root(root)
    try:
        fd = open_beneath(root_fd, pieces, directory=True)
        try:
            entries = []
            with os.scandir(fd) as iterator:
                for count, entry in enumerate(iterator):
                    if count >= MAX_ENTRIES:
                        raise ValueError('folder exceeds the 500-entry browsing limit')
                    path = '/'.join([*pieces, entry.name])
                    try:
                        safe_path(path)
                        info = entry.stat(follow_symlinks=False)
                    except (ValueError, OSError):
                        continue
                    if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                        entries.append({'path': path, 'name': entry.name, 'directory': stat.S_ISDIR(info.st_mode), 'size': info.st_size if stat.S_ISREG(info.st_mode) else 0})
            return sorted(entries, key=lambda row: (not row['directory'], row['name']))
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


def read_saved(root, paths):
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_FILES:
        raise ValueError('select between 1 and 32 files')
    pieces = [safe_path(path) for path in paths]
    if len(set(paths)) != len(paths):
        raise ValueError('duplicate saved file paths')
    root_fd = open_root(root)
    try:
        files, total = [], 0
        for path, components in zip(paths, pieces):
            fd = open_beneath(root_fd, components)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                    raise ValueError('select regular files no larger than 1 MiB')
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
                after = os.fstat(fd)
                signature = lambda item: (item.st_size, item.st_mtime_ns, item.st_ctime_ns)
                if len(raw) > MAX_FILE_BYTES or len(raw) != before.st_size or signature(before) != signature(after):
                    raise ValueError('saved file changed while being read; save and retry')
            finally:
                os.close(fd)
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                raise ValueError('selected files exceed 4 MiB total')
            files.append({'path': path, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'content_base64': base64.b64encode(raw).decode('ascii')})
        return files
    finally:
        os.close(root_fd)


def _jupyter_server_extension_points():
    return [{'module': 'waypoint_cairn'}]


def _load_jupyter_server_extension(serverapp):
    from jupyter_server.base.handlers import APIHandler
    from jupyter_server.utils import url_path_join
    from tornado.web import authenticated, HTTPError

    raw = os.environ.get('WORKSPACE_CAIRN_ORIGIN', '')
    if not raw:
        return
    origin = cairn_origin(raw, allow_loopback=os.environ.get('WORKSPACE_CAIRN_LOCAL_TEST') == '1')
    base = url_path_join(serverapp.web_app.settings['base_url'], 'cairn')

    class BridgeHandler(APIHandler):
        def set_default_headers(self):
            super().set_default_headers()
            self.set_header('Cache-Control', 'no-store')
            self.set_header('Referrer-Policy', 'no-referrer')
            self.set_header('X-Content-Type-Options', 'nosniff')
            self.set_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")

        @authenticated
        def get(self, resource=''):
            if resource == 'config':
                return self.finish({'version': 1, 'origin': origin, 'base': base})
            if resource in ('bridge.js', 'bridge.css'):
                return self.finish((STATIC / resource).read_bytes(), set_content_type='text/javascript' if resource.endswith('.js') else 'text/css')
            if resource == 'list':
                try:
                    entries = list_saved(self.contents_manager.root_dir, self.get_query_argument('directory', ''))
                except (ValueError, OSError):
                    raise HTTPError(400, 'Folder unavailable or unsupported (maximum 500 entries).') from None
                return self.finish({'entries': entries})
            if resource:
                raise HTTPError(404)
            # Issue Jupyter's existing XSRF cookie; no auth value enters page HTML.
            self.xsrf_token
            self.finish((STATIC / 'index.html').read_text(), set_content_type='text/html; charset=utf-8')

        @authenticated
        def post(self, resource=''):
            if resource != 'files':
                raise HTTPError(404)
            if len(self.request.body) > 16384:
                raise HTTPError(413)
            body = self.get_json_body()
            if not isinstance(body, dict) or set(body) != {'paths'}:
                raise HTTPError(400, 'Expected selected saved file paths.')
            try:
                files = read_saved(self.contents_manager.root_dir, body['paths'])
            except (ValueError, OSError):
                raise HTTPError(400, 'Files unavailable or unsupported; save and select again (32 files, 1 MiB each, 4 MiB total).') from None
            self.finish({'files': files})

    serverapp.web_app.add_handlers('.*$', [(base + r'/(.*)', BridgeHandler)])
