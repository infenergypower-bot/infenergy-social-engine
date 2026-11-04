"""Verify the authorized production rollout without exposing Railway credentials.

The optional generation exercise creates a draft only. It never approves,
schedules or publishes it, and it respects the configured daily call limits.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import time
import requests


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--railway',default='railway');parser.add_argument('--project',required=True);parser.add_argument('--service',required=True);parser.add_argument('--environment',default='production');parser.add_argument('--exercise-generation',action='store_true');parser.add_argument('--report',required=True);args=parser.parse_args()
    cli=[args.railway,'variable','list','-p',args.project,'-s',args.service,'-e',args.environment,'--json']
    result=subprocess.run(cli,capture_output=True,text=True,check=True)
    variables=json.loads(result.stdout)
    base=(variables.get('PUBLIC_BASE_URL') or 'https://'+variables['RAILWAY_PUBLIC_DOMAIN']).rstrip('/')
    owner=variables.get('INTELLIGENCE_OS_TOKEN') or variables.get('MANUAL_RUN_TOKEN')
    if not owner:raise RuntimeError('owner_authentication_not_configured')
    session=requests.Session();session.headers['Authorization']='Bearer '+owner
    def api(path,method='GET',body=None):
        response=session.request(method,base+'/api/os/production/'+path,json=body,timeout=90)
        if not response.ok:raise RuntimeError('production_api_http_'+str(response.status_code)+':'+path.split('/')[0])
        return response.json()
    report={'base_url':base,'checks':{},'social_posts_sent':0}
    health_response=requests.get(base+'/health',timeout=30);health=health_response.json();report['checks']['health']=health_response.status_code==200;report['checks']['healthy_runtime']=health.get('status')=='ok';report['operational_health']=health.get('production')
    report['checks']['owner_authentication']=requests.get(base+'/api/os/production/state',timeout=30).status_code==401
    for path in ['/os','/os/assets/production.js','/os/assets/production.css','/os/assets/advanced.html']:
        response=requests.get(base+path,timeout=30)
        report['checks'][path]=response.status_code==200
        if path=='/os':report['checks']['new_desk_default']='Production Desk' in response.text
    report['connections']=api('connections/verify','POST',{})['connections']
    state=api('state');report['runtime']=state['runtime'];report['checks']['verified_backup']=state['runtime']['backup']['status']=='VERIFIED'
    report['checks']['required_approval']=state['runtime']['approval_mode']=='required'
    if args.exercise_generation:
        draft=api('packages','POST',{'title':'Production verification · the missing charger','brief':'Create an original, warm, funny visual about a shared household phone-charger hunt: the cable everyone borrows and nobody admits taking. Earn a knowing smile from families and connect staying connected with small prepared habits. No product claims, manufactured urgency, forced checkout or generic superhero. This is a draft for owner review; do not publish.','format':'humor','objective':'audience_growth','platforms':['facebook','instagram']})['package']
        identifier=draft['id'];estimate=api('packages/'+identifier+'/estimate','POST',{})
        budget=estimate['budget'];report['budget_before']={k:budget[k] for k in ['total','image','next_reset_at_utc']}
        if budget['total']['remaining']<3 or budget['image']['remaining']<1:
            api('packages/'+identifier+'/queue','POST',{'revision':draft['revision'],'stage':'draft'})
            report['generation']={'status':'WAITING_BUDGET','package_id':identifier,'reason':'Queued within the configured daily limit; generation resumes at its next budget window.'}
        else:
            api('packages/'+identifier+'/queue','POST',{'revision':draft['revision'],'stage':'draft'})
            deadline=time.monotonic()+600;previous=None
            while time.monotonic()<deadline:
                state=api('state');jobs=[j for j in state['jobs'] if j['package_id']==identifier]
                package=next(p for p in state['packages'] if p['id']==identifier)
                progress=[(j['stage'],j['state']) for j in jobs]
                if progress!=previous:print(json.dumps({'generation_progress':progress}),flush=True);previous=progress
                failed=[j for j in jobs if j['state'] in {'FAILED','RECONCILIATION_REQUIRED','WAITING_BUDGET'}]
                if failed:
                    report['generation']={'status':failed[0]['state'],'package_id':identifier,'error':failed[0]['error']};break
                if jobs and all(j['state']=='SUCCEEDED' for j in jobs):
                    report['generation']={'status':'DRAFT_READY','package_id':identifier,'revision':package['revision'],'frames':len(package['payload']['assets']),'stages_succeeded':len(jobs),'approval':package['approval'],'outbox_id':package['outbox_id'],'ocr':[a.get('inspection',{}).get('ocr',{}).get('status') for a in package['payload']['assets']],'image_urls':[base+'/media/'+Path(a['path']).name for a in package['payload']['assets']]};break
                time.sleep(2)
            else:report['generation']={'status':'TIMED_OUT','package_id':identifier}
    report['passed']=all(report['checks'].values()) and all(c['status']=='PASS' for c in report['connections'].values()) and (not args.exercise_generation or report.get('generation',{}).get('status')=='DRAFT_READY')
    Path(args.report).write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
    if not report['passed']:raise SystemExit(1)

if __name__=='__main__':main()
