# 🧑💻 Unified Job Search Automation

Automate job discovery, scraping, enrichment, and AI-based resume matching — all in one Python project.  
This tool integrates **Gmail job alerts**, **web scraping**, **Google Sheets**, **Supabase**, and **Gemini AI** to help you track and analyze job postings efficiently.

***

## 🚀 Features
- **Email job alert parsing** from Google, LinkedIn, Microsoft.
- **Job description scraping** with Selenium & BeautifulSoup.
- **Data logging** to Google Sheets and Supabase.
- **AI-powered analysis** of jobs vs. your resume using Google's Gemini model.
- **Multi-source recovery** for pending/failed jobs.
- **Configurable keywords** for targeted job hunting.

***

## 📂 Project Structure
```
job-search-automation/
├── main.py                # Main automation script
├── requirements.txt       # Python packages
├── sample.env             # Example environment variables (no secrets)
├── README.md              # Project documentation
├── .gitignore             # Ignore sensitive files (.env, creds, etc.)
├── examples/              # Mock resumes & emails for demo purposes
└── docs/                  # Optional screenshots/docs
```

***

## ⚙️ Setup Instructions

### 1️⃣ Prerequisites
- **Python** 3.9+
- **pip** (Python package manager)
- A **Google Cloud project** with Gmail & Sheets API enabled
- **Supabase account** with a `jobs` table
- API key for **Serper** or similar search API
- **Firefox** installed (for Selenium WebDriver)

***

### 2️⃣ Install Dependencies
```bash
pip install -r requirements.txt
```

***

### 3️⃣ Environment Variables
Create a `.env` file in your project root (this file **must not** be committed to GitHub).

```bash
SERPER_API_KEY=your_serper_api_key
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_service_key
GOOGLE_SHEET_URL=your_google_sheet_url
GOOGLE_CLOUD_PROJECT=your_gcp_project_id
GOOGLE_CLOUD_LOCATION=us-central1
FIREFOX_PROFILE_PATH=/path/to/firefox/profile  # Windows or Mac/Linux path
```

💡 Works on **Windows, macOS, and Linux** — just adjust the file path style.

We’ve included a `sample.env` with placeholders for reference.

***

### 4️⃣ Local Files (Do Not Commit)
These will be created/used locally and must be added to `.gitignore`:
```
.env
token.json
client_secrets.json
credentials.json
*.txt        # if you store resumes locally
```

***

### 5️⃣ Running the Script
```bash
python main.py
```

***

## 🧪 Demo Mode (Safe for Public Sharing)
If you want to share a **GitHub-safe demo** without your real data:
1. Replace resume files with dummy text files in `/examples`.
2. Use fake job alert HTML files for email parsing tests.
3. Set environment variables to dummy values.
4. Disable API calls by mocking functions if needed.

***

## 🛡️ Security Notes
- **Never** commit `.env`, credential JSON files, or real resumes.
- The `.gitignore` is preconfigured to keep secrets safe.
- The script **loads credentials from your `.env` file** using `python-dotenv`.

***

## 🛠️ Tech Stack
- **Python** (requests, BeautifulSoup, Selenium, gspread, supabase-py)
- **Google APIs** (Gmail, Sheets, Drive)
- **Supabase** (PostgreSQL backend)
- **Firefox + GeckoDriver** (Selenium automation)
- **Gemini AI** (via `vertexai` SDK)
- **Serper API** (job data enrichment)

***

## 📜 License
This project is licensed under the MIT License — see `LICENSE` for details.

***

## ✨ Author & Contributions
Created as a **portfolio project** to demonstrate automation, API integration, and AI skills.  
Contributions and issue reports are welcome — fork the repo and submit PRs.

***

If you’d like, I can also **add badges, a screenshot/GIF section, and a "Quick Demo" CLI command** to make it pop more for recruiters.  
Want me to prepare that polished version for you? That would make the README stand out even more.
