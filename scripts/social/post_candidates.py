"""Replace rejected posts, preserving their reviews and the delivery slot."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os


class CandidateQualityError(RuntimeError):
    def __init__(self, stage, review):
        self.stage = stage
        self.review = review
        super().__init__('post_candidate_quality_rejected:' + stage)


def automatic():
    from social.production_quality import enabled
    return os.environ.get('EDITORIAL_APPROVAL_MODE') == 'automatic' and enabled()


def rejection(package):
    """Recognize an actual persisted rejection, never an infrastructure error."""
    for stage in ('release', 'plan'):
        receipt = (package.get('production_quality') or {}).get(stage) or {}
        if receipt.get('status') == 'BLOCKED' and receipt.get('review_complete') is not False:
            return CandidateQualityError(stage, receipt)
    return None


def replacement_brief(package):
    history = (package.get('candidate_recovery') or {}).get('rejected', [])
    if not history:
        return None
    return {
        'instruction': 'Create a genuinely different complete post: a new premise, human situation, hook, payoff, scene, captions and lettering design. Do not merely paraphrase the rejected copy or repair its image. Learn from every rejection below. Preserve the audience, format, approved facts and product identity; never reuse a scrapped draft. The consumer moment and source concept are context, not a locked premise. No image will be generated until this new post passes its independent plan review.',
        'rejected_posts': [{'concept':r.get('concept') or r.get('generation_thought'),
                            'captions':r.get('captions') or {p:r.get(k) for p,k in [('facebook','fb_caption'),('instagram','ig_caption')]},
                            'review':r['review']} for r in history[-8:]],
    }


def replace(package, failure):
    """Return a fresh candidate shell; never delete or overwrite rejected pixels."""
    result = deepcopy(package)
    recovery = result.setdefault('candidate_recovery', {})
    recovery.setdefault('original_brief',package.get('brief') or package.get('topic') or '')
    history = recovery.setdefault('rejected', [])
    record = {k: deepcopy(package[k]) for k in (
        'concept', 'generation_thought', 'consumer_receipt', 'image_text', 'captions',
        'fb_caption', 'ig_caption', 'li_text', 'production_quality', 'assets',
    ) if k in package}
    record.update(stage=failure.stage, review=deepcopy(failure.review),
                  assets=deepcopy((package.get('gemini_generation') or {}).get('assets') or package.get('assets') or []),
                  rejected_at=datetime.now(timezone.utc).isoformat())
    record['fingerprint'] = hashlib.sha256(json.dumps({k:v for k,v in record.items() if k!='rejected_at'}, sort_keys=True).encode()).hexdigest()
    history.append(record)
    recovery.update(candidate_number=len(history)+1, status='REPLACEMENT_QUEUED',
                    next_candidate_at=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat())
    for key in ('production_quality', 'review', 'creative_qa', 'master_copy', 'generated_visuals',
                'primary_publish_image_url', 'image_url', 'image_urls', 'carousel_assets',
                'asset_sha256', 'asset_identity', 'perceptual_fingerprint', 'copy_generation_source',
                'fb_caption', 'ig_caption', 'li_text', 'thought_statement', 'consumer_root', 'consumer_receipt', 'consumer_receipt_qa'):
        result.pop(key, None)
    for key in ('validation_status','validation_errors','quality_warnings','publish_decision','consumer_copy_fidelity','consumer_story_contract'):
        result.pop(key,None)
    result.update(concept={}, captions={}, image_text=[], assets=[], review={})
    for post in (result.get('platform_posts') or {}).values():
        for key in ('caption', 'final_caption', 'final_caption_qa', 'image_url', 'image_urls', 'media_type', 'asset_sha256'):
            post.pop(key, None)
    thought = result.get('generation_thought') or {}
    for key in ('statement', 'expansion', 'action', 'image_scene', 'overlay_text', 'instagram_hook', 'slides', 'prompt', 'moral', 'cta', 'consumer_receipt', 'consumer_root'):
        thought.pop(key, None)
    contract = result.get('generation_contract') or {}
    for key in ('hook', 'takeaway', 'cta', 'visible_text', 'typography_design', 'story_sequence', 'entertainment_hook', 'infenergy_action'):
        contract.pop(key, None)
    if 'generation_contract' in result:
        result['generation_contract'] = contract
        thought['generation_contract'] = contract
    if 'generation_thought' in result:
        result['generation_thought'] = thought
        result['brief']='Develop a different original Infenergy post for '+str(contract.get('audience_name') or thought.get('audience') or 'the scheduled audience')+'. Keep the format and verified product boundaries; find a new human moment with a visible worthwhile payoff.'
    if 'gemini_copy' in result:
        old = result['gemini_copy']
        result['gemini_copy'] = {k:old[k] for k in ('provider','strict_provider','fallback_allowed','task') if k in old}
        result['gemini_copy']['status'] = 'PENDING'
        if old.get('provider')=='editorial_bank':result['gemini_copy']['provider']='gemini'
    if 'gemini_generation' in result:
        old = result['gemini_generation']
        result['gemini_generation'] = {k:old[k] for k in ('provider','strict_provider','fallback_allowed','required_image_count','aspect_ratio') if k in old}
        result['gemini_generation'].update(status='PENDING', prompts=[], assets=[])
    return result


def require_fresh_post(package, authored):
    def normalize(text):
        return ' '.join(''.join(c.lower() if c.isalnum() else ' ' for c in str(text)).split())
    for old in (package.get('candidate_recovery') or {}).get('rejected', []):
        thought=old.get('generation_thought') or {}
        if any(normalize(authored.get(k,'')) and normalize(authored[k])==normalize(thought.get(k,'')) for k in ('statement','image_scene')):
            raise CandidateQualityError('copy',{'status':'BLOCKED','blockers':[{'criterion':'originality','issue':'The replacement reused a rejected premise or scene.','fix':'Develop a different complete post, not another image of the rejected idea.'}]})


def summary(package):
    recovery=package.get('candidate_recovery') or {}
    if not recovery:return None
    return {'candidate_number':recovery['candidate_number'],'rejected_count':len(recovery['rejected']),
            'replacement_queued':recovery['status']=='REPLACEMENT_QUEUED',
            'last_review':recovery['rejected'][-1]['review']}
