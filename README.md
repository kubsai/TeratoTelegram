# TeraBox Manager - Production Ready Telegram Bot + Web Dashboard

A robust, secure, and feature-rich TeraBox automation system with multiple cookie collection methods, Telegram bot integration, encrypted storage, and dual-server fallback support.

## Features

### Core Features
- Send any TeraBox share link → Automatically saves to your account
- Multiple cookie collection methods (Bookmarklet, Userscript, Manual, Experimental Login)
- Smart Userscript with **Python → PHP Fallback** support
- Telegram bot with commands (`/start`, `/login`, `/setcookie`, `/setfolder`)
- Experimental QR Code login via Telegram (mobile-friendly)
- Web Dashboard with status monitoring
- Encrypted cookie storage (when MySQL is enabled)
- Domain health check system (self-healing)
- Rate limiting and security features

### Advanced Features
- Embedded TeraBox login window in dashboard
- Bookmarklet for one-click cookie extraction
- Userscript that automatically detects and sends `ndus` cookie
- API endpoint with API key protection
- Health check endpoint (`/health`)
- MySQL support with automatic table creation
- JSON file fallback for cookie storage

## Project Structure

```
.
├── main.py                 # Main application (Flask + Telegram Bot)
├── terabox_helper.py       # TeraBox transfer logic
├── database.py             # MySQL helper functions
├── requirements.txt
├── .env.example
├── README.md
├── ResearchData.md
└── render.yaml
```

## Environment Variables

Create a `.env` file with the following variables:

```env
# Required
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TERABOX_NDUS_COOKIE=your_ndus_cookie_value

# Optional but Recommended
TARGET_FOLDER_PATH=/Telegram_Uploads
FLASK_SECRET_KEY=your_long_random_secret_key
API_KEY=your_secure_api_key_for_scripts

# Database (Optional)
USE_MYSQL=false
MYSQL_HOST=localhost
MYSQL_USER=root
MYSQL_PASSWORD=your_password
MYSQL_DATABASE=terabox_manager
MYSQL_PORT=3306

# Fallback Login
FALLBACK_USERNAME=admin
FALLBACK_PASSWORD=admin123
```

## Installation

### Local Development

```bash
git clone <your-repo>
cd terabox-manager
pip install -r requirements.txt
```

Create `.env` file with your credentials.

### Running the Project

```bash
python main.py
```

The application will start on `http://0.0.0.0:8080` (or the port specified in environment).

## Deployment

### Render.com (Recommended)

1. Push code to GitHub (keep repository **private**)
2. Create new Web Service on Render
3. Connect your repository
4. Use these settings:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main.py`
5. Add all environment variables

### Replit

1. Upload all files
2. Run in Shell:
   ```bash
   pip install -r requirements.txt
   ```
3. Click **Run**

### Other Platforms

The application works on any platform that supports Python 3.8+.

## Usage

### Web Dashboard

1. Open `http://localhost:8080`
2. Login with:
   - Username: `admin`
   - Password: `admin`
3. Go to **Cookie Collector**
4. Use any of the three methods:
   - **Bookmarklet** (Recommended)
   - **Userscript** (Automatic with fallback)
   - **Manual Paste**

### Telegram Bot

- `/start` — Welcome message
- `/login <email> <password>` — Experimental auto-login
- `/setcookie <value>` — Update ndus cookie
- `/setfolder <path>` — Change target folder
- Send any TeraBox link to transfer it

### API Endpoint

**URL**: `POST /api/save-cookie`

**Body**:
```json
{
  "ndus": "your_ndus_value",
  "api_key": "your_api_key"
}
```

## Security Notes

- Keep your repository **private**
- Never commit real `.env` files
- The `ndus` cookie is sensitive — treat it like a password
- API Key protection is enabled on `/api/save-cookie`
- Rate limiting is applied on login endpoints

## License

This project is for personal/educational use. Use at your own risk.

---

**Note**: The experimental QR Code and mobile emulation login methods are provided as options but may be unstable due to TeraBox's frequent changes. The recommended methods are Bookmarklet and Userscript.