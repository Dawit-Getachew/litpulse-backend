from typing import List, Dict, Optional
import logging
import uuid
from datetime import datetime, timezone
from emergentintegrations.llm.chat import LlmChat, UserMessage
import os
import asyncio

logger = logging.getLogger(__name__)

class DeduplicationRankingAgent:
    """Deduplicate and rank articles by relevance"""
    
    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.DeduplicationRankingAgent")
    
    def deduplicate_and_rank(
        self,
        articles: List[Dict],
        user_topics: List[str],
        preferred_journals: List[str],
        max_articles: int = 10,
        user_feedback: Optional[Dict[str, str]] = None
    ) -> List[Dict]:
        """Deduplicate by PMID and rank by relevance"""
        
        # Deduplicate by PMID
        seen_pmids = set()
        unique_articles = []
        
        for article in articles:
            pmid = article.get("pmid")
            if pmid and pmid not in seen_pmids:
                seen_pmids.add(pmid)
                unique_articles.append(article)
        
        self.logger.info(f"Deduplicated: {len(articles)} -> {len(unique_articles)} unique articles")
        
        # Score and rank
        for article in unique_articles:
            score = self._calculate_relevance_score(
                article,
                user_topics,
                preferred_journals,
                user_feedback
            )
            article["relevance_score"] = score
        
        # Sort by relevance score (descending)
        ranked = sorted(unique_articles, key=lambda x: x.get("relevance_score", 0), reverse=True)
        
        # Return top N
        return ranked[:max_articles]
    
    def _calculate_relevance_score(self, article: Dict, topics: List[str], preferred_journals: List[str], user_feedback: Optional[Dict[str, str]] = None) -> float:
        """Calculate relevance score for an article"""
        score = 0.0
        
        # Base score for having an article
        score += 1.0
        
        # Apply user feedback personalization (small adjustment)
        pmid = article.get("pmid")
        if user_feedback and pmid:
            feedback = user_feedback.get(pmid)
            if feedback == "useful":
                score += 0.5  # Small positive bump for previously useful articles
                self.logger.debug(f"Article {pmid}: +0.5 for previous 'useful' feedback")
            elif feedback == "not_relevant":
                score -= 0.7  # Small penalty for previously not relevant articles
                self.logger.debug(f"Article {pmid}: -0.7 for previous 'not_relevant' feedback")
        
        # Topic matching in title/abstract
        title = (article.get("title") or "").lower()
        abstract = (article.get("abstract") or "").lower()
        
        for topic in topics:
            topic_lower = topic.lower()
            if topic_lower in title:
                score += 3.0  # High weight for title match
            elif topic_lower in abstract:
                score += 1.5  # Medium weight for abstract match
        
        # Preferred journal bonus
        journal = article.get("journal") or ""
        if journal and any(pj.lower() in journal.lower() for pj in preferred_journals):
            score += 5.0
            article["is_preferred_journal"] = True
        else:
            article["is_preferred_journal"] = False
        
        # Study design tags with prioritization:
        # Guidelines > Meta-analysis > Systematic reviews > Review articles > Large observational
        design_tags = article.get("design_tags", [])
        design_boost = 0.0
        
        for design in design_tags:
            design_lower = design.lower()
            # Highest priority: Guidelines
            if "guideline" in design_lower or "practice guideline" in design_lower:
                design_boost = max(design_boost, 9.0)
            # Second priority: Meta-analyses
            elif "meta-analysis" in design_lower or "meta analysis" in design_lower:
                design_boost = max(design_boost, 8.0)
            # Third priority: Systematic reviews
            elif "systematic review" in design_lower:
                design_boost = max(design_boost, 7.0)
            # Fourth priority: Review Articles
            elif "review" in design_lower:
                design_boost = max(design_boost, 6.0)
            # Fifth priority: Large observational / cohort studies
            elif "cohort" in design_lower or "observational" in design_lower:
                design_boost = max(design_boost, 5.0)
            # Sixth priority: RCTs
            elif "randomized controlled trial" in design_lower or "rct" in design_lower:
                design_boost = max(design_boost, 4.0)
            # Lower priority: Other trials
            elif "clinical trial" in design_lower:
                design_boost = max(design_boost, 2.0)
        
        score += design_boost
        
        # Recency bonus (articles from last 7 days get small boost)
        # This is a simplified approach - in production would parse pub_date properly
        
        return score

class SummarizationAgent:
    """Generate AI summaries for articles using Anthropic Claude"""
    
    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.SummarizationAgent")
        self.api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("EMERGENT_LLM_KEY")
        
        if not self.api_key:
            self.logger.warning("OPENAI_API_KEY / EMERGENT_LLM_KEY not configured")
    
    async def summarize_articles(self, articles: List[Dict]) -> List[Dict]:
        """Summarize multiple articles that don't have summaries yet"""
        
        if not self.api_key:
            self.logger.warning("Cannot summarize: OPENAI_API_KEY / EMERGENT_LLM_KEY not configured")
            # Return articles with placeholder summaries
            for article in articles:
                if not article.get("ai_summary"):
                    article["ai_summary"] = "Summary not available"
                if not article.get("key_findings"):
                    article["key_findings"] = "Key findings not available"
            return articles
        
        summarized = []
        
        for article in articles:
            # Skip if already has summary
            if article.get("ai_summary") and article.get("key_findings"):
                summarized.append(article)
                continue
            
            # Check if abstract is available - use standard message if not
            abstract = article.get("abstract", "").strip()
            if not abstract or abstract.lower() in ["no abstract available", "abstract not available"]:
                # --- Re-fetch abstract from PubMed before giving up ---
                pmid = article.get("pmid")
                if pmid:
                    self.logger.info(f"Abstract missing for {pmid}, attempting re-fetch from PubMed...")
                    refetched_abstract = await self._retry_fetch_abstract(pmid)
                    if refetched_abstract:
                        article["abstract"] = refetched_abstract
                        abstract = refetched_abstract
                        self.logger.info(f"Successfully re-fetched abstract for {pmid} ({len(refetched_abstract)} chars)")
                    else:
                        self.logger.info(f"Re-fetch failed for {pmid}, abstract still unavailable")
            
            # Re-check after potential re-fetch
            if not abstract or abstract.lower() in ["no abstract available", "abstract not available"]:
                self.logger.info(f"Skipping AI summary for {article.get('pmid')}: No abstract available")
                article["ai_summary"] = "Full manuscript or abstract are not available to generate AI summary"
                article["key_findings"] = []
                article["population"] = ""
                article["study_size"] = ""
                article["key_questions"] = ""
                summarized.append(article)
                continue
            
            try:
                summary_data = await self._generate_summary(article)
                
                # Apply guardrail: Check if AI response indicates no abstract
                summary_text = summary_data.get("summary", "")
                if self._is_no_abstract_response(summary_text):
                    self.logger.info(f"AI returned 'no abstract' response for {article.get('pmid')}, using standard message")
                    article["ai_summary"] = "Full manuscript or abstract are not available to generate AI summary"
                    article["key_findings"] = []
                    article["population"] = ""
                    article["study_size"] = ""
                    article["key_questions"] = ""
                else:
                    article["ai_summary"] = summary_text
                    article["key_findings"] = summary_data.get("key_findings", [])
                    article["population"] = summary_data.get("population", "")
                    article["study_size"] = summary_data.get("study_size", "")
                    article["key_questions"] = summary_data.get("key_questions", "")
                
                summarized.append(article)
                
                # Rate limiting
                await asyncio.sleep(0.5)
                
            except Exception as e:
                self.logger.error(f"Failed to summarize article {article.get('pmid')}: {str(e)}")
                article["ai_summary"] = "Summary generation failed"
                article["key_findings"] = "Unable to extract key findings"
                summarized.append(article)
        
        self.logger.info(f"Summarized {len(summarized)} articles")
        return summarized
    
    def _is_no_abstract_response(self, summary_text: str) -> bool:
        """Check if AI response indicates no abstract was available"""
        if not summary_text:
            return False
        
        summary_lower = summary_text.lower()
        
        # Patterns that indicate "no abstract available" responses
        no_abstract_patterns = [
            "no abstract is available",
            "no abstract available",
            "abstract is not available",
            "abstract not available",
            "information not provided",
            "making it impossible to summarize",
            "cannot be determined without access",
            "full text is not available",
            "unable to provide a summary",
            "without access to the full"
        ]
        
        return any(pattern in summary_lower for pattern in no_abstract_patterns)
    
    async def _retry_fetch_abstract(self, pmid: str) -> Optional[str]:
        """Re-fetch a single article's abstract directly from PubMed.
        
        Called when an article's abstract is missing/unavailable during summarization.
        This handles the case where PubMed had not yet indexed the abstract at the time
        of the initial digest fetch, but it is available now.
        """
        try:
            from agents import PubMedSearchAgent
            searcher = PubMedSearchAgent()
            articles = await searcher._efetch([pmid])
            
            if articles:
                abstract = articles[0].get("abstract", "")
                if abstract and abstract.lower() not in ["no abstract available", "abstract not available", ""]:
                    return abstract
            
            return None
        except Exception as e:
            self.logger.warning(f"Re-fetch abstract failed for {pmid}: {type(e).__name__}: {e}")
            return None
    
    async def _generate_summary(self, article: Dict) -> Dict:
        """Generate summary for a single article using GPT-5-mini via emergentintegrations"""
        
        import uuid
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        
        system_message = """You are a medical literature summarization assistant for clinicians.

CRITICAL SAFETY AND GROUNDING RULES:
1. Summarize ONLY information EXPLICITLY stated in the provided abstract and metadata
2. Do NOT speculate, infer, or add any information not present in the source material
3. Do NOT provide medical advice or clinical recommendations beyond what the study states
4. If specific information is missing or unclear, explicitly state "Information not provided" rather than guessing
5. Never extrapolate beyond the study's actual findings
6. Focus strictly on: study design, population, interventions, outcomes, and stated limitations

DISCLAIMER REQUIREMENT:
Your summary is for educational purposes only. Clinicians must review the full article before applying any findings.

FORMAT:
Return ONLY valid JSON with these two fields:
{
  "summary": "2-3 paragraphs covering methods, results, and stated implications",
  "key_findings": ["Finding 1", "Finding 2", "Finding 3"]
}

The key_findings should be an array of 2-4 bullet points highlighting the most important results.
"""
        
        # Build prompt
        prompt = f"""Summarize this medical research article:
        
Title: {article.get('title', 'No title')}
Journal: {article.get('journal', 'Unknown')}
Publication Date: {article.get('pub_date', 'Unknown')}
Authors: {article.get('authors', 'Unknown')}

Abstract:
{article.get('abstract', 'No abstract available')}

Study Types: {', '.join(article.get('design_tags', []))}

Provide a JSON response with 'summary' and 'key_findings' fields.
"""
        
        summary_model = os.environ.get("SUMMARY_MODEL", "gpt-5-mini")
        
        # Use emergentintegrations LlmChat for OpenAI calls
        chat = LlmChat(
            api_key=self.api_key,
            session_id=f"summary_{uuid.uuid4().hex[:8]}",
            system_message=system_message,
        ).with_model("openai", summary_model)
        
        response_text = await chat.send_message(UserMessage(text=prompt))
        
        # Parse response
        try:
            # Try to extract JSON from response
            import json
            import re
            
            # Look for JSON block
            json_match = re.search(r'\{[\s\S]*"summary"[\s\S]*"key_findings"[\s\S]*\}', response_text)
            if json_match:
                data = json.loads(json_match.group())
                return {
                    "summary": data.get("summary", "Summary not available"),
                    "key_findings": data.get("key_findings", "Key findings not available")
                }
        except:
            pass
        
        # Fallback: use the whole response as summary
        return {
            "summary": response_text[:500] if len(response_text) > 500 else response_text,
            "key_findings": "See summary for key findings"
        }
