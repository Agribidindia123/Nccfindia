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

# ================================================================
# Configuration
# ================================================================
BASE_DIR = Path("C:/Users/ADMIN/PyCharmMiscProject")
JSON_DIR = BASE_DIR / "Json_data" / "only get api"
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

RESULT_FILE = REPORTS_DIR / "latest_results.json"

# Load .env if present
ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(dotenv_path=str(ENV_PATH))
else:
    load_dotenv()  # fallback to cwd

# Performance threshold (ms)
GLOBAL_PERF_THRESHOLD_MS = 1500

# ================================================================
# Tokens: fetch from /auth/login API
# ================================================================
def get_tokens_from_api():
    """Fetch tokens using /auth/login API."""
    login_url = os.getenv("LOGIN_API_URL", "http://34.47.192.60/auth/login")
    payload = {
        "username": os.getenv("LOGIN_USER"),
        "password": os.getenv("LOGIN_PASS")
    }

    if not payload["username"] or not payload["password"]:
        print("❌ LOGIN_USER or LOGIN_PASS missing in .env")
        return {"access_token": "", "id_token": "", "refresh_token": ""}

    headers = {"Content-Type": "application/json"}

    print("🔐 Calling /auth/login API to fetch tokens...")
    try:
        resp = requests.post(login_url, json=payload, headers=headers, timeout=int(os.getenv("REQUEST_TIMEOUT", 15)))
        resp.raise_for_status()
        data = resp.json()
        print("✅ Tokens fetched successfully from API.")
        return {
            "access_token": data.get("access_token", ""),
            "id_token": data.get("id_token", ""),
            "refresh_token": data.get("refresh_token", "")
        }
    except Exception as e:
        print(f"❌ Failed to fetch tokens: {e}")
        return {"access_token": "", "id_token": "", "refresh_token": ""}

def deterministic_dummy_id_token(seed_value: str) -> str:
    return f"dummy-id-{abs(hash(seed_value)) % (10**12)}"

def redact_headers(headers):
    redacted = {}
    for k, v in (headers or {}).items():
        if k and k.lower() in ("authorization", "token", "x-api-key", "x-access-token", "x-id-token"):
            redacted[k] = "***redacted***"
        else:
            redacted[k] = v
    return redacted

def validate_response_simple(resp_json, expected_response):
    errors = []
    if isinstance(expected_response, dict):
        data_dict = resp_json.get("data", {})
        mandatory_fields = expected_response.get("mandatory_fields", [])
        for field in mandatory_fields:
            if field not in data_dict:
                errors.append(f"Missing mandatory field: {field}")
        for k, v_type in (expected_response.get("field_types", {}) or {}).items():
            value = data_dict.get(k)
            if v_type == "string" and value is not None and not isinstance(value, str):
                errors.append(f"Field '{k}' expected string, got {type(value).__name__}")
            if v_type == "boolean" and not isinstance(value, bool):
                errors.append(f"Field '{k}' expected boolean, got {type(value).__name__}")
    return (len(errors) == 0), errors

# ================================================================
# Run all GET tests
# ================================================================
SUMMARY = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "results": []}

def run_all_tests():
    TOKENS = get_tokens_from_api()
    overall_start = time.time()

    print("🔹 Starting GET API tests...\n")

    for json_file in JSON_DIR.rglob("*.json"):
        print(f"Loading JSON file: {json_file}")
        try:
            with open(json_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            print(f"❌ Failed to load JSON: {e}")
            continue

        base_url = data.get("base_url", os.getenv("BASE_URL", "")).rstrip("/")
        default_method = data.get("method", "GET").upper()
        file_headers = data.get("headers", {}) or {}
        tokens_dict = data.get("tokens", {}) or {}

        if TOKENS.get("access_token"):
            tokens_dict["valid"] = TOKENS["access_token"]
        if TOKENS.get("id_token"):
            tokens_dict["valid_id"] = TOKENS["id_token"]

        for idx, case in enumerate(data.get("test_cases", []) or [], 1):
            test_id = case.get("test_id", f"{json_file.stem}_{idx:03d}")
            desc = case.get("description", "-")
            method = case.get("method", default_method).upper()
            endpoint = case.get("endpoint") or case.get("api_endpoint")

            print(f"Processing test: {test_id}, method: {method}, endpoint: {endpoint}")

            SUMMARY["total"] += 1

            if not endpoint:
                print(f"⚠️ Skipping {test_id} → Missing endpoint")
                SUMMARY["skipped"] += 1
                SUMMARY["results"].append({
                    "id": test_id, "desc": desc, "status_code": "-", "result": "SKIPPED",
                    "details": "Missing endpoint", "api_name": "Unknown", "method": "", "endpoint": ""
                })
                continue

            if method != "GET":
                print(f"⚠️ Skipping {test_id} → Non-GET method ({method})")
                SUMMARY["skipped"] += 1
                SUMMARY["results"].append({
                    "id": test_id, "desc": desc, "status_code": "-", "result": "SKIPPED",
                    "details": f"Skipped non-GET method ({method})", "api_name": f"{method} {endpoint}",
                    "method": method, "endpoint": endpoint
                })
                continue

            headers = {**file_headers, **(case.get("headers", {}) or {}), "Accept": "application/json"}
            params = case.get("query_params", {}) or {}

            # Replace path parameters in endpoint
            path_params = case.get("path_params", {}) or {}
            for ph in re.findall(r"\{(\w+)\}", endpoint):
                if ph in path_params:
                    endpoint = endpoint.replace(f"{{{ph}}}", str(path_params[ph]))

            url = f"{base_url}{endpoint}"

            # Auth token
            token_key = case.get("auth_token", "valid")
            access_token_value = tokens_dict.get(token_key, "")
            id_token_key = f"{token_key}_id"
            id_token_value = tokens_dict.get(id_token_key)
            if token_key != "empty" and not id_token_value:
                seed = f"{json_file}:{token_key}:{access_token_value}"
                id_token_value = deterministic_dummy_id_token(seed)
                tokens_dict[id_token_key] = id_token_value

            if token_key == "empty":
                headers.pop("Authorization", None)
                headers.pop("X-ID-Token", None)
            else:
                if access_token_value:
                    headers["Authorization"] = f"Bearer {access_token_value}"
                if id_token_value:
                    headers["X-ID-Token"] = id_token_value

            start = time.perf_counter()
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
                elapsed_ms = int((time.perf_counter() - start) * 1000)
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
                if expected_resp:
                    valid, err = validate_response_simple(resp_json, expected_resp)
                    if not valid:
                        test_passed = False
                        errors.extend(err)

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

                if test_passed:
                    SUMMARY["passed"] += 1
                else:
                    SUMMARY["failed"] += 1

            except Exception as e:
                elapsed_ms = int((time.perf_counter() - start) * 1000)
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
                print(f"❌ Test {test_id} failed: {e}")

    overall_duration = int((time.time() - overall_start) * 1000)
    print(f"\n🔹 All GET tests completed in {overall_duration} ms")
    print(f"Total: {SUMMARY['total']}, Passed: {SUMMARY['passed']}, Failed: {SUMMARY['failed']}, Skipped: {SUMMARY['skipped']}")
    return SUMMARY

# ================================================================
# ... everything above remains exactly the same ...

# ================================================================
# HTML Report
# ================================================================
def generate_html(summary):
    css = """
    body {font-family:'Segoe UI',Arial;margin:30px;background:#fafafa;}
    h1{color:#2c3e50;}
    h2{margin-top:40px;}
    table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;margin-bottom:30px;}
    th,td{padding:10px;border-bottom:1px solid #eee;text-align:left;}
    th{background:#f4f6f8;}
    .pass{color:green;font-weight:bold;}
    .fail{color:red;font-weight:bold;}
    .skip{color:#856404;font-weight:bold;}
    .details{display:none;background:#f9f9f9;border-left:4px solid #2980b9;margin:6px 0;padding:10px;border-radius:6px;}
    button.toggle{cursor:pointer;background:none;border:none;color:#2980b9;font-weight:bold;}
    input[type=text]{padding:8px;width:280px;border-radius:6px;border:1px solid #ccc;margin-bottom:10px;}
    label{margin-right:10px;}
    """
    script = """
    function toggle(id){
      var e=document.getElementById(id);
      e.style.display=e.style.display==='block'?'none':'block';
    }
    function applyFilters(){
      var s=document.getElementById('search').value.toLowerCase();
      var p=document.getElementById('pass').checked;
      var f=document.getElementById('fail').checked;
      var sk=document.getElementById('skip').checked;
      var rows=document.querySelectorAll('tbody tr');
      rows.forEach(r=>{
        var res=r.getAttribute('data-res');
        var txt=r.textContent.toLowerCase();
        var vis=txt.includes(s)&&((res=='PASS'&&p)||(res=='FAIL'&&f)||(res=='SKIPPED'&&sk));
        r.style.display=vis?'':'none';
      });
    }
    """
    api_groups = defaultdict(list)
    for r in summary["results"]:
        method = r.get("method", "")
        endpoint = r.get("endpoint", "")
        base_endpoint = re.sub(r"/[0-9a-fA-F-]{8,}|/invalid-[\\w-]+|/\\{.*?\\}", "", endpoint)
        api_name = f"{method} {base_endpoint}"
        api_groups[api_name].append(r)

    html_content = f"""
    <!DOCTYPE html><html lang='en'><head><meta charset='utf-8'/>
    <title>NCCF API Test Report</title><style>{css}</style><script>{script}</script></head>
    <body>
      <h1>📊 NCCF API Test Report</h1>

      <input type='text' id='search' placeholder='Search by Test ID or Description' onkeyup='applyFilters()'/>
      <div>
        <label><input type='checkbox' id='pass' checked onchange='applyFilters()'/> Show Passed</label>
        <label><input type='checkbox' id='fail' checked onchange='applyFilters()'/> Show Failed</label>
        <label><input type='checkbox' id='skip' checked onchange='applyFilters()'/> Show Skipped</label>
      </div>
    """
    for api_name, tests in api_groups.items():
        html_content += f"<h2>📝 {_html.escape(api_name)}</h2>"
        html_content += "<table><thead><tr><th>Test Case ID</th><th>Description</th><th>Status Code</th><th>Result</th><th>Details</th></tr></thead><tbody>"
        for i, r in enumerate(tests, 1):
            rid = f"det_{api_name}_{i}"
            color = "pass" if r["result"]=="PASS" else "fail" if r["result"]=="FAIL" else "skip"
            html_content += f"""
            <tr data-res="{r['result']}">
              <td>{_html.escape(str(r['id']))}</td>
              <td>{_html.escape(str(r['desc']))}</td>
              <td>{r['status_code']}</td>
              <td class="{color}">{r['result']}</td>
              <td><button class="toggle" onclick="toggle('{rid}')">▶ View</button>
              <div id="{rid}" class="details">{r['details']}</div></td>
            </tr>"""
        html_content += "</tbody></table>"
    html_content += "</body></html>"
    return html_content

# ================================================================
# Entry point
# ================================================================
if __name__ == "__main__":
    final_summary = run_all_tests()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    report_file = REPORTS_DIR / f"api_report_{timestamp}.html"
    html = generate_html(final_summary)
    report_file.write_text(html, encoding="utf-8")
    print(f"\n✅ HTML report generated successfully: {report_file}")
    webbrowser.open(report_file.as_uri())
