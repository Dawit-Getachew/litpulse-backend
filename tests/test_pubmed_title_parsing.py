"""
Regression tests for the "Untitled paper" library bug.

Root cause: PubMed <ArticleTitle> elements frequently contain inline markup
(<i> for gene/species names, <sub>/<sup> for formulae). ElementTree's .text
only returns the characters BEFORE the first child element, so the old parser
(`title = title_elem.text`) returned None or a truncated fragment for such
titles — the paper was then stored and displayed as "Untitled".

The fix uses "".join(title_elem.itertext()) to capture the full title text.
These tests build minimal PubmedArticle XML and assert the title is recovered
in full. They run offline (no network).
"""
import sys
import types
import xml.etree.ElementTree as ET

import pytest

# agents.py imports aiohttp at module top; stub it if not installed so the
# parser (pure stdlib xml) can be imported and tested offline.
if "aiohttp" not in sys.modules:
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        sys.modules["aiohttp"] = types.ModuleType("aiohttp")

from agents import PubMedSearchAgent


def _pubmed_article(title_inner_xml: str, pmid: str = "12345678") -> ET.Element:
    """Wrap an ArticleTitle inner XML into a minimal PubmedArticle element."""
    xml = f"""
    <PubmedArticle>
      <MedlineCitation>
        <PMID>{pmid}</PMID>
        <Article>
          <ArticleTitle>{title_inner_xml}</ArticleTitle>
          <Journal><Title>Test Journal</Title></Journal>
        </Article>
      </MedlineCitation>
    </PubmedArticle>
    """
    return ET.fromstring(xml)


@pytest.fixture(scope="module")
def agent():
    return PubMedSearchAgent()


def test_title_with_leading_markup_is_not_lost(agent):
    """A title that STARTS with markup previously returned None -> 'Untitled'."""
    el = _pubmed_article("<i>BRCA1</i> mutations in breast cancer.")
    parsed = agent._parse_article(el)
    assert parsed is not None
    assert parsed["title"] == "BRCA1 mutations in breast cancer."


def test_title_with_midstring_markup_is_complete(agent):
    """Mid-string markup previously truncated the title to the leading text."""
    el = _pubmed_article("Effect of <i>Helicobacter pylori</i> on gastric mucosa.")
    parsed = agent._parse_article(el)
    assert parsed is not None
    assert parsed["title"] == "Effect of Helicobacter pylori on gastric mucosa."


def test_title_with_sub_sup_markup(agent):
    el = _pubmed_article("CO<sub>2</sub> levels and the H<sub>2</sub>O cycle.")
    parsed = agent._parse_article(el)
    assert parsed is not None
    assert parsed["title"] == "CO2 levels and the H2O cycle."


def test_plain_title_unaffected(agent):
    el = _pubmed_article("A randomized controlled trial of aspirin.")
    parsed = agent._parse_article(el)
    assert parsed is not None
    assert parsed["title"] == "A randomized controlled trial of aspirin."


def test_empty_title_falls_back_to_no_title(agent):
    el = _pubmed_article("")
    parsed = agent._parse_article(el)
    assert parsed is not None
    assert parsed["title"] == "No title"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
