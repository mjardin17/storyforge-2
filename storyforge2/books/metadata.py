"""
storyforge2/books/metadata.py — book metadata generation and validation.

Handles ISBN generation (placeholder only — real ISBNs come from vendors),
metadata inference from manuscript content, and platform-specific formatting
(Amazon categories, Draft2Digital keywords, Gumroad tags, etc.).

BookMetadata is the canonical data structure for book information:
- ISBN (placeholder: 978-1-xxx-xxxxx-x format, never real)
- title, subtitle, author, description
- keywords (search terms)
- categories (Amazon BISAC, etc.)
- age_range (for kids/YA)
- language
- pricing_strategy (platform-specific overrides)
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional, Any
from datetime import datetime
import json
import hashlib

__all__ = ["BookMetadata", "ISBNGenerator", "MetadataBuilder"]


@dataclass
class BookMetadata:
    """Complete metadata for a published book."""

    isbn: str  # placeholder format: 978-1-{hash}-x
    title: str
    subtitle: str = ""
    author: str = "Empire OS Publishing"
    description: str = ""  # 500+ char book description
    keywords: list[str] = field(default_factory=list)  # 5-10 keywords
    categories: list[str] = field(default_factory=list)  # BISAC or platform-specific
    age_range: str = ""  # e.g. "8-12" or "18+" or ""
    language: str = "en"

    # Platform-specific pricing
    base_price: float = 9.99
    amazon_price: Optional[float] = None  # override if needed
    draft2digital_price: Optional[float] = None
    gumroad_price: Optional[float] = None

    # Source
    niche: str = ""  # which trend niche
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["generated_at"] = self.generated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BookMetadata:
        """Reconstruct from JSON."""
        if isinstance(data.get("generated_at"), str):
            data["generated_at"] = datetime.fromisoformat(data["generated_at"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @classmethod
    def from_json(cls, json_str: str) -> BookMetadata:
        return cls.from_dict(json.loads(json_str))


class ISBNGenerator:
    """Generate placeholder ISBNs for books.

    Real ISBNs come from vendors (Amazon, Draft2Digital, etc.). This generates
    a deterministic placeholder in ISBN-13 format (978-1-...) based on book
    title and niche for reproducibility.

    Format: 978-1-{8-digit-hash}-{check-digit}

    Rules:
    - Always starts with 978-1 (US publisher prefix)
    - Middle 8 digits are a hash of title + niche
    - Last digit is Luhn check digit
    - Never submitted to a real ISBN authority
    """

    @staticmethod
    def generate(title: str, niche: str) -> str:
        """Generate a placeholder ISBN-13."""
        seed = f"{title}:{niche}".encode()
        # Use hash but convert to decimal digits only (not hex)
        hash_int = int(hashlib.sha256(seed).hexdigest(), 16)
        middle_digits = str(hash_int % 100000000).zfill(8)  # 8 decimal digits
        base = f"978-1-{middle_digits}"

        # Calculate Luhn check digit
        digits = base.replace("-", "")
        total = sum(
            int(d) * (3 if i % 2 == 1 else 1)
            for i, d in enumerate(reversed(digits))
        )
        check_digit = (10 - (total % 10)) % 10

        return f"{base}-{check_digit}"

    @staticmethod
    def validate_placeholder(isbn: str) -> bool:
        """Verify that an ISBN is a valid placeholder."""
        if not isbn.startswith("978-1-"):
            return False
        if isbn.count("-") != 3:
            return False
        parts = isbn.split("-")
        if len(parts[2]) != 8 or not parts[2].isalnum():
            return False
        if not parts[3].isdigit() or len(parts[3]) != 1:
            return False
        return True


class MetadataBuilder:
    """Builds complete BookMetadata from a TrendOpportunity and manuscript.

    Takes the output of the manuscript generation stage and enriches it with
    metadata: keywords extracted from content, category suggestions, pricing
    strategy based on niche, etc.

    Future: use NLP to extract keywords automatically; suggest Amazon BISAC
    categories based on content analysis.
    """

    # BISAC categories (US trade book classification)
    # Subset for common niches
    BISAC_CATEGORIES = {
        "personal-finance": [
            "BUSINESS & ECONOMICS / Finance",
            "BUSINESS & ECONOMICS / Personal Finance",
        ],
        "productivity-systems": [
            "SELF-HELP / Time Management",
            "SELF-HELP / Personal Improvement and Analysis",
        ],
        "ai-for-business": [
            "BUSINESS & ECONOMICS / Artificial Intelligence",
            "COMPUTERS / Artificial Intelligence",
        ],
        "health-wellness": [
            "HEALTH & FITNESS",
            "MEDICAL / General",
        ],
        "remote-work": [
            "BUSINESS & ECONOMICS / Entrepreneurship",
            "BUSINESS & ECONOMICS / Management",
        ],
        "side-hustle": [
            "BUSINESS & ECONOMICS / Entrepreneurship",
            "SELF-HELP / Personal Improvement and Analysis",
        ],
        "technical-writing": [
            "REFERENCE / Writing Skills",
            "COMPUTERS / General",
        ],
        "machine-learning": [
            "COMPUTERS / Artificial Intelligence",
            "MATHEMATICS / Probability & Statistics",
        ],
        "copywriting-seo": [
            "BUSINESS & ECONOMICS / Marketing",
            "REFERENCE / Writing Skills",
        ],
        "real-estate-investing": [
            "BUSINESS & ECONOMICS / Real Estate",
            "BUSINESS & ECONOMICS / Investments",
        ],
        "cryptocurrency-blockchain": [
            "BUSINESS & ECONOMICS / Investments",
            "COMPUTERS / Networking & Internet",
        ],
        "personal-branding": [
            "BUSINESS & ECONOMICS / Entrepreneurship",
            "SELF-HELP / Personal Improvement and Analysis",
        ],
        "e-commerce-amazon": [
            "BUSINESS & ECONOMICS / E-Commerce",
            "BUSINESS & ECONOMICS / Entrepreneurship",
        ],
        "voice-podcast-strategy": [
            "BUSINESS & ECONOMICS / Marketing",
            "PERFORMING ARTS / Audio & Sound",
        ],
    }

    @staticmethod
    def build(
        title: str,
        niche: str,
        keywords: list[str],
        base_description: str = "",
        author: str = "Empire OS Publishing",
    ) -> BookMetadata:
        """Build complete metadata from basic info.

        Args:
            title: Book title
            niche: Which evergreen niche (personal-finance, etc.)
            keywords: 3-5 search terms
            base_description: Manuscript abstract or description
            author: Author name

        Returns:
            BookMetadata with all fields populated
        """
        isbn = ISBNGenerator.generate(title, niche)

        categories = MetadataBuilder.BISAC_CATEGORIES.get(niche, [])

        metadata = BookMetadata(
            isbn=isbn,
            title=title,
            author=author,
            description=base_description or f"A guide to {keywords[0]}.",
            keywords=keywords[:10],  # cap at 10
            categories=categories,
            niche=niche,
            base_price=9.99,  # default; override per niche if needed
        )

        # Apply niche-specific pricing rules
        # Premium niches: high market demand + business value
        if niche in ["machine-learning", "real-estate-investing", "e-commerce-amazon", "cryptocurrency-blockchain"]:
            metadata.base_price = 19.99  # Business/investing readers pay premium
        elif niche in ["technical-writing", "copywriting-seo", "personal-branding"]:
            metadata.base_price = 14.99  # Professional development/skills
        elif niche in ["voice-podcast-strategy", "ai-for-business"]:
            metadata.base_price = 12.99  # Medium demand + actionable value
        # Others stay at 9.99 for accessible pricing

        return metadata

    @staticmethod
    def extract_keywords_from_content(text: str, max_keywords: int = 10) -> list[str]:
        """Extract keywords from manuscript content (MVP: return empty).

        Future: use NLP (spacy, textblob, or Claude API) to extract keywords
        automatically from manuscript chapters.
        """
        # TODO: implement NLP-based keyword extraction
        return []
