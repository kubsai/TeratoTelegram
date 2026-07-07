import os
import logging
import threading
import asyncio
import time
import platform
import requests
from datetime import datetime, timezone
from flask import (
    Flask,
    render_template_string,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    flash,
)
from dotenv import load_dotenv
from terabox_helper import (
    list_terabox_folders,
    check_terabox_connectivity,
    update_cookie,
    TERABOX_BASE_URL,
    get_qr_login_data,
    poll_qr_login,
    upload_file_to_terabox,
    get_current_proxy,
    set_active_proxy,
    rotate_proxy,
    fetch_fresh_proxies,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "supersecretkey123")

current_target_folder = os.getenv("TARGET_FOLDER_PATH", "/Telegram_Uploads")
logs = []
cookie_history = []

# ===================== ADMIN SESSION CONFIG =====================

ADMIN_SESSION_TIMEOUT = 15 * 60  # 15 minutes in seconds
admin_sessions = {}  # {user_id: {"login_time": float, "expires_at": float, "chat_id": int}}
admin_audit_log = []  # [{"timestamp": str, "user_id": int, "action": str, "result": str}]
MAX_AUDIT_LOG = 500
SERVER_START_TIME = time.time()
DB_STATUS = {"connected": False, "error": None, "driver": None, "checked_at": None}  # Startup DB check result


def add_log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logs.append(f"[{timestamp}] {msg}")
    if len(logs) > 100:
        logs.pop(0)


def add_audit_log(user_id: int, action: str, result: str = "OK"):
    """Log an admin action to the audit trail."""
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id,
        "action": action,
        "result": result,
    }
    admin_audit_log.append(entry)
    if len(admin_audit_log) > MAX_AUDIT_LOG:
        admin_audit_log.pop(0)
    logger.info(f"[ADMIN AUDIT] User {user_id}: {action} → {result}")


def is_admin_session_valid(user_id: int) -> bool:
    """Check if a user has a valid (non-expired) admin session."""
    if user_id not in admin_sessions:
        return False
    session_data = admin_sessions[user_id]
    if time.time() > session_data["expires_at"]:
        # Session expired — clean up
        del admin_sessions[user_id]
        add_audit_log(user_id, "SESSION_EXPIRED", "Auto-expired")
        return False
    return True


def mask_sensitive(value: str, show_chars: int = 4) -> str:
    """Mask a sensitive value, showing only first and last N chars."""
    if not value:
        return "(not set)"
    if len(value) <= show_chars * 2:
        return "*" * len(value)
    return value[:show_chars] + "•" * (len(value) - show_chars * 2) + value[-show_chars:]


# ===================== DATABASE HELPER =====================


def _try_mysql_connector(config):
    """Try connecting with mysql-connector-python."""
    import mysql.connector
    conn = mysql.connector.connect(**config)
    return conn, "mysql-connector-python"


def _try_pymysql(config):
    """Try connecting with PyMySQL (fallback driver)."""
    import pymysql
    conn = pymysql.connect(
        host=config["host"],
        user=config["user"],
        password=config["password"],
        database=config["database"],
        port=config["port"],
        connect_timeout=config.get("connection_timeout", 10),
        cursorclass=pymysql.cursors.Cursor,
    )
    return conn, "pymysql"


def get_db_connection(return_error=False):
    """Get a MySQL connection. Tries mysql-connector-python first, then PyMySQL.
    
    Args:
        return_error: If True, returns (conn, error_string) tuple instead of just conn.
                      This lets callers show the actual error to the user.
    """
    use_mysql = os.getenv("USE_MYSQL", "false").lower() == "true"
    if not use_mysql:
        if return_error:
            return None, "MySQL is disabled (USE_MYSQL=false)"
        return None

    config = {
        "host": os.getenv("MYSQL_HOST", ""),
        "user": os.getenv("MYSQL_USER", ""),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "database": os.getenv("MYSQL_DATABASE", ""),
        "port": int(os.getenv("MYSQL_PORT", 3306)),
        "connection_timeout": 10,
    }

    # Check for missing config
    missing = [k for k in ["host", "user", "password", "database"] if not config.get(k)]
    if missing:
        err = f"Missing MySQL config: {', '.join(missing)}"
        logger.error(err)
        if return_error:
            return None, err
        return None

    errors = []

    # Try driver 1: mysql-connector-python
    try:
        conn, driver = _try_mysql_connector(config)
        if return_error:
            return conn, None
        return conn
    except ImportError:
        errors.append("mysql-connector-python: not installed")
    except Exception as e:
        errors.append(f"mysql-connector-python: {str(e)}")

    # Try driver 2: PyMySQL
    try:
        conn, driver = _try_pymysql(config)
        if return_error:
            return conn, None
        return conn
    except ImportError:
        errors.append("pymysql: not installed")
    except Exception as e:
        errors.append(f"pymysql: {str(e)}")

    # Both failed
    full_error = " | ".join(errors)
    logger.error(f"All MySQL drivers failed: {full_error}")
    if return_error:
        return None, full_error
    return None


def save_ndus_to_db(ndus_value):
    use_mysql = os.getenv("USE_MYSQL", "false").lower() == "true"
    if not use_mysql:
        return False
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_cookies (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ndus VARCHAR(255),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("INSERT INTO user_cookies (ndus) VALUES (%s)", (ndus_value,))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"MySQL save error: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return False


def safe_db_query(query: str, limit: int = 10):
    """Execute a read-only database query safely. Returns (result_dict, None) or (None, error_str)."""
    import re
    # Security: only allow SELECT and SHOW
    query_upper = query.strip().upper()
    if not (query_upper.startswith("SELECT") or query_upper.startswith("SHOW")):
        return None, "Only SELECT and SHOW queries are allowed."

    # Block dangerous keywords (word-boundary match to avoid false positives like 'updated_at')
    dangerous = ["DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "TRUNCATE", "CREATE", "GRANT", "REVOKE"]
    for kw in dangerous:
        if re.search(r'\b' + kw + r'\b', query_upper):
            return None, f"Blocked: '{kw}' is not allowed in read-only mode."

    conn, db_err = get_db_connection(return_error=True)
    if not conn:
        return None, f"Database not connected: {db_err}"

    try:
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(limit)
        total_rows = cursor.rowcount if cursor.rowcount >= 0 else len(rows)
        cursor.close()
        conn.close()
        return {"columns": columns, "rows": rows, "total": total_rows}, None
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return None, str(e)


def check_db_connection():
    """Test database connectivity and return detailed diagnostics."""
    global DB_STATUS
    use_mysql = os.getenv("USE_MYSQL", "false").lower() == "true"
    if not use_mysql:
        DB_STATUS = {"connected": False, "error": "MySQL disabled (USE_MYSQL=false)", "driver": None, "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        return DB_STATUS

    host = os.getenv("MYSQL_HOST", "(not set)")
    db_name = os.getenv("MYSQL_DATABASE", "(not set)")
    port = os.getenv("MYSQL_PORT", "3306")
    user = os.getenv("MYSQL_USER", "(not set)")

    conn, err = get_db_connection(return_error=True)
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT VERSION()")
            version = cursor.fetchone()[0]
            cursor.execute("SHOW TABLES")
            tables = [row[0] for row in cursor.fetchall()]
            cursor.close()
            conn.close()
            DB_STATUS = {
                "connected": True,
                "error": None,
                "driver": "mysql",
                "version": version,
                "host": host,
                "port": port,
                "database": db_name,
                "user": user,
                "tables": tables,
                "table_count": len(tables),
                "checked_at": checked_at,
            }
        except Exception as e:
            DB_STATUS = {"connected": False, "error": f"Connected but query failed: {e}", "driver": None, "checked_at": checked_at}
    else:
        DB_STATUS = {
            "connected": False,
            "error": err or "Unknown connection error",
            "driver": None,
            "host": host,
            "port": port,
            "database": db_name,
            "user": user,
            "checked_at": checked_at,
        }

    return DB_STATUS


# ===================== HTML TEMPLATES =====================

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>TeraBox Manager - Login</title>
    <style>
        body { font-family: system-ui; background: #0f172a; color: #e2e8f0; margin: 0;
               display: flex; justify-content: center; align-items: center; min-height: 100vh; }
        .card { background: #1e2937; padding: 40px; border-radius: 12px; width: 340px; }
        h2 { color: #60a5fa; margin-top: 0; text-align: center; }
        input { width: 100%; padding: 10px; margin: 8px 0 16px; box-sizing: border-box;
                background: #0f172a; border: 1px solid #475569; border-radius: 6px; color: #e2e8f0; }
        button { width: 100%; padding: 12px; background: #3b82f6; color: white;
                 border: none; border-radius: 8px; cursor: pointer; font-size: 15px; }
        button:hover { background: #2563eb; }
        .error { color: #f87171; background: #450a0a; padding: 10px; border-radius: 6px;
                 margin-bottom: 16px; text-align: center; }
    </style>
</head>
<body>
<div class="card">
    <h2>🚀 TeraBox Manager</h2>
    {% if error %}
        <div class="error">{{ error }}</div>
    {% endif %}
    <form method="post">
        <label>Username</label>
        <input type="text" name="username" placeholder="Username" required>
        <label>Password</label>
        <input type="password" name="password" placeholder="Password" required>
        <button type="submit">Login</button>
    </form>
</div>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>TeraBox Manager</title>
    <style>
        body { font-family: system-ui; background: #0f172a; color: #e2e8f0; margin: 0; padding: 20px; }
        .container { max-width: 1100px; margin: auto; }
        .card { background: #1e2937; padding: 24px; border-radius: 12px; margin-bottom: 20px; }
        h1 { color: #60a5fa; }
        .btn { background: #3b82f6; color: white; padding: 10px 18px; border-radius: 8px; text-decoration: none; display: inline-block; }
        .success { color: #4ade80; background: #052e16; padding: 10px; border-radius: 6px; margin-bottom: 16px; }
        .error { color: #f87171; background: #450a0a; padding: 10px; border-radius: 6px; margin-bottom: 16px; }
        .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }
        .cookie-value { font-family: monospace; background: #0f172a; padding: 8px; border-radius: 6px; word-break: break-all; }
        input[type=text] { background: #0f172a; border: 1px solid #475569; color: #e2e8f0;
                           padding: 8px; border-radius: 6px; }
        button { background: #3b82f6; color: white; padding: 10px 16px; border: none;
                 border-radius: 6px; cursor: pointer; }
        textarea { background: #0f172a; color: #e2e8f0; border: 1px solid #475569;
                   border-radius: 8px; padding: 12px; }
    </style>
</head>
<body>
<div class="container">
    <h1>🚀 TeraBox Manager</h1>

    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="{{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}

    <div class="card">
        <h3>System Status</h3>
        <div class="status-grid">
            <div><strong>Target Folder:</strong><br>{{ current_folder }}</div>
            <div><strong>Database:</strong><br>
                {% if mysql_connected %}
                    <span style="color:#4ade80;">✅ MySQL Connected</span>
                {% elif mysql_error %}
                    <span style="color:#f87171;">❌ Error: {{ mysql_error[:80] }}</span>
                {% else %}
                    <span style="color:#94a3b8;">⚫ Disabled (Fallback Mode)</span>
                {% endif %}
            </div>
            <div><strong>Telegram Bot:</strong><br>{{ 'Enabled' if telegram_enabled else 'Disabled' }}</div>
            <div><strong>API Key Status:</strong><br>{{ 'Configured' if api_key_set else 'Not Set' }}</div>
            <div><strong>Current ndus Cookie:</strong><br>
                <div class="cookie-value">{{ current_ndus[:30] + '...' if current_ndus else 'Not Set' }}</div>
            </div>
        </div>
    </div>

    <div class="card">
        <h3>Quick Actions</h3>
        <a href="/cookie-collector" class="btn">🍪 Open Advanced Cookie Collector</a>
        &nbsp;
        <a href="/logout" class="btn" style="background:#64748b;">Logout</a>
    </div>

    <div class="card">
        <h3>Update Target Folder</h3>
        <form method="post" action="/update-folder">
            <input type="text" name="folder" value="{{ current_folder }}" style="width:60%" required>
            &nbsp;<button type="submit">Update Folder</button>
        </form>
    </div>

    <div class="card">
        <h3>Recent Activity Logs</h3>
        <textarea readonly style="width:100%; height:200px;">{{ logs|join('\\n') }}</textarea>
    </div>
</div>
</body>
</html>
"""

COOKIE_COLLECTOR_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Cookie Collector</title>
    <style>
        body { font-family: system-ui; background: #0f172a; color: #e2e8f0; margin: 0; padding: 20px; }
        .container { max-width: 800px; margin: auto; }
        .card { background: #1e2937; padding: 24px; border-radius: 12px; margin-bottom: 20px; }
        h1 { color: #60a5fa; }
        textarea { width: 100%; background: #0f172a; color: #e2e8f0; border: 1px solid #475569;
                   border-radius: 8px; padding: 12px; box-sizing: border-box; }
        button { background: #3b82f6; color: white; padding: 10px 20px; border: none;
                 border-radius: 6px; cursor: pointer; font-size: 15px; }
        button:hover { background: #2563eb; }
        .back { color: #60a5fa; text-decoration: none; }
        .success { color: #4ade80; background: #052e16; padding: 10px; border-radius: 6px; display:none; }
        .error-msg { color: #f87171; background: #450a0a; padding: 10px; border-radius: 6px; display:none; }
    </style>
</head>
<body>
<div class="container">
    <h1>🍪 Cookie Collector</h1>
    <p><a href="/" class="back">← Back to Dashboard</a></p>

    <div class="card">
        <h3>Paste Your ndus Cookie</h3>
        <p>Open TeraBox in your browser, open DevTools → Application → Cookies and copy the <code>ndus</code> value.</p>
        <textarea id="ndusInput" rows="4" placeholder="Paste ndus cookie value here..."></textarea>
        <br><br>
        <button onclick="saveCookie()">💾 Save Cookie</button>
        <div id="successMsg" class="success" style="margin-top:12px;">✅ Cookie saved successfully!</div>
        <div id="errorMsg" class="error-msg" style="margin-top:12px;">❌ Failed to save cookie. Please try again.</div>
    </div>
</div>
<script>
function saveCookie() {
    const ndus = document.getElementById('ndusInput').value.trim();
    if (!ndus) { alert('Please enter an ndus value.'); return; }
    fetch('/save-ndus', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ndus: ndus })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            document.getElementById('successMsg').style.display = 'block';
            document.getElementById('errorMsg').style.display = 'none';
        } else {
            document.getElementById('errorMsg').style.display = 'block';
            document.getElementById('successMsg').style.display = 'none';
        }
    })
    .catch(() => {
        document.getElementById('errorMsg').style.display = 'block';
    });
}
</script>
</body>
</html>
"""


# ===================== ROUTES =====================


@app.route("/")
def dashboard():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    mysql_connected = DB_STATUS.get("connected", False)
    mysql_error = DB_STATUS.get("error", None)
    telegram_enabled = bool(os.getenv("TELEGRAM_BOT_TOKEN"))
    api_key_set = bool(os.getenv("API_KEY", "").strip())
    current_ndus = os.getenv("TERABOX_NDUS_COOKIE", "")

    return render_template_string(
        DASHBOARD_HTML,
        current_folder=current_target_folder,
        mysql_connected=mysql_connected,
        mysql_error=mysql_error,
        telegram_enabled=telegram_enabled,
        api_key_set=api_key_set,
        current_ndus=current_ndus,
        logs=logs[-30:],
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        fallback_user = os.getenv("FALLBACK_USERNAME", "admin")
        fallback_pass = os.getenv("FALLBACK_PASSWORD", "admin")
        if username == fallback_user and password == fallback_pass:
            session["logged_in"] = True
            flash("Login successful!", "success")
            return redirect(url_for("dashboard"))
        return render_template_string(LOGIN_HTML, error="Invalid username or password")
    return render_template_string(LOGIN_HTML, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/update-folder", methods=["POST"])
def update_folder():
    global current_target_folder
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    new_folder = request.form.get("folder", "").strip()
    if new_folder:
        current_target_folder = new_folder
        add_log(f"Target folder changed to: {new_folder}")
        flash("Target folder updated successfully!", "success")
    else:
        flash("Please enter a valid folder path", "error")
    return redirect(url_for("dashboard"))


@app.route("/cookie-collector")
def cookie_collector():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    return render_template_string(COOKIE_COLLECTOR_HTML)


@app.route("/save-ndus", methods=["POST"])
def save_ndus():
    if not session.get("logged_in"):
        return jsonify({"success": False})
    data = request.get_json()
    ndus_value = data.get("ndus", "").strip()
    if ndus_value:
        os.environ["TERABOX_NDUS_COOKIE"] = ndus_value
        save_ndus_to_db(ndus_value)
        add_log("ndus cookie saved via Cookie Collector")
        return jsonify({"success": True})
    return jsonify({"success": False})


# ===================== API ENDPOINT =====================

API_KEY = os.getenv("API_KEY", "default_secret_key_12345").strip()  # .strip() to handle trailing spaces in .env


@app.route("/api/save-cookie", methods=["POST"])
def api_save_cookie():
    data = request.get_json()
    if not data or data.get("api_key") != API_KEY:
        return jsonify({"success": False, "message": "Invalid API key"}), 401

    ndus_value = data.get("ndus", "").strip()
    if ndus_value:
        os.environ["TERABOX_NDUS_COOKIE"] = ndus_value
        save_ndus_to_db(ndus_value)
        add_log("ndus cookie received via API")
        return jsonify({"success": True})

    return jsonify({"success": False, "message": "No ndus provided"})


@app.route("/health")
def health():
    """Health check with live database test."""
    # Quick DB connectivity test
    db_ok = False
    db_error = None
    if os.getenv("USE_MYSQL", "false").lower() == "true":
        conn, err = get_db_connection(return_error=True)
        if conn:
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.close()
                conn.close()
                db_ok = True
            except Exception as e:
                db_error = str(e)
        else:
            db_error = err

    return jsonify(
        {
            "status": "ok",
            "database": {
                "enabled": os.getenv("USE_MYSQL", "false").lower() == "true",
                "connected": db_ok,
                "error": db_error,
            },
            "telegram_bot": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
        }
    )


# ===================== TELEGRAM BOT =====================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


def login_terabox(email, password):
    try:
        auth_url = "https://passport.baidu.com/v2/api/?login"
        mobile_headers = {
            "User-Agent": "com.dubox.drive/3.24.0 (Android; 13; Scale/3.00)",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        payload = {
            "username": email,
            "password": password,
            "clienttype": "1",
            "app_id": "250528",
            "ts": int(time.time() * 1000),
        }
        req_session = requests.Session()
        response = req_session.post(
            auth_url, data=payload, headers=mobile_headers, timeout=15
        )
        if response.status_code == 200:
            cookies = req_session.cookies.get_dict()
            if "ndus" in cookies:
                return {"success": True, "ndus": cookies["ndus"]}
            else:
                return {"success": False, "error": "ndus not found in response cookies"}
        else:
            return {"success": False, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def run_telegram_bot():
    if not TELEGRAM_TOKEN:
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.ext import (
            Application,
            CommandHandler,
            MessageHandler,
            CallbackQueryHandler,
            ContextTypes,
            filters,
        )

        # ─── Helper: Format DB table for Telegram ───

        def format_db_table(columns, rows, max_col_width=20):
            """Format database results as a monospace text table for Telegram."""
            if not columns:
                return "No data."

            # Truncate column values
            def trunc(val, width):
                s = str(val) if val is not None else "NULL"
                return s[:width] + "…" if len(s) > width else s

            # Calculate column widths
            col_widths = []
            for i, col in enumerate(columns):
                max_w = min(len(col), max_col_width)
                for row in rows:
                    val_len = len(trunc(row[i], max_col_width))
                    if val_len > max_w:
                        max_w = val_len
                col_widths.append(min(max_w, max_col_width))

            # Build header
            header = " │ ".join(trunc(col, col_widths[i]).ljust(col_widths[i]) for i, col in enumerate(columns))
            separator = "─┼─".join("─" * w for w in col_widths)

            # Build rows
            row_lines = []
            for row in rows:
                line = " │ ".join(trunc(row[i], col_widths[i]).ljust(col_widths[i]) for i in range(len(columns)))
                row_lines.append(line)

            result = f"{header}\n{separator}\n" + "\n".join(row_lines)
            return result

        # ─── /start Command ───

        async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await update.message.reply_text(
                "🚀 <b>Welcome to TeraBox Manager!</b>\n\n"
                "Use /help to see available commands.\n\n"
                "💡 <i>Send any TeraBox share link to auto-save it to your account.</i>",
                parse_mode="HTML",
            )

        # ─── /help Command (context-aware) ───

        async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id
            is_admin = is_admin_session_valid(user_id)

            base_commands = (
                "📋 <b>Available Commands</b>\n\n"
                "  /start — Welcome message\n"
                "  /help — Show this help\n"
                "  /status — Check TeraBox connection\n"
                "  /folders [path] — List TeraBox folders\n"
                "  /upload — Direct file upload info\n"
                "  /setfolder &lt;path&gt; — Change target folder\n"
                "  /setcookie &lt;value&gt; — Update cookie\n"
                "  /login — QR Code / login options\n"
                "  /admin — Admin authentication\n\n"
                "💡 <i>Send any file (document, video, photo) or TeraBox share link to save to your cloud!</i>\n"
            )

            if is_admin:
                remaining = admin_sessions[user_id]["expires_at"] - time.time()
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                admin_commands = (
                    "\n🔓 <b>Admin Commands</b> "
                    f"<i>(session: {mins}m {secs}s remaining)</i>\n\n"
                    "  /dbstatus — Database connection info\n"
                    "  /dbtables — List all database tables\n"
                    "  /dbquery &lt;table&gt; [limit] — View table data\n"
                    "  /transfers — View transfer history\n"
                    "  /sysinfo — System configuration\n"
                    "  /proxy — View active proxy status\n"
                    "  /setproxy &lt;url|direct&gt; — Configure proxy\n"
                    "  /rotateproxy — Auto-rotate proxy from live pool\n"
                    "  /adminlog — View admin audit log\n"
                    "  /adminlogout — End admin session\n"
                )
                await update.message.reply_text(
                    base_commands + admin_commands,
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(base_commands, parse_mode="HTML")

        # ─── /status Command ───

        async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            ndus = os.getenv("TERABOX_NDUS_COOKIE", "")
            if not ndus:
                await update.message.reply_text("❌ No ndus cookie configured. Use /setcookie to set one.")
                return
            result = check_terabox_connectivity(ndus)
            bot_status = "✅ Connected" if TELEGRAM_TOKEN else "❌ Not configured"

            if result["success"]:
                tera_status = "✅ " + result["message"]
            else:
                tera_status = "⚠️ " + result["message"]

            # Database status
            use_mysql = os.getenv('USE_MYSQL', 'false').lower() == 'true'
            if not use_mysql:
                db_line = "⚫ Disabled"
            elif DB_STATUS.get("connected"):
                db_line = f"✅ Connected ({DB_STATUS.get('table_count', '?')} tables)"
            else:
                db_line = f"❌ Error: {str(DB_STATUS.get('error', 'unknown'))[:80]}"

            # Extra cookies status
            browserid = os.getenv("TERABOX_BROWSERID", "")
            csrftoken = os.getenv("TERABOX_CSRFTOKEN", "")
            ndut_fmt = os.getenv("TERABOX_NDUT_FMT", "")
            ndut_fmv = os.getenv("TERABOX_NDUT_FMV", "")
            extra_cookies = []
            if ndut_fmt:
                extra_cookies.append("ndut_fmt ✅")
            else:
                extra_cookies.append("ndut_fmt ❌")
            if ndut_fmv:
                extra_cookies.append("ndut_fmv ✅")
            else:
                extra_cookies.append("ndut_fmv ❌")
            if browserid:
                extra_cookies.append("browserid ✅")
            if csrftoken:
                extra_cookies.append("csrfToken ✅")

            msg = (
                "📊 <b>System Status</b>\n\n"
                f"<b>TeraBox:</b> {tera_status}\n"
                f"<b>API Domain:</b> <code>{TERABOX_BASE_URL}</code>\n"
                f"<b>Telegram Bot:</b> {bot_status}\n"
                f"<b>ndus Cookie:</b> {len(ndus)} chars ({mask_sensitive(ndus, 4)})\n"
                f"<b>Extra Cookies:</b> {', '.join(extra_cookies)}\n"
                f"<b>Target Folder:</b> {current_target_folder}\n"
                f"<b>Database:</b> {db_line}\n"
            )
            await update.message.reply_text(msg, parse_mode="HTML")

        # ─── /folders Command ───

        async def folders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            ndus = os.getenv("TERABOX_NDUS_COOKIE", "")
            if not ndus:
                await update.message.reply_text("❌ No ndus cookie set. Use /setcookie to set one.")
                return

            path = context.args[0] if context.args else "/"
            await update.message.reply_text(f"📂 Loading folders in <code>{path}</code>...", parse_mode="HTML")

            result = list_terabox_folders(ndus, path)
            if result["success"]:
                items = result["list"]
                if not items:
                    await update.message.reply_text(f"📂 No items found in <code>{path}</code>", parse_mode="HTML")
                    return

                folders = []
                files = []
                for item in items[:20]:  # Limit to 20 items
                    name = item.get("server_filename", "?")
                    if item.get("isdir") == 1:
                        folders.append(f"  📁 {name}")
                    else:
                        size_mb = item.get("size", 0) / (1024 * 1024)
                        files.append(f"  📄 {name} ({size_mb:.1f} MB)")

                lines = [f"📂 <b>Contents of</b> <code>{path}</code>\n"]
                if folders:
                    lines.append("<b>Folders:</b>")
                    lines.extend(folders)
                if files:
                    lines.append("\n<b>Files:</b>")
                    lines.extend(files)
                if len(items) > 20:
                    lines.append(f"\n<i>...and {len(items) - 20} more items</i>")

                await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            else:
                await update.message.reply_text(f"❌ {result.get('error', 'Unknown error')}")

        # ─── /setfolder Command ───

        async def setfolder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            global current_target_folder
            if not context.args:
                await update.message.reply_text(
                    f"📂 <b>Current target folder:</b> <code>{current_target_folder}</code>\n\n"
                    f"<b>Usage:</b> <code>/setfolder /path/to/folder</code>\n\n"
                    f"Example: <code>/setfolder /Telegram_Uploads</code>",
                    parse_mode="HTML",
                )
                return

            new_folder = " ".join(context.args)
            if not new_folder.startswith("/"):
                new_folder = "/" + new_folder

            old_folder = current_target_folder
            current_target_folder = new_folder
            os.environ["TARGET_FOLDER_PATH"] = new_folder
            add_log(f"Target folder changed: {old_folder} → {new_folder} (via Telegram)")

            await update.message.reply_text(
                f"✅ <b>Target folder updated!</b>\n\n"
                f"<b>Old:</b> <code>{old_folder}</code>\n"
                f"<b>New:</b> <code>{new_folder}</code>",
                parse_mode="HTML",
            )

        # ─── /login Command (QR Code + Cookie Options) ───

        async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            keyboard = [
                [InlineKeyboardButton("📱 Scan QR Code (Mobile Friendly)", callback_data="login_qr")],
                [InlineKeyboardButton("🍪 Set Cookie Manually", callback_data="login_manual_info")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "🔐 <b>TeraBox Login Methods</b>\n\n"
                "Choose your preferred login method:\n\n"
                "1️⃣ <b>QR Code Scan (Recommended for Mobile)</b>:\n"
                "   • Generates a QR Code\n"
                "   • Open TeraBox mobile app → Settings → Scan QR\n"
                "   • Server captures your session automatically!\n\n"
                "2️⃣ <b>Set Cookie Manually</b>:\n"
                "   • Use <code>/setcookie &lt;ndus_value&gt;</code>",
                parse_mode="HTML",
                reply_markup=reply_markup,
            )

        # ─── /setcookie Command (Smart Cookie Parser) ───

        async def setcookie_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not context.args:
                await update.message.reply_text(
                    "🍪 <b>Usage:</b> <code>/setcookie &lt;ndus_or_full_cookie_string&gt;</code>\n\n"
                    "Paste your <code>ndus</code> value OR your full Cookie header from browser DevTools.",
                    parse_mode="HTML",
                )
                return

            raw_input = " ".join(context.args).strip()

            # Parse multi-cookie strings (e.g. key1=val1; key2=val2)
            if ";" in raw_input and "=" in raw_input:
                for part in raw_input.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        k_upper = k.strip().upper()
                        v_val = v.strip()
                        if k_upper == "NDUS":
                            os.environ["TERABOX_NDUS_COOKIE"] = v_val
                        elif k_upper == "BROWSERID":
                            os.environ["TERABOX_BROWSERID"] = v_val
                        elif k_upper == "CSRFTOKEN":
                            os.environ["TERABOX_CSRFTOKEN"] = v_val
                        elif k_upper == "NDUT_FMT":
                            os.environ["TERABOX_NDUT_FMT"] = v_val
                        elif k_upper == "NDUT_FMV":
                            os.environ["TERABOX_NDUT_FMV"] = v_val

                new_cookie = os.getenv("TERABOX_NDUS_COOKIE", raw_input)
            else:
                new_cookie = raw_input
                os.environ["TERABOX_NDUS_COOKIE"] = new_cookie

            update_cookie(new_cookie)
            save_ndus_to_db(new_cookie)
            add_log("Cookie updated via Telegram /setcookie")

            await update.message.reply_text(
                f"✅ <b>Cookie updated successfully!</b>\n"
                f"<b>ndus Preview:</b> <code>{mask_sensitive(new_cookie, 4)}</code>",
                parse_mode="HTML",
            )

        # ─── /admin Command (Multi-step Authentication) ───

        async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id

            # Check if already logged in
            if is_admin_session_valid(user_id):
                remaining = admin_sessions[user_id]["expires_at"] - time.time()
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                await update.message.reply_text(
                    f"✅ <b>Admin session active!</b>\n"
                    f"⏱ Expires in: {mins}m {secs}s\n\n"
                    f"Use /help to see admin commands.",
                    parse_mode="HTML",
                )
                return

            await update.message.reply_text(
                "🔐 <b>Admin Access Required</b>\n\n"
                "Please enter your API Key:",
                parse_mode="HTML",
            )
            context.user_data["admin_step"] = "api_key"
            context.user_data["admin_attempt_time"] = time.time()

        # ─── Admin Multi-step Handler ───

        async def handle_admin_steps(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Handle multi-step admin authentication via text messages."""
            user_id = update.effective_user.id
            text = update.message.text.strip()

            if "admin_step" not in context.user_data:
                return  # Not in admin auth flow — ignore

            # Timeout the auth flow after 2 minutes of inactivity
            attempt_time = context.user_data.get("admin_attempt_time", 0)
            if time.time() - attempt_time > 120:
                context.user_data.pop("admin_step", None)
                context.user_data.pop("admin_username", None)
                context.user_data.pop("admin_attempt_time", None)
                await update.message.reply_text("⏰ Authentication timed out. Use /admin to try again.")
                add_audit_log(user_id, "AUTH_TIMEOUT", "Login flow timed out")
                return

            step = context.user_data["admin_step"]

            if step == "api_key":
                api_key = os.getenv("API_KEY", "")
                if not api_key:
                    await update.message.reply_text("❌ API_KEY is not configured on the server.")
                    context.user_data.pop("admin_step", None)
                    add_audit_log(user_id, "AUTH_FAIL", "API_KEY not configured")
                    return

                if text == api_key:
                    context.user_data["admin_step"] = "username"
                    context.user_data["admin_attempt_time"] = time.time()
                    await update.message.reply_text("✅ API Key verified!\n\nEnter Username:")
                    add_audit_log(user_id, "AUTH_STEP", "API key verified")
                else:
                    await update.message.reply_text("❌ Invalid API Key.")
                    context.user_data.pop("admin_step", None)
                    add_audit_log(user_id, "AUTH_FAIL", "Invalid API key")

            elif step == "username":
                context.user_data["admin_username"] = text
                context.user_data["admin_step"] = "password"
                context.user_data["admin_attempt_time"] = time.time()
                await update.message.reply_text("Enter Password:")

            elif step == "password":
                username = context.user_data.get("admin_username", "")
                fallback_user = os.getenv("FALLBACK_USERNAME", "admin")
                fallback_pass = os.getenv("FALLBACK_PASSWORD", "admin")

                if username == fallback_user and text == fallback_pass:
                    # ✅ Login success — create time-limited session
                    now = time.time()
                    admin_sessions[user_id] = {
                        "login_time": now,
                        "expires_at": now + ADMIN_SESSION_TIMEOUT,
                        "chat_id": update.effective_chat.id,
                        "username": username,
                    }
                    expires_str = datetime.fromtimestamp(
                        now + ADMIN_SESSION_TIMEOUT
                    ).strftime("%H:%M:%S")
                    add_audit_log(user_id, "LOGIN_SUCCESS", f"Admin session started as '{username}'")
                    add_log(f"Admin login: user_id={user_id}")

                    # Build inline keyboard for quick access
                    keyboard = [
                        [
                            InlineKeyboardButton("📊 DB Status", callback_data="admin_dbstatus"),
                            InlineKeyboardButton("📋 DB Tables", callback_data="admin_dbtables"),
                        ],
                        [
                            InlineKeyboardButton("📜 Transfers", callback_data="admin_transfers"),
                            InlineKeyboardButton("⚙️ Sys Info", callback_data="admin_sysinfo"),
                        ],
                        [
                            InlineKeyboardButton("📝 Audit Log", callback_data="admin_auditlog"),
                            InlineKeyboardButton("🚪 Logout", callback_data="admin_logout"),
                        ],
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await update.message.reply_text(
                        f"✅ <b>Admin Unlocked!</b>\n\n"
                        f"⏱ Session expires at: <code>{expires_str}</code> "
                        f"({ADMIN_SESSION_TIMEOUT // 60} min window)\n\n"
                        f"Use /help to see all admin commands, or tap below:",
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                    )
                else:
                    await update.message.reply_text("❌ Invalid credentials.")
                    add_audit_log(user_id, "AUTH_FAIL", f"Bad credentials for user '{username}'")

                # Clean up auth state
                context.user_data.pop("admin_step", None)
                context.user_data.pop("admin_username", None)
                context.user_data.pop("admin_attempt_time", None)

        # ─── Admin-only command wrapper ───

        def require_admin(func):
            """Decorator that checks admin session before executing a command."""
            async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
                user_id = update.effective_user.id
                if not is_admin_session_valid(user_id):
                    await update.message.reply_text(
                        "🔒 <b>Unauthorized.</b> Use /admin to authenticate.",
                        parse_mode="HTML",
                    )
                    return
                add_audit_log(user_id, f"CMD:{func.__name__}")
                return await func(update, context)
            return wrapper

        # ─── /dbstatus ───

        @require_admin
        async def dbstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            use_mysql = os.getenv("USE_MYSQL", "false").lower() == "true"
            if not use_mysql:
                await update.message.reply_text("⚠️ MySQL is disabled (USE_MYSQL=false in .env).")
                return

            host = os.getenv("MYSQL_HOST", "?")
            db_name = os.getenv("MYSQL_DATABASE", "?")
            port = os.getenv("MYSQL_PORT", "3306")
            user = os.getenv("MYSQL_USER", "?")

            await update.message.reply_text("🔄 Testing database connection...")

            # Live connection test with error details
            conn_start = time.time()
            conn, conn_error = get_db_connection(return_error=True)
            conn_time = (time.time() - conn_start) * 1000  # ms

            if conn:
                try:
                    cursor = conn.cursor()
                    cursor.execute("SHOW TABLES")
                    tables = cursor.fetchall()
                    table_count = len(tables)
                    cursor.execute("SELECT VERSION()")
                    version = cursor.fetchone()[0]
                    cursor.close()
                    conn.close()
                    status = "✅ Connected"
                    error_detail = ""
                except Exception as e:
                    status = f"⚠️ Connected but query failed"
                    error_detail = f"\n<b>Error:</b> <code>{str(e)[:150]}</code>"
                    table_count = "?"
                    version = "?"
            else:
                status = "❌ Connection failed"
                error_detail = f"\n<b>Error:</b> <code>{str(conn_error)[:300]}</code>"
                table_count = "?"
                version = "?"
                conn_time = 0

            # Also show startup check result
            startup_info = ""
            if DB_STATUS.get("checked_at"):
                startup_status = "✅" if DB_STATUS.get("connected") else "❌"
                startup_info = f"\n<b>Startup Check:</b> {startup_status} ({DB_STATUS['checked_at']})"
                if DB_STATUS.get("error"):
                    startup_info += f"\n<b>Startup Error:</b> <code>{str(DB_STATUS['error'])[:150]}</code>"

            msg = (
                "📊 <b>Database Status</b>\n\n"
                f"<b>Status:</b> {status}\n"
                f"<b>Host:</b> <code>{host}:{port}</code>\n"
                f"<b>Database:</b> <code>{db_name}</code>\n"
                f"<b>User:</b> <code>{user}</code>\n"
                f"<b>MySQL Version:</b> {version}\n"
                f"<b>Tables:</b> {table_count}\n"
                f"<b>Latency:</b> {conn_time:.0f}ms"
                f"{error_detail}"
                f"{startup_info}\n"
            )
            await update.message.reply_text(msg, parse_mode="HTML")

        # ─── /dbtables ───

        @require_admin
        async def dbtables_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            conn, conn_err = get_db_connection(return_error=True)
            if not conn:
                await update.message.reply_text(
                    f"❌ <b>Cannot connect to database</b>\n\n"
                    f"<code>{str(conn_err)[:300]}</code>",
                    parse_mode="HTML",
                )
                return

            try:
                cursor = conn.cursor()
                cursor.execute("SHOW TABLES")
                tables = [row[0] for row in cursor.fetchall()]

                if not tables:
                    await update.message.reply_text("📋 No tables found in database.")
                    cursor.close()
                    conn.close()
                    return

                # Get row count for each table
                table_info = []
                for table in tables:
                    try:
                        cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                        count = cursor.fetchone()[0]
                        table_info.append((table, count))
                    except Exception:
                        table_info.append((table, "?"))

                cursor.close()
                conn.close()

                # Build message
                lines = ["📋 <b>Database Tables</b>\n"]
                for name, count in table_info:
                    lines.append(f"  📦 <code>{name}</code> — {count} rows")

                # Build inline keyboard for quick inspection
                keyboard = []
                row_buttons = []
                for name, _ in table_info:
                    row_buttons.append(
                        InlineKeyboardButton(f"📦 {name}", callback_data=f"dbview_{name}_10")
                    )
                    if len(row_buttons) == 2:
                        keyboard.append(row_buttons)
                        row_buttons = []
                if row_buttons:
                    keyboard.append(row_buttons)

                reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

                await update.message.reply_text(
                    "\n".join(lines) + "\n\n<i>Tap a table to view its data:</i>",
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            except Exception as e:
                try:
                    conn.close()
                except Exception:
                    pass
                await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

        # ─── /dbquery <table> [limit] ───

        @require_admin
        async def dbquery_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not context.args:
                await update.message.reply_text(
                    "📝 <b>Usage:</b> <code>/dbquery table_name [limit]</code>\n\n"
                    "Example: <code>/dbquery user_cookies 5</code>\n"
                    "<i>Max limit: 50 rows. Default: 10.</i>",
                    parse_mode="HTML",
                )
                return

            table_name = context.args[0]
            limit = 10
            if len(context.args) > 1:
                try:
                    limit = min(int(context.args[1]), 50)
                except ValueError:
                    limit = 10

            # Validate table name (basic SQL injection prevention)
            if not table_name.replace("_", "").isalnum():
                await update.message.reply_text("❌ Invalid table name.")
                return

            query = f"SELECT * FROM `{table_name}` LIMIT {limit}"
            result, error = safe_db_query(query, limit)

            if error:
                await update.message.reply_text(f"❌ Query error: {error}")
                return

            if not result["rows"]:
                await update.message.reply_text(f"📭 Table <code>{table_name}</code> is empty.", parse_mode="HTML")
                return

            table_text = format_db_table(result["columns"], result["rows"])
            msg = (
                f"📦 <b>{table_name}</b> "
                f"(showing {len(result['rows'])} rows)\n\n"
                f"<pre>{table_text}</pre>"
            )

            # Truncate if too long for Telegram (4096 char limit)
            if len(msg) > 4000:
                msg = msg[:3950] + "\n\n<i>... truncated (table too large)</i></pre>"

            await update.message.reply_text(msg, parse_mode="HTML")

        # ─── /transfers ───

        @require_admin
        async def transfers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            page = 1
            if context.args:
                try:
                    page = max(1, int(context.args[0]))
                except ValueError:
                    page = 1

            offset = (page - 1) * 10
            query = f"SELECT id, share_url, status, message, timestamp FROM transfer_history ORDER BY id DESC LIMIT 10 OFFSET {offset}"
            result, error = safe_db_query(query, 10)

            if error:
                if "doesn't exist" in error.lower() or "1146" in error:
                    await update.message.reply_text(
                        "📭 <b>No transfer history yet.</b>\n\n"
                        "<i>The transfer_history table will be created when the first transfer is made.</i>",
                        parse_mode="HTML",
                    )
                else:
                    await update.message.reply_text(f"❌ Error: {error}")
                return

            if not result["rows"]:
                await update.message.reply_text("📭 No transfer records found for this page.")
                return

            lines = [f"📜 <b>Transfer History</b> (Page {page})\n"]
            for row in result["rows"]:
                tid, url, status, message, ts = row
                short_url = (str(url)[:40] + "…") if url and len(str(url)) > 40 else str(url)
                ts_str = str(ts)[:19] if ts else "?"
                status_icon = "✅" if status and "success" in str(status).lower() else "❌"
                lines.append(f"{status_icon} <b>#{tid}</b> {ts_str}\n   <code>{short_url}</code>\n   {message}\n")

            # Pagination buttons
            keyboard = []
            nav_buttons = []
            if page > 1:
                nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"transfers_page_{page - 1}"))
            nav_buttons.append(InlineKeyboardButton(f"Page {page}", callback_data="noop"))
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"transfers_page_{page + 1}"))
            keyboard.append(nav_buttons)

            await update.message.reply_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        # ─── /sysinfo ───

        @require_admin
        async def sysinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            uptime_seconds = time.time() - SERVER_START_TIME
            hours = int(uptime_seconds // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            secs = int(uptime_seconds % 60)

            ndus = os.getenv("TERABOX_NDUS_COOKIE", "")
            token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            api_key = os.getenv("API_KEY", "")

            active_admins = sum(1 for uid in admin_sessions if is_admin_session_valid(uid))

            msg = (
                "⚙️ <b>System Information</b>\n\n"
                f"<b>Python:</b> {platform.python_version()}\n"
                f"<b>Platform:</b> {platform.system()} {platform.release()}\n"
                f"<b>Uptime:</b> {hours}h {minutes}m {secs}s\n\n"
                f"<b>TeraBox API:</b> <code>{TERABOX_BASE_URL}</code>\n"
                f"<b>ndus Cookie:</b> {mask_sensitive(ndus, 4)} ({len(ndus)} chars)\n"
                f"<b>Bot Token:</b> {mask_sensitive(token, 4)}\n"
                f"<b>API Key:</b> {mask_sensitive(api_key, 4)}\n"
                f"<b>Target Folder:</b> <code>{current_target_folder}</code>\n\n"
                f"<b>MySQL:</b> {'Enabled' if os.getenv('USE_MYSQL', 'false').lower() == 'true' else 'Disabled'}\n"
                f"<b>Flask Port:</b> {os.environ.get('PORT', '5000')}\n"
                f"<b>Active Admin Sessions:</b> {active_admins}\n"
                f"<b>Audit Log Entries:</b> {len(admin_audit_log)}\n"
                f"<b>Activity Logs:</b> {len(logs)}\n"
            )
            await update.message.reply_text(msg, parse_mode="HTML")

        # ─── /adminlog ───

        @require_admin
        async def adminlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not admin_audit_log:
                await update.message.reply_text("📝 No admin audit log entries yet.")
                return

            recent = admin_audit_log[-20:]  # Last 20 entries
            lines = ["📝 <b>Admin Audit Log</b> (last 20)\n"]
            for entry in reversed(recent):
                lines.append(
                    f"  <code>{entry['timestamp']}</code> "
                    f"👤{entry['user_id']} "
                    f"<b>{entry['action']}</b> → {entry['result']}"
                )

            await update.message.reply_text("\n".join(lines), parse_mode="HTML")

        # ─── /adminlogout ───

        async def adminlogout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id
            if user_id in admin_sessions:
                del admin_sessions[user_id]
                add_audit_log(user_id, "LOGOUT", "Manual logout")
                add_log(f"Admin logout: user_id={user_id}")
                await update.message.reply_text("🚪 <b>Admin session ended.</b> You are now logged out.", parse_mode="HTML")
            else:
                await update.message.reply_text("ℹ️ You don't have an active admin session.")

        # ─── Proxy Management Commands (Admin Only) ───

        @require_admin
        async def proxy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            cur = get_current_proxy()
            masked_p = mask_sensitive(cur, 8) if cur else "Direct (No Proxy)"
            await update.message.reply_text(
                f"🌐 <b>Active Proxy Configuration</b>\n\n"
                f"Current: <code>{masked_p}</code>\n\n"
                f"Commands:\n"
                f"• <code>/setproxy http://user:pass@ip:port</code> — Set proxy\n"
                f"• <code>/rotateproxy</code> — Auto-fetch & switch to next live proxy\n"
                f"• <code>/setproxy direct</code> — Disable proxy",
                parse_mode="HTML"
            )

        @require_admin
        async def setproxy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            args = context.args
            if not args:
                await update.message.reply_text("❌ Usage: <code>/setproxy http://user:pass@ip:port</code> or <code>/setproxy direct</code>", parse_mode="HTML")
                return
            target = args[0].strip()
            if target.lower() in ["direct", "none", "off", "clear"]:
                set_active_proxy("")
                await update.message.reply_text("🌐 Proxy cleared. Using direct connection.")
            else:
                set_active_proxy(target)
                await update.message.reply_text(f"🌐 Proxy updated successfully to:\n<code>{mask_sensitive(target, 10)}</code>", parse_mode="HTML")

        @require_admin
        async def rotateproxy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            msg = await update.message.reply_text("⏳ Fetching fresh dynamic proxy pool & rotating...")
            new_p = rotate_proxy()
            if new_p:
                await msg.edit_text(f"🔄 <b>Rotated successfully!</b>\nNew Proxy: <code>{mask_sensitive(new_p, 12)}</code>", parse_mode="HTML")
            else:
                await msg.edit_text("❌ Failed to fetch live proxies from community pool.")

        # ─── Callback Query Handler (Inline Keyboard Buttons) ───

        async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
            query = update.callback_query
            await query.answer()  # Acknowledge the button press

            user_id = query.from_user.id
            data = query.data

            if data == "noop":
                return

            # Check admin session for admin callbacks
            if data.startswith("admin_") or data.startswith("dbview_") or data.startswith("transfers_"):
                if not is_admin_session_valid(user_id):
                    await query.edit_message_text(
                        "🔒 <b>Session expired.</b> Use /admin to re-authenticate.",
                        parse_mode="HTML",
                    )
                    return

            # Route callbacks
            if data == "login_manual_info":
                await query.edit_message_text(
                    "🍪 <b>Manual Cookie Guide</b>\n\n"
                    "1. Open TeraBox in PC Browser\n"
                    "2. Open DevTools (F12) → Application → Cookies\n"
                    "3. Copy <code>ndus</code> value\n"
                    "4. Run <code>/setcookie &lt;your_ndus_value&gt;</code> in Telegram!",
                    parse_mode="HTML",
                )
                return

            elif data == "login_qr":
                await query.edit_message_text("⏳ Requesting QR Code from TeraBox Passport API...")
                qr_data = get_qr_login_data()
                if not qr_data.get("success"):
                    await query.edit_message_text(
                        f"⚠️ <b>QR Code Service Unavailable:</b> {qr_data.get('error')}\n\n"
                        f"<i>Please use <code>/setcookie &lt;ndus_value&gt;</code> to set your cookie directly!</i>",
                        parse_mode="HTML",
                    )
                    return

                try:
                    import qrcode
                    import io
                    qr_url = qr_data.get("url", "")
                    img = qrcode.make(qr_url)
                    bio = io.BytesIO()
                    img.save(bio, "PNG")
                    bio.seek(0)

                    await query.message.reply_photo(
                        photo=bio,
                        caption=(
                            "📸 <b>TeraBox QR Code Login</b>\n\n"
                            "1. Open your official <b>TeraBox Mobile App</b>\n"
                            "2. Go to <b>Settings → Scan QR Code</b>\n"
                            "3. Scan this code!\n\n"
                            "⏳ <i>Server is watching for your confirmation...</i>"
                        ),
                        parse_mode="HTML",
                    )

                    sign = qr_data.get("sign", "")
                    chat_id = query.message.chat_id

                    def poll_thread():
                        for _ in range(60):
                            time.sleep(2)
                            res = poll_qr_login(sign)
                            if res.get("status") == "confirmed":
                                ndus = res.get("ndus", "")
                                if ndus:
                                    os.environ["TERABOX_NDUS_COOKIE"] = ndus
                                    update_cookie(ndus)
                                    save_ndus_to_db(ndus)
                                    add_log(f"QR Login successful for chat_id={chat_id}")
                                    try:
                                        import urllib.request
                                        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
                                        msg_url = f"https://api.telegram.org/bot{bot_token}/sendMessage?chat_id={chat_id}&text=✅+QR+Login+Successful!+Your+session+has+been+saved."
                                        urllib.request.urlopen(msg_url)
                                    except Exception:
                                        pass
                                break
                            elif res.get("status") == "expired":
                                break

                    threading.Thread(target=poll_thread, daemon=True).start()

                except Exception as e:
                    await query.edit_message_text(f"❌ Error generating QR image: {e}")
                return

            elif data == "admin_dbstatus":
                add_audit_log(user_id, "BTN:dbstatus")
                # Inline: show db status
                use_mysql = os.getenv("USE_MYSQL", "false").lower() == "true"
                if not use_mysql:
                    await query.edit_message_text("⚠️ MySQL is disabled.")
                    return
                conn, conn_err = get_db_connection(return_error=True)
                if conn:
                    try:
                        cursor = conn.cursor()
                        cursor.execute("SHOW TABLES")
                        tc = len(cursor.fetchall())
                        cursor.execute("SELECT VERSION()")
                        ver = cursor.fetchone()[0]
                        cursor.close()
                        conn.close()
                        await query.edit_message_text(
                            f"📊 <b>DB Status</b>\n\n"
                            f"✅ Connected\n"
                            f"Host: <code>{os.getenv('MYSQL_HOST')}</code>\n"
                            f"DB: <code>{os.getenv('MYSQL_DATABASE')}</code>\n"
                            f"Version: {ver}\n"
                            f"Tables: {tc}",
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        await query.edit_message_text(f"❌ Connected but error: {str(e)[:200]}")
                else:
                    await query.edit_message_text(
                        f"❌ <b>Cannot connect to database</b>\n\n"
                        f"<code>{str(conn_err)[:300]}</code>",
                        parse_mode="HTML",
                    )

            elif data == "admin_dbtables":
                add_audit_log(user_id, "BTN:dbtables")
                conn, conn_err = get_db_connection(return_error=True)
                if not conn:
                    await query.edit_message_text(
                        f"❌ <b>Cannot connect to database</b>\n\n"
                        f"<code>{str(conn_err)[:300]}</code>",
                        parse_mode="HTML",
                    )
                    return
                try:
                    cursor = conn.cursor()
                    cursor.execute("SHOW TABLES")
                    tables = [row[0] for row in cursor.fetchall()]
                    table_info = []
                    for t in tables:
                        try:
                            cursor.execute(f"SELECT COUNT(*) FROM `{t}`")
                            c = cursor.fetchone()[0]
                            table_info.append((t, c))
                        except Exception:
                            table_info.append((t, "?"))
                    cursor.close()
                    conn.close()

                    lines = ["📋 <b>Database Tables</b>\n"]
                    for name, count in table_info:
                        lines.append(f"  📦 <code>{name}</code> — {count} rows")

                    keyboard = []
                    row_buttons = []
                    for name, _ in table_info:
                        row_buttons.append(
                            InlineKeyboardButton(f"📦 {name}", callback_data=f"dbview_{name}_10")
                        )
                        if len(row_buttons) == 2:
                            keyboard.append(row_buttons)
                            row_buttons = []
                    if row_buttons:
                        keyboard.append(row_buttons)

                    await query.edit_message_text(
                        "\n".join(lines) + "\n\n<i>Tap to view:</i>",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
                    )
                except Exception as e:
                    await query.edit_message_text(f"❌ Error: {str(e)[:200]}")

            elif data.startswith("dbview_"):
                # Format: dbview_tablename_limit (table name may contain underscores)
                # Use rsplit to split from the right — last segment is the limit
                suffix = data[len("dbview_"):]  # e.g. "transfer_history_10"
                if "_" not in suffix:
                    await query.edit_message_text("❌ Invalid table view request.")
                    return
                # Split on last underscore: "transfer_history_10" → ("transfer_history", "10")
                table_name, limit_str = suffix.rsplit("_", 1)
                try:
                    limit = min(int(limit_str), 50)
                except ValueError:
                    limit = 10

                add_audit_log(user_id, f"BTN:dbview_{table_name}")

                if not table_name.replace("_", "").isalnum():
                    await query.edit_message_text("❌ Invalid table name.")
                    return

                sql = f"SELECT * FROM `{table_name}` LIMIT {limit}"
                result, error = safe_db_query(sql, limit)

                if error:
                    await query.edit_message_text(f"❌ Error: {error}")
                    return

                if not result["rows"]:
                    await query.edit_message_text(f"📭 <code>{table_name}</code> is empty.", parse_mode="HTML")
                    return

                table_text = format_db_table(result["columns"], result["rows"])
                msg = (
                    f"📦 <b>{table_name}</b> ({len(result['rows'])} rows)\n\n"
                    f"<pre>{table_text}</pre>"
                )
                if len(msg) > 4000:
                    msg = msg[:3950] + "\n... truncated</pre>"

                # Back button
                keyboard = [[InlineKeyboardButton("⬅️ Back to Tables", callback_data="admin_dbtables")]]
                await query.edit_message_text(
                    msg,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )

            elif data == "admin_transfers":
                add_audit_log(user_id, "BTN:transfers")
                query_sql = "SELECT id, share_url, status, message, timestamp FROM transfer_history ORDER BY id DESC LIMIT 10"
                result, error = safe_db_query(query_sql, 10)

                if error:
                    if "doesn't exist" in error.lower() or "1146" in error:
                        await query.edit_message_text(
                            "📭 <b>No transfer history yet.</b>",
                            parse_mode="HTML",
                        )
                    else:
                        await query.edit_message_text(f"❌ Error: {error}")
                    return

                if not result["rows"]:
                    await query.edit_message_text("📭 No transfers recorded.")
                    return

                lines = ["📜 <b>Recent Transfers</b>\n"]
                for row in result["rows"]:
                    tid, url, status, message, ts = row
                    short_url = (str(url)[:35] + "…") if url and len(str(url)) > 35 else str(url)
                    ts_str = str(ts)[:19] if ts else "?"
                    icon = "✅" if status and "success" in str(status).lower() else "❌"
                    lines.append(f"{icon} #{tid} {ts_str}\n   <code>{short_url}</code>")

                await query.edit_message_text("\n".join(lines), parse_mode="HTML")

            elif data == "admin_sysinfo":
                add_audit_log(user_id, "BTN:sysinfo")
                uptime_s = time.time() - SERVER_START_TIME
                h, m, s = int(uptime_s // 3600), int((uptime_s % 3600) // 60), int(uptime_s % 60)
                ndus = os.getenv("TERABOX_NDUS_COOKIE", "")

                await query.edit_message_text(
                    f"⚙️ <b>System Info</b>\n\n"
                    f"Python: {platform.python_version()}\n"
                    f"Platform: {platform.system()}\n"
                    f"Uptime: {h}h {m}m {s}s\n"
                    f"Cookie: {mask_sensitive(ndus, 3)} ({len(ndus)}ch)\n"
                    f"Folder: <code>{current_target_folder}</code>\n"
                    f"MySQL: {'On' if os.getenv('USE_MYSQL', 'false').lower() == 'true' else 'Off'}\n"
                    f"Logs: {len(logs)} | Audits: {len(admin_audit_log)}",
                    parse_mode="HTML",
                )

            elif data == "admin_auditlog":
                add_audit_log(user_id, "BTN:auditlog")
                if not admin_audit_log:
                    await query.edit_message_text("📝 No audit entries yet.")
                    return

                recent = admin_audit_log[-15:]
                lines = ["📝 <b>Audit Log</b>\n"]
                for e in reversed(recent):
                    lines.append(
                        f"<code>{e['timestamp']}</code> "
                        f"👤{e['user_id']} {e['action']} → {e['result']}"
                    )
                await query.edit_message_text("\n".join(lines), parse_mode="HTML")

            elif data == "admin_logout":
                if user_id in admin_sessions:
                    del admin_sessions[user_id]
                    add_audit_log(user_id, "LOGOUT", "Button logout")
                    await query.edit_message_text("🚪 <b>Admin session ended.</b>", parse_mode="HTML")
                else:
                    await query.edit_message_text("ℹ️ No active session.")

            elif data.startswith("transfers_page_"):
                try:
                    page = max(1, int(data.split("_")[-1]))
                except ValueError:
                    page = 1

                add_audit_log(user_id, f"BTN:transfers_page_{page}")
                offset = (page - 1) * 10
                query_sql = f"SELECT id, share_url, status, message, timestamp FROM transfer_history ORDER BY id DESC LIMIT 10 OFFSET {offset}"
                result, error = safe_db_query(query_sql, 10)

                if error:
                    await query.edit_message_text(f"❌ Error: {error}")
                    return

                if not result["rows"]:
                    await query.edit_message_text(f"📭 No records on page {page}.")
                    return

                lines = [f"📜 <b>Transfers</b> (Page {page})\n"]
                for row in result["rows"]:
                    tid, url, status, message, ts = row
                    short_url = (str(url)[:35] + "…") if url and len(str(url)) > 35 else str(url)
                    ts_str = str(ts)[:19] if ts else "?"
                    icon = "✅" if status and "success" in str(status).lower() else "❌"
                    lines.append(f"{icon} #{tid} {ts_str}\n   <code>{short_url}</code>")

                nav = []
                if page > 1:
                    nav.append(InlineKeyboardButton("⬅️", callback_data=f"transfers_page_{page - 1}"))
                nav.append(InlineKeyboardButton(f"Pg {page}", callback_data="noop"))
                nav.append(InlineKeyboardButton("➡️", callback_data=f"transfers_page_{page + 1}"))

                await query.edit_message_text(
                    "\n".join(lines),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([nav]),
                )

        # ─── TeraBox Link Handler (Comprehensive Domain & Query Parser) ───

        async def handle_terabox_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Handle messages that contain TeraBox share links with verbose feedback."""
            text = update.message.text.strip()

            # Skip if in admin auth flow
            if "admin_step" in context.user_data:
                await handle_admin_steps(update, context)
                return

            # Comprehensive domain list including terabox.app, 1024tera, mirrobox, etc.
            terabox_keywords = [
                "terabox", "1024tera", "4funbox", "mirrobox",
                "nephobox", "momitbox", "freeterabox", "dubox", "surl="
            ]
            is_terabox_link = any(kw in text.lower() for kw in terabox_keywords)
            is_generic_url = "http://" in text.lower() or "https://" in text.lower()

            if is_terabox_link:
                from terabox_helper import transfer_to_terabox
                ndus = os.getenv("TERABOX_NDUS_COOKIE", "")
                if not ndus:
                    await update.message.reply_text("❌ No cookie configured. Use /setcookie to set one first.")
                    return

                # Send progress message
                status_msg = await update.message.reply_text(
                    f"⏳ <b>Processing TeraBox Link...</b>\n"
                    f"📂 <b>Target Folder:</b> <code>{current_target_folder}</code>\n"
                    f"🔄 <i>Contacting TeraBox servers...</i>",
                    parse_mode="HTML",
                )

                result = transfer_to_terabox(text, current_target_folder)
                add_log(f"Transfer: {text[:40]}... → {'Success' if result['success'] else 'Failed'}")

                # Log to database
                try:
                    from database import log_transfer
                    status_str = "success" if result["success"] else "failed"
                    log_transfer(text, status_str, result["message"])
                except Exception as e:
                    logger.error(f"Failed to log transfer: {e}")

                # Edit progress message with verbose result
                await status_msg.edit_text(result["message"], parse_mode="HTML")

            elif is_generic_url:
                await update.message.reply_text(
                    "⚠️ <b>Link Not Recognized</b>\n\n"
                    "Please send a valid TeraBox share link, e.g.:\n"
                    "<code>https://terabox.app/sharing/link?surl=...</code> or\n"
                    "<code>https://1024terabox.com/s/1...</code>",
                    parse_mode="HTML",
                )

        # ─── /upload Command ───

        async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await update.message.reply_text(
                "📤 <b>TeraBox Direct File Upload</b>\n\n"
                "Send or forward any document, photo, video, or audio file directly to this bot!\n\n"
                f"📂 <b>Target Folder:</b> <code>{current_target_folder}</code>\n"
                "<i>The bot will download your file and upload it directly to your TeraBox account.</i>",
                parse_mode="HTML",
            )

        # ─── Direct File Upload Handler ───

        async def handle_direct_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Handle direct document/file uploads sent to the Telegram bot."""
            ndus = os.getenv("TERABOX_NDUS_COOKIE", "")
            if not ndus:
                await update.message.reply_text("❌ No cookie configured. Use /setcookie to set one first.")
                return

            message = update.message
            document = message.document or message.video or message.audio or message.voice
            file_obj = None
            file_name = "uploaded_file"

            if message.document:
                file_obj = message.document
                file_name = message.document.file_name or "document"
            elif message.video:
                file_obj = message.video
                file_name = f"video_{message.video.file_unique_id}.mp4"
            elif message.audio:
                file_obj = message.audio
                file_name = message.audio.file_name or f"audio_{message.audio.file_unique_id}.mp3"
            elif message.photo:
                file_obj = message.photo[-1]
                file_name = f"photo_{file_obj.file_unique_id}.jpg"
            elif message.voice:
                file_obj = message.voice
                file_name = f"voice_{file_obj.file_unique_id}.ogg"

            if not file_obj:
                return

            status_msg = await update.message.reply_text(
                f"⏳ <b>Downloading file from Telegram...</b>\n"
                f"📄 <b>File Name:</b> <code>{file_name}</code>\n"
                f"📂 <b>Target Folder:</b> <code>{current_target_folder}</code>",
                parse_mode="HTML",
            )

            try:
                temp_dir = os.path.join(os.getcwd(), "scratch", "temp_uploads")
                os.makedirs(temp_dir, exist_ok=True)
                temp_path = os.path.join(temp_dir, file_name)

                tg_file = await context.bot.get_file(file_obj.file_id)
                await tg_file.download_to_drive(custom_path=temp_path)

                await status_msg.edit_text(
                    f"🔄 <b>Uploading to TeraBox...</b>\n"
                    f"📄 <b>File Name:</b> <code>{file_name}</code>\n"
                    f"📂 <b>Target Folder:</b> <code>{current_target_folder}</code>",
                    parse_mode="HTML",
                )

                result = upload_file_to_terabox(temp_path, current_target_folder)

                if os.path.exists(temp_path):
                    os.remove(temp_path)

                add_log(f"Direct Upload: {file_name} → {'Success' if result['success'] else 'Failed'}")
                await status_msg.edit_text(result["message"], parse_mode="HTML")

            except Exception as e:
                logger.error(f"Direct file upload error: {e}")
                await status_msg.edit_text(f"❌ Upload error: {str(e)}")

        # ─── Register All Handlers ───

        app_bot = Application.builder().token(TELEGRAM_TOKEN).build()

        # Command handlers
        app_bot.add_handler(CommandHandler("start", start))
        app_bot.add_handler(CommandHandler("help", help_cmd))
        app_bot.add_handler(CommandHandler("status", status_cmd))
        app_bot.add_handler(CommandHandler("folders", folders_cmd))
        app_bot.add_handler(CommandHandler("setfolder", setfolder_cmd))
        app_bot.add_handler(CommandHandler("setcookie", setcookie_cmd))
        app_bot.add_handler(CommandHandler("login", login_cmd))
        app_bot.add_handler(CommandHandler("upload", upload_cmd))
        app_bot.add_handler(CommandHandler("admin", admin_cmd))

        # Admin-only commands
        app_bot.add_handler(CommandHandler("dbstatus", dbstatus_cmd))
        app_bot.add_handler(CommandHandler("dbtables", dbtables_cmd))
        app_bot.add_handler(CommandHandler("dbquery", dbquery_cmd))
        app_bot.add_handler(CommandHandler("transfers", transfers_cmd))
        app_bot.add_handler(CommandHandler("sysinfo", sysinfo_cmd))
        app_bot.add_handler(CommandHandler("adminlog", adminlog_cmd))
        app_bot.add_handler(CommandHandler("adminlogout", adminlogout_cmd))
        app_bot.add_handler(CommandHandler("proxy", proxy_cmd))
        app_bot.add_handler(CommandHandler("setproxy", setproxy_cmd))
        app_bot.add_handler(CommandHandler("rotateproxy", rotateproxy_cmd))

        # Callback query handler (inline keyboard buttons)
        app_bot.add_handler(CallbackQueryHandler(handle_callbacks))

        # Text message handler (TeraBox links + admin auth flow)
        app_bot.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_terabox_link)
        )

        # File upload message handler (direct files sent to bot)
        app_bot.add_handler(
            MessageHandler(
                filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE,
                handle_direct_file_upload
            )
        )

        logger.info("Telegram bot starting with polling...")
        app_bot.run_polling(stop_signals=None)

    except Exception as e:
        logger.error(f"Bot error: {e}")
        import traceback
        traceback.print_exc()


# ===================== STARTUP DIAGNOSTICS =====================

def run_startup_diagnostics():
    """Run startup checks and log results."""
    logger.info("="*60)
    logger.info("🚀 TeraBox Manager — Startup Diagnostics")
    logger.info("="*60)

    # Check Telegram Bot Token
    if TELEGRAM_TOKEN:
        logger.info(f"✅ Telegram Bot Token: configured ({len(TELEGRAM_TOKEN)} chars)")
    else:
        logger.warning("❌ Telegram Bot Token: NOT SET — bot will not start")

    # Check ndus cookie
    ndus = os.getenv("TERABOX_NDUS_COOKIE", "")
    if ndus:
        logger.info(f"✅ TeraBox ndus Cookie: configured ({len(ndus)} chars)")
    else:
        logger.warning("⚠️ TeraBox ndus Cookie: NOT SET — transfers will fail")

    # Check API domain
    logger.info(f"🌐 TeraBox API Base: {TERABOX_BASE_URL}")

    # Check extra cookies
    browserid = os.getenv("TERABOX_BROWSERID", "")
    csrftoken = os.getenv("TERABOX_CSRFTOKEN", "")
    if browserid:
        logger.info(f"✅ TeraBox browserid: configured ({len(browserid)} chars)")
    else:
        logger.info("ℹ️ TeraBox browserid: not set (optional but recommended)")
    if csrftoken:
        logger.info(f"✅ TeraBox csrfToken: configured ({len(csrftoken)} chars)")
    else:
        logger.info("ℹ️ TeraBox csrfToken: not set (optional but recommended)")

    # Check API Key
    api_key = os.getenv("API_KEY", "").strip()
    if api_key and api_key != "default_secret_key_12345":
        logger.info(f"✅ API Key: configured ({len(api_key)} chars)")
    else:
        logger.warning("⚠️ API Key: using default or not set")

    # Check Database
    use_mysql = os.getenv("USE_MYSQL", "false").lower() == "true"
    if use_mysql:
        logger.info("🔄 MySQL: enabled — testing connection...")
        host = os.getenv("MYSQL_HOST", "?")
        db_name = os.getenv("MYSQL_DATABASE", "?")
        port = os.getenv("MYSQL_PORT", "3306")
        user = os.getenv("MYSQL_USER", "?")
        logger.info(f"   Host: {host}:{port}")
        logger.info(f"   Database: {db_name}")
        logger.info(f"   User: {user}")

        db_result = check_db_connection()
        if db_result["connected"]:
            logger.info(f"✅ MySQL: connected successfully!")
            logger.info(f"   Version: {db_result.get('version', '?')}")
            logger.info(f"   Tables: {db_result.get('table_count', '?')} ({', '.join(db_result.get('tables', []))})")
        else:
            logger.error(f"❌ MySQL: connection FAILED")
            logger.error(f"   Error: {db_result.get('error', 'unknown')}")
            logger.error(f"   💡 Tip: Free MySQL hosts (InfinityFree, FreeSQLDatabase) often block external connections.")
            logger.error(f"   💡 Try: Use a cloud DB (PlanetScale, Railway, Aiven) or Replit's built-in DB.")
    else:
        logger.info("ℹ️ MySQL: disabled (USE_MYSQL=false)")

    # Log target folder
    logger.info(f"📂 Target Folder: {current_target_folder}")
    logger.info("="*60)
    add_log("Server started — startup diagnostics complete")


# Run diagnostics before starting anything
run_startup_diagnostics()

if TELEGRAM_TOKEN:
    threading.Thread(target=run_telegram_bot, daemon=True).start()
    logger.info("Telegram bot thread started")
else:
    logger.warning("TELEGRAM_BOT_TOKEN not set — bot disabled")


# ===================== MAIN =====================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask on port {port}")
    app.run(host="0.0.0.0", port=port)
