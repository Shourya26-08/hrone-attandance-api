# DECISIONS.md

1. **Indexes.** Employee indexes: unique `emp_code` for identity;
`(department, emp_code)` for lists; `(department, joined_on)` for trend headcount;
`(joined_on, department)` for monthly headcount. Attendance indexes: unique
`(emp_code, date)` for daily uniqueness/monthly lookups; `(date desc, emp_code)`
for lists/month scans; `(status, date desc, emp_code)` for filtered lists;
`(emp_code, punch_in desc)` for latest-punch selection. Rejected `work_hours`:
nothing filters or sorts on it.

2. **Punch-in race.** Both requests read the employee, then try inserting the
same natural key. The unique index permits one insert (201); the other raises
`DuplicateKeyError`, translated to 409. There is no check-then-insert.

3. **Ties.** `$rank` produces competition ranks: 1, 2, 2, 4. Limit 2 returns
three rows, retaining both employees tied at the cutoff. Department filtering
precedes ranking; employee codes break display ties.

4. **Headcount.** Start from employees joined by month end, left-lookup monthly
logs, replace missing metrics with zeros, then count one per employee. No log
is needed for that contribution.

5. **100x scale.** Measure slow queries first; introduce daily rollups with
correction-triggered rebuilds, retaining raw logs and history for audits.
