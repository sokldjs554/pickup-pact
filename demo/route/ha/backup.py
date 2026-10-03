"""Byte-level backup sealing and strictly scoped rehearsal guards.

A valid bundle is NOT a completed database restore, an off-host copy or an RPO
claim. The caller must run pg_verifybackup and restore into an isolated target.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

MANIFEST='pact-backup-manifest.json'
METADATA={'cluster_id','source_commit','timeline','wal_segment_size','start_lsn','target_lsn','scope'}
# The development scope is the default; an inventory-approved set of independent hosts has its own explicit label.
SCOPES={'development_single_host','oracle_single_ad_fault_domains'}


def _lsn(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9A-F]{1,8}/[0-9A-F]{1,8}',value):
        raise ValueError('invalid WAL position')
    a,b=value.split('/');return (int(a,16)<<32)+int(b,16)


def required_wal(start_lsn:str,end_lsn:str,*,timeline:int,segment_bytes:int)->list[str]:
    if (type(timeline) is not int or not 1<=timeline<=0xffffffff or type(segment_bytes) is not int
        or not 1024*1024<=segment_bytes<=1024*1024*1024 or segment_bytes&(segment_bytes-1)):
        raise ValueError('invalid timeline or WAL segment size')
    start,end=_lsn(start_lsn),_lsn(end_lsn)
    if end<=start:raise ValueError('WAL range must be positive')
    first,last=start//segment_bytes,(end-1)//segment_bytes
    if last-first>100000:raise ValueError('WAL range exceeds rehearsal bound')
    per_log=0x100000000//segment_bytes
    return [f'{timeline:08X}{i//per_log:08X}{i%per_log:08X}' for i in range(first,last+1)]


def _metadata(meta):
    if not isinstance(meta,dict) or set(meta)!=METADATA:raise ValueError('invalid backup metadata')
    if not isinstance(meta['cluster_id'],str) or not re.fullmatch('[0-9]{1,20}',meta['cluster_id']):
        raise ValueError('invalid cluster identity')
    if not isinstance(meta['source_commit'],str) or not re.fullmatch('[0-9a-f]{40}',meta['source_commit']):
        raise ValueError('source identity required')
    if meta['scope'] not in SCOPES:raise ValueError('this backup pipeline does not attest that scope')
    return required_wal(meta['start_lsn'],meta['target_lsn'],timeline=meta['timeline'],segment_bytes=meta['wal_segment_size'])


def _inventory(root:Path):
    if root.is_symlink() or not root.is_dir():raise ValueError('backup must be a regular directory')
    items={}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():raise ValueError('symlinks are not accepted in this backup layout')
        if path.is_dir():continue
        if not path.is_file():raise ValueError('special files are not backup data')
        name=path.relative_to(root).as_posix()
        if name==MANIFEST:continue
        if name.startswith('/') or '..' in PurePosixPath(name).parts:raise ValueError('unsafe backup path')
        before=path.stat();digest=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        after=path.stat()
        if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
            raise ValueError('backup changed while being inspected')
        items[name]={'size':after.st_size,'sha256':digest.hexdigest()}
    return items


def _required(root:Path,meta,files):
    for name in ('base/PG_VERSION','base/backup_manifest','base/global/pg_control'):
        if name not in files:raise ValueError('base backup is incomplete')
    if (root/'base/PG_VERSION').read_text().strip()!='17':raise ValueError('PostgreSQL version mismatch')
    for wal in _metadata(meta):
        item=files.get('wal/'+wal)
        if not item or item['size']!=meta['wal_segment_size']:raise ValueError('required archived WAL is missing or partial')


def seal_bundle(root:Path,metadata:dict)->str:
    """Seal the exact copied bytes; return a digest to store separately."""
    root=Path(root);_metadata(metadata);files=_inventory(root);_required(root,metadata,files)
    report=metadata|{'version':1,'required_wal':_metadata(metadata),'files':files}
    raw=(json.dumps(report,ensure_ascii=True,sort_keys=True,separators=(',',':'))+'\n').encode()
    target=root/MANIFEST
    if target.is_symlink():raise ValueError('manifest cannot be a symlink')
    # Never leave a half-written manifest that could be mistaken for a seal.
    temp=root/(MANIFEST+'.pending')
    owned=None
    try:
        with temp.open('xb') as f:
            stat=os.fstat(f.fileno());owned=(stat.st_dev,stat.st_ino)
            f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(temp,target)
        directory=os.open(root,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if owned and temp.exists() and not temp.is_symlink():
            stat=temp.stat()
            if (stat.st_dev,stat.st_ino)==owned:temp.unlink()
    return hashlib.sha256(raw).hexdigest()


def verify_bundle(root:Path,*,expected_sha256:str,expected_cluster_id:str)->dict:
    root=Path(root)
    try:
        if not re.fullmatch('[0-9a-f]{64}',expected_sha256):raise ValueError('trusted digest required')
        target=root/MANIFEST
        if target.is_symlink() or not target.is_file() or target.stat().st_size>16*1024*1024:raise ValueError('unsealed backup')
        raw=target.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=expected_sha256:raise ValueError('backup manifest digest mismatch')
        report=json.loads(raw)
        if set(report)!=METADATA|{'version','required_wal','files'} or report['version']!=1:
            raise ValueError('unsupported manifest')
        meta={k:report[k] for k in METADATA}
        if meta['cluster_id']!=expected_cluster_id:raise ValueError('wrong cluster backup')
        if report['required_wal']!=_metadata(meta):raise ValueError('WAL range declaration mismatch')
        current=_inventory(root);_required(root,meta,current)
        if current!=report['files']:raise ValueError('backup bytes do not match sealed inventory')
        return report
    except (OSError,TypeError,KeyError,json.JSONDecodeError) as exc:
        raise ValueError('backup cannot be verified') from None


def assert_test_resource(name:str,labels:dict,run_id:str,role:str)->None:
    """Called before stopping/removing ONLY resources created by the rehearsal."""
    if (not isinstance(run_id,str) or not re.fullmatch('[0-9a-f]{16}',run_id)
        or role not in {'source','recovery','data','archive','restored','downloaded'}
        or name!='pickup-dr-'+run_id+'-'+role
        or labels.get('pickup.rehearsal')!=run_id or labels.get('pickup.role')!=role):
        raise ValueError('not an owned isolated rehearsal resource')
