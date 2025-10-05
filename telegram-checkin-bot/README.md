# Telegram Check-In/Out Bot

### Files
- `bot.py` — main bot code.
- `requirements.txt` — dependencies.
- `.env.example` — copy to `.env` and fill your secrets.

### Local run
```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env with your token/id
python bot.py
```

### Render.com quick start
1) Create new **Web Service**.
2) Upload this ZIP as a repository or drag-and-drop files.
3) Set Start Command: `python bot.py`.
4) Add environment variables from your `.env` (or keep `.env` in repo — not recommended for secrets).
