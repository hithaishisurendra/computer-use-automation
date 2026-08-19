# CoreServ: Target Application Spec

A deliberately legacy-styled credit union back-office servicing console. This is the
proxy target for a computer-use automation system. It is not the deliverable itself,
it is the surface the deliverable operates on.

Build this exactly. Do not improve the markup. The ugliness is the point.

## Stack

- Python 3.11+, FastAPI, Jinja2 templates
- In-memory seed data, no database
- No JS framework, no build step, no bundler
- Runs with a single command: `uvicorn app.main:app --port 8800`

Keep it to roughly: `app/main.py`, `app/data.py`, `app/faults.py`, `app/templates/*.html`.

## The flow

```
/                     login (any password accepted, username must be non-empty)
/search               member search form
/search/results       results table, one row per match
/member/{id}          member detail
/member/{id}/subaccount/new    multi-field open sub-account form
/member/{id}/subaccount/confirm  confirmation screen with a reference number
```

Navigation happens through a persistent left nav frame. Content loads in a content frame.

## Hostility rules

These are mandatory. They simulate server-rendered enterprise output.

**Structure is hostile:**
- Real `<frameset>` at the root: a `nav` frame (200px) and a `content` frame
- Every page uses table-based layout, nested at least three levels deep
- Zero `data-testid`, zero `aria-*` attributes, zero semantic landmarks
- Class names are meaningless: `cls_a1`, `cls_b7`, `cls_hdr2`
- Element `id` attributes are server-generated and rotate per render:
  `ctl00_{random6}_grid_r{row}_c{col}`. Regenerate the random segment on every
  response so ids are useless as locators.
- Forms post back to themselves with a hidden `__VIEWSTATE`-style field containing
  an opaque base64 blob
- Duplicate visible text is everywhere: a "View" link in every results row, a
  "Submit" button present in both the nav frame and the content frame
- Label whitespace and casing are inconsistent: `Member ID :`, `member id:`,
  `Member  Id`

**Controls stay real:**
- Use genuine `<a>`, `<button>`, `<input>`, `<select>`, `<table>` with `<tr>`/`<th>`/`<td>`
- Every control has visible text or an adjacent label
- No click handlers on `<div>` or `<td>`
- No canvas, no shadow DOM, no web components

This split is intentional and defensible: real legacy enterprise apps are
semantically poor in layout while their controls remain genuine HTML elements,
because that is what server-rendered ASP.NET and JSP emit. Role and accessible
name survive terrible nesting. That is exactly what makes an accessibility-tree
perception strategy viable here.

## Seed data

Twelve members, deterministic, defined in `app/data.py`.

Each member has:
- `member_id` (5 digits, e.g. `12345`)
- `first_name`, `last_name`
- `ssn` (full, fake, format `###-##-####`)
- `date_of_birth`
- `phone`, `email`, `address`
- `status`: one of `active`, `restricted`, `closed`
- `accounts`: list of `{account_number (10 digits), type, balance, opened_date}`

Include full SSNs and full account numbers. They are needed to demonstrate
redaction in artifacts and logs. Use obviously fake values.

Make at least one member `restricted` and at least one `closed`.
Give at least three members the same last name so search returns multiple rows.

## Sub-account form

Fields on `/member/{id}/subaccount/new`:
- Account type (select: Savings, Money Market, Certificate, Holiday Club)
- Initial deposit (text input, dollars)
- Nickname (text input, optional)
- Statement delivery (radio: Paper, Electronic)
- Terms acknowledgment (checkbox, required)

Validation, rendered as an inline error block above the form:
- Initial deposit must parse as a number and be at least 25.00
- Terms checkbox must be checked
- Nickname, if provided, must be 30 characters or fewer

On success, redirect to the confirmation screen showing a reference number in the
form `SA-{8 hex chars}` plus a summary of what was opened.

## Fault injection

Add a control endpoint. This is not part of the simulated app's own UI.

```
POST /_faults        body: {"fault": "<name>", "enabled": true|false}
GET  /_faults        returns current flags
POST /_faults/reset  clears all
```

Faults are server-side flags, not URL parameters. This matters: it lets the same
artifact run with the same inputs and produce a different outcome because the
world changed, not because the run was edited.

Supported faults:

| Name | Behavior |
|---|---|
| `member_not_found` | Search returns a "No records match your criteria." page |
| `restricted_member` | Member detail returns "You do not have permission to view this record." |
| `maintenance_interstitial` | A modal-style overlay table appears on next page load with a "Continue" button that dismisses it |
| `slow_response` | Inject a 6 second delay before rendering |
| `session_expired` | Any request bounces to the login page with "Your session has ended." |
| `validation_error` | Sub-account submission always returns "Deposit amount could not be processed." regardless of input |
| `server_error` | Return a 500 page with "An unexpected error occurred. Reference: ERR-{hex}" |

Build all seven. Each is a few lines. Only some will be used in the final evidence.

## Tenant variant

Read `TENANT` from the environment, default `northridge`.

`northridge` (base):
- Institution name "Northridge Credit Union"
- Field label "Member ID"
- Results columns: Member ID, Name, Status, Branch
- Confirmation heading "Sub-Account Opened"

`cascade` (variant):
- Institution name "Cascade Federal Credit Union"
- Field label "Account Number"
- Results columns: Name, Account Number, Branch, Status (order changed)
- Confirmation heading "New Sub-Account Confirmation"
- Slightly different nav wording: "Member Search" becomes "Find Member"

Same underlying product, same routes, same flow. This is the stand-in for two
tenants running the same vendor software configured differently.

Add a version string in the page footer: `CoreServ 4.2.1` for northridge,
`CoreServ 4.2.3` for cascade. This becomes a drift-detection signal later.

## Non-goals

Do not build any of these:
- A database or ORM
- Real authentication, sessions beyond a cookie holding a username
- CSS beyond minimal grey 1998 styling
- Pagination
- Any JavaScript beyond what a frameset needs
- Tests for the app itself

## Acceptance checks

Before moving on, verify by hand:

1. `uvicorn app.main:app --port 8800` starts with no arguments beyond the port
2. Login, search by last name, get a multi-row results table
3. Click View on a specific row, land on the right member
4. Open a sub-account, hit the validation error, then succeed
5. Element ids visibly differ between two loads of the same page
6. `TENANT=cascade` changes labels and column order without breaking routes
7. Each of the seven faults produces its described behavior
8. In Chrome DevTools, the Accessibility pane shows real roles and names for
   every interactive control despite the nested tables
