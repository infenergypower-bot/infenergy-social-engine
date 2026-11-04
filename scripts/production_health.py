"""Operational health for the reviewed workflow; legacy history stays visible."""
from __future__ import annotations
import json
import os
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
            drafts=c.execute("SELECT COUNT(*) FROM production_packages WHERE approval IS NULL AND outbox_id IS NULL AND state!='SCRAPPED'").fetchone()[0]
            deliveries=c.execute("SELECT p.payload,o.lifecycle_state,o.status FROM production_packages p JOIN content_outbox o ON o.outbox_id=p.outbox_id WHERE o.lifecycle_state NOT IN ('PUBLISHED','CANCELLED') AND o.status!='HELD'").fetchall()
        overdue=sum(datetime.fromisoformat(json.loads(r['payload'])['scheduled_at'].replace('Z','+00:00'))<now-timedelta(minutes=10) for r in deliveries)
        issues=[]
        if failed:issues.append('PRODUCTION_JOB_NEEDS_ATTENTION')
        if stalled:issues.append('PRODUCTION_QUEUE_STALLED')
        if overdue:issues.append('APPROVED_DELIVERY_OVERDUE')
        if os.environ.get('EDITORIAL_APPROVAL_MODE') == 'automatic':
            from posting_schedule import inventory_snapshot
            planned=[p for p in inventory_snapshot(data_dir) if p['lifecycle_state']!='PUBLISHED']
            ready=[p for p in planned if p['lifecycle_state'] in {'READY_TO_DISPATCH','PARTIALLY_PUBLISHED'}]
            next_post=min(planned,key=lambda p:datetime.fromisoformat(p['scheduled_at']),default=None)
            preparation=os.environ.get('CONTENT_PREGENERATION_ENABLED','false').lower() in {'1','true','yes','on'}
            if not preparation:issues.append('AUTOMATIC_PREPARATION_DISABLED')
            if not planned:issues.append('NO_UPCOMING_POSTS')
            at_risk=[p for p in planned if p not in ready and datetime.fromisoformat(p['scheduled_at'])<=now+timedelta(hours=4)]
            if at_risk:issues.append('POST_NOT_READY_BEFORE_DEADLINE')
            preparation_failures=[p for p in planned if p['lifecycle_state'] in {'FAILED_PREPARATION','QA_REJECTED','BLOCKED_CANON','BLOCKED_CONFIGURATION','BLOCKED_PROVIDER_POLICY'}]
            if preparation_failures:issues.append('AUTOMATIC_POST_NEEDS_ATTENTION')
            budget_paused=[p for p in planned if p['lifecycle_state']=='BLOCKED_BUDGET']
            budget_reset=None
            if budget_paused:
                from social.gemini_budget import budget_snapshot
                budget_reset=budget_snapshot().get('next_reset_at_utc')
                if not budget_reset or any(datetime.fromisoformat(p['scheduled_at']) < datetime.fromisoformat(budget_reset) for p in budget_paused):
                    issues.append('AI_BUDGET_BLOCKS_SCHEDULED_POST')
            return {'status':'NEEDS_ATTENTION' if issues else 'PAUSED_BUDGET' if budget_paused else 'READY' if ready else 'PREPARING',
                'issues':issues,'healthy':not issues,'automatic_preparation_enabled':preparation,
                'scheduled_posts':len(planned),'ready_posts':len(ready),'posts_at_risk':len(at_risk),
                'preparation_failures':len(preparation_failures),
                'budget_paused_posts':len(budget_paused),'budget_reset_at':budget_reset,
                'next_post':next_post,'failed_or_uncertain_jobs':failed,'stalled_jobs':stalled}
        return {'status':'NEEDS_ATTENTION' if issues else 'PAUSED_BUDGET' if paused else 'AWAITING_OWNER_REVIEW' if drafts and not deliveries else 'READY','issues':issues,'failed_or_uncertain_jobs':failed,'stalled_jobs':stalled,'budget_paused_jobs':paused,'drafts_awaiting_review':drafts,'approved_deliveries_overdue':overdue,'healthy':not issues}
    except sqlite3.Error:
        return {'status':'STARTING','issues':['PRODUCTION_DATABASE_NOT_READY'],'healthy':False}
