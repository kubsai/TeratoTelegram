"""
TeraBox FULL END-TO-END Diagnostic
Tests: precreate -> superfile2 -> create, with full response dumps.
Also tests share/transfer.
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

# Also prepare a direct (no-proxy) session for uploads
import requests as req_lib
direct_session = req_lib.Session()

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

def make_headers(domain):
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Cookie": cookie_str,
        "Referer": domain + "/",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }

def make_upload_headers(domain):
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Cookie": cookie_str,
        "Referer": domain + "/",
        "Accept": "application/json, text/plain, */*",
    }

BASE_PARAMS = {"app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0"}

# ============================================================
print("\n" + "=" * 70)
print("STEP 1: Find working domain for /api/list")
print("=" * 70)
# ============================================================

domains = [
    "https://www.1024terabox.com",
    "https://www.terabox.com",
    "https://dm.1024terabox.com",
    "https://1024terabox.com",
]

working_domain = None
for domain in domains:
    h = make_headers(domain)
    try:
        resp = session.get(f"{domain}/api/list", params={**BASE_PARAMS, "dir": "/", "num": "1"}, headers=h, timeout=10)
        data = resp.json()
        errno = data.get("errno", "?")
        if errno == 0:
            print(f"  ✅ {domain}: errno=0 (CONNECTED)")
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
# Try the working domain first, fallback to www.1024terabox.com
for jsdom in [working_domain, "https://www.1024terabox.com"]:
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
        print(f"  ⚠️ No jsToken found on {jsdom} (HTML={len(resp.text)} chars, redirected={resp.url != jsdom})")
    except Exception as e:
        print(f"  ❌ {jsdom}: {e}")

params = {**BASE_PARAMS}
if jstoken:
    params["jsToken"] = jstoken

# ============================================================
print("\n" + "=" * 70)
print("STEP 3: Test /api/precreate on working domain")
print("=" * 70)
# ============================================================

# Create a test file >= 256 bytes (TeraBox is lenient for small files for precreate)
test_file = "diag_test_upload.txt"
# Write enough data to not be trivially rejected
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

h = make_headers(working_domain)
h["Content-Type"] = "application/x-www-form-urlencoded"

print(f"  File: {test_file} ({file_size} bytes, MD5={file_md5[:16]}...)")
print(f"  Remote: {remote_path}")
print(f"  Domain: {working_domain}")

try:
    resp = direct_session.post(
        f"{working_domain}/api/precreate",
        params=params,
        data=precreate_data,
        headers=h,
        timeout=20,
    )
    p_data = resp.json()
    print(f"\n  📄 FULL precreate response ({resp.status_code}):")
    print(f"  {json.dumps(p_data, indent=2)[:600]}")
    
    p_errno = p_data.get("errno", -1)
    uploadid = p_data.get("uploadid", "")
    
    # Check if precreate returned any domain hint
    for key in ["upload_url", "upload_domain", "server", "host", "uploadhost", "server_filename"]:
        val = p_data.get(key)
        if val:
            print(f"\n  🔑 Found field '{key}': {val}")
    
    if p_errno != 0:
        print(f"\n  ❌ precreate errno={p_errno}. Cannot continue upload test.")
    elif not uploadid:
        print(f"\n  ❌ precreate returned errno=0 but no uploadid!")
    else:
        print(f"\n  ✅ uploadid: {uploadid[:30]}...")

except Exception as e:
    print(f"  ❌ precreate failed: {e}")
    uploadid = ""
    p_errno = -1

# ============================================================
if uploadid:
    print("\n" + "=" * 70)
    print("STEP 4: Test superfile2 on MULTIPLE candidate domains")
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
        f"https://c-jp.terabox.com",
        f"https://c-jp.1024terabox.com",
        working_domain,  # dm.1024terabox.com
        "https://data.1024terabox.com",
        "https://data.terabox.com",
        "https://www.1024terabox.com",
        "https://www.terabox.com",
    ]
    
    upload_hdrs = make_upload_headers(working_domain)
    success_domain = None
    
    for sf_dom in superfile_candidates:
        try:
            with open(test_file, "rb") as f:
                files_payload = {"file": (os.path.basename(test_file), f)}
                resp = direct_session.post(
                    f"{sf_dom}/rest/2.0/pcs/superfile2",
                    params=upload_params,
                    files=files_payload,
                    headers=upload_hdrs,
                    timeout=30,
                )
            status = resp.status_code
            body = resp.text[:200]
            print(f"  {sf_dom}")
            print(f"    HTTP {status}: {body}")
            
            if status in [200, 201]:
                try:
                    sf_data = resp.json()
                    if sf_data.get("md5") or sf_data.get("error_code", 0) == 0:
                        print(f"    ✅ UPLOAD CHUNK ACCEPTED! md5={sf_data.get('md5', '?')}")
                        success_domain = sf_dom
                        break
                    else:
                        print(f"    ⚠️ 200 but unexpected body: {json.dumps(sf_data)[:100]}")
                except:
                    print(f"    ⚠️ 200 but non-JSON body")
            else:
                print(f"    ❌ Rejected (HTTP {status})")
        except Exception as e:
            print(f"  {sf_dom}")
            print(f"    ❌ Exception: {str(e)[:80]}")
    
    # ============================================================
    if success_domain:
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
        
        h = make_headers(working_domain)
        h["Content-Type"] = "application/x-www-form-urlencoded"
        
        try:
            resp = direct_session.post(
                f"{working_domain}/api/create",
                params=params,
                data=create_data,
                headers=h,
                timeout=20,
            )
            c_data = resp.json()
            print(f"  📄 FULL create response ({resp.status_code}):")
            print(f"  {json.dumps(c_data, indent=2)[:400]}")
            
            c_errno = c_data.get("errno", -1)
            if c_errno in [0, 17]:
                print(f"\n  🎉 FILE UPLOAD COMPLETE! errno={c_errno}")
            else:
                print(f"\n  ❌ create errno={c_errno}")
        except Exception as e:
            print(f"  ❌ create failed: {e}")
    else:
        print("\n  ❌ No superfile2 domain accepted the upload. Cannot test /api/create.")

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
        
        # Try both with and without leading '1'
        surl_candidates = [surl]
        if surl.startswith("1"):
            surl_candidates.append(surl[1:])
        else:
            surl_candidates.append("1" + surl)
        
        for s in surl_candidates:
            print(f"\n  --- Trying surl={s} ---")
            list_params = {**params, "shorturl": s, "root": "1", "page": "1", "num": "100"}
            h = make_headers(working_domain)
            
            try:
                resp = session.get(f"{working_domain}/share/list", params=list_params, headers=h, timeout=12)
                data = resp.json()
                errno = data.get("errno", "?")
                print(f"  share/list errno={errno}")
                
                if errno == 0:
                    file_list = data.get("list", [])
                    shareid = data.get("share_id", "")
                    uk = data.get("uk", "")
                    fsids = [str(f.get("fs_id", "")) for f in file_list if f.get("fs_id")]
                    print(f"  shareid={shareid}, uk={uk}")
                    print(f"  Files: {[f.get('server_filename', '?') for f in file_list[:3]]}")
                    
                    # Now transfer
                    transfer_h = make_headers(working_domain)
                    transfer_h["Content-Type"] = "application/x-www-form-urlencoded"
                    transfer_data = {
                        "fsidlist": f"[{','.join(fsids)}]",
                        "path": "/Telegram_Uploads",
                        "shareid": str(shareid),
                        "from": str(uk),
                    }
                    
                    resp2 = session.post(
                        f"{working_domain}/share/transfer",
                        params=params,
                        data=transfer_data,
                        headers=transfer_h,
                        timeout=25,
                    )
                    t_data = resp2.json()
                    t_errno = t_data.get("errno", "?")
                    print(f"  share/transfer errno={t_errno}")
                    print(f"  Full response: {json.dumps(t_data)[:300]}")
                    
                    if t_errno == 0:
                        print(f"\n  🎉 SHARE TRANSFER SUCCEEDED!")
                    elif t_errno in [-10, 17]:
                        print(f"\n  ✅ File already exists (errno={t_errno})")
                    elif t_errno == -6:
                        print(f"\n  ❌ errno=-6: Auth failed for WRITE. Cookie rejected for write ops.")
                    else:
                        print(f"\n  ❌ Transfer failed: errno={t_errno}")
                    break  # Don't try other surl variants if this one worked
                    
                elif errno == 4000020:
                    print(f"  ⚠️ errno=4000020: IP verification challenge (need verify)")
                    print(f"  Full: {json.dumps(data)[:200]}")
                else:
                    print(f"  ❌ share/list failed: errno={errno}")
                    print(f"  Full: {json.dumps(data)[:200]}")
            except Exception as e:
                print(f"  ❌ Error: {e}")
else:
    print("  Skipped.")

# Cleanup
if os.path.exists(test_file):
    os.remove(test_file)

print("\n" + "=" * 70)
print("DIAGNOSTIC COMPLETE")
print("=" * 70)
