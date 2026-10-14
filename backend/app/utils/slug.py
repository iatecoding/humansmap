"""Slug helper — converts a person's display name to a URL-safe ASCII slug.

Contract:
  - Lowercase
  - Accent-fold via NFKD normalisation + strip combining marks
  - Replace any run of non-[a-z0-9] chars with a single hyphen
  - Trim leading/trailing hyphens
  - Cap at 60 characters (trim at last hyphen boundary if possible)
  - Empty / all-non-ASCII input returns ""

Examples:
  slugify("Cañón")          → "canon"
  slugify("J.S. Bach")      → "j-s-bach"
  slugify("hello 😀 world") → "hello-world"
  slugify("")               → ""
  slugify("日本語")          → ""
"""

import re
import unicodedata


def slugify(name: str) -> str:
    """Return a URL-safe ASCII slug for *name*.

    >>> slugify("Cañón")
    'canon'
    >>> slugify("J.S. Bach")
    'j-s-bach'
    >>> slugify("hello 😀 world")
    'hello-world'
    >>> slugify("")
    ''
    >>> slugify("日本語")
    ''
    """
    if not name:
        return ""

    # 1. NFKD normalisation then drop combining (accent) marks
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")

    # 2. Lowercase
    lower = ascii_only.lower()

    # 3. Replace runs of non-alphanumeric chars with a single hyphen
    slugged = re.sub(r"[^a-z0-9]+", "-", lower)

    # 4. Trim leading/trailing hyphens
    slugged = slugged.strip("-")

    # 5. Cap at 60 chars; trim at last hyphen boundary when possible
    if len(slugged) > 60:
        truncated = slugged[:60]
        last_hyphen = truncated.rfind("-")
        if last_hyphen > 0:
            truncated = truncated[:last_hyphen]
        slugged = truncated.strip("-")

    return slugged
