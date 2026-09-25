# Kalshi Bot — Monitoring App

Mobile-first PWA dashboard for the Kalshi BTC paper trading bot.

```
~/workspace/kalshi-bot/app/
├── server.py          # FastAPI web server (port 8788)
├── settings.json      # Persisted settings
├── README.md          # This file
└── static/
    ├── index.html
    ├── style.css
    ├── script.js
    ├── manifest.webmanifest
    ├── sw.js           # Service worker (PWA)
    ├── icon.svg        # SVG app icon
    ├── icon-192.png
    ├── icon-512.png
    └── make_icons.py   # Regenerate PNG icons if needed
```

## Quick Start

```bash
python3.11 ~/workspace/kalshi-bot/app/server.py
```

Open **http://localhost:8788** in your browser. On Android, long-press the
address bar → "Add to Home Screen" to install as a PWA.

## Server Options

| Variable   | Default     |
|------------|-------------|
| `HOST`     | `0.0.0.0`  |
| `PORT`     | `8788`     |

Example:
```bash
PORT=8788 python3.11 ~/workspace/kalshi-bot/app/server.py
```

## How the Bot Consumes App Settings

The app writes user-configurable settings to **`settings.json`** (in the
`app/` directory). The bot's `runner.py` can read this file on each loop
iteration (or on startup) to adjust its behaviour.

### Settings schema

```json
{
  "mode": "paper",
  "position_size": 1,
  "conviction_floor": 1
}
```

| Field             | Type     | Meaning                                          |
|-------------------|----------|--------------------------------------------------|
| `mode`            | string   | `"paper"` = virtual balance (default, no real orders). `"live"` = real Kalshi orders (not yet wired). |
| `position_size`   | integer  | Number of contracts per trade (1–100).           |
| `conviction_floor`| integer  | Minimum Jev conviction (0–3) required to trade.  |
|                   |          | 0 = trade everything, 3 = only max-confidence.   |

### Reading from runner.py

```python
import json
import os

SETTINGS_FILE = os.path.expanduser("~/workspace/kalshi-bot/app/settings.json")

def load_app_settings() -> dict:
    defaults = {"mode": "paper", "position_size": 1, "conviction_floor": 1}
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        defaults.update({k: v for k, v in data.items() if k in defaults})
    except (OSError, json.JSONDecodeError):
        pass
    return defaults

# Inside your main loop:
settings = load_app_settings()
min_conviction = settings["conviction_floor"]  # skip trades below this
position_size = settings["position_size"]       # use this many contracts
mode = settings["mode"]                         # "paper" | "live"
```

Note: `settings.json` is written atomically by the web server. runner.py
should treat it as advisory — validate and clamp values before using them.

## API Endpoints

| Method | Path             | Purpose                     |
|--------|------------------|-----------------------------|
| GET    | `/`              | SPA frontend                |
| GET    | `/api/status`    | Paper balance, P&L, stats   |
| GET    | `/api/decisions` | Recent Jev decisions/trades |
| GET    | `/api/settings`  | Current app settings        |
| POST   | `/api/settings`  | Update settings (partial)   |
| GET    | `/api/health`    | Server health check         |

## Stopping the Server

Press **`Ctrl+C`** in the terminal where it's running, or kill with:

```bash
pkill -f "server.py"   # kills the server process
```

## Port Conflicts

If port 8788 is in use, run on a different port:
```bash
PORT=8789 python3.11 ~/workspace/kalshi-bot/app/server.py
```

Then open `http://localhost:8789`.