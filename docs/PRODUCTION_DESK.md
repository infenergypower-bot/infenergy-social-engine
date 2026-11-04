# Infenergy Production Desk

The application creates entertaining, useful, recognizable Infenergy content that earns attention, saves, shares and return visits. Sales content is an explicit objective. The production path keeps the existing publishing outbox and adapters, preserves the previous workspace, and separates draft production from delivery.

## Daily use

1. Open `/os` and sign in with the existing owner password (`INTELLIGENCE_OS_TOKEN` or `MANUAL_RUN_TOKEN`). The previous interface remains at `/os/assets/advanced.html`.
2. Register approved character, product and brand images and exact canon under **Brand & canon**. References are immutable versions; choose the versions used by each draft. The engine does not invent missing permanent character canon.
3. Create a brief and select a format, objective and destinations. Develop the premise and storyboard before production. Record the intended brain movement and heart response. Products need an actual role, reference and supported evidence.
4. **Build complete draft** queues concept, captions, individual frames, controlled typesetting where applicable, and final image-text inspection. It stops at review. Use the individual actions to retain successful work and repair only a caption or frame. A full draft requires empty assets to avoid discarding existing work.
5. Edit captions separately by platform. Declare exact visible text in frame order. Place controlled text visually, with protected regions around faces and products. Oversized or overlapping text blocks typesetting instead of becoming tiny or covering the subject. Integrated physical typography remains inside the image-generation scene.
6. Inspect the actual finished images at phone size. OCR checks missing, extra and repeated words. If OCR is unavailable or uncertain, explain the manual inspection rather than treating the result as an automatic pass.
7. Choose the exact destination account and a timestamp with timezone offset. Complete the editorial review, approve the current revision, then queue it. Any creative edit clears review and approval. Scheduling is not evidence of publication; Calendar shows delivery state and readiness separately.
8. Record sourced observations at 24 hours, 7 days and 30 days. Missing metrics remain unknown. Excellent/rejected examples with explanations inform later drafts of that format.

StoryPage retains the existing four-panel, 9:16 canon; Instagram receives an image Story, while other chosen destinations receive the full vertical artwork. The story caption is retained as planning copy for Instagram Stories.

Supported desk formats: cinematic brand poster, current news, Infenergy Micro Mission, Infenergy StoryPage, superhero text integration, product proof story, product intervention, educational story carousel, humor, community, Culture Current, Try This IRL, and Energy Is an Identity. The advanced workspace retains the existing planning, Studio, video and legacy operational capabilities.

## What changed

- `production_policy.py` resolves format-specific creative instructions, CTA behavior, source precedence and review rubrics. Audience growth replaces hardcoded checkout-first/light-entertainment-only defaults in the revised prompt path.
- Drafts, revisions, immutable reference versions, audit events, jobs, editorial examples and measurement observations use additive tables in the existing inventory SQLite database.
- Optimistic revisions prevent stale browser saves. Jobs have atomic leases, heartbeats, finite attempts and revision checks. Editing queued work supersedes its jobs. Provider completion receipts survive crashes between provider completion and the database commit.
- A complete draft is a durable sequence: the successful result and next stage are committed together. A daily-budget pause resumes on the next UTC reset without consuming a provider attempt. Uncertain paid generations require explicit reconciliation; a receipt is reused when present. Without a receipt, an acknowledged retry may spend again.
- Image production attaches actual approved reference image bytes and previous-frame continuity references to one Gemini image call. No implicit multi-candidate tournament or image-repair loop runs in the new desk. Existing Studio production remains available in the advanced workspace.
- Provider artwork is normalized to exact 1080 × 1350 feed or 1080 × 1920 Story dimensions before review, with the entire foreground preserved rather than cropped. Instagram assets are converted to high-quality RGB JPEG during final inspection, before approval, while their original artwork is retained for later repairs. This follows the [Instagram publishing format requirement](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/content-publishing/). Local Pillow typesetting supports headlines, labels and dialogue. Tesseract inspects controlled text regions as text blocks and scans the full background for unexpected words. Integrated scene text uses full-image inspection. Faint visual shapes remain visible as review candidates; a human still judges identity, product geometry, story quality, claims, humor and mobile readability.
- Approval binds the revision, captions, images, reference versions, schedule and destination accounts. All six live adapters have a shared approval boundary. New desk packages stay protected even during a legacy migration. Owner-approved registered product references resolve the new workflow’s canon even when no older product-brief file exists.
- Facebook, Instagram and LinkedIn receive the full reviewed carousel. LinkedIn does not append a link, alter duplicate copy, discover another author or reframe the image after approval.
- Unknown publication results, prior unconfirmed requests and final publication HTTP 5xx responses are held for reconciliation. Confirmed platform successes are retained during partial recovery.
- The old growth scheduler cannot move an approved posting time. Required-mode readiness also blocks unstamped older packages before dispatch. Operational health distinguishes drafts awaiting review from active job/delivery failures while retaining the old publishing history and backlog diagnostics. In required mode the legacy autonomous factories and pregeneration schedules do not consume the new workflow's budget.
- Budget locking now uses an OS-released SQLite lock instead of a stale sentinel. Corrupt usage ledgers fail closed. Text calls have bounded timeouts, one SDK attempt and a recoverable quota cooldown.
- The service supervises the creative worker as a separate process. HTTP requests enqueue durable work rather than running generation.
- Startup takes a consistent SQLite backup before importing the worker and running migrations. Dependencies are pinned to the verified Python 3.12 environment. CI checks dependency consistency, JavaScript syntax and the Python suite.

## Brand sources and retained assets

Authoritative prompt inputs include `data/marketing/founder_brand_manifesto.json`, `data/marketing/human_truth/constitution.json`, and `data/marketing/infenergy_company_knowledge.json`. Selected products load the existing product brief and consumer profile. Approved visual reference versions supply the actual character/product images and identity requirements. Recent concepts and owner-rated examples give the editor repetition and quality context.

The existing company knowledge, product briefs, consumer profiles, format contracts, publishing history, backlog, provider integrations and advanced workspace are retained. Tests must not leave synthetic changes in those tracked data files.

## Runtime and deployment

For direct Railway uploads, set `BUILD_GIT_SHA` to the exact archived commit and `BUILD_TIMESTAMP` to the build time. Upload a clean `git archive` so ignored canonical source files are retained and local credentials/test artifacts are excluded. The Social Engine currently uses direct deployments; its GitHub deployment trigger is disconnected. Other services keep their existing source settings.

Continue using `python -u social_engine/start.py`. It makes the pre-migration database backup, starts HTTP, and supervises `scripts/production_worker.py`. The standalone worker supports `--once` for diagnostics. `PRODUCTION_WORKER_ENABLED=false` disables supervision when running a separate worker process.

Use a single Railway service replica with its existing persistent volume and shared `DATA_DIR`. This implementation does not add a paid queue service. SQLite is not a substitute for a shared database/queue across independent replicas. A future multi-replica deployment needs a shared transactional database and object storage.

Required configuration:

- Existing owner bearer password and `DATA_DIR`/`INVENTORY_DB_FILE`.
- Existing Gemini API key and valid text/image model routes. Credentials remain in Railway variables; no secrets belong in source control or browser code.
- `PUBLIC_BASE_URL` as the service HTTPS origin, or the Railway public domain fallback.
- Exact enabled destination IDs and existing provider credentials. LinkedIn uses the explicitly approved organization/person author instead of silently discovering one.
- Existing daily total/image limits. Monetary estimates require operator-supplied prices and their source; the app does not invent provider prices or actual dollar bills.
- `CONTENT_DISPATCH_ENABLED=true` to run the delivery scheduler. The creative worker itself never publishes.
- `EDITORIAL_APPROVAL_MODE=required` is the default. Legacy unstamped payloads require the explicit `legacy` migration switch. This switch restores older publishing behavior for those payloads; avoid leaving it enabled unintentionally. WordPress/video flows retained in the advanced workspace have not been moved into the image/carousel desk's approval schema.
- Tesseract and a scalable font. Nixpacks installs Tesseract, DejaVu and fontconfig. `INFENERGY_FONT_PATH` can select a specific installed font file.

`/api/os/production/*` uses the existing owner authentication. Asset uploads accept at most 8 MB; only that authenticated route accepts a larger encoded request body. Local asset access resolves symlinks and rejects paths outside the data directory. Public `/media` files are intentionally shareable by the social platforms.

## Backup and rollback

Startup creates `DATA_DIR/backups/before-production-desk-v1.sqlite` with SQLite's online backup API, verifies integrity, and records checksum/count metadata in the adjacent JSON file. The first verified backup is retained rather than overwritten on restart. It uses the existing service's volume and requires no new account-wide SSH credential.

Migrations add tables/columns; they do not erase the outbox or publishing history. To roll back application code, redeploy the previously successful image or original deployment branch. Do not restore an old database over newer publication receipts casually: doing so can lose confirmed delivery history and permit duplicate posts. Any database restoration must preserve/reconcile newer receipts first.

## Verification and limits

`tests/test_production_workflow.py` covers revisions, approval invalidation, exact media/account/caption binding, atomic claims, paid-job reconciliation, receipt reuse, complete draft sequencing, budget pauses and crash recovery, actual reference bytes, local typesetting/OCR, all three reviewed carousel adapters, unconfirmed delivery holds, source-aware prompts and database backup integrity.

`tools/verify_production_desk.py` exercises the authenticated browser flow using synthetic content, real local typesetting/OCR and publishing disabled. It checks desktop/mobile rendering, uploads, layout controls, review, approval and outbox scheduling, and writes a JSON report and screenshots. Install Playwright separately and pass a local Chromium path. It makes zero paid AI calls or social posts.

Unit/browser tests establish software behavior. Live model availability, provider credentials, actual generation, permissions and deployment health require production verification. Audience traction and improved creative quality require owner review and real observations; no virality score, invented engagement result or guaranteed cost saving is presented as measured evidence. The live verifier in `tools/verify_railway_production.py` checks the deployed UI, authentication, backup and provider connections; its optional generation exercise creates an unapproved draft within the configured budget. It never publishes.

Measurements are currently sourced manual entries; automatic platform analytics collection and provider dollar billing reconciliation are not implemented.
