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

    # ── Batch-fetch all existing checkins for the entire date range at once ───
    # One query covering every employee-day in this API response instead of one
    # SELECT per (employee, date) group — reduces DB round-trips from O(N×D) to 1.
    # Fetches device_id (app vs manual) and shift (to detect and fix null-shift records).
    if groups:
        all_empcodes = tuple({empcode for (empcode, _) in groups.keys()})
        all_dates    = sorted({_date for (_, _date) in groups.keys()})
        batch_start  = f"{min(all_dates)} 00:00:00"
        batch_end    = f"{max(all_dates)} 23:59:59"

        existing_all = frappe.db.sql("""
            SELECT employee,
                   name,
                   time,
                   log_type,
                   device_id,
                   shift
            FROM `tabEmployee Checkin`
            WHERE employee IN %s
              AND time BETWEEN %s AND %s
            ORDER BY employee, time ASC
        """, (all_empcodes, batch_start, batch_end), as_dict=True)

        existing_map = defaultdict(list)
        for row in existing_all:
            row_dt   = _ensure_datetime(row.time)
            row_date = row_dt.date()
            existing_map[(row.employee, row_date)].append({
                "name":             row.name,
                "time_str":         row_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "dt":               row_dt,
                "current_log_type": row.log_type,
                "device_id":        row.device_id,
                "shift":            row.shift,
            })

        # Batch-fetch employee existence + default_shift.
        # Replaces per-group frappe.db.exists() calls.
        emp_rows = frappe.db.sql("""
            SELECT name, default_shift FROM `tabEmployee` WHERE name IN %s
        """, (all_empcodes,), as_dict=True)
        employee_info = {row.name: (row.default_shift or "") for row in emp_rows}

        # Override with active Shift Assignments where they exist —
        # assignments take priority over the employee's default shift.
        sa_rows = frappe.db.sql("""
            SELECT employee, shift_type, start_date, end_date
            FROM `tabShift Assignment`
            WHERE employee IN %s
              AND status = 'Active'
              AND docstatus = 1
              AND start_date <= %s
            ORDER BY start_date DESC
        """, (all_empcodes, max(all_dates)), as_dict=True)
        for row in sa_rows:
            if row.employee in employee_info:
                if not row.end_date or row.end_date >= min(all_dates):
                    employee_info[row.employee] = row.shift_type or employee_info[row.employee]
    else:
        existing_map  = {}
        employee_info = {}

    for (empcode, _date), punches in groups.items():

        # Validate employee exists — use the pre-fetched employee_info dict
        # (batch query replaced per-group frappe.db.exists calls).
        if empcode not in employee_info:
            frappe.logger("biometric").warning(
                f"[Biometric] Employee not found for Empcode '{empcode}'. Skipping."
            )
            not_found += 1
            continue

        emp_shift    = employee_info[empcode]   # shift to tag every checkin with
        start_of_day = f"{_date} 00:00:00"
        end_of_day   = f"{_date} 23:59:59"

        existing_rows    = existing_map.get((empcode, _date), [])
        existing_by_time = {row["time_str"]: row for row in existing_rows}

        # ── Build merged list: existing + new, sorted chronologically ─────────
        # existing_rows already deduplicates DB records. New punches are appended
        # only when their timestamp is absent from existing_by_time.
        all_entries = []

        for row in existing_rows:
            all_entries.append({
                "time_str":         row["time_str"],
                "dt":               row["dt"],
                "name":             row["name"],
                "current_log_type": row["current_log_type"],
                "is_existing":      True,
                "device_id":        row["device_id"],
                "shift":            row["shift"],
                "mcid":             None,
            })

        seen_batch: set = set()  # guards against duplicate timestamps in one API response
        for punch in sorted(punches, key=lambda x: x["dt"]):
            time_str = punch["dt"].strftime("%Y-%m-%d %H:%M:%S")
            if time_str in existing_by_time or time_str in seen_batch:
                skipped += 1
                continue
            seen_batch.add(time_str)
            all_entries.append({
                "time_str":         time_str,
                "dt":               punch["dt"],
                "name":             None,
                "current_log_type": None,
                "is_existing":      False,
                "device_id":        None,
                "shift":            None,
                "mcid":             punch["mcid"],
            })

        all_entries.sort(key=lambda x: x["dt"])

        # ── Assign log_type by position: index 0 = IN, all others = OUT ───────
        # First arrival is IN; all subsequent punches are OUT. The latest OUT
        # wins for attendance calculation (via skip_auto_attendance below).
        # Corrections only touch app-inserted records (device_id set); manual
        # HR edits (device_id empty) are preserved.
        group_created = 0

        for idx, entry in enumerate(all_entries):
            correct = "IN" if idx == 0 else "OUT"

            if entry["is_existing"]:
                updates = {}

                if entry["current_log_type"] != correct:
                    if entry["device_id"]:
                        updates["log_type"] = correct
                        frappe.logger("biometric").info(
                            f"[Biometric] Corrected log_type for {empcode} at "
                            f"{entry['time_str']}: {entry['current_log_type']} → {correct}"
                        )
                    else:
                        frappe.logger("biometric").info(
                            f"[Biometric] Preserving manual edit on {entry['name']} "
                            f"({empcode} at {entry['time_str']})"
                        )

                # Fix null shift on app-inserted records by tagging with the
                # employee's shift. process_auto_attendance() applies threshold
                # rules — the checkin's job is only to carry the shift name.
                if entry["device_id"] and not entry["shift"] and emp_shift:
                    updates["shift"] = emp_shift
                    frappe.logger("biometric").info(
                        f"[Biometric] Fixed null shift for {entry['name']} "
                        f"({empcode} at {entry['time_str']}): set to '{emp_shift}'"
                    )

                if updates:
                    frappe.db.set_value(
                        "Employee Checkin", entry["name"], updates,
                        update_modified=False,
                    )
            else:
                doc = frappe.new_doc("Employee Checkin")
                doc.employee  = empcode
                doc.time      = entry["dt"]
                doc.log_type  = correct
                doc.device_id = entry["mcid"] or ""

                # Tag the checkin with the employee's shift.
                # We never decide if the punch is within the shift window —
                # that is process_auto_attendance()'s job.
                if emp_shift:
                    doc.shift = emp_shift

                # Bypass geolocation validate() — biometric devices have no GPS.
                # device_id already identifies the physical reader for audit.
                doc.flags.ignore_mandatory = True
                doc.flags.ignore_validate  = True
                doc.insert(ignore_permissions=True)
                created       += 1
                group_created += 1

        # ── Consolidate OUTs — only when new records were just added ──────────
        # Skip the self-join UPDATE on syncs where nothing changed for this group.
        if group_created > 0:
            frappe.db.sql("""
                UPDATE `tabEmployee Checkin`
                SET skip_auto_attendance = 1
                WHERE employee = %(employee)s
                  AND log_type = 'OUT'
                  AND time BETWEEN %(start_of_day)s AND %(end_of_day)s
                  AND name != (
                      SELECT latest_name FROM (
                          SELECT name AS latest_name
                          FROM `tabEmployee Checkin`
                          WHERE employee = %(employee)s
                            AND log_type = 'OUT'
                            AND time BETWEEN %(start_of_day)s AND %(end_of_day)s
                          ORDER BY time DESC
                          LIMIT 1
                      ) AS subquery
                  )
            """, {
                "employee":     empcode,
                "start_of_day": start_of_day,
                "end_of_day":   end_of_day,
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
