"""Offline tests for the parts of Link Séance that keep the medium honest.

    python -m pytest tests/     (or just: python tests/test_guardrail.py)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import link_seance as L  # noqa: E402

READER = ("Google Reader – Google Skip to content Thank you for stopping by. Google Reader has "
          "been discontinued . We want to thank all our loyal fans. All Google Reader "
          "subscription data will be systematically deleted from Google servers.")


def test_extracts_urls_from_markdown_and_skips_archive_links():
    text = "- [a](http://posterous.com/) and https://web.archive.org/web/1/x <http://geocities.com/x>."
    assert L.extract_urls(text) == ["http://posterous.com/", "http://geocities.com/x"]


def test_grounded_epitaph_is_accepted():
    ep = "Here lies Google Reader, discontinued after its loyal fans said goodbye. Rest now, gentle river of news."
    assert L.verify(ep, READER, "Google Reader – Google") is None


def test_true_but_unsourced_claim_is_rejected():
    # Reader really did manage subscriptions, but this snapshot never says so.
    ep = "Here lies Google Reader, the page for managing feeds. It is now silent."
    assert "never said" in L.verify(ep, READER, "Google Reader – Google")


def test_invented_names_and_numbers_are_rejected():
    ep = "Here lies Google Reader, loved by Larry Page since 2005. Farewell."
    assert "invented" in L.verify(ep, READER, "Google Reader – Google")


def test_first_person_is_rejected():
    ep = "Here lies Google Reader. We will miss it."
    assert "first person" in L.verify(ep, READER, "Google Reader – Google")


def test_tombstones_are_not_mistaken_for_the_living():
    pulse = {"final_title": "Yahoo Search - Web Search"}
    assert not L.looks_alive({"title": "Yahoo Search - Web Search", "description": "",
                              "text": "The search engine that helps you find exactly what you're looking for."}, pulse)
    assert not L.looks_alive({"title": "404 Not Found", "description": "", "text": "Not Found"}, {})
    assert L.looks_alive({"title": "Homepage | Yahoo Answers", "description": "",
                          "text": "Yahoo Answers is a great knowledge-sharing platform."}, pulse)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
