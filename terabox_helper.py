import requests
import os
import time
import logging
import re
import json
import hashlib
import urllib.parse
import html as html_mod
import importlib
from typing import Optional
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

TARGET_FOLDER = os.getenv("TARGET_FOLDER_PATH", "/Telegram_Uploads")
TERABOX_BASE_URL = os.getenv("TERABOX_BASE_URL", "https://www.terabox.com")

# Active TeraBox API domains to try automatically
# Order matters: try the user's cookie-origin domain first
TERABOX_DOMAINS = [
    TERABOX_BASE_URL.rstrip("/"),
    "https://dm.terabox.app",
    "https://www.terabox.app",
    "https://dm.1024terabox.com",
    "https://www.terabox.com",
    "https://www.1024terabox.com",
    "https://1024terabox.com",
]

# Initialize CloudScraper for Cloudflare WAF & JA3 fingerprint bypass
try:
    import cloudscraper
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
    )
    logger.info("CloudScraper initialized successfully.")
except Exception as e:
    scraper = requests.Session()
# Dynamic Proxy Pool & Self-Healing 3-Tier Rotator
WEBSHARE_ROTATING_PROXY = "http://auzgnkqe-rotate:kq11xgid3jux@p.webshare.io:80/"
_proxy_pool = []
_current_proxy = None

def get_current_proxy() -> str:
    global _current_proxy
    if _current_proxy is not None:
        return _current_proxy
    
    # Try database/persistent storage first
    try:
        from database import get_setting
        db_proxy = get_setting("TERABOX_PROXY", "").strip()
        if db_proxy:
            _current_proxy = db_proxy
            scraper.proxies.update({"http": db_proxy, "https": db_proxy})
            return _current_proxy
    except Exception:
        pass

    # Try environment variable next
    env_proxy = os.getenv("TERABOX_PROXY", "").strip()
    if env_proxy:
        _current_proxy = env_proxy
        scraper.proxies.update({"http": env_proxy, "https": env_proxy})
        return _current_proxy

    # Default fallback to user's Webshare rotating setup
    _current_proxy = WEBSHARE_ROTATING_PROXY
    scraper.proxies.update({"http": _current_proxy, "https": _current_proxy})
    return _current_proxy

def set_active_proxy(proxy_url: str):
    global _current_proxy
    proxy_clean = proxy_url.strip()
    _current_proxy = proxy_clean
    if proxy_clean:
        scraper.proxies.update({"http": proxy_clean, "https": proxy_clean})
        logger.info(f"🌐 Active proxy updated to: {proxy_clean}")
    else:
        scraper.proxies.clear()
        logger.info("🌐 Proxy cleared (using direct connection)")
        
    try:
        from database import save_setting
        save_setting("TERABOX_PROXY", proxy_clean)
    except Exception as e:
        logger.debug(f"Failed to persist proxy setting: {e}")

def fetch_fresh_proxies() -> list:
    """Fetch live public HTTP proxies from community repositories."""
    global _proxy_pool
    sources = [
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    ]
    fresh = []
    for url in sources:
        try:
            r = requests.get(url, timeout=6)
            if r.status_code == 200:
                for line in r.text.splitlines():
                    p = line.strip()
                    if p and ":" in p and not p.startswith("#"):
                        if not p.startswith("http"):
                            p = f"http://{p}"
                        fresh.append(p)
                if fresh:
                    break
        except Exception as e:
            logger.warning(f"Failed fetching proxies from {url}: {e}")
    _proxy_pool = fresh[:50]
    logger.info(f"🔄 Fetched {len(_proxy_pool)} dynamic proxies into pool.")
    return _proxy_pool

def rotate_proxy() -> str:
    """Rotate to next tier or proxy when current fails."""
    global _proxy_pool, _current_proxy
    cur = get_current_proxy()
    
    # Tier 2 Fallback: If current failed and isn't Webshare Rotating yet, switch to Webshare Rotating
    if cur != WEBSHARE_ROTATING_PROXY and WEBSHARE_ROTATING_PROXY:
        logger.info("🔄 Rotating proxy to Webshare Rotating Backup IP...")
        set_active_proxy(WEBSHARE_ROTATING_PROXY)
        return WEBSHARE_ROTATING_PROXY
        
    # Tier 3 Fallback: Community Dynamic Pool
    if not _proxy_pool:
        fetch_fresh_proxies()
    if _proxy_pool:
        next_proxy = _proxy_pool.pop(0)
        set_active_proxy(next_proxy)
        return next_proxy
    return ""

# Initialize initial proxy
get_current_proxy()

# In-memory jsToken cache
_cached_jstoken = {}


def _build_cookie_string() -> str:
    """Build the full cookie string from environment variables."""
    ndus = os.getenv("TERABOX_NDUS_COOKIE", "")
    browserid = os.getenv("TERABOX_BROWSERID", "")
    csrftoken = os.getenv("TERABOX_CSRFTOKEN", "")
    ndut_fmt = os.getenv("TERABOX_NDUT_FMT", "")
    ndut_fmv = os.getenv("TERABOX_NDUT_FMV", "")
    lang = os.getenv("TERABOX_LANG", "en")

    parts = []
    if ndus:
        parts.append(f"ndus={ndus}")
    if ndut_fmt:
        parts.append(f"ndut_fmt={ndut_fmt}")
    if ndut_fmv:
        parts.append(f"ndut_fmv={ndut_fmv}")
    if browserid:
        parts.append(f"browserid={browserid}")
    if csrftoken:
        parts.append(f"csrfToken={csrftoken}")
    parts.append(f"lang={lang}")
    return "; ".join(parts)


def _sync_scraper_cookies(domain: str):
    """Sync environment cookies into scraper's cookie jar for the given domain."""
    try:
        from urllib.parse import urlparse
        hostname = urlparse(domain).hostname or "www.terabox.com"
        cookie_map = {
            "ndus": os.getenv("TERABOX_NDUS_COOKIE", ""),
            "browserid": os.getenv("TERABOX_BROWSERID", ""),
            "csrfToken": os.getenv("TERABOX_CSRFTOKEN", ""),
            "ndut_fmt": os.getenv("TERABOX_NDUT_FMT", ""),
            "ndut_fmv": os.getenv("TERABOX_NDUT_FMV", ""),
            "lang": os.getenv("TERABOX_LANG", "en"),
        }
        for name, value in cookie_map.items():
            if value:
                scraper.cookies.set(name, value, domain=hostname)
                # Also set for bare domain
                bare = hostname.replace("www.", "").replace("dm.", "")
                scraper.cookies.set(name, value, domain=f".{bare}")
    except Exception as e:
        logger.debug(f"Cookie jar sync warning: {e}")


def get_headers(domain: Optional[str] = None):
    """Build request headers. Cookie is always built fresh from env vars."""
    base_url = domain or TERABOX_BASE_URL
    cookie_str = _build_cookie_string()

    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Cookie": cookie_str,
        "Referer": base_url + "/",
        "Accept": "application/json, text/plain, */*",
        "Origin": base_url,
        "X-Requested-With": "XMLHttpRequest",
    }


def get_upload_headers(domain: Optional[str] = None):
    """Build headers for file upload requests.
    No Content-Type (let requests set multipart boundary).
    No Referer/Origin — AList doesn't set these for PCS uploads and
    cross-domain Referer causes 'user not exists' on PCS servers.
    """
    cookie_str = _build_cookie_string()

    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Cookie": cookie_str,
        "Accept": "application/json, text/plain, */*",
    }


def get_jstoken(domain: str) -> str:
    """Fetch live jsToken from TeraBox webpage HTML. Cached for 30 minutes."""
    ndus = os.getenv("TERABOX_NDUS_COOKIE", "")
    cache_key = f"{domain}_{ndus[:8] if ndus else 'none'}"
    if cache_key in _cached_jstoken:
        token, ts = _cached_jstoken[cache_key]
        if time.time() - ts < 1800:
            return token

    headers = get_headers(domain)
    try:
        _sync_scraper_cookies(domain)
        resp = scraper.get(domain, headers=headers, timeout=10)
        text = resp.text

        # Method 1: Extract from URL-encoded eval block (matches AList's approach)
        match = re.search(r'fn%28%22([0-9A-Fa-f]+)%22%29', text)
        if match:
            jstoken = match.group(1)
            _cached_jstoken[cache_key] = (jstoken, time.time())
            logger.info(f"jsToken extracted from {domain}: {jstoken[:20]}...")
            return jstoken

        # Method 2: Direct assignment
        match2 = re.search(r'window\.jsToken\s*=\s*["\']([0-9A-Fa-f]+)["\']', text)
        if match2:
            jstoken = match2.group(1)
            _cached_jstoken[cache_key] = (jstoken, time.time())
            logger.info(f"jsToken extracted (method 2) from {domain}: {jstoken[:20]}...")
            return jstoken

        # Log diagnostic info
        if "login" in resp.url.lower() or "passport" in resp.url.lower():
            logger.warning(f"jsToken: {domain} redirected to login page ({resp.url}). Cookie may be rejected by this domain.")
        else:
            logger.warning(f"jsToken: regex found nothing in {len(text)} chars from {domain}")

    except Exception as e:
        logger.warning(f"jsToken fetch failed from {domain}: {e}")

    return ""


def update_cookie(new_cookie: str):
    os.environ["TERABOX_NDUS_COOKIE"] = new_cookie
    _cached_jstoken.clear()
    # Sync to scraper cookie jar
    for domain in TERABOX_DOMAINS:
        _sync_scraper_cookies(domain)
    logger.info("TeraBox ndus cookie updated and synced to scraper.")


def extract_surl(share_url: str) -> str:
    """Extract shorturl key (surl) from any TeraBox share link variant."""
    if not share_url:
        return ""

    # Case 1: Query parameter ?surl=xxxx or &surl=xxxx
    if "surl=" in share_url:
        match = re.search(r"[?&]surl=([a-zA-Z0-9_-]+)", share_url)
        if match:
            return match.group(1)

    # Case 2: Standard path /s/1xxxx or /s/xxxx
    if "/s/" in share_url:
        return share_url.split("/s/")[-1].split("?")[0].split("&")[0].split("/")[0]

    # Case 3: Regex fallback
    match = re.search(r"/(?:s/|sharing/link\?surl=)([a-zA-Z0-9_-]+)", share_url)
    if match:
        return match.group(1)

    return ""


def _make_api_params(jstoken: str = "") -> dict:
    """Standard query params for all TeraBox API calls."""
    params = {
        "app_id": "250528",
        "web": "1",
        "channel": "dubox",
        "clienttype": "0",
    }
    if jstoken:
        params["jsToken"] = jstoken
    return params


def transfer_to_terabox(share_url: str, target_folder: Optional[str] = None) -> dict:
    """Transfer a TeraBox share link to user's account.
    
    Architecture:
    1. Find the working authenticated domain (where /api/list returns errno=0)
    2. Use a PROXY-FREE CloudScraper for share/list and share/transfer
    3. If share/list gets 4000020 (IP challenge), retry with proxy-enabled scraper
    4. share/transfer uses the SAME session that succeeded on share/list
    """
    ndus = os.getenv("TERABOX_NDUS_COOKIE", "")
    if not ndus:
        return {"success": False, "message": "❌ ndus cookie is not configured. Use /setcookie to set one."}

    folder = target_folder or os.getenv("TARGET_FOLDER_PATH", "/Telegram_Uploads")

    surl_raw = extract_surl(share_url)
    if not surl_raw:
        return {"success": False, "message": f"❌ Could not parse surl from link:\n<code>{share_url[:80]}</code>"}

    # Try both with and without leading '1'
    surl_candidates = [surl_raw]
    if surl_raw.startswith("1"):
        surl_candidates.append(surl_raw[1:])
    else:
        surl_candidates.append("1" + surl_raw)

    logger.info(f"Transfer: surl candidates={surl_candidates} from {share_url}")

    debug_info = []

    # Create a PROXY-FREE CloudScraper for transfers
    try:
        import cloudscraper as cs
        direct_scraper = cs.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
    except Exception:
        direct_scraper = requests.Session()

    # Step 0: Find the working authenticated domain
    working_domain = None
    unique_domains = list(dict.fromkeys(TERABOX_DOMAINS))

    for domain in unique_domains:
        _sync_scraper_cookies(domain)
        for c in scraper.cookies:
            direct_scraper.cookies.set_cookie(c)
        
        test_headers = get_headers(domain)
        test_params = _make_api_params()
        test_params["dir"] = "/"
        test_params["num"] = "1"
        try:
            r = direct_scraper.get(
                f"{domain}/api/list",
                params=test_params,
                headers=test_headers,
                timeout=10
            )
            d = r.json()
            if d.get("errno") == 0:
                working_domain = domain
                logger.info(f"Transfer: working domain found: {domain}")
                break
            else:
                debug_info.append(f"auth-check {domain}: errno={d.get('errno')}")
        except Exception as e:
            debug_info.append(f"auth-check {domain}: {str(e)[:50]}")

    if not working_domain:
        debug_str = html_mod.escape("\n".join(f"• {d}" for d in debug_info))
        return {
            "success": False,
            "message": (
                f"❌ <b>No authenticated domain found.</b>\n\n"
                f"<b>Debug:</b>\n<code>{debug_str}</code>\n\n"
                f"💡 Your cookie may be expired. Update via /setcookie"
            )
        }

    # Get jsToken
    jstoken = get_jstoken(working_domain)
    if not jstoken:
        for fallback_dom in ["https://www.terabox.app", "https://www.1024terabox.com", "https://www.terabox.com"]:
            jstoken = get_jstoken(fallback_dom)
            if jstoken:
                break

    params = _make_api_params(jstoken)
    headers = get_headers(working_domain)

    # Sync cookies to direct_scraper
    _sync_scraper_cookies(working_domain)
    for c in scraper.cookies:
        direct_scraper.cookies.set_cookie(c)

    # We try two sessions: direct_scraper (no proxy) and scraper (with proxy)
    sessions_to_try = [
        ("direct", direct_scraper),
        ("proxy", scraper),
    ]

    last_error = ""

    for surl in surl_candidates:
        list_params = {**params, "shorturl": surl, "root": "1", "page": "1", "num": "100"}

        for session_name, sess in sessions_to_try:
            try:
                list_resp = sess.get(
                    f"{working_domain}/share/list",
                    params=list_params,
                    headers=headers,
                    timeout=15
                )
                try:
                    list_data = list_resp.json()
                except Exception:
                    debug_info.append(f"share/list {session_name} surl={surl}: non-JSON ({list_resp.status_code})")
                    continue

                errno = list_data.get("errno", -1)
                logger.info(f"share/list ({session_name}) on {working_domain} surl={surl}: errno={errno}")

                if errno == 4000020:
                    debug_info.append(f"share/list {session_name} surl={surl}: errno=4000020 (IP challenge)")
                    continue  # Try next session

                if errno == 0:
                    file_list = list_data.get("list", [])
                    if not file_list:
                        return {"success": False, "message": "❌ Link opened but no files found inside."}

                    shareid = list_data.get("share_id", "")
                    uk = list_data.get("uk", "")
                    fsid_list = [str(item.get("fs_id", "")) for item in file_list if item.get("fs_id")]
                    first_file_name = file_list[0].get("server_filename", "files")

                    # Use the SAME session that succeeded on share/list for transfer
                    transfer_headers = get_headers(working_domain)
                    transfer_headers["Content-Type"] = "application/x-www-form-urlencoded"

                    transfer_data = {
                        "fsidlist": f"[{','.join(fsid_list)}]",
                        "path": folder,
                        "shareid": str(shareid),
                        "from": str(uk),
                    }

                    logger.info(f"share/transfer ({session_name}) on {working_domain}: shareid={shareid}, uk={uk}, fsids={fsid_list[:3]}")

                    response = sess.post(
                        f"{working_domain}/share/transfer",
                        params=params,
                        data=transfer_data,
                        headers=transfer_headers,
                        timeout=25
                    )

                    try:
                        data = response.json()
                    except Exception:
                        debug_info.append(f"transfer {session_name}: non-JSON ({response.status_code})")
                        continue

                    t_errno = data.get("errno", -1)
                    logger.info(f"share/transfer result ({session_name}): errno={t_errno}, data={json.dumps(data)[:200]}")

                    if t_errno == 0:
                        return {
                            "success": True,
                            "message": (
                                f"✅ <b>Transfer Successful!</b>\n\n"
                                f"📁 <b>File:</b> <code>{first_file_name}</code>\n"
                                f"📊 <b>Total Files:</b> {len(file_list)}\n"
                                f"📂 <b>Saved To:</b> <code>{folder}</code>\n"
                                f"🌐 <b>Domain:</b> <code>{working_domain}</code>"
                            )
                        }
                    elif t_errno in [-10, 17]:
                        return {
                            "success": True,
                            "message": (
                                f"✅ <b>File Already Saved!</b>\n\n"
                                f"📁 <b>File:</b> <code>{first_file_name}</code>\n"
                                f"📂 <b>Location:</b> <code>{folder}</code>"
                            )
                        }
                    elif t_errno == 12:
                        return {"success": False, "message": "❌ Invalid or expired share link."}
                    else:
                        debug_info.append(f"transfer {session_name}: errno={t_errno}")
                        last_error = f"errno {t_errno} on {working_domain} ({session_name})"
                else:
                    debug_info.append(f"share/list {session_name} surl={surl}: errno={errno}")
                    last_error = f"share/list errno {errno}"

            except Exception as e:
                debug_info.append(f"{session_name} {working_domain}: {str(e)[:80]}")
                last_error = str(e)

    # All attempts failed
    debug_str = html_mod.escape("\n".join(f"• {d}" for d in debug_info[-8:]))
    return {
        "success": False,
        "message": (
            f"❌ <b>Transfer failed on all attempts.</b>\n\n"
            f"<b>Last error:</b> {html_mod.escape(last_error)}\n\n"
            f"<b>Debug log:</b>\n<code>{debug_str}</code>\n\n"
            f"💡 Try updating your cookie via /setcookie"
        )
    }


def list_terabox_folders(ndus: str, path: str = "/") -> dict:
    """List folders in TeraBox account."""
    if not ndus:
        return {"success": False, "error": "No ndus cookie provided"}

    # Ensure path starts with /
    if not path:
        path = "/"
    elif not path.startswith("/"):
        path = "/" + path

    unique_domains = list(dict.fromkeys(TERABOX_DOMAINS))
    last_errno = None

    for domain in unique_domains:
        _sync_scraper_cookies(domain)
        jstoken = get_jstoken(domain)
        params = _make_api_params(jstoken)
        params["dir"] = path
        params["order"] = "time"
        params["desc"] = "1"
        params["num"] = "100"
        headers = get_headers(domain)

        try:
            response = scraper.get(f"{domain}/api/list", params=params, headers=headers, timeout=10)
            if response.status_code != 200:
                continue
            try:
                data = response.json()
            except Exception:
                continue

            errno = data.get("errno")
            last_errno = errno

            if errno == 0:
                return {"success": True, "list": data.get("list", []), "path": path, "domain": domain}
            elif errno in [-2, 2, 31066]:
                return {"success": False, "error": f"Folder <code>{path}</code> does not exist in your TeraBox account."}

        except Exception as e:
            logger.warning(f"Folder list error on {domain}: {e}")

    if last_errno in [-2, 2, 31066]:
        return {"success": False, "error": f"Folder <code>{path}</code> does not exist in your TeraBox account."}

    return {"success": False, "error": "Cookie invalid or expired. Update via /setcookie."}


def check_terabox_connectivity(ndus: str) -> dict:
    """Check if the ndus cookie is valid."""
    if not ndus:
        return {"success": False, "message": "No ndus cookie provided"}

    unique_domains = list(dict.fromkeys(TERABOX_DOMAINS))

    for domain in unique_domains:
        _sync_scraper_cookies(domain)
        jstoken = get_jstoken(domain)
        params = _make_api_params(jstoken)
        params["dir"] = "/"
        params["order"] = "time"
        params["desc"] = "1"
        params["num"] = "1"
        headers = get_headers(domain)

        try:
            response = scraper.get(f"{domain}/api/list", params=params, headers=headers, timeout=8)
            if response.status_code == 200:
                try:
                    data = response.json()
                    if data.get("errno") == 0:
                        return {
                            "success": True,
                            "message": f"Connected to {domain} (Cookie Valid)",
                            "domain": domain,
                            "errno": 0
                        }
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"Check error on {domain}: {e}")

    return {
        "success": False,
        "message": "Cookie invalid or expired across all domains. Update via /setcookie."
    }


# ===================== QR CODE LOGIN HELPERS =====================

def get_qr_login_data() -> dict:
    passport_urls = [
        "https://passport.terabox.app/v2/api/qrcode/get",
        "https://www.terabox.app/api/qrcode/get",
        "https://passport.terabox.com/v2/api/qrcode/get",
        "https://passport.1024terabox.com/v2/api/qrcode/get",
    ]
    params = {"apptype": "web", "clienttype": "0", "app_id": "250528"}
    for url in passport_urls:
        try:
            resp = scraper.get(url, params=params, headers=get_headers(), timeout=8)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if data.get("errno") == 0:
                        qr_url = data.get("data", {}).get("qrcode_url") or data.get("data", {}).get("img")
                        sign = data.get("data", {}).get("sign") or data.get("data", {}).get("token")
                        return {"success": True, "url": qr_url, "sign": sign, "raw": data}
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"QR get error on {url}: {e}")
    return {"success": False, "error": "QR login unsupported. Use /setcookie or /autologin."}


def poll_qr_login(sign: str) -> dict:
    passport_urls = [
        "https://passport.terabox.app/v2/api/qrcode/poll",
        "https://www.terabox.app/api/qrcode/poll",
        "https://passport.terabox.com/v2/api/qrcode/poll",
        "https://passport.1024terabox.com/v2/api/qrcode/poll",
    ]
    params = {"apptype": "web", "clienttype": "0", "app_id": "250528", "sign": sign}
    for url in passport_urls:
        try:
            resp = scraper.get(url, params=params, headers=get_headers(), timeout=8)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    status = data.get("data", {}).get("status") or data.get("errno")
                    if status == 2 or data.get("errno") == 0:
                        ndus_val = scraper.cookies.get("ndus") or data.get("data", {}).get("ndus")
                        if ndus_val:
                            return {"status": "confirmed", "ndus": ndus_val}
                        return {"status": "confirmed_but_no_cookie", "raw": data}
                    elif status == 1:
                        return {"status": "scanned"}
                    elif status in [-1, 257]:
                        return {"status": "expired"}
                    return {"status": "waiting"}
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"QR poll error on {url}: {e}")
    return {"status": "error", "error": "Polling failed."}


# ===================== BROWSER SANDBOX & CREDENTIAL LOGIN WORKER =====================

def login_with_browser_sandbox(username: str, password: str) -> dict:
    """
    Automated Browser Sandbox & Passport Login Worker:
    Attempts to log in to TeraBox using username and password via:
      Stage 1: Playwright Headless Browser Sandbox (if available)
      Stage 2: Selenium Headless Browser Sandbox (if available)
      Stage 3: Passport API Direct Session POST (built-in fallback engine)

    Extracts all authentication cookies: ndus, browserid, csrfToken, ndut_fmt, ndut_fmv, lang.
    """
    logger.info(f"Starting Browser Sandbox / Credentials Login for user: {username[:4]}***")

    # Stage 1: Playwright Headless Browser
    try:
        playwright_sync = importlib.import_module("playwright.sync_api")
        sync_playwright = playwright_sync.sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            page.goto("https://www.terabox.app/main", timeout=30000)
            page.wait_for_timeout(2000)

            user_input = page.query_selector('input[name="username"], input[type="text"], input[type="email"]')
            pass_input = page.query_selector('input[name="password"], input[type="password"]')

            if user_input and pass_input:
                user_input.fill(username)
                pass_input.fill(password)
                btn = page.query_selector('button[type="submit"], .login-btn, input[type="submit"]')
                if btn:
                    btn.click()
                    page.wait_for_timeout(5000)

            cookies_list = context.cookies()
            browser.close()

            cookie_dict = {c['name']: c['value'] for c in cookies_list}
            if "ndus" in cookie_dict:
                logger.info("Playwright sandbox login successful!")
                return {"success": True, "cookies": cookie_dict, "method": "Playwright Headless Browser"}
    except Exception as e:
        logger.warning(f"Playwright sandbox login unavailable/failed: {e}")

    # Stage 2: Selenium Headless Browser
    try:
        selenium_driver = importlib.import_module("selenium.webdriver")
        chrome_opts = importlib.import_module("selenium.webdriver.chrome.options")
        webdriver = selenium_driver
        Options = chrome_opts.Options
        options = Options()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

        driver = webdriver.Chrome(options=options)
        driver.get("https://www.terabox.app/main")
        time.sleep(3)

        try:
            user_input = driver.find_element("name", "username")
            pass_input = driver.find_element("name", "password")
            if user_input and pass_input:
                user_input.send_keys(username)
                pass_input.send_keys(password)
                pass_input.submit()
                time.sleep(5)
        except Exception:
            pass

        raw_cookies = driver.get_cookies()
        driver.quit()

        cookie_dict = {c['name']: c['value'] for c in raw_cookies}
        if "ndus" in cookie_dict:
            logger.info("Selenium sandbox login successful!")
            return {"success": True, "cookies": cookie_dict, "method": "Selenium Headless Browser"}
    except Exception as e:
        logger.warning(f"Selenium sandbox login unavailable/failed: {e}")

    # Stage 3: Passport API Direct Session POST (Built-in Fallback)
    passport_targets = [
        ("https://www.terabox.app", "/v2/api"),
        ("https://dm.terabox.app", "/v2/api"),
        ("https://passport.terabox.com", "/v2/api"),
        ("https://www.terabox.com", "/v2/api"),
        ("https://www.terabox.app", "/passport/v2/api"),
        ("https://dm.terabox.app", "/passport/v2/api"),
        ("https://www.1024terabox.com", "/v2/api"),
        ("https://dm.1024terabox.com", "/v2/api"),
    ]

    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
    }

    import requests
    req_sess = requests.Session()

    for base_domain, api_prefix in passport_targets:
        try:
            # 1. Get initial token
            t_url = f"{base_domain}{api_prefix}/getapi"
            params = {"tpl": "netdisk", "apiver": "v3", "clienttype": "0", "app_id": "250528"}
            req_sess.get(t_url, params=params, headers=hdrs, timeout=8)

            # 2. Login POST
            l_url = f"{base_domain}{api_prefix}/login"
            payload = {
                "username": username,
                "password": password,
                "tpl": "netdisk",
                "apiver": "v3",
                "clienttype": "0",
                "app_id": "250528",
                "isphone": "0" if "@" in username else "1"
            }
            resp = req_sess.post(l_url, data=payload, headers=hdrs, timeout=12)
            c_dict = req_sess.cookies.get_dict()
            if "ndus" in c_dict:
                logger.info(f"Passport API login successful on {base_domain}!")
                return {"success": True, "cookies": c_dict, "method": f"Passport API ({base_domain})"}

        except Exception as e:
            logger.warning(f"Passport API login error on {base_domain}{api_prefix}: {e}")

    return {
        "success": False,
        "error": "Automated login failed across all browser sandbox & API engines. Please verify credentials or check if TeraBox requires SMS/CAPTCHA verification for your account."
    }


# ===================== DIRECT FILE UPLOAD HELPER =====================

def get_file_md5(file_path: str) -> str:
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def upload_file_to_terabox(file_path: str, target_folder: Optional[str] = None) -> dict:
    """Upload a local file to TeraBox via PCS upload API.
    
    Architecture:
    1. Find the working authenticated domain (the one where /api/list returns errno=0)
    2. Use that domain for precreate and create (commit)
    3. Use c-jp.terabox.com (AList-confirmed PCS upload cluster) for superfile2
    4. Use a PROXY-FREE CloudScraper (needs TLS fingerprint to bypass Cloudflare)
    """
    ndus = os.getenv("TERABOX_NDUS_COOKIE", "")
    if not ndus:
        return {"success": False, "message": "❌ ndus cookie not configured. Use /setcookie."}

    if not os.path.exists(file_path):
        return {"success": False, "message": "❌ Local file not found."}

    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    file_md5 = get_file_md5(file_path)
    folder = target_folder or os.getenv("TARGET_FOLDER_PATH", "/Telegram_Uploads")
    remote_path = f"{folder.rstrip('/')}/{file_name}"
    block_list_json = json.dumps([file_md5])

    debug_info = []

    # Create a PROXY-FREE CloudScraper for uploads
    # (bare requests.Session gets blocked by Cloudflare TLS fingerprinting)
    try:
        import cloudscraper as cs
        upload_scraper = cs.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
    except Exception:
        upload_scraper = requests.Session()

    # Step 0: Find the working authenticated domain
    working_domain = None
    unique_domains = list(dict.fromkeys(TERABOX_DOMAINS))

    for domain in unique_domains:
        _sync_scraper_cookies(domain)
        # Copy cookies to upload_scraper
        for c in scraper.cookies:
            upload_scraper.cookies.set_cookie(c)

        test_headers = get_headers(domain)
        test_params = _make_api_params()
        test_params["dir"] = "/"
        test_params["num"] = "1"
        try:
            r = upload_scraper.get(
                f"{domain}/api/list",
                params=test_params,
                headers=test_headers,
                timeout=10
            )
            d = r.json()
            if d.get("errno") == 0:
                working_domain = domain
                logger.info(f"Upload: working domain found: {domain}")
                break
            else:
                debug_info.append(f"auth-check {domain}: errno={d.get('errno')}")
        except Exception as e:
            debug_info.append(f"auth-check {domain}: {str(e)[:50]}")

    if not working_domain:
        debug_str = html_mod.escape("\n".join(f"• {d}" for d in debug_info))
        return {
            "success": False,
            "message": (
                f"❌ <b>No authenticated domain found.</b>\n\n"
                f"<b>Debug:</b>\n<code>{debug_str}</code>\n\n"
                f"💡 Your cookie may be expired. Update via /setcookie"
            )
        }

    # Get jsToken from the working domain (or fallback domains)
    jstoken = get_jstoken(working_domain)
    if not jstoken:
        for fallback_dom in ["https://www.terabox.app", "https://www.1024terabox.com", "https://www.terabox.com"]:
            jstoken = get_jstoken(fallback_dom)
            if jstoken:
                break

    params = _make_api_params(jstoken)

    # Sync cookies to upload_scraper for the working domain
    _sync_scraper_cookies(working_domain)
    for c in scraper.cookies:
        upload_scraper.cookies.set_cookie(c)

    api_hdrs = get_headers(working_domain)
    api_hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    upload_hdrs = get_upload_headers(working_domain)

    # Step 1: Precreate on the working domain
    precreate_data = {
        "path": remote_path,
        "size": str(file_size),
        "isdir": "0",
        "autoinit": "1",
        "block_list": block_list_json,
    }

    # Try precreate — if errno=4000023, refresh jsToken and retry (AList pattern)
    p_data = None
    for attempt in range(2):
        try:
            p_resp = upload_scraper.post(
                f"{working_domain}/api/precreate",
                params=params,
                data=precreate_data,
                headers=api_hdrs,
                timeout=20
            )
            p_data = p_resp.json()
        except Exception as e:
            return {
                "success": False,
                "message": f"❌ <b>Precreate failed:</b> {str(e)[:80]}\n💡 Update cookie via /setcookie"
            }

        p_errno = p_data.get("errno", -1)
        uploadid = p_data.get("uploadid", "")
        logger.info(f"precreate {working_domain} (attempt {attempt+1}): errno={p_errno}, uploadid={uploadid[:20] if uploadid else 'none'}")

        if p_errno == 4000023 and attempt == 0:
            # jsToken stale — refresh and retry (AList handles 4000023 the same way)
            logger.info("errno=4000023: refreshing jsToken and retrying precreate...")
            jstoken = get_jstoken(working_domain)
            if not jstoken:
                for fb in ["https://www.terabox.app", "https://www.1024terabox.com"]:
                    jstoken = get_jstoken(fb)
                    if jstoken:
                        break
            params = _make_api_params(jstoken)
            continue
        break

    if p_errno != 0 or not uploadid:
        return {
            "success": False,
            "message": (
                f"❌ <b>Precreate rejected (errno={p_errno}).</b>\n"
                f"<code>{html_mod.escape(json.dumps(p_data)[:200])}</code>\n\n"
                f"💡 Update cookie via /setcookie"
            )
        }

    # Step 2: Upload file chunk via superfile2
    # AList uses c-{prefix}.terabox.com where prefix is "jp"
    # We try multiple PCS-capable domains
    upload_params = {
        **params,
        "method": "upload",
        "type": "tmpfile",
        "path": remote_path,
        "uploadid": uploadid,
        "partseq": "0",
    }

    # PCS upload candidates — ordered by likelihood of success
    # c-jp.terabox.com/app is what AList uses (url_domain_prefix = "jp")
    pcs_candidates = [
        "https://c-jp.terabox.app",
        "https://c-jp.terabox.com",
        "https://c-jp.1024terabox.com",
        working_domain,
        "https://data.terabox.app",
        "https://data.1024terabox.com",
        "https://data.terabox.com",
    ]

    u_resp = None
    sf_debug = []
    for pcs_dom in list(dict.fromkeys(pcs_candidates)):
        # Re-sync cookies for each candidate
        for c in scraper.cookies:
            upload_scraper.cookies.set_cookie(c)
        try:
            with open(file_path, "rb") as f:
                files_payload = {"file": (file_name, f)}
                u_resp = upload_scraper.post(
                    f"{pcs_dom}/rest/2.0/pcs/superfile2",
                    params=upload_params,
                    files=files_payload,
                    headers=upload_hdrs,
                    timeout=60
                )
            status = u_resp.status_code
            body_preview = u_resp.text[:80]
            sf_debug.append(f"{pcs_dom}: HTTP {status} ({body_preview})")

            if status in [200, 201]:
                try:
                    sf_data = u_resp.json()
                    if sf_data.get("md5") or sf_data.get("error_code", -1) == 0:
                        logger.info(f"superfile2 accepted on {pcs_dom}: {json.dumps(sf_data)[:100]}")
                        break
                except Exception:
                    pass
                # Even if not JSON, a 200 may mean success
                logger.info(f"superfile2 HTTP 200 on {pcs_dom}")
                break
            else:
                u_resp = None  # Reset so we know it didn't succeed
        except Exception as err:
            sf_debug.append(f"{pcs_dom}: err={str(err)[:60]}")
            logger.warning(f"superfile2 {pcs_dom}: {err}")

    if not u_resp or u_resp.status_code not in [200, 201]:
        sf_str = html_mod.escape("\n".join(f"• {s}" for s in sf_debug))
        return {
            "success": False,
            "message": (
                f"❌ <b>superfile2 upload failed on all PCS domains.</b>\n\n"
                f"<b>Debug:</b>\n<code>{sf_str}</code>\n\n"
                f"💡 This may be a Cloudflare/IP block. Try from a different network."
            )
        }

    # Step 3: Create (commit) on the working domain
    create_data = {
        "path": remote_path,
        "size": str(file_size),
        "isdir": "0",
        "uploadid": uploadid,
        "block_list": block_list_json,
    }

    try:
        c_resp = upload_scraper.post(
            f"{working_domain}/api/create",
            params=params,
            data=create_data,
            headers=api_hdrs,
            timeout=20
        )
        c_data = c_resp.json()
    except Exception as e:
        return {
            "success": False,
            "message": f"❌ <b>Create (commit) failed:</b> {str(e)[:80]}"
        }

    c_errno = c_data.get("errno", -1)
    logger.info(f"create {working_domain}: errno={c_errno}, data={json.dumps(c_data)[:200]}")

    if c_errno in [0, -10, 17]:
        size_mb = file_size / (1024 * 1024)
        return {
            "success": True,
            "message": (
                f"✅ <b>File Uploaded to TeraBox!</b>\n\n"
                f"📄 <b>File:</b> <code>{file_name}</code>\n"
                f"📊 <b>Size:</b> {size_mb:.2f} MB\n"
                f"📂 <b>Folder:</b> <code>{folder}</code>\n"
                f"🌐 <b>Domain:</b> <code>{working_domain}</code>"
            )
        }
    else:
        return {
            "success": False,
            "message": (
                f"❌ <b>Create (commit) rejected: errno={c_errno}</b>\n"
                f"<code>{html_mod.escape(json.dumps(c_data)[:200])}</code>\n\n"
                f"💡 Update cookie via /setcookie"
            )
        }