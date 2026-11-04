"""Operational health for the reviewed workflow; legacy history stays visible."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
import sqlite3
from inventory_db import get_db_path


def snapshot(data_dir):
    now=datetime.now(timezone.utc)
    try:
        with sqlite3.connect(get_db_path(data_dir),timeout=5) as c:
            c.row_factory=sqlite3.Row
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'production_packages' not in tables:return {'status':'STARTING','issues':['PRODUCTION_WORKFLOW_INITIALIZING']}
            failed=c.execute("SELECT COUNT(*) FROM production_jobs j JOIN production_packages p ON p.id=j.package_id AND p.revision=j.revision WHERE j.state IN ('FAILED','RECONCILIATION_REQUIRED') AND datetime(j.created_at)>=datetime(?)",((now-timedelta(days=7)).isoformat(),)).fetchone()[0]
            stalled=c.execute("SELECT COUNT(*) FROM production_jobs WHERE state IN ('QUEUED','RETRY') AND datetime(available_at)<datetime(?)",((now-timedelta(minutes=5)).isoformat(),)).fetchone()[0]
            paused=c.execute("SELECT COUNT(*) FROM production_jobs WHERE state='WAITING_BUDGET'").fetchone()[0]
            drafts=c.execute("SELECT COUNT(*) FROM production_packages WHERE approval IS NULL AND outbox_id IS NULL").fetchone()[0]
            deliveries=c.execute("SELECT p.payload,o.lifecycle_state,o.status FROM production_packages p JOIN content_outbox o ON o.outbox_id=p.outbox_id WHERE o.lifecycle_state NOT IN ('PUBLISHED','CANCELLED') AND o.status!='HELD'").fetchall()
        overdue=sum(datetime.fromisoformat(json.loads(r['payload'])['scheduled_at'].replace('Z','+00:00'))<now-timedelta(minutes=10) for r in deliveries)
        issues=[]
        if failed:issues.append('PRODUCTION_JOB_NEEDS_ATTENTION')
        if stalled:issues.append('PRODUCTION_QUEUE_STALLED')
        if overdue:issues.append('APPROVED_DELIVERY_OVERDUE')
        return {'status':'NEEDS_ATTENTION' if issues else 'PAUSED_BUDGET' if paused else 'AWAITING_OWNER_REVIEW' if drafts and not deliveries else 'READY','issues':issues,'failed_or_uncertain_jobs':failed,'stalled_jobs':stalled,'budget_paused_jobs':paused,'drafts_awaiting_review':drafts,'approved_deliveries_overdue':overdue,'healthy':not issues}
    except sqlite3.Error:
        return {'status':'STARTING','issues':['PRODUCTION_DATABASE_NOT_READY'],'healthy':False}
