from datetime import datetime
import os

from sqlalchemy import func

import settings  # noqa: F401
from database import Contribution, Loan, LoanRepayment, Member, ReminderDispatchLog, SessionLocal, init_db
from mailer import send_email_sync

MONTHLY_BASELINE = 200.0
PUBLIC_BASE_URL = os.getenv("AMSF_PUBLIC_BASE_URL", "").rstrip("/")


def member_display(member: Member) -> str:
    return member.alias.strip() if member.alias and member.alias.strip() else member.original_name


def build_dashboard_link() -> str:
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/dashboard"
    return "http://127.0.0.1:8000/dashboard"


def loan_monthly_due_for_member(db, member_id: int, now: datetime) -> tuple[float, float]:
    sanctioned_loans = db.query(Loan).filter(Loan.requester_id == member_id, Loan.status == "Sanctioned").all()
    projected_monthly_installment = 0.0
    for loan in sanctioned_loans:
        if loan.interest_rate is None or not loan.repayment_months:
            continue
        total_return = loan.amount_requested + (loan.amount_requested * loan.interest_rate / 100)
        projected_monthly_installment += total_return / loan.repayment_months

    approved_repayments_this_month = (
        db.query(func.sum(LoanRepayment.amount))
        .filter(
            LoanRepayment.member_id == member_id,
            LoanRepayment.status == "Approved",
            func.strftime("%Y", LoanRepayment.transfer_date) == now.strftime("%Y"),
            func.strftime("%m", LoanRepayment.transfer_date) == now.strftime("%m"),
        )
        .scalar()
        or 0.0
    )
    return round(projected_monthly_installment, 2), round(max(0.0, projected_monthly_installment - approved_repayments_this_month), 2)


def contribution_due_for_member(db, member_id: int, now: datetime) -> tuple[float, float]:
    current_month_paid = (
        db.query(func.sum(Contribution.amount))
        .filter(
            Contribution.member_id == member_id,
            Contribution.status == "Approved",
            func.strftime("%Y", Contribution.transfer_date) == now.strftime("%Y"),
            func.strftime("%m", Contribution.transfer_date) == now.strftime("%m"),
        )
        .scalar()
        or 0.0
    )
    return round(current_month_paid, 2), round(max(0.0, MONTHLY_BASELINE - current_month_paid), 2)


def build_reminder_bodies(member_name: str, reminder_type: str, contribution_due: float, loan_due: float, dashboard_link: str) -> tuple[str, str, str]:
    subject = "AMSF Contribution Reminder" if reminder_type == "contribution_reminder" else "AMSF Contribution Due Reminder"
    heading = "Contribution Reminder" if reminder_type == "contribution_reminder" else "Contribution Due Reminder"
    intro = (
        "This is a friendly reminder to complete your AMSF monthly contribution."
        if reminder_type == "contribution_reminder"
        else "Your AMSF minimum monthly contribution is still pending. Please complete it as soon as possible."
    )
    total_due = round(contribution_due + loan_due, 2)
    text_body = (
        f"Hello {member_name},\n\n"
        f"{intro}\n\n"
        f"Minimum contribution due this month: Rs {contribution_due:,.2f}\n"
        f"Loan repayment due this month: Rs {loan_due:,.2f}\n"
        f"Total current obligation: Rs {total_due:,.2f}\n\n"
        f"Open your AMSF dashboard here:\n{dashboard_link}\n\n"
        "If you have already paid, please report the payment in the portal so the custodian can verify it."
    )
    html_body = f"""
    <html>
      <body style="font-family:Arial,sans-serif;background:#0d1324;color:#e2e8f0;padding:24px;">
        <div style="max-width:620px;margin:0 auto;background:#11192e;border:1px solid #24314f;border-radius:18px;padding:28px;">
          <div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#94a3b8;margin-bottom:8px;">AMSF Monthly Notice</div>
          <h2 style="margin:0 0 12px;color:#5eead4;">{heading}</h2>
          <p style="line-height:1.7;margin:0 0 18px;">Hello {member_name},</p>
          <p style="line-height:1.7;margin:0 0 18px;">{intro}</p>
          <div style="background:#0b152a;border:1px solid #24314f;border-radius:14px;padding:18px;margin-bottom:18px;">
            <div style="margin-bottom:8px;">Minimum contribution due this month: <strong>Rs {contribution_due:,.2f}</strong></div>
            <div style="margin-bottom:8px;">Loan repayment due this month: <strong>Rs {loan_due:,.2f}</strong></div>
            <div>Total current obligation: <strong>Rs {total_due:,.2f}</strong></div>
          </div>
          <a href="{dashboard_link}" style="display:inline-block;background:#5eead4;color:#081120;text-decoration:none;padding:12px 18px;border-radius:10px;font-weight:700;">Open AMSF Dashboard</a>
          <p style="line-height:1.7;color:#94a3b8;margin-top:18px;">If you have already paid, please report the payment in the portal so the custodian can verify it.</p>
        </div>
      </body>
    </html>
    """
    return subject, text_body, html_body


def main() -> None:
    init_db()
    now = datetime.utcnow()
    day = now.day
    if day < 5 or day > 15:
        print(f"No reminders due today ({now.date()}).")
        return

    reminder_type = "contribution_reminder" if day <= 10 else "due_reminder"
    dashboard_link = build_dashboard_link()
    db = SessionLocal()
    sent_count = 0
    try:
        members = db.query(Member).filter(Member.email.is_not(None), Member.password_changed.is_(True)).all()
        today_key = now.strftime("%Y-%m-%d")
        for member in members:
            current_month_paid, contribution_due = contribution_due_for_member(db, member.id, now)
            if contribution_due <= 0:
                continue

            existing_log = (
                db.query(ReminderDispatchLog)
                .filter(
                    ReminderDispatchLog.member_id == member.id,
                    ReminderDispatchLog.reminder_type == reminder_type,
                    ReminderDispatchLog.reminder_date == today_key,
                )
                .first()
            )
            if existing_log:
                continue

            _, loan_due = loan_monthly_due_for_member(db, member.id, now)
            subject, text_body, html_body = build_reminder_bodies(
                member_display(member),
                reminder_type,
                contribution_due,
                loan_due,
                dashboard_link,
            )
            send_email_sync(member.email, subject, text_body, html_body)
            db.add(ReminderDispatchLog(member_id=member.id, reminder_type=reminder_type, reminder_date=today_key))
            db.commit()
            sent_count += 1
            print(
                f"Reminder sent to {member.email} | paid this month: Rs {current_month_paid:,.2f} | "
                f"contribution due: Rs {contribution_due:,.2f} | loan due: Rs {loan_due:,.2f}"
            )
    finally:
        db.close()

    print(f"Reminder run completed. Sent {sent_count} email(s).")


if __name__ == "__main__":
    main()
