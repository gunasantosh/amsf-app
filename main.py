import asyncio
import calendar
import os
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from math import floor
from typing import Optional
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

import settings  # noqa: F401
from app_time import format_local_date, format_local_datetime, local_now, utc_now
from auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    get_current_user_from_cookie,
    get_password_hash,
    require_auth,
    verify_password,
)
from mailer import send_email, send_email_sync
from activity import dispatch_queued, queue_notification, queue_notifications, record_event
from database import (
    AuditEvent,
    Contribution,
    Loan,
    LoanRepayment,
    LoanVote,
    Member,
    NotificationLog,
    PasswordResetToken,
    get_db,
    get_display_name,
    init_db,
    months_active_since,
)
from money import ZERO, money, money_sum

init_db()

app = FastAPI(title="AMSF Web App")
app.add_middleware(GZipMiddleware, minimum_size=1000)

os.makedirs("static/css", exist_ok=True)
os.makedirs("static/js", exist_ok=True)
os.makedirs("static/img", exist_ok=True)
os.makedirs("templates", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.filters["local_datetime"] = format_local_datetime
templates.env.filters["local_date"] = format_local_date

MONTHLY_BASELINE = Decimal("200.00")
RESET_TOKEN_HOURS = 2
PUBLIC_BASE_URL = os.getenv("AMSF_PUBLIC_BASE_URL", "").rstrip("/")


@app.middleware("http")
async def disable_dynamic_response_cache(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


def majority_threshold(member_count: int) -> int:
    return floor(member_count / 2) + 1


def month_difference(start: datetime, end: datetime) -> int:
    return max(1, (end.year - start.year) * 12 + (end.month - start.month) + 1)


def add_months(start: datetime, months: int) -> datetime:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def format_period_label(dt: datetime, period: str) -> str:
    if period == "weekly":
        iso_year, iso_week, _ = dt.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if period == "monthly":
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y-%m-%d")


def aggregate_contribution_series(contributions: list[Contribution], period: str) -> dict:
    grouped: dict[str, Decimal] = {}
    running_total = ZERO
    labels = []
    data = []
    for contribution in contributions:
        key = format_period_label(contribution.transfer_date, period)
        grouped[key] = grouped.get(key, ZERO) + money(contribution.amount)
    for label in sorted(grouped.keys()):
        running_total += grouped[label]
        labels.append(label)
        data.append(float(money(running_total)))
    return {"labels": labels, "data": data}


def loan_projection(loan: Loan) -> dict:
    if loan.status != "Sanctioned" or loan.interest_rate is None or loan.due_date is None:
        return {"total_return": None, "months": None, "monthly_installment": None, "approved_repayments": ZERO, "remaining_balance": None}

    principal = money(loan.amount_requested)
    total_return = principal + (principal * loan.interest_rate / 100)
    months = loan.repayment_months or month_difference(utc_now(), loan.due_date)
    approved_repayments = money_sum(repayment.amount for repayment in loan.repayments if repayment.status == "Approved")
    remaining_balance = max(ZERO, money(total_return - approved_repayments))
    return {
        "total_return": money(total_return),
        "months": months,
        "monthly_installment": money(total_return / months),
        "approved_repayments": approved_repayments,
        "remaining_balance": remaining_balance,
    }


def redirect_with_message(path: str, message: str, flash_type: str = "info") -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    url = f"{path}{separator}flash={quote(message)}&flash_type={quote(flash_type)}"
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


def build_template_context(request: Request, **extra: object) -> dict:
    context = {
        "request": request,
        "flash": request.query_params.get("flash"),
        "flash_type": request.query_params.get("flash_type", "info"),
    }
    context.update(extra)
    return context


def send_reset_email_task(recipient: str, reset_link: str) -> None:
    send_email_sync(
        recipient,
        "AMSF password reset",
        (
            "A password reset was requested for your AMSF account.\n\n"
            f"Open this link to reset your password:\n{reset_link}\n\n"
            "This link expires in 2 hours."
        ),
        (
            "<html><body style=\"font-family:Arial,sans-serif;background:#0d1324;color:#e2e8f0;padding:24px;\">"
            "<div style=\"max-width:560px;margin:0 auto;background:#11192e;border:1px solid #24314f;border-radius:18px;padding:28px;\">"
            "<h2 style=\"margin:0 0 16px;color:#5eead4;\">AMSF Password Reset</h2>"
            "<p style=\"line-height:1.6;\">A password reset was requested for your AMSF account.</p>"
            f"<p style=\"line-height:1.6;\"><a href=\"{reset_link}\" style=\"display:inline-block;background:#5eead4;color:#081120;text-decoration:none;padding:12px 18px;border-radius:10px;font-weight:700;\">Reset Password</a></p>"
            f"<p style=\"line-height:1.6;word-break:break-all;\">If the button does not work, use this link:<br>{reset_link}</p>"
            "<p style=\"line-height:1.6;color:#94a3b8;\">This link expires in 2 hours.</p>"
            "</div></body></html>"
        ),
    )


async def send_reset_email(recipient: str, reset_link: str) -> None:
    await send_email(
        recipient,
        "AMSF password reset",
        (
            "A password reset was requested for your AMSF account.\n\n"
            f"Open this link to reset your password:\n{reset_link}\n\n"
            "This link expires in 2 hours."
        ),
        (
            "<html><body style=\"font-family:Arial,sans-serif;background:#0d1324;color:#e2e8f0;padding:24px;\">"
            "<div style=\"max-width:560px;margin:0 auto;background:#11192e;border:1px solid #24314f;border-radius:18px;padding:28px;\">"
            "<h2 style=\"margin:0 0 16px;color:#5eead4;\">AMSF Password Reset</h2>"
            "<p style=\"line-height:1.6;\">A password reset was requested for your AMSF account.</p>"
            f"<p style=\"line-height:1.6;\"><a href=\"{reset_link}\" style=\"display:inline-block;background:#5eead4;color:#081120;text-decoration:none;padding:12px 18px;border-radius:10px;font-weight:700;\">Reset Password</a></p>"
            f"<p style=\"line-height:1.6;word-break:break-all;\">If the button does not work, use this link:<br>{reset_link}</p>"
            "<p style=\"line-height:1.6;color:#94a3b8;\">This link expires in 2 hours.</p>"
            "</div></body></html>"
        ),
    )


def build_reset_link(request: Request, token: str) -> str:
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/reset-password?token={token}"
    return str(request.url_for("reset_password_page")) + f"?token={token}"


def require_admin(current_user: Member = Depends(require_auth)) -> Member:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return current_user


def build_vote_statuses(loan: Loan, members: list[Member], display_lookup: dict[int, str], original_lookup: dict[int, str] | None = None) -> list[dict]:
    vote_lookup = {vote.voter_id: vote.vote for vote in loan.votes}
    vote_time_lookup = {vote.voter_id: vote.created_at for vote in loan.votes}
    statuses = []
    for member in members:
        if member.id == loan.requester_id:
            statuses.append(
                {
                    "member_id": member.id,
                    "display_name": display_lookup.get(member.id, f"Member {member.id}"),
                    "original_name": original_lookup.get(member.id) if original_lookup else member.original_name,
                    "status": "Requester",
                    "timestamp": loan.created_at,
                }
            )
            continue
        statuses.append(
            {
                "member_id": member.id,
                "display_name": display_lookup.get(member.id, f"Member {member.id}"),
                "original_name": original_lookup.get(member.id) if original_lookup else member.original_name,
                "status": vote_lookup.get(member.id, "Pending"),
                "timestamp": vote_time_lookup.get(member.id),
            }
        )
    return statuses


def enrich_loans(
    loans: list[Loan],
    members: list[Member],
    display_lookup: dict[int, str],
    original_lookup: dict[int, str] | None = None,
) -> list[dict]:
    enriched = []
    threshold = majority_threshold(len(members))
    for loan in loans:
        approvals = sum(1 for vote in loan.votes if vote.vote == "Approve")
        rejections = sum(1 for vote in loan.votes if vote.vote == "Reject")
        enriched.append(
            {
                "loan": loan,
                "requester_name": display_lookup.get(loan.requester_id, f"Member {loan.requester_id}"),
                "requester_original_name": original_lookup.get(loan.requester_id) if original_lookup else None,
                "approvals": approvals,
                "rejections": rejections,
                "threshold": threshold,
                "projection": loan_projection(loan),
                "vote_statuses": build_vote_statuses(loan, members, display_lookup, original_lookup),
            }
        )
    return enriched


def member_balance_summary(db: Session, member: Member) -> str:
    approved_total = money(
        db.query(func.sum(Contribution.amount))
        .filter(Contribution.member_id == member.id, Contribution.status == "Approved")
        .scalar()
    )
    target_total = money(months_active_since(member.join_date) * MONTHLY_BASELINE)
    difference = money(target_total - approved_total)
    balance_label = "Pending dues" if difference > ZERO else "Advance balance"
    return (
        f"Approved contributions: Rs {approved_total:,.2f}\n"
        f"Current contribution target: Rs {target_total:,.2f}\n"
        f"{balance_label}: Rs {abs(difference):,.2f}"
    )


def loan_vote_summary(loan: Loan, members: list[Member]) -> str:
    vote_lookup = {vote.voter_id: vote.vote for vote in loan.votes}
    approvers = [get_display_name(member) for member in members if vote_lookup.get(member.id) == "Approve"]
    rejectors = [get_display_name(member) for member in members if vote_lookup.get(member.id) == "Reject"]
    pending = [
        get_display_name(member)
        for member in members
        if member.id != loan.requester_id and member.id not in vote_lookup
    ]
    return (
        f"Approvals ({len(approvers)}): {', '.join(approvers) or 'None'}\n"
        f"Rejections ({len(rejectors)}): {', '.join(rejectors) or 'None'}\n"
        f"Yet to respond ({len(pending)}): {', '.join(pending) or 'None'}"
    )


def build_member_contribution_rows(members: list[Member], db: Session) -> list[dict]:
    now = local_now()
    rows = []
    for member in members:
        approved_total = (
            db.query(func.sum(Contribution.amount))
            .filter(Contribution.member_id == member.id, Contribution.status == "Approved")
            .scalar()
            or ZERO
        )
        pending_total = (
            db.query(func.sum(Contribution.amount))
            .filter(Contribution.member_id == member.id, Contribution.status == "Pending")
            .scalar()
            or ZERO
        )
        reverted_total = (
            db.query(func.sum(Contribution.amount))
            .filter(Contribution.member_id == member.id, Contribution.status == "Reverted")
            .scalar()
            or ZERO
        )
        target_total = months_active_since(member.join_date, now) * MONTHLY_BASELINE
        due_amount = target_total - approved_total
        rows.append(
            {
                "member": member,
                "display_name": member.original_name,
                "public_name": get_display_name(member),
                "approved_total": money(approved_total),
                "pending_total": money(pending_total),
                "reverted_total": money(reverted_total),
                "target_total": money(target_total),
                "due_amount": money(abs(due_amount)),
                "due_status": "Pending Dues" if due_amount > 0 else "Advance Balance",
                "completion_percent": round((approved_total / target_total) * 100, 1) if target_total else 0.0,
                "current_month_paid": round(
                    db.query(func.sum(Contribution.amount))
                    .filter(
                        Contribution.member_id == member.id,
                        Contribution.status == "Approved",
                        func.strftime("%Y", Contribution.transfer_date) == now.strftime("%Y"),
                        func.strftime("%m", Contribution.transfer_date) == now.strftime("%m"),
                    )
                    .scalar()
                    or ZERO,
                    2,
                ),
            }
        )
    return sorted(rows, key=lambda row: row["display_name"].lower())


@app.post("/api/login")
def login(
    original_name: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(Member).filter(Member.original_name == original_name.strip()).first()
    if not user or not verify_password(password, user.hashed_password):
        return redirect_with_message("/login", "Incorrect name or password.", "error")

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": user.original_name}, expires_delta=access_token_expires)
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax",
    )
    return response


@app.get("/logout")
def logout(response: Response):
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("access_token")
    return response


@app.post("/api/setup")
def setup_account(
    email: str = Form(...),
    new_password: str = Form(...),
    alias: Optional[str] = Form(None),
    current_user: Member = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    current_user.email = email.strip().lower()
    current_user.hashed_password = get_password_hash(new_password)
    current_user.password_changed = True
    current_user.alias = alias.strip() if alias and alias.strip() else None
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=current_user.id,
        event_type="account.setup_completed",
        entity_type="member",
        entity_id=current_user.id,
        summary=f"{get_display_name(current_user)} completed account setup.",
    )
    db.commit()
    return redirect_with_message("/dashboard", "Account setup completed.", "success")


@app.post("/api/profile/alias")
def update_alias(
    alias: str = Form(...),
    current_user: Member = Depends(require_auth),
    db: Session = Depends(get_db),
):
    current_user.alias = alias.strip() if alias.strip() else None
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=current_user.id,
        event_type="profile.alias_changed",
        entity_type="member",
        entity_id=current_user.id,
        summary=f"{current_user.original_name} updated their display alias.",
    )
    db.commit()
    return redirect_with_message("/dashboard", "Alias updated.", "success")


@app.post("/api/forgot-password")
def forgot_password(
    request: Request,
    background_tasks: BackgroundTasks,
    original_name: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(Member).filter(Member.original_name == original_name.strip()).first()
    if user and user.email:
        db.query(PasswordResetToken).filter(
            PasswordResetToken.member_id == user.id,
            PasswordResetToken.used_at.is_(None),
        ).update({"used_at": utc_now()}, synchronize_session=False)
        token = secrets.token_urlsafe(32)
        reset_record = PasswordResetToken(
            member_id=user.id,
            token=token,
            expires_at=utc_now() + timedelta(hours=RESET_TOKEN_HOURS),
        )
        db.add(reset_record)
        db.commit()

        reset_link = build_reset_link(request, token)
        background_tasks.add_task(send_reset_email_task, user.email, reset_link)

    return redirect_with_message(
        "/forgot-password",
        "If that account has an email on file, a reset link has been prepared.",
        "success",
    )


@app.post("/api/reset-password")
def reset_password(
    token: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db),
):
    reset_record = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.token == token,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at >= utc_now(),
        )
        .first()
    )
    if not reset_record:
        return redirect_with_message("/forgot-password", "Reset link is invalid or expired.", "error")

    reset_record.member.hashed_password = get_password_hash(new_password)
    reset_record.member.password_changed = True
    reset_record.used_at = utc_now()
    db.commit()
    return redirect_with_message("/login", "Password updated. You can sign in now.", "success")


@app.post("/api/contributions/report")
def report_contribution(
    background_tasks: BackgroundTasks,
    amount: Decimal = Form(...),
    transfer_date: str = Form(...),
    current_user: Member = Depends(require_auth),
    db: Session = Depends(get_db),
):
    amount = money(amount)
    if amount <= ZERO:
        return redirect_with_message("/dashboard", "Contribution amount must be greater than zero.", "error")

    contribution = Contribution(
        member_id=current_user.id,
        amount=amount,
        transfer_date=datetime.fromisoformat(transfer_date),
        status="Pending",
    )
    db.add(contribution)
    db.flush()
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=current_user.id,
        event_type="contribution.reported",
        entity_type="contribution",
        entity_id=contribution.id,
        summary=f"{get_display_name(current_user)} reported a contribution of Rs {amount:,.2f}.",
    )
    custodians = db.query(Member).filter(Member.is_admin.is_(True)).all()
    notifications = queue_notifications(
        db,
        custodians,
        subject="Contribution pending approval",
        message=(
            f"{get_display_name(current_user)} reported a contribution of Rs {amount:,.2f} "
            f"for {contribution.transfer_date:%Y-%m-%d}. It is waiting for custodian review."
        ),
        event_type="contribution.reported",
        entity_type="contribution",
        entity_id=contribution.id,
    )
    db.commit()
    dispatch_queued(background_tasks, notifications)
    return redirect_with_message("/dashboard", "Contribution reported and sent for custodian review.", "success")


@app.post("/api/loans/request")
def request_loan(
    background_tasks: BackgroundTasks,
    amount_requested: Decimal = Form(...),
    reason: str = Form(...),
    current_user: Member = Depends(require_auth),
    db: Session = Depends(get_db),
):
    approved_total = (
        db.query(func.sum(Contribution.amount))
        .filter(Contribution.member_id == current_user.id, Contribution.status == "Approved")
        .scalar()
        or ZERO
    )
    amount_requested = money(amount_requested)
    loan_cap = approved_total * 2
    if amount_requested <= ZERO:
        return redirect_with_message("/dashboard", "Loan amount must be greater than zero.", "error")
    if amount_requested > loan_cap:
        return redirect_with_message(
            "/dashboard",
            f"Loan request exceeds your current cap of {loan_cap:.2f}.",
            "error",
        )

    loan = Loan(requester_id=current_user.id, amount_requested=amount_requested, reason=reason.strip(), status="Voting")
    db.add(loan)
    db.flush()
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=current_user.id,
        event_type="loan.requested",
        entity_type="loan",
        entity_id=loan.id,
        summary=f"{get_display_name(current_user)} requested a loan of Rs {amount_requested:,.2f}.",
        details=loan.reason,
    )
    members = db.query(Member).all()
    notifications = queue_notifications(
        db,
        members,
        subject="Loan request opened for voting",
        message=(
            f"{get_display_name(current_user)} requested a loan of Rs {amount_requested:,.2f}.\n"
            f"Reason: {loan.reason}\n"
            "The requester cannot vote on their own request. Other members can review and vote in AMSF."
        ),
        event_type="loan.requested",
        entity_type="loan",
        entity_id=loan.id,
    )
    db.commit()
    dispatch_queued(background_tasks, notifications)
    return redirect_with_message("/dashboard", "Loan request opened for member voting.", "success")


@app.post("/api/loans/{loan_id}/vote")
def vote_on_loan(
    loan_id: int,
    background_tasks: BackgroundTasks,
    vote: str = Form(...),
    current_user: Member = Depends(require_auth),
    db: Session = Depends(get_db),
):
    if vote not in {"Approve", "Reject"}:
        return redirect_with_message("/dashboard", "Invalid vote submitted.", "error")

    loan = db.query(Loan).filter(Loan.id == loan_id, Loan.status == "Voting").first()
    if not loan:
        return redirect_with_message("/dashboard", "Loan vote is no longer available.", "error")
    if loan.requester_id == current_user.id:
        return redirect_with_message("/dashboard", "You cannot vote on your own loan.", "error")

    existing_vote = db.query(LoanVote).filter(LoanVote.loan_id == loan_id, LoanVote.voter_id == current_user.id).first()
    if existing_vote:
        previous_vote = existing_vote.vote
        existing_vote.vote = vote
        existing_vote.created_at = utc_now()
    else:
        previous_vote = None
        db.add(LoanVote(loan_id=loan_id, voter_id=current_user.id, vote=vote))
    db.flush()
    action = "updated their loan vote to" if previous_vote else "voted"
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=loan.requester_id,
        event_type="loan.vote_recorded",
        entity_type="loan",
        entity_id=loan.id,
        summary=f"{get_display_name(current_user)} {action} {vote} on loan #{loan.id}.",
        details=f"Previous vote: {previous_vote or 'None'}",
    )
    members = db.query(Member).all()
    notification = queue_notification(
        db,
        loan.requester,
        subject=f"Loan #{loan.id} vote update",
        message=(
            f"{get_display_name(current_user)} voted {vote} on your loan request of Rs {loan.amount_requested:,.2f}.\n\n"
            f"{loan_vote_summary(loan, members)}"
        ),
        event_type="loan.vote_recorded",
        entity_type="loan",
        entity_id=loan.id,
    )
    db.commit()
    dispatch_queued(background_tasks, [notification] if notification else [])
    return redirect_with_message("/dashboard", "Your vote has been recorded.", "success")


@app.post("/api/loans/{loan_id}/cancel")
def cancel_loan_request(
    loan_id: int,
    background_tasks: BackgroundTasks,
    current_user: Member = Depends(require_auth),
    db: Session = Depends(get_db),
):
    loan = db.query(Loan).filter(Loan.id == loan_id, Loan.requester_id == current_user.id).first()
    if not loan:
        return redirect_with_message("/loans", "Loan request was not found.", "error")
    if loan.status != "Voting":
        return redirect_with_message("/loans", "Only voting-stage loan requests can be cancelled.", "error")

    loan.status = "Cancelled"
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=current_user.id,
        event_type="loan.cancelled",
        entity_type="loan",
        entity_id=loan.id,
        summary=f"{get_display_name(current_user)} cancelled loan request #{loan.id}.",
    )
    members = db.query(Member).all()
    notifications = queue_notifications(
        db,
        members,
        subject=f"Loan #{loan.id} cancelled",
        message=(
            f"{get_display_name(current_user)} cancelled their loan request of Rs {loan.amount_requested:,.2f}.\n\n"
            f"{loan_vote_summary(loan, members)}"
        ),
        event_type="loan.cancelled",
        entity_type="loan",
        entity_id=loan.id,
    )
    db.commit()
    dispatch_queued(background_tasks, notifications)
    return redirect_with_message("/loans", "Loan request cancelled.", "success")


@app.post("/api/loan-repayments/report")
def report_loan_repayment(
    background_tasks: BackgroundTasks,
    loan_id: int = Form(...),
    amount: Decimal = Form(...),
    transfer_date: str = Form(...),
    current_user: Member = Depends(require_auth),
    db: Session = Depends(get_db),
):
    loan = db.query(Loan).filter(Loan.id == loan_id, Loan.requester_id == current_user.id, Loan.status == "Sanctioned").first()
    if not loan:
        return redirect_with_message("/dashboard", "Selected sanctioned loan was not found.", "error")
    amount = money(amount)
    if amount <= ZERO:
        return redirect_with_message("/dashboard", "Repayment amount must be greater than zero.", "error")

    repayment = LoanRepayment(
        loan_id=loan.id,
        member_id=current_user.id,
        amount=amount,
        transfer_date=datetime.fromisoformat(transfer_date),
        status="Pending",
    )
    db.add(repayment)
    db.flush()
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=current_user.id,
        event_type="loan_repayment.reported",
        entity_type="loan_repayment",
        entity_id=repayment.id,
        summary=f"{get_display_name(current_user)} reported Rs {amount:,.2f} toward loan #{loan.id}.",
    )
    custodians = db.query(Member).filter(Member.is_admin.is_(True)).all()
    notifications = queue_notifications(
        db,
        custodians,
        subject="Loan repayment pending approval",
        message=(
            f"{get_display_name(current_user)} reported a repayment of Rs {amount:,.2f} "
            f"toward loan #{loan.id}. It is waiting for custodian review."
        ),
        event_type="loan_repayment.reported",
        entity_type="loan_repayment",
        entity_id=repayment.id,
    )
    db.commit()
    dispatch_queued(background_tasks, notifications)
    return redirect_with_message("/dashboard", "Loan repayment reported and sent for custodian review.", "success")


@app.post("/api/admin/contributions/{contribution_id}/approve")
def approve_contribution(
    contribution_id: int,
    background_tasks: BackgroundTasks,
    current_user: Member = Depends(require_admin),
    db: Session = Depends(get_db),
):
    contribution = db.query(Contribution).filter(Contribution.id == contribution_id).first()
    if not contribution:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if contribution.status != "Pending":
        return redirect_with_message("/admin", "This contribution has already been reviewed.", "error")
    contribution.status = "Approved"
    contribution.custodian_feedback = None
    contribution.reviewed_by_id = current_user.id
    contribution.reviewed_at = utc_now()
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=contribution.member_id,
        event_type="contribution.approved",
        entity_type="contribution",
        entity_id=contribution.id,
        summary=f"{get_display_name(current_user)} approved Rs {contribution.amount:,.2f} from {get_display_name(contribution.member)}.",
    )
    db.flush()
    notification = queue_notification(
        db,
        contribution.member,
        subject="Contribution approved",
        message=(
            f"Your contribution of Rs {contribution.amount:,.2f} for {contribution.transfer_date:%Y-%m-%d} was approved.\n\n"
            f"{member_balance_summary(db, contribution.member)}"
        ),
        event_type="contribution.approved",
        entity_type="contribution",
        entity_id=contribution.id,
    )
    db.commit()
    dispatch_queued(background_tasks, [notification] if notification else [])
    return redirect_with_message("/admin", "Contribution approved.", "success")


@app.post("/api/admin/contributions/{contribution_id}/revert")
def revert_contribution(
    contribution_id: int,
    background_tasks: BackgroundTasks,
    feedback: str = Form(...),
    current_user: Member = Depends(require_admin),
    db: Session = Depends(get_db),
):
    contribution = db.query(Contribution).filter(Contribution.id == contribution_id).first()
    if not contribution:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if contribution.status != "Pending":
        return redirect_with_message("/admin", "This contribution has already been reviewed.", "error")
    contribution.status = "Reverted"
    contribution.custodian_feedback = feedback.strip()
    contribution.reviewed_by_id = current_user.id
    contribution.reviewed_at = utc_now()
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=contribution.member_id,
        event_type="contribution.reverted",
        entity_type="contribution",
        entity_id=contribution.id,
        summary=f"{get_display_name(current_user)} reverted Rs {contribution.amount:,.2f} from {get_display_name(contribution.member)}.",
        details=contribution.custodian_feedback,
    )
    notification = queue_notification(
        db,
        contribution.member,
        subject="Contribution needs correction",
        message=(
            f"Your reported contribution of Rs {contribution.amount:,.2f} was reverted.\n"
            f"Custodian feedback: {contribution.custodian_feedback}"
        ),
        event_type="contribution.reverted",
        entity_type="contribution",
        entity_id=contribution.id,
    )
    db.commit()
    dispatch_queued(background_tasks, [notification] if notification else [])
    return redirect_with_message("/admin", "Contribution reverted with feedback.", "success")


@app.post("/api/admin/loan-repayments/{repayment_id}/approve")
def approve_loan_repayment(
    repayment_id: int,
    background_tasks: BackgroundTasks,
    current_user: Member = Depends(require_admin),
    db: Session = Depends(get_db),
):
    repayment = db.query(LoanRepayment).filter(LoanRepayment.id == repayment_id).first()
    if not repayment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if repayment.status != "Pending":
        return redirect_with_message("/admin", "This repayment has already been reviewed.", "error")
    repayment.status = "Approved"
    repayment.custodian_feedback = None
    repayment.reviewed_by_id = current_user.id
    repayment.reviewed_at = utc_now()
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=repayment.member_id,
        event_type="loan_repayment.approved",
        entity_type="loan_repayment",
        entity_id=repayment.id,
        summary=f"{get_display_name(current_user)} approved repayment #{repayment.id} of Rs {repayment.amount:,.2f}.",
    )
    notification = queue_notification(
        db,
        repayment.loan.requester,
        subject="Loan repayment approved",
        message=(
            f"Your repayment of Rs {repayment.amount:,.2f} toward loan #{repayment.loan_id} was approved.\n"
            f"Remaining loan balance: Rs {loan_projection(repayment.loan)['remaining_balance']:,.2f}"
        ),
        event_type="loan_repayment.approved",
        entity_type="loan_repayment",
        entity_id=repayment.id,
    )
    db.commit()
    dispatch_queued(background_tasks, [notification] if notification else [])
    return redirect_with_message("/admin", "Loan repayment approved.", "success")


@app.post("/api/admin/loan-repayments/{repayment_id}/revert")
def revert_loan_repayment(
    repayment_id: int,
    background_tasks: BackgroundTasks,
    feedback: str = Form(...),
    current_user: Member = Depends(require_admin),
    db: Session = Depends(get_db),
):
    repayment = db.query(LoanRepayment).filter(LoanRepayment.id == repayment_id).first()
    if not repayment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if repayment.status != "Pending":
        return redirect_with_message("/admin", "This repayment has already been reviewed.", "error")
    repayment.status = "Reverted"
    repayment.custodian_feedback = feedback.strip()
    repayment.reviewed_by_id = current_user.id
    repayment.reviewed_at = utc_now()
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=repayment.member_id,
        event_type="loan_repayment.reverted",
        entity_type="loan_repayment",
        entity_id=repayment.id,
        summary=f"{get_display_name(current_user)} reverted repayment #{repayment.id} of Rs {repayment.amount:,.2f}.",
        details=repayment.custodian_feedback,
    )
    notification = queue_notification(
        db,
        repayment.loan.requester,
        subject="Loan repayment needs correction",
        message=(
            f"Your reported repayment of Rs {repayment.amount:,.2f} toward loan #{repayment.loan_id} was reverted.\n"
            f"Custodian feedback: {repayment.custodian_feedback}"
        ),
        event_type="loan_repayment.reverted",
        entity_type="loan_repayment",
        entity_id=repayment.id,
    )
    db.commit()
    dispatch_queued(background_tasks, [notification] if notification else [])
    return redirect_with_message("/admin", "Loan repayment reverted with feedback.", "success")


@app.post("/api/admin/loans/{loan_id}/sanction")
def sanction_loan(
    loan_id: int,
    background_tasks: BackgroundTasks,
    interest_rate: Decimal = Form(...),
    repayment_months: int = Form(...),
    current_user: Member = Depends(require_admin),
    db: Session = Depends(get_db),
):
    loan = db.query(Loan).filter(Loan.id == loan_id, Loan.status == "Voting").first()
    if not loan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    total_members = db.query(Member).count()
    approvals = db.query(LoanVote).filter(LoanVote.loan_id == loan.id, LoanVote.vote == "Approve").count()
    if approvals < majority_threshold(total_members):
        return redirect_with_message("/admin", "Loan cannot be sanctioned before majority approval.", "error")

    if repayment_months <= 0:
        return redirect_with_message("/admin", "Repayment months must be greater than zero.", "error")
    if interest_rate < ZERO:
        return redirect_with_message("/admin", "Interest rate cannot be negative.", "error")

    loan.status = "Sanctioned"
    loan.interest_rate = money(interest_rate)
    loan.repayment_months = repayment_months
    loan.due_date = add_months(utc_now(), repayment_months)
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=loan.requester_id,
        event_type="loan.sanctioned",
        entity_type="loan",
        entity_id=loan.id,
        summary=f"{get_display_name(current_user)} sanctioned loan #{loan.id} for Rs {loan.amount_requested:,.2f}.",
        details=f"Interest: {loan.interest_rate}% | Repayment months: {repayment_months}",
    )
    members = db.query(Member).all()
    notifications = queue_notifications(
        db,
        members,
        subject=f"Loan #{loan.id} sanctioned",
        message=(
            f"{get_display_name(loan.requester)}'s loan of Rs {loan.amount_requested:,.2f} was sanctioned.\n"
            f"Interest: {loan.interest_rate}%\nRepayment period: {repayment_months} month(s)\n"
            f"Due date: {format_local_date(loan.due_date)}"
        ),
        event_type="loan.sanctioned",
        entity_type="loan",
        entity_id=loan.id,
    )
    db.commit()
    dispatch_queued(background_tasks, notifications)
    return redirect_with_message("/admin", "Loan sanctioned.", "success")


@app.post("/api/admin/loans/{loan_id}/reject")
def reject_loan(
    loan_id: int,
    background_tasks: BackgroundTasks,
    current_user: Member = Depends(require_admin),
    db: Session = Depends(get_db),
):
    loan = db.query(Loan).filter(Loan.id == loan_id).first()
    if not loan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if loan.status != "Voting":
        return redirect_with_message("/admin", "Only voting-stage loans can be rejected.", "error")
    loan.status = "Rejected"
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=loan.requester_id,
        event_type="loan.rejected",
        entity_type="loan",
        entity_id=loan.id,
        summary=f"{get_display_name(current_user)} rejected loan #{loan.id}.",
    )
    members = db.query(Member).all()
    notifications = queue_notifications(
        db,
        members,
        subject=f"Loan #{loan.id} rejected",
        message=f"{get_display_name(loan.requester)}'s loan request of Rs {loan.amount_requested:,.2f} was rejected.",
        event_type="loan.rejected",
        entity_type="loan",
        entity_id=loan.id,
    )
    db.commit()
    dispatch_queued(background_tasks, notifications)
    return redirect_with_message("/admin", "Loan marked as rejected.", "success")


@app.post("/api/admin/notifications/{notification_id}/retry")
def retry_notification(
    notification_id: int,
    background_tasks: BackgroundTasks,
    current_user: Member = Depends(require_admin),
    db: Session = Depends(get_db),
):
    notification = db.query(NotificationLog).filter(NotificationLog.id == notification_id).first()
    if not notification:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if notification.status == "Sent":
        return redirect_with_message("/admin", "That email was already delivered.", "info")

    notification.status = "Queued"
    notification.error_message = None
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=notification.recipient_member_id,
        event_type="notification.retry_queued",
        entity_type="notification",
        entity_id=notification.id,
        summary=f"{get_display_name(current_user)} queued email #{notification.id} for another delivery attempt.",
    )
    db.commit()
    dispatch_queued(background_tasks, [notification])
    return redirect_with_message("/admin", "Email queued for another delivery attempt.", "success")


@app.post("/api/admin/change-custodian")
def change_custodian(
    background_tasks: BackgroundTasks,
    member_id: int = Form(...),
    current_user: Member = Depends(require_admin),
    db: Session = Depends(get_db),
):
    new_custodian = db.query(Member).filter(Member.id == member_id).first()
    if not new_custodian:
        return redirect_with_message("/admin", "Selected member was not found.", "error")

    db.query(Member).update({"is_admin": False}, synchronize_session=False)
    new_custodian.is_admin = True
    record_event(
        db,
        actor_id=current_user.id,
        subject_member_id=new_custodian.id,
        event_type="custodian.changed",
        entity_type="member",
        entity_id=new_custodian.id,
        summary=f"Custodian role transferred from {get_display_name(current_user)} to {get_display_name(new_custodian)}.",
    )
    notifications = queue_notifications(
        db,
        db.query(Member).all(),
        subject="AMSF custodian changed",
        message=f"The AMSF custodian role was transferred to {get_display_name(new_custodian)}.",
        event_type="custodian.changed",
        entity_type="member",
        entity_id=new_custodian.id,
    )
    db.commit()
    dispatch_queued(background_tasks, notifications)

    if current_user.id == new_custodian.id:
        return redirect_with_message("/admin", "Custodian assignment confirmed.", "success")
    return redirect_with_message("/login", f"Custodian role transferred to {get_display_name(new_custodian)}.", "success")


@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse(url="/dashboard")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, current_user: Optional[Member] = Depends(get_current_user_from_cookie)):
    if current_user:
        return RedirectResponse(url="/dashboard")
    return templates.TemplateResponse(request, "login.html", build_template_context(request))


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html", build_template_context(request))


@app.get("/reset-password", response_class=HTMLResponse, name="reset_password_page")
@app.get("/reset-password/", response_class=HTMLResponse)
def reset_password_page(request: Request, token: Optional[str] = None):
    return templates.TemplateResponse(
        request,
        "reset_password.html",
        build_template_context(request, token=token, missing_token=not bool(token)),
    )


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, current_user: Member = Depends(get_current_user_from_cookie)):
    if not current_user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "setup.html", build_template_context(request, user=current_user))


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, current_user: Member = Depends(require_auth), db: Session = Depends(get_db)):
    now = local_now()
    months_active = months_active_since(current_user.join_date, now)
    total_approved = (
        db.query(func.sum(Contribution.amount))
        .filter(Contribution.member_id == current_user.id, Contribution.status == "Approved")
        .scalar()
        or ZERO
    )
    due_amount = (months_active * MONTHLY_BASELINE) - total_approved
    due_status = "Pending Dues" if due_amount > 0 else "Advance Balance"
    display_due = abs(due_amount)
    display_name = get_display_name(current_user)
    current_month_paid = (
        db.query(func.sum(Contribution.amount))
        .filter(
            Contribution.member_id == current_user.id,
            Contribution.status == "Approved",
            func.strftime("%Y", Contribution.transfer_date) == now.strftime("%Y"),
            func.strftime("%m", Contribution.transfer_date) == now.strftime("%m"),
        )
        .scalar()
        or ZERO
    )
    monthly_contribution_due = max(ZERO, MONTHLY_BASELINE - current_month_paid)
    current_loan_exposure = (
        db.query(func.sum(Loan.amount_requested))
        .filter(Loan.requester_id == current_user.id, Loan.status == "Sanctioned")
        .scalar()
        or ZERO
    )
    sanctioned_loans = (
        db.query(Loan)
        .filter(Loan.requester_id == current_user.id, Loan.status == "Sanctioned")
        .all()
    )
    approved_repayments_this_month = (
        db.query(func.sum(LoanRepayment.amount))
        .filter(
            LoanRepayment.member_id == current_user.id,
            LoanRepayment.status == "Approved",
            func.strftime("%Y", LoanRepayment.transfer_date) == now.strftime("%Y"),
            func.strftime("%m", LoanRepayment.transfer_date) == now.strftime("%m"),
        )
        .scalar()
        or ZERO
    )
    projected_monthly_installment = round(
        sum((loan_projection(loan)["monthly_installment"] or ZERO for loan in sanctioned_loans), ZERO),
        2,
    )
    monthly_loan_due = max(ZERO, money(projected_monthly_installment - approved_repayments_this_month))
    loan_cap = total_approved * 2
    available_loan_capacity = max(ZERO, loan_cap - current_loan_exposure)
    loan_capacity_percent = round((current_loan_exposure / loan_cap) * 100, 1) if loan_cap else 0.0
    loan_repayments = (
        db.query(LoanRepayment)
        .filter(LoanRepayment.member_id == current_user.id)
        .order_by(LoanRepayment.transfer_date.desc())
        .all()
    )

    personal_contributions = (
        db.query(Contribution)
        .filter(Contribution.member_id == current_user.id)
        .order_by(Contribution.transfer_date.desc())
        .all()
    )
    personal_events = (
        db.query(AuditEvent)
        .filter(AuditEvent.subject_member_id == current_user.id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(50)
        .all()
    )
    payment_due = now.day > 10 and monthly_contribution_due > 0

    members = db.query(Member).all()
    member_lookup = {member.id: get_display_name(member) for member in members}
    transparent_member_lookup = {member.id: member.alias.strip() if member.alias and member.alias.strip() else member.original_name for member in members}

    votable_loans = (
        db.query(Loan)
        .filter(Loan.status == "Voting", Loan.requester_id != current_user.id)
        .order_by(Loan.id.desc())
        .all()
    )
    voted_ids = {vote.loan_id for vote in db.query(LoanVote).filter(LoanVote.voter_id == current_user.id).all()}
    pending_votes = enrich_loans([loan for loan in votable_loans if loan.id not in voted_ids], members, transparent_member_lookup)

    my_loans = (
        db.query(Loan)
        .filter(Loan.requester_id == current_user.id)
        .order_by(Loan.id.desc())
        .all()
    )
    my_loans_view = enrich_loans(my_loans, members, transparent_member_lookup)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        build_template_context(
            request,
            user=current_user,
            display_name=display_name,
            due_amount=display_due,
            due_status=due_status,
            total_approved=total_approved,
            target_amount=months_active * MONTHLY_BASELINE,
            payment_due=payment_due,
            current_month_paid=round(current_month_paid, 2),
            monthly_contribution_due=round(monthly_contribution_due, 2),
            approved_repayments_this_month=round(approved_repayments_this_month, 2),
            projected_monthly_installment=projected_monthly_installment,
            monthly_loan_due=monthly_loan_due,
            total_monthly_due=round(monthly_contribution_due + monthly_loan_due, 2),
            personal_contributions=personal_contributions,
            personal_events=personal_events,
            sanctioned_loans=sanctioned_loans,
            loan_repayments=loan_repayments,
            loan_cap=loan_cap,
            current_loan_exposure=round(current_loan_exposure, 2),
            available_loan_capacity=round(available_loan_capacity, 2),
            loan_capacity_percent=loan_capacity_percent,
            pending_votes=pending_votes,
            my_loans=my_loans_view,
            today=now.date().isoformat(),
        ),
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, current_user: Member = Depends(require_admin), db: Session = Depends(get_db)):
    pending_contributions = (
        db.query(Contribution)
        .filter(Contribution.status == "Pending")
        .order_by(Contribution.transfer_date.desc())
        .all()
    )
    pending_loan_repayments = (
        db.query(LoanRepayment)
        .filter(LoanRepayment.status == "Pending")
        .order_by(LoanRepayment.transfer_date.desc())
        .all()
    )
    all_members = db.query(Member).all()
    member_lookup = {member.id: member.original_name for member in all_members}
    public_member_lookup = {member.id: get_display_name(member) for member in all_members}
    member_rows = build_member_contribution_rows(all_members, db)
    contribution_history_by_member = {
        member.id: (
            db.query(Contribution)
            .filter(Contribution.member_id == member.id)
            .order_by(Contribution.created_at.desc(), Contribution.id.desc())
            .limit(8)
            .all()
        )
        for member in all_members
    }
    all_loans = db.query(Loan).order_by(Loan.id.desc()).all()
    loan_views = enrich_loans(all_loans, all_members, member_lookup, public_member_lookup)

    total_contributions = db.query(func.sum(Contribution.amount)).filter(Contribution.status == "Approved").scalar() or ZERO
    active_loans_total = db.query(func.sum(Loan.amount_requested)).filter(Loan.status == "Sanctioned").scalar() or ZERO
    total_interest = (
        db.query(func.sum((Loan.amount_requested * Loan.interest_rate) / 100))
        .filter(Loan.status == "Sanctioned", Loan.interest_rate.is_not(None))
        .scalar()
        or ZERO
    )
    recent_events = db.query(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(100).all()
    recent_notifications = (
        db.query(NotificationLog)
        .order_by(NotificationLog.created_at.desc(), NotificationLog.id.desc())
        .limit(50)
        .all()
    )

    return templates.TemplateResponse(
        request,
        "admin.html",
        build_template_context(
            request,
            user=current_user,
            pending_contributions=pending_contributions,
            pending_loan_repayments=pending_loan_repayments,
            member_lookup=member_lookup,
            members=all_members,
            member_rows=member_rows,
            contribution_history_by_member=contribution_history_by_member,
            loan_views=loan_views,
            total_fund=round(total_contributions, 2),
            liquid_cash=round(total_contributions - active_loans_total, 2),
            total_interest=round(total_interest, 2),
            majority_threshold=majority_threshold(len(all_members)),
            recent_events=recent_events,
            recent_notifications=recent_notifications,
            today=local_now().date().isoformat(),
        ),
    )


@app.get("/loans", response_class=HTMLResponse)
def loans_page(request: Request, current_user: Member = Depends(require_auth), db: Session = Depends(get_db)):
    members = db.query(Member).all()
    member_lookup = {member.id: member.alias.strip() if member.alias and member.alias.strip() else member.original_name for member in members}
    loans = db.query(Loan).order_by(Loan.id.desc()).all()
    loan_views = enrich_loans(loans, members, member_lookup)
    return templates.TemplateResponse(
        request,
        "loans.html",
        build_template_context(
            request,
            user=current_user,
            loan_views=loan_views,
            current_user_id=current_user.id,
        ),
    )


@app.get("/investments", response_class=HTMLResponse)
def investments_page(request: Request, current_user: Member = Depends(require_auth)):
    return templates.TemplateResponse(request, "investments.html", build_template_context(request, user=current_user))


@app.get("/api/dashboard-data")
def get_dashboard_data(current_user: Member = Depends(require_auth), db: Session = Depends(get_db)):
    contributions = (
        db.query(Contribution)
        .filter(Contribution.status == "Approved")
        .order_by(Contribution.transfer_date)
        .all()
    )
    pulse_series = {
        "daily": aggregate_contribution_series(contributions, "daily"),
        "weekly": aggregate_contribution_series(contributions, "weekly"),
        "monthly": aggregate_contribution_series(contributions, "monthly"),
    }

    total_approved = (
        db.query(func.sum(Contribution.amount))
        .filter(Contribution.member_id == current_user.id, Contribution.status == "Approved")
        .scalar()
        or ZERO
    )
    total_group = db.query(func.sum(Contribution.amount)).filter(Contribution.status == "Approved").scalar() or ZERO
    loan_ceiling = total_approved * 2
    active_loans = db.query(func.sum(Loan.amount_requested)).filter(Loan.status == "Sanctioned").scalar() or ZERO
    personal_active_loans = (
        db.query(func.sum(Loan.amount_requested))
        .filter(Loan.requester_id == current_user.id, Loan.status == "Sanctioned")
        .scalar()
        or ZERO
    )
    liquid_cash = max(ZERO, total_group - active_loans)
    target_amount = months_active_since(current_user.join_date) * MONTHLY_BASELINE

    return {
        "pulse": {"series": pulse_series, "total": float(money(total_group))},
        "equity": {
            "used": float(money(personal_active_loans)),
            "available": float(max(ZERO, money(loan_ceiling - personal_active_loans))),
            "ceiling": float(money(loan_ceiling)),
        },
        "health": {"liquid": float(money(liquid_cash)), "loans": float(money(active_loans))},
        "baseline": {
            "labels": ["Target", "Actual"],
            "data": [float(money(target_amount)), float(money(total_approved))],
            "achievementPercent": round((total_approved / target_amount) * 100, 1) if target_amount else 0.0,
        },
        "personalSplit": {
            "labels": [get_display_name(current_user), "Rest of Group"],
            "data": [float(money(total_approved)), float(max(ZERO, money(total_group - total_approved)))],
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
