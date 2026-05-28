"""
etimeoffice_biometric/utils/sync.py
─────────────────────────────────────
Core sync logic:
  1. Fetch punch data from eTimeOffice API
  2. Group by (employee_code, calendar_date)
  3. First punch of the day  → Employee Checkin log_type="IN"
     Last punch of the day   → Employee Checkin log_type="OUT"
     Single punch only       → log_type="IN"
  4. Skip duplicates (same employee + timestamp + log_type already exists)
  5. Write a Biometric Sync Log record with the result
"""
import datetime
from collections import defaultdict

import frappe
from frappe.utils import now_datetime


# ─── Date format used by the eTimeOffice API ─────────────────────────────────
_API_PUNCH_FMT_LONG  = "%d/%m/%Y %H:%M:%S"
_API_PUNCH_FMT_SHORT = "%d/%m/%Y %H:%M"
_API_PARAM_FMT       = "%d/%m/%Y_%H:%M"


# ─── Public entry point ───────────────────────────────────────────────────────

def fetch_and_sync(emp_code="ALL", from_date=None, to_date=None):
    """
    Fetch punch data from eTimeOffice and create Employee Checkin records.

    Args:
        emp_code  : "ALL" or a specific Employee ID (same as Empcode)
        from_date : datetime | None  (defaults to sync_days_back ago)
        to_date   : datetime | None  (defaults to now)

    Returns:
        Biometric Sync Log doc (already inserted)
    """
    settings = frappe.get_single("Biometric Settings")
    now = now_datetime()

    # ── Resolve date range ────────────────────────────────────────────────────
    if not to_date:
        to_date = now

    if not from_date:
        days_back = int(settings.sync_days_back or 1)
        from_date = to_date - datetime.timedelta(days=days_back)

    # Ensure datetime objects
    from_date = _ensure_datetime(from_date)
    to_date   = _ensure_datetime(to_date)

    from_date_str = from_date.strftime(_API_PARAM_FMT)
    to_date_str   = to_date.strftime(_API_PARAM_FMT)

    # ── Create a pending Sync Log ─────────────────────────────────────────────
    log = frappe.new_doc("Biometric Sync Log")
    log.sync_time         = now
    log.employee_filter   = emp_code
    log.from_date         = from_date
    log.to_date           = to_date
    log.status            = "Failed"
    log.records_fetched   = 0
    log.records_created   = 0
    log.records_skipped   = 0
    log.records_not_found = 0

    try:
        from etimeoffice_biometric.utils.etimeoffice import fetch_punch_data

        punch_list = fetch_punch_data(settings, emp_code, from_date_str, to_date_str)
        log.records_fetched = len(punch_list)

        created, skipped, not_found = _process_punches(punch_list)

        log.records_created = created
        log.records_skipped = skipped
        log.records_not_found = not_found
        log.status = "Success" if not_found == 0 else "Partial"

    except Exception as exc:
        log.error_message = str(exc)
        log.status = "Failed"
        frappe.log_error(frappe.get_traceback(), "[Biometric] Sync Error")

    finally:
        log.flags.ignore_permissions = True
        log.insert(ignore_permissions=True)

    return log


# ─── Core punch processing ────────────────────────────────────────────────────

def _process_punches(punch_list):
    """
    Group punches by (Empcode, date), determine IN/OUT, and insert records.

    Returns:
        (created: int, skipped: int, not_found: int)
    """
    # ── Group by (empcode, calendar_date) ─────────────────────────────────────
    groups = defaultdict(list)

    for punch in punch_list:
        empcode     = (punch.get("Empcode") or "").strip()
        punch_str   = (punch.get("PunchDate") or "").strip()
        mcid        = punch.get("mcid") or punch.get("M_Flag") or ""

        if not empcode or not punch_str:
            continue

        punch_dt = _parse_punch_datetime(punch_str)
        if punch_dt is None:
            frappe.logger("biometric").warning(
                f"[Biometric] Could not parse PunchDate: '{punch_str}' for Empcode {empcode}"
            )
            continue

        groups[(empcode, punch_dt.date())].append({
            "dt":   punch_dt,
            "mcid": str(mcid).strip() if mcid else "",
            "name": (punch.get("Name") or "").strip(),
        })

    # ── Insert Employee Checkin records ───────────────────────────────────────
    created   = 0
    skipped   = 0
    not_found = 0

    # Track keys inserted during this batch to prevent intra-batch duplicates
    # when the same punch appears multiple times in the API response.
    inserted_this_batch: set = set()

    for (empcode, _date), punches in groups.items():

        # Validate employee exists (Empcode == ERPNext Employee ID)
        if not frappe.db.exists("Employee", empcode):
            frappe.logger("biometric").warning(
                f"[Biometric] Employee not found for Empcode '{empcode}'. Skipping."
            )
            not_found += 1
            continue

        punches_sorted = sorted(punches, key=lambda x: x["dt"])

        # Check if there are already check-ins for the day
        start_of_day = f"{_date} 00:00:00"
        end_of_day = f"{_date} 23:59:59"

        existing_checkins = frappe.get_all(
            "Employee Checkin",
            filters={
                "employee": empcode,
                "time": ["between", [start_of_day, end_of_day]]
            },
            limit=1
        )

        # Determine IN / OUT records
        checkins = []
        if existing_checkins:
            # If check-ins already exist, we know an IN already exists.
            # All new punches are OUT.
            for punch in punches_sorted:
                checkins.append({
                    "employee":  empcode,
                    "time":      punch["dt"],
                    "log_type":  "OUT",
                    "device_id": punch["mcid"],
                })
        else:
            # The first punch is IN, all subsequent punches are OUT
            for i, punch in enumerate(punches_sorted):
                checkins.append({
                    "employee":  empcode,
                    "time":      punch["dt"],
                    "log_type":  "IN" if i == 0 else "OUT",
                    "device_id": punch["mcid"],
                })

        for ci in checkins:
            time_str = ci["time"].strftime("%Y-%m-%d %H:%M:%S")
            batch_key = (ci["employee"], time_str, ci["log_type"])

            # Skip if already inserted in this batch run
            if batch_key in inserted_this_batch:
                skipped += 1
                continue

            # ── Duplicate check using direct SQL (reliable datetime match) ────
            # frappe.db.exists can miss duplicates due to datetime precision
            # differences. Direct SQL with DATE_FORMAT truncated to the second
            # is the most reliable approach and prevents duplicates across
            # separate fetch runs (manual or scheduled).
            # Log type is ignored so that overlapping fetches don't re-insert
            # early IN punches as OUT punches.
            already_exists = frappe.db.sql("""
                SELECT name FROM `tabEmployee Checkin`
                WHERE employee = %s
                  AND DATE_FORMAT(time, '%%Y-%%m-%%d %%H:%%i:%%s') = %s
                LIMIT 1
            """, (ci["employee"], time_str))

            if already_exists:
                skipped += 1
                continue

            doc = frappe.new_doc("Employee Checkin")
            doc.employee  = ci["employee"]
            doc.time      = ci["time"]
            doc.log_type  = ci["log_type"]
            doc.device_id = ci["device_id"]

            # ── Geolocation handling ──────────────────────────────────────────
            # When HR Settings has geolocation tracking enabled, ERPNext's
            # Employee Checkin validate() throws if latitude/longitude are
            # missing. Biometric devices don't provide GPS coordinates, so we
            # bypass the validate() method entirely. The device_id field already
            # identifies the physical machine location for audit purposes.
            doc.flags.ignore_mandatory = True
            doc.flags.ignore_validate  = True
            doc.insert(ignore_permissions=True)
            inserted_this_batch.add(batch_key)
            created += 1

        # Ensure only the absolute latest OUT punch for this employee on this date has skip_auto_attendance = 0
        frappe.db.sql("""
            UPDATE `tabEmployee Checkin`
            SET skip_auto_attendance = 1
            WHERE employee = %(employee)s
              AND log_type = 'OUT'
              AND time >= %(start_of_day)s
              AND time <= %(end_of_day)s
              AND name != (
                  SELECT latest_name FROM (
                      SELECT name AS latest_name
                      FROM `tabEmployee Checkin`
                      WHERE employee = %(employee)s
                        AND log_type = 'OUT'
                        AND time >= %(start_of_day)s
                        AND time <= %(end_of_day)s
                      ORDER BY time DESC
                      LIMIT 1
                  ) AS subquery
              )
        """, {
            "employee": empcode,
            "start_of_day": f"{_date} 00:00:00",
            "end_of_day": f"{_date} 23:59:59"
        })

    return created, skipped, not_found


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_punch_datetime(s):
    """Try both 'dd/MM/yyyy HH:mm:ss' and 'dd/MM/yyyy HH:mm' formats."""
    for fmt in (_API_PUNCH_FMT_LONG, _API_PUNCH_FMT_SHORT):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _ensure_datetime(val):
    """
    Coerce a string, date, or datetime to a naive datetime object.

    Handles all formats Frappe may store for a Datetime field:
      - Already a datetime / date object
      - Space-separated:  '2026-05-07 17:00:20.600365', '2026-05-07 17:00:20', '2026-05-07 17:00'
      - ISO 8601 T-sep:   '2026-05-07T17:00:20.600365', '2026-05-07T17:00:20', '2026-05-07T17:00'
      - Date only:        '2026-05-07'
    """
    if isinstance(val, datetime.datetime):
        return val
    if isinstance(val, datetime.date):
        return datetime.datetime.combine(val, datetime.time.min)
    if isinstance(val, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f",   # ISO 8601 with microseconds
            "%Y-%m-%dT%H:%M:%S",      # ISO 8601 without microseconds
            "%Y-%m-%dT%H:%M",         # ISO 8601 no seconds
            "%Y-%m-%d %H:%M:%S.%f",   # Frappe space-format with microseconds
            "%Y-%m-%d %H:%M:%S",      # Frappe space-format
            "%Y-%m-%d %H:%M",         # Frappe without seconds
            "%Y-%m-%d",               # date-only fallback
        ):
            try:
                return datetime.datetime.strptime(val, fmt)
            except ValueError:
                continue
        # Last-resort: Python's built-in ISO parser (handles timezone suffixes too)
        try:
            dt = datetime.datetime.fromisoformat(val)
            # Strip timezone info so we stay naive (consistent with frappe.utils.now_datetime)
            return dt.replace(tzinfo=None)
        except (ValueError, AttributeError):
            pass
    raise ValueError(f"Cannot convert to datetime: {val!r}")
