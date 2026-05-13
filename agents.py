import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import xml.etree.ElementTree as ET
import aiohttp
from urllib.parse import quote

logger = logging.getLogger(__name__)

class QueryPlannerAgent:
    """Plans PubMed queries based on user preferences"""
    
    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.QueryPlannerAgent")
    
    def plan_query(
        self,
        topics: List[str],
        custom_topics: List[str],
        journals: List[str],
        custom_journals: List[str]
    ) -> Dict[str, any]:
        """Plan a PubMed search query"""
        
        # Combine all topics
        all_topics = list(set(topics + custom_topics))
        
        # Combine all journals
        all_journals = list(set(journals + custom_journals))
        
        # Build query string - combine topics with OR
        if all_topics:
            # Escape quotes in topics
            escaped_topics = [topic.replace('"', '') for topic in all_topics]
            topic_query = ' OR '.join([f'"{topic}"[Title/Abstract]' for topic in escaped_topics[:15]])  # Limit to 15 topics
        else:
            topic_query = ""
        
        self.logger.info(f"Planned query for {len(all_topics)} topics and {len(all_journals)} journals")
        
        return {
            "query_string": topic_query,
            "journal_filter": all_journals,
            "topics_count": len(all_topics),
            "journals_count": len(all_journals)
        }

class PubMedSearchAgent:
    """Search PubMed and parse results"""
    
    ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    RATE_LIMIT_DELAY = 0.34  # ~3 requests per second
    
    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.PubMedSearchAgent")
    
    async def search(
        self,
        query: str,
        start_date: datetime,
        end_date: datetime,
        max_results: int = 20,
        journal_filter: Optional[List[str]] = None
    ) -> List[Dict]:
        """Search PubMed and return parsed articles"""
        
        if not query:
            self.logger.warning("Empty query provided")
            return []
        
        try:
            # Get PMIDs
            pmids = await self._esearch(
                query=query,
                start_date=start_date,
                end_date=end_date,
                max_results=max_results,
                journal_filter=journal_filter
            )
            
            if not pmids:
                self.logger.info("No PMIDs found for query")
                return []
            
            # Rate limiting
            await asyncio.sleep(self.RATE_LIMIT_DELAY)
            
            # Fetch article details
            articles = await self._efetch(pmids)
            
            self.logger.info(f"Retrieved {len(articles)} articles from PubMed")
            return articles
            
        except Exception as e:
            self.logger.error(f"PubMed search error: {str(e)}")
            return []
    
    async def fetch_by_pmids(self, pmids: List[str]) -> List[Dict]:
        """Public method to fetch articles by their PMIDs.
        
        Args:
            pmids: List of PubMed IDs to fetch
            
        Returns:
            List of article dictionaries with parsed data
        """
        if not pmids:
            return []
        
        try:
            articles = await self._efetch(pmids)
            self.logger.info(f"Fetched {len(articles)} articles by PMIDs")
            return articles
        except Exception as e:
            self.logger.error(f"Error fetching articles by PMIDs: {str(e)}")
            return []
    
    async def _esearch(
        self,
        query: str,
        start_date: datetime,
        end_date: datetime,
        max_results: int,
        journal_filter: Optional[List[str]] = None
    ) -> List[str]:
        """Execute ESearch to get PMIDs with retry logic for rate limits"""
        
        # Build date filter
        start_str = start_date.strftime("%Y/%m/%d")
        end_str = end_date.strftime("%Y/%m/%d")
        date_filter = f"AND {start_str}:{end_str}[Date - Publication]"
        
        # Build journal filter if provided
        journal_query = ""
        if journal_filter:
            # Take first 10 journals to keep query manageable
            journals_escaped = [j.replace('"', '') for j in journal_filter[:10]]
            journal_parts = [f'"{j}"[Journal]' for j in journals_escaped]
            journal_query = f" AND ({' OR '.join(journal_parts)})"
        
        # Combine query
        full_query = f"{query} {date_filter}{journal_query}"
        
        params = {
            "db": "pubmed",
            "term": full_query,
            "retmax": max_results,
            "retmode": "json",
            "sort": "pub_date"
        }
        
        self.logger.info(f"ESearch query: {full_query[:200]}...")
        
        max_retries = 3
        base_delay = 1.0
        
        for attempt in range(max_retries):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(self.ESEARCH_URL, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        pmids = data.get("esearchresult", {}).get("idlist", [])
                        self.logger.info(f"Found {len(pmids)} PMIDs")
                        return pmids
                    elif response.status == 429:
                        # Rate limited - wait and retry with exponential backoff
                        delay = base_delay * (2 ** attempt)
                        self.logger.warning(f"ESearch rate limited (429), retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(delay)
                    else:
                        self.logger.error(f"ESearch failed with status {response.status}")
                        return []
        
        self.logger.error(f"ESearch failed after {max_retries} retries due to rate limiting")
        return []
    
    async def _efetch(self, pmids: List[str]) -> List[Dict]:
        """Fetch article details using EFetch with retry logic for rate limits"""
        
        if not pmids:
            return []
        
        params = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml"
        }
        
        max_retries = 3
        base_delay = 1.0
        
        for attempt in range(max_retries):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(self.EFETCH_URL, params=params) as response:
                    if response.status == 200:
                        xml_data = await response.text()
                        return self._parse_xml(xml_data)
                    elif response.status == 429:
                        # Rate limited - wait and retry with exponential backoff
                        delay = base_delay * (2 ** attempt)
                        self.logger.warning(f"EFetch rate limited (429), retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(delay)
                    else:
                        self.logger.error(f"EFetch failed with status {response.status}")
                        return []
        
        self.logger.error(f"EFetch failed after {max_retries} retries due to rate limiting")
        return []
    
    def _parse_xml(self, xml_data: str) -> List[Dict]:
        """Parse PubMed XML into article dictionaries"""
        
        articles = []
        
        try:
            root = ET.fromstring(xml_data)
            
            for article_elem in root.findall(".//PubmedArticle"):
                try:
                    article = self._parse_article(article_elem)
                    if article:
                        articles.append(article)
                except Exception as e:
                    self.logger.warning(f"Failed to parse article: {str(e)}")
                    continue
            
        except Exception as e:
            self.logger.error(f"XML parsing error: {str(e)}")
        
        return articles
    
    def _parse_article(self, article_elem) -> Optional[Dict]:
        """Parse a single PubmedArticle element"""
        
        try:
            medline = article_elem.find(".//MedlineCitation")
            if medline is None:
                return None
            
            # PMID
            pmid_elem = medline.find(".//PMID")
            pmid = pmid_elem.text if pmid_elem is not None else None
            
            if not pmid:
                return None
            
            # Article details
            article_node = medline.find(".//Article")
            if article_node is None:
                return None
            
            # Title
            title_elem = article_node.find(".//ArticleTitle")
            title = title_elem.text if title_elem is not None else "No title"
            
            # Journal
            journal_elem = article_node.find(".//Journal/Title")
            journal = journal_elem.text if journal_elem is not None else "Unknown"
            
            # Publication date
            pub_date = self._extract_pub_date(article_node)
            
            # Authors
            authors = self._extract_authors(article_node)
            
            # Abstract
            abstract = self._extract_abstract(article_node)
            
            # DOI
            doi = self._extract_doi(article_elem)
            
            # MeSH terms
            mesh_terms = self._extract_mesh_terms(medline)
            
            # Publication types (design tags)
            design_tags = self._extract_publication_types(medline)
            
            # URL
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            
            return {
                "pmid": pmid,
                "doi": doi,
                "title": title,
                "journal": journal,
                "pub_date": pub_date,
                "authors": authors,
                "abstract": abstract,
                "mesh_terms": mesh_terms,
                "design_tags": design_tags,
                "url": url
            }
            
        except Exception as e:
            self.logger.warning(f"Error parsing article: {str(e)}")
            return None
    
    def _extract_pub_date(self, article_node) -> str:
        """Extract publication date"""
        try:
            pub_date = article_node.find(".//Journal/JournalIssue/PubDate")
            if pub_date is not None:
                year = pub_date.find("Year")
                month = pub_date.find("Month")
                day = pub_date.find("Day")
                
                if year is not None:
                    date_str = year.text
                    if month is not None:
                        date_str += f"-{month.text}"
                        if day is not None:
                            date_str += f"-{day.text}"
                    return date_str
        except:
            pass
        return "Unknown"
    
    def _extract_authors(self, article_node) -> str:
        """Extract authors list"""
        try:
            author_list = article_node.find(".//AuthorList")
            if author_list is not None:
                authors = []
                for author in author_list.findall("Author")[:5]:  # First 5 authors
                    lastname = author.find("LastName")
                    initials = author.find("Initials")
                    if lastname is not None:
                        name = lastname.text
                        if initials is not None:
                            name += f" {initials.text}"
                        authors.append(name)
                
                if authors:
                    result = ", ".join(authors)
                    if len(author_list.findall("Author")) > 5:
                        result += ", et al."
                    return result
        except:
            pass
        return "Unknown authors"
    
    def _extract_abstract(self, article_node) -> str:
        """Extract abstract text, preserving structured section labels and inline markup.
        
        Checks multiple locations in the PubMed XML where abstracts can appear:
        1. Standard Abstract element
        2. OtherAbstract elements (translations or alternative formats)
        3. Directly as AbstractText without wrapper
        """
        try:
            abstract_texts = []
            
            # Try standard Abstract element first
            abstract_elem = article_node.find(".//Abstract")
            if abstract_elem is not None:
                for text_elem in abstract_elem.findall(".//AbstractText"):
                    # Use itertext() to capture ALL text including inline tags (<i>, <b>, <sup>, etc.)
                    full_text = "".join(text_elem.itertext()).strip()
                    if not full_text:
                        continue
                    # Preserve section labels for structured abstracts (BACKGROUND, METHODS, etc.)
                    label = text_elem.get("Label", "").strip()
                    nlm_category = text_elem.get("NlmCategory", "").strip()
                    # Use Label if available, otherwise use NlmCategory
                    section_label = label or nlm_category
                    if section_label:
                        abstract_texts.append(f"{section_label}: {full_text}")
                    else:
                        abstract_texts.append(full_text)
            
            # If no abstract found, try OtherAbstract (may contain translations)
            if not abstract_texts:
                for other_abstract in article_node.findall(".//OtherAbstract"):
                    # Prefer English abstracts
                    lang = other_abstract.get("Language", "")
                    if lang.lower() == "eng" or not abstract_texts:
                        for text_elem in other_abstract.findall(".//AbstractText"):
                            full_text = "".join(text_elem.itertext()).strip()
                            if full_text:
                                label = text_elem.get("Label", "").strip()
                                if label:
                                    abstract_texts.append(f"{label}: {full_text}")
                                else:
                                    abstract_texts.append(full_text)
            
            # Return combined abstract or default message
            if abstract_texts:
                return "\n\n".join(abstract_texts)
            
            return "No abstract available"
            
        except Exception as e:
            self.logger.warning(f"Error extracting abstract: {str(e)}")
            return "No abstract available"
    
    def _extract_doi(self, article_elem) -> Optional[str]:
        """Extract DOI"""
        try:
            article_ids = article_elem.findall(".//ArticleId")
            for aid in article_ids:
                if aid.get("IdType") == "doi":
                    return aid.text
        except:
            pass
        return None
    
    def _extract_mesh_terms(self, medline) -> List[str]:
        """Extract MeSH terms"""
        try:
            mesh_list = medline.find(".//MeshHeadingList")
            if mesh_list is not None:
                terms = []
                for heading in mesh_list.findall("MeshHeading"):
                    descriptor = heading.find("DescriptorName")
                    if descriptor is not None and descriptor.text:
                        terms.append(descriptor.text)
                return terms[:10]  # First 10 MeSH terms
        except:
            pass
        return []
    
    def _extract_publication_types(self, medline) -> List[str]:
        """Extract publication types (design tags)"""
        try:
            pub_type_list = medline.find(".//PublicationTypeList")
            if pub_type_list is not None:
                types = []
                for pt in pub_type_list.findall("PublicationType"):
                    if pt.text:
                        types.append(pt.text)
                return types[:5]  # First 5 types
        except:
            pass
        return []
