"""Three-stage LLM triage layer for the Gazette insolvency pipeline.

Sits between `analyse_notices` (which produces heuristic-scored AnalysedNotice
objects) and the email renderer. Does not replace the heuristic — blends with it.

Stage 1: batched per-notice LLM judgement (15/call, 8 concurrent, 20s timeout,
3x retry). Each notice gets an ordinal tier L1|L2|L3|watch|drop and a category.
Items missing from a returned batch get tier=unknown, abort if >5%.

Stage 1.5: pure-Python co-occurrence features (same IP firm appearing on
multiple cases today, same SIC-prefix cluster).

Stage 2: blend the existing heuristic_score with tier_base plus capped
adjustments. Default formula:
    final = 0.5 * heuristic_score + 0.5 * tier_base + clamp(adjustments, -20, 20)

Stage 3: ONE LLM call to rewrite the situation description and add a
"buyer hypothesis" line for the top N (default 10) items only.

Per-notice decisions are written to data/triage-decisions-<date>.csv for audit.

Env knobs:
    OPENROUTER_API_KEY    required (if missing the module is a no-op)
    OPENROUTER_MODEL      default openrouter/auto (OpenRouter Auto Router)
    BATCH_SIZE            default 15
    LLM_CONCURRENCY       default 8
    RETRY_LIMIT           default 3
    PER_CALL_TIMEOUT      default 30
    UNKNOWN_THRESHOLD     default 0.05
    KEEP_THRESHOLD        default "watch" (L1>L2>L3>watch>drop>unknown)
    TRIAGE_TOP_N          default 10 (Stage 3 detail count)
    HEURISTIC_WEIGHT      default 0.5
    TIER_WEIGHT           default 0.5
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# OpenRouter Auto Router: picks a model from a curated high-quality pool per
# prompt, balancing cost and quality. https://openrouter.ai/docs (auto-router)
DEFAULT_MODEL = "openrouter/auto"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# Constrain the Auto Router to open-weight model namespaces only, so it never
# routes to expensive proprietary models (Claude/GPT/Gemini/Grok). These are
# the namespaces on OpenRouter that contain *only* open-weight models - we omit
# google/*, x-ai/*, cohere/* because those mix proprietary models (Gemini,
# Grok, Command) into the same namespace. Override via OPENROUTER_ALLOWED_MODELS
# (comma-separated wildcard patterns). https://openrouter.ai/docs (auto-router)
DEFAULT_ALLOWED_MODELS = [
    "qwen/*",
    "deepseek/*",
    "meta-llama/*",
    "mistralai/*",
    "nvidia/*",
    "z-ai/*",
    "moonshotai/*",
    "nousresearch/*",
    "allenai/*",
    "microsoft/*",
]
# cost_quality_tradeoff: 0 = best capability, 10 = cheapest wins (default 7).
# We lean toward cost (8) since the pool is already all cheap open models.
DEFAULT_COST_QUALITY = 8


def _allowed_models() -> list[str]:
    raw = os.environ.get("OPENROUTER_ALLOWED_MODELS", "")
    if raw.strip():
        return [m.strip() for m in raw.split(",") if m.strip()]
    return DEFAULT_ALLOWED_MODELS


def _cost_quality() -> int:
    return int(os.environ.get("OPENROUTER_COST_QUALITY", str(DEFAULT_COST_QUALITY)))


def _auto_router_extra_body() -> dict:
    """extra_body for an Auto Router call: provider routing + the open-weight
    whitelist. The auto-router plugin only takes effect when the model is
    openrouter/auto; for a pinned model it is harmless/ignored but we omit it."""
    body: dict = {"provider": {"sort": "throughput"}}
    if _model() == "openrouter/auto":
        body["plugins"] = [{
            "id": "auto-router",
            "allowed_models": _allowed_models(),
            "cost_quality_tradeoff": _cost_quality(),
        }]
    return body

TIER_ORDER = {"drop": 0, "unknown": 0, "watch": 1, "L3": 2, "L2": 3, "L1": 4}
TIER_BASE_SCORE = {"L1": 80, "L2": 60, "L3": 40, "watch": 20, "drop": 0, "unknown": 25}

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Stage 1 system prompt — UK gazette domain
# ---------------------------------------------------------------------------

STAGE1_SYSTEM = """You are a triage analyst for a UK distressed-M&A buyer scanning London Gazette insolvency notices. You receive a small batch of recent notices and classify each.

For EACH item in the batch, output one entry. Use ordinal tier labels (not numeric scores). Within the batch, also assign a relative rank (1 = most material in this batch).

OUTPUT JSON ONLY:
{"items": [
  {"i": "<id from input>", "tier": "L1|L2|L3|watch|drop", "rank": <1..N>, "category": "<one of the categories>", "asset": "<company name>", "size_eur_m": <number_or_null>, "why": "<one sentence in plain English, no marketing>", "evidence": "<short quote from notice text>"},
  ...one per input item, same order, no duplicates...
]}

CATEGORY MUST BE ONE OF:
- pre_pack_buyer_search: just-appointed administrator, asset sale imminent, buyer search opens this week
- creditor_bid_target: CVA, distressed sale, secured creditor likely buyer
- going_concern_sale: administrators marketing the business as going concern
- solvent_distribution: members' voluntary, no urgency, no distressed estate
- noise: pure noise — striking-off notices, dissolved entities, dormant shells with no estate

TIER RUBRIC (UK insolvency-specific):
- L1 (ACT THIS WEEK): just-appointed administrator (last ~48h) at a viable trading entity with material employees, charges, real accounts, or live website. Buyer search opens within days. Insolvency practitioner named.
- L2 (SCHEDULE THIS QUARTER): 1-2 weeks post-appointment, IP-led process; or CVL/receivership at a substance entity where there is still time to register interest with the IP.
- L3 (FYI): any insolvency notice with some M&A optionality — has charges, a website, or a non-dormant SIC code — but window has passed or substance is thin.
- watch: dormant or marginal entity; sector signal worth tracking but no estate today.
- drop: pure noise — striking-off, members' voluntary winding-up of solvent shell, "no estate" notices, dissolved companies.

When in doubt between drop and L3, prefer L3.

EVIDENCE OF SUBSTANCE (push toward L1/L2):
- has_charges = true (real lenders)
- has_filed_full_accounts = true (not dormant or micro)
- website_live = true (was actively trading)
- recent_activity = true (filed within last 12 months)
- non-dormant SIC code (manufacturing, retail, hospitality, construction, etc.)
- IP from a recognised firm named in notice

EVIDENCE OF NOISE (push toward drop/watch):
- phantom_detected = true (multiple shell-company red flags)
- accounts_type = dormant
- members' voluntary winding-up (solvent — owners keep proceeds)
- striking-off / dissolution notice
- no website, no charges, no filings — likely shell

DO NOT FABRICATE deal sizes, employee numbers, or banker affiliations not in the input. If you don't know size, set size_eur_m to null.
"""

STAGE3_SYSTEM = """You are the writer for a UK distressed-M&A buyer's daily worklist. You receive a pre-ranked top set of insolvency notices and produce, for each, a tight one-paragraph situation description plus a one-line buyer hypothesis.

Each input may include an `enrichment` block from a web-grounded Stage 2.5 verification (verified status, ip_firm, case_partner, principals, buyer_window, industry, key_facts). When enrichment is present, treat it as a trusted second source and weave it into your situation/buyer_hypothesis. If enrichment.verified == "already_sold" or "stale", state that the situation is already resolved and the hypothesis is moot.

Voice: plain, factual, no marketing language. No em dashes (—) or en dashes (–) anywhere; use commas, parentheses, or split sentences instead.

Hard rules:
- NEVER fabricate names, sizes, banker affiliations, or revenue figures not in the input or enrichment.
- If a fact is missing, omit it; do not invent.
- buyer_hypothesis: one sentence on the most plausible buyer angle (sector roll-up, asset-only carve-out, going-concern bid, secured-creditor pre-pack), grounded in the notice signals (charges, SIC, website, accounts type, IP firm) AND any enrichment signals (industry, principals, buyer_window). If signals don't support a specific angle, write "no specific angle; situational shape only".
- situation: 2-3 sentences. What was the company, what stage of insolvency, who's the IP (use enrichment.ip_firm if present), what is the time window for buyer engagement (use enrichment.buyer_window if present).

Output: {"writeups": [{"id": "<id>", "situation": "...", "buyer_hypothesis": "..."}, ...]}
JSON only, no markdown.
"""


# ---------------------------------------------------------------------------
# LLM client (lazy import so module loads even without openai installed)
# ---------------------------------------------------------------------------

def _client():
    from openai import OpenAI  # local import keeps the module lazy
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    base_url = os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=int(os.environ.get("PER_CALL_TIMEOUT", "30")),
    )


def _model() -> str:
    return os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)


def _parse_json(content: str) -> dict:
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


# ---------------------------------------------------------------------------
# Notice -> compact dict
# ---------------------------------------------------------------------------

def _ip_firms(notice) -> list[str]:
    out = []
    for p in (notice.practitioners or []):
        firm = (getattr(p, "firm", "") or "").strip()
        if firm:
            out.append(firm)
    return out


def _compact(notice) -> dict:
    """Compact AnalysedNotice -> dict for the LLM. Keep payload small."""
    sigs = (notice.opportunity_signals or [])[:6]
    return {
        "i": notice.notice_id,
        "name": notice.company_name,
        "no": notice.company_number,
        "type": notice.notice_type,
        "ch_status": notice.ch_status,
        "ch_accounts": notice.ch_accounts_type,
        "ch_phantom": bool(notice.ch_is_phantom),
        "ch_charges": notice.ch_total_charges,
        "ch_outstanding": notice.ch_outstanding_charges,
        "ch_sic": (notice.ch_sic_codes or [])[:3],
        "sector": notice.sector,
        "website": bool(notice.website_url),
        "ip_firms": _ip_firms(notice)[:2],
        "heuristic_score": notice.opportunity_score,
        "heuristic_cat": notice.opportunity_category,
        "signals": sigs,
    }


# ---------------------------------------------------------------------------
# Stage 1: batched per-notice judgement
# ---------------------------------------------------------------------------

def _batch_pass(items: list[dict], date: str) -> dict:
    client = _client()
    user = (
        f"DATE (UTC): {date}\n\n"
        f"INPUT BATCH ({len(items)} notices):\n"
        f"{json.dumps(items, ensure_ascii=False)}\n\n"
        f"Output exactly {len(items)} entries in input order. Close the array and stop."
    )
    resp = client.chat.completions.create(
        model=_model(),
        messages=[
            {"role": "system", "content": STAGE1_SYSTEM},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
        max_tokens=4000,
        extra_headers={
            "HTTP-Referer": "https://github.com/diktat1/gazette-insolvencies",
            "X-Title": "gazette-insolvencies (stage1)",
        },
        extra_body=_auto_router_extra_body(),
    )
    content = resp.choices[0].message.content or ""
    parsed = _parse_json(content)
    out: dict[str, dict] = {}
    for entry in parsed.get("items", []):
        if isinstance(entry, dict) and entry.get("i"):
            out[entry["i"]] = entry
    return out


def _batch_pass_with_retry(batch: list[dict], date: str, batch_idx: int) -> dict:
    retries = int(os.environ.get("RETRY_LIMIT", "3"))
    last = None
    for attempt in range(retries):
        try:
            return _batch_pass(batch, date)
        except Exception as e:  # noqa: BLE001
            last = e
            wait = 2 ** attempt
            logger.warning(
                "[stage1:batch%d] attempt %d/%d failed: %s: %s; sleeping %ds",
                batch_idx, attempt + 1, retries, type(e).__name__, str(e)[:160], wait,
            )
            time.sleep(wait)
    logger.error("[stage1:batch%d] giving up: %s", batch_idx, last)
    return {}


def stage1_batched_judgement(notices: list, date: str) -> dict:
    batch_size = int(os.environ.get("BATCH_SIZE", "15"))
    concurrency = int(os.environ.get("LLM_CONCURRENCY", "8"))

    compact = [_compact(n) for n in notices]
    batches = [compact[i : i + batch_size] for i in range(0, len(compact), batch_size)]
    logger.info("[stage1] %d notices in %d batches of %d, concurrency=%d",
                len(compact), len(batches), batch_size, concurrency)

    classifications: dict = {}
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {
            ex.submit(_batch_pass_with_retry, batch, date, idx): idx
            for idx, batch in enumerate(batches)
        }
        done = 0
        for fut in as_completed(futs):
            idx = futs[fut]
            try:
                classifications.update(fut.result())
                done += 1
                if done % 5 == 0 or done == len(batches):
                    logger.info("[stage1] %d/%d batches done", done, len(batches))
            except Exception as e:  # noqa: BLE001
                logger.error("[stage1:batch%d] uncaught: %s", idx, e)
    elapsed = time.monotonic() - t0
    logger.info("[stage1] complete in %.1fs, %d classifications", elapsed, len(classifications))

    for n in notices:
        if n.notice_id and n.notice_id not in classifications:
            classifications[n.notice_id] = {
                "i": n.notice_id,
                "tier": "unknown",
                "rank": 999,
                "category": "",
                "asset": n.company_name,
                "size_eur_m": None,
                "why": "no classification (batch failure)",
                "evidence": "",
            }

    n_unknown = sum(1 for c in classifications.values() if c.get("tier") == "unknown")
    threshold = float(os.environ.get("UNKNOWN_THRESHOLD", "0.05"))
    if notices and n_unknown / len(notices) > threshold:
        raise RuntimeError(
            f"stage1 abort: {n_unknown}/{len(notices)} items unclassified "
            f"({n_unknown/len(notices):.1%} > {threshold:.0%})"
        )
    if n_unknown:
        logger.warning("[stage1] %d items tier=unknown (within tolerance)", n_unknown)
    return classifications


# ---------------------------------------------------------------------------
# Stage 1.5: co-occurrence (pure Python)
# ---------------------------------------------------------------------------

def stage1_5_cooccurrence(classifications: dict, notices_by_id: dict) -> dict:
    """Per-notice features: same IP firm count today, SIC-prefix cluster size."""
    ip_counts: dict[str, int] = {}
    sic_prefix_counts: dict[str, int] = {}

    for iid, cls in classifications.items():
        if cls.get("tier") in ("drop", "unknown"):
            continue
        n = notices_by_id.get(iid)
        if not n:
            continue
        for firm in _ip_firms(n):
            key = firm.lower().strip()
            if key:
                ip_counts[key] = ip_counts.get(key, 0) + 1
        for sic in (n.ch_sic_codes or []):
            pref = str(sic)[:2]
            if pref:
                sic_prefix_counts[pref] = sic_prefix_counts.get(pref, 0) + 1

    feats: dict[str, dict] = {}
    for iid, n in notices_by_id.items():
        firms = [f.lower().strip() for f in _ip_firms(n)]
        same_ip = max((ip_counts.get(f, 0) for f in firms), default=0)
        sic_prefs = [str(s)[:2] for s in (n.ch_sic_codes or [])]
        sic_cluster = max((sic_prefix_counts.get(p, 0) for p in sic_prefs), default=0)
        feats[iid] = {
            "same_ip_firm_today": same_ip,
            "sic_cluster_size": sic_cluster,
        }
    return feats


# ---------------------------------------------------------------------------
# Stage 2: blend heuristic + tier with capped adjustments
# ---------------------------------------------------------------------------

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def stage2_blend(classifications: dict, notices_by_id: dict, features: dict) -> list[dict]:
    """Return list of dicts ranked by final score descending.

    final = HEURISTIC_WEIGHT * heuristic_score
          + TIER_WEIGHT      * tier_base
          + clamp(sum(adjustments), -20, 20)
    """
    h_w = float(os.environ.get("HEURISTIC_WEIGHT", "0.5"))
    t_w = float(os.environ.get("TIER_WEIGHT", "0.5"))

    out: list[dict] = []
    for iid, cls in classifications.items():
        n = notices_by_id.get(iid)
        if not n:
            continue
        tier = cls.get("tier") or "unknown"
        if tier == "drop":
            # Still keep in audit log but downstream callers will filter
            pass

        tier_base = TIER_BASE_SCORE.get(tier, 0)
        heuristic = float(getattr(n, "opportunity_score", 0) or 0)

        # Adjustments — pulled directly from the heuristic features so we
        # don't double-count score components, only sharpen ranking signal.
        adj_phantom = -15 if n.ch_is_phantom else 0
        adj_charges = 10 if (n.ch_total_charges or 0) > 0 else 0
        adj_outstanding = 5 if (n.ch_outstanding_charges or 0) > 0 else 0
        adj_website = 5 if n.website_url else 0
        adj_accounts = -10 if (n.ch_accounts_type or "").lower() == "dormant" else 0

        feat = features.get(iid) or {}
        adj_co = 5 if (feat.get("same_ip_firm_today") or 0) >= 2 else 0

        adjustments = (adj_phantom + adj_charges + adj_outstanding
                       + adj_website + adj_accounts + adj_co)
        adjustments_capped = _clamp(adjustments, -20, 20)

        final = h_w * heuristic + t_w * tier_base + adjustments_capped

        out.append({
            "id": iid,
            "tier": tier,
            "category": cls.get("category") or "",
            "why": cls.get("why") or "",
            "evidence": cls.get("evidence") or "",
            "tier_base": tier_base,
            "heuristic_score": heuristic,
            "adjustments": adjustments_capped,
            "components": {
                "phantom": adj_phantom,
                "charges": adj_charges,
                "outstanding_charges": adj_outstanding,
                "website": adj_website,
                "accounts_dormant": adj_accounts,
                "co_ip_firm": adj_co,
            },
            "features": feat,
            "final": round(final, 1),
        })

    out.sort(key=lambda r: (-r["final"], -TIER_ORDER.get(r["tier"], 0)))
    return out


# ---------------------------------------------------------------------------
# Stage 3: focused write for top N
# ---------------------------------------------------------------------------

ENRICH_SYSTEM = """You are an enrichment researcher for a UK insolvency / distressed-asset sweep.

Given a single statutory notice already flagged as actionable (e.g. administrator appointment, voluntary winding-up at a viable trading entity), run a targeted web check to:
1. Confirm the company is still trading (or the appointment is fresh and assets are intact, not already sold).
2. Identify the named insolvency practitioner / administrator firm (Begbies Traynor, FRP, Interpath, etc.) and their case partner if visible.
3. Identify any directors, owners, or relevant principals worth contacting.
4. Estimate the buyer-search window (days/weeks until administrator publishes statement of proposals, typically 8 weeks but pre-pack deals close in days).
5. Find any signals about the company's industry, employee count, revenue, premises, brand recognition.

Return JSON only, no prose:
{
  "id": "<id from input>",
  "verified": "trading|already_sold|stale|unknown",
  "ip_firm": "<insolvency practitioner firm name or 'pending'>",
  "case_partner": "<named partner or 'pending'>",
  "principals": ["Name (Role)", ...],
  "buyer_window": "<short phrase, e.g. 'within 5 days for pre-pack', 'within 8 weeks before SoP'>",
  "industry": "<short label>",
  "key_facts": ["short fact", ...],
  "additional_sources": ["url", ...]
}

If the company turns out to be already sold or struck off, set verified accordingly so it can be demoted.
"""


def _enrich_one_notice(notice, classification: dict, date: str) -> dict | None:
    """Single web-grounded enrichment call for one top notice."""
    enrich_model = os.environ.get("ENRICH_MODEL", "qwen/qwen3-235b-a22b-2507:online")
    client = _client()
    notice_payload = json.dumps({
        'id': classification.get('id'),
        'compact': _compact(notice),
        'stage1_tier': classification.get('tier'),
        'stage1_why': classification.get('why'),
        'category': classification.get('category'),
    }, indent=2, ensure_ascii=False)
    user = (
        f"DATE (UTC): {date}\n\n"
        f"NOTICE TO VERIFY:\n{notice_payload}\n\n"
        "Run a web search to verify the company status and enrich. Output JSON only, schema in system prompt."
    )
    try:
        resp = client.chat.completions.create(
            model=enrich_model,
            messages=[
                {"role": "system", "content": ENRICH_SYSTEM},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=2000,
            extra_headers={
                "HTTP-Referer": "https://github.com/diktat1/gazette-insolvencies",
                "X-Title": "gazette-insolvencies (enrich)",
            },
        )
        return _parse_json(resp.choices[0].message.content or "")
    except Exception as e:  # noqa: BLE001
        logger.warning("[stage2.5] enrich failed for %s: %s", classification.get("id"), e)
        return None


def stage2_5_enrich(top: list[dict], notices_by_id: dict, date: str) -> dict[str, dict]:
    """Web-grounded enrichment on top notices. Returns dict[id -> enrichment]."""
    enrich_top_n = int(os.environ.get("ENRICH_TOP_N", "5"))
    targets = top[:enrich_top_n]
    if not targets:
        return {}
    logger.info("[stage2.5] enriching top %d notices via :online", len(targets))
    enrichments: dict[str, dict] = {}
    concurrency = min(4, len(targets))
    from concurrent.futures import ThreadPoolExecutor, as_completed  # local import to avoid top-of-file noise
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {
            ex.submit(_enrich_one_notice, notices_by_id.get(r["id"]), r, date): r["id"]
            for r in targets if notices_by_id.get(r["id"])
        }
        for fut in as_completed(futs):
            iid = futs[fut]
            result = fut.result()
            if isinstance(result, dict):
                enrichments[iid] = result
                verified = result.get("verified", "unknown")
                logger.info("[stage2.5] %s: verified=%s, ip_firm=%s, principals=%d",
                            iid, verified, result.get("ip_firm", "?"),
                            len(result.get("principals") or []))
    return enrichments


def stage3_writeups(top: list[dict], notices_by_id: dict, date: str, enrichments: dict | None = None) -> dict:
    if not top:
        return {}
    enrichments = enrichments or {}
    inputs = []
    for r in top:
        n = notices_by_id.get(r["id"])
        if not n:
            continue
        inputs.append({
            "id": r["id"],
            "compact": _compact(n),
            "tier": r["tier"],
            "category": r["category"],
            "final": r["final"],
            "enrichment": enrichments.get(r["id"]) or {},
        })
    user = (
        f"DATE (UTC): {date}\n\n"
        f"=== TOP {len(inputs)} NOTICES ===\n"
        f"{json.dumps(inputs, ensure_ascii=False, indent=2)}\n\n"
        "Output one writeup per input, JSON only."
    )
    client = _client()
    logger.info("[stage3] writing detail for %d items", len(inputs))
    resp = client.chat.completions.create(
        model=_model(),
        messages=[
            {"role": "system", "content": STAGE3_SYSTEM},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
        max_tokens=3000,
        extra_headers={
            "HTTP-Referer": "https://github.com/diktat1/gazette-insolvencies",
            "X-Title": "gazette-insolvencies (stage3)",
        },
        extra_body=_auto_router_extra_body(),
    )
    parsed = _parse_json(resp.choices[0].message.content or "")
    by_id: dict[str, dict] = {}
    for w in parsed.get("writeups", []):
        if isinstance(w, dict) and w.get("id"):
            by_id[w["id"]] = w
    return by_id


# ---------------------------------------------------------------------------
# Audit CSV
# ---------------------------------------------------------------------------

def write_audit_csv(out_path: Path, ranked: list[dict], notices_by_id: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "id", "tier", "category", "final", "tier_base", "heuristic_score",
            "adjustments", "phantom", "charges", "outstanding_charges",
            "website", "accounts_dormant", "co_ip_firm",
            "same_ip_firm_today", "sic_cluster_size",
            "company", "ch_status", "ch_accounts", "notice_type", "why",
        ])
        for r in ranked:
            n = notices_by_id.get(r["id"])
            comp = r.get("components", {})
            feat = r.get("features", {})
            w.writerow([
                r["id"], r["tier"], r["category"], r["final"], r["tier_base"],
                r["heuristic_score"], r["adjustments"],
                comp.get("phantom", 0), comp.get("charges", 0),
                comp.get("outstanding_charges", 0), comp.get("website", 0),
                comp.get("accounts_dormant", 0), comp.get("co_ip_firm", 0),
                feat.get("same_ip_firm_today", 0), feat.get("sic_cluster_size", 0),
                getattr(n, "company_name", "") or "",
                getattr(n, "ch_status", "") or "",
                getattr(n, "ch_accounts_type", "") or "",
                getattr(n, "notice_type", "") or "",
                (r.get("why") or "")[:200],
            ])
    logger.info("[audit] wrote %d decisions to %s", len(ranked), out_path)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply_llm_triage(notices: list, date: str | None = None) -> list:
    """Apply the LLM triage layer in-place on AnalysedNotice objects.

    Adds the following attributes to each notice:
        llm_tier               L1|L2|L3|watch|drop|unknown
        llm_category           pre_pack_buyer_search | creditor_bid_target | ...
        llm_why                one-sentence rationale
        llm_evidence           short quote from source
        llm_buyer_hypothesis   filled for top-N only, else ""
        llm_situation          filled for top-N only, else ""
        triage_final           blended numeric score (used to sort)

    Returns the notices sorted by triage_final descending. If
    OPENROUTER_API_KEY is not set, returns the input list unchanged.
    """
    if not notices:
        return notices
    if not os.environ.get("OPENROUTER_API_KEY"):
        logger.warning("[triage] OPENROUTER_API_KEY not set, skipping LLM triage")
        return notices

    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    notices_by_id = {n.notice_id: n for n in notices if n.notice_id}

    # Stage 1
    classifications = stage1_batched_judgement(notices, date)

    # Stage 1.5
    features = stage1_5_cooccurrence(classifications, notices_by_id)

    # Stage 2
    ranked = stage2_blend(classifications, notices_by_id, features)

    # Audit log
    audit_path = ROOT / "data" / f"triage-decisions-{date}.csv"
    try:
        write_audit_csv(audit_path, ranked, notices_by_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[audit] could not write CSV: %s", e)

    # Stage 3 — only the top N
    top_n = int(os.environ.get("TRIAGE_TOP_N", "10"))
    keep_threshold = os.environ.get("KEEP_THRESHOLD", "watch")
    keep_min = TIER_ORDER.get(keep_threshold, 1)
    eligible = [r for r in ranked if TIER_ORDER.get(r["tier"], 0) >= keep_min]
    top = eligible[:top_n]

    # Stage 2.5 — web-grounded enrichment on top items
    try:
        enrichments = stage2_5_enrich(top, notices_by_id, date)
    except Exception as e:  # noqa: BLE001
        logger.warning("[stage2.5] failed, continuing without enrichment: %s", e)
        enrichments = {}

    # Demote items whose enrichment reveals already_sold / stale
    for r in top:
        e = enrichments.get(r["id"]) or {}
        if e.get("verified") in ("already_sold", "stale"):
            logger.info("[stage2.5] demoting %s (verified=%s)", r["id"], e.get("verified"))
            r["tier"] = "L3"

    try:
        writeups = stage3_writeups(top, notices_by_id, date, enrichments=enrichments)
    except Exception as e:  # noqa: BLE001
        logger.warning("[stage3] failed, continuing without writeups: %s", e)
        writeups = {}

    # Attach to notices
    by_id_rank = {r["id"]: r for r in ranked}
    for n in notices:
        r = by_id_rank.get(n.notice_id)
        if not r:
            n.llm_tier = "unknown"
            n.llm_category = ""
            n.llm_why = ""
            n.llm_evidence = ""
            n.llm_situation = ""
            n.llm_buyer_hypothesis = ""
            n.triage_final = float(getattr(n, "opportunity_score", 0) or 0)
            continue
        n.llm_tier = r["tier"]
        n.llm_category = r["category"]
        n.llm_why = r["why"]
        n.llm_evidence = r["evidence"]
        n.triage_final = r["final"]
        w = writeups.get(n.notice_id) or {}
        n.llm_situation = w.get("situation", "")
        n.llm_buyer_hypothesis = w.get("buyer_hypothesis", "")

    notices.sort(key=lambda n: (-getattr(n, "triage_final", 0),
                                -TIER_ORDER.get(getattr(n, "llm_tier", "unknown"), 0)))

    n_l1 = sum(1 for n in notices if getattr(n, "llm_tier", "") == "L1")
    n_l2 = sum(1 for n in notices if getattr(n, "llm_tier", "") == "L2")
    n_drop = sum(1 for n in notices if getattr(n, "llm_tier", "") == "drop")
    logger.info("[triage] L1=%d L2=%d drop=%d total=%d", n_l1, n_l2, n_drop, len(notices))

    return notices
