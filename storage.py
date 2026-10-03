"""Persistent course storage.

Layout (inside the project, gitignored):
    data/
        courses.json          # index: id, name, created_at, file list
        automation.json       # daily Auto Quiz / Auto Note schedules
        exams.json            # index of old exam papers (with answers)
        courses/
            1/                # one folder per course
                01_notes.pdf  # the stored PDFs
                01_notes.txt  # extracted text (cached at "Done")
            2/ ...
        exams/
            1/                # one folder per uploaded exam paper

All operations are synchronous and atomic (single process, no awaits inside),
which is safe for the bot's single-event-loop model.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from generator import (
    DEFAULT_QUESTION_COUNT,
    InsufficientTextError,
    Segment,
    extract_pages_from_pdf,
    extract_text_from_pdf,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
_COURSES_DIR = DATA_DIR / "courses"
_INDEX = DATA_DIR / "courses.json"
_AUTOMATION = DATA_DIR / "automation.json"

_MAX_NAME = 60


def set_data_dir(path: str | Path) -> None:
    """Point storage somewhere else (used by tests)."""
    global DATA_DIR, _COURSES_DIR, _INDEX, _AUTOMATION
    DATA_DIR = Path(path)
    _COURSES_DIR = DATA_DIR / "courses"
    _INDEX = DATA_DIR / "courses.json"
    _AUTOMATION = DATA_DIR / "automation.json"


@dataclass
class CourseFile:
    name: str            # original filename as sent by the user
    pdf: str             # path relative to data/courses/
    text: str | None = None  # extracted-text path relative to data/courses/

    def to_dict(self) -> dict:
        return {"name": self.name, "pdf": self.pdf, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> "CourseFile":
        return cls(name=d["name"], pdf=d["pdf"], text=d.get("text"))


@dataclass
class Course:
    id: int
    name: str
    created_at: str = ""
    files: list[CourseFile] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "files": [f.to_dict() for f in self.files],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Course":
        return cls(
            id=int(d["id"]),
            name=d["name"],
            created_at=d.get("created_at", ""),
            files=[CourseFile.from_dict(f) for f in d.get("files", [])],
        )


# --- index I/O ------------------------------------------------------------

def _load() -> dict:
    if not _INDEX.exists():
        return {"next_id": 1, "courses": []}
    try:
        data = json.loads(_INDEX.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"next_id": 1, "courses": []}
    data.setdefault("next_id", 1)
    data.setdefault("courses", [])
    return data


def _save(data: dict) -> None:
    _COURSES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _INDEX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_INDEX)  # atomic on the same filesystem


# --- public API -----------------------------------------------------------

def list_courses() -> list[Course]:
    return [Course.from_dict(c) for c in _load()["courses"]]


def get_course(course_id: int) -> Course | None:
    for c in _load()["courses"]:
        if int(c["id"]) == int(course_id):
            return Course.from_dict(c)
    return None


def create_course(name: str) -> Course:
    name = name.strip()
    if not name:
        raise ValueError("Course name cannot be empty.")
    data = _load()
    course = Course(
        id=int(data["next_id"]),
        name=name[:_MAX_NAME],
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    data["next_id"] = course.id + 1
    data["courses"].append(course.to_dict())
    _save(data)
    (_COURSES_DIR / str(course.id)).mkdir(parents=True, exist_ok=True)
    return course


def delete_course(course_id: int) -> Course | None:
    data = _load()
    before = len(data["courses"])
    removed = next(
        (Course.from_dict(c) for c in data["courses"] if int(c["id"]) == int(course_id)),
        None,
    )
    data["courses"] = [c for c in data["courses"] if int(c["id"]) != int(course_id)]
    if len(data["courses"]) != before:
        _save(data)
        shutil.rmtree(_COURSES_DIR / str(course_id), ignore_errors=True)
    return removed


def _safe_filename(original: str) -> str:
    original = Path(original.replace("\\", "/")).name  # strip any path parts
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", original).strip(" .")
    if not safe:
        safe = "document.pdf"
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    return safe[:100]


def add_pdf(course_id: int, src_path: str | Path, original_name: str) -> CourseFile:
    """Copy a downloaded PDF into the course folder and record it."""
    course = get_course(course_id)
    if course is None:
        raise LookupError(f"No course with id {course_id}")
    folder = _COURSES_DIR / str(course_id)
    folder.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(original_name)
    stored_name = f"{len(course.files) + 1:02d}_{safe}"
    shutil.copyfile(src_path, folder / stored_name)
    entry = CourseFile(name=safe, pdf=f"{course_id}/{stored_name}")

    data = _load()
    for c in data["courses"]:
        if int(c["id"]) == int(course_id):
            c["files"].append(entry.to_dict())
            break
    _save(data)
    return entry


def finalize_course(course_id: int) -> str:
    """Extract text from every PDF that doesn't have it yet.

    Returns the course's combined text.
    Raises InsufficientTextError when no file yields readable text.
    """
    course = get_course(course_id)
    if course is None:
        raise LookupError(f"No course with id {course_id}")

    data = _load()
    record = next((c for c in data["courses"] if int(c["id"]) == int(course_id)), None)
    if record is None:
        raise LookupError(f"No course with id {course_id}")

    changed = False
    errors: list[str] = []
    for file_rec, entry in zip(record["files"], course.files):
        if entry.text:
            continue
        pdf_path = _COURSES_DIR / entry.pdf
        try:
            pages = extract_pages_from_pdf(pdf_path)
        except Exception as exc:  # corrupt/unsupported file
            errors.append(f"{entry.name}: {exc}")
            continue
        text = "\n".join(pages)
        if not text.strip():
            errors.append(f"{entry.name}: no readable text (scanned image?)")
            continue
        text_path = Path(entry.pdf).with_suffix(".txt").as_posix()
        (_COURSES_DIR / text_path).write_text(text, encoding="utf-8")
        # keep per-page texts so questions can reference "p.N"
        pages_path = Path(entry.pdf).with_suffix(".pages.json").as_posix()
        (_COURSES_DIR / pages_path).write_text(
            json.dumps(pages, ensure_ascii=False), encoding="utf-8"
        )
        file_rec["text"] = text_path
        entry.text = text_path
        changed = True
    if changed:
        _save(data)

    combined = course_text(course_id)
    if not combined.strip():
        detail = "; ".join(errors[:3]) if errors else "unknown reason"
        raise InsufficientTextError(
            f"Could not read any text from the PDFs ({detail}). "
            "Scanned/image-only PDFs are not supported."
        )
    return combined


def course_text(course_id: int) -> str:
    """Combined extracted text of a course (finalizes lazily if needed)."""
    course = get_course(course_id)
    if course is None:
        return ""
    parts: list[str] = []
    for entry in course.files:
        if entry.text:
            p = _COURSES_DIR / entry.text
            if p.exists():
                parts.append(p.read_text(encoding="utf-8"))
    if not parts and course.files:
        # texts not extracted yet — do it now
        data = _load()
        record = next(
            (c for c in data["courses"] if int(c["id"]) == int(course_id)), None
        )
        if record:
            for file_rec, entry in zip(record["files"], course.files):
                if file_rec.get("text"):
                    continue
                try:
                    text = extract_text_from_pdf(_COURSES_DIR / entry.pdf)
                except Exception:
                    continue
                if text.strip():
                    text_path = Path(entry.pdf).with_suffix(".txt").as_posix()
                    (_COURSES_DIR / text_path).write_text(text, encoding="utf-8")
                    file_rec["text"] = text_path
                    parts.append(text)
            _save(data)
    return "\n".join(parts)


def _file_segments(course_name: str, entry: CourseFile) -> list[Segment]:
    """Provenance-tagged text chunks for one stored PDF.

    Order of preference:
      1. `.pages.json` (saved by finalize) — gives real page numbers
      2. re-extract from the PDF (legacy courses saved before page tracking)
      3. cached plain `.txt` — works, but page numbers are unknown
    """
    pages_file = _COURSES_DIR / Path(entry.pdf).with_suffix(".pages.json")

    def from_pages(pages: list[str]) -> list[Segment]:
        return [
            Segment(text=p, course=course_name, filename=entry.name, page=i)
            for i, p in enumerate(pages, 1)
            if p and p.strip()
        ]

    if pages_file.exists():
        try:
            return from_pages(json.loads(pages_file.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass

    # legacy course — try to build page data from the original PDF
    pdf_path = _COURSES_DIR / entry.pdf
    if pdf_path.exists():
        try:
            pages = extract_pages_from_pdf(pdf_path)
            if any(p.strip() for p in pages):
                pages_file.write_text(
                    json.dumps(pages, ensure_ascii=False), encoding="utf-8"
                )
                return from_pages(pages)
        except Exception:
            pass

    # last resort: cached text without page info
    if entry.text:
        txt_path = _COURSES_DIR / entry.text
        if txt_path.exists():
            content = txt_path.read_text(encoding="utf-8")
            if content.strip():
                return [
                    Segment(
                        text=content,
                        course=course_name,
                        filename=entry.name,
                        page=None,
                    )
                ]
    return []


def course_segments(course_id: int) -> list[Segment]:
    """All provenance-tagged material of a course (for quiz generation)."""
    course = get_course(course_id)
    if course is None:
        return []
    segments: list[Segment] = []
    for entry in course.files:
        segments.extend(_file_segments(course.name, entry))
    return segments


# --- Automation settings --------------------------------------------------

def _automation_defaults() -> dict:
    return {
        "auto_quiz": {"enabled": False, "time": None, "count": DEFAULT_QUESTION_COUNT,
                      "last_run": None},
        "auto_note": {"enabled": False, "time": None, "last_run": None},
        "note_last_course": None,
    }


def _load_automation() -> dict:
    data = _automation_defaults()
    if not _AUTOMATION.exists():
        return data
    try:
        stored = json.loads(_AUTOMATION.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return data
    for section in ("auto_quiz", "auto_note"):
        if isinstance(stored.get(section), dict):
            data[section].update({k: v for k, v in stored[section].items()
                                  if k in data[section]})
    if "note_last_course" in stored:
        data["note_last_course"] = stored["note_last_course"]
    return data


def _save_automation(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _AUTOMATION.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_AUTOMATION)  # atomic on the same filesystem


def get_automation() -> dict:
    return _load_automation()


def get_auto_quiz() -> dict:
    return _load_automation()["auto_quiz"]


def save_auto_quiz(**fields) -> dict:
    """Update any of enabled / time / count / last_run; returns the settings."""
    data = _load_automation()
    entry = data["auto_quiz"]
    for key, value in fields.items():
        if key not in entry:
            raise KeyError(f"Unknown auto quiz setting: {key}")
        entry[key] = value
    _save_automation(data)
    return entry


def get_auto_note() -> dict:
    return _load_automation()["auto_note"]


def save_auto_note(**fields) -> dict:
    """Update any of enabled / time / last_run; returns the settings."""
    data = _load_automation()
    entry = data["auto_note"]
    for key, value in fields.items():
        if key not in entry:
            raise KeyError(f"Unknown auto note setting: {key}")
        entry[key] = value
    _save_automation(data)
    return entry


def get_note_last_course() -> int | None:
    return _load_automation()["note_last_course"]


def set_note_last_course(course_id: int | None) -> None:
    data = _load_automation()
    data["note_last_course"] = course_id
    _save_automation(data)


# --- Old exams -----------------------------------------------------------
# Past exam papers (with answers) live beside the courses but are kept apart:
# they are not course notes, they are questions that already exist, and the
# quiz uses them differently. Same layout, separate index and folder.

_EXAMS_DIRNAME = "exams"
_EXAMS_INDEXNAME = "exams.json"


def _exams_globals() -> tuple[Path, Path]:
    """Exam folder and index, derived from DATA_DIR so set_data_dir() works."""
    return DATA_DIR / _EXAMS_DIRNAME, DATA_DIR / _EXAMS_INDEXNAME


@dataclass
class ExamFile:
    name: str
    pdf: str
    text: str | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "pdf": self.pdf, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> "ExamFile":
        return cls(name=d["name"], pdf=d["pdf"], text=d.get("text"))


@dataclass
class Exam:
    id: int
    name: str
    created_at: str = ""
    files: list[ExamFile] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "files": [f.to_dict() for f in self.files],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Exam":
        return cls(
            id=int(d["id"]),
            name=d["name"],
            created_at=d.get("created_at", ""),
            files=[ExamFile.from_dict(f) for f in d.get("files", [])],
        )


def _load_exams() -> dict:
    _, index = _exams_globals()
    if not index.exists():
        return {"next_id": 1, "exams": []}
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"next_id": 1, "exams": []}
    data.setdefault("next_id", 1)
    data.setdefault("exams", [])
    return data


def _save_exams(data: dict) -> None:
    folder, index = _exams_globals()
    folder.mkdir(parents=True, exist_ok=True)
    tmp = index.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(index)


def list_exams() -> list[Exam]:
    return [Exam.from_dict(e) for e in _load_exams()["exams"]]


def get_exam(exam_id: int) -> Exam | None:
    for e in _load_exams()["exams"]:
        if int(e["id"]) == int(exam_id):
            return Exam.from_dict(e)
    return None


def create_exam(name: str) -> Exam:
    name = name.strip()
    if not name:
        raise ValueError("Exam name cannot be empty.")
    folder, _ = _exams_globals()
    data = _load_exams()
    exam = Exam(
        id=int(data["next_id"]),
        name=name[:_MAX_NAME],
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    data["next_id"] = exam.id + 1
    data["exams"].append(exam.to_dict())
    _save_exams(data)
    (folder / str(exam.id)).mkdir(parents=True, exist_ok=True)
    return exam


def delete_exam(exam_id: int) -> Exam | None:
    folder, _ = _exams_globals()
    data = _load_exams()
    before = len(data["exams"])
    removed = next(
        (Exam.from_dict(e) for e in data["exams"] if int(e["id"]) == int(exam_id)),
        None,
    )
    data["exams"] = [e for e in data["exams"] if int(e["id"]) != int(exam_id)]
    if len(data["exams"]) != before:
        _save_exams(data)
        shutil.rmtree(folder / str(exam_id), ignore_errors=True)
    return removed


def add_exam_pdf(exam_id: int, src_path: str | Path, original_name: str) -> ExamFile:
    """Copy a downloaded exam PDF into the exam folder and record it."""
    exam = get_exam(exam_id)
    if exam is None:
        raise LookupError(f"No exam with id {exam_id}")
    folder, _ = _exams_globals()
    folder = folder / str(exam_id)
    folder.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(original_name)
    stored_name = f"{len(exam.files) + 1:02d}_{safe}"
    shutil.copyfile(src_path, folder / stored_name)
    entry = ExamFile(name=safe, pdf=f"{exam_id}/{stored_name}")

    data = _load_exams()
    for e in data["exams"]:
        if int(e["id"]) == int(exam_id):
            e["files"].append(entry.to_dict())
            break
    _save_exams(data)
    return entry


def finalize_exam(exam_id: int) -> str:
    """Extract text (per page) from every exam PDF that lacks it.

    Mirrors finalize_course. Raises InsufficientTextError when nothing yields
    readable text — an image-only scan cannot be used as an exam.
    """
    exam = get_exam(exam_id)
    if exam is None:
        raise LookupError(f"No exam with id {exam_id}")
    folder, _ = _exams_globals()

    data = _load_exams()
    record = next((e for e in data["exams"] if int(e["id"]) == int(exam_id)), None)
    if record is None:
        raise LookupError(f"No exam with id {exam_id}")

    changed = False
    errors: list[str] = []
    for file_rec, entry in zip(record["files"], exam.files):
        if entry.text:
            continue
        try:
            pages = extract_pages_from_pdf(folder / entry.pdf)
        except Exception as exc:
            errors.append(f"{entry.name}: {exc}")
            continue
        text = "\n".join(pages)
        if not text.strip():
            errors.append(f"{entry.name}: no readable text (scanned image?)")
            continue
        pages_path = Path(entry.pdf).with_suffix(".pages.json").as_posix()
        (folder / pages_path).write_text(
            json.dumps(pages, ensure_ascii=False), encoding="utf-8"
        )
        file_rec["text"] = pages_path
        entry.text = pages_path
        changed = True
    if changed:
        _save_exams(data)

    segments = exam_segments(exam_id)
    if not segments:
        detail = "; ".join(errors[:3]) if errors else "unknown reason"
        raise InsufficientTextError(
            f"Could not read any text from that PDF ({detail}). "
            "Scanned/image-only PDFs are not supported — the exam has to be "
            "a text PDF."
        )
    return "\n".join(s.text for s in segments)


def _exam_file_segments(exam_name: str, entry: ExamFile) -> list[Segment]:
    folder, _ = _exams_globals()
    pages_file = folder / Path(entry.pdf).with_suffix(".pages.json")
    if pages_file.exists():
        try:
            pages = json.loads(pages_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pages = []
        out = [
            Segment(text=p, course=exam_name, filename=entry.name, page=i)
            for i, p in enumerate(pages, 1)
            if p and p.strip()
        ]
        if out:
            return out

    pdf_path = folder / entry.pdf
    if pdf_path.exists():
        try:
            pages = extract_pages_from_pdf(pdf_path)
            if any(p.strip() for p in pages):
                pages_file.write_text(
                    json.dumps(pages, ensure_ascii=False), encoding="utf-8"
                )
                return [
                    Segment(text=p, course=exam_name, filename=entry.name, page=i)
                    for i, p in enumerate(pages, 1)
                    if p and p.strip()
                ]
        except Exception:
            pass

    if entry.text:
        txt = folder / entry.text
        if txt.exists() and txt.read_text(encoding="utf-8").strip():
            return [
                Segment(
                    text=txt.read_text(encoding="utf-8"),
                    course=exam_name,
                    filename=entry.name,
                    page=None,
                )
            ]
    return []


def exam_segments(exam_id: int) -> list[Segment]:
    """All provenance-tagged material of one exam."""
    exam = get_exam(exam_id)
    if exam is None:
        return []
    segments: list[Segment] = []
    for entry in exam.files:
        segments.extend(_exam_file_segments(exam.name, entry))
    return segments


# --- Forum topics --------------------------------------------------------
# Telegram gives a forum topic a numeric id that has to be passed as
# `message_thread_id` on every send. There is no API to look it up by name, so
# the bot learns it — from .env, from /topic run inside the topic, or from the
# service message Telegram sends when the topic is created — and remembers it.

def _topics_path() -> Path:
    return DATA_DIR / "topics.json"


def load_topics() -> dict:
    path = _topics_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_topic(name: str, thread_id: int) -> None:
    """Remember which topic id belongs to a topic name (case-insensitive)."""
    data = load_topics()
    data[name.strip().lower()] = int(thread_id)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _topics_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_topics_path())


def get_topic(name: str) -> int | None:
    """The thread id for a topic name, or None if it has not been learned."""
    value = load_topics().get(name.strip().lower())
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def forget_topic(name: str) -> None:
    data = load_topics()
    if data.pop(name.strip().lower(), None) is not None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _topics_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_topics_path())
