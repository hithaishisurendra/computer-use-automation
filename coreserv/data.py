"""In-memory seed data for CoreServ. No database, deterministic on every boot."""

MEMBERS = [
    {
        "member_id": "10001",
        "first_name": "John",
        "last_name": "Smith",
        "ssn": "412-88-2201",
        "date_of_birth": "1978-03-14",
        "phone": "555-201-4471",
        "email": "john.smith@example.com",
        "address": "118 Alder St, Northridge, CA 91324",
        "status": "active",
        "branch": "Downtown",
        "accounts": [
            {"account_number": "4471820019", "type": "Checking", "balance": 2140.55, "opened_date": "2011-06-01"},
            {"account_number": "4471820020", "type": "Savings", "balance": 8320.10, "opened_date": "2011-06-01"},
        ],
    },
    {
        "member_id": "10002",
        "first_name": "Mary",
        "last_name": "Nguyen",
        "ssn": "398-14-7723",
        "date_of_birth": "1985-11-02",
        "phone": "555-201-8832",
        "email": "mary.nguyen@example.com",
        "address": "44 Poplar Ave, Northridge, CA 91324",
        "status": "active",
        "branch": "Eastside",
        "accounts": [
            {"account_number": "5582910044", "type": "Checking", "balance": 950.00, "opened_date": "2015-02-19"},
        ],
    },
    {
        "member_id": "10003",
        "first_name": "Robert",
        "last_name": "Johnson",
        "ssn": "501-22-9981",
        "date_of_birth": "1969-07-30",
        "phone": "555-201-1120",
        "email": "robert.johnson@example.com",
        "address": "902 Cedar Ln, Northridge, CA 91324",
        "status": "active",
        "branch": "Westgate",
        "accounts": [
            {"account_number": "1120839972", "type": "Savings", "balance": 15230.44, "opened_date": "2003-09-12"},
        ],
    },
    {
        "member_id": "10004",
        "first_name": "Linda",
        "last_name": "Nguyen",
        "ssn": "477-63-3320",
        "date_of_birth": "1991-01-22",
        "phone": "555-201-6674",
        "email": "linda.nguyen@example.com",
        "address": "76 Magnolia Dr, Northridge, CA 91324",
        "status": "restricted",
        "branch": "Downtown",
        "accounts": [
            {"account_number": "6674209981", "type": "Checking", "balance": 112.30, "opened_date": "2019-04-08"},
        ],
    },
    {
        "member_id": "10005",
        "first_name": "James",
        "last_name": "Brown",
        "ssn": "339-55-1102",
        "date_of_birth": "1974-05-17",
        "phone": "555-201-3390",
        "email": "james.brown@example.com",
        "address": "230 Birch Ct, Northridge, CA 91324",
        "status": "active",
        "branch": "Eastside",
        "accounts": [
            {"account_number": "3390182274", "type": "Checking", "balance": 4410.02, "opened_date": "2008-12-01"},
            {"account_number": "3390182275", "type": "Certificate", "balance": 20000.00, "opened_date": "2021-01-15"},
        ],
    },
    {
        "member_id": "10006",
        "first_name": "Patricia",
        "last_name": "Nguyen",
        "ssn": "265-40-8817",
        "date_of_birth": "1988-09-09",
        "phone": "555-201-7743",
        "email": "patricia.nguyen@example.com",
        "address": "19 Willow Way, Northridge, CA 91324",
        "status": "active",
        "branch": "Westgate",
        "accounts": [
            {"account_number": "7743092284", "type": "Savings", "balance": 3305.90, "opened_date": "2017-08-23"},
        ],
    },
    {
        "member_id": "10007",
        "first_name": "Michael",
        "last_name": "Davis",
        "ssn": "184-77-2266",
        "date_of_birth": "1963-02-28",
        "phone": "555-201-9915",
        "email": "michael.davis@example.com",
        "address": "870 Chestnut Rd, Northridge, CA 91324",
        "status": "closed",
        "branch": "Downtown",
        "accounts": [
            {"account_number": "9915223367", "type": "Checking", "balance": 0.00, "opened_date": "1999-03-04"},
        ],
    },
    {
        "member_id": "10008",
        "first_name": "Barbara",
        "last_name": "Wilson",
        "ssn": "552-19-4408",
        "date_of_birth": "1980-06-11",
        "phone": "555-201-2245",
        "email": "barbara.wilson@example.com",
        "address": "63 Spruce St, Northridge, CA 91324",
        "status": "active",
        "branch": "Eastside",
        "accounts": [
            {"account_number": "2245781193", "type": "Money Market", "balance": 12750.60, "opened_date": "2013-10-30"},
        ],
    },
    {
        "member_id": "10009",
        "first_name": "William",
        "last_name": "Garcia",
        "ssn": "610-33-7752",
        "date_of_birth": "1972-12-05",
        "phone": "555-201-5567",
        "email": "william.garcia@example.com",
        "address": "301 Redwood Blvd, Northridge, CA 91324",
        "status": "active",
        "branch": "Westgate",
        "accounts": [
            {"account_number": "5567341128", "type": "Checking", "balance": 1875.25, "opened_date": "2016-07-19"},
        ],
    },
    {
        "member_id": "10010",
        "first_name": "Elizabeth",
        "last_name": "Martinez",
        "ssn": "429-88-1156",
        "date_of_birth": "1995-04-27",
        "phone": "555-201-8890",
        "email": "elizabeth.martinez@example.com",
        "address": "48 Sycamore Ave, Northridge, CA 91324",
        "status": "active",
        "branch": "Downtown",
        "accounts": [
            {"account_number": "8890451129", "type": "Savings", "balance": 640.75, "opened_date": "2020-11-02"},
        ],
    },
    {
        "member_id": "10011",
        "first_name": "David",
        "last_name": "Anderson",
        "ssn": "347-26-9903",
        "date_of_birth": "1966-08-19",
        "phone": "555-201-4432",
        "email": "david.anderson@example.com",
        "address": "512 Fir St, Northridge, CA 91324",
        "status": "active",
        "branch": "Eastside",
        "accounts": [
            {"account_number": "4432567781", "type": "Checking", "balance": 9830.15, "opened_date": "2005-05-25"},
        ],
    },
    {
        "member_id": "10012",
        "first_name": "Jennifer",
        "last_name": "Thomas",
        "ssn": "295-71-3348",
        "date_of_birth": "1983-10-08",
        "phone": "555-201-6601",
        "email": "jennifer.thomas@example.com",
        "address": "27 Elm Ct, Northridge, CA 91324",
        "status": "active",
        "branch": "Westgate",
        "accounts": [
            {"account_number": "6601893347", "type": "Holiday Club", "balance": 425.00, "opened_date": "2022-03-30"},
        ],
    },
]

TENANT_CONFIG = {
    "northridge": {
        "inst_name": "Northridge Credit Union",
        "id_label": "Member ID",
        "id_label_alt": "member id:",
        "id_label_alt2": "Member  Id",
        "nav_search_label": "Member Search",
        "results_columns": ["member_id", "name", "status", "branch"],
        "confirm_heading": "Sub-Account Opened",
        "version": "CoreServ 4.2.1",
    },
    "cascade": {
        "inst_name": "Cascade Federal Credit Union",
        "id_label": "Account Number",
        "id_label_alt": "account number:",
        "id_label_alt2": "Account  Number",
        "nav_search_label": "Find Member",
        "results_columns": ["name", "account_number", "branch", "status"],
        "confirm_heading": "New Sub-Account Confirmation",
        "version": "CoreServ 4.2.3",
    },
}


def get_member(member_id: str):
    for m in MEMBERS:
        if m["member_id"] == member_id:
            return m
    return None


def find_by_identifier(identifier: str, tenant: str):
    identifier = identifier.strip()
    if not identifier:
        return []
    if tenant == "cascade":
        for m in MEMBERS:
            for acc in m["accounts"]:
                if acc["account_number"] == identifier:
                    return [m]
        return []
    for m in MEMBERS:
        if m["member_id"] == identifier:
            return [m]
    return []


def find_by_last_name(last_name: str):
    last_name = last_name.strip().lower()
    if not last_name:
        return []
    return [m for m in MEMBERS if last_name in m["last_name"].lower()]
