import base64
import os
import secrets
import uuid
from typing import Optional

from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import asyncio

from coreserv import data, faults

TENANT = os.environ.get("TENANT", "northridge")
if TENANT not in data.TENANT_CONFIG:
    TENANT = "northridge"
CFG = data.TENANT_CONFIG[TENANT]

app = FastAPI()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def rid(suffix: str) -> str:
    return f"ctl00_{secrets.token_hex(3)}_{suffix}"


def viewstate() -> str:
    return base64.b64encode(secrets.token_bytes(48)).decode()


templates.env.globals["rid"] = rid
templates.env.globals["viewstate"] = viewstate
templates.env.globals["cfg"] = CFG
templates.env.globals["tenant"] = TENANT

SESSION_COOKIE = "csu"


def current_user(request: Request) -> Optional[str]:
    return request.cookies.get(SESSION_COOKIE)


def render(request: Request, name: str, ctx: dict, status_code: int = 200) -> HTMLResponse:
    ctx.setdefault("interstitial", faults.is_enabled("maintenance_interstitial"))
    ctx.setdefault("current_path", str(request.url.path) + (("?" + request.url.query) if request.url.query else ""))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def login_page(request: Request, message: str = "", status_code: int = 200) -> HTMLResponse:
    return render(request, "login.html", {"message": message}, status_code=status_code)


async def apply_global_faults(request: Request) -> Optional[HTMLResponse]:
    """Checked at the top of every authenticated content route. Order matters:
    a bounced session pre-empts everything else, then latency, then a hard 500.
    """
    if faults.is_enabled("session_expired"):
        return login_page(request, "Your session has ended.")
    if faults.is_enabled("slow_response"):
        await asyncio.sleep(6)
    if faults.is_enabled("server_error"):
        ref = f"ERR-{secrets.token_hex(4)}"
        return render(request, "error_500.html", {"reference": ref}, status_code=500)
    return None


def require_login(request: Request) -> Optional[HTMLResponse]:
    if current_user(request) is None:
        return render(request, "not_logged_in.html", {})
    return None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def login_get(request: Request):
    if current_user(request):
        return RedirectResponse(url="/home", status_code=303)
    return login_page(request)


@app.post("/", response_class=HTMLResponse)
async def login_post(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    viewstate_field: str = Form("", alias="__VIEWSTATE"),
):
    if not username.strip():
        return login_page(request, "Username is required.", status_code=400)
    resp = RedirectResponse(url="/home", status_code=303)
    resp.set_cookie(SESSION_COOKIE, username.strip(), httponly=True)
    return resp


@app.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------------------
# Frameset + nav
# ---------------------------------------------------------------------------

@app.get("/home", response_class=HTMLResponse)
async def home(request: Request):
    if current_user(request) is None:
        return RedirectResponse(url="/", status_code=303)
    return render(request, "home.html", {})


@app.get("/nav", response_class=HTMLResponse)
async def nav(request: Request):
    blocked = require_login(request)
    if blocked:
        return blocked
    return render(request, "nav.html", {"username": current_user(request)})


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.get("/search", response_class=HTMLResponse)
async def search_get(request: Request):
    blocked = require_login(request)
    if blocked:
        return blocked
    fault_resp = await apply_global_faults(request)
    if fault_resp:
        return fault_resp
    return render(request, "search.html", {"error": ""})


@app.post("/search", response_class=HTMLResponse)
async def search_post(
    request: Request,
    last_name: str = Form(""),
    identifier: str = Form(""),
    viewstate_field: str = Form("", alias="__VIEWSTATE"),
):
    blocked = require_login(request)
    if blocked:
        return blocked
    fault_resp = await apply_global_faults(request)
    if fault_resp:
        return fault_resp

    if not last_name.strip() and not identifier.strip():
        return render(
            request,
            "search.html",
            {"error": f"Enter a Last Name or {CFG['id_label']} to search."},
            status_code=400,
        )

    params = []
    if last_name.strip():
        params.append(f"last_name={last_name.strip()}")
    if identifier.strip():
        params.append(f"identifier={identifier.strip()}")
    qs = "&".join(params)
    return RedirectResponse(url=f"/search/results?{qs}", status_code=303)


@app.get("/search/results", response_class=HTMLResponse)
async def search_results(request: Request, last_name: str = "", identifier: str = ""):
    blocked = require_login(request)
    if blocked:
        return blocked
    fault_resp = await apply_global_faults(request)
    if fault_resp:
        return fault_resp

    if faults.is_enabled("member_not_found"):
        return render(request, "results.html", {"members": [], "no_records": True})

    if identifier:
        members = data.find_by_identifier(identifier, TENANT)
    elif last_name:
        members = data.find_by_last_name(last_name)
    else:
        members = []

    return render(request, "results.html", {"members": members, "no_records": len(members) == 0})


# ---------------------------------------------------------------------------
# Member detail
# ---------------------------------------------------------------------------

@app.get("/member/{member_id}", response_class=HTMLResponse)
async def member_detail(request: Request, member_id: str):
    blocked = require_login(request)
    if blocked:
        return blocked
    fault_resp = await apply_global_faults(request)
    if fault_resp:
        return fault_resp

    if faults.is_enabled("restricted_member"):
        return render(
            request,
            "error_permission.html",
            {"message": "You do not have permission to view this record."},
            status_code=403,
        )

    member = data.get_member(member_id)
    if member is None:
        return render(
            request,
            "results.html",
            {"members": [], "no_records": True},
            status_code=404,
        )

    return render(request, "member.html", {"member": member})


# ---------------------------------------------------------------------------
# Sub-account open flow
# ---------------------------------------------------------------------------

ACCOUNT_TYPES = ["Savings", "Money Market", "Certificate", "Holiday Club"]


@app.get("/member/{member_id}/subaccount/new", response_class=HTMLResponse)
async def subaccount_new_get(request: Request, member_id: str):
    blocked = require_login(request)
    if blocked:
        return blocked
    fault_resp = await apply_global_faults(request)
    if fault_resp:
        return fault_resp

    member = data.get_member(member_id)
    if member is None:
        return render(request, "results.html", {"members": [], "no_records": True}, status_code=404)

    return render(
        request,
        "subaccount_new.html",
        {
            "member": member,
            "errors": [],
            "account_types": ACCOUNT_TYPES,
            "form": {"account_type": ACCOUNT_TYPES[0], "deposit": "", "nickname": "", "delivery": "Paper", "terms": False},
        },
    )


@app.post("/member/{member_id}/subaccount/new", response_class=HTMLResponse)
async def subaccount_new_post(
    request: Request,
    member_id: str,
    account_type: str = Form(""),
    deposit: str = Form(""),
    nickname: str = Form(""),
    delivery: str = Form("Paper"),
    terms: Optional[str] = Form(None),
    viewstate_field: str = Form("", alias="__VIEWSTATE"),
):
    blocked = require_login(request)
    if blocked:
        return blocked
    fault_resp = await apply_global_faults(request)
    if fault_resp:
        return fault_resp

    member = data.get_member(member_id)
    if member is None:
        return render(request, "results.html", {"members": [], "no_records": True}, status_code=404)

    form = {
        "account_type": account_type,
        "deposit": deposit,
        "nickname": nickname,
        "delivery": delivery,
        "terms": bool(terms),
    }

    if faults.is_enabled("validation_error"):
        return render(
            request,
            "subaccount_new.html",
            {
                "member": member,
                "errors": ["Deposit amount could not be processed."],
                "account_types": ACCOUNT_TYPES,
                "form": form,
            },
            status_code=400,
        )

    errors = []
    deposit_value = None
    try:
        deposit_value = float(deposit.strip())
    except (TypeError, ValueError):
        errors.append("Initial deposit must be a valid number.")
    if deposit_value is not None and deposit_value < 25.00:
        errors.append("Initial deposit must be at least 25.00.")
    if not terms:
        errors.append("You must acknowledge the terms to continue.")
    if nickname and len(nickname) > 30:
        errors.append("Nickname must be 30 characters or fewer.")

    if errors:
        return render(
            request,
            "subaccount_new.html",
            {"member": member, "errors": errors, "account_types": ACCOUNT_TYPES, "form": form},
            status_code=400,
        )

    ref = f"SA-{secrets.token_hex(4)}"
    qs = (
        f"ref={ref}&account_type={account_type}&deposit={deposit_value:.2f}"
        f"&nickname={nickname}&delivery={delivery}"
    )
    return RedirectResponse(url=f"/member/{member_id}/subaccount/confirm?{qs}", status_code=303)


@app.get("/member/{member_id}/subaccount/confirm", response_class=HTMLResponse)
async def subaccount_confirm(
    request: Request,
    member_id: str,
    ref: str = "",
    account_type: str = "",
    deposit: str = "",
    nickname: str = "",
    delivery: str = "",
):
    blocked = require_login(request)
    if blocked:
        return blocked
    fault_resp = await apply_global_faults(request)
    if fault_resp:
        return fault_resp

    member = data.get_member(member_id)
    if member is None:
        return render(request, "results.html", {"members": [], "no_records": True}, status_code=404)

    return render(
        request,
        "subaccount_confirm.html",
        {
            "member": member,
            "ref": ref,
            "account_type": account_type,
            "deposit": deposit,
            "nickname": nickname,
            "delivery": delivery,
        },
    )


# ---------------------------------------------------------------------------
# Maintenance interstitial dismissal (part of the simulated app's own UI)
# ---------------------------------------------------------------------------

@app.post("/_dismiss_interstitial")
async def dismiss_interstitial(return_to: str = Form("/home")):
    faults.set_flag("maintenance_interstitial", False)
    return RedirectResponse(url=return_to, status_code=303)


# ---------------------------------------------------------------------------
# Fault control endpoint (NOT part of the simulated app's own UI)
# ---------------------------------------------------------------------------

@app.get("/_faults")
async def faults_get():
    return JSONResponse(faults.get_all())


@app.post("/_faults")
async def faults_post(request: Request):
    body = await request.json()
    name = body.get("fault")
    enabled = bool(body.get("enabled"))
    ok = faults.set_flag(name, enabled)
    if not ok:
        return JSONResponse({"error": f"unknown fault: {name}"}, status_code=400)
    return JSONResponse(faults.get_all())


@app.post("/_faults/reset")
async def faults_reset():
    faults.reset_all()
    return JSONResponse(faults.get_all())
