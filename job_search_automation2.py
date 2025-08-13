#!/usr/bin/env python3
"""
Unified Job Search Automation
1. Recover Pending: pick up any “Pending” rows from Google Sheets
2. Discovery: Fetch new job alerts from Gmail and log base records
3. Enrichment: Batch-scrape JDs, writing raw descriptions immediately
4. AI Analysis: Gemini match scoring & structured insights (with “Short JD” status)
5. Post-Analysis Recovery: retry rows marked Processing Failed
6. Logging: Safe updates to Google Sheets & Supabase
"""

# -----------------------------------------------------------------------------
# SECTION 0: IMPORTS & CONFIGURATION
# -----------------------------------------------------------------------------
import os, re, json, time, logging, traceback, base64
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests, gspread, vertexai
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as SA_Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from supabase import create_client
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FFOptions
from selenium.webdriver.firefox.service import Service as FFService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from vertexai.generative_models import GenerativeModel
from webdriver_manager.firefox import GeckoDriverManager
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile


load_dotenv()

SERPER_API_KEY      = os.getenv('SERPER_API_KEY')
SUPABASE_URL        = os.getenv('SUPABASE_URL')
SUPABASE_KEY        = os.getenv('SUPABASE_KEY')
GOOGLE_SHEET_URL    = os.getenv('GOOGLE_SHEET_URL')
GCP_PROJECT         = os.getenv('GOOGLE_CLOUD_PROJECT')
GCP_LOCATION        = os.getenv('GOOGLE_CLOUD_LOCATION')

MATCH_SCORE_THRESHOLD = 80
TIMEOUT_PAGE_LOAD     = 30
CAPTCHA_WAIT          = 30
DEFAULT_PROFILE       = os.getenv('FIREFOX_PROFILE_PATH', None)

JOB_CONFIG = [
    {"keywords": ["technical program manager","tpm"],      "resume": "resume_tpm.txt",        "sheet": "PM_Architect_Jobs"},
    {"keywords": ["product manager","pmt"],                "resume": "resume_pmt.txt",        "sheet": "PM_Architect_Jobs"},
    {"keywords": ["architect","cloud engineer"],           "resume": "resume_architect.txt",  "sheet": "PM_Architect_Jobs"},
    {"keywords": ["analyst","business analyst","quantitative"], "resume":"resume_analyst.txt", "sheet":"Analyst_Jobs"}
]

SHEET_HEADERS = [
    "date_found","company_name","title","location","apply_link","status",
    "match_score","match_summary","experience_level","salary_estimate",
    "technical_skills","resume_tips","description"
]

SHOW_MORE_SELECTORS = [
    (By.CLASS_NAME, "nNzjpf-cS4Vcb-PvZLI-vK2bNd-fmcmS"),
    (By.XPATH, "//button[contains(text(),'Show more')]"),
    (By.XPATH, "//button[contains(text(),'See more')]"),
    (By.XPATH, "//a[contains(text(),'Show full description')]")
]

SECTION_SELECTORS = [
    ("Qualifications",   "h4.yVFmQd:nth-child(3)", "ul.zqeyHd:nth-child(4)"),
    ("Responsibilities", "h4.yVFmQd:nth-child(5)", "ul.zqeyHd:nth-child(6)")
]

DESCRIPTION_SELECTORS = [
    (By.CLASS_NAME, "hkXmid"),
    (By.CLASS_NAME, "us2QZb"),
    (By.CSS_SELECTOR, "[data-test-description]"),
    (By.CLASS_NAME, "description__text"),
    (By.ID, "jobDescriptionText"),
    (By.CLASS_NAME, "jobsearch-jobDescriptionText"),
    (By.CSS_SELECTOR, ".job-description"),
    (By.CSS_SELECTOR, ".description")
]

SENDERS = {
    "Google":    ["jobs-noreply@google.com","notify-noreply@google.com"],
    "LinkedIn":  ["jobs-noreply@linkedin.com","jobalerts-noreply@linkedin.com"],
    "Microsoft": ["jobalerts@microsoft.com"]
}

# -----------------------------------------------------------------------------
# SECTION 1: SERVICE SETUP & UTILITIES
# -----------------------------------------------------------------------------
def setup_services():
    vertexai.init(project=GCP_PROJECT, location=GCP_LOCATION)
    model    = GenerativeModel('gemini-2.5-pro')
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
            creds = flow.run_local_server(open_browser=False)
        with open('token.json','w') as t:
            t.write(creds.to_json())

    gmail = build('gmail','v1',credentials=creds)

    sa_creds = SA_Credentials.from_service_account_file(
        'credentials.json',
        scopes=["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    )
    sheet = gspread.authorize(sa_creds).open_by_url(GOOGLE_SHEET_URL)

    return model, supabase, gmail, sheet

def clean_text(s):
    return re.sub(r'\s+',' ',s.strip()) if isinstance(s,str) else ""

def get_processed(supabase):
    try:
        rows = supabase.table('jobs').select('company_name','title').execute().data
        return {(clean_text(r['company_name']).lower(), clean_text(r['title']).lower()) for r in rows}
    except:
        return set()

def safe_update_sheet(ws, cell_range, vals, retries=3, backoff=2):
    for i in range(retries):
        try:
            ws.update(values=vals, range_name=cell_range)
            return True
        except Exception as e:
            logging.warning(f"Sheet update {cell_range} attempt {i+1}: {e}")
            time.sleep(backoff**i)
    logging.error(f"Permanent sheet failure {cell_range}")
    return False

def safe_insert_supabase(table, rec, retries=3, backoff=2):
    for i in range(retries):
        try:
            table.insert(rec).execute()
            return True
        except Exception as e:
            logging.warning(f"Supabase insert attempt {i+1}: {e}")
            time.sleep(backoff**i)
    logging.error("Permanent Supabase insertion failure")
    return False

# -----------------------------------------------------------------------------
# SECTION 2: EMAIL PARSERS & DISCOVERY
# -----------------------------------------------------------------------------
def parse_google_email(soup):
    jobs=[]
    for c in soup.select('td[style*="border-bottom: 1px solid #E5E5E5"]'):
        try:
            t = c.find('span', style=lambda s: s and 'font-size: 16px' in s)
            title = t.text.strip()
            comp = t.find_next_sibling('div').text.strip()
            loc_el = t.find_next_sibling('div').find('span')
            loc = loc_el.text.strip() if loc_el else "N/A"
            url = c.find('a', href=True)['href']
            jobs.append({"title":title,"company_name":comp,"location":loc,"url":url})
        except:
            continue
    return jobs

def parse_linkedin_email(soup):
    jobs=[]
    for card in soup.find_all('td', {'data-test-id':'job-card'}):
        try:
            a = card.find('a', class_=lambda c: c and 'text-color-brand' in c)
            title = a.text.strip()
            url = a.find_parent('a')['href']
            info = card.find('p', class_=lambda c: c and 'text-system-gray-100' in c).text.split('·')
            comp,loc = (info[0].strip(),info[1].strip()) if len(info)>1 else (info.strip(),"N/A")
            jobs.append({"title":title,"company_name":comp,"location":loc,"url":url})
        except:
            continue
    return jobs

def parse_microsoft_email(soup):
    jobs=[]
    for tbl in soup.find_all('table', cellpadding="0", cellspacing="0", role="presentation"):
        try:
            a = tbl.find('a', style=lambda s: s and 'color: #5c1b86' in s)
            title = a.text.strip().replace('\n',' ')
            url = a['href']
            jobs.append({"title":title,"company_name":"Microsoft","location":"N/A","url":url})
        except:
            continue
    return jobs

def discover_jobs(gmail):
    clauses = [f'from:{s}' for subs in SENDERS.values() for s in subs]
    query = f"is:unread label:inbox ({' OR '.join(clauses)})"
    logging.info(f"Searching Gmail with query: {query}")
    resp = gmail.users().messages().list(userId='me', q=query).execute()
    msgs = resp.get('messages',[])
    if not msgs:
        logging.info("No unread job alert emails.")
        return []
    jobs = []
    for m in msgs:
        msg = gmail.users().messages().get(userId='me', id=m['id'], format='full').execute()
        data = ""
        payload = msg.get('payload',{})
        if payload.get('mimeType') == 'text/html':
            data = payload['body']['data']
        else:
            for p in payload.get('parts', []):
                if p.get('mimeType') == 'text/html':
                    data = p['body']['data']
                    break
        if not data:
            continue
        html = base64.urlsafe_b64decode(data.encode('ASCII')).decode('utf-8')
        soup = BeautifulSoup(html, 'html.parser')
        frm = next((h['value'] for h in msg['payload']['headers'] if h['name']=='From'), "").lower()
        if any(s in frm for s in SENDERS['LinkedIn']):
            jobs += parse_linkedin_email(soup)
        elif any(s in frm for s in SENDERS['Microsoft']):
            jobs += parse_microsoft_email(soup)
        else:
            jobs += parse_google_email(soup)
        gmail.users().messages().modify(userId='me', id=m['id'], body={'removeLabelIds':['UNREAD']}).execute()
    uniq = {(j['company_name'], j['title']): j for j in jobs}
    return list(uniq.values())


# -----------------------------------------------------------------------------
# SECTION 3: SCRAPER HELPERS (UPDATED build_driver)
# -----------------------------------------------------------------------------


def build_driver(profile_path, headless=False):
    """
    Launch Firefox using the specified profile directory via -profile & -no-remote flags.
    """
    options = FFOptions()
    # Point to your custom “Jobs” profile
    if profile_path and Path(profile_path).exists():
        options.add_argument(f"-profile")
        options.add_argument(profile_path)
        # Prevent profile lock conflicts
        options.add_argument("-no-remote")
    if headless:
        options.add_argument("--headless")

    driver = webdriver.Firefox(
        options=options,
        service=FFService(GeckoDriverManager().install())
    )
    driver.implicitly_wait(10)
    driver.set_page_load_timeout(TIMEOUT_PAGE_LOAD)
    return driver


def wait_for_captcha(driver):
    logging.info(f"Waiting {CAPTCHA_WAIT}s for CAPTCHA/login…")
    try:
        WebDriverWait(driver, CAPTCHA_WAIT).until(
            EC.presence_of_element_located((By.TAG_NAME,'body'))
        )
    except:
        pass
    logging.info("Proceeding…")

def expand_more(driver):
    for by,loc in SHOW_MORE_SELECTORS:
        try:
            btn = WebDriverWait(driver,3).until(EC.element_to_be_clickable((by,loc)))
            driver.execute_script("arguments[0].scrollIntoView();", btn)
            time.sleep(0.5)
            btn.click()
            time.sleep(1)
            break
        except:
            continue

def extract_sections(html):
    soup = BeautifulSoup(html, 'html.parser')
    for t in soup(['script','style','nav','header','footer']):
        t.decompose()
    secs = {}
    for name,h_sel,u_sel in SECTION_SELECTORS:
        h = soup.select_one(h_sel)
        u = soup.select_one(u_sel)
        if h and u:
            items = [li.get_text(strip=True) for li in u.find_all('li')]
            if items:
                secs[name] = items
    if secs:
        return secs
    parts = []
    for by,loc in DESCRIPTION_SELECTORS:
        try:
            elems = soup.select(loc) if by==By.CSS_SELECTOR else soup.find_all(class_=loc)
            for e in elems:
                txt = e.get_text('\n', strip=True)
                if len(txt) > 50:
                    parts.append(txt)
        except:
            pass
    return {'Description': parts} if parts else {}

def batch_scrape(driver, urls):
    orig = driver.current_window_handle
    driver.set_page_load_timeout(60)
    results = {}
    for url in urls:
        logging.info(f"Scraping {url}")
        driver.execute_script("window.open('');")
        new_tab = [h for h in driver.window_handles if h != orig][-1]
        driver.switch_to.window(new_tab)
        try:
            driver.get(url)
            WebDriverWait(driver,30).until(
                lambda d: "linkedin.com/comm/jobs/view" not in url or
                          "linkedin.com/comm/jobs/view" in d.current_url
            )
            WebDriverWait(driver, TIMEOUT_PAGE_LOAD).until(
                EC.presence_of_element_located((By.TAG_NAME,'body'))
            )
            expand_more(driver)
            sections = extract_sections(driver.page_source)
            raw_desc = "\n".join(sections.get("Description", []))
            results[url] = raw_desc
        except Exception as e:
            logging.error(f"Error scraping {url}: {e}")
            results[url] = ""
        finally:
            try:
                driver.close()
            except:
                logging.warning(f"Could not close tab {new_tab}")
            try:
                driver.switch_to.window(orig)
            except:
                logging.error(f"Could not switch back to original {orig}")
    return results

# -----------------------------------------------------------------------------
# SECTION 4: ENRICHMENT & AI ANALYSIS
# -----------------------------------------------------------------------------
def enrich_job_with_api(job):
    query = f'"{job["title"]}" at "{job["company_name"]}"'
    logging.info(f"Enriching via Serper API: {query}")
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={'X-API-KEY': SERPER_API_KEY, 'Content-Type': 'application/json'},
            data=json.dumps({"q": query, "gl": "us", "hl": "en"}),
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        desc = data.get("jobs", [{}])[0].get("description") \
             or (data.get("organic", [{}]).get("snippet") if data.get("organic") else "")
        return desc
    except Exception as e:
        logging.warning(f"Serper enrichment failed: {e}")
        return ""

def ai_analyze(model, desc, resume):
    prompt = f"""
Analyze the provided job description based on the resume.
Return a single JSON object with keys:
match_score, match_summary, experience_level,
salary_estimate, technical_skills, resume_tips.

RESUME: ---
{resume}
---
JOB DESCRIPTION: ---
{desc[:3000]}
---
"""
    resp = model.generate_content(prompt)
    text = resp.text.strip().lstrip("``````")
    return json.loads(text)

def reprocess_failed_jobs(sheet, model, supabase):
    logging.info("--- Reprocessing Failed Jobs ---")
    ws_map = {ws.title: ws for ws in sheet.worksheets()}
    for cfg in JOB_CONFIG:
        ws = ws_map.get(cfg['sheet'])
        if not ws:
            continue
        recs = ws.get_all_records()
        for idx, rec in enumerate(recs, start=2):
            status = rec.get("status","")
            if status in ("Processing Failed","Reprocessing Failed"):
                title = rec["title"]
                company = rec["company_name"]
                url = rec["apply_link"]
                logging.info(f"Reprocessing row {idx}: {company} - {title}")
                desc = enrich_job_with_api({"title":title,"company_name":company})
                if not desc:
                    safe_update_sheet(ws, f"F{idx}", [["Reprocessing Failed"]])
                    continue
                resume = Path(cfg['resume']).read_text() if Path(cfg['resume']).exists() else ""
                try:
                    ai_res = ai_analyze(model, desc, resume)
                    score = ai_res.get("match_score",0)
                    new_status = "Analyzed - High Match" if score >= MATCH_SCORE_THRESHOLD else "Analyzed - Low Match"
                    safe_update_sheet(ws, f"F{idx}", [[new_status]])
                    safe_update_sheet(ws, f"G{idx}:M{idx}", [[
                        score,
                        ai_res.get("match_summary",""),
                        ai_res.get("experience_level",""),
                        ai_res.get("salary_estimate",""),
                        ", ".join(ai_res.get("technical_skills",[])),
                        " | ".join(ai_res.get("resume_tips",[])),
                        desc[:3000]
                    ]])
                    db = {
                        **{h: rec[h] for h in SHEET_HEADERS[:6]},
                        **{
                            "match_score": score,
                            "match_summary": ai_res.get("match_summary",""),
                            "experience_level": ai_res.get("experience_level",""),
                            "salary_estimate": ai_res.get("salary_estimate",""),
                            "technical_skills": ", ".join(ai_res.get("technical_skills",[])),
                            "resume_tips": " | ".join(ai_res.get("resume_tips",[])),
                            "description": desc[:3000]
                        }
                    }
                    supabase.table('jobs').insert(db).execute()
                except Exception as e:
                    logging.error(f"Reprocessing failed row {idx}: {e}")
                    safe_update_sheet(ws, f"F{idx}", [["Reprocessing Failed"]])

# -----------------------------------------------------------------------------
# SECTION 5: MAIN WORKFLOW
# -----------------------------------------------------------------------------
def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    model, supabase, gmail, sheet = setup_services()
    processed = get_processed(supabase)

    # Stage 0.5: Recover Pending
    ws_map = {ws.title: ws for ws in sheet.worksheets()}
    pending = []
    for cfg in JOB_CONFIG:
        ws = ws_map.get(cfg['sheet'])
        if not ws:
            continue
        recs = ws.get_all_records()
        for idx, rec in enumerate(recs, start=2):
            if rec.get("status","") in ("Pending","Pending Analysis"):
                pending.append({
                    "company_name": rec["company_name"],
                    "title": rec["title"],
                    "location": rec["location"],
                    "url": rec["apply_link"],
                    "ws": ws,
                    "row": idx
                })
    if pending:
        logging.info(f"Recovered {len(pending)} pending jobs")

    # Stage 1: Discovery
    discovered = discover_jobs(gmail)
    email_jobs = [
        j for j in discovered
        if (clean_text(j['company_name']).lower(), clean_text(j['title']).lower()) not in processed
    ]

    # Combine pending + discovered
    new_jobs = pending + email_jobs
    if not new_jobs:
        logging.info("No jobs to process.")
        return

    # Preload resumes
    resumes = {}
    for cfg in JOB_CONFIG:
        path = Path(cfg['resume'])
        resumes[cfg['resume']] = path.read_text() if path.exists() else ""

    # Log email jobs
    for job in email_jobs:
        cfg = next((c for c in JOB_CONFIG if any(k in job['title'].lower() for k in c['keywords'])), None)
        if not cfg:
            continue
        ws = ws_map[cfg['sheet']]
        row = len(ws.col_values(1)) + 1
        job.update({"ws": ws, "row": row})
        vals = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            job['company_name'], job['title'], job['location'], job['url'], "Pending"
        ]
        safe_update_sheet(ws, f"A{row}:F{row}", [vals])

    # Stage 2: Batch scrape + real-time sheet & DB update
    groups = defaultdict(list)
    for job in new_jobs:
        url = job['url'].lower()
        if "linkedin.com" in url:
            groups["LinkedIn"].append(url)
        elif "microsoft.com" in url:
            groups["Microsoft"].append(url)
        else:
            groups["Google"].append(url)

    driver = build_driver(DEFAULT_PROFILE, headless=False)
    wait_for_captcha(driver)

    for src, urls in groups.items():
        logging.info(f"Scraping {src} jobs ({len(urls)})")
        raw_map = batch_scrape(driver, urls)
        for job in new_jobs:
            ws, r = job['ws'], job['row']
            raw = raw_map.get(job['url'], "")

            if raw.strip():
                job['description'] = raw
                safe_update_sheet(ws, f"M{r}", [[raw[:3000]]])
                status_after = "JD Extracted"
            else:
                status_after = "JD Extraction Failed"

            safe_update_sheet(ws, f"F{r}", [[status_after]])

            db_min = {
                "date_found": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "company_name": job.get("company_name",""),
                "title": job.get("title",""),
                "location": job.get("location",""),
                "apply_link": job['url'],
                "status": status_after,
                "description": (raw[:3000] if raw else "")
            }
            supabase.table("jobs").upsert(db_min, on_conflict="apply_link").execute()

    # Stage 3: AI analysis & logging
    for job in new_jobs:
        desc = job.get('description',"") or enrich_job_with_api(job)
        cfg = next((c for c in JOB_CONFIG if any(k in job['title'].lower() for k in c['keywords'])), None)
        if not cfg:
            continue
        ws, r = job['ws'], job['row']
        resume = resumes.get(cfg['resume'],"")
        try:
            ai_res = ai_analyze(model, desc, resume)
            length = len(desc.strip())
            if length < 100:
                status = "Analyzed - Short JD"
            else:
                score = ai_res.get("match_score",0)
                status = "Analyzed - High Match" if score>=MATCH_SCORE_THRESHOLD else "Analyzed - Low Match"

            safe_update_sheet(ws, f"F{r}", [[status]])
            safe_update_sheet(ws, f"G{r}:M{r}", [[
                ai_res.get("match_score",""),
                ai_res.get("match_summary",""),
                ai_res.get("experience_level",""),
                ai_res.get("salary_estimate",""),
                ", ".join(ai_res.get("technical_skills",[])),
                " | ".join(ai_res.get("resume_tips",[])),
                desc[:3000]
            ]])

            db_row = {
                **dict(zip(SHEET_HEADERS[:6], [
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    job['company_name'], job['title'], job['location'],
                    job['url'], status
                ])),
                **{
                    "match_score": ai_res.get("match_score"),
                    "match_summary": ai_res.get("match_summary"),
                    "experience_level": ai_res.get("experience_level"),
                    "salary_estimate": ai_res.get("salary_estimate"),
                    "technical_skills": ", ".join(ai_res.get("technical_skills", [])),
                    "resume_tips": " | ".join(ai_res.get("resume_tips", [])),
                    "description": desc[:3000]
                }
            }
            safe_insert_supabase(supabase.table("jobs"), db_row)

        except Exception as e:
            logging.error(f"AI analysis failed for {job['url']}: {e}\n{traceback.format_exc()}")
            safe_update_sheet(ws, f"F{r}", [["Processing Failed"]])

    # Stage 4: Post-analysis recovery
    reprocess_failed_jobs(sheet, model, supabase)

    driver.quit()
    logging.info("Browser closed, automation complete.")

if __name__ == '__main__':
    main()
