# DECISIONS.md

1. **Indexes.** On `employees`: unique `{emp_code: 1}` guarantees unique business codes; `{department: 1, joined_on: 1}` and `{joined_on: 1}` serve summary aggregations without COLLSCAN. On `attendance_logs`: unique `{emp_code: 1, date: 1}` enforces atomic natural key uniqueness; `{date: -1, emp_code: 1}` serves paginated listings; `{emp_code: 1, punch_in: -1}` accelerates punch-out lookups; `{date: 1, late_minutes: 1}` powers leaderboard aggregations. A single-field index on `status` was rejected due to low cardinality and poor selectivity.

2. **Punch-in race.** Both concurrent requests calculate the same IST attendance date and execute `insert_one`. The database unique index on `(emp_code, date)` acts as arbiter: one insert succeeds (201 Created); the other triggers `DuplicateKeyError`, caught and returned as 409 Conflict.

3. **Ties.** We use competition ranking (`$rank: {}`, e.g., 1, 2, 2, 4). Filtering `rank <= limit` applies after ranking, so all tied employees at the cutoff are returned, sorted by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.** Department summary drives from `employees` (`joined_on <= month_end`), using `$lookup` with `preserveNullAndEmptyArrays: True`. Computing `$size: {$addToSet: "$emp_code"}` guarantees employees with zero logs are counted.

5. **One thing you would change for 100x data.** Pre-compute daily summaries and monthly rollups asynchronously using MongoDB change streams into dedicated read-model collections, converting expensive multi-collection aggregations into sub-millisecond indexed point reads.
