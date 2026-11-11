#!/usr/bin/env python3
"""Save a replacement key locally, hidden from terminal output and git."""
import getpass
import json
import os
from pathlib import Path

root = Path(__file__).resolve().parent
path = root / 'config.local.json'
config = json.loads((path if path.exists() else root / 'config.example.json').read_text())
key = getpass.getpass('New API key (hidden; do not reuse the key pasted in chat): ').strip()
if not key:
    raise SystemExit('No key saved.')
config['azure_api_key' if config.get('provider') == 'nyu_gateway' else 'api_key'] = key
os.umask(0o077)
temporary = path.with_name('config.credentials.local.json')
with temporary.open('w', encoding='utf-8') as stream:
    os.chmod(temporary, 0o600)
    json.dump(config, stream, indent=2)
    stream.write('\n')
os.replace(temporary, path)
print('Saved to ignored config.local.json with owner-only permissions. No API request sent.')
