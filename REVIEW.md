# Starter review

Locations below refer to functions in the original 162-line starter in the kit,
not line numbers in the finished implementation. Findings come from static
inspection; each reproduction case describes how to expose the original issue,
not a claim that the original starter was executed. The finished implementation
now passes 23 local rule and MongoDB/HTTP regression tests on Python 3.15.0 and
MongoDB 9.0.4. See `README.md` for commands, results, and remaining coverage gaps.

| # | Starter location | Defect | Concrete way to expose it | Fix in the implementation |
|---:|---|---|---|---|
| 1 | Application setup | No startup indexes: uniqueness and query performance were not enforced. | Inspect indexes after a fresh startup; explain a large attendance query. | Idempotently create unique natural-key indexes and indexes for list/lookup/month/punch-out queries. |
| 2 | `health` | Always returned 200 even when MongoDB was unavailable. | Stop MongoDB, then request `/health`. | Ping the database, ensure indexes are ready, and return 503 on database failure. |
| 3 | `compute_late_minutes` | Floored minutes before checking grace, treating 09:40:01 as on time. | Shift 09:30, punch 09:40:01: expected 10 late minutes. | Compare elapsed seconds strictly against 600, then floor minutes since shift start. |
| 4 | `compute_work_hours` | Binary-float `round` is not decimal half-up. | A 3,618-second duration is 1.005 hours and must produce 1.01. | Divide integer seconds using `Decimal`, then quantize with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | Counted overtime below the 30-minute minimum. | Shift end 18:30, punch-out 18:59:59: expected 0. | Require at least 1,800 elapsed seconds, then floor minutes. |
| 6 | `compute_overtime` | Put an overnight shift's end on its start date. | Shift 22:00–06:00, punch-out next day 06:40: expected 40 minutes. | Construct IST shift bounds and advance the end by one day for overnight shifts. |
| 7 | `EmployeeIn` | Accepted malformed codes/emails/shifts, impossible dates, empty names, and equal shift times. | Submit `joined_on=2026-02-30`, `shift_start=24:00`, or two 09:30 shift times. | Validate lengths, patterns, real calendar dates, and unequal start/end. |
| 8 | `PunchInIn` | Coerced timestamp strings and accepted seconds-like/out-of-range values and arbitrary statuses. | Submit a timestamp string, seconds, float, boolean, null, or status `ABSENT`. | Strict integer/range validation; omission alone defaults to now; presence-only enum. |
| 9 | `create_employee` | Check-then-insert lets parallel duplicate employee codes succeed. | Send six identical creates concurrently: exactly one must be 201. | Insert under a unique `emp_code` index and translate duplicate-key failures to 409. |
| 10 | `create_employee`, `punch_in` | Used host-local naive datetimes and emitted datetime strings rather than epoch milliseconds. | Run under a non-IST host zone; inspect `created_at` and punch fields. | Normalize to aware UTC, truncate instants to seconds, serialize integer epoch milliseconds recursively. |
| 11 | `list_employees` | `page * page_size` skips the entire first page. | Create two employees, then request page 1 with size 2. | Calculate skip as `(page - 1) * page_size`; bound both paging parameters. |
| 12 | `list_employees` | `total` counted all employees, not the department-filtered set. | Filter to a department containing one of several employees. | Reuse the filtered query for `count_documents`. |
| 13 | Both list routes | Employee results were unsorted; attendance ties had no `emp_code` ordering. | Insert codes in reverse order, including two logs on the same day. | Sort in MongoDB: employee code ascending; attendance date descending then code ascending. |
| 14 | `punch_in` | Dereferenced a missing employee and could return 500. | Punch in an unknown employee code. | Return 404 before using shift fields. |
| 15 | `punch_in` | Attendance date ignored IST and the overnight exception; subsecond instants were retained. | For a 22:00–06:00 shift, punch at 00:30 IST: date must be the previous day. | Truncate first, convert to IST, and assign the previous day only before overnight shift end. |
| 16 | `punch_in` | A second check-then-insert race allowed duplicate employee/day records. | Send identical punch-ins concurrently: exactly one must be 201. | Unique `(emp_code, date)` index; map duplicate-key errors to 409. |
| 17 | `punch_in`, `list_attendance` | Added an internal MongoDB-derived `id`, which is not part of the contract. | Inspect an attendance response. | Exclude `id` and `_id`; address records only by employee code/date. |
| 18 | `list_attendance` | Loaded and sorted every matched document in Python before slicing. | Query a 100k-record collection with page size 20. | Indexed database filtering/sort/skip/limit; materialize only the requested page. |
| 19 | `list_attendance` | Accepted invalid dates, reversed ranges, invalid statuses, and invalid page bounds. | Request an impossible date, `date_from > date_to`, or `page=0`. | Validate calendar values, ordering, enum, and pagination; return 422. |
| 20 | `list_attendance` | Legacy missing fields were not filled, and nested BSON dates were not normalized. | Read a record lacking `history`/`half_day`, or one containing correction timestamps. | Supply documented defaults and recursively serialize instants, including history changes. |

The TODO routes were unfinished work rather than defects in existing behavior:
punch-out, regularization, monthly/department analytics, leaderboard, trend, and
explain are implemented. Updates compare the previously read mutable fields,
atomically return the updated document, and append correction history with
`$push`; a losing concurrent edit returns 409 rather than overwriting history.

**Examined and retained:** `load_dotenv()` does not override environment
variables by default; `emp_code` is the business identifier; the five status
values and plain calendar strings match the data model. The starter already
initialized punch-in derived fields/history correctly in shape. Unknown body
fields were ignored by Pydantic, which is appropriate for read-only metrics.
