#!/usr/bin/env python3
"""Development-only physical backup/PITR rehearsal on one disposable Docker host.

The runner creates every resource itself. It never accepts an existing server,
DSN, volume or cloud account to destroy. This is NOT off-host disaster recovery.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4
from demo.route.ha.backup import assert_test_resource,seal_bundle,verify_bundle

IMAGE='postgres:17.11-bookworm@sha256:639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652'


def rehearse(output:Path)->dict:
    if os.environ.get('PICKUP_HA_TEST')!='1':raise ValueError('explicit isolated rehearsal opt-in required')
    transport=os.environ.get('PICKUP_BACKUP_TRANSPORT','local')
    if transport not in {'local','restic_https_development'}:raise ValueError('unsupported backup transport')
    import psycopg
    from psycopg.conninfo import make_conninfo
    from demo.route.ha.payment_repository import PostgresPaymentRepository
    from demo.route.ha.operation_store import PostgresOperationStore
    from ha_restore_journeys import JourneyRecoveryProbe
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    run=uuid4().hex[:16];prefix='pickup-dr-'+run+'-';password=secrets.token_hex(24)
    env={**os.environ,'PGPASSWORD':password,'POSTGRES_PASSWORD':password}
    containers=[];volumes=[];repositories=[]
    network_lab=None;remote_receipt=None;archive_role='archive'
    steps=[];report={'scope':'development_single_host','run_id':run,'passed':False}
    started=time.monotonic()
    def cli(*args,timeout=120,check=True):
        result=subprocess.run(['docker',*map(str,args)],env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
        if check and result.returncode:
            # Test password is never recorded even in setup failure output.
            raise RuntimeError('Docker stage failed: '+result.stderr.replace(password,'[redacted]')[-2400:])
        return result
    def labels(role):return ['--label','pickup.rehearsal='+run,'--label','pickup.role='+role]
    def validate_resource(kind,role):
        info=json.loads(cli(kind,'inspect',prefix+role).stdout)[0]
        lab=info.get('Labels',{}) if kind=='volume' else info.get('Config',{}).get('Labels',{})
        assert_test_resource(prefix+role,lab,run,role)
    def remove_container(role):
        validate_resource('container',role)
        logs=cli('logs',prefix+role,check=False)
        (output/(role+'-postgres.log')).write_text((logs.stdout+logs.stderr).replace(password,'[redacted]'))
        cli('rm','-f',prefix+role);containers.remove(role)
    def remove_volume(role):
        validate_resource('volume',role);cli('volume','rm',prefix+role);volumes.remove(role)
    def connect(role,dbname='pickup_ha_test'):
        port=cli('port',prefix+role,'5432/tcp').stdout.strip()
        if not port.startswith('127.0.0.1:'):raise AssertionError('unexpected database endpoint')
        return make_conninfo(host='127.0.0.1',port=port.rsplit(':',1)[1],user='pickup_test',password=password,dbname=dbname,connect_timeout=2)
    def ready(role):
        dsn=connect(role);deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            try:
                with psycopg.connect(dsn,autocommit=True) as db:
                    if not db.execute('SELECT pg_is_in_recovery()').fetchone()[0]:return dsn
            except psycopg.Error:pass
            time.sleep(.2)
        raise AssertionError('database readiness or PITR target was not reached')
    def sql(dsn,statement,params=()):
        with psycopg.connect(dsn,autocommit=True) as db:
            cursor=db.execute(statement,params)
            return cursor.fetchall() if cursor.description is not None else []
    def exec_source(*args,timeout=120):return cli('exec',prefix+'source',*args,timeout=timeout)
    def snapshot_work(store):
        row=store.read('pending-capture')
        return {k:row[k] for k in ('id','order_id','request_key','fingerprint','payload','status','phase_version','lease_version')}
    try:
        if transport=='restic_https_development':
            from ha_restic_lab import ResticLab
            network_lab=ResticLab().__enter__()
        cli('pull',IMAGE)
        for role in ('data','archive','restored'):
            cli('volume','create',*labels(role),prefix+role);volumes.append(role)
        cli('run','--rm','--network','none','-v',prefix+'archive:/backup',IMAGE,
            'bash','-c','mkdir -p /backup/wal && chown -R postgres:postgres /backup')
        cli('run','-d','--name',prefix+'source',*labels('source'),'-e','POSTGRES_PASSWORD','-e','PGPASSWORD',
            '-e','POSTGRES_USER=pickup_test','-e','POSTGRES_DB=pickup_ha_test','-e','POSTGRES_INITDB_ARGS=--data-checksums',
            '-p','127.0.0.1::5432','-v',prefix+'data:/var/lib/postgresql/data','-v',prefix+'archive:/backup',IMAGE,
            'postgres','-c','wal_level=replica','-c','archive_mode=on','-c',
            'archive_command=test ! -f /backup/wal/%f && cp %p /backup/wal/%f')
        containers.append('source');dsn=ready('source')
        cluster=str(sql(dsn,'SELECT system_identifier FROM pg_control_system()')[0][0])
        timeline=sql(dsn,'SELECT timeline_id FROM pg_control_checkpoint()')[0][0]
        segment=sql(dsn,"SELECT pg_size_bytes(current_setting('wal_segment_size'))")[0][0]
        code=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
        repo=PostgresPaymentRepository(dsn,schema='pact_restore_payment');repositories.append(repo)
        # Independent database, same physical cluster and common recovery point.
        sql(dsn,'CREATE DATABASE pickup_order_test')
        orders=PostgresOperationStore(connect('source','pickup_order_test'),schema='pact_restore_orders');repositories.append(orders)
        sql(dsn,'CREATE DATABASE pickup_merchant_test')
        journey_probe=JourneyRecoveryProbe(connect('source','pickup_order_test'),connect('source','pickup_merchant_test'),repo,initialize=True)
        repositories.append(journey_probe)
        journey_probe.seed_before_backup()
        c=dict(action='AUTHORIZE',world_id=run,order_id='PCT-COMPLETED',operation_key='approve-original',authorization_id='AUTH-ORIGINAL',amount_krw=2800,currency='KRW',payment_revision=1,quote_fingerprint='a'*64,card_token='demo-approved')
        repo.execute(c)
        sql(dsn,'CREATE TABLE rehearsal_markers(id text PRIMARY KEY)')
        sql(dsn,"INSERT INTO rehearsal_markers VALUES('before-backup')")
        exec_source('pg_basebackup','-h','127.0.0.1','-U','pickup_test','-D','/backup/base','-Fp','-Xstream','--checkpoint=fast','--manifest-checksums=SHA256')
        exec_source('pg_verifybackup','/backup/base')
        steps.append('base backup passed native pg_verifybackup')
        captured={k:v for k,v in c.items() if k!='card_token'}|dict(action='CAPTURE',operation_key='capture-original')
        original_receipt=repo.execute(captured)
        p=c|dict(order_id='PCT-PENDING',operation_key='approve-pending',authorization_id='AUTH-PENDING',amount_krw=3200)
        repo.execute(p)
        pending_capture={k:v for k,v in p.items() if k!='card_token'}|dict(action='CAPTURE',operation_key='capture-pending')
        orders.enqueue('pending-capture','PCT-PENDING',{'phase':'PAY_CAPTURE','external_command':pending_capture},request_key='request-pending')
        expected_final=repo.snapshot(run,'PCT-COMPLETED');expected_pending=repo.snapshot(run,'PCT-PENDING');expected_work=snapshot_work(orders)
        expected_journeys=journey_probe.prepare_target()
        target='pact_target_'+run
        sql(dsn,'SELECT pg_create_restore_point(%s)',(target,))
        target_lsn=str(sql(dsn,'SELECT pg_current_wal_flush_lsn()')[0][0])
        # This later transaction MUST NOT appear in point-in-time restoration.
        sql(dsn,"INSERT INTO rehearsal_markers VALUES('after-target-must-not-return')")
        sql(dsn,'SELECT pg_switch_wal()')
        base_manifest=json.loads(exec_source('cat','/backup/base/backup_manifest').stdout)
        start_lsn=base_manifest['WAL-Ranges'][0]['Start-LSN']
        metadata=dict(cluster_id=cluster,source_commit=code,timeline=timeline,wal_segment_size=segment,
            start_lsn=start_lsn,target_lsn=target_lsn,scope='development_single_host')
        from demo.route.ha.backup import required_wal
        needed=required_wal(start_lsn,target_lsn,timeline=timeline,segment_bytes=segment)
        deadline=time.monotonic()+30
        for wal in needed:
            while cli('exec',prefix+'source','test','-s','/backup/wal/'+wal,check=False).returncode:
                if time.monotonic()>deadline:raise AssertionError('required archived WAL was not published')
                time.sleep(.2)
        with tempfile.TemporaryDirectory(prefix='pickup-verified-backup-') as staging:
            bundle=Path(staging)/'bundle'
            cli('cp',prefix+'source:/backup',bundle)
            seal=seal_bundle(bundle,metadata)
            verified=verify_bundle(bundle,expected_sha256=seal,expected_cluster_id=cluster)
            # Negative controls exercise the exact original bundle, then restore it.
            version=bundle/'base/PG_VERSION';original=version.read_bytes();version.write_bytes(b'0\n')
            try:verify_bundle(bundle,expected_sha256=seal,expected_cluster_id=cluster)
            except ValueError:steps.append('modified base backup rejected')
            else:raise AssertionError('corrupt backup was accepted')
            finally:version.write_bytes(original)
            wal=bundle/'wal'/needed[-1];saved=wal.with_suffix('.held');wal.rename(saved)
            try:verify_bundle(bundle,expected_sha256=seal,expected_cluster_id=cluster)
            except ValueError:steps.append('missing archived WAL rejected')
            else:raise AssertionError('incomplete WAL was accepted')
            finally:saved.rename(wal)
            verify_bundle(bundle,expected_sha256=seal,expected_cluster_id=cluster)
            (output/'backup-manifest.json').write_text(json.dumps(verified,indent=2)+'\n')
            if any('\n' in name or '\r' in name for name in verified['files']):raise AssertionError('unexpected backup filename')
            (output/'backup-sha256.txt').write_text(''.join(item['sha256']+'  '+name+'\n' for name,item in verified['files'].items()))
            if network_lab:
                remote_receipt=network_lab.archive.upload(bundle,expected_sha256=seal,expected_cluster_id=cluster)
                report['remote_negative_controls']=network_lab.negative_controls(remote_receipt,bundle,seal,cluster)
                (output/'remote-receipt.json').write_text(json.dumps(remote_receipt,indent=2)+'\n')
                steps.append('encrypted HTTPS snapshot downloaded and verified before receipt publication')
        for r in repositories:r.close()
        repositories.clear()
        recovery_started=time.monotonic()
        remove_container('source');remove_volume('data')
        assert cli('volume','inspect',prefix+'data',check=False).returncode!=0
        steps.append('original test container and data volume removed; no replica exists')
        if network_lab:
            remove_volume('archive')
            assert cli('volume','inspect',prefix+'archive',check=False).returncode!=0
            assert not bundle.exists(), 'original local copied bundle must also be gone'
            archive_role='downloaded'
            cli('volume','create',*labels(archive_role),prefix+archive_role);volumes.append(archive_role)
            with tempfile.TemporaryDirectory(prefix='pickup-https-restore-') as downloaded:
                fetched=Path(downloaded)/'bundle'
                network_lab.archive.restore(remote_receipt,fetched,expected_sha256=seal,expected_cluster_id=cluster)
                cli('run','--rm','--network','none','-v',str(fetched)+':/received:ro',
                    '-v',prefix+archive_role+':/backup',IMAGE,'bash','-c',
                    'cp -a /received/. /backup/ && chown -R postgres:postgres /backup')
            report.update(local_archive_removed=True,local_staging_removed=True,
                recovery_input='pinned_restic_snapshot_over_verified_https',
                remote_repository_id=remote_receipt['repository_id'],remote_snapshot_id=remote_receipt['snapshot_id'])
            steps.append('original archive and staging removed; downloaded encrypted network snapshot into a new volume')
        # Revalidate the exact archive volume now that its sole writer is gone.
        cli('run','--rm','--network','none','-v',prefix+archive_role+':/backup:ro','-v',str(output)+':/evidence:ro','-w','/backup',IMAGE,
            'sha256sum','--check','/evidence/backup-sha256.txt')
        steps.append('surviving archive volume matches the separately retained seal')
        # Read-only backup mount, fresh target volume. Copy only the sealed base.
        cli('run','--rm','--network','none','-v',prefix+archive_role+':/backup:ro','-v',prefix+'restored:/restore',IMAGE,
            'bash','-c','cp -a /backup/base/. /restore/ && touch /restore/recovery.signal && chown -R postgres:postgres /restore && chmod 700 /restore')
        cli('run','-d','--name',prefix+'recovery',*labels('recovery'),'-e','PGPASSWORD','-p','127.0.0.1::5432',
            '-v',prefix+'restored:/var/lib/postgresql/data','-v',prefix+archive_role+':/backup:ro',IMAGE,
            'postgres','-c','archive_mode=off','-c','restore_command=cp /backup/wal/%f %p',
            '-c','recovery_target_name='+target,'-c','recovery_target_action=promote')
        containers.append('recovery');restored_dsn=ready('recovery')
        assert str(sql(restored_dsn,'SELECT system_identifier FROM pg_control_system()')[0][0])==cluster
        assert sql(restored_dsn,'SELECT id FROM rehearsal_markers ORDER BY id')==[('before-backup',)]
        r=PostgresPaymentRepository(restored_dsn,schema='pact_restore_payment',initialize=False);repositories.append(r)
        w=PostgresOperationStore(connect('recovery','pickup_order_test'),schema='pact_restore_orders',initialize=False);repositories.append(w)
        assert r.snapshot(run,'PCT-COMPLETED')==expected_final
        assert r.snapshot(run,'PCT-PENDING')==expected_pending
        assert snapshot_work(w)==expected_work
        assert r.execute(captured)==original_receipt
        claim=w.claim_due('restored-worker')[0]
        response=r.execute(claim['payload']['external_command'])
        assert response['ok'] and w.apply_result(claim,{'phase':'COMPLETE','transaction_id':response['transaction_id']},finished=True)
        assert r.snapshot(run,'PCT-PENDING')['capture_count']==1
        assert r.snapshot(run,'PCT-PENDING')['held_krw']==0
        assert r.execute(pending_capture)==response
        steps.append('payment and work ownership restored to target; original receipts preserved')
        restored_probe=JourneyRecoveryProbe(connect('recovery','pickup_order_test'),connect('recovery','pickup_merchant_test'),r,initialize=False)
        repositories.append(restored_probe)
        journey_result=restored_probe.verify_restored(expected_journeys)
        (output/'journey-reconciliation.json').write_text(json.dumps(journey_result,ensure_ascii=False,indent=2)+'\n')
        steps.append('three application databases restored; original claim key, order, merchant and benefits reconciled')
        report.update(passed=True,source_commit=code,image=IMAGE,cluster_id=cluster,backup_digest=seal,
            source_volume_removed=True,replica_used=False,target_name=target,target_lsn=target_lsn,
            base_start_lsn=start_lsn,archived_wal=needed,completed_capture_krw=expected_final['captured_krw'],
            pending_resumed_capture_krw=3200,unexpected_after_target_rows=0,
            restore_rehearsal_seconds=round(time.monotonic()-recovery_started,3),
            application_databases=3,journey_reconciliation=journey_result,
            completion_scope='whole synthetic journey, merchant, payment and benefits via native repositories; single-host physical restore, not off-host HA')
    except Exception as exc:
        report['error']=str(exc).replace(password,'[redacted]')
        raise
    finally:
        for r in repositories:
            try:r.close()
            except Exception:pass
        cleanup=[]
        for role in list(reversed(containers)):
            try:
                logs=cli('logs',prefix+role,check=False)
                (output/(role+'-postgres.log')).write_text((logs.stdout+logs.stderr).replace(password,'[redacted]'))
                remove_container(role)
            except Exception as exc:cleanup.append(type(exc).__name__+': '+str(exc).replace(password,'[redacted]'))
        for role in list(reversed(volumes)):
            try:remove_volume(role)
            except Exception as exc:cleanup.append(type(exc).__name__+': '+str(exc).replace(password,'[redacted]'))
        if network_lab:
            try:network_lab.__exit__(None,None,None)
            except Exception as exc:cleanup.append('network repository cleanup: '+type(exc).__name__)
        report.update(backup_transport=transport,steps=steps,cleanup_errors=cleanup,elapsed_seconds=round(time.monotonic()-started,3))
        if cleanup:report['passed']=False
        (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    if not report['passed']:raise AssertionError('rehearsal cleanup failed')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--repeat',type=int,default=3)
    args=p.parse_args()
    if not 1<=args.repeat<=3:p.error('repeat must be between 1 and 3')
    for attempt in range(args.repeat):
        result=rehearse(args.output/str(attempt+1))
        print(json.dumps({k:result[k] for k in ('passed','scope','restore_rehearsal_seconds')},ensure_ascii=False))
