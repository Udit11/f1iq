# F1IQ — Formula 1 Intelligence Platform

A lightweight live F1 dashboard that streams real-time timing, telemetry, pit strategy, and predictions into one page.

## ✅ What it is

- **Live race dashboard** (timing, gaps, pit stops)
- **Historical session playback** (FastF1 telemetry + laps)
- **Strategy & probability analysis** (based on session state)
- **Single-process app**: backend + frontend served from one FastAPI server

## ▶️ Getting started

### 1) Clone the repo
```powershell
git clone <your-repo-url>
cd F1
```

### 2) Create & activate a virtualenv (recommended)
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 3) Install dependencies
```powershell
pip install -r requirements.txt
```

### 4) Run the server
```powershell
uvicorn backend.main:app --reload --port 8000
```

### 5) Open the dashboard
Open **http://localhost:8000** in your browser.

---

## 📌 Where to look next

- Backend logic: `backend/`
- Frontend UI: `frontend/templates/index.html`
- WebSocket + API client: `frontend/static/js/app.js`

---

## 🔧 Notes

- The app uses open data sources (OpenF1, FastF1, Ergast) and runs entirely locally.
- No external build step is required; just start the FastAPI server.
