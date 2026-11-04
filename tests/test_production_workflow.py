from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from production_workflow import Conflict, Production
from production_worker import run_one, schedule_stage, visual_stage
from production_policy import resolve
from publication_authorization import authorize
from production_typography import render


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.setenv('EDITORIAL_APPROVAL_MODE','required')
    monkeypatch.setenv('PUBLIC_BASE_URL','https://social.example.com')
    monkeypatch.setenv('META_IG_USER_ID','approved-account')
    monkeypatch.setenv('CONTENT_DISPATCH_ENABLED','true')
    monkeypatch.setenv('ENABLE_INSTAGRAM','true')
    return Production(str(tmp_path))


def asset(store, name='frame.jpg', color='#183224'):
    target=Path(store.data_dir)/'public_media'/name
    target.parent.mkdir(exist_ok=True)
    Image.new('RGB',(1080,1350),color).save(target)
    record=store.asset_record(str(target.relative_to(store.data_dir)))
    record['inspection']={'sha256':record['sha256'],'expected':[],'ocr':{'status':'PASS'}}
    return record


def complete(store):
    item=store.create({'brief':'A relatable charger hunt with a knowing payoff','format':'humor'})
    item=store.revise(item['id'],{'concept':{'human_moment':'The family charger disappears','hook':'A familiar mystery','payoff':'The one cable everyone needs','visual_direction':'A lived-in kitchen','brain_movement':'Recognize the shared habit','heart_response':'Affectionate recognition'},'captions':{'instagram':'Some household mysteries have a very short list of suspects.'},'assets':[asset(store)],'destination_accounts':{'instagram':'approved-account'},'scheduled_at':'2030-01-03T12:00:00+00:00'},item['revision'])
    return store.revise(item['id'],{'review':{k:True for k in ['brand','caption','visual','mobile','canon','truth']}},item['revision'])


def approved_package(store):
    item=complete(store);item=store.approve(item['id'],item['revision'])
    job={'package_id':item['id'],'revision':item['revision']}
    result=schedule_stage(store,job,item['payload'])
    with store.db() as c:
        payload=json.loads(c.execute('SELECT package_json FROM content_outbox WHERE outbox_id=?',(result['outbox_id'],)).fetchone()[0])
    return item,payload,result


def test_edits_invalidate_approval_and_stale_edit_is_rejected(store):
    item=complete(store);item=store.approve(item['id'],item['revision'])
    changed=store.revise(item['id'],{'title':'New premise'},item['revision'])
    assert changed['approval'] is None
    assert changed['payload']['review']=={}
    with pytest.raises(Conflict):store.revise(item['id'],{'title':'Stale overwrite'},item['revision'])
    with pytest.raises(ValueError,match='approval_required'):store.queue(changed['id'],'schedule',changed['revision'])


def test_each_historical_revision_is_kept(store):
    item=complete(store)
    with store.db() as c:
        assert c.execute('SELECT COUNT(*) FROM production_revisions WHERE package_id=?',(item['id'],)).fetchone()[0]==item['revision']


def test_schedule_is_idempotent_and_exact_package_is_authorized(store):
    item,payload,result=approved_package(store)
    authorize(payload,'instagram',data_dir=store.data_dir)
    assert schedule_stage(store,{'package_id':item['id'],'revision':item['revision']},item['payload'])==result
    with store.db() as c:
        assert c.execute('SELECT COUNT(*) FROM content_outbox').fetchone()[0]==1
        row=c.execute('SELECT readiness_json FROM content_outbox').fetchone()[0]
        assert json.loads(row)['ready'],row


@pytest.mark.parametrize('change',['caption','account','image','adapter_caption','adapter_image','carousel'])
def test_publish_guard_rejects_changed_delivery(store,monkeypatch,change):
    item,payload,_=approved_package(store)
    if change=='caption':payload['platform_posts']['instagram']['final_caption']='Changed caption'
    elif change=='account':monkeypatch.setenv('META_IG_USER_ID','another-account')
    elif change=='image':Image.new('RGB',(1080,1350),'red').save(store.local_asset(item['payload']['assets'][0]['path']))
    elif change=='adapter_caption':payload['ig_caption']='Changed caption'
    elif change=='adapter_image':payload['primary_publish_image_url']='https://social.example.com/media/other.png'
    else:payload['carousel_assets'][0]['public_url']='https://social.example.com/media/other.png'
    with pytest.raises(ValueError):authorize(payload,'instagram',data_dir=store.data_dir)


def test_guard_runs_before_network_for_all_six_adapters(store,monkeypatch):
    import importlib
    for name,args in [('facebook',({},'')),('instagram',({},)),('linkedin',({},'')),('wordpress',({},)),('youtube',({},)),('tiktok',({},))]:
        module=importlib.import_module('publish_'+name)
        with pytest.raises(ValueError,match='editorial_approval_required'):
            module.publish(*args,dry_run=False)


def test_only_one_worker_claims_job(store):
    p=store.create({'brief':'one idea','format':'humor'});store.queue(p['id'],'concept',p['revision'])
    with ThreadPoolExecutor(max_workers=5) as pool:claims=list(pool.map(lambda n:store.claim(str(n)),range(5)))
    assert sum(c is not None for c in claims)==1


def test_paid_job_expiry_is_not_blindly_retried(store):
    p=store.create({'brief':'one idea','format':'humor'});q=store.queue(p['id'],'visual',p['revision'])
    job=store.claim('dead-worker',lease_seconds=-1)
    assert store.claim('another-worker') is None
    with store.db() as c:assert c.execute('SELECT state FROM production_jobs WHERE id=?',(q['job_id'],)).fetchone()[0]=='RECONCILIATION_REQUIRED'
    with pytest.raises(Conflict,match='lease_lost'):store.finish(job,updates={'concept':{}})
    with pytest.raises(ValueError):store.retry_job(q['job_id'])
    store.retry_job(q['job_id'],acknowledge_possible_spend=True)
    assert store.claim('owner-approved-retry')['attempts']==2


def test_receipt_recovery_does_not_call_provider(store,monkeypatch):
    item=store.create({'brief':'one idea','format':'humor'});q=store.queue(item['id'],'concept',item['revision'])
    path=Path(store.data_dir)/'production_receipts'/(q['job_id']+'.json');path.parent.mkdir()
    path.write_text(json.dumps({'revision':item['revision'],'updates':{'concept':{'hook':'Recovered result'}},'result':None}))
    monkeypatch.setattr('production_worker.text_stage',lambda *args:pytest.fail('provider called again'))
    assert run_one(store)
    assert store.get(item['id'])['payload']['concept']['hook']=='Recovered result'


def test_failed_job_is_visible_and_retry_is_explicit(store,monkeypatch):
    item=store.create({'brief':'one idea','format':'humor'});q=store.queue(item['id'],'concept',item['revision'])
    monkeypatch.setattr('production_worker.text_stage',Mock(side_effect=RuntimeError('secret provider token')))
    run_one(store)
    job=store.snapshot()['jobs'][0]
    assert job['state']=='FAILED' and job['error']=='RuntimeError'
    store.retry_job(q['job_id'],acknowledge_possible_spend=True)
    monkeypatch.setattr('production_worker.text_stage',lambda *args:{'concept':{'hook':'A repaired premise'}})
    run_one(store)
    assert store.get(item['id'])['payload']['concept']['hook']=='A repaired premise'


def test_reference_and_asset_paths_cannot_escape_data_directory(store,tmp_path):
    with pytest.raises(ValueError):store.asset_record('../../etc/passwd')
    outside=tmp_path.parent/'outside.png';Image.new('RGB',(32,32)).save(outside)
    (tmp_path/'escape.png').symlink_to(outside)
    with pytest.raises(ValueError):store.asset_record('escape.png')


def test_character_and_product_canon_are_required(store):
    item=store.create({'brief':'Hero uses a battery in a family scene','format':'product_intervention'})
    errors=store.checks(item['id'])['errors']
    assert 'character_canon_required' in errors
    assert 'product_identity_role_evidence_and_reference_required' in errors


def test_missing_metrics_are_unknown_and_measurements_retain_source(store):
    item=store.create({'brief':'test','format':'humor'})
    store.observe(item['id'],{'platform':'instagram','window_hours':24,'source':'owner export','collected_at':'2030-01-01T10:00:00Z','metrics':{'reach':100,'shares':0}})
    obs=store.snapshot()['observations'][0]
    assert obs['rates']['shares_per_reach']==0
    assert obs['rates']['saves_per_reach'] is None
    assert obs['source']=='owner export'
    with pytest.raises(ValueError):store.observe(item['id'],{'platform':'instagram','window_hours':24,'collected_at':'2030-01-01T10:00:00Z','source':'export','metrics':{'reach':float('nan')}})


def test_cost_estimate_never_invents_prices(store):
    item=store.create({'brief':'test','format':'infenergy_micro_mission'})
    estimate=store.estimate(item['id'])
    assert estimate['estimated_usd'] is None
    assert estimate['image_attempts_planned']==9
    assert estimate['minimum_image_budget_days']>=1
    assert store.estimate(item['id'],{'image_usd':.1,'text_call_usd':.01,'source':'test rates'})['estimated_usd']==.92


def test_typography_fits_and_rejects_unreadable_or_overlapping_text(store):
    source=store.local_asset(asset(store)['path']);target=Path(store.data_dir)/'type.png'
    receipt=render(source,target,[{'text':'STAY CONNECTED','rect':[.06,.70,.88,.23]}])
    assert receipt['layers'][0]['font_size']>=43
    assert receipt['ocr']['status']=='PASS',receipt
    with pytest.raises(ValueError,match='overlaps'):render(source,target,[{'text':'Hello','rect':[.06,.7,.88,.23]}],protected=[[.06,.7,.3,.2]])
    with pytest.raises(ValueError,match='does_not_fit'):render(source,target,[{'text':'A long story '*20,'rect':[.06,.70,.3,.1]}])
    with pytest.raises(ValueError,match='finite'):render(source,target,[{'text':'Hello','rect':[float('nan'),.7,.3,.1]}])


def test_final_pixel_inspection_is_required_after_text_edit(store):
    item=complete(store)
    changed=store.revise(item['id'],{'image_text':['NEW WORDS']},item['revision'])
    assert 'image_text_changed_reinspect_pixels' in store.checks(changed['id'])['errors']
    store.queue(changed['id'],'inspect',changed['revision']);run_one(store)
    changed=store.get(item['id'])
    assert changed['payload']['assets'][0]['inspection']['ocr']['status']=='REVIEW_REQUIRED'
    assert 'ocr_exception_explanation_required' in store.checks(item['id'])['errors']


def test_brand_policy_allows_entertainment_and_integrated_scene_text():
    rules=resolve('superhero_text_integration')
    assert rules['typography']=='integrated'
    assert 'checkout_first' in rules['legacy_conflicts_superseded']
    with pytest.raises(ValueError):resolve('superhero_text_integration',overrides={'typography':'controlled'})


def test_api_conflicts_and_static_workspace(store):
    from social_engine.intelligence_os.web import handle
    item=store.create({'brief':'one idea','format':'humor'})
    status,_,_=handle('PATCH','/api/os/production/packages/'+item['id'],{'revision':999,'updates':{'title':'stale'}},store.data_dir)
    assert status==409
    for path in ['/os','/os/assets/advanced.html','/os/assets/production.js','/os/assets/production.css']:
        assert handle('GET',path,None,store.data_dir)[0]==200


def test_budget_mutex_recovers_after_process_is_killed(tmp_path):
    path=str(tmp_path/'ledger.json');ready=tmp_path/'ready'
    code="import sys,time;sys.path.insert(0,'scripts');from social.gemini_budget import _ledger_lock;from pathlib import Path\nwith _ledger_lock(sys.argv[1]):\n Path(sys.argv[2]).write_text('ready')\n time.sleep(60)"
    proc=subprocess.Popen([sys.executable,'-c',code,path,str(ready)],cwd=Path(__file__).resolve().parents[1])
    import time
    try:
        deadline=time.monotonic()+10
        while not ready.exists() and time.monotonic()<deadline:time.sleep(.05)
        assert ready.exists();proc.kill();proc.wait(timeout=5)
        from social.gemini_budget import _ledger_lock
        with _ledger_lock(path):pass
    finally:
        if proc.poll() is None:proc.kill();proc.wait()


def test_frame_repair_preserves_other_frames_and_sends_reference_bytes(store,monkeypatch):
    from types import SimpleNamespace
    from google import genai
    item=store.create({'brief':'A coherent four-frame consumer story','format':'educational_story_carousel'})
    frames=[asset(store,'first.png','#112233'),asset(store,'second.png','#445566')]
    item=store.revise(item['id'],{'concept':{'visual_direction':'A coherent kitchen scene'},'assets':frames},item['revision'])
    ref=store.register_reference({'kind':'brand','name':'Brand reference','canon':'Keep these colors','files':[frames[0]['path']]})
    item=store.revise(item['id'],{'references':[ref]},item['revision'])
    buffer=BytesIO();Image.new('RGB',(1080,1350),'#889977').save(buffer,'PNG')
    response=SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=buffer.getvalue(),mime_type='image/png'))]))],usage_metadata=None)
    call=Mock(return_value=response);monkeypatch.setattr(genai,'Client',lambda **kwargs:SimpleNamespace(models=SimpleNamespace(generate_content=call)))
    monkeypatch.setenv('GEMINI_API_KEY','synthetic-key')
    changed=visual_stage(store,{'stage':'visual:1','package_id':item['id'],'id':'repair-job'},item['payload'])
    assert changed['assets'][0]==frames[0]
    assert changed['assets'][1]['path']!=frames[1]['path']
    assert call.call_count==1
    assert len(call.call_args.kwargs['contents'])==4  # prompt, canon bytes, continuity label and frame bytes
    from social.gemini_budget import budget_snapshot
    assert budget_snapshot()['image']['used']==1


def test_budget_exhaustion_waits_without_consuming_a_provider_attempt(store,monkeypatch):
    from social.gemini_budget import reserve_gemini_call
    monkeypatch.setenv('GEMINI_DAILY_CALL_LIMIT','1')
    reserve_gemini_call('reasoning','test','already used')
    item=store.create({'brief':'new concept','format':'humor'});store.queue(item['id'],'concept',item['revision'])
    monkeypatch.setattr('production_worker.text_stage',lambda *args:pytest.fail('paid call made despite exhausted budget'))
    run_one(store)
    job=store.snapshot()['jobs'][0]
    assert job['state']=='WAITING_BUDGET' and job['attempts']==0
    assert store.claim('next-worker') is None


def test_corrupt_budget_ledger_fails_closed(store,monkeypatch,tmp_path):
    from social.gemini_budget import budget_snapshot,GeminiBudgetExceeded
    path=tmp_path/'corrupt.json';path.write_text('{invalid json')
    monkeypatch.setenv('GEMINI_USAGE_PATH',str(path))
    with pytest.raises(GeminiBudgetExceeded):budget_snapshot()


def test_growth_scheduler_cannot_move_an_approved_post(store):
    from content_operations import apply_growth_schedule_to_ready_inventory
    item,payload,result=approved_package(store)
    assert apply_growth_schedule_to_ready_inventory(store.data_dir,start_date='2029-01-01')['updated']==0
    with store.db() as c:assert c.execute('SELECT scheduled_at FROM content_outbox WHERE outbox_id=?',(result['outbox_id'],)).fetchone()[0]==item['payload']['scheduled_at']


@pytest.mark.parametrize('platform,endpoint',[('facebook','https://graph.test/123/feed'),('instagram','https://graph.test/123/media_publish'),('linkedin','https://api.test/ugcPosts')])
def test_final_publication_server_errors_are_never_blindly_retried(monkeypatch,platform,endpoint):
    import importlib
    import requests
    module=importlib.import_module('publish_'+platform)
    response=Mock(status_code=500,ok=False)
    post=Mock(return_value=response);monkeypatch.setattr(module.requests,'post',post)
    with pytest.raises(requests.Timeout):
        if platform=='linkedin':module._post_with_retry(endpoint,headers={},json_payload={})
        else:module._post_with_retry(endpoint,{})
    assert post.call_count==1


def test_required_mode_does_not_schedule_legacy_generators(monkeypatch):
    import worker
    monkeypatch.setenv('EDITORIAL_APPROVAL_MODE','required')
    monkeypatch.setenv('CONTENT_PREGENERATION_ENABLED','true')
    monkeypatch.setenv('AUTONOMOUS_AI_ENABLED','true')
    monkeypatch.setenv('CONTENT_FACTORY_ENABLED','true')
    worker.register_scheduled_jobs()
    functions={job.job_func.func.__name__ for job in worker.schedule.jobs}
    assert '_start_pregeneration_thread' not in functions
    assert '_start_factory_thread' not in functions
    assert 'resume_deferred_run' not in functions


def test_caption_prompt_reads_real_brand_sources_and_format_examples(store,monkeypatch):
    from production_worker import text_stage
    from social import model_router
    item=complete(store)
    store.add_example({'format':'humor','verdict':'excellent','explanation':'An earned, knowing payoff','content':'A witty household observation'})
    provider=Mock(return_value={'captions':{'instagram':'A fresh caption that adds a knowing payoff.'}})
    monkeypatch.setattr(model_router,'generate_json',provider)
    result=text_stage(store,{'stage':'caption','package_id':item['id']},item['payload'])
    prompt=json.loads(provider.call_args.args[1])
    assert 'data/marketing/founder_brand_manifesto.json' in prompt['owner_sources']
    assert 'data/marketing/human_truth/constitution.json' in prompt['owner_sources']
    assert prompt['examples'][0]['explanation']=='An earned, knowing payoff'
    assert prompt['locked_concept']['hook']==item['payload']['concept']['hook']
    assert result['captions']['instagram'].startswith('A fresh caption')


def test_full_draft_sequence_is_atomic_and_stops_before_approval(store,monkeypatch):
    item=store.create({'brief':'A relatable family charger mystery','format':'humor'})
    store.queue(item['id'],'draft',item['revision'])
    def text(s,j,p):
        if j['stage']=='concept':return {'concept':{'human_moment':'A household ritual','hook':'Who took it?','payoff':'A knowing smile','visual_direction':'Kitchen','brain_movement':'Recognition','heart_response':'Belonging','text_layers':[[]]},'image_text':[]}
        return {'captions':{'instagram':'Some mysteries solve themselves at the dinner table.'}}
    monkeypatch.setattr('production_worker.text_stage',text)
    monkeypatch.setattr('production_worker.visual_stage',lambda s,j,p:{'assets':[asset(s)]})
    stages=[]
    while run_one(store):
        stages=[j['stage'] for j in reversed(store.snapshot()['jobs'])]
        assert len(stages)<10
    result=store.get(item['id'])
    assert stages==['concept','caption','visual','typography','inspect']
    assert result['approval'] is None and result['outbox_id'] is None
    assert result['state']=='REVIEW'
    assert all(j['state']=='SUCCEEDED' for j in store.snapshot()['jobs'])


@pytest.mark.parametrize('platform',['facebook','instagram','linkedin'])
def test_reviewed_carousel_reaches_each_adapter_with_all_frames_and_exact_caption(store,monkeypatch,platform):
    from types import SimpleNamespace
    import dispatch_outbox
    monkeypatch.setenv('META_PAGE_ID','fb-approved')
    monkeypatch.setenv('LINKEDIN_ORGANIZATION_URN','urn:li:organization:approved')
    for name in ['FACEBOOK','INSTAGRAM','LINKEDIN']:monkeypatch.setenv('ENABLE_'+name,'true')
    item=store.create({'brief':'A four-frame useful discovery','format':'educational_story_carousel','platforms':[platform]})
    concept={'human_moment':'A shared household ritual','hook':'A familiar interruption','payoff':'A useful discovery','visual_direction':'A coherent kitchen scene','brain_movement':'Notice the useful pattern','heart_response':'Quiet confidence','storyboard':[{'action':'Distinct story beat '+str(i)} for i in range(4)]}
    account={'facebook':'fb-approved','instagram':'approved-account','linkedin':'urn:li:organization:approved'}[platform]
    caption='A useful discovery to keep your household connected.'
    item=store.revise(item['id'],{'concept':concept,'captions':{platform:caption},'assets':[asset(store,'frame-'+str(i)+'.jpg') for i in range(4)],'destination_accounts':{platform:account},'scheduled_at':'2030-01-03T12:00:00+00:00'},item['revision'])
    item=store.revise(item['id'],{'review':{k:True for k in ['brand','caption','visual','mobile','canon','truth']}},item['revision'])
    item=store.approve(item['id'],item['revision'])
    result=schedule_stage(store,{'package_id':item['id'],'revision':item['revision']},item['payload'])
    with store.db() as c:package=json.loads(c.execute('SELECT package_json FROM content_outbox WHERE outbox_id=?',(result['outbox_id'],)).fetchone()[0])
    if platform=='facebook':
        import publish_facebook as adapter
        monkeypatch.setattr(adapter,'_resolve_page_access_token',lambda *args:'synthetic-token')
        monkeypatch.setenv('META_PAGE_ACCESS_TOKEN','synthetic-token')
        send=Mock(return_value={'id':'fb-confirmed'})
        monkeypatch.setattr(adapter,'_publish_feed_with_photo_urls',send)
        assert dispatch_outbox._publish(package,platform)['id']=='fb-confirmed'
        assert len(send.call_args.kwargs['image_urls'])==4
        assert send.call_args.kwargs['message']==caption
    elif platform=='instagram':
        import publish_instagram as adapter
        monkeypatch.setenv('META_PAGE_ACCESS_TOKEN','synthetic-token')
        monkeypatch.setattr(adapter,'_is_reachable_image_url',lambda *args:True)
        monkeypatch.setattr(adapter,'_wait_for_media_container',lambda *args:(True,'ready'))
        send=Mock(side_effect=lambda *args,**kwargs:SimpleNamespace(ok=True,json=lambda:{'id':'ig-confirmed'}))
        monkeypatch.setattr(adapter,'_post_with_retry',send)
        assert dispatch_outbox._publish(package,platform)['id']=='ig-confirmed'
        payloads=[args.args[1] for args in send.call_args_list]
        assert sum(p.get('is_carousel_item')=='true' for p in payloads)==4
        assert next(p['caption'] for p in payloads if 'caption' in p)==caption
    else:
        import publish_linkedin as adapter
        upload=Mock(side_effect=lambda author,path:'urn:li:asset:'+Path(path).stem)
        monkeypatch.setattr(adapter,'_upload_image_asset',upload)
        monkeypatch.setattr(adapter,'_headers',lambda **kwargs:{})
        send=Mock(return_value=SimpleNamespace(ok=True,headers={'x-restli-id':'li-confirmed'}))
        monkeypatch.setattr(adapter,'_post_with_retry',send)
        monkeypatch.setattr(adapter,'_is_duplicate_error',lambda response:False)
        monkeypatch.setattr(adapter,'_raise_with_body',lambda response:None)
        assert dispatch_outbox._publish(package,platform)['id']=='li-confirmed'
        assert upload.call_count==4
        share=send.call_args.kwargs['json_payload']['specificContent']['com.linkedin.ugc.ShareContent']
        assert len(share['media'])==4 and share['shareCommentary']['text']==caption
        assert all(args.args[0]==account for args in upload.call_args_list)


def test_unconfirmed_platform_request_is_held_instead_of_resent(store,monkeypatch):
    import dispatch_outbox
    from content_operations import begin_platform_transaction
    item,payload,result=approved_package(store)
    begin_platform_transaction(store.data_dir,outbox_id=result['outbox_id'],platform='instagram',payload={'request':'previous process'})
    send=Mock();monkeypatch.setattr(dispatch_outbox,'_publish',send)
    outcome=dispatch_outbox.dispatch_due(data_dir=store.data_dir,now_utc='2030-01-03T12:00:01+00:00')
    assert outcome['status']=='EXTERNAL_ACTION_REQUIRED'
    assert outcome['platforms']['instagram']['state']=='AMBIGUOUS'
    send.assert_not_called()


def test_service_local_backup_is_consistent_and_never_overwritten(store):
    from production_backup import backup_before_migration
    item=store.create({'brief':'A preserved original','format':'humor'})
    first=backup_before_migration(store.data_dir)
    assert first['status']=='VERIFIED' and first['integrity']=='ok'
    with sqlite3.connect(first['path']) as c:
        assert c.execute('SELECT COUNT(*) FROM production_packages').fetchone()[0]==1
    store.create({'brief':'An additive later draft','format':'humor'})
    second=backup_before_migration(store.data_dir)
    assert second==first
    with sqlite3.connect(second['path']) as c:
        assert c.execute('SELECT COUNT(*) FROM production_packages').fetchone()[0]==1


def test_connection_checks_never_return_credentials(store,monkeypatch):
    import production_connections
    from types import SimpleNamespace
    monkeypatch.setenv('GEMINI_API_KEY','synthetic-private-key')
    monkeypatch.setenv('META_PAGE_ACCESS_TOKEN','synthetic-private-token')
    monkeypatch.setenv('META_PAGE_ID','fb-approved')
    from social.model_router import route_for
    def get(url,**kwargs):
        if 'googleapis' in url:return SimpleNamespace(status_code=200,json=lambda:{'models':[{'name':'models/'+model,'supportedGenerationMethods':['generateContent']} for model in {route_for('topic_generation'),route_for('copy_editing'),'gemini-2.5-flash-image'}]})
        return SimpleNamespace(status_code=200,json=lambda:{'id':url.rsplit('/',1)[-1],'name':'Infenergy'})
    monkeypatch.setattr(production_connections.requests,'get',get)
    result=production_connections.verify()
    assert all(v['status']=='PASS' for v in result.values())
    assert 'synthetic-private' not in json.dumps(result)


def test_approved_product_reference_can_schedule_without_a_legacy_brief_file(store):
    item=complete(store)
    reference=store.register_reference({'kind':'product','name':'A new owner-approved product','canon':'Only the supported input/output behavior','files':[item['payload']['assets'][0]['path']]})
    item=store.revise(item['id'],{'product_id':'new-product','product_role':'Keep a phone connected','evidence':['Owner-verified input/output specification'],'references':[reference]},item['revision'])
    item=store.revise(item['id'],{'review':{k:True for k in ['brand','caption','visual','mobile','canon','truth']}},item['revision'])
    item=store.approve(item['id'],item['revision'])
    result=schedule_stage(store,{'package_id':item['id'],'revision':item['revision']},item['payload'])
    with store.db() as c:
        row=c.execute('SELECT package_json,readiness_json FROM content_outbox WHERE outbox_id=?',(result['outbox_id'],)).fetchone()
        assert json.loads(row['readiness_json'])['ready']
        assert json.loads(row['package_json'])['canon']['registry']=='production_references'


def test_storypage_retains_canonical_panels_and_instagram_story_delivery(store,monkeypatch):
    import dispatch_outbox,publish_instagram
    item=store.create({'brief':'An earned four-panel hero story','format':'infenergy_storypage'})
    rules=item['payload']['rules'];assert rules['aspect_ratio']=='9:16' and rules['panels']==4
    assert rules['format_contract']['canon']['hero_panel_required']
    ref_asset=asset(store,'hero-reference.png')
    ref=store.register_reference({'kind':'character','name':'Owner-approved Infenergy','canon':'Preserve this exact original identity','files':[ref_asset['path']]})
    image=Path(store.data_dir)/'public_media/story.jpg';Image.new('RGB',(1080,1920),'#153428').save(image)
    record=store.asset_record('public_media/story.jpg');record['inspection']={'sha256':record['sha256'],'expected':[],'ocr':{'status':'PASS'}}
    concept={'human_moment':'A relatable interruption','hook':'The moment changes','payoff':'Earned capability','visual_direction':'Four panels','brain_movement':'Notice the useful action','heart_response':'Quiet confidence','storyboard':[{'action':'A complete story','panels':[{'action':'Panel action '+str(i)} for i in range(4)]}]}
    item=store.revise(item['id'],{'concept':concept,'assets':[record],'references':[ref],'captions':{'instagram':'Planning context for the complete story.'},'destination_accounts':{'instagram':'approved-account'},'scheduled_at':'2030-01-03T12:00:00+00:00'},item['revision'])
    item=store.revise(item['id'],{'review':{k:True for k in ['brand','caption','visual','mobile','canon','truth']}},item['revision'])
    item=store.approve(item['id'],item['revision']);result=schedule_stage(store,{'package_id':item['id'],'revision':item['revision']},item['payload'])
    with store.db() as c:package=json.loads(c.execute('SELECT package_json FROM content_outbox WHERE outbox_id=?',(result['outbox_id'],)).fetchone()[0])
    monkeypatch.setenv('META_PAGE_ACCESS_TOKEN','synthetic-token')
    send=Mock(return_value={'id':'story-confirmed','media_type':'STORY'});monkeypatch.setattr(publish_instagram,'_publish_story_video',send)
    assert dispatch_outbox._publish(package,'instagram')['id']=='story-confirmed'
    assert send.call_args.kwargs['image_url'].endswith('/media/story.jpg')


def test_clone_retains_work_but_clears_approval_schedule_and_provenance(store):
    original=complete(store);original=store.approve(original['id'],original['revision'])
    draft=store.clone(original['id'])
    assert draft['approval'] is None and not draft['payload']['scheduled_at']
    assert draft['payload']['assets']==original['payload']['assets']
    assert draft['payload']['review']=={} and draft['payload']['provenance']==[]
    assert draft['payload']['title'].endswith('(new draft)')


def test_delivery_assets_are_copied_into_the_actually_served_directory(store):
    root=Path(store.data_dir);(root/'source-assets').mkdir()
    original=root/'source-assets/source.png';Image.new('RGB',(1080,1350),'#183224').save(original)
    copied=store.delivery_asset('source-assets/source.png')
    assert copied['path'].startswith('public_media/')
    assert store.local_asset(copied['path']).read_bytes()==original.read_bytes()


def test_unapproved_legacy_outbox_is_blocked_before_dispatch(store):
    from content_operations import mark_ready,claim_due
    identifier=mark_ready(store.data_dir,content_date='2030-01-03',slot='morning',scheduled_at='2030-01-03T12:00:00+00:00',decision_id='legacy',package={'post_id':'unreviewed-old','image_url':'https://social.example.com/media/old.png','routing':{'platforms':['instagram']},'platform_posts':{'instagram':{'final_caption':'A complete older caption'}}})
    with store.db() as c:
        readiness=json.loads(c.execute('SELECT readiness_json FROM content_outbox WHERE outbox_id=?',(identifier,)).fetchone()[0])
    assert not readiness['ready'] and readiness['reason_codes']==['EDITORIAL_APPROVAL_REQUIRED']
    assert claim_due(store.data_dir,'2030-01-03T12:00:01+00:00') is None


def test_operational_health_distinguishes_unreviewed_drafts_from_failed_jobs(store):
    from production_health import snapshot
    item=store.create({'brief':'Waiting for owner review','format':'humor'})
    health=snapshot(store.data_dir)
    assert health['healthy'] and health['status']=='AWAITING_OWNER_REVIEW'
    store.queue(item['id'],'concept',item['revision']);job=store.claim('worker')
    store.finish(job,error='provider_unavailable')
    assert snapshot(store.data_dir)['healthy'] is False
    assert 'PRODUCTION_JOB_NEEDS_ATTENTION' in snapshot(store.data_dir)['issues']


def test_provider_normalization_preserves_the_entire_frame_and_feed_dimensions():
    from production_images import normalize
    from PIL import ImageDraw
    original=Image.new('RGB',(896,1152),'#123456')
    draw=ImageDraw.Draw(original);draw.rectangle((0,0,895,20),fill='red');draw.rectangle((0,1131,895,1151),fill='lime')
    output=normalize(original,'4:5')
    assert output.size==(1080,1350)
    assert output.getpixel((540,5))[0]>200
    assert output.getpixel((540,1345))[1]>200
    assert normalize(original,'9:16').size==(1080,1920)


def test_typesetting_repairs_provider_aspect_without_another_image_call(store):
    source=Path(store.data_dir)/'public_media/raw-provider.png';source.parent.mkdir(exist_ok=True)
    Image.new('RGB',(896,1152),'#123456').save(source)
    target=source.with_name('normalized-type.png')
    receipt=render(source,target,[{'text':'STAY CONNECTED','rect':[.06,.70,.88,.23]}],aspect_ratio='4:5')
    with Image.open(target) as image:assert image.size==(1080,1350)
    assert receipt['ocr']['status']=='PASS'


def test_ocr_separates_faint_background_shapes_from_actual_missing_or_duplicate_text(monkeypatch,tmp_path):
    from production_typography import inspect_text
    from types import SimpleNamespace
    header='level\tconf\ttext\n'
    monkeypatch.setattr('production_typography.shutil.which',lambda name:'/tesseract')
    def respond(rows):monkeypatch.setattr('production_typography.subprocess.run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=header+rows))
    respond('5\t9\tyY\n5\t96\tSTAY\n5\t96\tCONNECTED\n')
    result=inspect_text(tmp_path/'image.png',['STAY CONNECTED'])
    assert result['status']=='PASS' and result['low_confidence_candidates'][0]['text']=='yY'
    respond('5\t9\tyY\n5\t96\tSTAY\n')
    assert inspect_text(tmp_path/'image.png',['STAY CONNECTED'])['status']=='REVIEW_REQUIRED'
    respond('5\t96\tSTAY\n5\t96\tCONNECTED\n5\t9\tCONNECTED\n')
    assert inspect_text(tmp_path/'image.png',['STAY CONNECTED'])['status']=='REVIEW_REQUIRED'
    respond('5\t96\tSTAY\n5\t96\tCONNECTED\n5\t95\tUNEXPECTED\n')
    assert inspect_text(tmp_path/'image.png',['STAY CONNECTED'])['status']=='REVIEW_REQUIRED'


def test_controlled_region_ocr_still_blocks_unexpected_words_elsewhere(store):
    from PIL import ImageDraw,ImageFont
    source=store.local_asset(asset(store)['path'])
    with Image.open(source) as original:
        image=original.convert('RGB');draw=ImageDraw.Draw(image)
        draw.text((90,100),'UNEXPECTED WORD',font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',60),fill='white')
        image.save(source)
    result=render(source,source.with_name('region-qa.png'),[{'text':'STAY CONNECTED','rect':[.06,.70,.88,.23]}])
    assert result['ocr']['inspection_mode']=='controlled_regions_and_full_background'
    assert result['ocr']['status']=='REVIEW_REQUIRED'
    assert 'unexpected' in result['ocr']['unexpected_or_duplicate_words']


def test_final_inspection_prepares_instagram_jpeg_without_losing_original_artwork(store):
    original=asset(store,'original-artwork.png')
    item=store.create({'brief':'An unlettered consumer scene','format':'humor'})
    item=store.revise(item['id'],{'assets':[original]},item['revision'])
    store.queue(item['id'],'inspect',item['revision']);run_one(store)
    result=store.get(item['id'])['payload']['assets'][0]
    assert result['path'].endswith('.jpg') and result['format']=='JPEG'
    assert result['source_path']==original['path']
    assert store.asset_record(original['path'])['sha256']==original['sha256']
    assert 'instagram_delivery_requires_jpeg_inspect_final_assets' not in store.checks(item['id'])['errors']


def test_asset_reordering_preserves_original_source_and_typesetting_metadata(store):
    from production_api import handle
    item=complete(store);record=item['payload']['assets'][0]
    record['source_path']=record['path'];record['typography']={'mode':'controlled','layers':[{'text':'example','rect':[.06,.7,.88,.23]}]}
    item=store.revise(item['id'],{'assets':[record]},item['revision'])
    status,result=handle('PATCH','/api/os/production/packages/'+item['id'],{'revision':item['revision'],'updates':{'assets':[record['path']]}},store.data_dir)
    assert status==200
    assert result['package']['payload']['assets'][0]['source_path']==record['source_path']
    assert result['package']['payload']['assets'][0]['typography']==record['typography']
