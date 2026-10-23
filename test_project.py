import sys
import time
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By

BASE_URL = "http://localhost:8080"
ACCESS_LOG = Path("logs/access.log")
REPORT_DIR = Path("reports")
REPORT_WAIT_SECONDS = 90      # how long to wait for the monitor's report
HEADLESS = "--headless" in sys.argv   # run Chrome without a visible window
SKIP_REPORT = "--skip-report" in sys.argv

results = []


def check(name, condition, hint=""):
    """Print PASS/FAIL and remember the result."""
    if condition:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}" + (f"  -> {hint}" if hint else ""))
    results.append(bool(condition))


def log_size():
    return ACCESS_LOG.stat().st_size if ACCESS_LOG.exists() else 0


def log_text_since(offset):
    if not ACCESS_LOG.exists():
        return ""
    with open(ACCESS_LOG, "rb") as f:
        f.seek(offset)
        return f.read().decode("utf-8", errors="replace")


# ---------- Test 0: is Nginx running? ----------
try:
    requests.get(f"{BASE_URL}/success", timeout=5)
except requests.RequestException:
    print("FAIL: Nginx is not reachable at", BASE_URL)
    print("Start it with the docker run command, then try again.")
    sys.exit(1)

start_time = time.time()
log_offset = log_size()   # only look at log lines created by this test

# ---------- Test 1: status codes ----------
print("\n--- Status codes ---")
r = requests.get(f"{BASE_URL}/success")
check("/success returns 200", r.status_code == 200, f"got {r.status_code}")

r = requests.get(f"{BASE_URL}/error")
check("/error returns 500", r.status_code == 500, f"got {r.status_code}")

# ---------- Test 2: what the browser shows ----------
print("\n--- Browser (Selenium) ---")
options = webdriver.ChromeOptions()
if HEADLESS:
    options.add_argument("--headless=new")
driver = webdriver.Chrome(options=options)

try:
    driver.get(f"{BASE_URL}/success")
    body = driver.find_element(By.TAG_NAME, "body").text
    check("browser shows the 200 message", "This is a 200 response" in body, body)

    driver.get(f"{BASE_URL}/error")
    body = driver.find_element(By.TAG_NAME, "body").text
    check("browser shows the 500 message", "This is a 500 response" in body, body)
finally:
    driver.quit()

# ---------- Test 3: did Nginx write the log? ----------
print("\n--- Log file ---")
time.sleep(1)
new_logs = log_text_since(log_offset)
check("access.log has the 200 request", '"GET /success HTTP/1.1" 200' in new_logs,
      "is the logs folder mounted? (-v ...logs:/var/log/nginx)")
check("access.log has the 500 request", '"GET /error HTTP/1.1" 500' in new_logs,
      "is the logs folder mounted? (-v ...logs:/var/log/nginx)")

# ---------- Test 4: did the monitor create a report? ----------
if SKIP_REPORT:
    print("\n--- Monitor report: skipped ---")
else:
    print(f"\n--- Monitor report (waiting up to {REPORT_WAIT_SECONDS}s) ---")
    report = None
    deadline = time.time() + REPORT_WAIT_SECONDS
    while time.time() < deadline:
        if REPORT_DIR.exists():
            new_reports = [p for p in REPORT_DIR.glob("report_*.md")
                           if p.stat().st_mtime >= start_time]
            if new_reports:
                report = max(new_reports, key=lambda p: p.stat().st_mtime)
                break
        time.sleep(2)

    check("monitor created a new report", report is not None,
          "is monitor_gemini.py running in another window?")
    if report:
        size = report.stat().st_size
        check("report is not empty", size > 100, f"{size} bytes")
        print(f"Report file: {report}")

# ---------- Summary ----------
passed = sum(results)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)