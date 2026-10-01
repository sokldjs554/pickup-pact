"""Published evidence must resolve to the exact retained bytes, not a dead manifest."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[2]

def test_all_published_payment_evidence_files_match_manifest():
    directory = ROOT / 'docs/media/payment'
    manifest = json.loads((directory / 'publication.json').read_text())
    assert manifest['files'], 'No published evidence files are declared.'
    for name, expected in manifest['files'].items():
        target = (directory / name).resolve()
        assert target.is_relative_to(directory.resolve()), name
        assert target.is_file(), f'Manifest references a missing evidence file: {name}'
        assert hashlib.sha256(target.read_bytes()).hexdigest() == expected, name
