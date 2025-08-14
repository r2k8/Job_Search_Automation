#!/usr/bin/env python3
"""
Unified Job Search Automation — full, modular, production-ready

Key features
- Modular phases with CLI: discovery → jd_extraction → scoring → recovery.
- Default behavior runs all phases when --phase is not provided.
- JD extraction uses **Firefox** with a dedicated **jobs profile** for best site compatibility.
- Real-time Google Sheets writes after each significant step (pending → JD Extracted → Analyzed …).
- Gmail discovery parses job alert emails and extracts plausible links.
- Selenium scraper expands job descriptions and extracts content via multiple selectors with BS4 fallback.
- Optional SERPER enrichment if scraping fails.
- Vertex AI Gemini analysis returns structured JSON (match score, summary, skills, tips).
- Idempotent Supabase writes via upsert; resilient retries for Sheets & DB.

Requirements
- Python 3.10+
- Env vars in .env or shell (see ENV section below).
- Files: client_secrets.json (Gmail OAuth), credentials.json (Sheets service acct), resumes (see JOB_CONFIG).

Run
  python jobbot.py                     # run all phases in order
  python jobbot.py --phase discovery   # run a single phase
  python jobbot.py --phase jd_extraction --no-headless --profile "/path/to/Firefox/Profiles/jobs"

"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import gspread
import requests
import vertexai
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as SA_Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FFOptions
from selenium.webdriver.firefox.service import Service as FFService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from supabase import create_client
from vertexai.generative_models import GenerativeModel
from webdriver_manager.firefox import GeckoDriverManager

# -----------------------------------------------------------------------------
# ENV / CONFIG
# -----------------------------------------------------------------------------
load_dotenv()
SERPER_API_KEY: Optional[str] = os.getenv("SERPER_API_KEY")
SUPABASE_URL: Optional[str] = os.getenv("SUPABASE_URL")
SUPABASE_KEY: Optional[str] = os.getenv("SUPABASE_KEY")
GOOGLE_SHEET_URL: Optional[str] = os.getenv("GOOGLE_SHEET_URL")
GCP_PROJECT: Optional[str] = os.getenv("GOOGLE_CLOUD_PROJECT")
GCP_LOCATION: Optional[str] = os.getenv("GOOGLE_CLOUD_LOCATION")
MATCH_SCORE_THRESHOLD = int(os.getenv("MATCH_SCORE_THRESHOLD", 80))
TIMEOUT_PAGE_LOAD = int(os.getenv("TIMEOUT_PAGE_LOAD", 30))
CAPTCHA_WAIT = int(os.getenv("CAPTCHA_WAIT", 30))
DEFAULT_PROFILE = os.getenv("FIREFOX_PROFILE_PATH", None)  # e.g. /Users/you/Library/Application Support/Firefox/Profiles/abcd.jobs
HEADLESS_DEFAULT = os.getenv("HEADLESS", "1") not in ("0", "false", "False")
GMAIL_SINCE_DAYS = int(os.getenv("GMAIL_SINCE_DAYS", 14))
GMAIL_MAX_RESULTS = int(os.getenv("GMAIL_MAX_RESULTS", 50))

# Map roles to resumes and target sheet tab
JOB_CONFIG = [
    {"keywords": ["technical program manager", "tpm"], "resume": "resume_tpm.txt", "sheet": "PM_Architect_Jobs"},
    {"keywords": ["product manager", "pmt"], "resume": "resume_pmt.txt", "sheet": "PM_Architect_Jobs"},
    {"keywords": ["architect", "cloud engineer"], "resume": "resume_architect.txt", "sheet": "PM_Architect_Jobs"},
    {"keywords": ["analyst", "business analyst", "quantitative"], "resume": "resume_analyst.txt", "sheet": "Analyst_Jobs"},
]

SHEET_HEADERS = [
    "date_found",
    "company_name",
    "title",
    "location",
    "apply_link",
    "status",
    "match_score",
    "match_summary",
    "experience_level",
    "salary_estimate",
    "technical_skills",
    "resume_tips",
    "description",
]

SENDERS = {
    "Google": ["jobs-noreply@google.com", "notify-noreply@google.com"],
    "LinkedIn": ["jobs-noreply@linkedin.com", "jobalerts-noreply@linkedin.com"],
    "Microsoft": ["jobalerts@microsoft.com"],
}

SHOW_MORE_SELECTORS = [
    (By.XPATH, "//button[contains(., 'Show more') or contains(., 'See more') or contains(., 'Read more')]|//a[contains(., 'Show more') or contains(., 'See more') or contains(., 'Read more')]"),
    (By.CSS_SELECTOR, "#ember41 > span:nth-child(2)"),  # LinkedIn specific "see more" button
]

DESCRIPTION_SELECTORS = [
    (By.ID, "jobDescriptionText"),
    (By.CLASS_NAME, "jobsearch-jobDescriptionText"),
    (By.CSS_SELECTOR, "[data-test-description]"),
    (By.CSS_SELECTOR, ".description__text"),
    (By.CSS_SELECTOR, ".job-description"),
    (By.CSS_SELECTOR, ".jobs-description__container"),
    (By.CSS_SELECTOR, ".us2QZb"),
    (By.CSS_SELECTOR, ".hkXmid"),
    # LinkedIn specific selectors
    (By.CSS_SELECTOR, ".jobs-description__content"),
    (By.CSS_SELECTOR, ".jobs-box__html-content"),
    (By.CSS_SELECTOR, "[data-job-description]"),
    (By.CSS_SELECTOR, ".jobs-description"),
    (By.CSS_SELECTOR, ".jobs-box__content"),
]

BS4_DESCRIPTION_CSS = [
    "#jobDescriptionText",
    ".jobsearch-jobDescriptionText",
    "[data-test-description]",
    ".job-description",
    ".description__text",
    "article[role='main']",
    "main .content",
    "section[aria-label*='Description']",
    # LinkedIn specific BS4 selectors
    ".jobs-description__content",
    ".jobs-box__html-content",
    "[data-job-description]",
    ".jobs-description",
    ".jobs-box__content",
    ".jobs-description__container",
]

JOB_URL_ALLOWLIST = (
    "linkedin.com/jobs",
    "indeed.com",
    "jobs.google.com",
    "careers.google.com",
    "microsoft.com/careers",
    "lever.co",
    "greenhouse.io",
    "myworkdayjobs.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "boards.greenhouse.io",
    "jobs.lever.co",
)

# -----------------------------------------------------------------------------
# UTILITIES
# -----------------------------------------------------------------------------

def validate_env() -> None:
    missing = [
        name
        for name, val in [
            ("SUPABASE_URL", SUPABASE_URL),
            ("SUPABASE_KEY", SUPABASE_KEY),
            ("GOOGLE_SHEET_URL", GOOGLE_SHEET_URL),
            ("GOOGLE_CLOUD_PROJECT", GCP_PROJECT),
            ("GOOGLE_CLOUD_LOCATION", GCP_LOCATION),
        ]
        if not val
    ]
    if missing:
        logging.warning("Missing env vars: %s — some features may fail until provided.", ", ".join(missing))


def setup_services(headless: bool) -> tuple[GenerativeModel, any, any, any]:
    validate_env()

    vertexai.init(project=GCP_PROJECT, location=GCP_LOCATION)
    model = GenerativeModel("gemini-2.5-pro")

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Gmail
    SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
    creds: Optional[Credentials] = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("client_secrets.json", SCOPES)
            creds = flow.run_local_server(open_browser=False)
        with open("token.json", "w") as t:
            t.write(creds.to_json())
    gmail = build("gmail", "v1", credentials=creds)

    # Sheets via Service Account
    sa_creds = SA_Credentials.from_service_account_file(
        "credentials.json",
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"],
    )
    sheet = gspread.authorize(sa_creds).open_by_url(GOOGLE_SHEET_URL)

    return model, supabase, gmail, sheet


def clean_text(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", s.strip()) if isinstance(s, str) else ""


def get_processed(supabase) -> set[tuple[str, str]]:
    try:
        rows = supabase.table("jobs").select("company_name", "title").execute().data
        return {(clean_text(r.get("company_name", "")).lower(), clean_text(r.get("title", "")).lower()) for r in rows or []}
    except Exception as e:
        logging.warning("Failed to load processed from Supabase: %s", e)
        return set()


def safe_update_sheet(ws, cell_range: str, vals: List[List[object]], retries: int = 3, backoff: int = 2) -> bool:
    for i in range(retries):
        try:
            ws.update(values=vals, range_name=cell_range)
            return True
        except Exception as e:
            logging.warning("Sheet update %s attempt %d: %s", cell_range, i + 1, e)
            time.sleep(backoff**i)
    logging.error("Permanent sheet failure %s", cell_range)
    return False


def safe_insert_supabase(table, rec: Dict, retries: int = 3, backoff: int = 2) -> bool:
    for i in range(retries):
        try:
            table.insert(rec).execute()
            return True
        except Exception as e:
            logging.warning("Supabase insert attempt %d: %s", i + 1, e)
            time.sleep(backoff**i)
    logging.error("Permanent Supabase insertion failure")
    return False


def safe_upsert_supabase(table, rec: Dict, on_conflict: str, retries: int = 3, backoff: int = 2) -> bool:
    for i in range(retries):
        try:
            table.upsert(rec, on_conflict=on_conflict).execute()
            return True
        except Exception as e:
            logging.warning("Supabase upsert attempt %d: %s", i + 1, e)
            time.sleep(backoff**i)
    logging.error("Permanent Supabase upsert failure")
    return False


# -----------------------------------------------------------------------------
# DISCOVERY (Gmail)
# -----------------------------------------------------------------------------

def discover_jobs(gmail) -> List[Dict]:
    logging.info("Discovering jobs from Gmail …")

    since = (datetime.now(timezone.utc) - timedelta(days=GMAIL_SINCE_DAYS)).strftime("%Y/%m/%d")
    addresses = [a for lst in SENDERS.values() for a in lst]
    from_q = " OR ".join([f"from:{a}" for a in addresses])
    q = f"({from_q}) is:unread newer_than:{GMAIL_SINCE_DAYS}d after:{since}"

    out: List[Dict] = []
    try:
        msg_list = gmail.users().messages().list(userId="me", q=q, maxResults=GMAIL_MAX_RESULTS).execute()
    except HttpError as e:
        logging.error("Gmail list failed: %s", e)
        return out

    ids = [m["id"] for m in msg_list.get("messages", [])]
    logging.info("Found %d job alert emails", len(ids))

    for mid in ids:
        try:
            m = gmail.users().messages().get(userId="me", id=mid, format="full").execute()
        except HttpError as e:
            logging.warning("Gmail get failed for %s: %s", mid, e)
            continue

        headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "")
        from_header = headers.get("from", "")
        body_text = _gmail_extract_body_text(m)
        
        # For Google job alerts, extract URLs from HTML content
        if "notify-noreply@google.com" in from_header:
            raw_content = ""
            payload = m.get("payload", {})
            if "parts" in payload:
                for p in payload.get("parts", []) or []:
                    mime = p.get("mimeType", "")
                    if mime == "text/html":
                        try:
                            raw_content = base64.urlsafe_b64decode(p.get("body", {}).get("data", "").encode("utf-8")).decode("utf-8", errors="ignore")
                            break
                        except Exception:
                            pass
            
            if raw_content:
                # Extract URLs from HTML content for Google job alerts
                html_urls = re.findall(r'https?://[^\s<>"]+', raw_content)
                logging.info("URLs found in raw HTML: %d", len(html_urls))
                if html_urls:
                    logging.info("First few HTML URLs: %s", html_urls[:3])
                    # Filter for Google redirect URLs
                    google_redirect_urls = [url for url in html_urls if "notifications.googleapis.com/email/redirect" in url]
                    logging.info("Google redirect URLs found: %d", len(google_redirect_urls))
                    urls = google_redirect_urls
                else:
                    urls = _extract_urls(body_text)
            else:
                urls = _extract_urls(body_text)
        else:
            urls = _extract_urls(body_text)
        
        # Special handling for Google job alerts
        if "notify-noreply@google.com" in from_header:
            logging.info("Processing Google job alert email")
            logging.info("Subject: %s", subject)
            logging.info("Body text length: %d", len(body_text))
            logging.info("Body text preview: %s", body_text[:200])
            logging.info("Body text contains 'notifications.googleapis.com': %s", "notifications.googleapis.com" in body_text)
            logging.info("Body text contains 'redirect': %s", "redirect" in body_text)
            logging.info("URLs found: %d", len(urls))
            if urls:
                logging.info("First few URLs: %s", urls[:3])
            else:
                logging.info("No URLs found in body text")
                # Look for Google redirect URLs specifically
                google_redirect_pattern = r'https://notifications\.googleapis\.com/email/redirect\?[^"\s]+'
                google_urls = re.findall(google_redirect_pattern, body_text)
                logging.info("Google redirect URLs found: %d", len(google_urls))
                if google_urls:
                    logging.info("First few Google redirect URLs: %s", google_urls[:3])
                else:
                    # Try a broader search for any URLs
                    all_urls = re.findall(r'https?://[^\s<>"]+', body_text)
                    logging.info("All URLs found in body: %d", len(all_urls))
                    if all_urls:
                        logging.info("First few all URLs: %s", all_urls[:3])
            jobs = _extract_google_job_alerts(body_text, urls)
            out.extend(jobs)
        else:
            job_url = next((u for u in urls if any(d in u for d in JOB_URL_ALLOWLIST)), urls[0] if urls else None)
            if not job_url:
                continue

            company_guess = _guess_company(from_header, subject, job_url)
            title_guess = _guess_title(subject, body_text)
            location_guess = _guess_location(body_text)

            out.append({
                "msg_id": mid,
                "company_name": company_guess,
                "title": title_guess,
                "location": location_guess,
                "url": job_url,
            })

    logging.info("Discovered %d candidate jobs from Gmail", len(out))
    # Mark all fetched job alert emails as read
    try:
        if ids:
            gmail.users().messages().batchModify(userId="me", body={
                "ids": ids,
                "removeLabelIds": ["UNREAD"],
            }).execute()
            logging.info("Marked %d Gmail messages as read", len(ids))
    except HttpError as e:
        logging.warning("Failed to mark emails as read: %s", e)
    return out


def _gmail_extract_body_text(m: Dict) -> str:
    def decode(b64: str) -> str:
        try:
            return base64.urlsafe_b64decode(b64.encode("utf-8")).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    payload = m.get("payload", {})
    data_chunks: List[str] = []

    if "parts" in payload:
        for p in payload.get("parts", []) or []:
            mime = p.get("mimeType", "")
            if mime in ("text/plain", "text/html"):
                data_chunks.append(decode(p.get("body", {}).get("data", "")))
            for sp in p.get("parts", []) or []:
                if sp.get("mimeType") in ("text/plain", "text/html"):
                    data_chunks.append(decode(sp.get("body", {}).get("data", "")))
    else:
        if payload.get("mimeType") in ("text/plain", "text/html"):
            data_chunks.append(decode(payload.get("body", {}).get("data", "")))

    text = "\n".join([BeautifulSoup(c, "html.parser").get_text(" ") for c in data_chunks if c])
    return clean_text(text)

URL_REGEX = re.compile(r"https?://[\w\-\.\?/=&%#]+", re.I)

def _extract_urls(text: str) -> List[str]:
    urls = URL_REGEX.findall(text or "")
    logging.debug("Found %d regular URLs", len(urls))
    
    # Also look for Google job alert redirect URLs
    google_redirect_pattern = r'https://notifications\.googleapis\.com/email/redirect\?[^"\s]+'
    google_urls = re.findall(google_redirect_pattern, text or "")
    logging.debug("Found %d Google redirect URLs", len(google_urls))
    
    all_urls = urls + google_urls
    logging.debug("Total URLs found: %d", len(all_urls))
    
    if all_urls:
        logging.debug("First few URLs: %s", all_urls[:3])
    
    return [resolve_redirect(u) for u in all_urls]


def _guess_company(from_header: str, subject: str, url: str) -> str:
    # Clean HTML entities from subject
    subject = subject.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    
    m = re.search(r"^(.*?)\s+[-|—]", subject)
    if m:
        return clean_text(m.group(1))
    
    # For Google job alerts, try to extract company from the body text
    # Look for patterns like "Company Name" after job titles
    if "notify-noreply@google.com" in from_header:
        # This is a Google job alert, try to extract company from the email body
        # The company name is usually on the line after the job title
        return "Google Job Alert"  # Placeholder, will be updated in the main processing
    
    host = re.sub(r"^https?://", "", url).split("/")[0].replace("www.", "")
    return host.split(".")[0].title()


def _guess_title(subject: str, body_text: str) -> str:
    # Clean HTML entities from subject
    subject = subject.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    
    m = re.search(r"\b(?:Role|Position|Job|New):\s*(.+)$", subject, re.I)
    if m:
        return clean_text(m.group(1))
    
    # Look for job titles in the body text (Google job alerts have structured format)
    for line in (subject + "\n" + body_text).splitlines():
        line = line.strip()
        # Look for lines that contain job titles (usually have company names after)
        if any(k in line.lower() for k in ("engineer", "manager", "architect", "analyst", "lead", "director", "program manager")):
            # Clean the line and extract just the title part
            title = clean_text(line)
            # Remove common suffixes like "via LinkedIn", "via Google", etc.
            title = re.sub(r'\s+via\s+[A-Za-z\s]+$', '', title)
            if len(title) > 10 and len(title) < 200:
                return title
    
    return clean_text(subject)[:140]


def _extract_google_job_alerts(body_text: str, urls: List[str]) -> List[Dict]:
    """Extract multiple jobs from Google job alert emails."""
    jobs = []
    
    logging.debug("Processing Google job alert with %d URLs", len(urls))
    logging.debug("Body text preview: %s", body_text[:500])
    
    # If no URLs provided, try to extract Google redirect URLs from the body
    if not urls:
        google_redirect_pattern = r'https://notifications\.googleapis\.com/email/redirect\?[^"\s]+'
        google_urls = re.findall(google_redirect_pattern, body_text)
        logging.info("Found %d Google redirect URLs in email body", len(google_urls))
        
        # Follow the redirect URLs to get actual job URLs
        resolved_urls = []
        for i, redirect_url in enumerate(google_urls[:10]):  # Limit to first 10 URLs
            try:
                resolved_url = resolve_redirect(redirect_url)
                resolved_urls.append(resolved_url)
                logging.debug("Resolved redirect %d: %s -> %s", i+1, redirect_url[:50] + "...", resolved_url)
            except Exception as e:
                logging.warning("Failed to resolve redirect %d: %s", i+1, e)
                # Use the redirect URL as fallback
                resolved_urls.append(redirect_url)
        
        urls = resolved_urls
        logging.info("Resolved %d URLs from Google redirects", len(urls))
    
    # Split the body into lines and look for job patterns
    lines = body_text.split('\n')
    logging.debug("Processing %d lines", len(lines))
    
    # Look for specific patterns in Google job alerts
    # Google job alerts have a specific structure with job titles followed by company names
    
    # First, let's try to find job titles by looking for specific patterns
    job_titles = []
    companies = []
    
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
            
        line_lower = line.lower()
        
        # Look for job titles (more specific patterns)
        # Skip the subject line and other non-job title lines
        if (any(keyword in line_lower for keyword in ["technical program manager", "program manager", "engineering program manager", "staff technical program manager", "senior technical program manager"]) and
            not line_lower.startswith('"technical program manager" in') and  # Skip subject line
            not line_lower.startswith('united states') and  # Skip location lines
            not line_lower.startswith('via ') and  # Skip source lines
            not line_lower.startswith('time icon') and  # Skip icon lines
            not line_lower.startswith('work icon') and  # Skip icon lines
            not line_lower.startswith('logo') and  # Skip logo lines
                          not (len(line.strip()) == 1 and line.strip().lower() in ['j', 'g', 'f', 'a'])):  # Skip actual single letter lines only
            
            # Clean the line
            title = clean_text(line)
            title = re.sub(r'\s+via\s+[A-Za-z\s]+$', '', title)
            
            if len(title) > 10 and len(title) < 200:
                job_titles.append((i, title))
                logging.debug("Found job title: %s", title)
        
        # Look for company names (usually shorter lines without job keywords)
        elif (len(line) < 100 and 
              not any(keyword in line_lower for keyword in ["engineer", "manager", "architect", "analyst", "lead", "director", "program manager", "via"]) and
              not line_lower.startswith('time icon') and  # Skip icon lines
              not line_lower.startswith('work icon') and  # Skip icon lines
              not line_lower.startswith('logo') and  # Skip logo lines
              not (len(line.strip()) == 1 and line.strip().lower() in ['j', 'g', 'f', 'a']) and  # Skip actual single letter lines only
              not line_lower.startswith('via ') and  # Skip source lines
              not line_lower.startswith('aug ') and  # Skip date lines
              not line_lower.startswith('full-time') and  # Skip job type lines
              not line_lower.startswith('part-time') and  # Skip job type lines
              # Don't exclude location keywords from company names - companies can have locations in their names
              not line_lower.startswith('"technical program manager" in') and  # Skip subject line
              not line_lower.startswith('united states') and  # Skip standalone location lines
              not line_lower.startswith('new york, ny') and  # Skip standalone location lines
              not line_lower.startswith('springdale, ar') and  # Skip standalone location lines
              not line_lower.startswith('mill valley, ca') and  # Skip standalone location lines
              not line_lower.startswith('hillsboro, or') and  # Skip standalone location lines
              not line_lower.startswith('maple heights-lake desire, wa') and  # Skip standalone location lines
              not line_lower.startswith('livingston, nj') and  # Skip standalone location lines
              not line_lower.startswith('mountain view, ca') and  # Skip standalone location lines
              not line_lower.startswith('cupertino, ca')):  # Skip standalone location lines
            
            company = clean_text(line)
            if len(company) > 2 and len(company) < 100:  # Increased max length for longer company names
                companies.append((i, company))
                logging.debug("Found company: %s", company)
    
    logging.debug("Found %d job titles and %d companies", len(job_titles), len(companies))
    
    # Now match job titles with companies and URLs
    for i, (title_line, title) in enumerate(job_titles):
        # Find the closest company name (look for companies that come after the job title)
        company = "Unknown Company"
        for comp_line, comp_name in companies:
            if comp_line > title_line and comp_line <= title_line + 5:  # Company should come after title, within 5 lines
                company = comp_name
                break
        
        # If no company found after title, look for companies before the title
        if company == "Unknown Company":
            for comp_line, comp_name in companies:
                if comp_line < title_line and comp_line >= title_line - 3:  # Company before title, within 3 lines
                    company = comp_name
                    break
        
        # Get a URL for this job
        job_url = ""
        if i < len(urls):
            job_url = urls[i]
        elif urls:
            job_url = urls[0]  # Use first available URL
        else:
            # If no URLs available, create a placeholder URL
            job_url = f"https://google.com/jobs/placeholder/{i+1}"
            logging.info("No URLs available, using placeholder URL for job: %s", title)
        
        jobs.append({
            "company_name": company,
            "title": title,
            "location": "",  # We'll extract this separately if needed
            "url": job_url,
        })
        logging.debug("Extracted job: %s at %s", title, company)
    
    logging.info("Extracted %d jobs from Google job alert", len(jobs))
    return jobs


def _guess_location(text: str) -> str:
    m = re.search(r"\b(Remote|Hybrid|On[- ]site|New York|Seattle|SF|San Francisco|Austin|Redmond|Chicago|Boston|London)\b", text, re.I)
    return m.group(0) if m else ""


# -----------------------------------------------------------------------------
# JD EXTRACTION (Selenium + BS4)
# -----------------------------------------------------------------------------

def resolve_redirect(url: str) -> str:
    if not url:
        return url
    try:
        # LinkedIn login redirect → extract the actual target job URL
        if "linkedin.com/uas/login" in url and "session_redirect=" in url:
            from urllib.parse import urlparse, parse_qs, unquote
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            target = qs.get("session_redirect", [None])[0]
            if target:
                return unquote(target)
        if "google.com/url?q=" in url:
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(url).query).get("q", [None])[0]
            if q:
                return q
        r = requests.head(url, allow_redirects=True, timeout=10)
        return r.url or url
    except Exception:
        return url


def build_driver(profile_path: Optional[str], headless: bool) -> webdriver.Firefox:
    opts = FFOptions()
    opts.headless = headless

    # Prefer using an existing Firefox profile (e.g., "jobs") for higher site compatibility
    if profile_path and os.path.isdir(profile_path):
        try:
            # Modern Selenium: pass the profile directory via Firefox args
            opts.add_argument("-profile")
            opts.add_argument(profile_path)
            logging.info("Using Firefox profile at %s", profile_path)
        except Exception as e:
            logging.warning("Failed to attach Firefox profile at %s: %s", profile_path, e)

    service = FFService(GeckoDriverManager().install())
    driver = webdriver.Firefox(service=service, options=opts)
    driver.set_page_load_timeout(TIMEOUT_PAGE_LOAD)
    return driver


def wait_for_captcha(driver: webdriver.Firefox, max_wait: int = CAPTCHA_WAIT) -> None:
    try:
        html = (driver.page_source or "").lower()
        # More specific CAPTCHA detection - avoid false positives on LinkedIn
        captcha_indicators = [
            "captcha", 
            "unusual traffic", 
            "are you a robot",
            "verify you're human",
            "security check",
            "cloudflare"
        ]
        if any(k in html for k in captcha_indicators) and not "linkedin.com" in (driver.current_url or ""):
            logging.info("Possible CAPTCHA detected; pausing for %ds", max_wait)
            time.sleep(max_wait)
    except Exception:
        pass


def batch_scrape(driver: webdriver.Firefox, urls: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for u in urls:
        try:
            out[u] = scrape_job_page(driver, u)
            # Basic throttling to reduce CAPTCHA risk on LinkedIn
            if "linkedin.com" in (u or ""): 
                time.sleep(1.0)
        except Exception as e:
            logging.warning("Failed to scrape %s: %s", u, e)
            out[u] = ""
    return out


def scrape_job_page(driver: webdriver.Firefox, url: str) -> str:
    driver.get(url)
    wait_for_captcha(driver)

    # For LinkedIn, wait a bit longer for the page to fully load
    if "linkedin.com" in url:
        time.sleep(2)
    
    click_show_more(driver)
    
    # Wait a bit more after clicking show more for LinkedIn
    if "linkedin.com" in url:
        time.sleep(2)
    
    # Check if job is closed on LinkedIn AFTER page interaction
    if "linkedin.com" in url:
        try:
            # Check multiple possible selectors for closed jobs
            closed_selectors = [
                ".artdeco-inline-feedback__message",
                ".jobs-unified-top-card__subtitle",
                ".jobs-unified-top-card__job-insight",
                ".jobs-box__message",
                ".jobs-description__content"
            ]
            
            for selector in closed_selectors:
                closed_elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for elem in closed_elements:
                    elem_text = elem.text.lower()
                    if any(phrase in elem_text for phrase in [
                        "no longer accepting applications",
                        "this position is no longer accepting applications",
                        "job closed",
                        "position closed",
                        "applications closed"
                    ]):
                        logging.info("LinkedIn job is closed: %s (found in %s)", url, selector)
                        return "JOB_CLOSED: No longer accepting applications"
        except Exception as e:
            logging.debug("Error checking job status: %s", e)
    
    # Try traditional selectors first
    text = extract_description(driver)
    if text and len(text) > 200:
        return text

    # Fallback: Extract all text and use AI to identify job description
    soup = BeautifulSoup(driver.page_source, "html.parser")
    
    # Try specific selectors first
    for css in BS4_DESCRIPTION_CSS:
        node = soup.select_one(css)
        if node:
            txt = clean_text(node.get_text(" "))
            if txt and len(txt) > 200:
                logging.debug("Found description with BS4 selector: %s", css)
                return txt
    
    # LinkedIn-specific selector
    if "linkedin.com" in url:
        linkedin_desc = soup.select_one("div.mt4:nth-child(2)")
        if linkedin_desc:
            txt = clean_text(linkedin_desc.get_text(" "))
            if txt and len(txt) > 200:
                logging.debug("Found LinkedIn description with div.mt4:nth-child(2)")
                return txt
    
    # Final fallback: Extract all text and use AI to identify job description
    body_text = clean_text(soup.get_text(" "))
    if body_text and len(body_text) > 500:
        # Check if the full page text contains closure indicators
        if any(phrase in body_text.lower() for phrase in [
            "no longer accepting applications",
            "this position is no longer accepting applications",
            "job closed",
            "position closed",
            "applications closed"
        ]):
            logging.info("LinkedIn job is closed (found in page text): %s", url)
            return "JOB_CLOSED: No longer accepting applications"
        
        logging.debug("Using AI to extract job description from full page text, length: %d", len(body_text))
        return extract_jd_with_ai(body_text, url)
    
    return ""


def click_show_more(driver: webdriver.Firefox) -> None:
    for by, sel in SHOW_MORE_SELECTORS:
        try:
            elems = driver.find_elements(by, sel)
            for el in elems[:2]:  # Reduced from 4 to 2 to avoid unnecessary clicks
                try:
                    # Check if element text contains "show more", "see more", "read more" to avoid clicking company names
                    element_text = el.text.lower()
                    if not any(keyword in element_text for keyword in ["show more", "see more", "read more", "more"]):
                        continue
                    
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                    time.sleep(0.5)
                    if el.is_displayed() and el.is_enabled():
                        el.click()
                        time.sleep(1.0)
                        logging.debug("Clicked show more button: %s", sel)
                        break  # Stop after first successful click
                except Exception as e:
                    logging.debug("Failed to click show more: %s", e)
                    continue
        except Exception as e:
            logging.debug("Show more selector failed: %s - %s", sel, e)
            continue


def extract_description(driver: webdriver.Firefox) -> str:
    for by, sel in DESCRIPTION_SELECTORS:
        try:
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((by, sel)))
            for el in driver.find_elements(by, sel):
                text = clean_text(el.text)
                if text and len(text) > 100:
                    logging.debug("Found job description with selector: %s", sel)
                    return text
        except Exception as e:
            logging.debug("Description selector failed: %s - %s", sel, e)
            continue
    
    # Fallback: try to get any text content from the page
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        text = clean_text(body.text)
        if text and len(text) > 200:
            logging.debug("Using fallback body text extraction")
            return text
    except Exception as e:
        logging.debug("Fallback extraction failed: %s", e)
    
    return ""


# -----------------------------------------------------------------------------
# AI SCORING (Gemini) & ENRICHMENT (Serper)
# -----------------------------------------------------------------------------

def ai_analyze(model: GenerativeModel, jd_text: str, resume_text: str) -> Dict:
    prompt = (
        "You are a hiring analyst. Compare the job description to the resume and return ONLY valid JSON with these keys: "
        "{\\n  'match_score': int 0-100,\\n  'match_summary': str,\\n  'experience_level': str one of ['Intern','Junior','Mid','Senior','Staff','Principal','Director','Executive'],\\n  'salary_estimate': str,\\n  'technical_skills': list[str],\\n  'resume_tips': list[str]\\n}. Be concise in 'match_summary'."
    )

    try:
        resp = model.generate_content(
            [
                {"role": "user", "parts": [{"text": prompt}]},
                {"role": "user", "parts": [{"text": f"JOB DESCRIPTION:\n{jd_text[:14000]}"}]},
                {"role": "user", "parts": [{"text": f"RESUME:\n{resume_text[:12000]}"}]},
            ],
            generation_config={"response_mime_type": "application/json"},
        )
        raw = resp.text or "{}"
        data = json.loads(raw)
        data.setdefault("match_score", 0)
        data.setdefault("match_summary", "")
        data.setdefault("experience_level", "")
        data.setdefault("salary_estimate", "")
        data.setdefault("technical_skills", [])
        data.setdefault("resume_tips", [])
        if isinstance(data.get("technical_skills"), str):
            data["technical_skills"] = [s.strip() for s in data["technical_skills"].split(",") if s.strip()]
        if isinstance(data.get("resume_tips"), str):
            data["resume_tips"] = [s.strip() for s in data["resume_tips"].split("|") if s.strip()]
        return data
    except Exception as e:
        logging.error("Gemini analysis failed: %s\n%s", e, traceback.format_exc())
        return {
            "match_score": 0,
            "match_summary": "",
            "experience_level": "",
            "salary_estimate": "",
            "technical_skills": [],
            "resume_tips": ["Tailor bullet points to the JD keywords."],
        }


def extract_jd_with_ai(full_page_text: str, url: str) -> str:
    """Use AI to extract job description from full page text."""
    try:
        # Create a simple prompt to extract job description
        prompt = f"""
        Extract ONLY the job description from this webpage text. 
        Remove navigation, headers, footers, ads, and other non-job-related content.
        Return only the actual job description, requirements, and responsibilities.
        
        Webpage text:
        {full_page_text[:8000]}  # Limit to 8k chars to keep costs low
        
        Job description:
        """
        
        # Use a smaller, cheaper model for this task
        model = GenerativeModel("gemini-1.5-flash")  # Cheaper than gemini-2.5-pro
        
        response = model.generate_content(prompt)
        extracted_text = response.text.strip()
        
        if extracted_text and len(extracted_text) > 100:
            logging.info("Successfully extracted job description using AI from %s", url)
            return extracted_text[:6000]  # Limit output to keep costs low
        else:
            logging.warning("AI extraction returned empty or too short text for %s", url)
            return ""
            
    except Exception as e:
        logging.warning("AI extraction failed for %s: %s", url, e)
        return ""


def enrich_job_with_api(job: Dict) -> str:
    if not SERPER_API_KEY:
        return ""
    q = f"{job.get('title','')} {job.get('company_name','')} job description"
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": q, "num": 5},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        for item in data.get("organic", [])[:3]:
            link = item.get("link")
            if not link:
                continue
            try:
                html = requests.get(link, timeout=15).text
                soup = BeautifulSoup(html, "html.parser")
                blocks = [s for css in BS4_DESCRIPTION_CSS for s in soup.select(css)]
                text = "\n\n".join([clean_text(b.get_text(" ")) for b in blocks if clean_text(b.get_text(" "))])
                if not text:
                    text = clean_text(soup.get_text(" "))
                if len(text) > 200:
                    return text[:12000]
            except Exception:
                continue
    except Exception as e:
        logging.warning("Serper enrichment failed: %s", e)
    return ""


# -----------------------------------------------------------------------------
# PIPELINE PHASES
# -----------------------------------------------------------------------------

def recover_pending_jobs(sheet) -> List[Dict]:
    """Return only jobs from Google Sheet that need processing (Pending or JD Retry Needed)."""
    logging.info("Recovering only pending jobs (no Gmail discovery)")
    ws_map = {ws.title: ws for ws in sheet.worksheets()}
    pending: List[Dict] = []
    for cfg in JOB_CONFIG:
        ws = ws_map.get(cfg["sheet"])  # type: ignore[index]
        if not ws:
            continue
        recs = ws.get_all_records()
        for idx, rec in enumerate(recs, start=2):
            if rec.get("status", "") in ("Pending", "Pending Analysis", "JD Retry Needed"):
                pending.append({
                    "company_name": rec.get("company_name", ""),
                    "title": rec.get("title", ""),
                    "location": rec.get("location", ""),
                    "url": rec.get("apply_link", ""),
                    "ws": ws,
                    "row": idx,
                })
    logging.info("Recovered %d pending jobs from sheet", len(pending))
    return pending


def phase_discovery(model, supabase, gmail, sheet) -> List[Dict]:
    logging.info("=== Phase: Discovery ===")
    processed = get_processed(supabase)
    ws_map = {ws.title: ws for ws in sheet.worksheets()}

    # Recover Pending rows
    pending: List[Dict] = []
    for cfg in JOB_CONFIG:
        ws = ws_map.get(cfg["sheet"])  # type: ignore[index]
        if not ws:
            continue
        recs = ws.get_all_records()
        for idx, rec in enumerate(recs, start=2):
            if rec.get("status", "") in ("Pending", "Pending Analysis"):
                pending.append({
                    "company_name": rec.get("company_name", ""),
                    "title": rec.get("title", ""),
                    "location": rec.get("location", ""),
                    "url": rec.get("apply_link", ""),
                    "ws": ws,
                    "row": idx,
                })
    if pending:
        logging.info("Recovered %d pending jobs", len(pending))

    # Gmail discovery
    discovered = discover_jobs(gmail)
    logging.info("Checking %d discovered jobs against %d existing jobs in Supabase", len(discovered), len(processed))
    
    email_jobs = []
    duplicates = 0
    for j in discovered:
        job_key = (clean_text(j["company_name"]).lower(), clean_text(j["title"]).lower())
        if job_key not in processed:
            email_jobs.append(j)
        else:
            duplicates += 1
            logging.debug("Duplicate found: %s - %s", j["company_name"], j["title"])
    
    if duplicates > 0:
        logging.info("Filtered out %d duplicate jobs", duplicates)

    # Pre-assign worksheet + row and log new email jobs to sheet (real-time)
    for job in email_jobs:
        cfg = next((c for c in JOB_CONFIG if any(k in job["title"].lower() for k in c["keywords"])), None)
        if not cfg:
            logging.info("Skipping job with title '%s' — no matching JOB_CONFIG keywords", job.get("title", ""))
            continue
        ws = ws_map.get(cfg["sheet"])  # type: ignore[index]
        if not ws:
            logging.warning("Worksheet '%s' not found for job '%s'", cfg["sheet"], job.get("title", ""))
            continue
        row = len(ws.col_values(1)) + 1
        job.update({"ws": ws, "row": row})
        vals = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            job.get("company_name", ""),
            job.get("title", ""),
            job.get("location", ""),
            job.get("url", ""),
            "Pending",
        ]
        if safe_update_sheet(ws, f"A{row}:F{row}", [vals]):  # realtime add
            logging.info("Added new job to sheet '%s' row %d: %s — %s", ws.title, row, job.get("company_name", ""), job.get("title", ""))

    return pending + email_jobs


def phase_jd_extraction(model, supabase, gmail, sheet, jobs: List[Dict], *, profile_path: Optional[str], headless: bool) -> None:
    logging.info("=== Phase: JD Extraction ===")
    if not jobs:
        logging.info("No jobs to extract.")
        return

    # Normalize URLs
    for job in jobs:
        if job.get("url"):
            job["url"] = resolve_redirect(job["url"])  # type: ignore[index]

    # Group by rough source for logging only
    groups: Dict[str, List[str]] = defaultdict(list)
    for job in jobs:
        url = (job.get("url") or "").lower()
        if "linkedin.com" in url:
            groups["LinkedIn"].append(job.get("url", ""))
        elif "microsoft.com" in url:
            groups["Microsoft"].append(job.get("url", ""))
        else:
            groups["Other"].append(job.get("url", ""))

    driver = build_driver(profile_path or DEFAULT_PROFILE, headless)
    wait_for_captcha(driver)

    try:
        for src, urls in groups.items():
            logging.info("Scraping %s jobs (%d)", src, len(urls))
            raw_map = batch_scrape(driver, urls)
            for job in jobs:
                if job.get("url") not in urls:
                    continue
                ws, r = job.get("ws"), job.get("row")
                if not ws or not r:
                    logging.warning("Job missing ws/row for %s, skipping JD write.", job.get("url", ""))
                    continue

                # Check current status to determine retry count
                current_status = ""
                try:
                    status_cell = ws.acell(f"F{r}").value
                    current_status = status_cell or ""
                except Exception:
                    current_status = ""

                raw = clean_text(raw_map.get(job.get("url", ""), ""))
                
                # Handle closed jobs
                if raw and raw.startswith("JOB_CLOSED:"):
                    safe_update_sheet(ws, f"F{r}", [["Job Closed"]])
                    logging.info("Marked job as closed: %s", job.get("url", ""))
                    continue
                
                if not raw:
                    # Try enrichment fallback once before marking for retry
                    enriched = enrich_job_with_api(job)
                    if enriched:
                        raw = clean_text(enriched)
                    if not raw:
                        logging.warning("No JD scraped for %s", job.get("url", ""))
                        
                        # Check retry count and update status accordingly
                        if current_status == "JD Retry Needed":
                            safe_update_sheet(ws, f"F{r}", [["JD Extraction Failed"]])  # Mark as failed after 2nd attempt
                            logging.error("JD extraction failed after retry for %s", job.get("url", ""))
                        else:
                            safe_update_sheet(ws, f"F{r}", [["JD Retry Needed"]])  # First retry
                        continue

                job["description"] = raw
                safe_update_sheet(ws, f"M{r}", [[raw[:3000]]])  # realtime JD write
                safe_update_sheet(ws, f"F{r}", [["JD Extracted"]])       # realtime status

                db_min = {
                    "date_found": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "company_name": job.get("company_name", ""),
                    "title": job.get("title", ""),
                    "location": job.get("location", ""),
                    "apply_link": job.get("url", ""),
                    "status": "JD Extracted",
                    "description": raw[:3000],
                }
                # Check if job already exists to avoid duplicates
                try:
                    existing = supabase.table("jobs").select("id").eq("apply_link", job.get("url", "")).execute()
                    if not existing.data:
                        safe_insert_supabase(supabase.table("jobs"), db_min)
                        logging.info("Inserted new job to Supabase: %s", job.get("url", ""))
                    else:
                        logging.info("Job already exists in Supabase: %s", job.get("url", ""))
                except Exception as e:
                    logging.warning("Failed to check/insert job in Supabase: %s", e)
    finally:
        driver.quit()


def phase_scoring(model, supabase, gmail, sheet, jobs: List[Dict]) -> None:
    logging.info("=== Phase: Scoring ===")
    if not jobs:
        logging.info("No jobs passed into scoring.")
        return

    resumes: Dict[str, str] = {}
    for cfg in JOB_CONFIG:
        path = Path(cfg["resume"])  # type: ignore[index]
        resumes[cfg["resume"]] = path.read_text(encoding="utf-8") if path.exists() else ""

    for job in jobs:
        desc = job.get("description") or enrich_job_with_api(job)
        if not desc:
            continue

        cfg = next((c for c in JOB_CONFIG if any(k in (job.get("title", "").lower()) for k in c["keywords"])), None)
        if not cfg:
            continue

        ws, r = job.get("ws"), job.get("row")
        if not ws or not r:
            logging.warning("Job missing ws/row for %s, skipping score write.", job.get("url", ""))
            continue

        resume = resumes.get(cfg["resume"], "")

        try:
            ai_res = ai_analyze(model, desc, resume)
            length = len(desc.strip())
            status = (
                "Analyzed - Short JD" if length < 100 else
                ("Analyzed - High Match" if ai_res.get("match_score", 0) >= MATCH_SCORE_THRESHOLD else "Analyzed - Low Match")
            )

            # realtime updates
            safe_update_sheet(ws, f"F{r}", [[status]])
            safe_update_sheet(ws, f"G{r}:M{r}", [[
                ai_res.get("match_score", ""),
                ai_res.get("match_summary", ""),
                ai_res.get("experience_level", ""),
                ai_res.get("salary_estimate", ""),
                ", ".join(ai_res.get("technical_skills", [])),
                " | ".join(ai_res.get("resume_tips", [])),
                desc[:3000],
            ]])

            db_row = {
                **dict(zip(SHEET_HEADERS[:6], [
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    job.get("company_name", ""),
                    job.get("title", ""),
                    job.get("location", ""),
                    job.get("url", ""),
                    status,
                ])),
                **{
                    "match_score": ai_res.get("match_score"),
                    "match_summary": ai_res.get("match_summary"),
                    "experience_level": ai_res.get("experience_level"),
                    "salary_estimate": ai_res.get("salary_estimate"),
                    "technical_skills": ", ".join(ai_res.get("technical_skills", [])),
                    "resume_tips": " | ".join(ai_res.get("resume_tips", [])),
                    "description": desc[:3000],
                }
            }
            safe_upsert_supabase(supabase.table("jobs"), db_row, on_conflict="apply_link")
        except Exception as e:
            logging.error("AI analysis failed for %s: %s\n%s", job.get("url", ""), e, traceback.format_exc())
            safe_update_sheet(ws, f"F{r}", [["Processing Failed"]])


def phase_recovery(model, supabase, gmail, sheet) -> None:
    logging.info("=== Phase: Recovery ===")
    ws_map = {ws.title: ws for ws in sheet.worksheets()}
    for cfg in JOB_CONFIG:
        ws = ws_map.get(cfg["sheet"])  # type: ignore[index]
        if not ws:
            continue
        recs = ws.get_all_records()
        for idx, rec in enumerate(recs, start=2):
            status = rec.get("status", "")
            if status in ("Processing Failed", "Reprocessing Failed"):
                title = rec.get("title", "")
                company = rec.get("company_name", "")
                logging.info("Reprocessing row %d: %s - %s", idx, company, title)
                desc = enrich_job_with_api({"title": title, "company_name": company})
                if not desc:
                    safe_update_sheet(ws, f"F{idx}", [["Reprocessing Failed"]])
                    continue
                resume_path = Path(cfg["resume"])  # type: ignore[index]
                resume = resume_path.read_text(encoding="utf-8") if resume_path.exists() else ""
                try:
                    ai_res = ai_analyze(model, desc, resume)
                    score = ai_res.get("match_score", 0)
                    new_status = "Analyzed - High Match" if score >= MATCH_SCORE_THRESHOLD else "Analyzed - Low Match"
                    safe_update_sheet(ws, f"F{idx}", [[new_status]])
                    safe_update_sheet(ws, f"G{idx}:M{idx}", [[
                        score,
                        ai_res.get("match_summary", ""),
                        ai_res.get("experience_level", ""),
                        ai_res.get("salary_estimate", ""),
                        ", ".join(ai_res.get("technical_skills", [])),
                        " | ".join(ai_res.get("resume_tips", [])),
                        desc[:3000],
                    ]])
                    db = {
                        **{h: rec.get(h, "") for h in SHEET_HEADERS[:6]},
                        **{
                            "match_score": score,
                            "match_summary": ai_res.get("match_summary", ""),
                            "experience_level": ai_res.get("experience_level", ""),
                            "salary_estimate": ai_res.get("salary_estimate", ""),
                            "technical_skills": ", ".join(ai_res.get("technical_skills", [])),
                            "resume_tips": " | ".join(ai_res.get("resume_tips", [])),
                            "description": desc[:3000],
                        }
                    }
                    safe_upsert_supabase(supabase.table("jobs"), db, on_conflict="apply_link")
                except Exception as e:
                    logging.error("Reprocessing failed row %d: %s", idx, e)
                    safe_update_sheet(ws, f"F{idx}", [["Reprocessing Failed"]])


# -----------------------------------------------------------------------------
# PHASE RUNNER (modular, default-all)
# -----------------------------------------------------------------------------

PHASE_FUNCS = {
    "discovery": lambda ctx: phase_discovery(*ctx),
    "jd_extraction": lambda ctx, **kw: phase_jd_extraction(*ctx, **kw),
    "scoring": lambda ctx: phase_scoring(*ctx, ctx[-1]),  # dummy; we pass jobs explicitly below
    "recovery": lambda ctx: phase_recovery(*ctx),
}


def main():
    parser = argparse.ArgumentParser(description="Unified Job Search Automation")
    parser.add_argument("--phase", choices=["discovery", "jd_extraction", "scoring", "recovery"], help="Phase to run. If omitted, all phases run in order.")
    parser.add_argument("--headless", dest="headless", action="store_true", help="Run browser in headless mode (default)")
    parser.add_argument("--no-headless", dest="headless", action="store_false", help="Run browser with UI")
    parser.add_argument("--profile", dest="profile", default=DEFAULT_PROFILE, help="Path to Firefox profile directory for JD extraction (e.g., jobs profile)")
    parser.set_defaults(headless=HEADLESS_DEFAULT)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(sys.stdout)])

    # Shared service clients
    model, supabase, gmail, sheet = setup_services(args.headless)

    # Full context tuple used across phases
    ctx = (model, supabase, gmail, sheet)

    if args.phase == "discovery":
        jobs = phase_discovery(*ctx)
        return

    if args.phase == "jd_extraction":
        jobs = recover_pending_jobs(sheet)
        if not jobs:
            logging.info("No pending jobs found — nothing to extract.")
            return
        phase_jd_extraction(*ctx, jobs=jobs, profile_path=args.profile, headless=args.headless)
        return

    if args.phase == "scoring":
        jobs = phase_discovery(*ctx)
        phase_jd_extraction(*ctx, jobs=jobs, profile_path=args.profile, headless=args.headless)
        phase_scoring(*ctx, jobs=jobs)
        return

    if args.phase == "recovery":
        phase_recovery(*ctx)
        return

    # Default: run all phases in order
    jobs = phase_discovery(*ctx)
    phase_jd_extraction(*ctx, jobs=jobs, profile_path=args.profile, headless=args.headless)
    phase_scoring(*ctx, jobs=jobs)
    phase_recovery(*ctx)


if __name__ == "__main__":
    main()
