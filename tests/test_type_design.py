from __future__ import annotations
import sys
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from social.type_design import resolve,instruction
from social.scene_message import scene_message_direction
from social.production_quality import criteria_for,decision,PLAN_CRITERIA,RELEASE_CRITERIA


def test_type_direction_has_character_but_preserves_exact_words():
    message=scene_message_direction(headline='Watch the shade',supporting='Place panels thoughtfully',problem='Moving shadows',outcome='A better location',scene='A traveler follows the shadow line',typography_design={'emphasis':['shade','Invented slogan']})
    design=message['typography_design']
    assert design['emphasis']==['shade']
    assert message['gemini_rendered_text']==['Watch the shade','Place panels thoughtfully']
    assert message['text_overlay']=={'enabled':False}
    prompt=message['typography_render_instruction']
    assert 'custom' in prompt.lower() and 'slab-serif' in prompt
    assert 'plain white all-caps' in prompt
    assert 'No later text overlay' in prompt


def test_formats_receive_different_coherent_lettering_personalities():
    funny=resolve(format_id='humor',lines=['Someone borrowed it'])
    cinematic=resolve(format_id='cinematic_brand_poster',lines=['Choose your range'])
    comic=resolve(format_id='infenergy_storypage',lines=['One smart move'])
    assert len({x['letterforms'] for x in [funny,cinematic,comic]})==3
    assert funny['style']=='human' and cinematic['style']=='cinematic' and comic['style']=='comic'
    assert 'readable on a phone' in instruction(comic)


@pytest.mark.parametrize('bad',[{'emphasis':'Add words'},{'letterforms':42},{'personality':'x'}])
def test_invalid_authored_direction_is_rejected_before_rendering(bad):
    with pytest.raises(ValueError):resolve(bad,lines=['Exact text'])


def test_correct_spelling_cannot_hide_boring_type_when_design_is_requested():
    subject={'typography_design':[resolve(lines=['Watch the shade'])]}
    scores={key:5 for key in RELEASE_CRITERIA};scores['lettering']=3
    response={'scores':scores,'evidence':{key:'Specific evidence explaining this assessment in the actual work.' for key in scores},'blockers':[]}
    result=decision(response,subject,'release','test-reviewer')
    assert result['status']=='BLOCKED'
    assert any(b['criterion']=='lettering' for b in result['blockers'])


def test_previously_reviewed_unstyled_packets_keep_their_original_review_contract():
    assert criteria_for({},'plan')==PLAN_CRITERIA
    assert criteria_for({},'release')==RELEASE_CRITERIA
    assert 'lettering' in criteria_for({'typography_design':[{}]},'plan')
