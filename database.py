import os
import logging
import hashlib
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
logger = logging.getLogger(__name__)

USE_MYSQL = os.getenv("USE_MYSQL", "false").lower() == "true"


def get_mysql_config():
    if not USE_MYSQL:
        return None
    config = {
        "host": os.getenv("MYSQL_HOST"),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE"),
        "port": int(os.getenv("MYSQL_PORT", 3306))
    }
    missing = [k for k, v in config.items() if v is None]
    if missing:
        raise ValueError(f"MySQL enabled but missing fields: {missing}")
    return config


def get_db_connection():
    """Get a MySQL connection. Tries mysql-connector-python first, then PyMySQL."""
    if not USE_MYSQL:
        return None

    config = get_mysql_config()
    if not config:
        return None

    # Try driver 1: mysql-connector-python
    try:
        import mysql.connector
        conn = mysql.connector.connect(**config)
        return conn
    except ImportError:
        pass
    except Exception as e:
        logger.error(f"mysql-connector-python failed: {e}")

    # Try driver 2: PyMySQL
    try:
        import pymysql
        conn = pymysql.connect(
            host=config["host"],
            user=config["user"],
            password=config["password"],
            database=config["database"],
            port=config["port"],
            connect_timeout=10,
            cursorclass=pymysql.cursors.Cursor,
        )
        return conn
    except ImportError:
        pass
    except Exception as e:
        logger.error(f"pymysql failed: {e}")

    logger.error("All MySQL drivers failed to connect")
    return None


def hash_value(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# Fallback credentials from environment
FALLBACK_USERNAME = os.getenv("FALLBACK_USERNAME", "admin")
FALLBACK_PASSWORD_HASH = hash_value(os.getenv("FALLBACK_PASSWORD", "admin"))


def verify_login(username: str, password: str) -> bool:
    """Check against database if available, else fallback"""
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT password_hash FROM users WHERE username = %s",
                (username,)
            )
            row = cursor.fetchone()
            if row and row[0] == hash_value(password):
                return True
        except Exception as e:
            logger.error(f"DB auth error: {e}")
        finally:
            conn.close()

    # Fallback
    if username == FALLBACK_USERNAME and hash_value(password) == FALLBACK_PASSWORD_HASH:
        return True
    return False


def log_transfer(share_url: str, status: str, message: str):
    """Log transfer history"""
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS transfer_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    share_url VARCHAR(500),
                    status VARCHAR(50),
                    message TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute(
                "INSERT INTO transfer_history (share_url, status, message) VALUES (%s, %s, %s)",
                (share_url, status, message)
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to log transfer: {e}")
        finally:
            conn.close()
    else:
        logger.info(f"[NO DB] Transfer logged: {status} - {message}")


def save_setting(key: str, value: str):
    """Save persistent setting to MySQL table bot_settings + local JSON backup."""
    import json
    # Local JSON backup
    try:
        settings_file = os.path.join(os.getcwd(), "settings.json")
        data = {}
        if os.path.exists(settings_file):
            with open(settings_file, "r") as f:
                data = json.load(f)
        data[key] = value
        with open(settings_file, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed saving setting to local JSON: {e}")

    # MySQL persistence
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    setting_key VARCHAR(100) PRIMARY KEY,
                    setting_value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                INSERT INTO bot_settings (setting_key, setting_value)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """, (key, value))
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to save setting {key} to MySQL: {e}")
        finally:
            conn.close()


def get_setting(key: str, default: str = "") -> str:
    """Retrieve setting from MySQL table or local JSON backup."""
    import json
    # Try MySQL first
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT setting_value FROM bot_settings WHERE setting_key = %s", (key,))
            row = cursor.fetchone()
            if row and row[0] is not None:
                return str(row[0])
        except Exception as e:
            logger.debug(f"MySQL get_setting failed for {key}: {e}")
        finally:
            conn.close()

    # Try local JSON backup
    try:
        settings_file = os.path.join(os.getcwd(), "settings.json")
        if os.path.exists(settings_file):
            with open(settings_file, "r") as f:
                data = json.load(f)
                if key in data:
                    return str(data[key])
    except Exception:
        pass

    return default