"""Build and persist a 30-day company-truth social content calendar."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import textwrap
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from company_knowledge import agent_specialization, knowledge_digest, load_company_knowledge, refresh_persistent_company_knowledge
from content_operations import archive_candidate, cancel_unpublished_inventory, create_council_session, daily_status, ensure_daily_slots, mark_ready, replace_unpublished_slot
from agents.learning_context import load_operational_learning
from inventory_db import get_db_path
from social.carousel_director import OFFICIAL_LOGO_URL, normalize_slide_dicts
from social.scene_message import scene_message_direction, visual_story_instruction
from social_visuals import build_product_image_approval
from posting_schedule import first_scheduled_at, growth_schedule
from consumer_life import assess_copy_fidelity, validate_consumer_receipt


DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
ELITE_SLATE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "marketing", "elite_monthly_slate.json"))
PLATFORMS = ("facebook", "instagram", "linkedin")
MAX_CALENDAR_DAYS = 120
WEEKLY_BRAND_MIX = "weekly_brand_mix"
CONTENT_PLAN_120 = "content_plan_120"
BACKGROUND_ROTATION = ("#10212B", "#F7F4EC", "#DCEEF2", "#4F7658", "#E45B3A")
INK_BY_BACKGROUND = {
    "#10212B": "#F7F4EC",
    "#F7F4EC": "#10212B",
    "#DCEEF2": "#10212B",
    "#4F7658": "#F7F4EC",
    "#E45B3A": "#F7F4EC",
}
OFFICIAL_LOGO_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "brand", "infenergy_official_logo.png"))

CONSUMER_MOMENTS = (
    "keeping phones, lights, and communication available during an outage",
    "protecting work, caregiving, and household routines when grid power is interrupted",
    "taking dependable power into travel, outdoor, and mobile-work settings",
    "making a calm plan before severe weather enters the forecast",
    "matching essential devices to realistic runtime and recharge needs",
    "helping family and neighbors understand the first practical readiness step",
)

SUPERHERO_QUOTES = (
    ("Prepared is a decision you make before the lights go out.", "Eleven stands in a lived-in kitchen at dusk while a family calmly checks its readiness plan."),
    ("Real power is staying useful when the day changes without permission.", "Eleven helps a small business owner keep one essential task moving during a neighborhood outage."),
    ("Calm is not luck. It is a plan you can reach in the dark.", "Eleven kneels beside an organized emergency shelf as storm light moves across the room."),
    ("The strongest backup plan protects people, not just devices.", "Eleven checks on an older neighbor while practical lights and communication remain available."),
    ("Readiness turns uncertainty into the next clear move.", "Eleven points a family toward three labeled priorities on a power-readiness checklist."),
)

HISTORICAL_MISSION_FACTS = (
    {"event": "the Northeast blackout of 2003", "fact": "The August 2003 blackout interrupted power across parts of the United States and Canada and showed how widely one grid disturbance can travel.", "source": "https://www.energy.gov/oe/august-2003-blackout"},
    {"event": "Hurricane Katrina in 2005", "fact": "Hurricane Katrina damaged critical Gulf Coast infrastructure and demonstrated how a disaster can disrupt power, transportation, communication, and daily care at the same time.", "source": "https://www.nhc.noaa.gov/data/tcr/AL122005_Katrina.pdf"},
    {"event": "Hurricane Sandy in 2012", "fact": "Hurricane Sandy caused extensive power outages across the Northeast, leaving households and communities to manage prolonged disruptions after the storm passed.", "source": "https://www.nhc.noaa.gov/data/tcr/AL182012_Sandy.pdf"},
    {"event": "Hurricane Maria in 2017", "fact": "Hurricane Maria devastated Puerto Rico's electric grid and made the human cost of long-duration power loss impossible to ignore.", "source": "https://www.gao.gov/products/gao-18-472"},
    {"event": "the February 2021 Texas winter storm", "fact": "The February 2021 cold-weather event caused widespread generation failures and outages, reinforcing that energy readiness is not limited to hurricane season.", "source": "https://www.ferc.gov/media/february-2021-cold-weather-outages-texas-and-south-central-united-states-ferc-nerc-and"},
)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "arialbd.ttf" if bold else "arial.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrapped_lines(text: str, width: int) -> list[str]:
    return textwrap.wrap(str(text).strip(), width=width, break_long_words=False, break_on_hyphens=False) or [""]


def _load_locked_canon_references() -> list[str]:
    base_url = os.environ.get("ENTERTAINMENT_STUDIO_URL", "").strip().rstrip("/")
    if not base_url:
        return []
    request = urllib.request.Request(
        f"{base_url}/api/studio",
        headers={"Accept": "application/json", "Authorization": f"Bearer {os.environ.get('ENTERTAINMENT_STUDIO_TOKEN', '').strip()}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            snapshot = json.load(response)
    except Exception:
        return []
    canons = snapshot.get("canons") if isinstance(snapshot, dict) else []
    locked = next((canon for canon in canons or [] if isinstance(canon, dict) and canon.get("status") == "LOCKED"), None)
    assets = locked.get("assets") if isinstance(locked, dict) and isinstance(locked.get("assets"), list) else []
    ranked = sorted(
        (asset for asset in assets if isinstance(asset, dict) and asset.get("id")),
        key=lambda asset: int(asset.get("authorityRank") or 0),
        reverse=True,
    )
    return [f"{base_url}/api/assets/{asset['id']}" for asset in ranked[:4]]


def _load_current_news(limit: int) -> list[dict[str, str]]:
    query = urllib.parse.quote("power outage OR hurricane OR energy business when:7d")
    request = urllib.request.Request(
        f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en",
        headers={"User-Agent": "InfenergySocial/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            root = ET.fromstring(response.read())
    except Exception:
        return []
    items: list[dict[str, str]] = []
    for item in root.findall("./channel/item"):
        title = str(item.findtext("title") or "").strip()
        link = str(item.findtext("link") or "").strip()
        published = str(item.findtext("pubDate") or "").strip()
        if title and link:
            items.append({"title": title, "url": link, "published": published})
        if len(items) >= limit:
            break
    return items


def _synthetic_thought(*, identifier: str, kind: str, content_type: str, statement: str, expansion: str, image_scene: str, visual_execution: str = "editorial_scene", format_name: str = "single", source_note: str = "", slides: list[dict[str, str]] | None = None, characters: list[str] | None = None, reference_image_urls: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": identifier,
        "pillar": "outage_readiness",
        "kind": kind,
        "content_type": content_type,
        "format": format_name,
        "statement": statement,
        "overlay_text": statement,
        "expansion": expansion,
        "useful_detail": "Connect the lesson to one realistic household, work, travel, or community decision.",
        "action": "Choose one essential routine and identify what it needs to continue safely.",
        "prompt": "What would your first practical move be?",
        "linkedin_lens": "Resilience becomes useful when a broad lesson is translated into an operational decision.",
        "instagram_hook": statement,
        "hashtags": ["Infenergy", "PowerReadiness", "PracticalPower"],
        "visual_motif": image_scene,
        "image_scene": image_scene,
        "visual_execution": visual_execution,
        "source_note": source_note,
        "slides": slides or [],
        "characters": characters or [],
        "reference_image_urls": reference_image_urls or [],
        "audience": "households, caregivers, small businesses, travelers, and community-minded consumers",
        "editorial_mode": kind,
    }


def _plan_entry_thought(entry: dict[str, Any]) -> dict[str, Any]:
    exact_text = entry.get("exact_visible_text") if isinstance(entry.get("exact_visible_text"), list) else []
    statement = str((exact_text or [entry.get("title") or entry.get("hook") or "Power the next move."])[0]).strip()
    if statement.upper().startswith("POV:"):
        statement = statement[4:].strip()
    support = str(entry.get("support_statement") or entry.get("takeaway") or entry.get("story") or "").strip()
    product = entry.get("product") if isinstance(entry.get("product"), dict) else {}
    is_comic = entry.get("format") in {"product_micro_mission_comic", "product_comic_strip_carousel", "product_story_page"}
    is_comic_carousel = entry.get("format") == "product_comic_strip_carousel"
    is_story_page = entry.get("format") == "product_story_page"
    is_company_message = entry.get("format") == "infenergy_company_quote_visual"
    story_sequence = [str(item) for item in entry.get("story_sequence", []) if str(item).strip()]
    generation_contract = deepcopy(entry)
    editorial_pillar = str(entry.get("editorial_pillar") or "preparedness_education")
    copy_form = str(entry.get("copy_form") or "direct_preparedness_advice")
    return {
        "id": f"PLAN120-D{int(entry.get('day_number') or 0):03d}",
        "slot": str(entry.get("slot") or "midday"),
        "pillar": str(entry.get("creative_territory") or (entry.get("company_source") or {}).get("pillar") or "everyday_power"),
        "kind": "product_micro_mission" if is_comic else "company_super_message" if is_company_message else "planned_editorial",
        "content_type": "product" if product else "editorial",
        "product_id": str(entry.get("product_id") or product.get("product_id") or ""),
        "format": "carousel" if is_comic_carousel else "single",
        "statement": statement,
        "overlay_text": statement,
        "expansion": support or str(entry.get("hook") or statement),
        "useful_detail": str(entry.get("product_proof_direction") or entry.get("human_reality") or ""),
        "action": str(entry.get("cta") or "Stay prepared. Stay connected."),
        "prompt": str(entry.get("natural_response") or entry.get("cta") or "What will you keep ready?"),
        "linkedin_lens": str(entry.get("brain_movement") or support),
        "instagram_hook": statement,
        "hashtags": ["Infenergy", "PowerReadiness", "PracticalPower"],
        "visual_motif": str(entry.get("visual_reveal") or entry.get("story") or statement),
        "image_scene": " ".join(story_sequence) if story_sequence else str(entry.get("story") or entry.get("infenergy_action") or statement),
        "visual_execution": "three-frame product comic strip carousel" if is_comic_carousel else "single vertical three-panel Story page" if is_story_page else "single vertical three-panel product comic" if is_comic else "single-frame integrated typography" if is_company_message else str(entry.get("format_label") or "editorial_scene"),
        "characters": ["Infenergy"] if entry.get("canon_required") or is_comic or is_company_message else [],
        "canon_required": bool(entry.get("canon_required") or is_comic or is_company_message),
        "weekly_role": "comic_strip_carousel" if is_comic_carousel else "story_page" if is_story_page else "micro_mission" if is_comic else "superhero_quote" if is_company_message else "planned_editorial",
        "slides": [
            {"role": f"panel_{index}", "headline": item, "supporting": ""}
            for index, item in enumerate(story_sequence, start=1)
        ],
        "generation_contract": generation_contract,
        "consumer_root": deepcopy(entry.get("consumer_root") or {}),
        "consumer_root_id": str(entry.get("consumer_root_id") or ""),
        "consumer_world_id": str(entry.get("consumer_world_id") or ""),
        "consumer_moment_id": str(entry.get("consumer_moment_id") or ""),
        "consumer_receipt": deepcopy(entry.get("consumer_receipt") or {}),
        "consumer_story_contract": deepcopy(entry.get("consumer_story_contract") or {}),
        "consumer_selection_receipt": deepcopy(entry.get("consumer_selection_receipt") or {}),
        "product_compatibility": deepcopy(entry.get("product_compatibility") or {}),
        "audience": str(entry.get("audience_name") or entry.get("audience_id") or ""),
        "editorial_pillar": editorial_pillar,
        "copy_form": copy_form,
        "editorial_mode": str(entry.get("entertainment_mode") or entry.get("format") or "planned_editorial"),
        "source_note": str((entry.get("company_source") or {}).get("knowledge_id") or "120-day Infenergy content plan"),
    }


def _content_plan_120_thoughts(*, data_dir: str, start: date, days: int) -> list[dict[str, Any]]:
    from content_plan_120 import build_120_day_plan

    plan = build_120_day_plan(data_dir=data_dir, start_date=start.isoformat(), days=days)
    entries = plan.get("entries") if isinstance(plan.get("entries"), list) else []
    if len(entries) != days:
        raise RuntimeError(f"content_plan_120_coverage_incomplete:{len(entries)}/{days}")
    return [_plan_entry_thought(entry) for entry in entries if isinstance(entry, dict)]


def _weekly_brand_mix_thoughts(slate: dict[str, Any], *, start: date, days: int) -> list[dict[str, Any]]:
    posts = [post for post in slate["posts"] if isinstance(post, dict)]
    products = [post for post in posts if post.get("content_type") == "product"]
    if len(products) < 3:
        raise ValueError("weekly brand mix requires at least three verified product posts")
    weeks = (days + 6) // 7
    current_news = _load_current_news(weeks)
    canon_references = _load_locked_canon_references()
    if len(current_news) < weeks:
        raise RuntimeError(f"current_news_coverage_incomplete:{len(current_news)}/{weeks}")
    if not canon_references:
        raise RuntimeError("locked_infenergy_canon_unavailable")
    compiled: list[dict[str, Any]] = []
    for week in range(weeks):
        week_number = week + 1
        week_start = start + timedelta(days=week * 7)
        weekly: list[dict[str, Any]] = []
        for product_slot in range(3):
            product = deepcopy(products[(week * 3 + product_slot) % len(products)])
            product["id"] = f"WB{week_number:02d}-P{product_slot + 1}-{product['id']}"
            product["weekly_role"] = "product"
            product["audience"] = "households, caregivers, small businesses, travelers, and community-minded consumers"
            product["expansion"] = f"{product['expansion']} Frame the benefit around {CONSUMER_MOMENTS[(week * 3 + product_slot) % len(CONSUMER_MOMENTS)]}."
            weekly.append(product)

        news = current_news[week]
        news_thought = _synthetic_thought(
            identifier=f"WB{week_number:02d}-NEWS",
            kind="current_event",
            content_type="current_event",
            statement=news["title"],
            expansion="Use the verified report as current context, explain the practical energy-readiness consequence, and avoid speculation beyond the source.",
            image_scene="A documentary editorial scene showing the human routine affected by the reported power, weather, or business event without disaster sensationalism.",
            source_note=news["url"],
        )
        news_thought["event_series"] = f"weekly-news-{week_start.isoformat()}"
        news_thought["source_published_at"] = news["published"]
        news_thought["weekly_role"] = "current_news"
        weekly.append(news_thought)

        quote, scene = SUPERHERO_QUOTES[week % len(SUPERHERO_QUOTES)]
        superhero = _synthetic_thought(
            identifier=f"WB{week_number:02d}-HERO",
            kind="infenergy_superhero_quote",
            content_type="superhero_quote",
            statement=quote,
            expansion="Infenergy's superhero turns the quote into a coherent human scene where readiness is visible through behavior, not spectacle.",
            image_scene=scene,
            visual_execution="canon_character_quote_scene",
            characters=["Eleven"],
            reference_image_urls=canon_references,
        )
        superhero["weekly_role"] = "superhero_quote"
        superhero["canon_required"] = True
        weekly.append(superhero)

        mission_slides = [
            {"role": "mission", "headline": f"Micro-Mission {week_number}", "supporting": "Protect one essential routine before the next interruption."},
            {"role": "notice", "headline": "Name the routine", "supporting": "Choose communication, light, care, work, food, or mobility."},
            {"role": "inventory", "headline": "List what it uses", "supporting": "Write down every device the routine actually depends on."},
            {"role": "priority", "headline": "Choose the essential three", "supporting": "Separate must-continue needs from conveniences."},
            {"role": "demand", "headline": "Check real demand", "supporting": "Use published device and product information, not guesses."},
            {"role": "duration", "headline": "Set a time target", "supporting": "Decide how long the routine needs support."},
            {"role": "recharge", "headline": "Plan the refill", "supporting": "Identify where and how power can be restored safely."},
            {"role": "rehearse", "headline": "Run one rehearsal", "supporting": "Practice the sequence before pressure makes decisions harder."},
            {"role": "share", "headline": "Share the plan", "supporting": "Make sure everyone knows the first move and the limits."},
        ]
        mission = _synthetic_thought(
            identifier=f"WB{week_number:02d}-MISSION",
            kind="micro_mission",
            content_type="micro_mission",
            statement=f"This week's Micro-Mission: protect one essential routine.",
            expansion="A nine-step practical exercise turns preparedness from a vague intention into one completed household action.",
            image_scene="Nine distinct, full-canvas editorial scenes that progress through one realistic power-readiness mission.",
            visual_execution="full_canvas_nine_slide_carousel",
            format_name="carousel",
            slides=mission_slides,
            characters=["Eleven"],
            reference_image_urls=canon_references,
        )
        mission["weekly_role"] = "micro_mission"
        mission["canon_required"] = True
        weekly.append(mission)

        history = HISTORICAL_MISSION_FACTS[week % len(HISTORICAL_MISSION_FACTS)]
        historical = _synthetic_thought(
            identifier=f"WB{week_number:02d}-HISTORY",
            kind="historical_mission",
            content_type="historical_mission",
            statement=f"What {history['event']} still teaches about readiness.",
            expansion=f"{history['fact']} This is why Infenergy exists: to make practical continuity more understandable and reachable before disruption becomes personal.",
            image_scene=f"A respectful documentary composition connecting an archival visual cue from {history['event']} to a present-day household making a calm readiness plan.",
            visual_execution="sourced_historical_editorial",
            source_note=history["source"],
        )
        historical["weekly_role"] = "historical_mission"
        weekly.append(historical)
        compiled.extend(weekly)
    return compiled[:days]


def _render_card(path: str, *, headline: str, supporting: str, pillar: str, index: int, slide_label: str = "", role: str = "STORY", canvas: tuple[int, int] = (1080, 1080)) -> None:
    background = BACKGROUND_ROTATION[index % len(BACKGROUND_ROTATION)]
    ink = INK_BY_BACKGROUND[background]
    accent = "#F2C94C" if background != "#F7F4EC" else "#E45B3A"
    width, height = canvas
    image = Image.new("RGB", canvas, background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((72, 70, 92, height - 70), fill=accent)
    draw.rectangle((92, 70, width - 72, 78), fill=accent)
    draw.text((132, 116), "INFENERGY", font=_font(34, bold=True), fill=ink)
    draw.text((132, 166), pillar.replace("_", " ").upper(), font=_font(22, bold=True), fill=accent)
    if slide_label:
        draw.text((900, 116), slide_label, font=_font(22, bold=True), fill=ink, anchor="ra")

    headline_lines = _wrapped_lines(headline, 25 if len(headline) > 65 else 22)
    headline_size = 68 if len(headline_lines) <= 4 else 56
    y = max(300, int(height * 0.25))
    for line in headline_lines[:6]:
        draw.text((132, y), line, font=_font(headline_size, bold=True), fill=ink)
        y += headline_size + 20

    if supporting:
        y = max(y + 34, int(height * 0.68))
        for line in _wrapped_lines(supporting, 48)[:4]:
            draw.text((132, y), line, font=_font(31), fill=ink)
            y += 45

    if role == "FINALE":
        with Image.open(OFFICIAL_LOGO_PATH) as logo_source:
            logo = logo_source.convert("RGBA")
            logo.thumbnail((210, 130), Image.Resampling.LANCZOS)
        image.paste(logo, (width - 282, height - 260), logo)

    draw.text((132, height - 96), "PRACTICAL POWER FOR REAL LIFE", font=_font(20, bold=True), fill=ink)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image.save(path, format="PNG", optimize=True)


def _draw_fitted_text(draw: ImageDraw.ImageDraw, text: str, box: tuple[int, int, int, int], *, fill: str, max_size: int, min_size: int = 24, bold: bool = False) -> None:
    left, top, right, bottom = box
    for size in range(max_size, min_size - 1, -2):
        font = _font(size, bold=bold)
        lines = _wrapped_lines(text, max(12, int((right - left) / (size * 0.56))))
        line_height = int(size * 1.18)
        if len(lines) * line_height <= bottom - top:
            for line_index, line in enumerate(lines):
                draw.text((left, top + line_index * line_height), line, font=font, fill=fill)
            return


@lru_cache(maxsize=32)
def _load_product_reference(url: str) -> Image.Image | None:
    if not url.startswith("https://"):
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "InfenergySocial/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            with Image.open(io.BytesIO(response.read())) as source:
                return source.convert("RGBA")
    except Exception:
        return None


def _render_product_story_page(path: str, *, contract: dict[str, Any], receipt: dict[str, Any], pillar: str, product_reference_url: str = "") -> list[str]:
    canvas = contract.get("canvas_px") if isinstance(contract.get("canvas_px"), dict) else {}
    width = int(canvas.get("width") or 1080)
    height = int(canvas.get("height") or 1920)
    image = Image.new("RGB", (width, height), "#F4F0E7")
    draw = ImageDraw.Draw(image)
    visible = contract.get("visible_text") if isinstance(contract.get("visible_text"), dict) else {}
    panels = (
        ("#DDE9E6", "#10212B", str(visible.get("headline") or contract.get("hook") or "")),
        ("#F2C94C", "#10212B", str(visible.get("infenergy_line") or contract.get("takeaway") or "")),
        ("#10212B", "#F7F4EC", str(visible.get("resolution_line") or contract.get("cta") or "")),
    )
    review_issues: list[str] = []
    product_reference = _load_product_reference(product_reference_url)
    if product_reference is None:
        review_issues.append("verified_product_reference_unavailable")
    margin = 54
    gap = 18
    header_height = 132
    panel_height = int((height - header_height - (margin * 2) - (gap * 2)) / 3)

    draw.rectangle((0, 0, width, header_height), fill="#F4F0E7")
    draw.text((margin, 38), "INFENERGY", font=_font(34, bold=True), fill="#10212B")
    draw.text((width - margin, 45), pillar.replace("_", " ").upper(), font=_font(20, bold=True), fill="#4F7658", anchor="ra")

    for panel_index, (background, ink, copy) in enumerate(panels):
        top = header_height + margin + panel_index * (panel_height + gap)
        bottom = top + panel_height
        draw.rounded_rectangle((margin, top, width - margin, bottom), radius=8, fill=background)
        draw.text((margin + 34, top + 28), f"0{panel_index + 1}", font=_font(22, bold=True), fill=ink)

        if panel_index == 0:
            court_y = bottom - 118
            draw.line((margin + 26, court_y, width - margin - 26, court_y), fill="#7AA49B", width=5)
            draw.ellipse((width - 328, top + 74, width - 98, top + 304), outline="#7AA49B", width=8)
            draw.rounded_rectangle((width - 290, top + 126, width - 136, top + 224), radius=10, fill="#10212B")
            draw.text((width - 213, top + 144), "3%", font=_font(47, bold=True), fill="#F2C94C", anchor="ma")
            scene = " / ".join(filter(None, (str(receipt.get("who") or ""), str(receipt.get("where") or ""), str(receipt.get("doing") or ""))))
            _draw_fitted_text(draw, scene.upper(), (margin + 34, bottom - 88, width - margin - 34, bottom - 30), fill=ink, max_size=20, min_size=16, bold=True)
        elif panel_index == 1:
            center_x = width - 208
            center_y = top + 176
            draw.ellipse((center_x - 82, center_y - 82, center_x + 82, center_y + 82), fill="#F7F4EC")
            draw.line((center_x, center_y - 48, center_x, center_y + 18), fill="#10212B", width=18)
            draw.ellipse((center_x - 11, center_y + 43, center_x + 11, center_y + 65), fill="#10212B")
        else:
            product_name = str(contract.get("product_name") or (contract.get("product") or {}).get("product_name") or "PORTABLE POWER")
            if product_reference is not None:
                product = product_reference.copy()
                product.thumbnail((250, 250), Image.Resampling.LANCZOS)
                product_x = width - margin - 34 - product.width
                product_y = top + 74
                draw.rounded_rectangle((product_x - 14, product_y - 14, product_x + product.width + 14, product_y + product.height + 14), radius=8, fill="#FFFFFF")
                image.paste(product, (product_x, product_y), product)
                phone_left = width - 238
                phone_top = top + 374
                draw.rounded_rectangle((phone_left, phone_top, phone_left + 82, phone_top + 142), radius=14, outline="#F7F4EC", width=5)
                draw.line((product_x + int(product.width * 0.7), product_y + product.height + 8, phone_left + 41, phone_top), fill="#F2C94C", width=6)
                draw.ellipse((phone_left + 33, phone_top + 116, phone_left + 49, phone_top + 132), fill="#F2C94C")
            _draw_fitted_text(draw, product_name.upper(), (width - 354, top + 334, width - 88, top + 368), fill="#F2C94C", max_size=15, min_size=12, bold=True)

        _draw_fitted_text(
            draw,
            copy,
            (margin + 34, top + 92, width - 390, bottom - 120),
            fill=ink,
            max_size=48 if panel_index else 52,
            min_size=28,
            bold=panel_index == 0,
        )

    draw.text((margin, height - 38), "FREEDOM, DESIGNED", font=_font(18, bold=True), fill="#10212B")
    draw.text((width - margin, height - 38), "INFENERGYPOWER.COM", font=_font(18, bold=True), fill="#10212B", anchor="ra")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image.save(path, format="PNG", optimize=True)
    return review_issues


def _carousel_slides(thought: dict[str, Any]) -> list[dict[str, str]]:
    custom = thought.get("slides") if isinstance(thought.get("slides"), list) else []
    if len(custom) >= 2:
        narrative = [
            {
                "role": str(slide.get("role") or f"slide_{index}"),
                "headline": str(slide.get("headline") or ""),
                "supporting": str(slide.get("supporting") or ""),
            }
            for index, slide in enumerate(custom[:10], start=1)
            if isinstance(slide, dict)
        ]
    else:
        narrative = [
            {"role": "thought", "headline": thought["statement"], "supporting": ""},
            {"role": "meaning", "headline": "Why this matters", "supporting": thought["expansion"]},
            {"role": "application", "headline": "Make it practical", "supporting": _application_for(thought["pillar"])},
            {"role": "community_question", "headline": thought["prompt"], "supporting": "Bring one clear answer into your power plan."},
        ]
    return normalize_slide_dicts(
        narrative,
        title=str(thought.get("mission_title") or thought.get("overlay_text") or thought.get("statement") or "INFENERGY MICRO MISSION"),
        moral=str(thought.get("moral") or thought.get("expansion") or ""),
        call_to_action=str(thought.get("cta") or "GO TO INFENERGY"),
    )


def _load_product_brief(data_dir: str, product_id: str) -> dict[str, Any]:
    candidates = (
        os.path.join(data_dir, "product_briefs", f"{product_id}.json"),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "product_briefs", f"{product_id}.json")),
    )
    for path in candidates:
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            brief = json.load(handle)
        if isinstance(brief, dict) and str(brief.get("product_id") or "") == product_id:
            return brief
    raise ValueError(f"verified product brief not found: {product_id}")


def _gemini_generation_plan(thought: dict[str, Any], product: dict[str, Any] | None = None) -> dict[str, Any]:
    contract = thought.get("generation_contract") if isinstance(thought.get("generation_contract"), dict) else {}
    is_product_comic = contract.get("format") in {"product_micro_mission_comic", "product_comic_strip_carousel", "product_story_page"}
    is_comic_carousel = contract.get("format") == "product_comic_strip_carousel"
    is_story_page = contract.get("format") == "product_story_page"
    is_company_message = contract.get("format") == "infenergy_company_quote_visual"
    output_ratio = "4:5" if is_comic_carousel or is_company_message else "9:16" if is_product_comic else "1:1"
    output_shape = "portrait 4:5" if is_comic_carousel or is_company_message else "vertical 9:16" if is_product_comic else "square 1:1"
    slides = [
        {
            "role": str(slide.get("role") or f"panel_{index}"),
            "headline": str(slide.get("headline") or ""),
            "supporting": str(slide.get("supporting") or ""),
        }
        for index, slide in enumerate(thought.get("slides") or [], start=1)
        if isinstance(slide, dict)
    ] if is_comic_carousel else _carousel_slides(thought) if thought.get("format") == "carousel" else [
        {"role": "thought", "headline": thought["statement"], "supporting": thought["expansion"]}
    ]
    visual_execution = str(thought.get("visual_execution") or "editorial_scene")
    image_scene = str(thought.get("image_scene") or thought["visual_motif"]).strip()
    product_instruction = ""
    if product:
        product_instruction = (
            f"The attached reference image is the exact {product['name']}. Reproduce that exact product's shape, proportions, "
            "color, controls, ports, markings, and physical details without redesigning or substituting a generic device. "
            f"Stage it naturally as follows: {product['visual_direction']} "
        )
    characters = [str(character) for character in thought.get("characters", []) if str(character).strip()]
    character_instruction = ""
    if characters:
        character_instruction = (
            f"Feature the canonical Infenergy character {', '.join(characters)} and preserve identity, face, body, chest logo, costume, and behavior from the attached authority references. "
            "Do not redesign, approximate, recolor, or substitute the character. "
        )
    prompts: list[dict[str, Any]] = []
    for slide_index, slide in enumerate(slides, start=1):
        role = str(slide.get("role") or "STORY").upper()
        overlay_copy = str(
            slide.get("headline")
            if len(slides) > 1
            else thought.get("overlay_text") or thought["statement"]
        ).strip()
        framing_instruction = (
            "Design a high-impact original Infenergy mission cover with the mission title as the first delivery frame. "
            if role == "COVER"
            else "Show the ending revelation and its useful human outcome as the final delivery frame. "
            if role == "FINALE"
            else ""
        )
        contract_instruction = ""
        if is_product_comic:
            sequence = " ".join(str(item) for item in contract.get("story_sequence", []) if str(item).strip())
            integration = contract.get("product_integration") if isinstance(contract.get("product_integration"), dict) else {}
            delivery_instruction = (
                "Deliver this ONE panel as a separate 1080x1350 portrait 4:5 carousel image; the three generated images form one sequential swipeable comic strip carousel. "
                if is_comic_carousel else
                "Deliver ONE 1080x1920 vertical 9:16 Story page containing exactly THREE clearly separated, sequential comic panels; never return separate images or a carousel. "
            )
            contract_instruction = (
                delivery_instruction
                +
                f"Entertainment mode: {contract.get('entertainment_mode')}. Entertainment direction: {contract.get('entertainment_hook')} "
                f"Required sequence: {sequence} Product plot rule: {integration.get('role')} {integration.get('plot_test')} "
                f"Claim boundary: {integration.get('boundary')} Humor enabled: {bool(contract.get('humor_enabled'))}. {contract.get('humor_guardrail')} "
                "Preserve clean safe-area regions in each panel for the exact copy supplied at image-generation time. "
            )
        elif is_company_message:
            contract_instruction = (
                "Deliver ONE 1080x1350 portrait 4:5 image with a clean, intentional region reserved for the exact message supplied at image-generation time. "
                f"The reserved region should visually support {contract.get('typography_material')}; Infenergy action: {contract.get('infenergy_action')} "
                "Do not use a floating quote card or a generic character pose. "
            )
        scene_prompt = (
            f"Create one premium, photorealistic {output_shape} editorial image for Infenergy Power about practical energy readiness. "
            f"The image must express this exact post and no generic substitute: {thought['statement']} "
            f"Authored scene: {image_scene} Scene meaning: {slide.get('headline', '')}. "
            f"Visual execution: {visual_execution}. {contract_instruction}{product_instruction}{character_instruction}{framing_instruction}"
            f"This is slide {slide_index} of {len(slides)} with the role {slide['role']}. "
            "Show a believable real-life environment, natural human stakes, physically credible portable-energy context, "
            "cinematic directional light and rich material detail. Compose the scene and approved wording together. "
            "Use a restrained palette of charcoal, deep navy, warm amber, clean white, and natural environmental colors. "
            "Do not render any words, letters, logos, numbers, labels, watermarks, UI, badges, product claims, or fake specifications except the exact approved copy supplied at image-generation time. "
            "Avoid generic stock-photo smiles, disaster sensationalism, dominant purple, floating devices, visual clutter, and synthetic poster styling. "
            f"Output one finished {output_ratio} image only."
        )
        visible_text = contract.get("visible_text") if isinstance(contract.get("visible_text"), dict) else {}
        message = scene_message_direction(
            headline=str(overlay_copy if len(slides) > 1 else visible_text.get("headline") or overlay_copy),
            supporting=str(slide.get("supporting") or "") if len(slides) > 1 else str(visible_text.get("infenergy_line") or slide.get("supporting") or ""),
            resolution=str(visible_text.get("resolution_line") or ""),
            problem=str(thought["statement"]),
            outcome=str(thought["expansion"]),
            scene=image_scene,
            comic=is_story_page,
        )
        scene_prompt += "\n\n" + visual_story_instruction(message["visual_story"])
        scene_prompt += "\n\n" + message["typography_render_instruction"]
        prompts.append({
            "slide_index": slide_index,
            "slide_count": len(slides),
            "role": slide["role"],
            "gemini_image_prompt": scene_prompt,
            "prompt_sha256": hashlib.sha256(scene_prompt.encode("utf-8")).hexdigest(),
            "v5_direction": {
                "semantic_role": role,
                "official_logo_path": OFFICIAL_LOGO_PATH if role == "FINALE" else "",
                "official_logo_url": OFFICIAL_LOGO_URL if role == "FINALE" else "",
                **message,
            },
        })
    return {
        "provider": "gemini",
        "model_env": "GEMINI_IMAGE_MODEL",
        "generation_timing": "post_time_before_any_platform_publish",
        "strict_provider": True,
        "fallback_allowed": False,
        "reuse_across_platforms": True,
        "required_image_count": len(prompts),
        "aspect_ratio": output_ratio,
        "generation_contract": contract,
        "reference_image_urls": list(dict.fromkeys([
            *[str(url) for url in thought.get("reference_image_urls", []) if str(url).startswith("http")],
            *([OFFICIAL_LOGO_URL] if len(slides) > 1 else []),
        ])),
        "prompts": prompts,
    }


def _application_for(pillar: str) -> str:
    return {
        "preparedness_mindset": "Choose one small readiness action and make it repeatable.",
        "everyday_power": "Notice where a normal day depends on access to a fixed outlet.",
        "outage_readiness": "List the first three devices or routines your household would protect.",
        "travel_and_outdoors": "Map the devices, duration, weight, and recharge access for the trip.",
        "power_literacy": "Compare device demand and expected duration with published specifications.",
        "community_resilience": "Share locations, limits, and first steps with the people in the plan.",
    }.get(pillar, "Turn the thought into one clear next step.")


def _captions(thought: dict[str, Any]) -> dict[str, str]:
    statement = str(thought["statement"])
    expansion = str(thought["expansion"])
    useful_detail = str(thought.get("useful_detail") or "").strip()
    action = str(thought.get("action") or _application_for(str(thought["pillar"]))).strip()
    prompt = str(thought["prompt"])
    humor = str(thought.get("humor") or "").strip()
    linkedin_lens = str(thought.get("linkedin_lens") or "").strip()
    instagram_hook = str(thought.get("instagram_hook") or statement).strip()
    tags = thought.get("hashtags") if isinstance(thought.get("hashtags"), list) else []
    tag_pool = [
        *tags, "InfenergyPower", "PowerReadiness", "PracticalPower", "BackupPower",
        "Preparedness", "PowerPlanning", "EnergyLiteracy", "HomeResilience",
        "BusinessContinuity", "PowerBackupTips",
    ]
    normalized_tags = list(dict.fromkeys(str(tag).strip().lstrip("#") for tag in tag_pool if str(tag).strip()))[:10]
    hashtag_line = " ".join(f"#{tag}" for tag in normalized_tags)
    receipt = thought.get("consumer_receipt") if isinstance(thought.get("consumer_receipt"), dict) else {}
    contract = thought.get("generation_contract") if isinstance(thought.get("generation_contract"), dict) else {}
    if contract.get("format") == "product_story_page":
        visible = contract.get("visible_text") if isinstance(contract.get("visible_text"), dict) else {}
        scene = ""
        if receipt:
            scene = f"A {receipt.get('who')} is {str(receipt.get('doing') or '').lower()} at the {receipt.get('where')} when the smallest dependency starts running the whole routine."
        core = [
            str(visible.get("headline") or statement),
            scene,
            str(visible.get("resolution_line") or expansion),
            action,
            " ".join(f"#{tag}" for tag in normalized_tags[:5]),
        ]
        linkedin = [str(visible.get("infenergy_line") or statement), *core[1:]]
        return {
            "facebook": "\n\n".join(block for block in core if block),
            "instagram": "\n\n".join(block for block in core if block),
            "linkedin": "\n\n".join(block for block in linkedin if block),
        }
    copy_form = str(thought.get("copy_form") or "")
    consumer_blocks = []
    if receipt:
        consumer_blocks = [
            f"Picture {receipt.get('who')} at {receipt.get('where')}, {str(receipt.get('doing') or '').lower()}.",
            f"The friction: {receipt.get('friction')}",
            f"What matters next: {receipt.get('useful_discovery')}",
            f"Try this now: {receipt.get('immediate_action')}",
        ]
    if not copy_form:
        facebook_blocks = [statement, *consumer_blocks, expansion, useful_detail, humor, action, prompt, hashtag_line]
        instagram_blocks = [instagram_hook, *consumer_blocks, expansion, humor, action, prompt, hashtag_line]
        linkedin_blocks = [statement, *consumer_blocks, expansion, useful_detail, linkedin_lens, action, prompt, hashtag_line]
        return {
            "facebook": "\n\n".join(block for block in facebook_blocks if block),
            "instagram": "\n\n".join(block for block in instagram_blocks if block),
            "linkedin": "\n\n".join(block for block in linkedin_blocks if block),
        }
    forms = {
        "direct_preparedness_advice": [statement, expansion, useful_detail, action],
        "worked_example": [statement, useful_detail, expansion, action],
        "decision_rule": [statement, expansion, action, useful_detail],
        "evidence_standard": [statement, useful_detail, linkedin_lens, action],
        "company_principle": [statement, expansion, humor, action],
        "single_community_prompt": [statement, expansion, useful_detail, prompt],
        "cost_saving_recommendation": [statement, action, expansion, useful_detail],
        "travel_scenario_advice": [instagram_hook, expansion, action, useful_detail],
        "caregiver_safety_advice": [statement, action, useful_detail, expansion],
        "operational_recommendation": [statement, linkedin_lens, useful_detail, action],
    }
    core_blocks = forms.get(copy_form, forms["direct_preparedness_advice"])
    facebook_blocks = [*consumer_blocks, *core_blocks, hashtag_line]
    instagram_blocks = [instagram_hook, *consumer_blocks, *core_blocks[1:], hashtag_line]
    linkedin_blocks = [statement, *consumer_blocks, *core_blocks[1:], linkedin_lens if linkedin_lens not in core_blocks else "", hashtag_line]
    return {
        "facebook": "\n\n".join(block for block in facebook_blocks if block),
        "instagram": "\n\n".join(block for block in instagram_blocks if block),
        "linkedin": "\n\n".join(block for block in linkedin_blocks if block),
    }


def _public_url(file_name: str) -> str:
    base = str(os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip().rstrip("/")
    if base and not base.startswith("http"):
        base = f"https://{base}"
    return f"{base}/media/{file_name}" if base else ""


def _render_assets(data_dir: str, thought: dict[str, Any], index: int) -> dict[str, Any]:
    folder = os.path.join(data_dir, "public_media")
    stem = f"monthly_{index + 1:02d}_{thought['id'].lower()}"
    contract = thought.get("generation_contract") if isinstance(thought.get("generation_contract"), dict) else {}
    if contract.get("format") == "product_comic_strip_carousel":
        slides = [
            {
                "role": str(slide.get("role") or f"panel_{slide_index}"),
                "headline": str(slide.get("headline") or ""),
                "supporting": str(slide.get("supporting") or ""),
            }
            for slide_index, slide in enumerate(thought.get("slides") or [], start=1)
            if isinstance(slide, dict)
        ]
    elif thought.get("format") == "carousel":
        slides = _carousel_slides(thought)
    elif thought.get("visual_execution") == "statement_graphic":
        slides = [{"role": "statement", "headline": thought["overlay_text"], "supporting": ""}]
    else:
        slides = [{"role": "thought", "headline": thought["statement"], "supporting": thought["expansion"]}]
    canvas_px = contract.get("canvas_px") if isinstance(contract.get("canvas_px"), dict) else {}
    canvas = (
        int(canvas_px.get("width") or 1080),
        int(canvas_px.get("height") or 1080),
    )
    assets: list[dict[str, str]] = []
    review_issues: list[str] = []
    product_reference_url = ""
    product_id = str(thought.get("product_id") or "")
    if contract.get("format") == "product_story_page" and product_id:
        product_reference_url = str(_load_product_brief(data_dir, product_id).get("source_image_url") or "")
    for slide_index, slide in enumerate(slides, start=1):
        file_name = f"{stem}_{slide_index}.png"
        local_path = os.path.abspath(os.path.join(folder, file_name))
        if contract.get("format") == "product_story_page":
            review_issues.extend(_render_product_story_page(
                local_path,
                contract=contract,
                receipt=thought.get("consumer_receipt") if isinstance(thought.get("consumer_receipt"), dict) else {},
                pillar=thought["pillar"],
                product_reference_url=product_reference_url,
            ))
        else:
            _render_card(
                local_path,
                headline=slide["headline"],
                supporting=slide["supporting"],
                pillar=thought["pillar"],
                index=index + slide_index - 1,
                slide_label=f"{slide_index}/{len(slides)}" if len(slides) > 1 else "",
                role=str(slide.get("role") or "STORY").upper(),
                canvas=canvas,
            )
        assets.append({
            "role": slide["role"],
            "local_path": local_path,
            "public_url": _public_url(file_name),
            "logo_url": str(slide.get("logo_url") or ""),
        })
    return {"primary": assets[0], "slides": assets, "review_issues": list(dict.fromkeys(review_issues))}


def _package(knowledge: dict[str, Any], thought: dict[str, Any], content_date: str, index: int, data_dir: str, *, defer_images: bool = False) -> dict[str, Any]:
    is_plan_120 = str(thought.get("id") or "").startswith("PLAN120-")
    defer_rendering = defer_images or is_plan_120
    assets = {"primary": {"local_path": "", "public_url": ""}, "slides": [], "review_issues": []} if defer_rendering else _render_assets(data_dir, thought, index)
    captions = _captions(thought)
    public_captions = {platform: "" for platform in PLATFORMS} if is_plan_120 else captions
    digest = knowledge_digest(knowledge)
    thought_digest = hashlib.sha256(json.dumps(thought, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
    content_id = hashlib.sha256(f"{content_date}:{thought['id']}:{digest}:{thought_digest}".encode("utf-8")).hexdigest()[:20]
    product_id = str(thought.get("product_id") or "").strip()
    product = _load_product_brief(data_dir, product_id) if product_id else None
    generation_plan = _gemini_generation_plan(thought, product)
    platform_posts = {
        platform: {
            "platform": platform,
            "final_caption": public_captions[platform],
            "caption": public_captions[platform],
            "destination_url": "https://www.infenergypower.com",
            "content_format": (
                "carousel" if thought.get("format") == "carousel" and platform in ("facebook", "instagram")
                else "single_image_product" if product
                else "single_image_thought"
            ),
            "final_caption_qa": {
                "status": "PENDING_GEMINI" if is_plan_120 else "PRESENTATION_READY",
                "reasons": ["strict_gemini_copy_required"] if is_plan_120 else [],
            },
        }
        for platform in PLATFORMS
    }
    primary_path = assets["primary"]["local_path"]
    primary_url = assets["primary"]["public_url"]
    package = {
        "content_id": content_id,
        "post_id": content_id,
        "content_date": content_date,
        "content_mode": "product_education" if product else "company_thought",
        "thought_id": thought["id"],
        "thought_kind": thought["kind"],
        "thought_statement": thought["statement"],
        "weekly_role": str(thought.get("weekly_role") or ""),
        "characters": [str(character) for character in thought.get("characters", []) if str(character).strip()],
        "canon_required": bool(thought.get("canon_required")),
        "reference_image_urls": [str(url) for url in thought.get("reference_image_urls", []) if str(url).startswith("http")],
        "content_type": str(thought.get("content_type") or "editorial"),
        "editorial_pillar": str(thought.get("editorial_pillar") or thought.get("pillar") or ""),
        "copy_form": str(thought.get("copy_form") or thought.get("editorial_mode") or ""),
        "event_series": str(thought.get("event_series") or ""),
        "editorial_sources": [str(thought.get("source_note"))] if thought.get("source_note") else [],
        "topic": thought["pillar"],
        "pillar": thought["pillar"],
        "product_id": product_id or None,
        "product_name": str(product.get("name") or "") if product else "",
        "product_image_url": str(product.get("source_image_url") or "") if product else "",
        "product_verified_facts": list(product.get("verified_facts") or []) if product else [],
        "product_proof_rule": str(product.get("proof_rule") or "") if product else "",
        "copy_generation_source": "pending_strict_gemini",
        "gemini_copy": {
            "provider": "gemini",
            "task": "copy_editing",
            "status": "PENDING",
            "strict_provider": True,
            "fallback_allowed": False,
            "generation_timing": "before_image_generation",
            "required_outputs": ["statement", "expansion", "action", "visible_text", "platform_captions"],
            "source_statement": thought["statement"],
        },
        "generation_thought": thought,
        "generation_contract": deepcopy(thought.get("generation_contract") or {}),
        "consumer_root": deepcopy(thought.get("consumer_root") or {}),
        "consumer_root_id": str(thought.get("consumer_root_id") or ""),
        "consumer_world_id": str(thought.get("consumer_world_id") or ""),
        "consumer_moment_id": str(thought.get("consumer_moment_id") or ""),
        "consumer_receipt": deepcopy(thought.get("consumer_receipt") or {}),
        "consumer_story_contract": deepcopy(thought.get("consumer_story_contract") or {}),
        "consumer_selection_receipt": deepcopy(thought.get("consumer_selection_receipt") or {}),
        "product_compatibility": deepcopy(thought.get("product_compatibility") or {}),
        "company_knowledge": {
            "knowledge_id": knowledge.get("knowledge_id"),
            "schema_version": knowledge.get("schema_version"),
            "digest": digest,
        },
        "agent_specializations": {
            name: agent_specialization(knowledge, name)
            for name in ("copywriter_agent", "creative_director_agent", "channel_editor_agent", "qa_agent")
        },
        "operational_learning": load_operational_learning(data_dir),
        "master_copy": {
            key: thought.get(key)
            for key in ("statement", "expansion", "useful_detail", "action", "prompt", "humor", "linkedin_lens", "editorial_pillar", "copy_form", "editorial_mode", "audience", "source_note", "overlay_text")
            if thought.get(key)
        },
        "fb_caption": public_captions["facebook"],
        "ig_caption": public_captions["instagram"],
        "li_text": public_captions["linkedin"],
        "platform_posts": platform_posts,
        "routing": {"platforms": list(PLATFORMS)},
        "destination_url": "https://www.infenergypower.com",
        "visual_plan": {
            "creative_route": "PREMIUM_PRODUCT_HERO" if product else "BRANDED_COMPANY_THOUGHT",
            "visual_format": "CAROUSEL" if thought.get("format") == "carousel" else "SINGLE_IMAGE",
            "visual_execution": str(thought.get("visual_execution") or "editorial_scene"),
            "visual_motif": thought["visual_motif"],
            "image_scene": str(thought.get("image_scene") or thought["visual_motif"]),
            "consumer_story_contract": deepcopy(thought.get("consumer_story_contract") or {}),
            "copy_visual_alignment": thought["statement"],
            "carousel_slides": _carousel_slides(thought) if thought.get("format") == "carousel" else [],
        },
        "gemini_generation": generation_plan,
        "carousel_assets": assets["slides"] if thought.get("format") == "carousel" else [],
        "generated_visuals": {} if defer_rendering else {
            "facebook": primary_path,
            "instagram": primary_path,
            "linkedin": primary_path,
            "render_engines": {
                platform: "deterministic_product_story_renderer" if (thought.get("generation_contract") or {}).get("format") == "product_story_page" else "company_truth_renderer"
                for platform in PLATFORMS
            },
            "artifact_reviews": {
                platform: {
                    "verdict": "PASS" if not assets.get("review_issues") else "REGENERATE_VISUAL",
                    "issues": list(assets.get("review_issues") or []),
                    "contract_checks": ["three_distinct_panels", "approved_visible_text", "product_in_plot"] if (thought.get("generation_contract") or {}).get("format") == "product_story_page" else [],
                }
                for platform in PLATFORMS
            },
        },
        "primary_publish_image_url": primary_url,
        "validation_status": "passed",
        "validation_errors": [],
        "quality_warnings": [],
        "publish_decision": {"decision": "publish", "publishable": True, "reasons": [], "source": "canonical_company_truth"},
    }
    product_image_approval = build_product_image_approval(package, package["product_image_url"])
    if product_image_approval:
        package["product_image_approval"] = product_image_approval
    if package.get("consumer_root"):
        consumer_qa = validate_consumer_receipt(package)
        copy_fidelity = {"passed": True, "checks": {}, "missing": [], "status": "PENDING_GEMINI"} if is_plan_120 else assess_copy_fidelity(package)
        package["consumer_receipt_qa"] = consumer_qa
        package["consumer_copy_fidelity"] = copy_fidelity
        if not consumer_qa["passed"] or not copy_fidelity["passed"]:
            consumer_errors = [*consumer_qa["errors"], *[f"consumer_copy_missing:{field}" for field in copy_fidelity["missing"]]]
            package["validation_status"] = "failed"
            package["validation_errors"].extend(consumer_errors)
            package["publish_decision"] = {
                "decision": "reject",
                "publishable": False,
                "reasons": consumer_errors,
                "source": "consumer_receipt_qa",
            }
    return package


def _existing_content_ids(data_dir: str) -> set[str]:
    if not os.path.exists(get_db_path(data_dir)):
        return set()
    connection = sqlite3.connect(get_db_path(data_dir))
    try:
        rows = connection.execute("SELECT content_id FROM content_outbox WHERE status != 'CANCELLED'").fetchall()
        return {str(row[0]) for row in rows}
    except sqlite3.OperationalError:
        return set()
    finally:
        connection.close()


def _save_calendar(data_dir: str, payload: dict[str, Any]) -> str:
    folder = os.path.join(data_dir, "social", "monthly")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"company_truth_calendar_{payload['start_date']}_{payload['end_date']}.json")
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    os.replace(temporary, path)
    return path


def _load_editorial_slate() -> dict[str, Any]:
    with open(ELITE_SLATE_PATH, "r", encoding="utf-8") as handle:
        slate = json.load(handle)
    posts = slate.get("posts") if isinstance(slate, dict) else None
    if not isinstance(posts, list) or len(posts) != 30:
        raise ValueError("elite monthly slate must contain exactly 30 posts")
    identifiers = [str(post.get("id") or "") for post in posts if isinstance(post, dict)]
    if len(identifiers) != 30 or len(set(identifiers)) != 30:
        raise ValueError("elite monthly slate post identifiers must be complete and unique")
    return slate


def latest_monthly_calendar(data_dir: str = DATA_DIR) -> dict[str, Any]:
    folder = os.path.join(data_dir, "social", "monthly")
    if not os.path.isdir(folder):
        return {}
    paths = sorted(
        (os.path.join(folder, name) for name in os.listdir(folder) if name.endswith(".json")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not paths:
        return {}
    with open(paths[0], "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        payload["calendar_path"] = paths[0]
        return payload
    return {}


def prepare_monthly_gemini_prompts(data_dir: str = DATA_DIR) -> dict[str, Any]:
    calendar = latest_monthly_calendar(data_dir)
    entries = calendar.get("entries") if isinstance(calendar.get("entries"), list) else []
    if not entries:
        raise RuntimeError("monthly_content_not_found")
    queued_entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("outbox_id")]
    slate = _load_editorial_slate()
    thoughts = {
        str(thought.get("id") or ""): thought
        for thought in slate["posts"]
        if isinstance(thought, dict)
    }
    connection = sqlite3.connect(get_db_path(data_dir), timeout=30)
    prepared_entries = 0
    prepared_prompts = 0
    prepared_at = datetime.now(timezone.utc).isoformat()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for entry in queued_entries:
            thought = thoughts.get(str(entry.get("thought_id") or ""))
            package = entry.get("package") if isinstance(entry.get("package"), dict) else {}
            if not thought and isinstance(package.get("generation_thought"), dict):
                thought = package["generation_thought"]
            outbox_id = str(entry.get("outbox_id") or "")
            if not thought or not package or not outbox_id:
                continue
            product_id = str(thought.get("product_id") or "").strip()
            product = _load_product_brief(data_dir, product_id) if product_id else None
            generation = _gemini_generation_plan(thought, product)
            generation["status"] = "PROMPTS_READY"
            generation["prompts_prepared_at_utc"] = prepared_at
            package["gemini_generation"] = generation
            linkedin = ((package.get("platform_posts") or {}).get("linkedin") or {})
            if isinstance(linkedin, dict):
                linkedin["content_format"] = "single_image_thought"
            changed = connection.execute(
                "UPDATE content_outbox SET package_json=? WHERE outbox_id=? AND status IN ('READY', 'DUE')",
                (json.dumps(package, ensure_ascii=True, separators=(",", ":"), default=str), outbox_id),
            ).rowcount
            if changed != 1:
                continue
            entry["package"] = package
            prepared_entries += 1
            prepared_prompts += int(generation["required_image_count"])
        if prepared_entries != len(queued_entries):
            raise RuntimeError(f"monthly_prompt_preparation_incomplete:{prepared_entries}/{len(queued_entries)}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    calendar["gemini_prompt_status"] = "READY"
    calendar["gemini_prompts_prepared_at_utc"] = prepared_at
    calendar["gemini_prompt_count"] = prepared_prompts
    calendar_path = _save_calendar(data_dir, calendar)
    return {
        "status": "READY",
        "prepared_entries": prepared_entries,
        "prepared_prompts": prepared_prompts,
        "calendar_path": calendar_path,
        "prepared_at_utc": prepared_at,
    }


def build_monthly_calendar(
    *,
    data_dir: str = DATA_DIR,
    start_date: str | date | None = None,
    days: int = 30,
    enqueue: bool = True,
    replace_unpublished: bool = False,
    content_plan: str | None = None,
) -> dict[str, Any]:
    if days < 1 or days > MAX_CALENDAR_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_CALENDAR_DAYS}")
    if content_plan not in (None, "", WEEKLY_BRAND_MIX, CONTENT_PLAN_120):
        raise ValueError(f"unsupported content plan: {content_plan}")
    knowledge_refresh = (
        refresh_persistent_company_knowledge(data_dir)
        if replace_unpublished
        else {"status": "USED_EXISTING", "path": None, "backup_path": None}
    )
    knowledge = load_company_knowledge(data_dir)
    slate = _load_editorial_slate()
    start = date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
    start = start or (datetime.now(timezone.utc).date() + timedelta(days=1))
    if content_plan == CONTENT_PLAN_120:
        thoughts = _content_plan_120_thoughts(data_dir=data_dir, start=start, days=days)
    elif content_plan == WEEKLY_BRAND_MIX:
        thoughts = _weekly_brand_mix_thoughts(slate, start=start, days=days)
    else:
        thoughts = [deepcopy(slate["posts"][index % len(slate["posts"])]) for index in range(days)]
    packages = [
        _package(
            knowledge,
            thought,
            (start + timedelta(days=index)).isoformat(),
            index,
            data_dir,
            defer_images=False,
        )
        for index, thought in enumerate(thoughts)
    ]
    existing_ids = _existing_content_ids(data_dir)
    cancellation = cancel_unpublished_inventory(data_dir) if enqueue and replace_unpublished else {"cancelled_outbox": 0}
    if cancellation["cancelled_outbox"]:
        existing_ids = _existing_content_ids(data_dir)
    entries: list[dict[str, Any]] = []
    queued = 0
    skipped_existing = 0

    for index in range(days):
        day = start + timedelta(days=index)
        thought = thoughts[index]
        package = packages[index]
        scheduled = first_scheduled_at(day)
        assigned_slot = str(thought.get("slot") or "midday").strip().lower()
        if assigned_slot not in {"morning", "midday", "evening"}:
            assigned_slot = "midday"
        package["platform_schedule"] = growth_schedule(day)
        entry = {
            "date": day.isoformat(),
            "scheduled_at": scheduled,
            "slot": assigned_slot,
            "content_id": package["content_id"],
            "thought_id": thought["id"],
            "format": thought["format"],
            "statement": thought["statement"],
            "package": package,
            "outbox_id": None,
        }
        if enqueue:
            ensure_daily_slots(
                data_dir,
                day,
                schedule={
                    "morning": datetime.combine(day, time(13, 0), tzinfo=timezone.utc).isoformat(),
                    "midday": scheduled,
                    "evening": datetime.combine(day, time(23, 0), tzinfo=timezone.utc).isoformat(),
                },
                platform_policy={
                    "platforms": list(PLATFORMS),
                    "source": "company_truth_month",
                    "required_slots": [assigned_slot],
                },
            )
            assigned = next(
                (slot for slot in daily_status(data_dir, day)["slots"] if slot["slot"] == assigned_slot),
                None,
            )
            date_already_covered = bool(
                assigned
                and assigned.get("outbox_id")
                and assigned.get("status") in {"READY", "DUE", "CLAIMED", "PUBLISHING", "PUBLISHED"}
            )
            if package["content_id"] in existing_ids and date_already_covered:
                skipped_existing += 1
                entry["queue_status"] = f"PRESERVED_{assigned.get('status')}"
            else:
                if not replace_unpublished_slot(data_dir, day.isoformat(), assigned_slot):
                    entry["queue_status"] = "ACTIVE_OR_PUBLISHED_SLOT_PRESERVED"
                    entries.append(entry)
                    continue
                decision_id = create_council_session(
                    data_dir,
                    content_date=day.isoformat(),
                    slot=assigned_slot,
                    blackboard={
                        "company_knowledge": package["company_knowledge"],
                        "thought": package["master_copy"],
                        "content_mode": package["content_mode"],
                    },
                    rationale=["canonical_company_truth", "one_complete_post_per_day", "copy_visual_alignment"],
                )
                archive_candidate(
                    data_dir,
                    decision_id=decision_id,
                    ordinal=1,
                    content=package,
                    status="SELECTED",
                    score=100.0,
                    loss_reasons=[],
                )
                entry["outbox_id"] = mark_ready(
                    data_dir,
                    content_date=day.isoformat(),
                    slot=assigned_slot,
                    scheduled_at=scheduled,
                    decision_id=decision_id,
                    package=package,
                )
                existing_ids.add(package["content_id"])
                queued += 1
                entry["queue_status"] = "READY"
        entries.append(entry)

    covered_dates: list[str] = []
    if enqueue:
        publishable_states = {"READY", "DUE", "CLAIMED", "PUBLISHING", "PUBLISHED"}
        for index in range(days):
            requested_date = (start + timedelta(days=index)).isoformat()
            requested_slot = str(thoughts[index].get("slot") or "midday").strip().lower()
            if requested_slot not in {"morning", "midday", "evening"}:
                requested_slot = "midday"
            assigned = next(
                (slot for slot in daily_status(data_dir, requested_date)["slots"] if slot["slot"] == requested_slot),
                None,
            )
            if not assigned or assigned.get("status") not in publishable_states or not assigned.get("outbox_id"):
                raise RuntimeError(f"monthly_schedule_coverage_incomplete:{requested_date}:{requested_slot}")
            covered_dates.append(requested_date)

    payload = {
        "status": "READY",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "knowledge_id": knowledge.get("knowledge_id"),
        "knowledge_version": knowledge.get("schema_version"),
        "knowledge_digest": knowledge_digest(knowledge),
        "campaign_id": slate.get("campaign_id"),
        "editorial_standard": slate.get("editorial_standard"),
        "knowledge_refresh": knowledge_refresh,
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=days - 1)).isoformat(),
        "days": days,
        "coverage_days": len(covered_dates),
        "content_plan": content_plan or "elite_monthly_slate",
        "queued": queued,
        "skipped_existing": skipped_existing,
        "cancelled_legacy_outbox": cancellation["cancelled_outbox"],
        "single_image_posts": sum(1 for entry in entries if entry["format"] == "single"),
        "carousel_posts": sum(1 for entry in entries if entry["format"] == "carousel"),
        "product_posts": sum(1 for thought in thoughts if thought.get("content_type") == "product"),
        "statement_graphics": sum(1 for thought in thoughts if thought.get("visual_execution") == "statement_graphic"),
        "current_event_posts": sum(
            1 for thought in thoughts
            if thought.get("weekly_role") == "current_news" or (content_plan != WEEKLY_BRAND_MIX and thought.get("event_series"))
        ),
        "superhero_posts": sum(1 for thought in thoughts if thought.get("weekly_role") == "superhero_quote"),
        "micro_mission_posts": sum(1 for thought in thoughts if thought.get("weekly_role") == "micro_mission"),
        "historical_mission_posts": sum(1 for thought in thoughts if thought.get("weekly_role") == "historical_mission"),
        "entries": entries,
    }
    payload["calendar_path"] = _save_calendar(data_dir, payload)
    return payload


def main() -> None:
    print(json.dumps(build_monthly_calendar(), ensure_ascii=True, default=str))


if __name__ == "__main__":
    main()
