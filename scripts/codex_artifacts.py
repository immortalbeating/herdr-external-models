"""Collect explicitly selected Codex-generated images without moving originals."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

import native_records
import record_redaction

_UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
_REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


class ImageCollectionError(OSError):
    """A publication failure with receipts for files already safely retained."""
    def __init__(self, message, completed_receipts):
        super().__init__(message)
        self.completed_receipts = list(completed_receipts)


def _safe_error(exc, spec):
    message = record_redaction.redact(str(exc))
    for key, value in (spec.get('env') or {}).items():
        if re.search(r'KEY|TOKEN|PASSWORD|SECRET', key, re.I) and isinstance(value, str) and value:
            message = message.replace(value, '[REDACTED]')
    return type(exc).__name__ + ': ' + message


def _absolute(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Paths must be absolute and must not contain parent traversal')
    return path


def _safe_components(path):
    # Check every existing component, including ancestors, before resolving anything.
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & _REPARSE:
            raise ValueError('Symlinks and reparse points are not permitted')


def _inside(path, root):
    return path == root or root in path.parents


def _digest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def collect_images(job: Path, state: dict, sources: list[str | Path], destination: str | Path | None = None) -> list[dict]:
    spec = state.get('spec', {})
    mode, kind = spec.get('mode'), spec.get('kind')
    if not ((mode == 'exec' and kind in (None, 'codex')) or (mode == 'herdr' and kind == 'codex')):
        raise ValueError('Image collection requires a Codex exec or Herdr job')
    native = state.get('native_session')
    if not isinstance(native, dict) or native.get('kind') != 'id' or native.get('agent') != 'codex':
        raise ValueError('A Codex native session ID is required')
    sid = native.get('value')
    if not isinstance(sid, str) or not _UUID.fullmatch(sid):
        raise ValueError('Invalid Codex native session ID')
    if not isinstance(sources, (list, tuple)) or not sources:
        raise ValueError('At least one explicit PNG source is required')
    root = native_records.codex_home(spec.get('env')) / 'generated_images' / sid
    _safe_components(root)
    inputs = []
    for source in sources:
        path = _absolute(source)
        if not _inside(path, root) or path == root or path.suffix.lower() != '.png':
            raise ValueError('Source must be a PNG inside the exact Codex session directory')
        _safe_components(path)
        if not path.is_file():
            raise ValueError('Source PNG does not exist or is not a regular file')
        if os.path.normcase(path.name) in [os.path.normcase(p.name) for p in inputs]:
            raise ValueError('Duplicate source filename')
        inputs.append(path)
    if destination is None:
        dest = _absolute(job) / 'artifacts' / 'codex-images' / sid
    else:
        dest = _absolute(destination)
        allowed = spec.get('artifact_output_dirs')
        if not isinstance(allowed, list) or not allowed:
            raise ValueError('Explicit destination requires artifact_output_dirs')
        roots = []
        for item in allowed:
            if not isinstance(item, str):
                raise ValueError('artifact_output_dirs must contain absolute path strings')
            root_dir = _absolute(item)
            _safe_components(root_dir)
            roots.append(root_dir)
        if not any(_inside(dest, allowed_root) for allowed_root in roots):
            raise ValueError('Destination is outside artifact_output_dirs')
    _safe_components(dest)
    if dest.exists() and not dest.is_dir():
        raise ValueError('Destination is not a directory')
    prepared = []
    for source in inputs:
        target = dest / source.name
        _safe_components(target)
        sha, size = _digest(source)
        if target.exists():
            if not target.is_file():
                raise ValueError('Destination conflicts with a non-file')
            if _digest(target) != (sha, size):
                raise ValueError('Destination file differs; refusing overwrite')
        prepared.append((source, target, sha, size))
    dest.mkdir(parents=True, exist_ok=True)
    _safe_components(dest)
    receipts = []
    for source, target, sha, size in prepared:
        try:
            _safe_components(target)
            present = False
            if target.exists():
                if not target.is_file() or _digest(target) != (sha, size):
                    raise ValueError('Destination file differs; refusing overwrite')
                present = True
                receipts.append({'kind': 'codex-generated-image', 'source': str(source), 'path': str(target),
                                 'sha256': sha, 'bytes': size, 'native_session_id': sid, 'already_present': present})
            else:
                fd, temp_name = tempfile.mkstemp(prefix='.codex-image-', dir=dest)
                temp = Path(temp_name)
                try:
                    with os.fdopen(fd, 'wb') as output, source.open('rb') as input_file:
                        shutil.copyfileobj(input_file, output)
                    if _digest(temp) != (sha, size):
                        raise OSError('Copied image failed hash or size verification')
                    try:
                        os.link(temp, target)
                    except FileExistsError:
                        _safe_components(target)
                        if not target.is_file() or _digest(target) != (sha, size):
                            raise ValueError('Destination file differs; refusing overwrite')
                        present = True
                    receipts.append({'kind': 'codex-generated-image', 'source': str(source), 'path': str(target),
                                     'sha256': sha, 'bytes': size, 'native_session_id': sid, 'already_present': present})
                finally:
                    temp.unlink(missing_ok=True)
        except Exception as exc:
            raise ImageCollectionError(_safe_error(exc, spec), receipts) from exc
    return receipts
