"""
Patch: fix_missing_shift_on_checkins
─────────────────────────────────────
Retroactively populate the `shift` field on Employee Checkin records
that were created by the biometric sync without a shift value.

Root cause: sync.py used ignore_validate=True to bypass geolocation
checks, which also skipped fetch_shift(). ERPNext's process_auto_attendance
queries WHERE shift = <shift_name>, so checkins with a NULL shift were
invisible and employees were marked Absent.

Run via Frappe console:
    bench --site <your-site> execute \
        etimeoffice_biometric.patches.fix_missing_shift_on_checkins.execute
"""

import frappe


def execute():
    checkins = frappe.get_all(
        "Employee Checkin",
        filters={"shift": ["is", "not set"]},
        fields=["name", "employee", "time"],
        order_by="time asc",
    )

    total = len(checkins)
    updated = 0
    skipped = 0

    frappe.logger("biometric").info(
        f"[Patch] fix_missing_shift_on_checkins: found {total} checkins to fix"
    )

    for c in checkins:
        try:
            doc = frappe.get_doc("Employee Checkin", c.name)
            doc.fetch_shift()
            if doc.shift:
                frappe.db.set_value(
                    "Employee Checkin", c.name, "shift", doc.shift, update_modified=False
                )
                updated += 1
            else:
                skipped += 1
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"[Patch] fix_missing_shift_on_checkins: error on {c.name}",
            )
            skipped += 1

        # Commit every 200 records to avoid one giant transaction
        if (updated + skipped) % 200 == 0:
            frappe.db.commit()
            frappe.logger("biometric").info(
                f"[Patch] Progress: {updated + skipped}/{total} processed "
                f"({updated} updated, {skipped} skipped)"
            )

    frappe.db.commit()
    frappe.logger("biometric").info(
        f"[Patch] fix_missing_shift_on_checkins done: "
        f"{updated} updated, {skipped} skipped (no shift assignment found)"
    )
    print(f"Done: {updated} updated, {skipped} skipped out of {total} total.")
