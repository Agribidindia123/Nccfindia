import pytest
import requests
import json
from pathlib import Path
import datetime
import webbrowser
import re
import time
import html as _html
import os
from jsonschema import validate as json_validate, ValidationError  # ✅ NEW
import shutil

# -----------------------
# Paths
# -----------------------
BASE_DIR = Path("C:/Users/HP/PycharmProjects/PythonProject")
JSON_DIR = BASE_DIR / "Json_data" / "All 88 jason"
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

# Optional: clean old reports (keep last 5)
old_reports = sorted(REPORTS_DIR.glob("api_report_*.html"))
if len(old_reports) > 5:
    for rpt in old_reports[:-5]:
        rpt.unlink()

# -----------------------
# Summary for CI/CD
# -----------------------
SUMMARY = {
    "total": 0,
    "passed": 0,
    "failed": 0,
    "skipped": 0,
    "times": []
}

GLOBAL_PERF_THRESHOLD_MS = 1500  # default 1.5 sec

# -----------------------
# Load JSON files dynamically
# -----------------------
def load_json_files():
    json_files = list(JSON_DIR.glob("*.json"))
    test_data = []
    for jf in json_files:
        with open(jf, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                test_data.append((jf.stem, data))
            except json.JSONDecodeError:
                print(f"⚠️ Skipping invalid JSON file: {jf.name}")
    return test_data

all_json_data = load_json_files()

# -----------------------
# Redact sensitive headers
# -----------------------
def redact_headers(headers):
    redacted = {}
    for k, v in headers.items():
        if k.lower() in ("authorization", "token", "x-api-key", "x-access-token"):
            redacted[k] = "***redacted***"
        else:
            redacted[k] = v
    return redacted

# -----------------------
# NEW: Schema validation using jsonschema
# -----------------------
def validate_schema(test_id, resp_json, schema):
    """
    Validates the API response against a provided JSON Schema.
    """
    try:
        json_validate(instance=resp_json, schema=schema)
    except ValidationError as e:
        assert False, f"{test_id} | Schema validation failed: {e.message}"

# -----------------------
# Existing Manual Validation
# -----------------------
def validate_response(test_id, resp_json, expected_response_options):
    """
    Validates API response against JSON test definitions.
    Supports:
    - mandatory_fields presence
    - field_types checks
    - expected messages
    """
    errors = []

    if not isinstance(expected_response_options, list):
        expected_response_options = [expected_response_options]

    matched = False

    for expected_response in expected_response_options:
        temp_errors = []

        if isinstance(expected_response, dict):
            data_dict = resp_json.get("data", {})

            # ✅ NEW: Schema validation (if defined)
            if "schema" in expected_response:
                validate_schema(test_id, resp_json, expected_response["schema"])

            # Mandatory fields
            mandatory_fields = expected_response.get("mandatory_fields", [])
            for field in mandatory_fields:
                if field not in data_dict:
                    temp_errors.append(f"Missing mandatory field: {field}")

            # Field types
            field_types = expected_response.get("field_types", {})
            for k, v_type in field_types.items():
                value = data_dict.get(k)
                if v_type == "string" and value is not None and not isinstance(value, str):
                    temp_errors.append(f"Field '{k}' expected string, got {type(value).__name__}")
                elif v_type == "boolean" and not isinstance(value, bool):
                    temp_errors.append(f"Field '{k}' expected boolean, got {type(value).__name__}")
                elif v_type == "null|string" and value is not None and not isinstance(value, str):
                    temp_errors.append(f"Field '{k}' expected null|string, got {type(value).__name__}")

            if not temp_errors:
                matched = True
                break
            else:
                errors.extend(temp_errors)

        elif isinstance(expected_response, str):
            if expected_response in json.dumps(resp_json):
                matched = True
                break
            else:
                errors.append(f"Expected message not found: {expected_response}")

    assert matched, f"{test_id} | Response validation failed: {errors}"

# -----------------------
# Dynamic API Test (main test)
# -----------------------
@pytest.mark.parametrize("filename, data", all_json_data, ids=[name for name, _ in all_json_data])
def test_api_from_json(filename, data, request):
    base_url = data.get("base_url", "").rstrip("/")
    assert base_url, f"{filename}.json must have 'base_url'"

    for idx, case in enumerate(data.get("test_cases", []), 1):
        SUMMARY["total"] += 1
        test_id = case.get("test_id", f"{filename}_{idx:03d}")
        endpoint = case.get("endpoint") or case.get("api_endpoint")
        if not endpoint:
            SUMMARY["skipped"] += 1
            pytest.skip(f"{test_id} skipped: 'endpoint' missing in JSON")

        method = case.get("method", data.get("method", "POST")).upper()
        headers = {**data.get("headers", {}), **case.get("headers", {}), "Accept": "application/json"}
        body = case.get("body") or case.get("request_body") or case.get("payload") or {}
        params = case.get("query_params", {})

        # Replace placeholders
        path_params = case.get("path_params", {})
        for ph in re.findall(r"\{(\w+)\}", endpoint):
            if ph in path_params:
                endpoint = endpoint.replace(f"{{{ph}}}", str(path_params[ph]))
        url = f"{base_url}{endpoint}"

        # Token handling
        token_key = case.get("auth_token", "valid")
        token_value = data.get("tokens", {}).get(token_key)
        if token_value:
            headers["Authorization"] = f"Bearer {token_value}"
        elif token_key in (None, "empty"):
            headers.pop("Authorization", None)

        # Request
        start = time.perf_counter()
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=body if method in ["POST", "PUT", "PATCH"] else None,
            timeout=30
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        SUMMARY["times"].append((test_id, elapsed_ms, url))

        # Response
        try:
            resp_json = response.json()
        except ValueError:
            resp_json = {}

        expected_status = case.get("expected_status")
        if expected_status:
            assert response.status_code == expected_status, \
                f"{test_id} | Expected {expected_status}, got {response.status_code}"

        # ✅ Unified validation: Manual + Schema
        expected_response = case.get("expected_response")
        if expected_response:
            validate_response(test_id, resp_json, expected_response)

        SUMMARY["passed"] += 1
