# JobPilot — AI Job Applicator

Search real jobs posted in the last 24 hours across all major platforms,
tailor your resume with AI, check your ATS score, and apply — all in one place.

---

## Setup (5 minutes)

### 1. Prerequisites
- Python 3.10 or higher
- VS Code (recommended)

### 2. Clone / open the project
Open this `jobpilot/` folder in VS Code.

### 3. Create a virtual environment
```bash
python -m venv venv

# Mac/Linux:
source venv/bin/activate

# Windows:
venv\Scripts\activate
```

### 4. Install dependencies
```bash
pip install -r requirements.txt
```

### 5. Add your Anthropic API key
Edit the `.env` file and replace the placeholder:
```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```
Get a key at: https://console.anthropic.com

### 6. Add your resumes
Copy your resume files (.docx, .pdf, or .txt) into the `resumes/` folder.

**Tip:** The AI works best with `.txt` or `.docx` files.
If you have PDF resumes, convert them to `.txt` first for best results.

### 7. Run the app
```bash
python app.py
```

### 8. Open in browser
Visit: **http://localhost:5000**

---

## How to use

1. **Select a job title** from the dropdown (Data Engineer, Software Engineer, etc.)
2. **Set your location** (default: Seattle / Bellevue, WA)
3. **Toggle platforms** on/off — all are on by default
4. **Select your resume** from the sidebar
5. **Click "Find jobs now"** — searches all platforms for jobs posted in the last 24h
6. **Click any job card** → right panel opens with 3 tabs:
   - **Job description** — read the full JD + apply directly without tailoring
   - **Tailor & Edit** — pick resume → AI tailors it → edit inline → check ATS score
   - **ATS Score** — see detailed score, matched/missing keywords, and apply
7. **Apply now** button opens the real job page in your browser
8. **Download** your tailored resume as .docx

---

## Project structure

```
jobpilot/
├── app.py              ← Flask backend (run this)
├── job_scraper.py      ← Scrapes Indeed, Dice, ZipRecruiter, LinkedIn, Amazon, Microsoft...
├── ai_engine.py        ← Claude API: ATS scoring, resume tailoring, line improvement
├── resume_reader.py    ← Reads .docx, .pdf, .txt resumes
├── requirements.txt    ← Python dependencies
├── .env                ← Your API key (never commit this)
├── resumes/            ← PUT YOUR RESUME FILES HERE
├── generated/          ← Tailored resumes saved here after download
├── logs/               ← Error logs
└── static/
    ├── index.html      ← Main UI
    ├── css/style.css   ← All styles
    └── js/app.js       ← All frontend logic
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| "ANTHROPIC_API_KEY not set" | Edit `.env` file and add your key |
| "No resumes found" | Add .docx or .txt files to `resumes/` folder |
| Jobs not loading | Some platforms block scrapers — try again or check console |
| Download fails | Make sure `python-docx` is installed: `pip install python-docx` |
| Port 5000 in use | Change port in `app.py`: `app.run(port=5001)` |

---

## Notes

- Job scraping works best with a real IP (not VPN) as some platforms detect datacenter IPs
- The app caches jobs in memory — click "Find jobs now" again to refresh
- Tailored resumes are saved to the `generated/` folder automatically
- All AI calls use Claude Sonnet for speed and quality
