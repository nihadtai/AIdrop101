import io
import logging
import re
import urllib.request
from typing import Any, Dict, List, Optional
from PIL import Image

logger = logging.getLogger(__name__)


_SYNONYM_GROUPS = [
    {"earbud", "earbuds", "earphone", "earphones", "headphone", "headphones", "headset", "tws"},
    {"espresso", "coffee"},
    {"ultrasonic", "ultrasound"},
    {"jewelry", "jewellery", "glasses", "eyeglass", "watch", "ring"},
    {"massager", "massage"},
    {"projector", "lamp", "light", "lighting"},
    {"blender", "juicer", "mixer"},
    {"charger", "charging"},
    {"trimmer", "shaver", "clipper"},
    {"humidifier", "diffuser", "aroma"},
]


def _expand_synonyms(words: set) -> set:
    expanded = set(words)
    for w in list(words):
        stem = w.rstrip("s")
        expanded.add(stem)
        for grp in _SYNONYM_GROUPS:
            if w in grp or stem in grp:
                expanded |= grp
    return expanded


def compute_text_similarity(target_name: str, candidate_name: str) -> int:
    """
    Computes token-based product title similarity (0-100) between the winning product
    and a supplier candidate title, enforcing core head-noun matching so unrelated
    cheap accessories (e.g. phone screen wipers or ice ball molds) never score >= 45%.
    """
    if not target_name or not candidate_name:
        return 0
    stop = {
        "for", "with", "and", "the", "new", "pro", "hot", "sale", "set", "pcs",
        "max", "ultra", "viral", "tiktok", "original", "premium", "display", "power",
    }
    generic_tail_nouns = {"maker", "cleaner", "machine", "device", "tool", "gadget", "kit", "box", "holder"}

    t_list = [w for w in re.findall(r"[a-z0-9]+", target_name.lower()) if len(w) > 2 and w not in stop]
    c_list = [w for w in re.findall(r"[a-z0-9]+", candidate_name.lower()) if len(w) > 2 and w not in stop]
    if not t_list or not c_list:
        return 20

    t_words = set(t_list)
    c_words = set(c_list)
    c_expanded = _expand_synonyms(c_words)

    matched_target_words = {w for w in t_words if w in c_expanded or w.rstrip("s") in c_expanded}
    overlap = len(matched_target_words)
    if overlap == 0:
        return 0

    # Check head noun & domain qualifier (last 2 words of target)
    head_noun = t_list[-1]
    head_matched = (head_noun in c_expanded) or (head_noun.rstrip("s") in c_expanded)

    # If the head noun is generic (e.g., 'maker', 'cleaner', 'machine'), the preceding domain noun
    # (e.g., 'espresso', 'jewelry'/'ultrasonic') MUST also match!
    domain_matched = True
    if len(t_list) >= 2 and head_noun in generic_tail_nouns:
        qualifiers = set(t_list[:-1])
        if not any((q in c_expanded or q.rstrip("s") in c_expanded) for q in qualifiers):
            domain_matched = False

    if not head_matched or not domain_matched:
        # Cap unrelated product types at 25% so they never pass the 45% relevance gate
        return min(25, int(round((overlap / len(t_words)) * 35)))

    union = len(t_words | c_words)
    jaccard = overlap / union if union else 0.0
    recall = overlap / len(t_words)
    score = (recall * 0.72 + jaccard * 0.28) * 100
    if overlap >= 2 and head_matched and domain_matched:
        score = max(score, 52.0)
    return max(0, min(100, int(round(score))))


def _dhash(img: Image.Image, hash_size: int = 8) -> int:
    """Computes a 64-bit difference hash (dHash) for fast perceptual image matching."""
    resized = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = list(resized.getdata())
    diff = 0
    for row in range(hash_size):
        row_start = row * (hash_size + 1)
        for col in range(hash_size):
            if pixels[row_start + col] > pixels[row_start + col + 1]:
                diff |= 1 << (row * hash_size + col)
    return diff


def _download_image_for_hash(url: str) -> Optional[Image.Image]:
    if not url or not url.startswith("http"):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = resp.read()
        return Image.open(io.BytesIO(data))
    except Exception:
        return None


def compute_visual_similarity(target_img_url: str, candidate_img_url: str) -> Optional[int]:
    """
    100% FREE local perceptual dHash similarity (0-100) between Agent 1's winning product image
    and the supplier's image. Returns None if either image cannot be downloaded.
    """
    if not target_img_url or not candidate_img_url:
        return None
    if target_img_url == candidate_img_url:
        return 100
    img1 = _download_image_for_hash(target_img_url)
    img2 = _download_image_for_hash(candidate_img_url)
    if img1 is None or img2 is None:
        return None
    h1 = _dhash(img1)
    h2 = _dhash(img2)
    hamming = bin(h1 ^ h2).count("1")
    # 64 bits total; <= 10 is virtually identical, >= 32 is unrelated
    similarity = max(0, min(100, int(round((1 - (hamming / 48.0)) * 100))))
    return similarity


def score_and_rank_suppliers(
    candidates: List[Dict[str, Any]],
    target_product_name: str,
    target_image_url: str = "",
    min_match_confidence: int = 45,
) -> List[Dict[str, Any]]:
    """
    Phase 2 Supplier Scoring Engine:
      - Computes Match Confidence % (text similarity + perceptual visual dHash)
      - Hard-filters out live candidates with Match Confidence < min_match_confidence (default 45%)
      - Computes weighted Supplier Score (0-100):
          * Match Confidence (Title + Visual Relevance): 55%
          * Landed Cost (Product + US/EU Shipping): 20%
          * Market Proof (Listed / Orders Volume + Live Verification): 15%
          * Delivery Speed (US/EU transit days): 10%
    """
    if not candidates:
        return []

    verified_costs = [
        (c["unit_economics"]["product_cost"] + c["unit_economics"]["shipping_cost"])
        for c in candidates
        if c.get("is_live_verified") and c.get("unit_economics", {}).get("product_cost") is not None
    ]
    min_cost = min(verified_costs) if verified_costs else 5.0
    max_cost = max(verified_costs) if verified_costs else 30.0
    cost_span = max(1.0, max_cost - min_cost)

    scored: List[Dict[str, Any]] = []
    for idx, sup in enumerate(candidates):
        ue = sup.get("unit_economics") or {}
        prod_cost = ue.get("product_cost")
        ship_cost = ue.get("shipping_cost") or 5.20

        # 1. Text & Visual Match Confidence (0-100)
        cand_title = f"{sup.get('product_name', '')} {sup.get('supplier_name', '')}".strip()
        text_sim = compute_text_similarity(target_product_name, cand_title)
        vis_sim = None
        if idx < 5 and sup.get("is_live_verified") and target_image_url and sup.get("product_image_url") and text_sim >= 35:
            vis_sim = compute_visual_similarity(target_image_url, sup["product_image_url"])

        if vis_sim is not None and text_sim >= 35:
            match_confidence = int(round(text_sim * 0.70 + vis_sim * 0.30))
        else:
            match_confidence = text_sim

        # Relevance Gate: reject unrelated live catalog items (e.g. $1.68 phone screen wipers)
        if sup.get("is_live_verified") and match_confidence < min_match_confidence:
            logger.info(
                f"Filtered out low-relevance supplier candidate '{sup.get('product_name') or sup.get('supplier_name')}' "
                f"(match_confidence={match_confidence}% < {min_match_confidence}% for target '{target_product_name}')"
            )
            continue

        # 2. Landed Cost Score (0-100; lower cost -> higher score)
        if sup.get("is_live_verified") and prod_cost is not None:
            landed = prod_cost + ship_cost
            cost_score = max(15.0, min(100.0, 100.0 - ((landed - min_cost) / cost_span) * 75.0))
        else:
            cost_score = 10.0

        # 3. Market Proof Score (0-100)
        listed = int(sup.get("listed_count") or 0)
        proof_score = 20.0
        if sup.get("is_live_verified"):
            proof_score = min(100.0, 45.0 + min(55.0, (listed / 500.0) * 55.0))

        # 4. Delivery Speed Score (0-100)
        days_str = str(sup.get("delivery_days") or "7-12")
        nums = [int(n) for n in re.findall(r"\d+", days_str)]
        avg_days = sum(nums) / len(nums) if nums else 10.0
        speed_score = max(20.0, min(100.0, 110.0 - avg_days * 4.5))

        total_score = round(
            match_confidence * 0.55
            + cost_score * 0.20
            + proof_score * 0.15
            + speed_score * 0.10,
            1,
        )

        sup["match_confidence"] = match_confidence
        sup["visual_match"] = vis_sim
        sup["supplier_score"] = total_score
        scored.append(sup)

    # Sort descending by supplier_score
    scored.sort(key=lambda x: (not x.get("is_live_verified", False), -x.get("supplier_score", 0.0)))
    return scored
