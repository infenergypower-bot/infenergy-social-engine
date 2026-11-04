"""One service-local, consistent SQLite backup before the additive desk migration."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from datetime import datetime, timezone
from contextlib import closing


BACKUP_VERSION = 'production-desk-v1'


def backup_before_migration(data_dir: str, *, version: str = BACKUP_VERSION) -> dict:
    root=Path(data_dir).resolve()
    source=root/(os.environ.get('INVENTORY_DB_FILE') or 'inventory.db')
    if not source.is_file(): return {'status':'NO_EXISTING_DATABASE'}
    folder=root/'backups';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not version or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in version):
        raise ValueError('invalid_backup_version')
    target=folder/('before-'+version+'.sqlite')
    metadata=target.with_suffix('.json')
    lock=sqlite3.connect(str(folder/'migration-backup-lock.sqlite'),timeout=30)
    temporary=folder/(uuid.uuid4().hex+'.partial.sqlite')
    try:
        lock.execute('BEGIN IMMEDIATE')
        if target.is_file() and metadata.is_file():
            return json.loads(metadata.read_text())
        with closing(sqlite3.connect(str(source),timeout=30)) as original:
            with closing(sqlite3.connect(str(temporary))) as destination:
                original.backup(destination)
        with closing(sqlite3.connect(str(temporary))) as check:
            integrity=check.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity!='ok': raise RuntimeError('pre_migration_backup_integrity_failed')
            tables={r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            rows=check.execute('SELECT COUNT(*) FROM content_outbox').fetchone()[0] if 'content_outbox' in tables else 0
        with temporary.open('rb') as stream: sha256=hashlib.file_digest(stream,'sha256').hexdigest()
        os.chmod(temporary,0o600);os.replace(temporary,target)
        result={'status':'VERIFIED','version':version,'path':str(target),'sha256':sha256,'integrity':integrity,'outbox_records':rows,'bytes':target.stat().st_size,'created_at':datetime.now(timezone.utc).isoformat()}
        metadata.write_text(json.dumps(result,indent=2));os.chmod(metadata,0o600)
        return result
    finally:
        temporary.unlink(missing_ok=True)
        lock.rollback();lock.close()
