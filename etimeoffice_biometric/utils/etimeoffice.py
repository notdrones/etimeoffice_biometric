"""
etimeoffice_biometric/utils/etimeoffice.py
──────────────────────────────────────────
HTTP client for the eTimeOffice Biometric API.
Handles authentication and data fetching.
"""
import base64
import frappe
import requests


# ─── Auth ────────────────────────────────────────────────────────────────────

def _build_auth_header(settings):
    password = settings.get_password("password") or ""
    raw = f"{settings.corporate_id}:{settings.username}:{password}:true"
    encoded = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
    return {
        "Authorization": "Basic " + encoded,
        "User-Agent": "curl/7.81.0",
    }


# ─── API Call ─────────────────────────────────────────────────────────────────

def fetch_punch_data(settings, emp_code="ALL", from_date=None, to_date=None):
    base_url    = (settings.api_base_url or "https://api.etimeoffice.com").rstrip("/")
    endpoint    = f"{base_url}/api/DownloadPunchDataMCID"
    params      = {"Empcode": emp_code, "FromDate": from_date, "ToDate": to_date}
    headers     = _build_auth_header(settings)

    frappe.logger("biometric").debug(
        f"[Biometric API] GET {endpoint} | Empcode={emp_code} "
        f"FromDate={from_date} ToDate={to_date}"
    )

    try:
        response = requests.get(endpoint, headers=headers, params=params, timeout=60)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        raise Exception("eTimeOffice API request timed out (60 s). Please try again.")
    except requests.exceptions.ConnectionError as exc:
        raise Exception(f"Could not connect to eTimeOffice API: {exc}")
    except requests.exceptions.HTTPError:
        body = response.text if response else "No response"
        frappe.logger("biometric").error(
            "[Biometric API ERROR]"
            f"\nStatus: {response.status_code}"
            f"\nURL: {response.url}"
            f"\nResponse: {body}"
        )
        raise Exception(f"HTTP {response.status_code}: {body}")

    try:
        data = response.json()
    except ValueError:
        raise Exception(
            f"eTimeOffice API returned non-JSON response: {response.text[:300]}"
        )

    if data.get("Error"):
        raise Exception(f"eTimeOffice API error — {data.get('Msg', 'Unknown error')}")

    punch_data = data.get("PunchData") or []
    frappe.logger("biometric").debug(
        f"[Biometric API] Received {len(punch_data)} punch records."
    )
    return punch_data


# ─── Connection Test ──────────────────────────────────────────────────────────

def test_api_connection(settings):
    import datetime
    now = datetime.datetime.now()
    from_date = (now - datetime.timedelta(days=1)).strftime("%d/%m/%Y_%H:%M")
    to_date = now.strftime("%d/%m/%Y_%H:%M")
    try:
        data = fetch_punch_data(settings, "ALL", from_date, to_date)
        return True, f"Connection successful! Received {len(data)} punch record(s)."
    except Exception as exc:
        return False, str(exc)
