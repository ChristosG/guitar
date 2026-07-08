"""`/students` routes: CRUD for the Student roster.

No-auth PoC posture, same as `routers/knowledge.py` — no
authentication/authorization here either; this deploys origin-locked behind
Cloudflare for a single user.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.student import Student
from app.schemas.students import StudentCreate, StudentOut, StudentUpdate

router = APIRouter(tags=["students"])


def _to_student_out(student: Student) -> StudentOut:
    return StudentOut.model_validate(student, from_attributes=True)


def _get_student_or_404(db: Session, student_id: UUID) -> Student:
    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="student not found")
    return student


@router.post("/students", response_model=StudentOut)
def create_student(payload: StudentCreate, db: Session = Depends(get_db)) -> StudentOut:
    student = Student(
        name=payload.name,
        birthdate=payload.birthdate,
        level=payload.level,
        instrument=payload.instrument,
        preferred_language=payload.preferred_language,
    )
    db.add(student)
    db.commit()  # Postgres RETURNING populates id/created_at/updated_at eagerly — no refresh needed
    return _to_student_out(student)


@router.get("/students", response_model=list[StudentOut])
def list_students(db: Session = Depends(get_db)) -> list[StudentOut]:
    students = db.scalars(select(Student).order_by(Student.created_at.desc())).all()
    return [_to_student_out(s) for s in students]


@router.get("/students/{student_id}", response_model=StudentOut)
def get_student(student_id: UUID, db: Session = Depends(get_db)) -> StudentOut:
    return _to_student_out(_get_student_or_404(db, student_id))


@router.patch("/students/{student_id}", response_model=StudentOut)
def update_student(
    student_id: UUID, payload: StudentUpdate, db: Session = Depends(get_db)
) -> StudentOut:
    student = _get_student_or_404(db, student_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(student, field, value)
    db.commit()
    return _to_student_out(student)


@router.delete("/students/{student_id}", status_code=204, response_model=None)
def delete_student(student_id: UUID, db: Session = Depends(get_db)) -> None:
    student = _get_student_or_404(db, student_id)
    db.delete(student)
    db.commit()
