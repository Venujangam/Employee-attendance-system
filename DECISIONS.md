# Technical Decisions

**Indexes.** Employees have a unique `emp_code` index, `department` for exact filters, `(department, emp_code)` for sorted filtered lists, and `joined_on` for period headcount. Attendance has unique `(emp_code, date)`, `(emp_code, punch_in)`, `(emp_code, date desc)`, `(date desc, emp_code asc)`, plus `date` and `status` indexes for month/range lookup, punch-out selection, employee history, the global feed, and status filters. I considered a status/date/sort compound index but deferred it without workload evidence; every index adds write and storage cost.

**Punch-in race.** Both identical requests can pass the preliminary read. The unique `(emp_code, date)` constraint allows one insert; the other raises `DuplicateKeyError`, which the API maps to 409.

**Ties.** MongoDB `$rank` gives tied totals the same rank. Filtering after rank returns the complete tie group at `limit`, ordered by employee code.

**Headcount.** The department summary starts from eligible employees and uses `$lookup` with `preserveNullAndEmptyArrays`, so an employee with no logs still reaches the group and counts once.

**100x scale.** I would benchmark the workload, then introduce pre-aggregated monthly summaries with a defined refresh strategy while retaining raw attendance logs as the source of truth.
