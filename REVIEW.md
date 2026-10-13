# Review Findings

| # | Where | Defect and how to reproduce | Fix |
|---|---|---|---|
| 1 | `list_employees` | `GET /employees` returned BSON `created_at` as an ISO string instead of epoch milliseconds. | Serialize each item with `serialize_employee`; verified the response values are integers and `_id` is absent. |
| 2 | `EmployeeIn.joined_on` | `date.fromisoformat` accepted compact dates such as `20260701`, despite the required `YYYY-MM-DD` representation. | Added a strict date pattern and retained calendar validation; covered compact and impossible dates. |
| 3 | Attendance query validation | Invalid status/date query values were accepted as strings rather than rejected with 422. | Use a status literal and `date` parameters; real HTTP requests for invalid status/date now return 422. |
| 4 | Request models | Optional epoch/status fields accepted explicit `null`, although the contract allows omission but not null. | Validators reject explicit null while omitted values retain defaults; covered in model tests. |
| 5 | `punch_out` | A timestamp before the employee's only punch-in returned 404, not the specified 422. | Check for a later punch-in when no eligible record exists; verified before-punch-in returns 422 and no-record returns 404. |
| 6 | `regularize_attendance` | Valid overnight punch-ins after midnight were compared to the calendar date, not the shift-start attendance date. | Validate via `record_date_for_punch`; the overnight correction test retains the prior shift date. |
| 7 | `regularize_attendance` | Seeded records may omit `history`; matching `history: []` then made a valid correction fail with 409. | Optimistically match an absent field when absent and append with `$push`; verified one audit entry is written. |
| 8 | `department_trend` | The endpoint fetched employees and logs in per-day Python loops, contrary to the aggregation requirement and scale target. | Generate dates/headcount/stats in MongoDB and use `$setWindowFields` for the moving average; tested gaps, joiners, weekends, and rounding. |
| 9 | `late_leaderboard` | Ranking and cutoff filtering ran in Python rather than the required tie-aware MongoDB pipeline. | Use `$rank` after department filtering and retain every row with rank at or below the cutoff; tested a tie at rank 1. |
| 10 | `/admin/explain/{endpoint}` | Find explains used default query-planner verbosity; several analytics explains used simplified pipelines rather than endpoint pipelines. | Request `executionStats`; share the attendance query and monthly, department, leaderboard, and trend builders with their explain paths. |
| 11 | Index setup | The attendance feed's mixed-direction sort and filtered employee list lacked matching compound indexes. | Added sort/filter indexes and verified the five required explain paths against 100,000 attendance records. |
| 12 | `ensure_indexes` | Startup swallowed index-creation errors, allowing the service to appear started without required guarantees. | Let creation failures surface during startup; verified the active unique and query indexes in the disposable test database. |

## Reviewed and Not Defects

- Employee-code and attendance natural-key uniqueness are enforced by unique MongoDB indexes, so the pre-insert checks are not the concurrency guarantee; duplicate-key handling is.
- Punch-out uses a conditional update on the open record, and regularization uses a history snapshot in its update filter. Concurrent requests therefore cannot both silently win.
- The time helpers truncate instants before storage, use IST attendance dates, floor late/overtime minutes, and use decimal half-up rounding. Boundary tests cover these rules.
- Department summary starts from employees and preserves empty lookup results, so employees without logs count toward headcount. Average work hours are over qualifying records, not employee averages.

## Verification

`python -m unittest discover -s tests -v`: 10 tests passed. HTTP smoke checks passed for readiness, listing, monthly analytics, explain output, invalid query 422s, employee creation/duplicate conflict, punch-in/out, and regularization. On MongoDB 8.0.34, all five explainable query shapes on a disposable 100,000-record dataset showed `IXSCAN`, `executionStats`, and no `COLLSCAN`. This does not replace testing on the grader's MongoDB 7.0 dataset.
