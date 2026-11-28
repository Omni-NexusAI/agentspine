"""Bounded source inspection, safe even when the target cannot be imported."""
import ast
import hashlib
import os
import re
from pathlib import Path

NAME = re.compile(r'^[a-z0-9_]{1,80}$')
SKIP = {'data','tmp','logs','models','voices','node_modules','__pycache__','venv','build','dist'}
SOURCE = {'.py','.js','.mjs','.html','.css','.yaml','.md'}
MAX_FILES, MAX_BYTES, MAX_FILE = 512, 8 * 1024 * 1024, 2 * 1024 * 1024


def name(value):
    if not isinstance(value,str) or not NAME.fullmatch(value):
        raise ValueError('Select a valid plugin identifier')
    return value


def locate(roots, target):
    target = name(target)
    fallback = None
    for root in roots:
        root = Path(root).resolve()
        path = root / target
        if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
            raise ValueError('Linked plugin directories require manual review')
        if path.is_dir() and path.resolve().parent == root:
            # User storage may exist for a built-in without owning its source.
            # Match native manifest precedence, retaining a missing-manifest
            # fallback so genuinely broken installations remain diagnosable.
            manifest = path / 'plugin.yaml'
            if manifest.exists() or manifest.is_symlink():
                return path
            if fallback is None:
                fallback = path
    if fallback is not None:
        return fallback
    raise ValueError('Plugin directory not found')


def inventory(roots):
    found = set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if NAME.fullmatch(child.name) and child.is_dir() and not child.is_symlink():
                found.add(child.name)
                if len(found) >= 512:
                    return sorted(found)
    return sorted(found)


def inspect(roots, target):
    root = locate(roots,target)
    report = {'target':target, 'files':[], 'issues':[], 'bytes':0, 'truncated':False,
              'executed_target_code':False, 'read_configuration':False}
    manifest = root / 'plugin.yaml'
    if manifest.is_symlink():
        report['issues'].append({'file':'plugin.yaml','problem':'Linked manifest requires manual review'})
    elif not manifest.is_file():
        report['issues'].append({'file':'plugin.yaml','problem':'Manifest is missing'})
    for directory, folders, files in os.walk(root, followlinks=False):
        base = Path(directory)
        folders[:] = sorted(d for d in folders if d not in SKIP and not d.startswith('.')
                            and not (base/d).is_symlink()
                            and not (hasattr(base/d,'is_junction') and (base/d).is_junction())
                            and len((base/d).relative_to(root).parts) < 12)
        for filename in sorted(files):
            path = base / filename
            if filename.startswith('.') or path.suffix not in SOURCE or path.is_symlink() or not path.is_file():
                continue
            # Configurations and logs are not diagnostic source. Never return them.
            if filename in {'config.yaml','config.yml','default_config.yaml'}:
                continue
            relative = path.relative_to(root).as_posix()
            if not path.resolve().is_relative_to(root):
                continue
            size = path.stat().st_size
            if report['bytes'] + size > MAX_BYTES or len(report['files']) >= MAX_FILES:
                report['truncated'] = True
                report['passed'] = False
                return report
            if size > MAX_FILE:
                report['truncated'] = True
                continue
            with path.open('rb') as stream:
                raw = stream.read(MAX_FILE + 1)
            if len(raw) > MAX_FILE or report['bytes'] + len(raw) > MAX_BYTES:
                report['truncated'] = True
                continue
            report['bytes'] += len(raw)
            entry = {'file':relative, 'bytes':len(raw), 'sha256':hashlib.sha256(raw).hexdigest()}
            report['files'].append(entry)
            if path.suffix == '.py':
                try:
                    ast.parse(raw, filename=relative)
                except (SyntaxError, ValueError) as exc:
                    report['issues'].append({'file':relative,'line':getattr(exc,'lineno',None),
                                             'problem':'Invalid Python syntax (source not executed)'})
            if path == manifest:
                try:
                    import yaml
                    value = yaml.safe_load(raw)
                    if not isinstance(value,dict) or value.get('name') != target or not value.get('title'):
                        report['issues'].append({'file':relative,'problem':'Manifest name/title does not match the plugin'})
                except (ValueError, yaml.YAMLError):
                    report['issues'].append({'file':relative,'problem':'Invalid manifest YAML'})
    report['passed'] = not report['issues'] and not report['truncated']
    return report
