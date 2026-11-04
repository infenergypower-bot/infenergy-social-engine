from __future__ import annotations
import copy
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import content_operations as outbox
import dispatch_outbox as dispatch
from social import post_candidates as candidates, production_quality as quality
from social.gemini_budget import GeminiBudgetExceeded


@pytest.fixture
def pipeline(tmp_path,monkeypatch):
    monkeypatch.setenv('EDITORIAL_APPROVAL_MODE','automatic')
    monkeypatch.setenv('PRODUCTION_QUALITY_ENABLED','true')
    root=str(tmp_path)
    outbox.init_content_operations(root)
    due=(datetime.now(timezone.utc)+timedelta(hours=12)).isoformat()
    p={'post_id':'candidate-test','routing':{'platforms':['facebook']},
       'platform_schedule':{'facebook':due},'platform_posts':{'facebook':{}},
       'gemini_copy':{'provider':'gemini','strict_provider':True,'fallback_allowed':False,'status':'PENDING'},
       'generation_thought':{'visual_motif':'People helping each other'},'generation_contract':{'format':'humor'},
       'gemini_generation':{'provider':'gemini','strict_provider':True,'fallback_allowed':False,'required_image_count':1,'prompts':[],'assets':[]}}
    outbox.mark_ready(root,outbox_id='slot-one',content_date=due[:10],slot='midday',scheduled_at=due,decision_id='decision-one',package=p)
    calls={'copy':0,'image':0,'plan':0,'release':0,'preflights':[],'fail_plans':2,'fail_release':0}
    monkeypatch.setattr(dispatch,'_hydrate_product_image_approval',lambda p:p)
    monkeypatch.setattr(dispatch,'_gemini_copy_ready',lambda p:p['gemini_copy'].get('status')=='COMPLETE')
    monkeypatch.setattr(dispatch,'_gemini_assets_ready',lambda p,*_:bool(p['gemini_generation'].get('assets')))
    monkeypatch.setattr(dispatch,'_delivery_media_ready',lambda p:bool(p['gemini_generation'].get('assets')))
    monkeypatch.setattr(quality,'current',lambda p,root,stage:(p.get('production_quality',{}).get(stage) or {}).get('status')=='PASS')
    monkeypatch.setattr(quality,'ready',lambda p,root:all(quality.current(p,root,s) for s in ('plan','release')))
    def author(p,*_):
        if dispatch._gemini_copy_ready(p):return p
        calls['copy']+=1
        p['generation_thought'].update(statement=f'New premise {calls["copy"]}',image_scene=f'A different scene {calls["copy"]}')
        p['gemini_copy']['status']='COMPLETE'
        if p.get('candidate_recovery'):p['candidate_recovery']['status']='REPLACEMENT_DRAFTED'
        return p
    def review(p,root,stage):
        if quality.current(p,root,stage):return p['production_quality'][stage]
        calls[stage]+=1
        fail=calls[stage]<=calls['fail_plans' if stage=='plan' else 'fail_release']
        receipt={'status':'BLOCKED' if fail else 'PASS','review_complete':True,'blockers':[{'criterion':'meaning','issue':'The idea has no useful payoff.','fix':'Choose a different human situation.'}] if fail else []}
        p.setdefault('production_quality',{})[stage]=receipt
        return receipt
    def image(p,*_):
        assert quality.current(p,root,'plan')
        calls['image']+=1
        p['gemini_generation']['assets']=[{'local_path':f'candidate-{calls["copy"]}.jpg'}]
        return p
    monkeypatch.setattr(dispatch,'_prepare_gemini_copy',author)
    monkeypatch.setattr(quality,'review',review)
    monkeypatch.setattr(dispatch,'_prepare_gemini_assets',image)
    monkeypatch.setattr(dispatch,'preflight_gemini_workflow',lambda **kw:calls['preflights'].append(kw))
    monkeypatch.setattr(dispatch,'evaluate_outbox_readiness',lambda *a,**k:{'ready':True,'state':'READY_TO_DISPATCH','reason_codes':['READY']})
    for module in (dispatch.publish_facebook,dispatch.publish_instagram,dispatch.publish_linkedin):
        monkeypatch.setattr(module,'publish',lambda *a,**k:pytest.fail('Preparation must never publish ahead of the scheduled dispatcher.'),raising=False)
    def next_tick():
        row=outbox.ready_package_for_preparation(root,'slot-one')
        p=row['package']
        if p.get('candidate_recovery'):
            p['candidate_recovery']['next_candidate_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
            outbox.update_ready_package(root,'slot-one',p)
        return dispatch.pregenerate_upcoming(data_dir=root,outbox_id='slot-one')
    return root,due,calls,next_tick


def test_rejected_posts_continue_until_pass_without_images_for_failed_plans(pipeline):
    root,due,calls,tick=pipeline
    for number in (2,3):
        result=tick()
        assert result['status']=='REPLACEMENT_QUEUED' and result['candidate_number']==number
        assert calls['image']==0
    assert tick()['status']=='PREGENERATED'
    row=outbox.ready_package_for_preparation(root,'slot-one')
    assert row['lifecycle_state']=='READY_TO_DISPATCH'
    assert row['scheduled_at']==due and row['package']['platform_schedule']['facebook']==due
    assert len(row['package']['candidate_recovery']['rejected'])==2
    assert calls['copy']==3 and calls['image']==1 and calls['release']==1
    assert tick()['status']=='IDLE'
    assert calls['image']==1


def test_failed_finished_post_returns_to_new_concept_not_image_repair(pipeline):
    root,_,calls,tick=pipeline
    calls.update(fail_plans=0,fail_release=1)
    assert tick()['status']=='REPLACEMENT_QUEUED'
    p=outbox.ready_package_for_preparation(root,'slot-one')['package']
    assert not p['gemini_generation']['assets'] and p['gemini_copy']['status']=='PENDING'
    assert p['candidate_recovery']['rejected'][0]['assets']==[{'local_path':'candidate-1.jpg'}]
    assert tick()['status']=='PREGENERATED'
    assert calls['copy']==2 and calls['plan']==2 and calls['image']==2


def test_plan_screening_works_with_zero_image_budget_and_resumes_without_redrafting(pipeline,monkeypatch):
    root,_,calls,tick=pipeline
    calls['fail_plans']=0
    def budget(**kw):
        if kw['image_calls']:raise GeminiBudgetExceeded('image budget exhausted')
    monkeypatch.setattr(dispatch,'preflight_gemini_workflow',budget)
    assert tick()['status']=='BLOCKED_BUDGET'
    assert calls['copy']==1 and calls['plan']==1 and calls['image']==0
    p=outbox.ready_package_for_preparation(root,'slot-one')['package']
    assert p['production_quality']['plan']['status']=='PASS'
    monkeypatch.setattr(dispatch,'preflight_gemini_workflow',lambda **kw:None)
    assert tick()['status']=='PREGENERATED'
    assert calls['copy']==1 and calls['image']==1


def test_provider_failure_does_not_become_a_creative_retry_loop(pipeline,monkeypatch):
    root,_,calls,_=pipeline
    monkeypatch.setattr(quality,'review',lambda *a:(_ for _ in ()).throw(RuntimeError('provider unavailable')))
    assert dispatch.pregenerate_upcoming(data_dir=root)['status']=='FAILED_PREPARATION'
    assert dispatch.pregenerate_upcoming(data_dir=root)['status']=='IDLE'
    assert calls['copy']==1 and calls['image']==0


def test_incomplete_review_does_not_discard_a_post(pipeline,monkeypatch):
    root,_,_,tick=pipeline
    monkeypatch.setattr(quality,'review',lambda *a:{'status':'BLOCKED','review_complete':False})
    assert tick()['status']=='FAILED_PREPARATION'
    assert not outbox.ready_package_for_preparation(root,'slot-one')['package'].get('candidate_recovery')


def test_previous_image_rejection_survives_budget_pause_and_starts_a_new_post(pipeline):
    root,_,calls,tick=pipeline
    outbox.transition_package(root,'slot-one','PREPARING','TEST',actor='test')
    outbox.transition_package(root,'slot-one','FAILED_PREPARATION','PREPARATION_FAILED',actor='test',detail={'error':'RuntimeError:gemini_generation_failed:semantic_quality_rejected:misspelled_text'})
    outbox.transition_package(root,'slot-one','PREPARATION_REQUIRED','RETRY',actor='test')
    outbox.transition_package(root,'slot-one','PREPARING','TEST',actor='test')
    outbox.transition_package(root,'slot-one','BLOCKED_BUDGET','AI_BUDGET_EXHAUSTED',actor='test')
    assert tick()['status']=='REPLACEMENT_QUEUED'
    p=outbox.ready_package_for_preparation(root,'slot-one')['package']
    assert p['candidate_recovery']['candidate_number']==2
    assert 'misspelled_text' in p['candidate_recovery']['rejected'][0]['review']['blockers'][0]['issue']
    assert calls['copy']==0 and calls['image']==0


@pytest.mark.parametrize('state',['REQUEST_SENT','CONFIRMED_SUCCESS','AMBIGUOUS'])
def test_atomic_replacement_preserves_posts_with_external_delivery_receipts(pipeline,state):
    root,_,_,_=pipeline
    p=outbox.ready_package_for_preparation(root,'slot-one')['package']
    outbox.transition_package(root,'slot-one','PREPARING','TEST',actor='test')
    outbox.begin_platform_transaction(root,outbox_id='slot-one',platform='facebook',payload={})
    if state!='REQUEST_SENT':outbox.complete_platform_transaction(root,outbox_id='slot-one',platform='facebook',state=state,external_id='external-one')
    new=candidates.replace(p,candidates.CandidateQualityError('plan',{'status':'BLOCKED'}))
    assert not outbox.replace_rejected_candidate(root,'slot-one',p,new)
    assert outbox.platform_transaction(root,'slot-one','facebook')['state']==state


def test_replacement_preserves_facts_and_rejected_pixels_but_clears_approvals():
    p={'product_id':'approved-panel','product_verified_facts':['200W nameplate only'],
       'generation_thought':{'statement':'Old premise','image_scene':'Old scene'},
       'gemini_copy':{'provider':'gemini','strict_provider':True,'fallback_allowed':False,'status':'COMPLETE'},
       'gemini_generation':{'required_image_count':1,'assets':[{'local_path':'original.jpg'}]},
       'production_quality':{'release':{'status':'BLOCKED'}},'primary_publish_image_url':'https://example.com/original.jpg'}
    old=copy.deepcopy(p)
    new=candidates.replace(p,candidates.CandidateQualityError('release',p['production_quality']['release']))
    assert p==old
    assert new['product_verified_facts']==p['product_verified_facts'] and new['product_id']=='approved-panel'
    assert 'production_quality' not in new and 'primary_publish_image_url' not in new
    assert new['candidate_recovery']['rejected'][0]['assets']==p['gemini_generation']['assets']
    with pytest.raises(candidates.CandidateQualityError):candidates.require_fresh_post(new,{'statement':'OLD premise','image_scene':'Different scene'})
    assert 'different complete post' in candidates.replacement_brief(new)['instruction']


def test_scrapped_candidate_cannot_be_replaced(pipeline):
    root,_,_,_=pipeline
    p=outbox.ready_package_for_preparation(root,'slot-one')['package'];p['scrapped']=True
    with pytest.raises(ValueError,match='scrapped'):
        outbox.replace_rejected_candidate(root,'slot-one',p,candidates.replace(p,candidates.CandidateQualityError('plan',{})))


@pytest.mark.parametrize('stage',['quality_plan','quality'])
def test_durable_creative_worker_queues_a_new_post_after_rejection(tmp_path,monkeypatch,stage):
    from production_workflow import Production
    import production_worker
    monkeypatch.setenv('EDITORIAL_APPROVAL_MODE','automatic')
    monkeypatch.setenv('PRODUCTION_QUALITY_ENABLED','true')
    monkeypatch.setattr('social.gemini_budget.preflight_gemini_workflow',lambda **kw:None)
    store=Production(str(tmp_path))
    item=store.create({'brief':'A useful story about preparedness','format':'humor'})
    item=store.revise(item['id'],{'concept':{'human_moment':'An old situation'},'captions':{'instagram':'Original caption.'},'image_text':['Original headline']},item['revision'])
    store.queue(item['id'],stage,item['revision'])
    def review(payload,root,review_stage):
        receipt={'status':'BLOCKED' if stage=='quality_plan' or review_stage=='release' else 'PASS','review_complete':True,'blockers':[{'criterion':'meaning','issue':'The post has no worthwhile point.','fix':'Choose a different premise.'}]}
        payload.setdefault('production_quality',{})[review_stage]=receipt
        return receipt
    monkeypatch.setattr(quality,'review',review)
    assert production_worker.run_one(store,'test-worker')
    updated=store.get(item['id'])
    assert not updated['payload']['concept'] and not updated['payload']['assets']
    assert updated['payload']['candidate_recovery']['candidate_number']==2
    with store.db() as c:
        queued=c.execute("SELECT * FROM production_jobs WHERE state='QUEUED'").fetchone()
        assert queued['stage']=='concept' and queued['revision']==updated['revision']
        assert 'quality_plan' in queued['next_stages']
    assert store.claim('too-soon') is None
