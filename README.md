# 🧑💻 Unified Job Search Automation (`jobbot.py`)

Automate **job discovery**, **job description scraping**, **data enrichment**, and **AI-powered resume matching** — all in one Python application.  
Integrates **Gmail job alerts**, **Selenium scraping**, **Google Sheets**, **Supabase**, and **Gemini AI** to help track and analyze job opportunities efficiently.

***

## 🚀 Features

- **Email job alert parsing** from Gmail (Google alerts, LinkedIn, Microsoft, …)
- **Robust job description scraping** with Selenium & BeautifulSoup
- **Automatic logging & status tracking** in Google Sheets
- **Duplicate prevention** via Supabase
- **AI-powered job matching & scoring** using your resumes with Google Gemini
- **Multi-phase CLI** — run only what you need (`discovery`, `jd_extraction`, `scoring`, `recovery`)
- Configurable per-role **keywords** for targeting the right jobs
- **Safe write** — Sheets and Supabase are updated live, row-by-row

***

## 📂 Project Structure

```
job-automation/
├── jobbot.py              # Main automation script (all phases inside)
├── requirements.txt       # Python dependencies
├── sample.env             # Example environment variables (no secrets)
├── README.md              # Documentation
├── .gitignore             # Ignore secrets, creds, etc.
├── resumes/               # Resume text files (per JOB_CONFIG role)
└── examples/              # Sample HTML/job alert test files
```

***

## ⚙️ Setup Instructions

### 1️⃣ Prerequisites
- Python **3.10+**
- **pip** installed
- **Google Cloud Project** with Gmail API & Sheets API enabled
- A **Google Sheet** with tabs matching the `JOB_CONFIG` roles in `jobbot.py`
- A **Supabase** account with a `jobs` table  
- Firefox installed (for Selenium) + Auto-managed **GeckoDriver**
- Optional: Serper.dev API key for JD enrichment

***

### 2️⃣ Install dependencies

```bash
pip install -r requirements.txt
```

***

### 3️⃣ Environment variables

Create `.env` in the project root:

```env
SERPER_API_KEY=your_serper_key
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_service_key
GOOGLE_SHEET_URL=your_google_sheet_url
GOOGLE_CLOUD_PROJECT=your_gcp_project
GOOGLE_CLOUD_LOCATION=us-central1
FIREFOX_PROFILE_PATH=/path/to/Firefox/Profiles/jobs
MATCH_SCORE_THRESHOLD=80
GMAIL_SINCE_DAYS=14
```

💡 Paths differ for macOS/Linux vs Windows — adjust accordingly.

***

### 4️⃣ Local files (DO NOT commit)

Add these to `.gitignore`:

```
.env
token.json                # Gmail OAuth cache
client_secrets.json       # Gmail OAuth client
credentials.json          # Google Sheets service account
*.txt                     # Local resumes
```

***

## ▶️ Running phases

The script supports modular **phases** that can run independently.

### **1. Discovery**
- Reads **pending** rows in Google Sheet
- Checks **Gmail job alerts** for new jobs
- Writes new jobs directly into the correct sheet tab, status `"Pending"`  
**Command:**
```bash
python jobbot.py --phase discovery
```

***

### **2. Job Description Extraction**
- **Now independent** — *only* runs for rows with status `Pending`, `Pending Analysis`, or `JD Retry Needed`
- Opens each job link with **Selenium + Firefox profile**
- Scrapes & cleans JD text (with AI assistance)
- Updates sheet status to `"JD Extracted"`

**Command:**
```bash
python jobbot.py --phase jd_extraction
```

***

### **3. Scoring / AI Enhancement**
- Reads **only jobs** with status `JD Extracted`
- **Skips** rows already `"Analyzed - High Match"`, `"Low Match"`, `"Short JD"`, `"Applied"`, `"Skipped"`, `"Job Closed"`
- Compares JD with correct resume file for that role
- Writes match score, summary, skills, tips to the sheet

**Command:**
```bash
python jobbot.py --phase scoring
```

***

### **4. Recovery**
- Picks up rows with `"Processing Failed"` / `"Reprocessing Failed"`
- Attempts enrichment (Serper API) to re-score

**Command:**
```bash
python jobbot.py --phase recovery
```

***

### **Run everything in sequence**
```bash
python jobbot.py
```
Order: discovery → jd_extraction → scoring → recovery

***

## 🛠️ Tech stack

- **Python** (`requests`, `BeautifulSoup4`, `selenium`, `gspread`, `supabase-py`)
- **Google APIs**: Gmail, Sheets, Drive
- **Supabase** (Postgres backend)
- **Firefox + GeckoDriver** (Selenium automation)
- **Google Vertex AI** (`gemini-2.5-pro` & cheaper `gemini-1.5-flash`)
- **Serper API** (JD enrichment fallback)

***

## 🛡 Security & Safety

- **Do NOT commit** `.env`, credentials, or resumes to GitHub
- OAuth tokens (`token.json`) are personal
- Supabase keys must be service keys, stored in `.env`

***

## ✨ Notes for Contributors
- PRs welcome — keep each **phase** idempotent
- Add new role keywords in `JOB_CONFIG`
- Unit test Gmail parsing logic with `/examples` HTML files

***

If you want, I can also add:
- CLI examples in a **"Quick Start"** section
- A **flow diagram** showing data movement between Gmail, Selenium, Sheets, Supabase
- Badges for Python version, license, and Google API use  

***
