"""Finished text banks, independently reviewed before image generation."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from posting_schedule import growth_schedule
from social import production_quality as quality
from social.scene_message import SCENE_MESSAGE_VERSION
from social.type_design import resolve as type_design


def package_from_copy(item, product=None):
    """Build the same strict Gemini contract consumed by the scheduled renderer."""
    required=('date','headline','supporting','statement','expansion','action','image_scene','facebook','instagram','linkedin','pillar','audience','format_id')
    if any(not isinstance(item.get(k),str) or not item[k].strip() for k in required):raise ValueError('calendar_copy_incomplete')
    day=date.fromisoformat(item['date'])
    if len(item['headline'])>36 or len(item['headline'].split())>5:raise ValueError('calendar_headline_too_long')
    if len(item['supporting'])>48 or len(item['supporting'].split())>7:raise ValueError('calendar_supporting_too_long')
    if any(len(item[p])>5000 for p in ('facebook','instagram','linkedin')):raise ValueError('calendar_caption_too_long')
    if item['format_id'] not in {'humor','community','cinematic_brand_poster','product_proof_story'}:raise ValueError('calendar_requires_one_image_format')
    if bool(product)!=bool(item.get('product_id')) or (product and product['product_id']!=item['product_id']):raise ValueError('calendar_product_source_mismatch')
    if item['format_id']=='product_proof_story' and not product:raise ValueError('calendar_product_required')
    design=type_design(item.get('typography_design'),format_id=item['format_id'],lines=[item['headline'],item['supporting']])
    contract={'format':item['format_id'],'audience_name':item['audience'],'creative_territory':item['pillar'],
              'visible_text':{'headline':item['headline'],'infenergy_line':item['supporting'],'resolution_line':''},'typography_design':design}
    thought={'id':'BANK-'+day.isoformat(),'kind':'reviewed_editorial','format':'single','pillar':item['pillar'],
             'statement':item['statement'],'expansion':item['expansion'],'action':item['action'],
             'overlay_text':item['headline'],'image_scene':item['image_scene'],'visual_motif':item['image_scene'],
             'generation_contract':contract,'visual_execution':item.get('visual_execution') or 'scene-led editorial'}
    from build_monthly_content import _gemini_generation_plan
    generation=_gemini_generation_plan(thought,product)
    schedule={p:t for p,t in growth_schedule(day).items() if p in ('facebook','instagram')}
    identifier=hashlib.sha256(('six-month-20261104:'+day.isoformat()).encode()).hexdigest()[:24]
    output_sha=hashlib.sha256(json.dumps(item,sort_keys=True,ensure_ascii=True).encode()).hexdigest()
    p={'post_id':identifier,'content_id':identifier,'content_date':day.isoformat(),'title':item['headline'],
       'topic':item['statement'],'brief':item['statement']+' '+item['expansion'],'thought_statement':item['statement'],
       'weekly_role':'reviewed_six_month_editorial','content_type':'editorial','editorial_pillar':item['pillar'],
       'generation_thought':thought,'generation_contract':contract,'gemini_generation':generation,
       'routing':{'platforms':['facebook','instagram']},'platforms':['facebook','instagram'],'platform_schedule':schedule,
       'platform_posts':{platform:{'platform':platform,'caption':item[platform],'final_caption':item[platform],
                                  'content_format':'single_image_product' if product else 'single_image_thought',
                                  'final_caption_qa':{'status':'TEXT_COMPLETE_REVIEW_PENDING','provider':'editorial_bank','clarity':'PENDING'}} for platform in ('facebook','instagram','linkedin')},
       'fb_caption':item['facebook'],'ig_caption':item['instagram'],'li_text':item['linkedin'],
       'gemini_copy':{'provider':'editorial_bank','strict_provider':True,'fallback_allowed':False,'status':'COMPLETE',
                     'model_output_sha256':output_sha,'scene_message_version':SCENE_MESSAGE_VERSION,
                     'generated_at_utc':datetime.now(timezone.utc).isoformat(),
                     'qa':{'schema':'PASS','clarity':'PENDING','product_claims':'PENDING'}},
       'provider_policy':{'creative_provider':'gemini'},'product_id':item.get('product_id') or '',
       'product_name':product['name'] if product else '',
       'product_verified_facts':product.get('verified_facts',[]) if product else [],
       'product_image_url':product.get('source_image_url','') if product else '',
       'product_role':item.get('product_role') or ('Compare documented fit before use.' if product else ''),
       'reference_image_urls':[],'references':[],'unique_creative_required':True,
       'editorial_bank':{'id':'six-month-20261104','copy_status':'FINAL','image_generation':'DEFERRED',
                         'source_model':item.get('source_model','Codex'),'copy_sha256':output_sha}}
    if product:
        p['product_source_sha256']=quality.digest(product)
    return p


def copy_digest(p):
    subject={k:p.get(k) for k in ('generation_thought','generation_contract','fb_caption','ig_caption','li_text','product_id','product_verified_facts')}
    subject['captions']={k:v.get('final_caption') or v.get('caption') for k,v in (p.get('platform_posts') or {}).items()}
    return quality.digest(subject)


def finish_plan_review(p,receipt):
    p.setdefault('production_quality',{})['plan']=receipt
    if receipt.get('status')=='PASS' and receipt.get('review_complete') is True:
        p['gemini_copy']['qa'].update(clarity='PASS',product_claims='PASS')
        for post in p['platform_posts'].values():post['final_caption_qa'].update(status='PRESENTATION_READY',clarity='PASS')
        p['editorial_bank']['review_status']='PLAN_PASSED'
        p['editorial_bank']['reviewed_copy_sha256']=copy_digest(p)
    return p


def apply_reviews(data_dir,payload):
    """Import actual reviewer decisions after owner permission; never invent a pass."""
    if payload.get('authorization')!='owner-approved-gemini-text-review':raise ValueError('explicit_external_review_permission_required')
    from content_operations import _connect,_json,_record_transition
    rows=payload.get('reviews')
    if not isinstance(rows,list) or not 1<=len(rows)<=30:raise ValueError('invalid_editorial_review_batch')
    bank={p['content_date']:p for p in staged_posts(data_dir)};approved=[];blocked=[]
    prepared=[]
    for item in rows:
        p=bank[item['date']]
        subject=quality.packet(p,data_dir)
        if item.get('subject_sha256')!=quality.digest(subject):raise ValueError('editorial_review_subject_changed')
        receipt=quality.decision(item.get('response'),subject,'plan',item.get('model') or 'unknown')
        if not receipt['review_complete']:raise ValueError('editorial_review_incomplete')
        finish_plan_review(p,receipt)
        if receipt['status']=='PASS':validate_package(p,data_dir);approved.append(p['content_date'])
        else:blocked.append(p['content_date'])
        prepared.append(p)
    connection=_connect(data_dir)
    try:
        connection.execute('BEGIN IMMEDIATE')
        for p in prepared:
            identifier='bank_'+p['content_id']
            row=connection.execute('SELECT status,lifecycle_state FROM content_outbox WHERE outbox_id=?',(identifier,)).fetchone()
            if not row or row['status']!='READY' or row['lifecycle_state']!='BLOCKED_CONFIGURATION':raise ValueError('editorial_post_no_longer_held_for_review')
            if connection.execute("SELECT 1 FROM platform_transactions WHERE outbox_id=? AND state IN ('REQUEST_SENT','CONFIRMED_SUCCESS','AMBIGUOUS')",(identifier,)).fetchone():raise ValueError('editorial_post_has_external_receipt')
            connection.execute('UPDATE content_outbox SET package_json=? WHERE outbox_id=?',(_json(p),identifier))
            if p['content_date'] in approved:_record_transition(connection,outbox_id=identifier,destination='PREPARATION_REQUIRED',reason_code='REVIEWED_COPY_READY_FOR_IMAGE_GENERATION',actor='editorial_calendar')
        connection.commit()
    except Exception:connection.rollback();raise
    finally:connection.close()
    return {'approved':approved,'blocked':blocked,'image_calls':0,'external_calls':0}


def validate_package(p,data_dir):
    from dispatch_outbox import _gemini_copy_ready
    from publication_authorization import assert_not_scrapped
    assert_not_scrapped(p,data_dir)
    if not _gemini_copy_ready(p) or not quality.current(p,data_dir,'plan'):raise ValueError('calendar_post_not_ready_for_images')
    r=p['production_quality']['plan']
    rebuilt=quality.decision({k:r.get(k) for k in ('scores','evidence','blockers')},quality.packet(p,data_dir),'plan',r.get('model'))
    if rebuilt['status']!='PASS' or r.get('review_complete') is not True:raise ValueError('calendar_review_incomplete_or_blocked')
    generation=p.get('gemini_generation') or {}
    if generation.get('required_image_count')!=1 or generation.get('assets'):raise ValueError('calendar_import_must_be_text_only')
    if p.get('product_id'):
        from build_monthly_content import _load_product_brief
        product=_load_product_brief(data_dir,p['product_id'])
        if p.get('product_source_sha256')!=quality.digest(product) or p.get('product_verified_facts')!=product.get('verified_facts',[]):raise ValueError('calendar_product_source_changed')
        approval=p.get('product_image_approval') or {}
        if approval.get('product_id')!=p['product_id'] or approval.get('source_url')!=product.get('source_image_url') or not re.fullmatch('[a-f0-9]{64}',approval.get('sha256','')):raise ValueError('calendar_product_reference_not_approved')
    if p.get('platform_schedule')!={k:v for k,v in growth_schedule(p['content_date']).items() if k in ('facebook','instagram')}:raise ValueError('calendar_platform_schedule_mismatch')
    return True


def bank_folder(data_dir,identifier='six-month-20261104'):
    if identifier!='six-month-20261104':raise ValueError('unknown_editorial_bank')
    return Path(data_dir)/'editorial_banks'/identifier


def stage(data_dir,payload):
    """Store finished local copy. This route never calls an external model."""
    identifier=payload.get('id');folder=bank_folder(data_dir,identifier)
    batch=payload.get('batch');posts=payload.get('posts')
    if type(batch) is not int or not 0<=batch<9 or not isinstance(posts,list) or not 1<=len(posts)<=20:raise ValueError('invalid_editorial_stage_batch')
    dates=[]
    for p in posts:
        day=date.fromisoformat(p['content_date'])
        if not date(2026,11,5)<=day<=date(2027,5,3):raise ValueError('editorial_date_outside_campaign')
        if p.get('editorial_bank',{}).get('id')!=identifier or p.get('gemini_copy',{}).get('provider')!='editorial_bank':raise ValueError('editorial_source_required')
        if p.get('gemini_generation',{}).get('assets') or p.get('gemini_generation',{}).get('required_image_count')!=1:raise ValueError('text_only_single_image_plan_required')
        if not p.get('fb_caption') or not p.get('ig_caption') or not p.get('generation_thought',{}).get('image_scene'):raise ValueError('finished_copy_and_scene_required')
        lines=p.get('generation_contract',{}).get('visible_text',{})
        if not lines.get('headline') or len(lines['headline'])>36 or len(lines['headline'].split())>5 or not lines.get('infenergy_line') or len(lines['infenergy_line'])>48 or len(lines['infenergy_line'].split())>7:raise ValueError('editorial_image_text_limits')
        dates.append(day.isoformat())
    if len(set(dates))!=len(dates):raise ValueError('duplicate_editorial_date')
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    target=folder/f'batch-{batch:02d}.json';temporary=target.with_suffix('.partial')
    temporary.write_text(json.dumps(posts,ensure_ascii=False),encoding='utf-8');temporary.replace(target)
    return {'status':'STAGED_TEXT_ONLY','batch':batch,'posts':len(posts),'external_calls':0,'image_calls':0}


def staged_posts(data_dir):
    folder=bank_folder(data_dir)
    posts=[p for f in sorted(folder.glob('batch-*.json')) for p in json.loads(f.read_text(encoding='utf-8'))]
    if len({p['content_date'] for p in posts})!=len(posts):raise ValueError('duplicate_editorial_date_across_batches')
    return sorted(posts,key=lambda p:p['content_date'])


def schedule_pending_review(data_dir):
    """Atomically date the text bank, with a hard hold before external review."""
    from content_operations import init_content_operations,_connect,_json,_record_transition
    from production_backup import backup_before_migration
    init_content_operations(data_dir)
    posts=staged_posts(data_dir)
    expected={(date(2026,11,5)+timedelta(days=i)).isoformat() for i in range(180)}
    if {p['content_date'] for p in posts}!=expected:raise ValueError('six_month_text_coverage_incomplete')
    backup=backup_before_migration(data_dir,version='six-month-calendar-20261104')
    connection=_connect(data_dir);stamp=datetime.now(timezone.utc).isoformat()
    try:
        connection.execute('BEGIN IMMEDIATE')
        first=connection.execute("SELECT package_json,outbox_id FROM content_outbox WHERE content_date='2026-11-04' AND status='READY' AND lifecycle_state='READY_TO_DISPATCH'").fetchall()
        if len(first)!=1 or not quality.ready(json.loads(first[0]['package_json']),data_dir):raise ValueError('preserved_approved_first_post_required')
        existing=connection.execute("SELECT * FROM content_outbox WHERE content_date BETWEEN '2026-11-05' AND '2027-05-03' AND status NOT IN ('CANCELLED','HELD')").fetchall()
        for row in existing:
            current=json.loads(row['package_json'])
            if (current.get('editorial_bank') or {}).get('id')=='six-month-20261104':continue
            if row['status'] in {'PUBLISHED','CLAIMED','PUBLISHING'} or connection.execute("SELECT 1 FROM platform_transactions WHERE outbox_id=? AND state IN ('REQUEST_SENT','CONFIRMED_SUCCESS','AMBIGUOUS')",(row['outbox_id'],)).fetchone():raise ValueError('cannot_replace_external_delivery_receipt')
            if quality.ready(current,data_dir):raise ValueError('another_finished_approved_post_requires_preservation')
            source=row['lifecycle_state']
            if source=='AWAITING_QA':
                _record_transition(connection,outbox_id=row['outbox_id'],destination='QA_REJECTED',reason_code='SUPERSEDED_BY_FINISHED_TEXT_BANK',actor='editorial_calendar');source='QA_REJECTED'
            if source in {'QA_REJECTED','PREPARING'}:
                _record_transition(connection,outbox_id=row['outbox_id'],destination='FAILED_PREPARATION',reason_code='SUPERSEDED_BY_FINISHED_TEXT_BANK',actor='editorial_calendar')
            _record_transition(connection,outbox_id=row['outbox_id'],destination='CANCELLED',reason_code='REPLACED_BY_SIX_MONTH_FINISHED_COPY',actor='editorial_calendar')
            connection.execute("UPDATE content_outbox SET status='CANCELLED' WHERE outbox_id=?",(row['outbox_id'],))
            connection.execute("UPDATE daily_slots SET content_id=NULL,decision_id=NULL,outbox_id=NULL,status='UNPLANNED',last_error=NULL,updated_at=? WHERE outbox_id=?",(stamp,row['outbox_id']))
            connection.execute("UPDATE asset_reservations SET reservation_state='RELEASED',updated_at=? WHERE outbox_id=? AND reservation_state='RESERVED'",(stamp,row['outbox_id']))
        for p in posts:
            identifier='bank_'+p['content_id'];decision='bank_'+p['content_date'];day=p['content_date']
            due=min(p['platform_schedule'].values(),key=datetime.fromisoformat)
            found=connection.execute('SELECT package_json FROM content_outbox WHERE outbox_id=?',(identifier,)).fetchone()
            if found:
                if json.loads(found['package_json'])!=p:raise ValueError('editorial_activation_idempotency_conflict')
                continue
            connection.execute("INSERT INTO council_sessions VALUES(?,?,?,'READY',?,?,?,?)",(decision,day,'midday',_json({'source':'six_month_finished_copy','title':p['title']}),_json(['finished_local_editorial_copy','external_review_permission_pending']),stamp,stamp))
            connection.execute("INSERT INTO content_candidates VALUES(?,?,1,'SELECTED',NULL,'[]',?,?)",(p['content_id'],decision,_json(p),stamp))
            connection.execute("INSERT INTO content_outbox(outbox_id,content_id,decision_id,content_date,slot,scheduled_at,package_json,status,created_at,ready_at,lifecycle_state,reason_code,readiness_json,correlation_id) VALUES(?,?,?,?,'midday',?,?,'READY',?,?,'DRAFT','PACKAGE_CREATED','{}',?)",(identifier,p['content_id'],decision,day,due,_json(p),stamp,stamp,identifier))
            _record_transition(connection,outbox_id=identifier,destination='PREPARATION_REQUIRED',reason_code='SIX_MONTH_COPY_COMPLETE',actor='editorial_calendar')
            _record_transition(connection,outbox_id=identifier,destination='BLOCKED_CONFIGURATION',reason_code='EXTERNAL_REVIEW_PERMISSION_REQUIRED',actor='editorial_calendar')
            connection.execute("INSERT INTO daily_slots(content_date,slot,scheduled_at,platform_policy_json,content_id,decision_id,outbox_id,status,ready_at,updated_at) VALUES(?,'midday',?,?,?,?,?,'READY',?,?) ON CONFLICT(content_date,slot) DO UPDATE SET scheduled_at=excluded.scheduled_at,platform_policy_json=excluded.platform_policy_json,content_id=excluded.content_id,decision_id=excluded.decision_id,outbox_id=excluded.outbox_id,status='READY',ready_at=excluded.ready_at,claimed_at=NULL,published_at=NULL,last_error=NULL,updated_at=excluded.updated_at",(day,due,_json({'platforms':['facebook','instagram']}),p['content_id'],decision,identifier,stamp,stamp))
        connection.commit()
        return {'status':'SCHEDULED_AWAITING_REVIEW_PERMISSION','new_posts':180,'preserved_posts':1,'scheduled_dates':181,'start_date':'2026-11-04','end_date':'2027-05-03','backup':backup,'image_calls':0,'external_calls':0}
    except Exception:
        connection.rollback();raise
    finally:connection.close()


def snapshot(data_dir):
    from content_operations import _connect
    posts=staged_posts(data_dir)
    if not posts:return {'available':False,'entries':[]}
    connection=_connect(data_dir)
    try:
        first=connection.execute("SELECT package_json,lifecycle_state FROM content_outbox WHERE content_date='2026-11-04' AND status NOT IN ('CANCELLED','HELD') ORDER BY created_at DESC LIMIT 1").fetchone()
        active={r['content_date']:dict(r) for r in connection.execute("SELECT content_date,lifecycle_state,package_json FROM content_outbox WHERE content_date BETWEEN '2026-11-05' AND '2027-05-03' AND status NOT IN ('CANCELLED','HELD')")}
    finally:connection.close()
    pairs=[]
    if first:pairs.append((json.loads(first['package_json']),first['lifecycle_state']))
    pairs.extend((json.loads(active[p['content_date']]['package_json']),active[p['content_date']]['lifecycle_state']) if p['content_date'] in active and (json.loads(active[p['content_date']]['package_json']).get('editorial_bank') or {}).get('id')=='six-month-20261104' else (p,'STAGED') for p in posts)
    entries=[]
    for p,state in pairs:
        generation=p.get('gemini_generation') or {};prompts=generation.get('prompts') or [];direction=prompts[0].get('v5_direction',{}) if prompts else {}
        passed=quality.current(p,data_dir,'plan')
        entries.append({'date':p['content_date'],'title':p.get('title') or (direction.get('gemini_rendered_text') or ['Scheduled post'])[0],
                        'facebook':p.get('fb_caption') or (p.get('platform_posts',{}).get('facebook') or {}).get('final_caption',''),
                        'instagram':p.get('ig_caption') or (p.get('platform_posts',{}).get('instagram') or {}).get('final_caption',''),
                        'image_text':direction.get('gemini_rendered_text',[]),'scene':(p.get('generation_thought') or {}).get('image_scene',''),
                        'typography':direction.get('typography_design'),'product':p.get('product_name') or '',
                        'format':(p.get('generation_contract') or {}).get('format') or p.get('content_type'),
                        'schedule':p.get('platform_schedule',{}),'state':state,'plan_passed':passed,'artwork_ready':bool(generation.get('assets'))})
    return {'available':True,'id':'six-month-20261104','start_date':'2026-11-04','end_date':'2027-05-03','entries':entries,
            'text_complete':len(entries),'awaiting_review':sum(not e['plan_passed'] for e in entries),
            'ready_for_images':sum(e['plan_passed'] and not e['artwork_ready'] for e in entries),
            'artwork_ready':sum(e['artwork_ready'] for e in entries),'scheduled_dates':sum(e['state']!='STAGED' for e in entries)}
