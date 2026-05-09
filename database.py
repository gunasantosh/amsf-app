import os
from sqlite3 import OperationalError
from datetime import datetime

import pandas as pd
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

import settings  # noqa: F401

DATABASE_URL = os.getenv("AMSF_DATABASE_URL", "sqlite:///./amsf.db")
DEFAULT_PASSWORD = os.getenv("AMSF_DEFAULT_PASSWORD", "amsf123")
MONTHLY_BASELINE = 200.0

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
class Member(Base):
    __tablename__ = "members"

    id = Column(Integer, primary_key=True, index=True)
    original_name = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True, nullable=True)
    hashed_password = Column(String)
    alias = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False)
    join_date = Column(DateTime, default=datetime.utcnow)
    password_changed = Column(Boolean, default=False)

    contributions = relationship("Contribution", back_populates="member")
    loans = relationship("Loan", back_populates="requester")
    reset_tokens = relationship("PasswordResetToken", back_populates="member")

class Contribution(Base):
    __tablename__ = "contributions"

    id = Column(Integer, primary_key=True, index=True)
    member_id = Column(Integer, ForeignKey("members.id"))
    amount = Column(Float)
    transfer_date = Column(DateTime)
    status = Column(String, default="Approved") # Pending, Approved, Reverted
    custodian_feedback = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    member = relationship("Member", back_populates="contributions")

class Loan(Base):
    __tablename__ = "loans"

    id = Column(Integer, primary_key=True, index=True)
    requester_id = Column(Integer, ForeignKey("members.id"))
    amount_requested = Column(Float)
    reason = Column(String)
    interest_rate = Column(Float, nullable=True)
    due_date = Column(DateTime, nullable=True)
    repayment_months = Column(Integer, nullable=True)
    status = Column(String, default="Voting") # Voting, Sanctioned, Rejected
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    requester = relationship("Member", back_populates="loans")
    votes = relationship("LoanVote", back_populates="loan")
    repayments = relationship("LoanRepayment", back_populates="loan")

class LoanVote(Base):
    __tablename__ = "loan_votes"

    id = Column(Integer, primary_key=True, index=True)
    loan_id = Column(Integer, ForeignKey("loans.id"))
    voter_id = Column(Integer, ForeignKey("members.id"))
    vote = Column(String) # Approve, Reject
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    loan = relationship("Loan", back_populates="votes")


class LoanRepayment(Base):
    __tablename__ = "loan_repayments"

    id = Column(Integer, primary_key=True, index=True)
    loan_id = Column(Integer, ForeignKey("loans.id"))
    member_id = Column(Integer, ForeignKey("members.id"))
    amount = Column(Float)
    transfer_date = Column(DateTime)
    status = Column(String, default="Pending")  # Pending, Approved, Reverted
    custodian_feedback = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    loan = relationship("Loan", back_populates="repayments")


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False, index=True)
    token = Column(String, unique=True, index=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    member = relationship("Member", back_populates="reset_tokens")


class ReminderDispatchLog(Base):
    __tablename__ = "reminder_dispatch_logs"

    id = Column(Integer, primary_key=True, index=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False, index=True)
    reminder_type = Column(String, nullable=False)
    reminder_date = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_display_name(member: Member) -> str:
    return member.alias.strip() if member.alias and member.alias.strip() else f"Member {member.id}"


def months_active_since(join_date: datetime, now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    return max(1, (now.year - join_date.year) * 12 + (now.month - join_date.month) + 1)


def resolve_source_workbook() -> str:
    for candidate in ("AMSF.xlsx", "tracking.xlsx"):
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError("Neither AMSF.xlsx nor tracking.xlsx was found in the project directory.")


def parse_row_date(row: pd.Series) -> datetime:
    for column in ("CONTRIBUTION DATE", "DATE"):
        value = row.get(column)
        if pd.notna(value):
            parsed = pd.to_datetime(value, errors="coerce")
            if pd.notna(parsed):
                return parsed.to_pydatetime()
    return datetime.utcnow()


def init_db():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        try:
            loans_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(loans)")).fetchall()}
            if "repayment_months" not in loans_columns:
                connection.execute(text("ALTER TABLE loans ADD COLUMN repayment_months INTEGER"))
            if "created_at" not in loans_columns:
                connection.execute(text("ALTER TABLE loans ADD COLUMN created_at DATETIME"))
                connection.execute(text("UPDATE loans SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))

            contribution_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(contributions)")).fetchall()}
            if "created_at" not in contribution_columns:
                connection.execute(text("ALTER TABLE contributions ADD COLUMN created_at DATETIME"))
                connection.execute(text("UPDATE contributions SET created_at = transfer_date WHERE created_at IS NULL"))

            vote_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(loan_votes)")).fetchall()}
            if "created_at" not in vote_columns:
                connection.execute(text("ALTER TABLE loan_votes ADD COLUMN created_at DATETIME"))
                connection.execute(text("UPDATE loan_votes SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))

            repayment_tables = {
                row[0]
                for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
            }
            if "loan_repayments" not in repayment_tables:
                LoanRepayment.__table__.create(bind=connection)
            if "reminder_dispatch_logs" not in repayment_tables:
                ReminderDispatchLog.__table__.create(bind=connection)
        except OperationalError:
            pass
    db = SessionLocal()
    
    # Check if we need to seed the database
    if db.query(Member).count() == 0:
        print("Seeding database from workbook...")
        try:
            workbook_path = resolve_source_workbook()
            df = pd.read_excel(workbook_path, sheet_name="SUMMARY")
            
            # Extract unique members
            members_data = {}
            for _, row in df.iterrows():
                member_name = str(row["MEMBER NAME"]).strip()
                if member_name == "nan" or not member_name:
                    continue

                date_val = parse_row_date(row)

                if member_name not in members_data:
                    is_admin = member_name.upper() == "B. GUNA"
                    new_member = Member(
                        original_name=member_name,
                        hashed_password="",
                        is_admin=is_admin,
                        join_date=date_val,
                    )
                    db.add(new_member)
                    db.commit()
                    db.refresh(new_member)
                    members_data[member_name] = new_member.id
                
                # Add contribution
                amount = row["AMOUNT CONTRIBUTED"]
                if pd.notna(amount) and amount > 0:
                    contribution = Contribution(
                        member_id=members_data[member_name],
                        amount=float(amount),
                        transfer_date=date_val,
                        status="Approved",
                    )
                    db.add(contribution)

            from auth import get_password_hash

            for member in db.query(Member).all():
                member.hashed_password = get_password_hash(DEFAULT_PASSWORD)
            
            db.commit()
            print("Database seeded successfully.")
        except Exception as e:
            print(f"Error seeding database: {e}")
    db.close()
