# INF Energy Social Engine

Production social publishing worker for INF Energy with a strategy-driven marketing layer.

## What It Does

- Generates slot-based content (`morning`, `midday`, `evening`) with Gemini and deterministic fallbacks.
- Publishes to WordPress, Facebook, Instagram, and LinkedIn.
- Applies campaign runtime controls:
  - funnel stage mapping by slot,
  - channel/day scheduling,
  - anti-repeat windows for topics/hooks/CTAs,
  - claim guardrails and quality scoring,
  - optional skip-if-recent-success behavior,
  - UTM link tagging per channel.
- Exposes worker endpoints for health and runtime state.

## Endpoints

- `GET /health` or `GET /healthz`: health and uptime.
- `GET /status`: latest run status and quality summary.
- `GET /history?limit=20`: recent post history.
- `GET /campaign`: latest generated campaign plan artifact.
- `GET /campaign-current?token=...`: latest structured weekly campaign.
- `GET /content-preview?token=...&slot=midday&platform=facebook`: generate preview content without publishing.
- `GET /schedule-preview?token=...`: show eligible platforms by slot for the next seven days.
- `GET /quality-report?token=...&limit=200`: summarize recent quality, rejections, and distribution.
- `GET /inventory-db?token=...`: inspect inventory/brand database status and current brand profile.
- `GET /inventory-sync?token=...&force=true`: re-sync product CSV and brand artifacts into the database.
- `GET /brand-profile-apply?token=...`: apply the conference brand profile package directly into the brand profile table.
- `GET /selling-ideology?token=...`: inspect the structured selling ideology profile currently in the database.
- `GET /selling-ideology-apply?token=...`: apply the conference selling ideology package directly into the selling ideology table.
- `GET /gemini-visual-repo?token=...`: inspect Gemini style reference repository and visual generation settings.
- `GET /gemini-visual-repo-seed?token=...`: seed default high-end ad style ideas into the Gemini repository.
- `GET /gemini-visual-repo-apply?token=...&active_style_keys=...&override_url=...`: update active style pack and product image override URL.
- `GET /gemini-visual-repo-bootstrap?token=...`: force automation bootstrap from Railway env vars.
- `GET /run-now?slot=morning&token=...`: manual engine run.
- `GET /run-marketing?token=...`: generate marketing strategy artifacts.
- `GET /run-weekly?token=...`: generate weekly and campaign plan artifacts.
- `GET /refresh-meta?token=...`: refresh Meta long-lived user token and page token for runtime.

All operational preview and control endpoints use the same `MANUAL_RUN_TOKEN` protection.

## Important Environment Variables

- `SOCIAL_DRY_RUN` (`true` by default)
- `MANUAL_RUN_TOKEN`
- `META_REFRESH_TOKEN` (optional; if not set, `/refresh-meta` falls back to `MANUAL_RUN_TOKEN`)
- `DATA_DIR` (optional; default `data/`)
- `INVENTORY_DB_FILE` (optional; default `inventory.db` inside `DATA_DIR`)
- `ENABLE_WORDPRESS`, `ENABLE_FACEBOOK`, `ENABLE_INSTAGRAM`, `ENABLE_LINKEDIN`
- `ENABLE_FACEBOOK_SLOTS`, `ENABLE_INSTAGRAM_SLOTS`, `ENABLE_LINKEDIN_SLOTS`, `ENABLE_WORDPRESS_SLOTS`
- `SKIP_RECENT_SUCCESS_HOURS` (default `0`, disabled)
- `ANTI_REPEAT_HOOK_WINDOW` (default `30`)
- `ANTI_REPEAT_CTA_WINDOW` (default `30`)
- `UTM_CAMPAIGN_NAME` (default `infenergy_engine`)
- `META_AUTO_REFRESH_ENABLED` (default `true`; refreshes before a run when close to expiry)
- `META_REFRESH_THRESHOLD_HOURS` (default `72`)
- `META_REFRESH_EVERY_RUN` (default `false`; set `true` to refresh Meta tokens before every run)
- `META_GRAPH_VERSION` (default `v20.0`)
- `GEMINI_API_KEY` (required for Gemini image generation)
- `VISUAL_IMAGE_STRATEGY` (recommended `gemini_generated`)
- `GEMINI_IMAGE_MODEL` (recommended `gemini-2.5-flash-image`)
- `GEMINI_STYLE_REFERENCES` (optional URL list split by `;`, `,`, or newlines)
- `VISUAL_PRODUCT_IMAGE_OVERRIDE` (optional public URL)
- `GEMINI_STYLE_ACTIVE_KEYS` (optional comma-separated style keys from repo)
- `VISUAL_REPO_AUTO_SEED` (default `true`; auto-seeds visual idea repo if empty)
- `ENTERTAINMENT_STUDIO_URL` (optional; enables canonical Eleven/LUX production through the Studio)
- `ENTERTAINMENT_STUDIO_TOKEN` (required with `ENTERTAINMENT_STUDIO_URL`; must equal the Studio's `SOCIAL_ENGINE_TOKEN`)
- `ENTERTAINMENT_STUDIO_IMAGE_PROVIDER` (`openai` by default; use `deterministic` only for local contract testing)
- `HTTP_MAX_CONCURRENT_REQUESTS` (default `8`; caps simultaneous HTTP handlers to bound memory during media bursts)

## Railway Resource Controls

The worker streams `/media/` and `/approved-assets/` responses in bounded chunks, supports byte
ranges for video playback, and returns an `ETag` so repeat requests can use `304 Not Modified`.
Do not replace these routes with
whole-file reads: concurrent image or video requests otherwise multiply each file's size in
resident memory.

For the Social Engine service, keep `HTTP_MAX_CONCURRENT_REQUESTS=8` unless Railway metrics show
that the instance has enough memory for more simultaneous transfers. Lower it to `4` on a
memory-constrained instance. Autonomous generation and scheduled publishing remain enabled; the
limit only queues excess HTTP requests.

Generated media under `DATA_DIR/public_media` is durable publication state. Back up the attached
volume before deleting files, and prune only assets that are no longer referenced by READY,
CLAIMED, PUBLISHING, or externally published packages. For large long-term libraries, use object
storage plus a CDN instead of serving the volume directly from Railway.

## Entertainment Studio Routing

The orchestrator now creates a structured `CreativeRequest` from its approved strategy, winning concept, human-truth gate, art direction, product proof, and explicit `WHAT_HAPPENS`. Every `PostPackage` and memory record retains this request for decision tracing.

Only earned canonical routes are sent to Entertainment Studio:

- `INFENERGY_CHARACTER`
- `MICRO_MISSION`
- `LUX_LED`
- `CINEMATIC_STORY`

An available catalog product does not enter a character story unless the approved strategy supplies an explicit product role and verified proof. Real-world, science, editorial, community, and ordinary product work continues through the existing Gemini provider. If Studio is unavailable or rejects a request, the provider records the reason and falls back to Gemini, then the existing deterministic template provider.

## New Files

- `scripts/build_campaign_plan.py`: builds structured weekly campaign artifacts.
- `scripts/build_utm_url.py`: generates validated UTM URLs while preserving existing query parameters.
- `scripts/anti_repeat.py`: anti-repeat windows and duplicate signature checks.
- `scripts/validate_product_claims.py`: hard validation for risky or unsupported product claims.
- `scripts/score_content.py`: weighted scoring and regeneration decision logic.
- `data/marketing/campaigns/campaign_*.json`: immutable structured campaign outputs.
- `data/marketing/funnel_config.json`: funnel distribution and stage metadata.
- `data/marketing/channel_schedule.json`: weekday and slot platform eligibility rules.
- `data/marketing/cta_library.json`: stage-specific CTA options.
- `data/marketing/anti_repeat_config.json`: repeat-protection windows.

Platform secrets (required in live mode by enabled channel):

- WordPress: `WP_URL`, `WP_USERNAME`, `WP_APP_PASSWORD`
- Facebook: `META_PAGE_ID`, `META_PAGE_ACCESS_TOKEN`
- Instagram: `META_IG_USER_ID`, `META_PAGE_ACCESS_TOKEN`
- LinkedIn: `LINKEDIN_ACCESS_TOKEN` (optional `LINKEDIN_AUTHOR_URN`)

Meta refresh secrets (required for `/refresh-meta`):

- `META_APP_ID`
- `META_APP_SECRET`
- `META_LONG_LIVED_USER_TOKEN`
- `META_PAGE_ID`

The refresh endpoint updates runtime tokens in-memory and writes state to `data/marketing/meta_token_state.json`.

## Inventory And Brand Database

The engine now uses a SQLite database as the primary source for:

- Product inventory used for product selection and fact-grounded copy
- Brand personality, voice rules, approved verbiage, and guardrails used in prompt generation
- Structured selling ideology directives used as default prompt constraints for conversion-first messaging

Location:

- `DATA_DIR/inventory.db` by default
- configurable with `INVENTORY_DB_FILE`

Bootstrap behavior:

- Products are seeded from `data/products/*.csv` if the DB is empty.
- Brand profile is seeded from `data/marketing/founder_brand_manifesto.json` and latest marketing strategy artifacts.
- Use `/inventory-sync?token=...&force=true` to re-import and overwrite DB records from source artifacts.
- Gemini visual repository automation runs on worker startup and before each slot run:
  - auto-seeds default style ideas when repo is empty,
  - imports external style reference URLs from `GEMINI_STYLE_REFERENCES`,
  - applies `VISUAL_PRODUCT_IMAGE_OVERRIDE` and `GEMINI_STYLE_ACTIVE_KEYS` into DB settings.

## Railway Cron For Meta Refresh

To keep Meta tokens fresh without local scripts, add a Railway Cron job that calls:

```text
GET https://your-service.up.railway.app/refresh-meta?token=YOUR_META_REFRESH_TOKEN
```

Recommended cadence: every 7 days.

Suggested Railway variables:

- `META_REFRESH_TOKEN` (unique random secret)
- `META_AUTO_REFRESH_ENABLED=true`
- `META_REFRESH_THRESHOLD_HOURS=72`

If `META_REFRESH_TOKEN` is not configured, the endpoint uses `MANUAL_RUN_TOKEN`.

## Adjusting Content Distribution

Edit `data/marketing/funnel_config.json`.

- `distribution` controls the target mix across `ATTENTION`, `EDUCATION`, `DESIRE`, `TRUST`, and `CONVERSION`.
- `stages` defines the objective, CTA rules, preferred formats, hook styles, and primary metric for each stage.
- Stage selection uses recent history so the next post favors the most under-served stage.

Example:

```json
{
  "distribution": {
    "ATTENTION": 0.2,
    "EDUCATION": 0.3,
    "DESIRE": 0.25,
    "TRUST": 0.15,
    "CONVERSION": 0.1
  }
}
```

## Adjusting Channel Scheduling

Edit `data/marketing/channel_schedule.json`.

- Rules are keyed by weekday and slot.
- Each item can set `platform`, `stage`, `enabled`, and `preferred_content_formats`.
- Manual platform override remains available through `POST_PLATFORMS` or `/run-now?...&platforms=facebook,instagram`.

Example:

```json
{
  "thursday": {
    "midday": [
      {
        "platform": "linkedin",
        "stage": "TRUST",
        "enabled": true,
        "preferred_content_formats": ["spec_breakdown", "authority_post"]
      }
    ]
  }
}
```

## Adding CTA Options

Edit `data/marketing/cta_library.json`.

- Each top-level key is a funnel stage.
- Keep CTAs short and single-action.
- The runtime rotates choices and avoids recent repeats.

Example:

```json
{
  "EDUCATION": [
    "Save this checklist.",
    "Read the full comparison."
  ],
  "CONVERSION": [
    "Build your backup-power setup.",
    "Shop available products."
  ]
}
```

## Changing Quality Thresholds

Edit `scripts/score_content.py` to change numeric thresholds or weighted components.

Current defaults:

- `82+`: approve when hard validation also passes.
- `75-81`: regenerate once.
- `<75`: reject.
- Hard validation failure: reject regardless of score.
- Maximum attempts per run: `2`.

Hard validation rules live in `scripts/validate_product_claims.py`.

## Previewing Content Without Publishing

Use the preview endpoint with your token:

```powershell
Invoke-WebRequest "https://your-service.up.railway.app/content-preview?token=YOUR_TOKEN&slot=midday&platform=facebook" | Select-Object -Expand Content
```

Supported query parameters:

- `slot`
- `platform`
- `funnel_stage`
- `product_id`

The endpoint generates preview content only. It does not publish.

## Running Tests

```powershell
Set-Location C:\Users\v-jmoten\infenergy-social-engine
py -3 -m unittest discover -s tests -p "test_*.py" -v
```

Tests must never create a live post.

## Dry-Run Validation

```powershell
Set-Location C:\Users\v-jmoten\infenergy-social-engine
$env:SOCIAL_DRY_RUN='true'
python scripts/run_marketing_team.py
python scripts/run_marketing_weekly.py
python scripts/run_engine.py
python -m unittest discover -s tests -p "test_*.py" -v
```

## Railway Dry Run

Recommended safe verification sequence after deployment:

1. Set `SOCIAL_DRY_RUN=true` in Railway variables.
2. Call `/run-marketing?token=...`.
3. Call `/run-weekly?token=...`.
4. Call `/content-preview?token=...&slot=morning`.
5. Call `/quality-report?token=...`.
6. Call `/run-now?slot=midday&token=...` and confirm the run completes without live publishing.

## Rollback

To roll back the campaign system safely:

1. Keep `SOCIAL_DRY_RUN=true`.
2. Revert `worker.py`, `scripts/run_engine.py`, and `scripts/generate_posts.py` to the prior version.
3. Leave `data/post_history.json` in place; older and newer history rows remain readable.
4. Ignore the new `data/marketing/*` runtime files if reverting to an earlier engine version.
5. Re-run dry-run validation before enabling live publishing again.

## Intentionally Excluded

This implementation intentionally does not include:

- automated comment replies
- automated direct messages
- weather-triggered posts
- invasive cross-site visitor tracking
- complicated lead scoring
- paid-ad automation
- automatic price discounts
- fake urgency
- fake testimonials

## Runtime Files

- `data/funnel_config.json` (legacy compatibility copy)
- `data/channel_schedule.json` (legacy compatibility copy)
- `data/post_history.json`
- `data/marketing/marketing_strategy_*.json`
- `data/marketing/weekly_plan_*.json`
- `data/marketing/campaign_plan_*.json`
- `data/marketing/campaigns/campaign_*.json`
- `data/marketing/funnel_config.json`
- `data/marketing/channel_schedule.json`
- `data/marketing/cta_library.json`
- `data/marketing/anti_repeat_config.json`
