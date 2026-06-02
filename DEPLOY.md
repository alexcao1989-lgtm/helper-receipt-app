# Helper Receipt App — Deploy Guide (Streamlit Cloud + Supabase)

This guide helps you deploy the app for **free** with persistent cloud data.

---

## Part A — Supabase (free cloud database)

### A1. Register

1. Open [https://supabase.com](https://supabase.com)
2. Click **Start your project** → sign in with **GitHub** (recommended)
3. Confirm your email if asked

### A2. Create a project

1. Click **New project**
2. Choose your **Organization** (or create one)
3. **Name**: e.g. `helper-receipt`
4. **Database password**: save it somewhere safe (for Supabase dashboard only)
5. **Region**: pick closest to Hong Kong (e.g. Singapore)
6. Click **Create new project** → wait ~2 minutes

### A3. Create tables (one-click SQL)

1. In the left menu: **SQL Editor**
2. Click **New query**
3. Open the file `supabase_schema.sql` in this project folder
4. Copy **all** SQL → paste into Supabase → click **Run**
5. You should see “Success”

### A4. Get URL and API key

1. Left menu: **Project Settings** (gear icon)
2. Click **API**
3. Copy these two values:

| Name | Where to find | Use in Streamlit |
|------|----------------|------------------|
| **SUPABASE_URL** | Project URL | `https://xxxxx.supabase.co` |
| **SUPABASE_KEY** | **service_role** → secret (click Reveal) | Server-side only — never share publicly |

> Use the **service_role** key for this app (full access, only stored in Streamlit Secrets).  
> Do **not** put the service_role key in frontend JavaScript.

---

## Part B — Local secrets (before GitHub)

### B1. Install dependencies

```powershell
cd "c:\03. Agent APP\01. Helper Receipt APP"
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### B2. Create `secrets.toml` (local only — not uploaded to GitHub)

```powershell
copy .streamlit\secrets.toml.example .streamlit\secrets.toml
```

Edit `.streamlit\secrets.toml` and fill in:

```toml
OPENROUTER_API_KEY = "sk-or-v1-你的密钥"
SUPABASE_URL = "https://你的项目.supabase.co"
SUPABASE_KEY = "你的 service_role 密钥"
EMPLOYER_PASSWORD = "123456"
APP_PASSWORD = ""
```

### B3. Test locally

```powershell
streamlit run app.py
```

Open `http://localhost:8501` — upload a receipt and check Supabase **Table Editor** for new rows.

---

## Part C — GitHub (private repository)

### C1. Install Git (if needed)

Download: [https://git-scm.com/download/win](https://git-scm.com/download/win)

### C2. Create a private repo on GitHub

1. Go to [https://github.com/new](https://github.com/new)
2. **Repository name**: `helper-receipt-app`
3. Select **Private**
4. Do **not** add README / .gitignore (we already have files)
5. Click **Create repository**

### C3. Upload your code (first time)

In PowerShell:

```powershell
cd "c:\03. Agent APP\01. Helper Receipt APP"

git init
git add app.py database.py requirements.txt supabase_schema.sql .gitignore .streamlit/secrets.toml.example DEPLOY.md
git commit -m "Initial commit: Helper Receipt App with Supabase"

git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/helper-receipt-app.git
git push -u origin main
```

Replace `YOUR_USERNAME` with your GitHub username.  
GitHub will ask you to log in (browser or Personal Access Token).

> **Important:** `secrets.toml` is in `.gitignore` and will **not** be pushed. That is correct.

### C4. Later updates

```powershell
git add .
git commit -m "Describe your change"
git push
```

---

## Part D — Streamlit Community Cloud

### D1. Sign in

1. Open [https://share.streamlit.io](https://share.streamlit.io)
2. Click **Sign in** → use the **same GitHub account**

### D2. New app

1. Click **Create app**
2. **Repository**: `YOUR_USERNAME/helper-receipt-app`
3. **Branch**: `main`
4. **Main file path**: `app.py`
5. Click **Deploy** (advanced settings below if deploy fails)

### D3. Secrets (required)

1. Open your app on Streamlit Cloud
2. Click **⋮** (menu) → **Settings** → **Secrets**
3. Paste (use your real values):

```toml
OPENROUTER_API_KEY = "sk-or-v1-..."
SUPABASE_URL = "https://xxxxx.supabase.co"
SUPABASE_KEY = "eyJhbG...service_role..."
EMPLOYER_PASSWORD = "123456"
APP_PASSWORD = ""
```

4. Click **Save** → **Reboot app**

### D4. Share the link

After reboot, copy the public URL (e.g. `https://your-app.streamlit.app`) and send it to your helper’s phone.

---

## Checklist

- [ ] Supabase tables created (`supabase_schema.sql` run successfully)
- [ ] Local test works with `.streamlit/secrets.toml`
- [ ] GitHub repo is **Private**, no `secrets.toml` committed
- [ ] Streamlit Cloud Secrets filled (all 4–5 keys)
- [ ] App rebooted after saving secrets

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Missing SUPABASE_URL` | Add secrets in Streamlit Cloud and reboot |
| `relation "expenses" does not exist` | Run `supabase_schema.sql` in Supabase SQL Editor |
| OpenRouter error | Check `OPENROUTER_API_KEY` in Secrets |
| Wrong employer password | Update `EMPLOYER_PASSWORD` in Secrets and reboot |
| Data empty on cloud | Confirm you are not still using old local `expenses.db` — cloud uses Supabase only |

---

## Files to upload to GitHub

| File | Required |
|------|----------|
| `app.py` | Yes |
| `database.py` | Yes |
| `requirements.txt` | Yes |
| `supabase_schema.sql` | Recommended |
| `.gitignore` | Yes |
| `.streamlit/secrets.toml.example` | Recommended |
| `DEPLOY.md` | Optional |
| `venv/`, `expenses.db`, `secrets.toml` | **Never upload** |
