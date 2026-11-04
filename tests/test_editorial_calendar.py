from datetime import date,timedelta
import copy,json,sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import editorial_calendar as calendar
import content_operations as operations
from social import production_quality as quality


def item(day='2026-11-05',number=1):
    return {'date':day,'headline':f'Give the handoff {number}','supporting':'Make the plan useful to others',
            'statement':'A visitor cannot find the useful written plan.','expansion':'Explain the actual location and next step.',
            'action':'Ask the visitor what needs explaining.','image_scene':'A host shows a visitor the written plan beside the door.',
            'facebook':'A useful plan should be understandable to someone who did not write it. Show the location and ask what is unclear.',
            'instagram':'Show where the useful plan lives.\n\nAsk what needs explaining before assuming the handoff is complete.',
            'linkedin':'A handoff should be usable by someone who did not create it. Make the next step clear.',
            'pillar':'community_resilience','audience':'Working households','format_id':'community','product_id':''}


@pytest.fixture
def staged(tmp_path,monkeypatch):
    monkeypatch.setenv('PRODUCTION_QUALITY_ENABLED','true')
    root=str(tmp_path);operations.init_content_operations(root)
    image=tmp_path/'public_media/first.jpg';image.parent.mkdir();Image.new('RGB',(1200,1200),'#345848').save(image)
    p={'content_date':'2026-11-04','title':'Keep the finished post','routing':{'platforms':['facebook']},
       'platform_posts':{'facebook':{'final_caption':'Keep this finished approved post on its original schedule.','image_url':'https://example.com/first.jpg'}},
       'primary_publish_image_url':'https://example.com/first.jpg','gemini_generation':{'assets':[{'local_path':str(image)}]}}
    for stage in ('plan','release'):
        subject=quality.packet(p,root,include_assets=stage=='release');keys=quality.criteria_for(subject,stage)
        raw={'scores':{k:5 for k in keys},'evidence':{k:'The supplied copy and actual scene support this criterion.' for k in keys},'blockers':[]}
        p.setdefault('production_quality',{})[stage]=quality.decision(raw,subject,stage,'test-reviewer')
    operations.mark_ready(root,outbox_id='preserve-first',content_date='2026-11-04',slot='midday',scheduled_at='2026-11-04T08:05:00-06:00',decision_id='old-first',package=p)
    operations.transition_package(root,'preserve-first','READY_TO_DISPATCH','TEST_READY',actor='test')
    with operations._connect(root) as c:p=json.loads(c.execute("SELECT package_json FROM content_outbox WHERE outbox_id='preserve-first'").fetchone()[0])
    posts=[calendar.package_from_copy(item((date(2026,11,5)+timedelta(days=i)).isoformat(),i+1)) for i in range(180)]
    for batch in range(9):calendar.stage(root,{'id':'six-month-20261104','batch':batch,'posts':posts[batch*20:(batch+1)*20]})
    return root,posts,p


def test_complete_six_month_copy_is_scheduled_with_review_hold_and_preserved_first(staged,monkeypatch):
    root,posts,first=staged
    result=calendar.schedule_pending_review(root)
    assert result['scheduled_dates']==181 and result['image_calls']==result['external_calls']==0
    snap=calendar.snapshot(root)
    assert snap['text_complete']==snap['scheduled_dates']==181
    assert snap['awaiting_review']==180 and snap['ready_for_images']==0 and snap['artwork_ready']==1
    assert {r['date'] for r in snap['entries']}=={(date(2026,11,4)+timedelta(days=i)).isoformat() for i in range(181)}
    with operations._connect(root) as c:
        saved=json.loads(c.execute("SELECT package_json FROM content_outbox WHERE outbox_id='preserve-first'").fetchone()[0])
        assert saved==first
        assert c.execute("SELECT COUNT(*) FROM content_outbox WHERE lifecycle_state='BLOCKED_CONFIGURATION' AND reason_code='EXTERNAL_REVIEW_PERMISSION_REQUIRED'").fetchone()[0]==180
    import dispatch_outbox
    monkeypatch.setattr(dispatch_outbox,'_prepare_gemini_copy',lambda *a:pytest.fail('Held drafts must not leave for external review.'))
    assert dispatch_outbox.pregenerate_upcoming(data_dir=root)['status']=='IDLE'
    assert calendar.schedule_pending_review(root)['new_posts']==180
    assert calendar.snapshot(root)['scheduled_dates']==181


def test_incomplete_bank_cannot_partly_replace_the_calendar(staged):
    root,_,_=staged
    (calendar.bank_folder(root)/'batch-08.json').unlink()
    with pytest.raises(ValueError,match='coverage_incomplete'):calendar.schedule_pending_review(root)
    with operations._connect(root) as c:assert c.execute('SELECT COUNT(*) FROM content_outbox').fetchone()[0]==1


def test_external_receipt_prevents_any_bulk_replacement(staged):
    root,_,_=staged
    operations.mark_ready(root,outbox_id='sent-later',content_date='2026-11-05',slot='midday',scheduled_at='2026-11-05T09:05:00-06:00',decision_id='sent',package={'routing':{'platforms':['facebook']}})
    operations.begin_platform_transaction(root,outbox_id='sent-later',platform='facebook',payload={})
    with pytest.raises(ValueError,match='external_delivery_receipt'):calendar.schedule_pending_review(root)
    with operations._connect(root) as c:assert c.execute('SELECT COUNT(*) FROM content_outbox').fetchone()[0]==2


def test_old_unfinished_slot_is_cancelled_without_leaving_a_ready_pointer(staged):
    root,_,_=staged
    operations.mark_ready(root,outbox_id='old-placeholder',content_date='2026-11-05',slot='evening',scheduled_at='2026-11-05T19:00:00-06:00',decision_id='old-placeholder',package={'routing':{'platforms':['facebook']}})
    with operations._connect(root) as c:
        c.execute("INSERT INTO daily_slots(content_date,slot,scheduled_at,platform_policy_json,outbox_id,status,updated_at) VALUES('2026-11-05','evening','2026-11-05T19:00:00-06:00','{}','old-placeholder','READY','2026-11-03T12:00:00+00:00')");c.commit()
    calendar.schedule_pending_review(root)
    with operations._connect(root) as c:
        assert c.execute("SELECT status FROM content_outbox WHERE outbox_id='old-placeholder'").fetchone()[0]=='CANCELLED'
        row=c.execute("SELECT status,outbox_id FROM daily_slots WHERE content_date='2026-11-05' AND slot='evening'").fetchone()
        assert row['status']=='UNPLANNED' and row['outbox_id'] is None


def test_staging_rejects_path_escape_images_and_oversized_text(tmp_path):
    p=calendar.package_from_copy(item())
    with pytest.raises(ValueError,match='unknown_editorial_bank'):calendar.stage(str(tmp_path),{'id':'../../escape','batch':0,'posts':[p]})
    p['gemini_generation']['assets']=[{'local_path':'fake.jpg'}]
    with pytest.raises(ValueError,match='text_only'):calendar.stage(str(tmp_path),{'id':'six-month-20261104','batch':0,'posts':[p]})
    with pytest.raises(ValueError,match='headline_too_long'):calendar.package_from_copy({**item(),'headline':'This headline contains far too many words'})


def test_no_forged_quality_pass_can_make_unreviewed_copy_image_ready(tmp_path):
    p=calendar.package_from_copy(item())
    assert p['gemini_copy']['provider']=='editorial_bank'
    assert p['gemini_copy']['qa']['clarity']=='PENDING'
    with pytest.raises(ValueError,match='not_ready_for_images'):calendar.validate_package(p,str(tmp_path))


def test_passing_review_unlocks_only_the_reviewed_copy_and_survives_delivery_metadata(staged):
    root,posts,_=staged
    calendar.schedule_pending_review(root)
    p=posts[0];subject=quality.packet(p,root);keys=quality.criteria_for(subject,'plan')
    raw={'scores':{k:5 for k in keys},'evidence':{k:'The exact scene and copy support the stated purpose.' for k in keys},'blockers':[]}
    payload={'authorization':'owner-approved-gemini-text-review','reviews':[{'date':p['content_date'],'subject_sha256':quality.digest(subject),'model':'test-reviewer','response':raw}]}
    assert calendar.apply_reviews(root,payload)['approved']==[p['content_date']]
    with operations._connect(root) as c:
        r=c.execute('SELECT lifecycle_state,package_json FROM content_outbox WHERE content_date=? AND status=\'READY\'',(p['content_date'],)).fetchone()
        assert r['lifecycle_state']=='PREPARATION_REQUIRED';saved=json.loads(r['package_json'])
    from dispatch_outbox import _gemini_copy_ready
    assert _gemini_copy_ready(saved) and quality.current(saved,root,'plan')
    saved['platform_posts']['instagram']['image_url']='https://example.com/final.jpg'
    assert _gemini_copy_ready(saved)
    saved['platform_posts']['instagram']['final_caption']='Changed after the review.'
    assert not _gemini_copy_ready(saved) and not quality.current(saved,root,'plan')
