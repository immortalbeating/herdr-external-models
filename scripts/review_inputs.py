import hashlib
import json
import os
import stat
import time


def validate_paths(paths):
    if not isinstance(paths, list) or not paths:
        raise ValueError('paths must be a nonempty list')
    result, seen = [], set()
    for path in paths:
        if not isinstance(path, str) or not path.strip() or not os.path.isabs(path):
            raise ValueError('paths must contain nonempty absolute file paths')
        # The Windows extended-length prefix includes a literal question mark.
        wildcard_part = path[4:] if path.startswith('\\\\?\\') else path
        if '*' in wildcard_part or '?' in wildcard_part:
            raise ValueError('wildcard paths are unsupported')
        path = os.path.normpath(path)
        key = os.path.normcase(path)
        if key in seen:
            continue
        current = path
        while current and current != os.path.dirname(current):
            if os.path.islink(current) or (hasattr(os.path, 'isjunction') and os.path.isjunction(current)):
                raise ValueError('links and reparse points are unsupported')
            try:
                attributes = getattr(os.lstat(current), 'st_file_attributes', 0)
            except FileNotFoundError:
                attributes = 0
            if attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                raise ValueError('links and reparse points are unsupported')
            current = os.path.dirname(current)
        if os.path.lexists(path) and not os.path.isfile(path):
            raise ValueError('path is not a regular file')
        result.append(path)
        seen.add(key)
    return result


def _read(path):
    validate_paths([path])
    try:
        before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return {'path': path, 'exists': False, 'sha256': None, 'size': None}
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('path is not a regular file')
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        opened = os.fstat(f.fileno())
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
        after_open = os.fstat(f.fileno())
    after = os.stat(path, follow_symlinks=False)
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if (signature(before) != signature(after) or signature(opened) != signature(after_open)
            or signature(before)[:4] != signature(opened)[:4]):
        raise OSError('file changed while reading')
    return {'path': path, 'exists': True, 'sha256': h.hexdigest(), 'size': before.st_size}


def _digest(files):
    payload = json.dumps(files, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def snapshot(paths):
    start = time.monotonic()
    files = [_read(path) for path in validate_paths(paths)]
    return {'algorithm': 'sha256', 'files': files, 'digest': _digest(files),
            'elapsed_ms': round((time.monotonic() - start) * 1000, 3)}


def check(saved):
    start = time.monotonic()
    changed, errors, scope = [], [], 0
    try:
        if not isinstance(saved, dict) or saved.get('algorithm') != 'sha256' or not isinstance(saved.get('files'), list) or not saved['files']:
            raise ValueError('invalid snapshot structure')
        files = saved['files']
        paths = [f['path'] for f in files if isinstance(f, dict) and 'path' in f]
        if len(paths) != len(files) or validate_paths(paths) != paths:
            raise ValueError('invalid snapshot paths')
        scope = len(files)
        for item in files:
            exists, digest, size = item.get('exists'), item.get('sha256'), item.get('size')
            if type(exists) is not bool or (exists and (not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest) or type(size) is not int or size < 0)) or (not exists and (digest is not None or size is not None)):
                raise ValueError('invalid file record')
            if set(item) != {'path', 'exists', 'sha256', 'size'}:
                raise ValueError('invalid file record')
        if saved.get('digest') != _digest(files):
            raise ValueError('snapshot digest mismatch')
        for item in files:
            try:
                if _read(item['path']) != item:
                    changed.append(item['path'])
            except (OSError, ValueError) as exc:
                errors.append({'path': item['path'], 'reason': type(exc).__name__})
    except (ValueError, TypeError, KeyError, OSError) as exc:
        errors.append({'path': '', 'reason': str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    return {'status': 'unavailable' if errors else 'changed' if changed else 'match',
            'changed': changed, 'errors': errors, 'scope_files': scope,
            'elapsed_ms': round((time.monotonic() - start) * 1000, 3)}
