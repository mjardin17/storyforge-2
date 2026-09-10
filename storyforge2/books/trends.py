"""
storyforge2/books/trends.py — trend discovery and opportunity generation.

Scans for book market opportunities. MVP uses a curated list of evergreen niches;
production can integrate Google Trends, Amazon bestseller lists, Reddit/Twitter
social signals, etc.

A TrendOpportunity is a potential book idea with:
- niche: e.g. "personal-finance" or "ai-productivity"
- keywords: 3-5 search terms for the book
- target_audience: e.g. "busy professionals" or "indie hackers"
- pitch: one-sentence premise
- estimated_audience_size: rough market size (small/medium/large)

Rules:
- Pick ONE niche per scan (24-hour rotation across niches)
- Evergreen niches only (no trend-of-the-week, no time-bound topics)
- Each scan produces AT MOST one opportunity (avoid spam)
- Dry-run mode returns a TrendOpportunity without any state changes
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional, Any
import json
from pathlib import Path
import hashlib

__all__ = ["TrendOpportunity", "TrendScanner"]

# Evergreen niches with consistent demand, repeated yearly.
# Every spec carries "genre" — this is what actually decides which formula
# engine (patterson_formula.py's PattersonFormula vs NonFictionFormula) writes
# the book. It must contain a substring formula_for_genre() recognizes:
# nonfiction markers ("non-fiction", "nonfiction", "self-help", "guide",
# "how-to") or fiction markers ("fiction", "thriller", "mystery", "romance",
# "fantasy", "sci-fi", "horror", "adventure", "drama", "ya").
EVERGREEN_NICHES = {
    "personal-finance": {
        "name": "Personal Finance",
        "genre": "non-fiction",
        "keywords": ["financial independence", "budgeting", "investing basics"],
        "audience": "working professionals aged 25-45",
        "pitch_template": "A practical guide to building wealth on a {profession} salary",
        "avg_audience_size": "large",
    },
    "productivity-systems": {
        "name": "Productivity Systems",
        "genre": "non-fiction",
        "keywords": ["time management", "focus", "deep work"],
        "audience": "knowledge workers, creators, entrepreneurs",
        "pitch_template": "How to implement {system} for {context}",
        "avg_audience_size": "large",
    },
    "ai-for-business": {
        "name": "AI for Business",
        "genre": "non-fiction",
        "keywords": ["ChatGPT", "automation", "AI workflows"],
        "audience": "small business owners, freelancers, SMBs",
        "pitch_template": "{AI tool} for {business_type}: a practical handbook",
        "avg_audience_size": "medium",
    },
    "health-wellness": {
        "name": "Health & Wellness",
        "genre": "non-fiction",
        "keywords": ["sleep optimization", "nutrition", "fitness"],
        "audience": "health-conscious adults",
        "pitch_template": "The science-backed guide to {health_topic}",
        "avg_audience_size": "large",
    },
    "remote-work": {
        "name": "Remote Work",
        "genre": "non-fiction",
        "keywords": ["work from home", "async teams", "digital nomad"],
        "audience": "remote workers and distributed teams",
        "pitch_template": "Building a {work_aspect} strategy for remote teams",
        "avg_audience_size": "medium",
    },
    "side-hustle": {
        "name": "Side Hustles",
        "genre": "non-fiction",
        "keywords": ["passive income", "freelancing", "micro-businesses"],
        "audience": "part-time entrepreneurs",
        "pitch_template": "Starting your {business_type} side hustle: a 90-day plan",
        "avg_audience_size": "large",
    },
    "machine-learning": {
        "name": "Machine Learning Basics",
        "genre": "non-fiction",
        "keywords": ["neural networks", "ML workflow", "model training"],
        "audience": "aspiring ML engineers, data scientists",
        "pitch_template": "From zero to ML: a practical introduction for {background}",
        "avg_audience_size": "medium",
    },
    "copywriting-seo": {
        "name": "Copywriting & SEO",
        "genre": "non-fiction",
        "keywords": ["persuasive writing", "SEO optimization", "conversion copywriting"],
        "audience": "content marketers, freelance writers, entrepreneurs",
        "pitch_template": "The {channel} copywriting playbook: how to write content that converts",
        "avg_audience_size": "large",
    },
    "real-estate-investing": {
        "name": "Real Estate Investing",
        "genre": "non-fiction",
        "keywords": ["rental properties", "house flipping", "commercial real estate"],
        "audience": "aspiring property investors",
        "pitch_template": "Starting in real estate: {strategy} for building wealth with property",
        "avg_audience_size": "large",
    },
    "cryptocurrency-blockchain": {
        "name": "Cryptocurrency & Blockchain",
        "genre": "non-fiction",
        "keywords": ["Bitcoin", "blockchain technology", "DeFi investing"],
        "audience": "crypto investors, tech enthusiasts",
        "pitch_template": "Understanding {crypto_topic}: a practical guide for {investor_type}",
        "avg_audience_size": "medium",
    },
    "personal-branding": {
        "name": "Personal Branding",
        "genre": "non-fiction",
        "keywords": ["thought leadership", "personal brand", "online presence"],
        "audience": "entrepreneurs, professionals, content creators",
        "pitch_template": "Building your personal brand: stand out and monetize your expertise",
        "avg_audience_size": "large",
    },
    "e-commerce-amazon": {
        "name": "E-Commerce & Amazon",
        "genre": "non-fiction",
        "keywords": ["Amazon FBA", "dropshipping", "e-commerce automation"],
        "audience": "online sellers, entrepreneurs",
        "pitch_template": "The {platform} selling playbook: build a 6-figure {business_type}",
        "avg_audience_size": "large",
    },
    # ── Fiction niches — routed to PattersonFormula (YA-thriller pacing
    # engine: dialogue ratio, cliffhang endings, sentence-length targets).
    # MVP reuses one formula across all fiction subgenres below; if a
    # specific subgenre's output reads wrong (e.g. cozy mystery shouldn't
    # actually cliffhang every chapter), that's a formula-tuning follow-up,
    # not a routing bug.
    # Note: Romance is massive ($1.44B market, 40% of self-pub titles) so we
    # include both contemporary and paranormal variants.
    "contemporary-romance": {
        "name": "Contemporary Romance",
        "genre": "contemporary romance",
        "keywords": ["enemies to lovers", "second chance romance", "small town romance"],
        "audience": "adult romance readers (80% female)",
        "pitch_template": "A contemporary romance between {character_type} who must overcome {obstacle} to find love",
        "avg_audience_size": "large",
    },
    "paranormal-romance": {
        "name": "Paranormal Romance",
        "genre": "paranormal romance",
        "keywords": ["vampire romance", "supernatural romance", "paranormal love story"],
        "audience": "paranormal romance fans",
        "pitch_template": "A paranormal romance where a {supernatural_being} falls for a {human_type} despite the danger",
        "avg_audience_size": "large",
    },
    "ya-thriller": {
        "name": "YA Thriller",
        "genre": "YA thriller",
        "keywords": ["young adult suspense", "page-turner", "twist ending"],
        "audience": "YA readers aged 13-18",
        "pitch_template": "A YA thriller about a teen who uncovers a secret that could destroy everything",
        "avg_audience_size": "large",
    },
    "cozy-mystery": {
        "name": "Cozy Mystery",
        "genre": "cozy mystery",
        "keywords": ["amateur sleuth", "small town", "whodunit"],
        "audience": "adult cozy mystery readers",
        "pitch_template": "A cozy mystery where an amateur sleuth in a small town solves a murder no one else can crack",
        "avg_audience_size": "large",
    },
    "psychological-thriller": {
        "name": "Psychological Thriller",
        "genre": "psychological thriller",
        "keywords": ["unreliable narrator", "domestic suspense", "slow-burn twist"],
        "audience": "adult thriller readers",
        "pitch_template": "A psychological thriller about a narrator whose grip on the truth is slipping",
        "avg_audience_size": "large",
    },
    "epic-fantasy": {
        "name": "Epic Fantasy",
        "genre": "epic fantasy",
        "keywords": ["magic system", "chosen one", "world-building"],
        "audience": "fantasy readers aged 16+",
        "pitch_template": "An epic fantasy where an unlikely hero must master a forbidden power to save their world",
        "avg_audience_size": "medium",
    },
    "fantasy-romance": {
        "name": "Fantasy Romance",
        "genre": "fantasy romance",
        "keywords": ["romantasy", "enemies to lovers", "magic academy"],
        "audience": "adult fantasy romance readers",
        "pitch_template": "A fantasy romance where rival magic-wielders are forced into an alliance neither of them wants",
        "avg_audience_size": "large",
    },
    "scifi-adventure": {
        "name": "Sci-Fi Adventure",
        "genre": "sci-fi adventure",
        "keywords": ["space opera", "survival", "first contact"],
        "audience": "sci-fi readers aged 14+",
        "pitch_template": "A sci-fi adventure where a stranded crew must survive first contact with something that shouldn't exist",
        "avg_audience_size": "medium",
    },
}


@dataclass
class TrendOpportunity:
    """A potential book market opportunity."""

    niche: str  # key from EVERGREEN_NICHES
    title: str  # generated title
    premise: str  # one-sentence pitch
    keywords: list[str]  # 3-5 search terms
    target_audience: str  # description of ideal reader
    estimated_audience_size: str  # small/medium/large

    # Metadata
    # Default "non-fiction" so from_dict() can still reconstruct scan_history
    # entries saved before fiction niches existed (they never had a "genre").
    genre: str = "non-fiction"
    generated_at: datetime = field(default_factory=datetime.utcnow)
    scan_id: str = ""  # unique ID for this scan session

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["generated_at"] = self.generated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrendOpportunity:
        """Reconstruct from JSON."""
        if isinstance(data.get("generated_at"), str):
            data["generated_at"] = datetime.fromisoformat(data["generated_at"])
        return cls(**data)


class TrendScanner:
    """Discovers book market opportunities.

    MVP uses a round-robin schedule across evergreen niches. Each niche is
    visited every N days (where N = number of niches). This ensures stable
    content generation without redundant topics.

    Future: integrate Google Trends, Amazon bestseller lists, Reddit/Twitter
    social signals, etc.
    """

    def __init__(self, state_db: Path | str = "books/trends_state.json"):
        self.state_db = Path(state_db)
        self.state_db.parent.mkdir(parents=True, exist_ok=True)
        self._load_state()

    def _load_state(self):
        """Load scan history and last-scanned niche."""
        if self.state_db.exists():
            with open(self.state_db) as f:
                state = json.load(f)
                self.last_scan: Optional[datetime] = (
                    datetime.fromisoformat(state.get("last_scan"))
                    if state.get("last_scan") else None
                )
                self.last_niche_index: int = state.get("last_niche_index", -1)
                self.scan_history: list[str] = state.get("scan_history", [])
        else:
            self.last_scan = None
            self.last_niche_index = -1
            self.scan_history = []

    def _save_state(self):
        """Persist scan history to disk."""
        state = {
            "last_scan": self.last_scan.isoformat() if self.last_scan else None,
            "last_niche_index": self.last_niche_index,
            "scan_history": self.scan_history[-100:],  # keep last 100 scans
        }
        with open(self.state_db, "w") as f:
            json.dump(state, f, indent=2)

    def scan(self, dry_run: bool = True) -> Optional[TrendOpportunity]:
        """Scan for the next niche opportunity.

        Returns a TrendOpportunity if a new niche is due for scanning, else None.
        Rounds through EVERGREEN_NICHES in order.

        Args:
            dry_run: If True, don't persist state changes.

        Returns:
            TrendOpportunity if a niche is due, else None.
        """
        niche_keys = list(EVERGREEN_NICHES.keys())
        next_index = (self.last_niche_index + 1) % len(niche_keys)
        niche_key = niche_keys[next_index]
        niche_spec = EVERGREEN_NICHES[niche_key]

        # Generate a TrendOpportunity
        scan_id = hashlib.md5(
            f"{niche_key}:{datetime.utcnow().isoformat()}".encode()
        ).hexdigest()[:8]

        opportunity = TrendOpportunity(
            niche=niche_key,
            title=f"[Opportunity] {niche_spec['name']} Book",
            premise=niche_spec["pitch_template"].replace("{profession}", "tech").replace(
                "{system}", "Getting Things Done"
            ).replace(
                "{context}", "creative work"
            ).replace(
                "{AI tool}", "ChatGPT"
            ).replace(
                "{business_type}", "freelance"
            ).replace(
                "{work_aspect}", "hiring"
            ).replace(
                "{health_topic}", "sleep"
            ).replace(
                "{background}", "software engineers"
            ),
            keywords=niche_spec["keywords"],
            target_audience=niche_spec["audience"],
            estimated_audience_size=niche_spec["avg_audience_size"],
            genre=niche_spec.get("genre", "non-fiction"),
            scan_id=scan_id,
        )

        if not dry_run:
            self.last_niche_index = next_index
            self.last_scan = datetime.utcnow()
            self.scan_history.append(f"{niche_key}:{scan_id}")
            self._save_state()

        return opportunity

    def get_niche_for_date(self, date: datetime) -> str:
        """Deterministic niche selection by date.

        Given a date, return which niche should be scanned that day (for
        scheduled tasks that need reproducibility).
        """
        niche_keys = list(EVERGREEN_NICHES.keys())
        day_of_year = date.timetuple().tm_yday
        niche_index = day_of_year % len(niche_keys)
        return niche_keys[niche_index]
