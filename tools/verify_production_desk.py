"""Local browser smoke test; synthetic data, no social publishing or paid AI calls.

Run with the project venv plus Playwright installed and a Chromium executable.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--artifacts',default='/tmp/infenergy-browser-verification');parser.add_argument('--chromium',default='/usr/bin/chromium');args=parser.parse_args()
    artifacts=Path(args.artifacts);artifacts.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='infenergy-browser-') as data:
        os.environ.update({'DATA_DIR':data,'INTELLIGENCE_OS_TOKEN':'local-verification-only','PUBLIC_BASE_URL':'https://social.example.com','META_IG_USER_ID':'approved-account','CONTENT_DISPATCH_ENABLED':'false','SOCIAL_DRY_RUN':'true','GEMINI_API_KEY':''})
        import worker
        from http.server import ThreadingHTTPServer
        from production_worker import run_one
        from production_workflow import Production
        server=ThreadingHTTPServer(('127.0.0.1',0),worker.HealthHandler)
        url='http://127.0.0.1:'+str(server.server_address[1]);threading.Thread(target=server.serve_forever,daemon=True).start()
        from playwright.sync_api import sync_playwright
        from PIL import Image
        source=Path(data)/'source.png';Image.new('RGB',(1080,1350),'#1b3a2e').save(source)
        errors=[]
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.on('pageerror',lambda e:errors.append(str(e)))
            assert page.request.get(url+'/api/os/production/state').status==401
            page.goto(url+'/os');page.locator('#password').fill('local-verification-only');page.get_by_role('button',name='Open Production Desk').click()
            page.get_by_role('button',name='+ New creative brief').click()
            page.locator('[name=title]').fill('The missing charger')
            page.locator('[name=brief]').fill('An affectionate household charger mystery; a knowing payoff about staying connected.')
            page.locator('[name=format]').select_option('humor');page.get_by_role('button',name='Create draft',exact=True).click()
            page.locator('[name=human_moment]').fill('A family searches for the one charger everyone borrows.')
            page.locator('[name=hook]').fill('A familiar household mystery.')
            page.locator('[name=payoff]').fill('The cable was with the person who said they had not seen it.')
            page.locator('[name=visual_direction]').fill('A knowing, lived-in kitchen scene with negative space below the action.')
            page.locator('[name=brain_movement]').fill('Recognize the shared household habit.')
            page.locator('[name=heart_response]').fill('A knowing smile and affection for family.')
            page.locator('[name=caption_instagram]').fill('Some household mysteries have a very short list of suspects.')
            page.locator('[name=image_text]').fill('STAY CONNECTED')
            page.locator('[name=account_instagram]').fill('approved-account')
            page.locator('[name=scheduled_at]').fill('2030-01-03T12:00:00+00:00')
            page.get_by_role('button',name='Prepare bottom text layout').click()
            page.get_by_role('button',name='Save creative edits').click()
            page.wait_for_function("document.querySelector('#notice').textContent.includes('New revision saved')")
            page.locator('#upload-frames').set_input_files(source)
            page.wait_for_function("document.querySelector('#notice').textContent.includes('Frames uploaded')")
            page.locator('#layout-placement').select_option('top')
            page.locator('#layout-placement').select_option('bottom')
            page.get_by_role('button',name='Save creative edits').click()
            page.wait_for_function("document.querySelector('#notice').textContent.includes('New revision saved')")
            page.get_by_role('button',name='Typeset controlled text').click()
            page.wait_for_function("document.querySelector('#notice').textContent.includes('Production job queued')")
            store=Production(data);assert run_one(store,'browser-test')
            page.locator('#refresh').click();page.locator('[data-stage=inspect]').click()
            page.wait_for_function("document.querySelector('#notice').textContent.includes('Production job queued')")
            assert run_one(store,'browser-test');page.locator('#refresh').click()
            for name in ['brand','caption','visual','mobile','canon','truth']:page.locator('#review-form [name='+name+']').check()
            page.get_by_role('button',name='Save editorial review').click()
            page.wait_for_function("document.querySelector('#notice').textContent.includes('Editorial review saved')")
            assert page.locator('#checks').inner_text().startswith('All required checks passed.'),page.locator('#checks').inner_text()
            page.locator('#approve').click();page.wait_for_function("document.querySelector('#notice').textContent.includes('exact revision is approved')")
            page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=str(artifacts/'desk-desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(artifacts/'desk-mobile.png'),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile overflow'
            page.locator('#schedule').click();page.wait_for_function("document.querySelector('#notice').textContent.includes('Scheduling job queued')")
            assert run_one(store,'browser-test')
            page.locator('[data-page=calendar]').click();page.locator('#refresh').click()
            page.wait_for_function("document.querySelector('#content').textContent.includes('SCHEDULED')", timeout=10000)
            for name in ['results','canon','operations']:page.locator('[data-page='+name+']').click()
            assert not errors,errors
            item=store.snapshot()['packages'][0]
            assert item['outbox_id']
            report={'passed':True,'browser_errors':errors,'verified':['authenticated API','draft creation and revisions','asset upload','local typesetting','real OCR inspection','owner review','revision-bound approval','outbox scheduling with dispatch disabled','five workspace pages','desktop and mobile layout'],'package_state':item['state'],'paid_ai_calls':0,'social_posts_sent':0}
            (artifacts/'browser-report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
            browser.close()
        server.shutdown();server.server_close()

if __name__=='__main__':main()
