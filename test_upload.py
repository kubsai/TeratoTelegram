"""
Test Direct File Upload to TeraBox
Creates a small sample text file and uploads it via upload_file_to_terabox().
Run: python test_upload.py
"""
import os
import time
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from terabox_helper import upload_file_to_terabox

sample_file = "sample_test_upload.txt"
with open(sample_file, "w") as f:
    f.write(f"Hello from TeraBox Bot test upload at {time.ctime()}!\n")

print(f"📄 Created sample file: {sample_file}")
print("🔄 Starting direct upload to TeraBox account...")

result = upload_file_to_terabox(sample_file, "/Telegram_Uploads")

print("\n" + "="*60)
print("UPLOAD RESULT:")
print("="*60)
print(f"Success: {result.get('success')}")
print("Message:\n" + result.get("message", "No message"))

if os.path.exists(sample_file):
    os.remove(sample_file)
