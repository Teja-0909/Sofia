"""Tests never inherit production credentials or notebook state."""
import os
import socket

import pytest

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
for name in (
    'BOT_TOKEN', 'GEMINI_API_KEY', 'GROQ_API_KEY', 'OPENROUTER_API_KEY',
    'TURSO_DATABASE_URL', 'TURSO_AUTH_TOKEN', 'TOGETHER_API_KEY',
    'HF_TOKEN', 'HUGGINGFACE_API_KEY', 'GITHUB_TOKEN',
    'BROWSER_WORKER_URL', 'BROWSER_WORKER_TOKEN',
):
    os.environ[name] = ''


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    """Local HTTP integration tests are allowed; public services require mocks."""
    original = socket.getaddrinfo

    def local_only(host, *args, **kwargs):
        if host not in ('localhost', '127.0.0.1', '::1', None, b'localhost', b'127.0.0.1'):
            raise AssertionError(f'Live network is disabled in tests: {host}')
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', local_only)


@pytest.fixture(autouse=True)
def isolated_notebook(monkeypatch, tmp_path):
    from app import memory_file
    monkeypatch.setattr(memory_file, 'MEMORY_FILE_PATH', tmp_path / 'memory.md')
    monkeypatch.setattr(memory_file, '_CACHED_MEMORY_MD', None)
