"""
HotelDataSnapshot contract tests.
Run: py -3 test_contract.py
Every PMS adapter must pass the GOOD-payload case; the broken cases verify
the gates that protect publication.
"""
import copy
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from test_analyst import DUMMY_DATA
from db.contract import build_data_quality, is_publishable

ROOMS = 167
passed = failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("── 1. Complete v1.2 payload ──")
dq = build_data_quality(DUMMY_DATA, ROOMS)
check("complete", dq["complete"], str(dq["missing_fields"][:5]))
check("not legacy_mode", not dq["legacy_mode"])
check("all sanity pass", all(dq["sanity"].values()), str(dq["sanity"]))
check("rows counted", dq["rows_fetched"]["otb_by_date"] == 90, str(dq["rows_fetched"]))
ok, reason = is_publishable(DUMMY_DATA, ROOMS)
check("publishable", ok, reason)

print("── 2. Legacy payload (old fetcher — no signal fields) ──")
legacy = {k: copy.deepcopy(v) for k, v in DUMMY_DATA.items()
          if k not in ("pickup_daily", "otb_by_date", "current_month_remaining")}
for p in legacy["pace"]:
    p.pop("rn_stly", None)
    p.pop("rn_final_ly", None)
dq = build_data_quality(legacy, ROOMS)
check("legacy_mode detected", dq["legacy_mode"])
check("still publishable (core intact)", dq["complete"], str(dq["missing_fields"]))
check("pace[].rn_stly reported missing", "pace[].rn_stly" in dq["missing_fields"])

print("── 3. Zero-data payload (the Potidea bug) ──")
zero = copy.deepcopy(DUMMY_DATA)
zero["yesterday"]["revenue"] = 0
zero["yesterday"]["roomNights"] = 0
dq = build_data_quality(zero, ROOMS)
check("not complete", not dq["complete"])
check("yesterday_nonzero failed", not dq["sanity"]["yesterday_nonzero"])
ok, reason = is_publishable(zero, ROOMS)
check("blocked from publication", not ok, reason)

print("── 4. Impossible occupancy ──")
bad_occ = copy.deepcopy(DUMMY_DATA)
bad_occ["mtd"]["occupancy"] = 1.4
dq = build_data_quality(bad_occ, ROOMS)
check("occupancy_bounds failed", not dq["sanity"]["occupancy_bounds"])
check("not publishable", not dq["complete"])

print("── 5. Remaining OTB exceeds capacity (v1.2 D1 sanity) ──")
overcap = copy.deepcopy(DUMMY_DATA)
overcap["current_month_remaining"]["rn_remaining_otb_ty"] = 99_999
dq = build_data_quality(overcap, ROOMS)
check("remaining_fits_capacity failed", not dq["sanity"]["remaining_fits_capacity"])
check("not publishable", not dq["complete"])

print("── 6. Missing core section ──")
no_core = {k: v for k, v in DUMMY_DATA.items() if k != "yesterday"}
dq = build_data_quality(no_core, ROOMS)
check("yesterday reported missing", "yesterday" in dq["missing_fields"])
check("not publishable", not dq["complete"])

print()
print(f"{'ALL PASS' if failed == 0 else 'FAILURES'}: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
