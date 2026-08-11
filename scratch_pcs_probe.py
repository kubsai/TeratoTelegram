"""Extract PCS upload URLs from TeraBox web app JavaScript."""
import cloudscraper, re, os
from dotenv import load_dotenv
load_dotenv()

ndus = os.getenv('TERABOX_NDUS_COOKIE','')
browserid = os.getenv('TERABOX_BROWSERID','')
csrftoken = os.getenv('TERABOX_CSRFTOKEN','')
ndut_fmt = os.getenv('TERABOX_NDUT_FMT','')
ndut_fmv = os.getenv('TERABOX_NDUT_FMV','')

cookie_parts = [f"ndus={ndus}"]
if ndut_fmt: cookie_parts.append(f"ndut_fmt={ndut_fmt}")
if ndut_fmv: cookie_parts.append(f"ndut_fmv={ndut_fmv}")
if browserid: cookie_parts.append(f"browserid={browserid}")
if csrftoken: cookie_parts.append(f"csrfToken={csrftoken}")
cookie_parts.append("lang=en")
cookie_str = "; ".join(cookie_parts)

s = cloudscraper.create_scraper(browser={'browser':'chrome','platform':'windows','mobile':False})
h = {'Cookie': cookie_str, 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# Fetch the main TeraBox page and any JS bundles
print("=== Fetching dm.terabox.app/main ===")
resp = s.get('https://dm.terabox.app/main', headers=h, timeout=15)
print(f"Status: {resp.status_code}, HTML length: {len(resp.text)}")

# Search for PCS-related URLs
pcs_patterns = re.findall(r'["\']([^"\']*(?:superfile|pcs|uploadDomain|c-jp|c-\w+\.terabox|rest/2\.0)[^"\']*)["\']', resp.text)
print("\n--- PCS-related URL patterns found: ---")
for p in sorted(set(pcs_patterns))[:30]:
    print(f"  {p}")

# Search for any domain configuration  
domain_patterns = re.findall(r'["\']([^"\']*(?:terabox\.app|terabox\.com|1024terabox)[^"\']*\.(?:com|app)[^"\']*)["\']', resp.text)
print("\n--- Domain config patterns: ---")
for p in sorted(set(domain_patterns))[:30]:
    print(f"  {p}")

# Search for upload-related JS bundle URLs
js_urls = re.findall(r'src=["\']([^"\']*\.js[^"\']*)["\']', resp.text)
print(f"\n--- JS bundles found: {len(js_urls)} ---")

# Fetch the first few JS bundles and search for upload URLs
for js_url in js_urls[:5]:
    if not js_url.startswith('http'):
        js_url = 'https://dm.terabox.app' + js_url
    try:
        js_resp = s.get(js_url, headers=h, timeout=15)
        print(f"\nJS: {js_url[:80]} ({len(js_resp.text)} bytes)")
        
        # Search for upload/PCS domains in JS
        upload_domains = re.findall(r'["\']([^"\']*(?:superfile2|pcs/file|uploadDomain|c-\w+\.terabox)[^"\']*)["\']', js_resp.text)
        for ud in sorted(set(upload_domains))[:10]:
            print(f"  UPLOAD: {ud}")
        
        # Search for 'd.terabox' or 'c.terabox' patterns
        dc_patterns = re.findall(r'(?:https?://)?[dc][\w.-]*\.terabox\.\w+', js_resp.text)
        for dc in sorted(set(dc_patterns))[:10]:
            print(f"  DC: {dc}")
            
    except Exception as e:
        print(f"  ERROR: {e}")
