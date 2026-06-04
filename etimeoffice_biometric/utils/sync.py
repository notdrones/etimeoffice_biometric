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

        created, skipped, not_found, shift_min_times = _process_punches(punch_list)

        log.records_created = created
        log.records_skipped = skipped
        log.records_not_found = not_found
        log.status = "Success" if not_found == 0 else "Partial"

        # Reset each affected Shift Type's last_sync_of_checkin to just before
        # the earliest punch we inserted. This ensures ERPNext's scheduled
        # process_auto_attendance job finds and processes all newly inserted
        # historical records. Without this, after_insert fires during each
        # checkin insert and advances the watermark to server time, making
        # historical punch times invisible to future scheduler runs.
        if shift_min_times:
            _reset_shift_watermarks(shift_min_times)

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
        (created: int, skipped: int, not_found: int, shift_min_times: dict)
        shift_min_times maps Shift Type name → earliest inserted punch datetime.
        Used by fetch_and_sync to reset last_sync_of_checkin after the batch.
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
    created         = 0
    skipped         = 0
    not_found       = 0
    shift_min_times = {}   # {shift_type_name: earliest_punch_datetime}

    # ── Batch-fetch existing checkins and employee validity in two queries ────
    # One checkin SELECT for the full date range × all employees (O(1) queries).
    # One Employee SELECT for existence check — replaces per-group db.exists().
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

        emp_rows = frappe.db.sql(
            "SELECT name FROM `tabEmployee` WHERE name IN %s",
            (all_empcodes,), as_dict=True,
        )
        valid_employees = {row.name for row in emp_rows}
    else:
        existing_map    = {}
        valid_employees = set()

    for (empcode, _date), punches in groups.items():

        if empcode not in valid_employees:
            frappe.logger("biometric").warning(
                f"[Biometric] Employee not found for Empcode '{empcode}'. Skipping."
            )
            not_found += 1
            continue

        start_of_day = f"{_date} 00:00:00"
        end_of_day   = f"{_date} 23:59:59"

        existing_rows    = existing_map.get((empcode, _date), [])
        existing_by_time = {row["time_str"]: row for row in existing_rows}

        # ── Build merged list: existing + new, sorted chronologically ─────────
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
        # Biometric devices send raw punches with no direction. First arrival is IN;
        # all subsequent are OUT. The latest OUT wins for attendance calculation.
        # Only app-inserted records (device_id set) are auto-corrected;
        # manual HR edits (device_id empty) are preserved.
        group_created = 0
        group_changed = False

        for idx, entry in enumerate(all_entries):
            correct = "IN" if idx == 0 else "OUT"

            if entry["is_existing"]:
                updates = {}

                # ── Correct wrong log_type ────────────────────────────────────
                if entry["current_log_type"] != correct:
                    if entry["device_id"]:
                        updates["log_type"] = correct
                        # A record corrected from OUT→IN may have had skip=1 set
                        # while it was an intermediate OUT. Clear it so ERPNext
                        # auto-attendance can use this record as the IN anchor.
                        if correct == "IN":
                            updates["skip_auto_attendance"] = 0
                        frappe.logger("biometric").info(
                            f"[Biometric] Corrected log_type for {empcode} at "
                            f"{entry['time_str']}: {entry['current_log_type']} → {correct}"
                        )
                    else:
                        frappe.logger("biometric").info(
                            f"[Biometric] Preserving manual edit on {entry['name']} "
                            f"({empcode} at {entry['time_str']})"
                        )

                # ── Fix null shift on app-inserted records ────────────────────
                # Load the committed doc and call fetch_shift() so ERPNext
                # populates all timing fields (shift_actual_start/end, shift_start/end,
                # offshift, overtime_type) that process_auto_attendance() needs
                # for grouping logs. validate() would do this automatically but we
                # bypass it on insert to skip the geolocation check.
                if entry["device_id"] and not entry["shift"]:
                    try:
                        tmp = frappe.get_doc("Employee Checkin", entry["name"])
                        tmp.fetch_shift()
                        shift_upd = _collect_shift_updates(tmp)
                        updates.update(shift_upd)
                        if shift_upd.get("shift"):
                            frappe.logger("biometric").info(
                                f"[Biometric] Fixed shift for {entry['name']} "
                                f"({empcode}): set to '{shift_upd['shift']}'"
                            )
                    except Exception:
                        frappe.log_error(
                            frappe.get_traceback(),
                            f"[Biometric] fetch_shift() failed for existing record "
                            f"{entry['name']} ({empcode} at {entry['time_str']})",
                        )

                if updates:
                    frappe.db.set_value(
                        "Employee Checkin", entry["name"], updates,
                        update_modified=False,
                    )
                    group_changed = True

            else:
                doc = frappe.new_doc("Employee Checkin")
                doc.employee  = empcode
                doc.time      = entry["dt"]
                doc.log_type  = correct
                doc.device_id = entry["mcid"] or ""

                # ignore_mandatory: biometric records have no GPS coordinates,
                #   so lat/lon/geolocation mandatory checks must be skipped.
                # ignore_validate: bypasses any geolocation assertion in validate().
                #   fetch_shift() is called manually below instead.
                # ignore_after_insert: prevents ERPNext's after_insert from calling
                #   process_auto_attendance() on each insert. That call advances
                #   Shift Type.last_sync_of_checkin to server time, which places
                #   all historical punch times (in the past) behind the watermark
                #   and makes them invisible to the daily scheduler. We reset the
                #   watermark ourselves at the end of fetch_and_sync() instead.
                doc.flags.ignore_mandatory    = True
                doc.flags.ignore_validate     = True
                doc.flags.ignore_after_insert = True
                doc.insert(ignore_permissions=True)

                # Call fetch_shift() AFTER insert so the document is committed
                # and ERPNext can resolve all shift timing fields correctly.
                try:
                    doc.fetch_shift()
                    shift_upd = _collect_shift_updates(doc)
                    if shift_upd:
                        frappe.db.set_value(
                            "Employee Checkin", doc.name, shift_upd,
                            update_modified=False,
                        )
                        # Track the earliest punch time per shift so we can reset
                        # last_sync_of_checkin after the full batch completes.
                        sname = shift_upd.get("shift")
                        if sname:
                            if sname not in shift_min_times or entry["dt"] < shift_min_times[sname]:
                                shift_min_times[sname] = entry["dt"]
                except Exception:
                    frappe.log_error(
                        frappe.get_traceback(),
                        f"[Biometric] fetch_shift() failed for {empcode} at {entry['dt']}",
                    )

                created       += 1
                group_created += 1
                group_changed  = True

        # ── Consolidate OUTs ──────────────────────────────────────────────────
        # Mark all OUT records except the latest as skip_auto_attendance=1 so
        # ERPNext's process_auto_attendance() only uses the final exit time.
        # Run whenever anything in this group changed (new inserts or corrections).
        if group_changed:
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

    return created, skipped, not_found, shift_min_times


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _collect_shift_updates(doc):
    """
    Return a dict of shift-related fields populated by doc.fetch_shift(),
    ready for frappe.db.set_value(). Skips None values.

    fetch_shift() sets these fields on the document in-memory:
      shift              — Shift Type name
      shift_actual_start — shift start minus begin_check_in_before_shift_start_time
      shift_actual_end   — shift end plus allow_check_out_after_shift_end_time
      shift_start        — nominal shift start datetime
      shift_end          — nominal shift end datetime
      offshift           — 1 if no shift found for this timestamp, else 0
      overtime_type      — Overtime Type if configured on the shift

    process_auto_attendance() groups checkins by (employee, shift_actual_start)
    so all these fields must be persisted — not just the shift name.
    """
    result = {}
    for field in (
        "shift", "shift_actual_start", "shift_actual_end",
        "shift_start", "shift_end", "overtime_type",
        # offshift intentionally excluded: if fetch_shift() finds no shift it
        # sets offshift=1, which causes process_auto_attendance to permanently
        # skip the checkin. We leave that field at its default (0) so ERPNext
        # can re-evaluate it at attendance-processing time; if the shift
        # assignment is later added or corrected, the checkin can still be used.
    ):
        val = doc.get(field)
        if val is not None:
            result[field] = val
    return result


def _reset_shift_watermarks(shift_min_times):
    """
    For each Shift Type that received new checkins this sync run, reset
    last_sync_of_checkin to 1 second before the earliest punch we inserted.

    ERPNext's process_auto_attendance filters checkins using:
        checkin.time >= shift_type.last_sync_of_checkin

    When our sync inserts historical punch records (times in the past),
    after_insert may have already advanced last_sync_of_checkin to server time,
    placing those punch times behind the watermark. This function pulls the
    watermark back so the next scheduler run picks up all the new records.

    We only reset if the current watermark is ahead of our earliest punch —
    we never push the watermark forward or touch unaffected shifts.
    """
    for shift_name, min_punch_dt in shift_min_times.items():
        try:
            current_raw = frappe.db.get_value("Shift Type", shift_name, "last_sync_of_checkin")
            if not current_raw:
                continue

            current_watermark = _ensure_datetime(current_raw)
            # Set watermark to 1 second before the earliest punch we inserted.
            new_watermark = min_punch_dt - datetime.timedelta(seconds=1)

            if current_watermark > new_watermark:
                frappe.db.set_value(
                    "Shift Type", shift_name,
                    "last_sync_of_checkin", new_watermark,
                    update_modified=False,
                )
                frappe.logger("biometric").info(
                    f"[Biometric] Reset last_sync_of_checkin for '{shift_name}' "
                    f"from {current_watermark} → {new_watermark}"
                )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"[Biometric] Failed to reset watermark for Shift Type '{shift_name}'",
            )


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
