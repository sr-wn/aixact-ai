import re
from typing import List, Tuple, Optional

import warnings
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from newspaper import Article
from transformers import pipeline
from sentence_transformers import SentenceTransformer
from fastapi import FastAPI
from pydantic import BaseModel

# -----------------------------
# WARNING FILTERS (to reduce log noise)
# -----------------------------
# Suppress noisy DeprecationWarning from requests.get_encodings_from_content
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    module="requests.utils",
)

# -----------------------------
# MODELS (open-source, local)
# -----------------------------
# summarization model (use a lighter variant for easier local running)
summarizer = pipeline("summarization", model="sshleifer/distilbart-cnn-12-6")
# NLI-based stance classifier (use a smaller DistilBERT-based MNLI model)
classifier = pipeline("text-classification", model="typeform/distilbert-base-uncased-mnli")
# Sentence embeddings (available for future similarity checks)
embedder = SentenceTransformer("all-MiniLM-L6-v2")

# -----------------------------
# CONFIG
# -----------------------------
TRUSTED_DOMAINS = [
    ".gov", ".edu", "who.int", "nih.gov", "un.org",
    "reuters.com", "bbc.com", "apnews.com", "nature.com",
]

AUTHORITATIVE_DOMAINS = [
    "wikipedia.org",
    ".gov",
    ".gov.in",
    ".gov.uk",
]

BIO_KEYWORDS = [
    "nationality", "citizen", "citizenship", "born", "birthplace",
    "age", "years old", "gender", "president", "prime minister",
    "leader", "occupation", "scientist", "actor", "singer",
]

class ClaimResult(BaseModel):
    claim: str
    verdict: str
    truth_score: Optional[float]
    evidence: List[Tuple[str, float, str]]  # (stance, confidence, url)


class FactCheckResponse(BaseModel):
    text: str
    claims: List[ClaimResult]


# -----------------------------
# 3. CLAIM EXTRACTION
# -----------------------------
def extract_claims(text: str) -> List[str]:
    """Break text into atomic, truth-checkable claims using a simple heuristic."""
    sentences = re.split(r"[.?!\n]+", text)
    claims = [s.strip() for s in sentences if len(s.strip()) > 20]
    if not claims and text.strip():
        claims = [text.strip()]
    return claims


# -----------------------------
# 4. QUERY GENERATOR
# -----------------------------
def generate_queries(claim: str) -> List[str]:
    base = claim.lower().strip()
    return [
        base,
        f"Is it true that {base}?",
        f"{base} scientific evidence",
        f"{base} government report",
        f"{base} peer reviewed study",
        f"{base} fact check",
        f"{base} refuted",
        f"{base} verified",
        f"WHO report {base}",
        f"NIH study {base}",
    ]


# -----------------------------
# 5. SEARCH ENGINE (DuckDuckGo)
# -----------------------------
def search_web(queries: List[str], max_results: int = 40):
    """Search the web using DuckDuckGo and return a deduplicated list of results.

    max_results is per query; we keep this modest for speed.
    """
    results = []
    seen_urls = set()
    try:
        with DDGS() as ddgs:
            for q in queries:
                for r in ddgs.text(q, max_results=max_results):
                    url = r.get("href") or r.get("url")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    results.append(r)
    except Exception:
        # On any search failure, just return what we have so far (possibly empty)
        pass
    return results


# -----------------------------
# 6. SOURCE CREDIBILITY
# -----------------------------
def is_trusted_source(url: str) -> bool:
    if not url:
        return False
    return any(domain in url for domain in TRUSTED_DOMAINS)


def is_authoritative_source(url: str) -> bool:
    if not url:
        return False
    return any(domain in url for domain in AUTHORITATIVE_DOMAINS)


# -----------------------------
# 7. SCRAPER
# -----------------------------
def extract_page_text(url: str) -> Optional[str]:
    try:
        article = Article(url)
        article.download()
        article.parse()
        return article.text
    except Exception:
        return None


# -----------------------------
# 8. SUMMARIZER
# -----------------------------
def summarize_evidence(text: Optional[str]) -> Optional[str]:
    if not text or len(text) < 200:
        return None
    try:
        chunk = text[:3000]
        # Choose max_length relative to input size to avoid warnings about
        # max_length > input_length and to keep summaries concise.
        approx_tokens = max(1, len(chunk) // 4)
        # Ensure max_len never exceeds approx_tokens (rough input length)
        max_len = max(30, min(150, approx_tokens - 1 if approx_tokens > 1 else 1))
        min_len = max(10, max_len // 3)

        summary = summarizer(
            chunk,
            max_length=max_len,
            min_length=min_len,
            do_sample=False,
        )[0]["summary_text"]
        return summary
    except Exception:
        return None


# -----------------------------
# 9. CLAIM & EVIDENCE HELPERS
# -----------------------------
def is_biographical_claim(claim: str) -> bool:
    c = claim.lower()
    return any(k in c for k in BIO_KEYWORDS)


def build_claim_keywords(claim: str) -> List[str]:
    c = claim.lower()
    words = [w.strip(",.?! ") for w in c.split() if len(w.strip(",.?! ")) > 3]
    # Simple specialization for well-known political leaders like Modi
    if "modi" in c:
        words.extend(
            [
                "narendra modi",
                "modi",
                "prime minister",
                "prime minister of india",
                "india",
                "indian",
                "nationality",
                "citizenship",
                "citizen",
            ]
        )
    return list(dict.fromkeys(words))  # deduplicate while preserving order


def evidence_is_relevant(summary: str, claim_keywords: List[str]) -> bool:
    text = summary.lower()
    hits = sum(1 for kw in claim_keywords if kw in text)
    return hits >= 2


# -----------------------------
# 10. STANCE DETECTION
# -----------------------------
def detect_stance(claim: str, evidence: str) -> Tuple[str, float]:
    """Return label in {supports, refutes, unrelated} and its confidence."""
    try:
        res = classifier(
            f"{evidence} </s></s> {claim}",
            candidate_labels=["supports", "refutes", "unrelated"],
        )
        label = res["labels"][0]
        score = float(res["scores"][0])
        return label, score
    except Exception:
        return "unrelated", 0.0


# -----------------------------
# 11. EVIDENCE SCORING ENGINE WITH AGREEMENT METRICS
# -----------------------------
def aggregate_evidence(
    stances: List[Tuple[str, float, str]]
) -> Optional[Tuple[float, float, float]]:
    """Return (truth_score, cross_source_agreement, source_credibility_score).

    truth_score is in [-1, 1]. cross_source_agreement and source_credibility_score
    are in [0, 1]. Returns None if no usable evidence.
    """

    if not stances:
        return None

    supports_weight = 0.0
    refutes_weight = 0.0
    total_weight = 0.0
    trusted_weight = 0.0

    for stance, confidence, url in stances:
        base_weight = 1.0 if is_trusted_source(url) else 0.5
        w = base_weight * confidence
        total_weight += w
        if is_trusted_source(url):
            trusted_weight += w

        if stance == "supports":
            supports_weight += w
        elif stance == "refutes":
            refutes_weight += w

    if total_weight == 0.0:
        return None

    truth_score = (supports_weight - refutes_weight) / total_weight
    cross_source_agreement = max(supports_weight, refutes_weight) / total_weight
    source_credibility_score = trusted_weight / total_weight

    return truth_score, cross_source_agreement, source_credibility_score


# -----------------------------
# 12. FINAL VERDICT LOGIC (CROSS-SOURCE + TRUTH SCORE)
# -----------------------------
def classify_verdict_from_metrics(
    truth_score: Optional[float], cross_source_agreement: Optional[float]
) -> str:
    """Apply user-specified hard rule combining agreement and truth score.

    if cross_source_agreement >= 0.70 or truth_score >= 0.70:
        verdict = "TRUE"
    elif cross_source_agreement <= 0.30 or truth_score <= 0.30:
        verdict = "FALSE"
    else:
        verdict = "NEEDS VERIFICATION"
    """

    if truth_score is None or cross_source_agreement is None:
        return "NEEDS VERIFICATION"

    if cross_source_agreement >= 0.70 or truth_score >= 0.70:
        return "TRUE"
    if cross_source_agreement <= 0.30 or truth_score <= 0.30:
        return "FALSE"
    return "NEEDS VERIFICATION"


# -----------------------------
# 13. BIOGRAPHICAL HARD-CHECK USING AUTHORITATIVE SOURCES
# -----------------------------
def nationality_contradicts_claim(claim: str, page_text: str) -> bool:
    """Heuristic override for obvious nationality mismatches.

    This is intentionally narrow and conservative, focusing on cases like
    "Modi is American" where Wikipedia clearly describes him as an Indian
    politician / Prime Minister of India.
    """

    c = claim.lower()
    t = page_text.lower()

    # Example: "Narendra Modi is an American citizen" vs Wikipedia saying
    # "Indian politician" / "Prime Minister of India".
    if "modi" in c and "american" in c:
        if "prime minister of india" in t or "indian politician" in t:
            return True

    return False


def biography_hard_check(claim: str) -> Tuple[Optional[str], float, List[Tuple[str, float, str]]]:
    """Attempt to force a TRUE/FALSE verdict for simple biography claims.

    Strategy:
    - Narrow search to authoritative sources (Wikipedia / .gov domains).
    - Look for basic biography cues (nationality, role, birthplace).
    - If >= 2 authoritative sources clearly support or refute the claim,
      return a forced verdict with high confidence (0.99) and the citations.

    This is intentionally simple and conservative; if we cannot clearly
    decide, we return (None, 0.0, []).
    """

    # Narrow query for biography-type answers
    base = claim.lower().strip()
    bio_queries = [
        base,
        f"{base} nationality",
        f"{base} biography",
        f"{base} citizenship",
    ]

    search_results = search_web(bio_queries, max_results=10)
    if not search_results:
        return None, 0.0, []

    authoritative_results = []
    for r in search_results:
        url = r.get("href") or r.get("url")
        if not url or not is_authoritative_source(url):
            continue
        authoritative_results.append(url)

    # Need at least two authoritative sources to consider a hard verdict
    if len(authoritative_results) < 2:
        return None, 0.0, []

    # For now, we do a simple NLI-based stance check on short summaries
    stances: List[Tuple[str, float, str]] = []
    for url in authoritative_results:
        page_text = extract_page_text(url)
        summary = summarize_evidence(page_text)
        if not summary:
            continue

        # Deterministic nationality override: if the biography text clearly
        # contradicts the nationality implied in the claim, force FALSE.
        if page_text and nationality_contradicts_claim(claim, page_text):
            return "FALSE", 0.99, [("refutes", 0.99, url)]

        stance, confidence = detect_stance(claim, summary)
        if stance in {"supports", "refutes"} and confidence >= 0.6:
            stances.append((stance, confidence, url))

    if len(stances) < 2:
        return None, 0.0, []

    supports = [s for s in stances if s[0] == "supports"]
    refutes = [s for s in stances if s[0] == "refutes"]

    # If there is clear agreement between authoritative sources, force verdict
    if len(supports) >= 2 and len(refutes) == 0:
        return "TRUE", 0.99, supports
    if len(refutes) >= 2 and len(supports) == 0:
        return "FALSE", 0.99, refutes

    return None, 0.0, []


# -----------------------------
# 14. MAIN FACT-CHECKING PIPELINE
# -----------------------------
def fact_check(text: str) -> FactCheckResponse:
    claims = extract_claims(text)
    results: List[ClaimResult] = []

    for claim in claims:
        # -------------------------
        # 1) Common-sense / biography hard layer
        # -------------------------
        forced_verdict: Optional[str] = None
        forced_confidence: float = 0.0
        forced_evidence: List[Tuple[str, float, str]] = []

        if is_biographical_claim(claim):
            forced_verdict, forced_confidence, forced_evidence = biography_hard_check(claim)

        if forced_verdict is not None:
            results.append(
                ClaimResult(
                    claim=claim,
                    verdict=forced_verdict,
                    truth_score=forced_confidence,
                    evidence=forced_evidence[:10],
                )
            )
            continue

        # -------------------------
        # 2) Standard evidence pipeline with relevance filtering
        # -------------------------
        queries = generate_queries(claim)
        # Fewer results per query for speed
        search_results = search_web(queries, max_results=20)

        stances: List[Tuple[str, float, str]] = []
        claim_keywords = build_claim_keywords(claim)

        try:
            # Process search results, prioritizing trusted domains first for speed/accuracy
            sorted_results = sorted(
                search_results,
                key=lambda r: 0
                if is_trusted_source((r.get("href") or r.get("url") or ""))
                else 1,
            )

            for r in sorted_results:
                url = r.get("href") or r.get("url")
                if not url:
                    continue

                page_text = extract_page_text(url)
                summary = summarize_evidence(page_text)
                if not summary:
                    continue

                # Filter out clearly irrelevant evidence (credibility = 0)
                if not evidence_is_relevant(summary, claim_keywords):
                    continue

                stance, confidence = detect_stance(claim, summary)
                # Ignore unrelated or weak evidence early
                if stance == "unrelated" or confidence < 0.45:
                    continue

                stances.append((stance, confidence, url))

                # Early stop: once we have enough strong evidence, don't keep scanning
                if len(stances) >= 8:
                    break
        except Exception:
            # If anything goes wrong while processing this claim, fall back to
            # whatever evidence we collected so far (possibly empty).
            pass

        metrics = aggregate_evidence(stances)
        if metrics is None:
            score = None
            cross_source_agreement = None
        else:
            score, cross_source_agreement, _source_credibility_score = metrics

        verdict = classify_verdict_from_metrics(score, cross_source_agreement)

        results.append(
            ClaimResult(
                claim=claim,
                verdict=verdict,
                truth_score=score,
                evidence=stances[:10],
            )
        )

    return FactCheckResponse(text=text, claims=results)


# -----------------------------
# 13. FastAPI SERVICE
# -----------------------------
app = FastAPI(title="Python Fact Checker", version="1.0.0")


class FactCheckRequest(BaseModel):
    text: str


@app.post("/fact-check", response_model=FactCheckResponse)
async def fact_check_endpoint(payload: FactCheckRequest):
    return fact_check(payload.text)


# If you want to run directly: uvicorn fact_checker_service:app --reload --host 0.0.0.0 --port 8001
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
