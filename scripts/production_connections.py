"""Read-only provider checks. Never return bearer tokens or raw provider errors."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import os
import requests


def _check(provider):
    if provider=='gemini':
        key=os.environ.get('GEMINI_API_KEY','')
        if not key:return {'status':'NOT_CONFIGURED'}
        response=requests.get('https://generativelanguage.googleapis.com/v1beta/models',headers={'x-goog-api-key':key},timeout=20)
        if response.status_code!=200:return {'status':'UNAVAILABLE','http':response.status_code}
        models={m.get('name','').removeprefix('models/') for m in response.json().get('models',[]) if 'generateContent' in m.get('supportedGenerationMethods',[])}
        from social.model_router import route_for
        required={route_for('topic_generation'),route_for('copy_editing'),os.environ.get('GEMINI_IMAGE_MODEL','gemini-2.5-flash-image')}
        missing=sorted(required-models)
        return {'status':'PASS' if not missing else 'MODEL_UNAVAILABLE','http':200,'missing_models':missing}
    token=os.environ.get('META_PAGE_ACCESS_TOKEN','')
    account=os.environ.get('META_PAGE_ID' if provider=='facebook' else 'META_IG_USER_ID','')
    if not token or not account:return {'status':'NOT_CONFIGURED'}
    version=os.environ.get('META_GRAPH_VERSION','v22.0')
    response=requests.get('https://graph.facebook.com/'+version+'/'+account,headers={'Authorization':'Bearer '+token},params={'fields':'id,name' if provider=='facebook' else 'id,username'},timeout=20)
    body=response.json()
    return {'status':'PASS' if response.status_code==200 and str(body.get('id'))==account else 'UNAVAILABLE','http':response.status_code,'account_name':body.get('name') or body.get('username')}


def verify():
    providers=['gemini','facebook','instagram']
    def safe(provider):
        try:return provider,_check(provider)
        except Exception as exc:return provider,{'status':'UNAVAILABLE','reason':type(exc).__name__}
    with ThreadPoolExecutor(max_workers=3) as pool:return dict(pool.map(safe,providers))
