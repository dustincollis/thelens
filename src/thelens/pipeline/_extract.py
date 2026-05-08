"""Internal helpers for pulling visible text and title out of HTML.

Used by `audit.py` (for char counts) and by AI pipeline steps that need
page content as a string.
"""

from __future__ import annotations

from bs4 import BeautifulSoup


_DROP_TAGS = ["script", "style", "noscript", "nav", "footer"]


def extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


def extract_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    title = soup.find("title")
    if not title:
        return ""
    return (title.get_text() or "").strip()
