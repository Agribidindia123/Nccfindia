# Keep all your previous imports unchanged
import requests
import json
import os
import time
import datetime
import webbrowser
import html as _html
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv
import re
import sys

# ================================================================
# Configuration
# ================================================================
BASE_DIR = Path("C:/Users/ADMIN/PyCharmMiscProject")
JSON_DIR = BASE_DIR / "Json_data" / "role_based2"
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = REPORTS_DIR / "api_test.log"
RESULT_FILE = REPORTS_DIR / "latest_results.json"

# Automatically keep only the latest 5 reports
MAX_REPORTS_TO_KEEP = 5


def cleanup_old_reports():
    try:
        reports = sorted(REPORTS_DIR.glob("api_report_*.html"), key=os.path.getmtime, reverse=True)
        for old_report in reports[MAX_REPORTS_TO_KEEP:]:
            old_report.unlink()
            print(f"🧹 Deleted old report: {old_report.name}")
    except Exception as e:
        print(f"⚠️ Cleanup failed: {e}")


# Perform cleanup on startup
cleanup_old_reports()

# Load .env if present
ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(dotenv_path=str(ENV_PATH))
else:
    load_dotenv()  # fallback to cwd

# Performance threshold (ms)
GLOBAL_PERF_THRESHOLD_MS = 1500

# Control partial message matching
ALLOW_PARTIAL_MESSAGE_MATCH = os.getenv("ALLOW_PARTIAL_MESSAGE_MATCH", "false").lower() == "true"

# Retry config for transient failures
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 2))
RETRY_DELAY = int(os.getenv("RETRY_DELAY", 2))  # seconds

# ================================================================
# Role Credentials Mapping
# ================================================================
ROLE_CREDENTIALS = {
    "admin": {
        "user": os.getenv("LOGIN_USER_ADMIN"),
        "pass": os.getenv("LOGIN_PASS_ADMIN")
    },
    "aggregator": {
        "user": os.getenv("LOGIN_USER_AGGREGATOR"),
        "pass": os.getenv("LOGIN_PASS_AGGREGATOR")
    },
    "branch": {
        "user": os.getenv("LOGIN_USER_BRANCH"),
        "pass": os.getenv("LOGIN_PASS_BRANCH")
    }
}


# ================================================================
# Tokens: fetch from /auth/login API
# ================================================================
def get_tokens_from_api(role="admin"):
    creds = ROLE_CREDENTIALS.get(role)
    if not creds or not creds["user"] or not creds["pass"]:
        print(f"❌ LOGIN_USER or LOGIN_PASS missing for role {role}")
        return {"access_token": "", "id_token": "", "refresh_token": ""}

    payload = {"username": creds["user"], "password": creds["pass"]}
    headers = {"Content-Type": "application/json"}

    print(f"🔐 Fetching tokens for {role}...")
    try:
        resp = requests.post(os.getenv("LOGIN_API_URL"), json=payload, headers=headers,
                             timeout=int(os.getenv("REQUEST_TIMEOUT", 15)))
        resp.raise_for_status()
        data = resp.json()
        print(f"✅ Tokens fetched successfully for {role}.")
        return {
            "access_token": data.get("access_token", ""),
            "id_token": data.get("id_token", ""),
            "refresh_token": data.get("refresh_token", "")
        }
    except Exception as e:
        print(f"❌ Failed to fetch tokens for {role}: {e}")
        return {"access_token": "", "id_token": "", "refresh_token": ""}


def deterministic_dummy_id_token(seed_value: str) -> str:
    return f"dummy-id-{abs(hash(seed_value)) % (10 ** 12)}"


def redact_headers(headers):
    redacted = {}
    for k, v in (headers or {}).items():
        if k and k.lower() in ("authorization", "token", "x-api-key", "x-access-token", "x-id-token"):
            redacted[k] = "***redacted***"
        else:
            redacted[k] = v
    return redacted


# ================================================================
# Logging
# ================================================================
def log_message(msg):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {msg}\n")
    print(msg)


# ================================================================
# Response Validation
# ================================================================
def validate_response_simple(resp_json, expected_response, query_content=None, test_type=None, nested_keys=None):
    errors = []

    if not isinstance(resp_json, dict):
        errors.append("Response is not a JSON object")
        return False, errors

    expected_response_dict = expected_response if isinstance(expected_response, dict) else {}

    for key in ("status", "message"):
        if key in expected_response_dict:
            expected_value = str(expected_response_dict[key]).strip()
            actual_value = str(resp_json.get(key, "")).strip()
            if ALLOW_PARTIAL_MESSAGE_MATCH:
                if expected_value.lower() not in actual_value.lower():
                    errors.append(f"Expected '{key}' to contain: {expected_value}, got: {actual_value}")
            else:
                if expected_value != actual_value:
                    errors.append(f"Expected '{key}': {expected_value}, got: {actual_value}")

    if query_content and test_type and "authentication" not in test_type.lower():
        data_list = resp_json.get("data", [])
        if isinstance(data_list, list):
            matched = any(
                query_content.lower() in str(item.get("farmer_full_name", "")).lower()
                for item in data_list if isinstance(item, dict)
            )
            if not matched:
                errors.append(f"Query content '{query_content}' not found in any farmer_full_name in response data")
        else:
            errors.append("Response 'data' is not a list")

    if nested_keys:
        for key_path, expected_value in nested_keys.items():
            keys = key_path.split(".")
            val = resp_json
            for k in keys:
                if isinstance(val, list):
                    try:
                        idx = int(k)
                        if 0 <= idx < len(val):
                            val = val[idx]
                            continue
                        else:
                            val = None
                            break
                    except ValueError:
                        val = None
                        break
                if isinstance(val, dict) and k in val:
                    val = val[k]
                else:
                    val = None
                    break
            if val is None:
                errors.append(f"Missing nested key: {key_path}")
            else:
                val_str = str(val)
                if ALLOW_PARTIAL_MESSAGE_MATCH:
                    if str(expected_value).lower() not in val_str.lower():
                        errors.append(
                            f"Nested key '{key_path}' value mismatch: expected (partial) '{expected_value}', got '{val}'")
                else:
                    if str(expected_value) != val_str:
                        errors.append(
                            f"Nested key '{key_path}' value mismatch: expected '{expected_value}', got '{val}'")

    return (len(errors) == 0), errors


def validate_headers(resp_headers, expected_headers):
    errors = []
    for key, expected_value in (expected_headers or {}).items():
        actual_value = resp_headers.get(key)
        if actual_value is None:
            errors.append(f"Missing header: {key}")
        elif str(actual_value).lower() != str(expected_value).lower():
            errors.append(f"Header '{key}' mismatch: expected '{expected_value}', got '{actual_value}'")
    return (len(errors) == 0), errors


# ================================================================
# Pre-fetch all tokens at startup
# ================================================================
ALL_TOKENS = {}
role_map = {
    "valid_admin": "admin",
    "valid_aggregator": "aggregator",
    "valid_branch": "branch",
    "valid": "admin"  # assuming 'valid' uses admin login
}
for key, role in role_map.items():
    ALL_TOKENS[key] = get_tokens_from_api(role)

# ================================================================
# Test Execution
# ================================================================
SUMMARY = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "results": [], "slow_tests": []}
PERF_STATS = []


def run_all_tests():
    overall_start = time.time()
    log_message("🔹 Starting GET API tests...\n")

    for json_file in JSON_DIR.rglob("*.json"):
        log_message(f"Loading JSON file: {json_file}")
        try:
            with open(json_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            log_message(f"❌ Failed to load JSON: {e}")
            continue

        base_url = data.get("base_url", os.getenv("BASE_URL", "")).rstrip("/")
        default_method = data.get("method", "GET").upper()
        file_headers = data.get("headers", {}) or {}
        tokens_dict = data.get("tokens", {}) or {}

        for idx, case in enumerate(data.get("test_cases", []) or [], 1):
            test_id = case.get("test_id", f"{json_file.stem}_{idx:03d}")
            desc = case.get("description", "-")
            test_type = case.get("type", "")
            method = case.get("method", default_method).upper()
            endpoint = case.get("endpoint") or case.get("api_endpoint")

            log_message(f"Processing test: {test_id}, method: {method}, endpoint: {endpoint}")
            SUMMARY["total"] += 1

            if method != "GET":
                log_message(f"⚠️ Skipping {test_id} → Non-GET method ({method})")
                SUMMARY["skipped"] += 1
                SUMMARY["results"].append({
                    "id": test_id, "desc": desc, "status_code": "-", "result": "SKIPPED",
                    "details": f"Skipped non-GET method ({method})", "api_name": f"{method} {endpoint}",
                    "method": method, "endpoint": endpoint
                })
                continue

            if not endpoint:
                log_message(f"⚠️ Skipping {test_id} → Missing endpoint")
                SUMMARY["skipped"] += 1
                SUMMARY["results"].append({
                    "id": test_id, "desc": desc, "status_code": "-", "result": "SKIPPED",
                    "details": "Missing endpoint", "api_name": "Unknown", "method": "", "endpoint": ""
                })
                continue

            headers = {**file_headers, **(case.get("headers", {}) or {}), "Accept": "application/json"}
            params = case.get("query_params", {}) or {}
            path_params = case.get("path_params", {}) or {}
            for ph in re.findall(r"\{(\w+)\}", endpoint):
                if ph in path_params:
                    endpoint = endpoint.replace(f"{{{ph}}}", str(path_params[ph]))
            url = f"{base_url}{endpoint}"

            # ---------------------------
            # JSON-driven auth token logic (use pre-fetched tokens)
            # ---------------------------
            token_key = case.get("auth_token", "valid")
            if token_key in ALL_TOKENS:
                TOKENS = ALL_TOKENS[token_key]
                headers["Authorization"] = f"Bearer {TOKENS.get('access_token', '')}"
                headers["X-ID-Token"] = TOKENS.get("id_token", "")
            elif token_key == "empty":
                headers.pop("Authorization", None)
                headers.pop("X-ID-Token", None)
            else:
                # negative tokens directly from JSON
                headers["Authorization"] = f"Bearer {tokens_dict.get(token_key, '')}"
                headers["X-ID-Token"] = deterministic_dummy_id_token(token_key)

            # ---------------------------
            # Test request & validation
            # ---------------------------
            attempt = 0
            while attempt <= MAX_RETRIES:
                start = time.perf_counter()
                try:
                    resp = requests.get(url, headers=headers, params=params, timeout=30)
                    elapsed_ms = int((time.perf_counter() - start) * 1000)
                    PERF_STATS.append(elapsed_ms)
                    status_code = resp.status_code

                    try:
                        resp_json = resp.json()
                        resp_body = json.dumps(resp_json, indent=2, ensure_ascii=False)
                    except Exception:
                        resp_json = {}
                        resp_body = resp.text or ""

                    test_passed = True
                    errors = []

                    expected_status = case.get("expected_status")
                    if expected_status and status_code != expected_status:
                        test_passed = False
                        errors.append(f"Expected {expected_status}, got {status_code}")

                    expected_resp = case.get("expected_response") or case.get("expected_response_options")
                    query_content = params.get("content")
                    nested_keys = case.get("nested_keys", {})  # nested validation
                    if expected_resp or nested_keys:
                        valid, err = validate_response_simple(resp_json, expected_resp or {},
                                                              query_content=query_content,
                                                              test_type=test_type, nested_keys=nested_keys)
                        if not valid:
                            test_passed = False
                            errors.extend(err)

                    headers_valid, header_errors = validate_headers(resp.headers, file_headers)
                    if not headers_valid:
                        test_passed = False
                        errors.extend(header_errors)

                    ct_expected = case.get("expected_content_type", "application/json")
                    if resp.headers.get("Content-Type") and ct_expected not in resp.headers.get("Content-Type"):
                        test_passed = False
                        errors.append(
                            f"Expected Content-Type '{ct_expected}', got '{resp.headers.get('Content-Type')}'")

                    slow_flag = f" → ⚠️ Slow ({elapsed_ms} > {GLOBAL_PERF_THRESHOLD_MS} ms)" if elapsed_ms > GLOBAL_PERF_THRESHOLD_MS else " → ✅ OK"
                    request_display = json.dumps(params, indent=2) if params else "-"
                    resp_body_pretty = _html.escape(resp_body)
                    error_section = f"<div style='color:red;font-weight:bold;'>Errors: {json.dumps(errors, indent=2)}</div>" if errors else ""

                    details_html = f"""
<pre>
=== Request {test_id} ===
Scenario: {desc}
URL: {method} {url}
Headers: {redact_headers(headers)}
{_html.escape(request_display)}

--- Response {test_id} --- 
Status: {status_code}
Time: {elapsed_ms} ms{slow_flag}
Body:
{resp_body_pretty}
</pre>
{error_section}
<pre>
{'✅ PASSED' if test_passed else '❌ FAILED'} {test_id}
</pre>
"""

                    SUMMARY["results"].append({
                        "id": test_id,
                        "desc": desc,
                        "status_code": status_code,
                        "result": "PASS" if test_passed else "FAIL",
                        "details": details_html,
                        "api_name": f"{method} {endpoint}",
                        "method": method,
                        "endpoint": endpoint
                    })

                    if elapsed_ms > GLOBAL_PERF_THRESHOLD_MS:
                        SUMMARY["slow_tests"].append(test_id)

                    if test_passed:
                        SUMMARY["passed"] += 1
                    else:
                        SUMMARY["failed"] += 1

                    break

                except requests.RequestException as e:
                    attempt += 1
                    log_message(f"❌ Test {test_id} attempt {attempt} failed: {e}")
                    if attempt > MAX_RETRIES:
                        SUMMARY["failed"] += 1
                        SUMMARY["results"].append({
                            "id": test_id,
                            "desc": desc,
                            "status_code": "ERROR",
                            "result": "FAIL",
                            "details": f"<pre>Exception: {_html.escape(str(e))}</pre>",
                            "api_name": f"{method} {endpoint}",
                            "method": method,
                            "endpoint": endpoint
                        })
                    else:
                        time.sleep(RETRY_DELAY)

    avg_time = int(sum(PERF_STATS) / len(PERF_STATS)) if PERF_STATS else 0
    max_time = max(PERF_STATS) if PERF_STATS else 0
    min_time = min(PERF_STATS) if PERF_STATS else 0

    log_message(f"\n🔹 All GET tests completed")
    return SUMMARY, {"avg": avg_time, "max": max_time, "min": min_time}


# HTML Report Generator
# ================================================================
def generate_html(summary, perf_stats):
    css = """
    body {font-family:'Segoe UI',Arial;margin:30px;background:#fafafa;}
    h1{color:#2c3e50;}
    h2{margin-top:40px;}
    table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;margin-bottom:30px;}
    th,td{padding:10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top;}
    th{background:#f4f6f8;}
    .pass{color:green;font-weight:bold;}
    .fail{color:red;font-weight:bold;}
    .skip{color:#856404;font-weight:bold;}
    .details{display:none;background:#f9f9f9;border-left:4px solid #2980b9;margin:6px 0;padding:10px;border-radius:6px;}
    button.toggle{cursor:pointer;background:none;border:none;color:#2980b9;font-weight:bold;}
    input[type=text]{padding:8px;width:320px;border-radius:6px;border:1px solid #ccc;margin-bottom:10px;}
    """

    script = """
    function toggle(id){var e=document.getElementById(id);e.style.display=e.style.display==='block'?'none':'block';}
    function applyFilters(){
      var s=document.getElementById('search').value.toLowerCase();
      var p=document.getElementById('pass').checked;
      var f=document.getElementById('fail').checked;
      var sk=document.getElementById('skip').checked;
      var rows=document.querySelectorAll('tbody tr');
      rows.forEach(r=>{
        var txt=r.textContent.toLowerCase();
        var res=r.getAttribute('data-res');
        var vis=(s===''||txt.includes(s));
        var resFlag=((res=='PASS'&&p)||(res=='FAIL'&&f)||(res=='SKIPPED'&&sk));
        r.style.display=(vis&&resFlag)?'':'none';
      });
    }
    """

    slow_tests_count = len(summary.get("slow_tests", []))

    # === Summary Box with bold aligned metrics ===
    html_content = f"""
    <!DOCTYPE html><html lang='en'><head><meta charset='utf-8'/>
    <title>NCCF API Test Report</title><style>{css}</style><script>{script}</script></head>
    <body>
      <h1>📊 NCCF API Test Report</h1>
      <div class="summary-box" style="display:flex; flex-direction:column; gap:8px; background:#fff; padding:15px; border-radius:8px; box-shadow:0 2px 5px rgba(0,0,0,0.1); margin-bottom:20px;">
        <div style="font-weight:bold;">⚡ Slow Tests (&gt; {GLOBAL_PERF_THRESHOLD_MS} ms): {slow_tests_count}</div>
        <div style="font-weight:bold;">⏱ Performance (ms): avg={perf_stats['avg']}, max={perf_stats['max']}, min={perf_stats['min']}</div>
        <div style="font-weight:bold;">🌐 Environment: BASE_URL={_html.escape(os.getenv('BASE_URL', ''))}</div>
      </div>
      <div style="margin-bottom:20px;">
        <input type="text" id="search" placeholder='Search by Test ID or Description' onkeyup='applyFilters()'/>
        <label><input type="checkbox" id="pass" checked onclick="applyFilters()"> Show Passed</label>
        <label><input type="checkbox" id="fail" checked onclick="applyFilters()"> Show Failed</label>
        <label><input type="checkbox" id="skip" checked onclick="applyFilters()"> Show Skipped</label>
      </div>
    """

    # Group APIs
    api_groups = defaultdict(list)
    for r in summary["results"]:
        endpoint = f"{r.get('method', 'GET')} {r.get('endpoint', '')}".strip() or r.get("api_name", "")
        endpoint = endpoint.split("?")[0]
        base_endpoint = re.sub(r"/[0-9a-fA-F-]{8,}|/\d+|/invalid-[\w-]+|/\{.*?\}", "", endpoint)
        base_endpoint = base_endpoint.rstrip("/") or "/"
        api_groups[base_endpoint].append(r)

    for api_name, tests in sorted(api_groups.items()):
        group_id = re.sub(r'[^a-zA-Z0-9]', '_', api_name).strip('_')
        html_content += f"<h2>📝 {_html.escape(api_name)}</h2>"
        html_content += "<table><thead><tr><th style='width:18%'>Test Case ID</th><th style='width:42%'>Description</th><th style='width:10%'>Status Code</th><th style='width:10%'>Result</th><th style='width:20%'>Details</th></tr></thead><tbody>"
        for i, r in enumerate(tests, 1):
            rid = f"det_{group_id}_{i}"
            color = "pass" if r["result"] == "PASS" else "fail" if r["result"] == "FAIL" else "skip"
            details_html = r.get("details", "")
            html_content += f"""
            <tr data-res="{r['result']}">
              <td>{_html.escape(str(r['id']))}</td>
              <td>{_html.escape(str(r['desc']))}</td>
              <td>{r['status_code']}</td>
              <td class="{color}">{r['result']}</td>
              <td><button class="toggle" onclick="toggle('{rid}')">▶ View</button>
              <div id="{rid}" class="details">{details_html}</div></td>
            </tr>"""
        html_content += "</tbody></table>"

    html_content += "</body></html>"
    return html_content


# ================================================================
# Entry Point
# ================================================================
if __name__ == "__main__":
    final_summary, perf_stats = run_all_tests()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    report_file = REPORTS_DIR / f"api_report_{timestamp}.html"
    html = generate_html(final_summary, perf_stats)
    report_file.write_text(html, encoding="utf-8")
    log_message(f"\n✅ HTML report generated successfully: {report_file}")
    webbrowser.open(report_file.as_uri())

    # Exit code for CI/CD
    sys.exit(1 if final_summary["failed"] > 0 else 0)
