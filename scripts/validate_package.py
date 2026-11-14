"""Validate distribution layout and syntax without importing Home Assistant."""
import ast
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
integration = root / 'custom_components' / 'dsppa_am8328'
manifest = json.loads((integration / 'manifest.json').read_text())
assert manifest['domain'] == 'dsppa_am8328'
assert manifest['config_flow'] is True
for field in ('version', 'documentation', 'issue_tracker', 'codeowners'):
    assert manifest[field], field
assert len(list((root / 'custom_components').iterdir())) == 1
assert (integration / 'brand' / 'icon.png').is_file()
assert (root / 'NOTICE.md').is_file()
for file in integration.rglob('*.json'):
    json.loads(file.read_text())
for file in integration.rglob('*.py'):
    ast.parse(file.read_text(), filename=str(file))
for file in integration.rglob('*'):
    assert '__pycache__' not in file.parts and not file.name.startswith('.smbdelete')
    assert file.suffix != '.pyc'
strings = json.loads((integration / 'strings.json').read_text())
assert 'config' in strings
json.loads((root / 'hacs.json').read_text())
print(f'Validated {len(list(integration.rglob("*.py")))} Python files, JSON metadata, flow strings and HACS package layout.')
