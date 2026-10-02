"""Interrupted or concurrent backup publication must preserve prior evidence."""
import pytest
from test_backup_guards import api,bundle


def test_existing_incomplete_seal_is_not_deleted_or_accepted(tmp_path):
    b=api();root,meta=bundle(tmp_path)
    pending=root/(b.MANIFEST+'.pending');pending.write_bytes(b'another writer or incomplete upload')
    with pytest.raises((ValueError,FileExistsError)):b.seal_bundle(root,meta)
    assert pending.read_bytes()==b'another writer or incomplete upload'
    assert not (root/b.MANIFEST).exists()
