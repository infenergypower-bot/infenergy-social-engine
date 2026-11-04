from __future__ import annotations

import re
from typing import Any


_METRIC_PATTERN = re.compile(
    r"\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\s?(?:Wh|mAh|W|kW|V|A|hours?|%)\b",
    flags=re.IGNORECASE,
)

_RUNTIME_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\s?hours?\b", flags=re.IGNORECASE)
_PRICE_PATTERN = re.compile(r"\$\s?\d{1,4}(?:[\.,]\d{2})?", flags=re.IGNORECASE)

_PRODUCT_DOMAIN_PATTERNS = {
    "water": re.compile(r"\b(?:water\s+(?:filter|purifier|filtration)|filter\s+straw|purification)\b", re.IGNORECASE),
    "lighting": re.compile(r"\b(?:light(?:ing)?|lamp|bulb|lantern)\b", re.IGNORECASE),
    "mobility": re.compile(r"\b(?:e-?bike|electric\s+bike|bicycle|scooter)\b", re.IGNORECASE),
    "power": re.compile(r"\b(?:power\s+station|generator|inverter|battery|charger|power\s+bank|solar\s+panel)\b", re.IGNORECASE),
}

_FOREIGN_SUBJECT_PATTERNS = {
    "water": re.compile(
        r"\b(?:solar\s+panels?|battery\s+(?:capacity|runtime|charging)|power\s+stations?|generators?|inverters?|wattage)\b",
        re.IGNORECASE,
    ),
    "lighting": re.compile(r"\b(?:water\s+(?:filter|purifier|filtration)|e-?bike\s+(?:range|grade|motor))\b", re.IGNORECASE),
    "mobility": re.compile(r"\b(?:water\s+(?:filter|purifier|filtration)|refrigerator\s+runtime|solar\s+panel\s+output)\b", re.IGNORECASE),
    "power": re.compile(r"\b(?:water\s+(?:filter|purifier|filtration)|e-?bike\s+(?:range|grade|motor))\b", re.IGNORECASE),
}


class ValidationResult(dict):
    @property
    def passed(self) -> bool:
        return bool(self.get("passed", False))


def _collect_text(content: dict[str, Any]) -> str:
    parts = [
        str(content.get("wp_content", "")),
        str(content.get("fb_caption", "")),
        str(content.get("ig_caption", "")),
        str(content.get("li_text", "")),
    ]
    thought = content.get('generation_thought') or {}
    parts.extend(str(thought.get(key) or '') for key in ['statement','expansion','action','image_scene'])
    return "\n".join(parts)


def _parse_numeric_tokens(text: str) -> set[str]:
    return {m.strip().lower() for m in _METRIC_PATTERN.findall(text or "")}


def _contains_testimonial_like_claim(text: str) -> bool:
    low = (text or "").lower()
    triggers = (
        "customer said",
        "customer review",
        "testimonial",
        "our client",
        "real customer story",
    )
    return any(t in low for t in triggers)


def _product_domain(content: dict[str, Any]) -> str:
    categories = content.get("product_categories") or []
    if isinstance(categories, str):
        categories = [categories]
    evidence = " ".join(
        [
            *(str(item) for item in categories if str(item).strip()),
            str(content.get("product_name", "")),
            str(content.get("product_facts", "")),
        ]
    )
    for domain, pattern in _PRODUCT_DOMAIN_PATTERNS.items():
        if pattern.search(evidence):
            return domain
    return ""


def _strategy_subject(content: dict[str, Any]) -> str:
    brief = content.get("strategic_brief") if isinstance(content.get("strategic_brief"), dict) else {}
    topic_path = brief.get("topic_path") if isinstance(brief.get("topic_path"), dict) else {}
    return " ".join(
        str(value)
        for value in (
            content.get("selected_hook", ""),
            topic_path.get("angle", ""),
            brief.get("angle", ""),
        )
        if str(value).strip()
    )


def validate_generated_content(content: dict[str, Any]) -> ValidationResult:
    """Validate product-bound claims before publishing.

    This is a conservative, rule-based validator intended to block risky
    content when product evidence is missing or mismatched.
    """

    errors: list[str] = []
    warnings: list[str] = []

    text = _collect_text(content)
    product_name = str(content.get("product_name", "")).strip()
    if not product_name and re.search(r'\binfenergy[ -]+(?:power station|battery|solar panel|generator)\b', text, re.I):
        errors.append('invented_infenergy_hardware')
    if 'solar' in product_name.lower() and re.search(r'\bdirect(?:ly)?\b[^.!?\n]{0,100}\b(?:van|vehicle|house|car|auxiliary)\b[^.!?\n]{0,45}\b(?:battery|port|inlet)\b',text,re.I):
        errors.append('unverified_direct_solar_battery_connection')
    if product_name and re.search(r'\b(?:keeps?|ensures?|guarantees?)\b[^.!?\n]{0,90}\b(?:all (?:day|night|evening)|fully (?:charged?|tops?)|tops? off)\b', text,re.I):
        errors.append('unverified_charge_or_runtime_guarantee')
    product_metrics = {str(x).strip().lower() for x in (content.get("product_metrics", []) or []) if str(x).strip()}
    product_facts = str(content.get("product_facts", "") or "")
    verified_facts = content.get("product_verified_facts") or []
    if isinstance(verified_facts, str):
        verified_facts = [verified_facts]
    verified_product_text = " ".join(
        [product_name, product_facts, *(str(fact) for fact in verified_facts if str(fact).strip())]
    ).lower()
    product_price = str(content.get("product_price", "") or "").strip()
    product_sale_price = str(content.get("product_sale_price", "") or "").strip()
    product_url = str(content.get("product_url", "") or content.get("destination_url", "")).strip()
    in_stock = str(content.get("product_in_stock", "") or "").strip().lower()

    if product_name:
        if not product_url:
            errors.append("product_url_missing")

        if in_stock in ("0", "false", "no", "outofstock", "out of stock"):
            errors.append("product_unavailable_or_out_of_stock")

        generated_metrics = _parse_numeric_tokens(text)
        for token in generated_metrics:
            if token not in product_metrics and token not in verified_product_text:
                if "wh" in token or "mah" in token:
                    errors.append(f"capacity_not_verified:{token}")
                elif "w" in token or "kw" in token:
                    errors.append(f"wattage_not_verified:{token}")
                elif "hour" in token:
                    errors.append(f"runtime_not_verified:{token}")
                else:
                    warnings.append(f"numeric_claim_not_verified:{token}")

        if _RUNTIME_PATTERN.search(text) and "hour" not in verified_product_text:
            errors.append("runtime_claim_not_supported")

        prices_mentioned = _PRICE_PATTERN.findall(text)
        valid_prices = set()
        if product_price:
            valid_prices.add(f"${product_price}")
        if product_sale_price:
            valid_prices.add(f"${product_sale_price}")
        if prices_mentioned and not valid_prices:
            errors.append("price_claim_without_product_price")
        if valid_prices:
            for p in prices_mentioned:
                norm = p.replace(" ", "")
                if norm not in {v.replace(' ', '') for v in valid_prices}:
                    errors.append(f"price_mismatch:{p}")

        low_text = text.lower()
        if "compatible with" in low_text and "compat" not in verified_product_text:
            errors.append("compatibility_not_verified")

        product_domain = _product_domain(content)
        foreign_subject = _FOREIGN_SUBJECT_PATTERNS.get(product_domain)
        strategy_subject = _strategy_subject(content)
        if foreign_subject and strategy_subject and foreign_subject.search(strategy_subject):
            errors.append(f"topic_product_semantic_mismatch:{product_domain}")

        if _contains_testimonial_like_claim(text):
            errors.append("testimonial_or_customer_claim_unverified")

        image_url = str(content.get("product_image_url", "") or "")
        image_candidates = [str(x) for x in (content.get("product_image_candidates", []) or [])]
        if image_url and image_candidates and image_url not in image_candidates:
            errors.append("image_candidate_mismatch")

    passed = len(errors) == 0
    return ValidationResult(
        {
            "passed": passed,
            "errors": errors,
            "warnings": warnings,
            "hard_failure": not passed,
        }
    )
