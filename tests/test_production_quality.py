from __future__ import annotations
import copy
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from social import production_quality as quality


@pytest.fixture
def work(tmp_path,monkeypatch):
    monkeypatch.setenv('PRODUCTION_QUALITY_ENABLED','true')
    image=tmp_path/'public_media'/'finished.png';image.parent.mkdir()
    Image.new('RGB',(1080,1080),'#42664f').save(image)
    package={'brief':'A practical check before placing solar panels.',
        'generation_thought':{'statement':'Look for moving shade.','image_scene':'A traveler checks the shadow line before placing a panel.'},
        'routing':{'platforms':['facebook']},'platform_posts':{'facebook':{'final_caption':'Check the afternoon shadow line before choosing a panel position.'}},
        'gemini_generation':{'prompts':[{'v5_direction':{'gemini_rendered_text':['Watch the shade.']}}],
            'assets':[{'local_path':str(image)}]},'primary_publish_image_url':'https://example.com/media/finished.png'}
    return package,str(tmp_path)


def review_response(stage='release'):
    keys=quality.PLAN_CRITERIA if stage=='plan' else quality.RELEASE_CRITERIA
    return {'scores':{key:5 for key in keys},'evidence':{key:'The exact supplied scene and wording support this criterion.' for key in keys},'blockers':[]}


def approve(work):
    package,root=work
    package['production_quality']={stage:quality.decision(review_response(stage),quality.packet(package,root,include_assets=stage=='release'),stage,'test-reviewer') for stage in ['plan','release']}
    return package,root


def test_release_binds_copy_and_actual_pixels(work):
    package,root=approve(work)
    assert quality.ready(package,root)
    quality.require(package,root)
    package['platform_posts']['facebook']['final_caption']='A different message.'
    assert not quality.ready(package,root)
    with pytest.raises(ValueError,match='stale'):quality.require(package,root)


def test_changing_pixels_or_delivery_url_invalidates_finished_review(work):
    package,root=approve(work)
    original=copy.deepcopy(package)
    package['primary_publish_image_url']='https://example.com/media/another.png'
    assert quality.current(package,root,'plan')
    assert not quality.current(package,root,'release')
    package=original
    Image.new('RGB',(1080,1080),'#935133').save(package['gemini_generation']['assets'][0]['local_path'])
    assert not quality.current(package,root,'release')


@pytest.mark.parametrize('bad',[None,True,float('nan'),6,-1,3])
def test_a_high_average_cannot_hide_one_bad_or_missing_criterion(work,bad):
    package,root=work;response=review_response()
    response['scores']['meaning']=bad
    receipt=quality.decision(response,quality.packet(package,root,include_assets=True),'release','test')
    assert receipt['status']=='BLOCKED'
    assert any(b['criterion']=='meaning' for b in receipt['blockers'])


def test_truth_and_exact_text_require_full_marks(work):
    package,root=work
    for key in ['truth','text']:
        response=review_response();response['scores'][key]=4
        assert quality.decision(response,quality.packet(package,root,include_assets=True),'release','test')['status']=='BLOCKED'


def test_review_cannot_pass_without_specific_evidence_and_findings_list(work):
    package,root=work;response=review_response()
    response['evidence']['copy']='Fine'
    response.pop('blockers')
    result=quality.decision(response,quality.packet(package,root),'plan','test')
    assert result['status']=='BLOCKED'


def test_asset_outside_data_directory_cannot_receive_a_valid_receipt(work,tmp_path):
    package,root=work
    package['gemini_generation']['assets'][0]['local_path']=str(tmp_path.parent/'outside.png')
    with pytest.raises(ValueError,match='inside_data'):quality.packet(package,root,include_assets=True)


def test_preproduction_review_uses_independent_editor_and_reuses_current_receipt(work,monkeypatch):
    package,root=work;calls=[]
    def assess(task,prompt,**kwargs):
        calls.append((task,prompt));return review_response('plan')
    monkeypatch.setattr('social.model_router.generate_json',assess)
    assert quality.review(package,root,'plan')['status']=='PASS'
    assert quality.review(package,root,'plan')['status']=='PASS'
    assert len(calls)==1 and calls[0][0]=='final_review'


def test_failed_plan_never_spends_on_an_image(work,monkeypatch):
    import dispatch_outbox
    package,root=work
    package['gemini_generation'].update({'strict_provider':True,'required_image_count':1})
    package['gemini_copy']={'strict_provider':True}
    monkeypatch.setenv('EDITORIAL_APPROVAL_MODE','automatic')
    monkeypatch.setattr(dispatch_outbox,'_gemini_copy_ready',lambda *_:True)
    monkeypatch.setattr(dispatch_outbox,'_gemini_assets_ready',lambda *_:False)
    monkeypatch.setattr(dispatch_outbox,'_prepare_gemini_copy',lambda p,*_:p)
    monkeypatch.setattr(dispatch_outbox,'_hydrate_product_image_approval',lambda p:p)
    monkeypatch.setattr(dispatch_outbox,'preflight_gemini_workflow',lambda **_:None)
    monkeypatch.setattr(dispatch_outbox,'upcoming_ready_packages',lambda *a,**k:[{'outbox_id':'one','scheduled_at':'2030-01-01T12:00:00+00:00','lifecycle_state':'PREPARATION_REQUIRED','package':package}])
    monkeypatch.setattr(dispatch_outbox,'transition_package',lambda *a,**k:None)
    monkeypatch.setattr(dispatch_outbox,'update_ready_package',lambda *a,**k:True)
    monkeypatch.setattr(quality,'review',lambda p,*_:{'status':'BLOCKED'})
    monkeypatch.setattr(dispatch_outbox,'_prepare_gemini_assets',lambda *a,**k:pytest.fail('Image generation must not run after a failed plan.'))
    result=dispatch_outbox.pregenerate_upcoming(data_dir=root)
    assert result['status']=='FAILED_PREPARATION'
    assert 'quality_plan_blocked' in result['error']


def test_durable_draft_stops_before_images_when_plan_review_is_blocked(work):
    from production_workflow import Production
    _,root=work;store=Production(root)
    item=store.create({'brief':'Useful panel placement advice','format':'humor'})
    store.queue(item['id'],'draft',item['revision'])
    concept=store.claim('test');assert concept['stage']=='concept'
    store.finish(concept,updates={'concept':{'human_moment':'Shade crosses a campsite.'},'image_text':['Watch the shade.']})
    caption=store.claim('test');assert caption['stage']=='caption'
    store.finish(caption,updates={'captions':{'instagram':'Check the shadow line before placing panels.'}})
    plan=store.claim('test');assert plan['stage']=='quality_plan'
    store.finish(plan,updates={'production_quality':{'plan':{'status':'BLOCKED','blockers':[{'issue':'The idea needs a clearer payoff.'}]}}})
    assert store.claim('test') is None


def test_automatic_publish_boundary_requires_the_new_release_receipt(work,monkeypatch):
    from publication_authorization import authorize
    package,root=work
    monkeypatch.setenv('EDITORIAL_APPROVAL_MODE','automatic')
    with pytest.raises(ValueError,match='production_quality_review_required'):
        authorize(package,'facebook',data_dir=root)
