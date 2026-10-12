# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How it is fixed |
|---:|---|---|---|---|
| 1 | `compute_late_minutes` | Uses the punch timestamp's wall-clock date without consistently handling UTC/IST and truncation rules. | UTC timestamps around midnight or milliseconds produce a wrong comparison. | Normalize instants to UTC, convert to IST, truncate to whole seconds at the API boundary, and apply the strict >10-minute rule. |
| 2 | `compute_work_hours` | Python `round` uses binary floating point / ties-to-even rather than required decimal half-up rounding. | A duration exactly at a half-centesimal boundary rounds incorrectly. | Use `Decimal` with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Builds shift end on the attendance date even for overnight shifts; it also lacks the 30-minute threshold. | A 22:00–06:00 shift gets incorrect overtime after midnight. | Resolve the shift-end calendar day from shift times and count overtime only when floored minutes are at least 30. |
| 4 | `create_employee` | Check-then-insert is racy; two requests can both pass the duplicate check. | Parallel POST requests can create duplicate employee codes. | Create a unique index and translate `DuplicateKeyError` to 409. |
| 5 | `create_employee` | Uses naive `datetime.now()` for a BSON instant. | Stored timestamps have inconsistent timezone semantics. | Store UTC-aware server timestamps. |
| 6 | `list_employees` | `skip = page * page_size` skips page 1 and later pages are offset incorrectly. | Page 1 starts after the first 20 records. | Use `(page - 1) * page_size`; validate page and page size and cap size at 100. |
| 7 | `list_employees` | `total` ignores the department filter. | Filtered items and reported total disagree. | Count with the same filter used by `find`. |
| 8 | `list_employees` | No deterministic sort. | Page contents can shift between requests. | Sort by `emp_code` ascending and index the lookup/sort fields. |
| 9 | `punch_in` | Does not return 404 for an unknown employee before dereferencing `emp`. | Unknown code causes a 500 / `None` subscript error. | Explicitly return 404. |
| 10 | `punch_in` | Uses local machine time via `fromtimestamp` / `datetime.now`, and derives date without IST or overnight logic. | Results differ by server timezone; overnight punches get the wrong attendance date. | Normalize all instants to UTC, calculate attendance date in IST, and apply R1. |
| 11 | `punch_in` | Duplicate detection is check-then-insert, not concurrency-safe. | Simultaneous punches can create duplicate records. | Enforce unique `(emp_code, date)` index and map duplicate-key failures to 409. |
| 12 | `punch_in` | Does not validate status against the three permitted punch-in statuses. | `ABSENT` or `LEAVE` can be created with a punch timestamp. | Validate with a literal enum. |
| 13 | `punch_in` | Timestamp parsing does not safely reject invalid epoch values. | Extreme or malformed values can raise an internal error. | Parse and truncate safely; return 422 for invalid timestamps. |
| 14 | `list_attendance` | Loads every matching record into Python and sorts/slices in memory. | Latency and memory grow with the full 100k-record collection. | Use indexed MongoDB sort, skip, limit, and a separate filtered count. |
| 15 | `list_attendance` | Returns MongoDB datetimes and ObjectIds in inconsistent formats and does not enforce page bounds. | JSON serialization fails or response fields violate the contract. | Serialize instants as epoch milliseconds, omit internal IDs, and validate pagination. |
| 16 | Missing punch-out endpoint | No atomic close operation or derived-field calculation exists. | Records cannot be closed safely; concurrent punch-outs can both succeed. | Add conditional `find_one_and_update` and compute work/overtime/half-day fields. |
| 17 | Missing regularization endpoint | No validation or append-only audit history exists. | Corrections cannot be reviewed and concurrent edits can lose history. | Validate final state, record changed fields only, append history atomically, and compare-and-set. |
| 18 | Missing analytics endpoints | Required summaries, rank semantics, and date gap filling are absent. | Contract routes return 404 or incorrect metrics. | Implement aggregation pipelines, `$rank`, and a densified calendar trend. |
| 19 | Missing indexes and readiness check | No startup indexes; `/health` always says ready without pinging MongoDB. | Duplicate writes remain possible and the runner may test an unavailable database. | Create idempotent indexes at startup and return 503 if MongoDB ping fails. |

## Checked and not treated as defects

- `compute_work_hours`'s basic elapsed-seconds formula is conceptually correct; its rounding method was the defect.
- The natural key for attendance is `(emp_code, date)`, not MongoDB `_id`; this matches the supplied data model.
- Keeping history only for manual corrections is intentional; punch-in and punch-out must not append audit entries.
