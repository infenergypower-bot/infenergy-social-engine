from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from scripts.validate_product_claims import validate_generated_content  # noqa: E402
from scripts.score_content import score_content  # noqa: E402


class PhaseSevenEightTests(unittest.TestCase):
    def test_claim_validator_blocks_unverified_solar_connection_and_runtime(self) -> None:
        content={'product_name':'Aferiy 400W Solar Panel', 'product_url':'https://example.com/solar',
            'fb_caption':'Plug the panel directly into your van battery port. This keeps the cabin lights running all evening.'}
        errors=validate_generated_content(content)['errors']
        self.assertIn('unverified_direct_solar_battery_connection',errors)
        self.assertIn('unverified_charge_or_runtime_guarantee',errors)

    def test_claim_validator_checks_product_free_scene_for_invented_hardware(self) -> None:
        content={'fb_caption':'Check on your neighbors.',
            'generation_thought':{'image_scene':'A neighbor connects an Infenergy power station to a laptop.'}}
        self.assertIn('invented_infenergy_hardware',validate_generated_content(content)['errors'])

    def test_claim_validator_rejects_invented_wattage(self) -> None:
        content = {
            "product_name": "PowerFlex",
            "product_metrics": ["200W", "154Wh"],
            "product_facts": "Published specs include 200W and 154Wh.",
            "product_price": "499",
            "product_sale_price": "",
            "product_url": "https://example.com/powerflex",
            "product_in_stock": "1",
            "wp_content": "<p>This unit delivers 900W and 20 hours runtime.</p>",
            "fb_caption": "Great for outages.",
            "ig_caption": "Backup made simple.",
            "li_text": "Practical power planning.",
            "product_image_url": "https://example.com/img.jpg",
            "product_image_candidates": ["https://example.com/img.jpg"],
        }
        result = validate_generated_content(content)
        self.assertFalse(result["passed"])
        self.assertTrue(any("wattage_not_verified" in e for e in result["errors"]))

    def test_claim_validator_rejects_missing_product_url(self) -> None:
        content = {
            "product_name": "PowerFlex",
            "product_metrics": ["200W"],
            "product_facts": "200W",
            "product_price": "499",
            "product_url": "",
            "product_in_stock": "1",
            "wp_content": "<p>Uses verified 200W spec.</p>",
            "fb_caption": "",
            "ig_caption": "",
            "li_text": "",
        }
        result = validate_generated_content(content)
        self.assertFalse(result["passed"])
        self.assertIn("product_url_missing", result["errors"])

    def test_claim_validator_accepts_numeric_tokens_in_official_product_name(self) -> None:
        content = {
            "product_name": "SolarMax Pro 5kW-10kW",
            "product_metrics": ["240W"],
            "product_facts": "Published specifications include 240W.",
            "product_url": "https://example.com/solarmax",
            "product_in_stock": "1",
            "wp_content": "SolarMax Pro 5kW-10kW includes a published 240W option.",
            "fb_caption": "SolarMax Pro 5kW-10kW with 240W.",
            "ig_caption": "SolarMax Pro 5kW-10kW with 240W.",
            "li_text": "SolarMax Pro 5kW-10kW with 240W.",
        }

        result = validate_generated_content(content)

        self.assertTrue(result["passed"], result["errors"])

    def test_claim_validator_uses_canonical_verified_facts(self) -> None:
        content = {
            "product_name": "PowerCharge Pro",
            "product_verified_facts": ["10,000mAh battery capacity"],
            "product_url": "https://example.com/powercharge-pro",
            "product_in_stock": "1",
            "fb_caption": "The Infenergy 10,000mAh battery keeps a phone charged.",
            "ig_caption": "Connect the Infenergy 10,000mAh battery before travel.",
            "li_text": "Infenergy provides a verified 10,000mAh battery capacity.",
        }

        verified = validate_generated_content(content)
        content["fb_caption"] += " It is not a 20,000mAh battery."
        invented = validate_generated_content(content)

        self.assertTrue(verified["passed"], verified["errors"])
        self.assertFalse(invented["passed"])
        self.assertIn("capacity_not_verified:20,000mah", invented["errors"])

    def test_claim_validator_rejects_foreign_product_subject(self) -> None:
        content = {
            "product_name": "Wosfer Portable Electric Water Filter & Purifier",
            "product_categories": ["Water Purification"],
            "product_facts": "Portable six-stage water filtration for travel and emergency kits.",
            "product_url": "https://example.com/wosfer-water-filter",
            "product_in_stock": "1",
            "selected_hook": "Why solar panels rarely hit their rated output?",
            "strategic_brief": {"topic_path": {"angle": "Why solar panels rarely hit their rated output?"}},
            "wp_content": "Clean-water preparedness starts with a verified filtration method.",
            "fb_caption": "Add clean-water support to a travel kit.",
            "ig_caption": "Plan for safe water.",
            "li_text": "Water preparedness requires a suitable filtration method.",
        }

        result = validate_generated_content(content)

        self.assertFalse(result["passed"])
        self.assertIn("topic_product_semantic_mismatch:water", result["errors"])

    def test_claim_validator_allows_matching_and_product_free_subjects(self) -> None:
        matching = {
            "product_name": "Sorein Foldable Solar Panel",
            "product_categories": ["Portable Solar Panels"],
            "product_facts": "Portable solar charging equipment.",
            "product_url": "https://example.com/solar-panel",
            "product_in_stock": "1",
            "selected_hook": "Why solar panels rarely hit their rated output?",
            "fb_caption": "Panel output depends on real conditions.",
        }
        product_free = {
            "selected_hook": "Why solar panels rarely hit their rated output?",
            "fb_caption": "A practical science question.",
        }

        self.assertTrue(validate_generated_content(matching)["passed"])
        self.assertTrue(validate_generated_content(product_free)["passed"])

    def test_score_content_threshold_logic(self) -> None:
        content = {
            "selected_hook": "Most buyers miss this outage planning mistake?",
            "selected_cta": "Build your backup-power setup.",
            "funnel_stage": "CONVERSION",
            "product_name": "PowerFlex",
            "wp_content": "<p>" + ("Useful guidance with 200W and 154Wh details. " * 60) + "</p>",
            "fb_caption": "Home readiness with 20% better planning. #Energy #Backup #Home",
            "ig_caption": "Hook line\nHelpful details with 154Wh and 200W. #Power #Energy #Backup #Home #Prepared",
            "li_text": "Professional resilience framework with measurable specs.",
        }
        scored = score_content(content)
        self.assertIn("total", scored)
        self.assertIn("decision", scored)
        self.assertIn("component_scores", scored)
        self.assertIn(scored["decision"], {"approve", "regenerate_once", "reject"})


if __name__ == "__main__":
    unittest.main()
