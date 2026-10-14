"""SEO utility helpers shared across routers.

This module is stdlib-only — no external dependencies.
"""


def is_thin_stub(person: dict, connections_count: int) -> bool:
    """Return True when a person page is a low-quality stub not worth indexing.

    # SEO thin-stub threshold: no photo, no IT/EN description, <2 connections.
    # Tune based on Search Console indexation data.

    A stub is defined as ALL of:
      - image_cached_path is None or falsy (no photo)
      - description_en is None or empty string (no English description)
      - description_it is None or empty string (no Italian description)
      - connections_count < 2

    When True, callers should emit <meta name="robots" content="noindex, follow">
    and exclude the person from sitemaps.  The page still renders normally —
    noindex signals Google not to index it while follow lets PageRank flow.
    """
    has_photo = bool(person.get("image_cached_path"))
    has_desc_en = bool(person.get("description_en") or "")
    has_desc_it = bool(person.get("description_it") or "")
    return (
        not has_photo
        and not has_desc_en
        and not has_desc_it
        and connections_count < 2
    )



def is_thin_organization(organization: dict, connections_count: int) -> bool:
    """Return True only for an organization with no usable search story.

    A small organization with one founder, director, owner, member, or
    structural link can still satisfy a specific investigative search. Unlike
    person pages, it does not need two connections or a photo to be indexable.
    """
    has_description = bool(organization.get("description_en") or organization.get("description_it"))
    return not has_description and connections_count == 0
