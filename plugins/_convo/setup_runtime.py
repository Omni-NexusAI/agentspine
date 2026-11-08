"""Explicit one-time dependency setup. Activation never invokes this module."""
from pathlib import Path
import subprocess
import sys
import venv


def main():
    if sys.platform != 'linux' or '--approve-dependencies' not in sys.argv:
        raise SystemExit('Linux host and explicit --approve-dependencies are required')
    plugin = Path(__file__).resolve().parent
    base = plugin.parent.parent
    root = base.parent if base.name == 'usr' else base
    target = root / 'usr/plugins/_convo/data/runtime-env'
    if not (target / 'pyvenv.cfg').exists():
        if target.exists() and any(target.iterdir()):
            raise SystemExit('Existing non-environment directory: setup refused')
        venv.EnvBuilder(with_pip=True).create(target)
    executable = target / 'bin/python'
    requirement = 'requirements-studio.txt' if '--with-studio' in sys.argv else 'requirements.txt'
    subprocess.run([str(executable), '-m', 'pip', 'install', '--disable-pip-version-check',
                    '--no-cache-dir', '-r', str(plugin / 'sidecar' / requirement)],
                   check=True, timeout=300)
    subprocess.run([str(executable), '-c', 'import fastapi, httpx, uvicorn'], check=True)
    if '--with-studio' in sys.argv:
        subprocess.run([str(executable), '-c', 'import gradio'], check=True)
    print('Convo isolated gateway environment ready. No models installed or started.')


if __name__ == '__main__':
    main()
