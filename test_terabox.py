"""
TeraBox API Comprehensive Diagnostic Script
Tests connectivity, jsToken extraction, share/list, and share/transfer.
Run directly: python test_terabox.py
"""
import os
import re
import json
import sys
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Try cloudscraper, fallback to requests
try:
    import cloudscraper
    session = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
    )
    print("✅ Using CloudScraper")
except Exception:
    import requests
    session = requests.Session()
    print("⚠️ Using requests.Session (cloudscraper unavailable)")

ndus = os.getenv("TERABOX_NDUS_COOKIE", "")
browserid = os.getenv("TERABOX_BROWSERID", "")
csrftoken = os.getenv("TERABOX_CSRFTOKEN", "")
ndut_fmt = os.getenv("TERABOX_NDUT_FMT", "")
ndut_fmv = os.getenv("TERABOX_NDUT_FMV", "")

print(f"\n📋 Cookie Status:")
print(f"  ndus:      {ndus[:6]}...{ndus[-4:]} ({len(ndus)} chars)" if ndus else "  ndus:      NOT SET")
print(f"  browserid: {'✅ set' if browserid else '❌ NOT SET'}")
print(f"  csrfToken: {'✅ set' if csrftoken else '❌ NOT SET'}")
print(f"  ndut_fmt:  {'✅ set' if ndut_fmt else '❌ NOT SET'}")
print(f"  ndut_fmv:  {'✅ set' if ndut_fmv else '❌ NOT SET'}")

if not ndus:
    print("\n❌ TERABOX_NDUS_COOKIE not set in .env. Cannot proceed.")
    sys.exit(1)

# Build cookie string
cookie_parts = [f"ndus={ndus}"]
if ndut_fmt:
    cookie_parts.append(f"ndut_fmt={ndut_fmt}")
if ndut_fmv:
    cookie_parts.append(f"ndut_fmv={ndut_fmv}")
if browserid:
    cookie_parts.append(f"browserid={browserid}")
if csrftoken:
    cookie_parts.append(f"csrfToken={csrftoken}")
cookie_parts.append("lang=en")
cookie_str = "; ".join(cookie_parts)

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Cookie": cookie_str,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}

domains = [
    "https://www.1024terabox.com",
    "https://www.terabox.com",
    "https://dm.1024terabox.com",
    "https://1024terabox.com",
]

print("\n" + "=" * 70)
print("TEST 1: /api/list (authenticated read - same as /status)")
print("=" * 70)

working_domain = None
for domain in domains:
    h = {**headers, "Referer": domain + "/", "Origin": domain}
    url = f"{domain}/api/list"
    params = {"app_id": "250528", "channel": "dubox", "clienttype": "0", "web": "1", "dir": "/", "num": "1"}
    try:
        resp = session.get(url, params=params, headers=h, timeout=10)
        try:
            data = resp.json()
            errno = data.get("errno", "?")
            if errno == 0:
                print(f"  ✅ {domain}: errno=0 (CONNECTED)")
                working_domain = domain
            else:
                print(f"  ❌ {domain}: errno={errno}")
        except Exception:
            print(f"  ❌ {domain}: non-JSON response ({resp.status_code})")
    except Exception as e:
        print(f"  ❌ {domain}: {str(e)[:60]}")

if not working_domain:
    print("\n❌ No domain accepted your cookies. Your ndus cookie is likely expired.")
    print("   Go to TeraBox in your browser → DevTools → Application → Cookies → copy ndus")
    sys.exit(1)

print(f"\n✅ Working domain: {working_domain}")

print("\n" + "=" * 70)
print("TEST 2: jsToken Extraction")
print("=" * 70)

jstoken = ""
h = {**headers, "Referer": working_domain + "/", "Origin": working_domain}
try:
    resp = session.get(working_domain, headers=h, timeout=10)
    print(f"  Page URL: {resp.url}")
    print(f"  Status: {resp.status_code}")
    print(f"  HTML length: {len(resp.text)} chars")

    if "login" in resp.url.lower() or "passport" in resp.url.lower():
        print(f"  ⚠️ REDIRECTED TO LOGIN PAGE — cookies rejected for this domain's HTML page")
        print(f"     This means jsToken cannot be extracted. This is expected if your account")
        print(f"     was created on a different TeraBox domain.")
    else:
        match = re.search(r'fn%28%22([0-9A-Fa-f]+)%22%29', resp.text)
        if match:
            jstoken = match.group(1)
            print(f"  ✅ jsToken extracted: {jstoken[:24]}...")
        else:
            match2 = re.search(r'window\.jsToken\s*=\s*["\']([0-9A-Fa-f]+)["\']', resp.text)
            if match2:
                jstoken = match2.group(1)
                print(f"  ✅ jsToken (method 2): {jstoken[:24]}...")
            else:
                print(f"  ⚠️ jsToken NOT FOUND in HTML")
except Exception as e:
    print(f"  ❌ Error: {e}")

print("\n" + "=" * 70)
print("TEST 3: /api/list WITH jsToken")
print("=" * 70)

params = {"app_id": "250528", "channel": "dubox", "clienttype": "0", "web": "1", "dir": "/", "num": "5"}
if jstoken:
    params["jsToken"] = jstoken
h = {**headers, "Referer": working_domain + "/", "Origin": working_domain}
try:
    resp = session.get(f"{working_domain}/api/list", params=params, headers=h, timeout=10)
    data = resp.json()
    errno = data.get("errno", "?")
    file_list = data.get("list", [])
    if errno == 0:
        print(f"  ✅ errno=0, {len(file_list)} items in root")
        for item in file_list[:5]:
            name = item.get("server_filename", "?")
            isdir = "📁" if item.get("isdir") else "📄"
            print(f"     {isdir} {name}")
    else:
        print(f"  ❌ errno={errno}")
except Exception as e:
    print(f"  ❌ Error: {e}")

# Ask user if they want to test transfer
print("\n" + "=" * 70)
print("TEST 4: Share Link Transfer (WRITE operation)")
print("=" * 70)

test_url = input("  Paste a TeraBox share link to test (or press Enter to skip): ").strip()
if test_url:
    # Extract surl
    surl = ""
    if "surl=" in test_url:
        match = re.search(r"[?&]surl=([a-zA-Z0-9_-]+)", test_url)
        if match:
            surl = match.group(1)
    elif "/s/" in test_url:
        surl = test_url.split("/s/")[-1].split("?")[0].split("&")[0].split("/")[0]

    if not surl:
        print(f"  ❌ Could not extract surl from: {test_url[:60]}")
    else:
        print(f"  Extracted surl: {surl}")

        # Step 1: share/list
        list_params = {**params, "shorturl": surl, "root": "1", "page": "1", "num": "100"}
        if jstoken:
            list_params["jsToken"] = jstoken

        try:
            resp = session.get(f"{working_domain}/share/list", params=list_params, headers=h, timeout=12)
            data = resp.json()
            errno = data.get("errno", "?")
            print(f"  share/list: errno={errno}")

            if errno == 0:
                file_list = data.get("list", [])
                shareid = data.get("share_id", "")
                uk = data.get("uk", "")
                fsids = [str(f.get("fs_id", "")) for f in file_list if f.get("fs_id")]
                print(f"  shareid={shareid}, uk={uk}, fsids={fsids[:3]}")
                print(f"  Files: {[f.get('server_filename', '?') for f in file_list[:3]]}")

                # Step 2: share/transfer
                transfer_params = {**params}
                if jstoken:
                    transfer_params["jsToken"] = jstoken

                transfer_h = {**h, "Content-Type": "application/x-www-form-urlencoded"}
                transfer_data = {
                    "fsidlist": f"[{','.join(fsids)}]",
                    "path": "/Telegram_Uploads",
                    "shareid": str(shareid),
                    "from": str(uk),
                }

                resp2 = session.post(
                    f"{working_domain}/share/transfer",
                    params=transfer_params,
                    data=transfer_data,
                    headers=transfer_h,
                    timeout=25
                )
                t_data = resp2.json()
                t_errno = t_data.get("errno", "?")
                print(f"  share/transfer: errno={t_errno}")
                print(f"  Full response: {json.dumps(t_data)[:300]}")

                if t_errno == 0:
                    print(f"\n  🎉 TRANSFER SUCCEEDED!")
                elif t_errno == -10:
                    print(f"\n  ✅ File already exists in your account")
                elif t_errno == -6:
                    print(f"\n  ❌ errno=-6: Authentication failed for WRITE operation")
                    print(f"     This confirms: your cookies work for READ but fail for WRITE.")
                    print(f"     Possible causes:")
                    print(f"     1. jsToken is missing or invalid")
                    print(f"     2. Cookie was obtained from a different domain")
                    print(f"     3. TeraBox blocked this operation from server IPs")
                else:
                    print(f"\n  ❌ Transfer failed: errno={t_errno}")
            else:
                print(f"  ❌ share/list failed: errno={errno}")
                print(f"     Full: {json.dumps(data)[:200]}")
        except Exception as e:
            print(f"  ❌ Error: {e}")
else:
    print("  Skipped.")

print("\n" + "=" * 70)
print("DIAGNOSTIC SUMMARY")
print("=" * 70)
print(f"  Working domain: {working_domain or 'NONE'}")
print(f"  jsToken:        {'✅ ' + jstoken[:20] + '...' if jstoken else '❌ NOT FOUND'}")
print(f"  Read (list):    {'✅ Working' if working_domain else '❌ Failed'}")
print(f"  Write (transfer): Run test above to check")
print()
