# DECISIONS.md

1. **Indexes.** I use a unique index on `employees.emp_code` and a unique compound index on `attendance_logs.emp_code, date` for identity and race-safe inserts. Date/employee ordering and date/status filters have supporting attendance indexes; employee department/join-date queries have a compound index. I would avoid indexing every derived metric because each extra index slows writes and consumes storage.

2. **Punch-in race.** Both requests may initially find no record. MongoDB's unique `(emp_code, date)` index arbitrates the insert: one insert succeeds and returns 201; the other raises `DuplicateKeyError` and becomes 409. Correctness does not depend on the initial read.

3. **Ties.** Ranking uses MongoDB `$rank` on total late minutes. If two employees tie at the cutoff rank, both are returned; the next rank is skipped. Rows are then ordered by total minutes descending and employee code ascending for deterministic output.

4. **Headcount.** Department summary starts from employees joined by the month end and looks up logs, preserving employees with no logs. It therefore counts eligible headcount independently of attendance activity and excludes future joiners.

5. **100x scale.** I would review query plans and workload with production-like data, consider pre-aggregated daily summaries for expensive analytics, and add observability and load tests before changing the data model.
