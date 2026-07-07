"""
TeraBox FULL END-TO-END Diagnostic v2
- Uses SAME CloudScraper session for ALL operations (no bare requests.Session)
- Includes terabox.app domains (matches cookie origin)
- Retries precreate on errno=4000023 with fresh jsToken (AList pattern)
- Tests all domain variants for write operations
Run: python test_full_diagnostic.py
"""
import os
import sys
import re
import json
import hashlib
import time
import urllib.parse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import cloudscraper
    session = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
    )
    print("✅ Using CloudScraper")
except Exception:
    import requests
    session = requests.Session()
    print("⚠️ Using requests.Session")

# --- Load cookies ---
ndus = os.getenv("TERABOX_NDUS_COOKIE", "")
browserid = os.getenv("TERABOX_BROWSERID", "")
csrftoken = os.getenv("TERABOX_CSRFTOKEN", "")
ndut_fmt = os.getenv("TERABOX_NDUT_FMT", "")
ndut_fmv = os.getenv("TERABOX_NDUT_FMV", "")

if not ndus:
    print("❌ TERABOX_NDUS_COOKIE not set. Cannot proceed.")
    sys.exit(1)

cookie_parts = [f"ndus={ndus}"]
if ndut_fmt: cookie_parts.append(f"ndut_fmt={ndut_fmt}")
if ndut_fmv: cookie_parts.append(f"ndut_fmv={ndut_fmv}")
if browserid: cookie_parts.append(f"browserid={browserid}")
if csrftoken: cookie_parts.append(f"csrfToken={csrftoken}")
cookie_parts.append("lang=en")
cookie_str = "; ".join(cookie_parts)

# Also set cookies in the session's cookie jar for proper handling
for dom_suffix in [".terabox.app", ".1024terabox.com", ".terabox.com"]:
    session.cookies.set("ndus", ndus, domain=dom_suffix)
    if browserid:
        session.cookies.set("browserid", browserid, domain=dom_suffix)
    if csrftoken:
        session.cookies.set("csrfToken", csrftoken, domain=dom_suffix)
    if ndut_fmt:
        session.cookies.set("ndut_fmt", ndut_fmt, domain=dom_suffix)
    if ndut_fmv:
        session.cookies.set("ndut_fmv", ndut_fmv, domain=dom_suffix)
    session.cookies.set("lang", "en", domain=dom_suffix)

def make_headers(domain):
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Cookie": cookie_str,
        "Referer": domain + "/",
        "Origin": domain,
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }

def make_upload_headers(domain):
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Cookie": cookie_str,
        "Referer": domain + "/",
        "Origin": domain,
        "Accept": "application/json, text/plain, */*",
    }

BASE_PARAMS = {"app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0"}

# Include terabox.app domains (user cookies are from .terabox.app)
domains = [
    "https://dm.terabox.app",
    "https://www.terabox.app",
    "https://www.1024terabox.com",
    "https://www.terabox.com",
    "https://dm.1024terabox.com",
    "https://1024terabox.com",
]

# ============================================================
print("\n" + "=" * 70)
print("STEP 1: Find working domain for /api/list")
print("=" * 70)
# ============================================================

working_domain = None
for domain in domains:
    h = make_headers(domain)
    try:
        resp = session.get(f"{domain}/api/list", params={**BASE_PARAMS, "dir": "/", "num": "1"}, headers=h, timeout=10)
        data = resp.json()
        errno = data.get("errno", "?")
        if errno == 0:
            print(f"  ✅ {domain}: errno=0 (CONNECTED)")
            if not working_domain:
                working_domain = domain
        else:
            print(f"  ❌ {domain}: errno={errno}")
    except Exception as e:
        print(f"  ❌ {domain}: {str(e)[:60]}")

if not working_domain:
    print("\n❌ No domain accepted cookies. Cookie expired?")
    sys.exit(1)

print(f"\n🎯 Working domain: {working_domain}")

# ============================================================
print("\n" + "=" * 70)
print("STEP 2: Get jsToken")
print("=" * 70)
# ============================================================

jstoken = ""
# Try working domain first, then terabox.app, then 1024terabox.com
for jsdom in [working_domain, "https://www.terabox.app", "https://dm.terabox.app", "https://www.1024terabox.com"]:
    h = make_headers(jsdom)
    try:
        resp = session.get(jsdom, headers=h, timeout=10)
        match = re.search(r'fn%28%22([0-9A-Fa-f]+)%22%29', resp.text)
        if match:
            jstoken = match.group(1)
            print(f"  ✅ jsToken from {jsdom}: {jstoken[:24]}...")
            break
        match2 = re.search(r'window\.jsToken\s*=\s*["\']([0-9A-Fa-f]+)["\']', resp.text)
        if match2:
            jstoken = match2.group(1)
            print(f"  ✅ jsToken (m2) from {jsdom}: {jstoken[:24]}...")
            break
        if "login" in resp.url.lower() or "passport" in resp.url.lower():
            print(f"  ⚠️ {jsdom}: redirected to login ({resp.url[:60]})")
        else:
            print(f"  ⚠️ {jsdom}: no jsToken found (HTML={len(resp.text)} chars)")
    except Exception as e:
        print(f"  ❌ {jsdom}: {e}")

params = {**BASE_PARAMS}
if jstoken:
    params["jsToken"] = jstoken

# ============================================================
print("\n" + "=" * 70)
print("STEP 3: Test /api/precreate on ALL domains (not just working domain)")
print("=" * 70)
# ============================================================

test_file = "diag_test_upload.txt"
test_content = ("TeraBox diagnostic test file - " + time.ctime() + "\n") * 100
with open(test_file, "w") as f:
    f.write(test_content)

file_size = os.path.getsize(test_file)
file_md5 = hashlib.md5(open(test_file, "rb").read()).hexdigest()
remote_path = "/Telegram_Uploads/diag_test_upload.txt"
block_list = json.dumps([file_md5])

precreate_data = {
    "path": remote_path,
    "size": str(file_size),
    "isdir": "0",
    "autoinit": "1",
    "block_list": block_list,
}

print(f"  File: {test_file} ({file_size} bytes, MD5={file_md5[:16]}...)")
print(f"  Remote: {remote_path}")

# Test precreate on EACH domain separately (using the SAME CloudScraper session)
precreate_success_domain = None
uploadid = None

for domain in domains:
    h = make_headers(domain)
    h["Content-Type"] = "application/x-www-form-urlencoded"
    
    # First attempt
    try:
        resp = session.post(
            f"{domain}/api/precreate",
            params=params,
            data=precreate_data,
            headers=h,
            timeout=15,
        )
        p_data = resp.json()
        p_errno = p_data.get("errno", -1)
        p_uploadid = p_data.get("uploadid", "")
        
        # If 4000023, try refreshing jsToken and retry
        if p_errno == 4000023:
            print(f"  ⚠️ {domain}: errno=4000023 (need verify) — refreshing jsToken...")
            # Refresh jsToken from THIS domain
            try:
                jr = session.get(domain, headers=make_headers(domain), timeout=10)
                jm = re.search(r'fn%28%22([0-9A-Fa-f]+)%22%29', jr.text)
                if jm:
                    fresh_jstoken = jm.group(1)
                    fresh_params = {**BASE_PARAMS, "jsToken": fresh_jstoken}
                    resp2 = session.post(
                        f"{domain}/api/precreate",
                        params=fresh_params,
                        data=precreate_data,
                        headers=h,
                        timeout=15,
                    )
                    p_data = resp2.json()
                    p_errno = p_data.get("errno", -1)
                    p_uploadid = p_data.get("uploadid", "")
                    print(f"  🔄 {domain}: retry with fresh jsToken → errno={p_errno}")
            except Exception as retry_err:
                print(f"  ❌ {domain}: jsToken refresh failed: {retry_err}")
        
        if p_errno == 0 and p_uploadid:
            print(f"  ✅ {domain}: errno=0, uploadid={p_uploadid[:30]}...")
            precreate_success_domain = domain
            uploadid = p_uploadid
            # Check for any domain hints in response
            for key in ["upload_url", "upload_domain", "server", "host"]:
                val = p_data.get(key)
                if val:
                    print(f"  🔑 Found hint '{key}': {val}")
        else:
            print(f"  ❌ {domain}: errno={p_errno} ({p_data.get('errmsg', '?')})")
    except Exception as e:
        print(f"  ❌ {domain}: {str(e)[:60]}")

if precreate_success_domain:
    print(f"\n  🎯 Precreate worked on: {precreate_success_domain}")
else:
    print(f"\n  ❌ Precreate failed on ALL domains.")
    print(f"     This means TeraBox is blocking WRITE operations from this IP.")
    print(f"     errno=4000023 = jsToken/session challenge")
    print(f"     This typically happens on cloud hosting IPs (Replit, Render, etc.)")

# ============================================================
if uploadid and precreate_success_domain:
    print("\n" + "=" * 70)
    print("STEP 4: Test superfile2 on MULTIPLE PCS domains")
    print("=" * 70)
    
    upload_params = {
        **params,
        "method": "upload",
        "type": "tmpfile",
        "path": remote_path,
        "uploadid": uploadid,
        "partseq": "0",
    }
    
    superfile_candidates = [
        "https://c-jp.terabox.app",
        "https://c-jp.terabox.com",
        "https://c-jp.1024terabox.com",
        precreate_success_domain,
        "https://data.terabox.app",
        "https://data.1024terabox.com",
        "https://data.terabox.com",
    ]
    
    upload_hdrs = make_upload_headers(precreate_success_domain)
    success_sf_domain = None
    
    for sf_dom in list(dict.fromkeys(superfile_candidates)):
        try:
            with open(test_file, "rb") as f:
                files_payload = {"file": (os.path.basename(test_file), f)}
                resp = session.post(
                    f"{sf_dom}/rest/2.0/pcs/superfile2",
                    params=upload_params,
                    files=files_payload,
                    headers=upload_hdrs,
                    timeout=30,
                )
            status = resp.status_code
            body = resp.text[:150]
            print(f"  {sf_dom}")
            print(f"    HTTP {status}: {body}")
            
            if status in [200, 201]:
                try:
                    sf_data = resp.json()
                    if sf_data.get("md5") or sf_data.get("error_code", -1) == 0:
                        print(f"    ✅ UPLOAD CHUNK ACCEPTED! md5={sf_data.get('md5', '?')}")
                        success_sf_domain = sf_dom
                        break
                    else:
                        print(f"    ⚠️ 200 but unexpected: {json.dumps(sf_data)[:100]}")
                except:
                    print(f"    ⚠️ 200 but non-JSON body")
            else:
                print(f"    ❌ Rejected")
        except Exception as e:
            print(f"  {sf_dom}")
            print(f"    ❌ Exception: {str(e)[:80]}")
    
    # ============================================================
    if success_sf_domain:
        print("\n" + "=" * 70)
        print("STEP 5: Test /api/create (commit)")
        print("=" * 70)
        
        create_data = {
            "path": remote_path,
            "size": str(file_size),
            "isdir": "0",
            "uploadid": uploadid,
            "block_list": block_list,
        }
        
        h = make_headers(precreate_success_domain)
        h["Content-Type"] = "application/x-www-form-urlencoded"
        
        try:
            resp = session.post(
                f"{precreate_success_domain}/api/create",
                params=params,
                data=create_data,
                headers=h,
                timeout=20,
            )
            c_data = resp.json()
            print(f"  📄 create response ({resp.status_code}):")
            print(f"  {json.dumps(c_data, indent=2)[:400]}")
            
            c_errno = c_data.get("errno", -1)
            if c_errno in [0, 17]:
                print(f"\n  🎉 FILE UPLOAD COMPLETE! errno={c_errno}")
            else:
                print(f"\n  ❌ create errno={c_errno}")
        except Exception as e:
            print(f"  ❌ create failed: {e}")
    else:
        print("\n  ❌ No superfile2 domain accepted the upload.")

# ============================================================
print("\n" + "=" * 70)
print("STEP 6: Test share/transfer (save link to account)")
print("=" * 70)
# ============================================================

test_share = input("  Paste a TeraBox share link (or Enter to skip): ").strip()
if test_share:
    surl = ""
    if "surl=" in test_share:
        m = re.search(r"[?&]surl=([a-zA-Z0-9_-]+)", test_share)
        if m: surl = m.group(1)
    elif "/s/" in test_share:
        surl = test_share.split("/s/")[-1].split("?")[0].split("&")[0].split("/")[0]
    
    if not surl:
        print(f"  ❌ Could not extract surl from: {test_share[:60]}")
    else:
        print(f"  surl: {surl}")
        
        surl_candidates = [surl]
        if surl.startswith("1"):
            surl_candidates.append(surl[1:])
        else:
            surl_candidates.append("1" + surl)
        
        # Try share/list on ALL domains (not just working domain)
        share_success = False
        for domain in domains:
            if share_success:
                break
            for s in surl_candidates:
                list_params = {**params, "shorturl": s, "root": "1", "page": "1", "num": "100"}
                h = make_headers(domain)
                
                try:
                    resp = session.get(f"{domain}/share/list", params=list_params, headers=h, timeout=12)
                    data = resp.json()
                    errno = data.get("errno", "?")
                    
                    if errno == 0:
                        file_list = data.get("list", [])
                        shareid = data.get("share_id", "")
                        uk = data.get("uk", "")
                        fsids = [str(f.get("fs_id", "")) for f in file_list if f.get("fs_id")]
                        print(f"  ✅ share/list OK on {domain} (surl={s})")
                        print(f"     shareid={shareid}, uk={uk}")
                        print(f"     Files: {[f.get('server_filename', '?') for f in file_list[:3]]}")
                        
                        # Transfer
                        transfer_h = make_headers(domain)
                        transfer_h["Content-Type"] = "application/x-www-form-urlencoded"
                        transfer_data = {
                            "fsidlist": f"[{','.join(fsids)}]",
                            "path": "/Telegram_Uploads",
                            "shareid": str(shareid),
                            "from": str(uk),
                        }
                        
                        resp2 = session.post(
                            f"{domain}/share/transfer",
                            params=params,
                            data=transfer_data,
                            headers=transfer_h,
                            timeout=25,
                        )
                        t_data = resp2.json()
                        t_errno = t_data.get("errno", "?")
                        print(f"  share/transfer: errno={t_errno}")
                        print(f"  Response: {json.dumps(t_data)[:300]}")
                        
                        if t_errno == 0:
                            print(f"\n  🎉 SHARE TRANSFER SUCCEEDED!")
                        elif t_errno in [-10, 17]:
                            print(f"\n  ✅ File already exists (errno={t_errno})")
                        share_success = True
                        break
                        
                    elif errno == 4000020:
                        print(f"  ⚠️ {domain} surl={s}: errno=4000020 (IP challenge)")
                    elif errno == 4000023:
                        print(f"  ⚠️ {domain} surl={s}: errno=4000023 (need verify)")
                    else:
                        print(f"  ❌ {domain} surl={s}: errno={errno}")
                except Exception as e:
                    print(f"  ❌ {domain} surl={s}: {str(e)[:60]}")
        
        if not share_success:
            print(f"\n  ❌ share/list failed on all domains (IP challenge)")
else:
    print("  Skipped.")

# Cleanup
if os.path.exists(test_file):
    os.remove(test_file)

print("\n" + "=" * 70)
print("DIAGNOSTIC SUMMARY")
print("=" * 70)
print(f"  Working domain:  {working_domain or 'NONE'}")
print(f"  jsToken:         {'✅ ' + jstoken[:20] + '...' if jstoken else '❌ NOT FOUND'}")
print(f"  Read (list):     {'✅' if working_domain else '❌'}")
print(f"  Precreate:       {'✅ ' + precreate_success_domain if precreate_success_domain else '❌ BLOCKED'}")
print(f"  Base URL in env: {os.getenv('TERABOX_BASE_URL', 'NOT SET')}")
print(f"  Cookie domain:   Check your browser - should match TERABOX_BASE_URL")
print()
