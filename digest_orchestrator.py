from typing import Dict, List, Optional
import logging
from datetime import datetime, timezone, timedelta
import uuid
from motor.motor_asyncio import AsyncIOMotorDatabase

from agents import QueryPlannerAgent, PubMedSearchAgent
from digest_agents import DeduplicationRankingAgent, SummarizationAgent
from date_utils import compute_next_run
from email_service import send_digest_email

logger = logging.getLogger(__name__)

class DigestOrchestrator:
    """Orchestrates the full digest generation pipeline"""
    
    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.logger = logging.getLogger(f"{__name__}.DigestOrchestrator")
        
        # Initialize agents
        self.query_planner = QueryPlannerAgent()
        self.pubmed_searcher = PubMedSearchAgent()
        self.ranker = DeduplicationRankingAgent()
        self.summarizer = SummarizationAgent()
    
    async def generate_digest_for_user(self, user_id: str, send_email: bool = True) -> Optional[Dict]:
        """Generate digest for a user (optionally send email)"""
        
        try:
            self.logger.info(f"Starting digest generation for user: {user_id}")
            
            # 1. Fetch user and preferences
            user = await self.db.users.find_one({"user_id": user_id}, {"_id": 0})
            if not user:
                self.logger.warning(f"User not found: {user_id}")
                return None
            
            preferences = await self.db.preferences.find_one({"user_id": user_id}, {"_id": 0})
            if not preferences or not preferences.get("is_active"):
                self.logger.warning(f"No active preferences for user: {user_id}")
                return None
            
            # 2. Compute date window
            last_run = preferences.get("last_run_timestamp")
            if last_run:
                start_date = datetime.fromisoformat(last_run)
            else:
                # First run: go back 30 days
                start_date = datetime.now(timezone.utc) - timedelta(days=30)
            
            end_date = datetime.now(timezone.utc)
            
            self.logger.info(f"Date window: {start_date} to {end_date}")
            
            # 3. Plan query
            query_plan = self.query_planner.plan_query(
                topics=preferences.get("topics_selected", []),
                custom_topics=preferences.get("custom_topics", []),
                journals=preferences.get("journals_selected", []),
                custom_journals=preferences.get("custom_journals", [])
            )
            
            # 4. Two-tier search
            all_articles = []
            
            # Tier 1: Preferred journals
            if query_plan["journal_filter"]:
                self.logger.info("Running Tier 1 search (preferred journals)")
                tier1_articles = await self.pubmed_searcher.search(
                    query=query_plan["query_string"],
                    start_date=start_date,
                    end_date=end_date,
                    max_results=20,
                    journal_filter=query_plan["journal_filter"]
                )
                all_articles.extend(tier1_articles)
                
                # If Tier 1 has fewer than 5 articles, run Tier 2
                if len(tier1_articles) < 5:
                    self.logger.info("Running Tier 2 search (broad)")
                    tier2_articles = await self.pubmed_searcher.search(
                        query=query_plan["query_string"],
                        start_date=start_date,
                        end_date=end_date,
                        max_results=20,
                        journal_filter=None
                    )
                    all_articles.extend(tier2_articles)
            else:
                # No preferred journals, just run broad search
                self.logger.info("Running broad search (no journal preference)")
                all_articles = await self.pubmed_searcher.search(
                    query=query_plan["query_string"],
                    start_date=start_date,
                    end_date=end_date,
                    max_results=20
                )
            
            if not all_articles:
                self.logger.info("No articles found")
                return {"article_count": 0, "message": "No new articles found"}
            
            # 5. Fetch user feedback for personalization
            user_feedback_map = {}
            try:
                from bson import ObjectId
                
                feedback_docs = await self.db.user_articles.find(
                    {
                        "user_id": user_id,
                        "relevance_feedback": {"$exists": True, "$ne": None}
                    },
                    {"_id": 0, "article_id": 1, "relevance_feedback": 1}
                ).to_list(None)
                
                # Build map of pmid -> feedback
                for doc in feedback_docs:
                    article_id = doc.get("article_id")
                    feedback = doc.get("relevance_feedback")
                    if article_id and feedback:
                        # Convert string to ObjectId if needed
                        if isinstance(article_id, str):
                            try:
                                article_id = ObjectId(article_id)
                            except:
                                continue
                        
                        # Get pmid from article_id
                        article_doc = await self.db.articles.find_one({"_id": article_id}, {"pmid": 1})
                        if article_doc and article_doc.get("pmid"):
                            user_feedback_map[article_doc["pmid"]] = feedback
                
                if user_feedback_map:
                    self.logger.info(f"Loaded {len(user_feedback_map)} feedback items for user {user_id} for personalization")
            except Exception as e:
                self.logger.warning(f"Could not load user feedback: {e}")
            
            # 6. Deduplicate and rank (with personalization)
            max_articles = preferences.get("max_articles_per_digest", 10)
            all_topics = preferences.get("topics_selected", []) + preferences.get("custom_topics", [])
            all_journals = preferences.get("journals_selected", []) + preferences.get("custom_journals", [])
            
            ranked_articles = self.ranker.deduplicate_and_rank(
                articles=all_articles,
                user_topics=all_topics,
                preferred_journals=all_journals,
                max_articles=max_articles,
                user_feedback=user_feedback_map
            )
            
            if not ranked_articles:
                self.logger.info("No articles after ranking")
                return {"article_count": 0, "message": "No relevant articles found"}
            
            # 7. Summarize articles
            self.logger.info(f"Summarizing {len(ranked_articles)} articles")
            summarized_articles = await self.summarizer.summarize_articles(ranked_articles)
            
            # 8. Save articles to database
            article_ids, article_pmids = await self._save_articles(summarized_articles)
            
            # 9. Save user_articles associations
            await self._save_user_articles(user_id, article_ids, article_pmids)
            
            # 10. Create digest record
            digest_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            
            digest_doc = {
                "digest_id": digest_id,
                "user_id": user_id,
                "generated_at": now.isoformat(),
                "email_sent_at": None,
                "status": "pending",
                "frequency_snapshot": preferences.get("schedule", {}).get("frequency", "unknown"),
                "articles": article_ids,
                "article_pmids": article_pmids,  # Store PMIDs for screen queue
                "specialty_id": preferences.get("specialty_id", ""),
                "subspecialty_id": preferences.get("subspecialty_id", ""),
                "created_at": now.isoformat()
            }
            
            # 10. Send email if requested, user is verified, and email not suppressed
            if send_email and user.get("is_verified"):
                # Check email notification settings (backward compatible defaults)
                email_enabled = preferences.get("email_notifications_enabled", True)
                email_suppress_until = preferences.get("email_suppress_until")
                
                # Check if email is currently suppressed
                email_suppressed = False
                if email_suppress_until:
                    try:
                        suppress_date = datetime.fromisoformat(email_suppress_until.replace('Z', '+00:00'))
                        if suppress_date > now:
                            email_suppressed = True
                            self.logger.info(f"Email suppressed until {email_suppress_until} for user {user_id}")
                    except Exception as e:
                        self.logger.warning(f"Invalid suppress_until date: {str(e)}")
                
                # Send email only if enabled and not suppressed
                if not email_enabled:
                    digest_doc["status"] = "completed"
                    self.logger.info(f"Email notifications disabled for user {user['email']}")
                elif email_suppressed:
                    digest_doc["status"] = "completed"
                    self.logger.info(f"Email suppressed for user {user['email']}")
                else:
                    try:
                        # Load specialty/subspecialty names from config
                        specialty_name, subspecialty_name = await self._get_specialty_names(
                            preferences.get("specialty_id"),
                            preferences.get("subspecialty_id")
                        )
                        
                        email_sent = send_digest_email(
                            email=user["email"],
                            full_name=user.get("full_name", user["email"].split('@')[0]),
                            specialty_name=specialty_name,
                            subspecialty_name=subspecialty_name,
                            articles=summarized_articles,
                            digest_date=now.strftime("%Y-%m-%d")
                        )
                        
                        if email_sent:
                            digest_doc["status"] = "sent"
                            digest_doc["email_sent_at"] = now.isoformat()
                            self.logger.info(f"Digest email sent to {user['email']}")
                        else:
                            digest_doc["status"] = "failed"
                            self.logger.warning(f"Failed to send digest email to {user['email']}")
                            
                    except Exception as e:
                        self.logger.error(f"Email sending error: {str(e)}")
                        digest_doc["status"] = "failed"
            elif not send_email:
                digest_doc["status"] = "completed"
                self.logger.info(f"Digest generated (email not requested) for {user['email']}")
            else:
                digest_doc["status"] = "skipped_unverified"
                self.logger.info(f"User {user_id} not verified, skipping email")
            
            # Save digest
            await self.db.digests.insert_one(digest_doc)
            
            # 11. Update preferences timestamps (non-blocking — digest already saved)
            try:
                await self.db.preferences.update_one(
                    {"user_id": user_id},
                    {
                        "$set": {
                            "last_run_timestamp": now.isoformat(),
                            "next_run_timestamp": compute_next_run(now, preferences["schedule"]).isoformat(),
                            "updated_at": now.isoformat()
                        }
                    }
                )
            except Exception as pref_err:
                self.logger.warning(f"Failed to update preference timestamps (non-blocking): {pref_err}")
            
            self.logger.info(f"Digest generated successfully: {digest_id} ({len(ranked_articles)} articles)")
            
            # 12. Trigger Daily Briefing for premium users (non-blocking)
            try:
                from services.briefing_service import create_daily_briefing
                await create_daily_briefing(
                    db=self.db,
                    user_id=user_id,
                    digest_id=digest_id,
                    article_count=len(ranked_articles),
                )
            except Exception as briefing_err:
                self.logger.warning(f"Daily briefing creation failed (non-blocking): {briefing_err}")
            
            return {
                "digest_id": digest_id,
                "article_count": len(ranked_articles),
                "status": digest_doc["status"],
                "message": "Digest generated successfully"
            }
            
        except Exception as e:
            self.logger.error(f"Digest generation failed for user {user_id}: {str(e)}")
            import traceback
            traceback.print_exc()
            return None
    
    async def generate_digest_for_profile(
        self, user_id: str, profile: dict, send_email: bool = True
    ):
        """
        Generate a digest for a specific digest_profile document.
        Phase 5: ENABLE_MULTI_DIGEST_PROFILES=true path.

        Mirrors generate_digest_for_user() but:
        - Uses profile fields (specialty_id, topics, journals, schedule) as preferences
        - Stores profile_id in the digest document
        - Updates next_run_timestamp on digest_profiles, not preferences
        - Tolerates incomplete/legacy profiles (all lists default to [])

        PHI-Zero: profile names / keywords / custom_topics are never logged.
        """
        profile_id = profile.get("profile_id", "unknown")
        error_stage = "init"
        try:
            self.logger.info("[PROFILES] Generating digest for user=%s profile_id=%s", user_id, profile_id)

            error_stage = "fetch_user"
            user = await self.db.users.find_one({"user_id": user_id}, {"_id": 0})
            if not user:
                self.logger.warning("[PROFILES] User not found: user=%s", user_id)
                return None

            if not profile.get("is_active"):
                self.logger.info("[PROFILES] Profile inactive, skipping: profile_id=%s", profile_id)
                return None

            # ---------------------------------------------------------------
            # Read UX-E enriched fields with safe defaults for older profiles
            # ---------------------------------------------------------------
            topics_selected = profile.get("topics_selected") or []
            custom_topics = profile.get("custom_topics") or profile.get("custom_keywords") or []
            journals_selected = profile.get("journals_selected") or []
            custom_journals = profile.get("custom_journals") or []
            max_articles = profile.get("max_articles_per_digest") or 10
            email_notifications_enabled = profile.get("email_notifications_enabled", True)
            email_suppress_until = profile.get("email_suppress_until")

            # Build schedule dict compatible with compute_next_run()
            raw_schedule = profile.get("schedule")
            if isinstance(raw_schedule, dict):
                schedule_dict = {
                    "frequency": raw_schedule.get("frequency", "weekly"),
                    "time_local": raw_schedule.get("time_local", "09:00"),
                    "timezone": raw_schedule.get("timezone", "UTC"),
                    "day_of_week": raw_schedule.get("day_of_week"),
                    "day_of_month": raw_schedule.get("day_of_month"),
                }
            else:
                # Legacy profile: only has digest_frequency string
                schedule_dict = {
                    "frequency": profile.get("digest_frequency", "weekly"),
                    "time_local": "09:00",
                    "timezone": "UTC",
                }

            # ---------------------------------------------------------------
            # Date window
            # ---------------------------------------------------------------
            last_run = profile.get("last_run_timestamp")
            if last_run:
                start_date = datetime.fromisoformat(last_run)
            else:
                start_date = datetime.now(timezone.utc) - timedelta(days=30)
            end_date = datetime.now(timezone.utc)

            # ---------------------------------------------------------------
            # Query plan — use same inputs as legacy path
            # ---------------------------------------------------------------
            error_stage = "query_plan"
            all_topics = topics_selected + custom_topics
            all_journals = journals_selected + custom_journals

            query_plan = self.query_planner.plan_query(
                topics=topics_selected,
                custom_topics=custom_topics,
                journals=journals_selected,
                custom_journals=custom_journals,
            )

            # If no keywords at all, fall back to specialty search
            if not query_plan["query_string"]:
                spec = profile.get("specialty_id", "medicine")
                query_plan["query_string"] = f'"{spec}"[MeSH Terms]'

            # ---------------------------------------------------------------
            # Two-tier search (same as generate_digest_for_user)
            # ---------------------------------------------------------------
            error_stage = "pubmed_search"
            all_articles = []
            if query_plan.get("journal_filter"):
                tier1 = await self.pubmed_searcher.search(
                    query=query_plan["query_string"],
                    start_date=start_date,
                    end_date=end_date,
                    max_results=20,
                    journal_filter=query_plan["journal_filter"],
                )
                all_articles.extend(tier1)
                if len(tier1) < 5:
                    tier2 = await self.pubmed_searcher.search(
                        query=query_plan["query_string"],
                        start_date=start_date,
                        end_date=end_date,
                        max_results=30,
                    )
                    all_articles.extend(tier2)
            else:
                all_articles = await self.pubmed_searcher.search(
                    query=query_plan["query_string"],
                    start_date=start_date,
                    end_date=end_date,
                    max_results=30,
                )

            if not all_articles:
                self.logger.info("[PROFILES] No articles found for profile_id=%s", profile_id)
                return {"digest_id": None, "article_count": 0, "status": "no_articles"}

            # ---------------------------------------------------------------
            # Dedupe, rank, summarise — SAME pipeline as legacy
            # ---------------------------------------------------------------
            error_stage = "rank"
            user_feedback = {}
            try:
                fb = await self.db.user_articles.find(
                    {"user_id": user_id, "relevance_feedback": {"$ne": None}},
                    {"_id": 0, "article_id": 1, "relevance_feedback": 1},
                ).to_list(1000)
                user_feedback = {f["article_id"]: f["relevance_feedback"] for f in fb}
            except Exception:
                pass

            # ---------------------------------------------------------------
            # Practice profile personalization (conservative boost only)
            # - Explicit digest preferences (topics_selected, journals) stay primary
            # - Practice profile adds secondary ranking signals
            # - Does NOT override or remove any explicit preference
            # ---------------------------------------------------------------
            ranking_topics = list(all_topics)  # copy explicit topics
            ranking_journals = list(all_journals)  # copy explicit journals
            try:
                pp = user.get("practice_profile") or {}
                if pp:
                    # Subspecialties → secondary topic boost (added after explicit topics)
                    for sub in (pp.get("subspecialties") or []):
                        if sub and sub not in ranking_topics:
                            ranking_topics.append(sub)
                    # Specialty 2 → light topic boost
                    s2 = pp.get("specialty_2")
                    if s2 and s2 not in ranking_topics:
                        ranking_topics.append(s2)
            except Exception:
                pass  # Never fail digest on practice profile errors

            ranked = self.ranker.deduplicate_and_rank(
                articles=all_articles,
                user_topics=ranking_topics,
                preferred_journals=ranking_journals,
                max_articles=max_articles,
                user_feedback=user_feedback,
            )

            if not ranked:
                self.logger.info("[PROFILES] No articles after ranking for profile_id=%s", profile_id)
                return {"digest_id": None, "article_count": 0, "status": "no_articles"}

            error_stage = "summarize"
            summarized = await self.summarizer.summarize_articles(ranked)

            # ---------------------------------------------------------------
            # Persist digest
            # ---------------------------------------------------------------
            error_stage = "persist_digest"
            article_ids, article_pmids = await self._save_articles(summarized)
            await self._save_user_articles(user_id, article_ids, article_pmids)

            now = datetime.now(timezone.utc)
            digest_id = str(uuid.uuid4())
            digest_doc = {
                "digest_id": digest_id,
                "user_id": user_id,
                "profile_id": profile_id,   # Phase 5: associate with profile
                "generated_at": now.isoformat(),
                "email_sent_at": None,
                "status": "pending",
                "frequency_snapshot": schedule_dict.get("frequency", "unknown"),
                "articles": article_ids,
                "article_pmids": article_pmids,  # Store PMIDs for screen queue
                "specialty_id": profile.get("specialty_id", ""),
                "subspecialty_id": profile.get("subspecialty_id", ""),
                "created_at": now.isoformat(),
                "deleted_at": None,          # Phase 5: soft-delete support
            }

            # ---------------------------------------------------------------
            # Email (same logic as legacy path)
            # ---------------------------------------------------------------
            error_stage = "email"
            if send_email:
                # Check email notification settings
                email_suppressed = False
                if email_suppress_until:
                    try:
                        suppress_date = datetime.fromisoformat(
                            email_suppress_until.replace("Z", "+00:00")
                        )
                        if suppress_date > now:
                            email_suppressed = True
                    except Exception:
                        pass

                if not email_notifications_enabled:
                    digest_doc["status"] = "completed"
                elif email_suppressed:
                    digest_doc["status"] = "completed"
                else:
                    specialty_name, subspecialty_name = await self._get_specialty_names(
                        profile.get("specialty_id"), profile.get("subspecialty_id")
                    )
                    try:
                        from email_service import send_digest_email
                        sent = send_digest_email(
                            email=user["email"],
                            full_name=user.get("full_name", user["email"].split("@")[0]),
                            specialty_name=specialty_name,
                            subspecialty_name=subspecialty_name,
                            articles=summarized,
                            digest_date=now.strftime("%Y-%m-%d"),
                        )
                        digest_doc["status"] = "sent" if sent else "failed"
                        if sent:
                            digest_doc["email_sent_at"] = now.isoformat()
                    except Exception:
                        digest_doc["status"] = "failed"
            elif not send_email:
                digest_doc["status"] = "completed"

            await self.db.digests.insert_one(digest_doc)

            # ---------------------------------------------------------------
            # Update profile timestamps + clear error fields on success
            # ---------------------------------------------------------------
            error_stage = "update_timestamps"
            from date_utils import compute_next_run
            await self.db.digest_profiles.update_one(
                {"profile_id": profile_id},
                {"$set": {
                    "last_run_timestamp": now.isoformat(),
                    "next_run_timestamp": compute_next_run(now, schedule_dict).isoformat(),
                    "updated_at": now.isoformat(),
                    "last_digest_error_code": None,
                    "last_digest_error_at": None,
                    "last_digest_error_stage": None,
                }},
            )

            self.logger.info(
                "[PROFILES] Digest complete profile_id=%s digest_id=%s articles=%d",
                profile_id, digest_id, len(ranked),
            )
            return {"digest_id": digest_id, "article_count": len(ranked), "status": digest_doc["status"]}

        except Exception as e:
            # PHI-safe logging: exception type + file:line + function — never user content
            tb = e.__traceback__
            fname = tb.tb_frame.f_code.co_filename.split("/")[-1] if tb else "?"
            lineno = tb.tb_lineno if tb else "?"
            func = tb.tb_frame.f_code.co_name if tb else "?"
            self.logger.error(
                "[PROFILES] Digest failed profile_id=%s: %s at %s:%s in %s (stage=%s)",
                profile_id, type(e).__name__, fname, lineno, func, error_stage,
            )
            return None

    async def _save_articles(self, articles: List[Dict]) -> tuple:
        """Save articles to database and return their IDs and PMIDs.
        
        If an article already exists with 'No abstract available' but the new
        fetch has a real abstract, the abstract will be updated.
        
        Returns:
            tuple: (article_ids, article_pmids) - both as lists of strings
        """
        article_ids = []
        article_pmids = []
        
        for article in articles:
            try:
                # Add timestamps
                now = datetime.now(timezone.utc).isoformat()
                article["updated_at"] = now
                
                # Upsert by pmid
                pmid = article.get("pmid")
                if pmid:
                    # Check if article already exists
                    existing = await self.db.articles.find_one({"pmid": pmid})
                    
                    if existing:
                        # Smart merge: only update abstract if new one is better
                        new_abstract = article.get("abstract", "")
                        old_abstract = existing.get("abstract", "")
                        
                        # Keep the existing abstract if new one is empty/unavailable
                        if new_abstract in ["", "No abstract available"] and old_abstract not in ["", "No abstract available"]:
                            article["abstract"] = old_abstract
                            self.logger.debug(f"Preserving existing abstract for {pmid}")
                        
                        # Don't overwrite created_at for existing articles
                        article["created_at"] = existing.get("created_at", now)
                    else:
                        article["created_at"] = now
                    
                    result = await self.db.articles.update_one(
                        {"pmid": pmid},
                        {"$set": article},
                        upsert=True
                    )
                    
                    # Get the article _id
                    saved_article = await self.db.articles.find_one({"pmid": pmid})
                    if saved_article:
                        article_ids.append(str(saved_article["_id"]))
                        article_pmids.append(pmid)
                
            except Exception as e:
                self.logger.error(f"Failed to save article {article.get('pmid')}: {str(e)}")
                continue
        
        return article_ids, article_pmids
    
    async def _save_user_articles(self, user_id: str, article_ids: List[str],
                                    article_pmids: Optional[List[str]] = None):
        """Save user-article associations.

        Stage 1A: uses the legacy-aware helper so that an existing record
        (whether keyed by ObjectId or PMID) is updated rather than duplicated,
        and always persists the canonical ``pmid`` field.
        """
        from utils.user_article_compat import ua_match_filter

        now = datetime.now(timezone.utc).isoformat()
        pmids = article_pmids or [None] * len(article_ids)

        for article_id, pmid in zip(article_ids, pmids):
            try:
                filt = ua_match_filter(user_id, pmid=pmid, article_obj_id=article_id)
                set_on_insert: dict = {
                    "user_id": user_id,
                    "article_id": article_id,
                    "saved_to_library": False,
                    "saved_at": None,
                    "created_at": now,
                }
                set_fields: dict = {
                    "seen_in_digest_at": now,
                    "updated_at": now,
                }
                # Always persist pmid when known
                if pmid:
                    set_fields["pmid"] = pmid

                await self.db.user_articles.update_one(
                    filt,
                    {"$set": set_fields, "$setOnInsert": set_on_insert},
                    upsert=True,
                )
            except Exception as e:
                self.logger.error(f"Failed to save user_article: {str(e)}")
                continue
    
    async def _get_specialty_names(self, specialty_id: str, subspecialty_id: str) -> tuple:
        """Get human-readable specialty names from config"""
        try:
            from pathlib import Path
            import json
            
            config_path = Path(__file__).parent / "config" / "specialty_config.json"
            with open(config_path, 'r') as f:
                config = json.load(f)
            
            for specialty in config["specialties"]:
                if specialty["id"] == specialty_id:
                    specialty_name = specialty["label"]
                    for subspec in specialty["subspecialties"]:
                        if subspec["id"] == subspecialty_id:
                            return specialty_name, subspec["label"]
                    return specialty_name, "General"
            
            return "Medicine", "General"
            
        except Exception as e:
            self.logger.error(f"Failed to load specialty names: {str(e)}")
            return "Medicine", "General"
