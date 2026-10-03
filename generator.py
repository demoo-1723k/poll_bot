"""Turn PDF study material into quiz questions and notes (rule-based, no AI API).

Pipeline
--------
1. Extract text per page with pdfplumber (page boundaries are preserved so a
   question can point back to "course · file · p.N").
2. Clean each page: split into sentences and throw away everything that is not
   study material — bullet glyphs, heading/table-of-contents dumps, page
   furniture, figure captions, and vacuous "definitions" such as
   *"Color change is one rectangle at a time."*
3. Decide which words are worth asking about. A good study term is **recurrent
   and central**: it shows up several times and on several pages. A word used
   once on one page ("rectangle" in a worked example) scores low and is almost
   never used as a blank or a distractor.
4. Build four kinds of question, always anchored on that material:
   - ``def2term``  *Which term is described as …?*   (from "X is Y")
   - ``term2def``  *______ is Y*                      (blank the term)
   - ``number``    *______* with a numeric distractor set
   - ``tf``        true/false, mutated from a real fact (a number or a key term)
   Distractors are always **other central terms of the same shape** — never a
   random word pulled from somewhere else in the document.
5. Every question's explanation quotes the source sentence and cites
   course · file · page, so a student can re-read the exact part.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

import pdfplumber

# --- Telegram poll limits -------------------------------------------------
MAX_QUESTION_LEN = 300   # poll question: 1-300 chars
MAX_OPTION_LEN = 100     # poll option: 1-100 chars
MAX_EXPLANATION_LEN = 190  # poll explanation: keep safely under the limit

DEFAULT_QUESTION_COUNT = 15  # fallback when no quantity was requested
MAX_QUESTIONS = 50           # hard cap (typed quantities and presets)

_BLANK = "______"

MIN_WORDS = 5
MAX_WORDS = 45

STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because been
    before being below between both but by can could did do does doing down during
    each few for from further had has have having he her here hers herself him
    himself his how i if in into is it its itself just me more most my myself no
    nor not now of off on once only or other our ours ourselves out over own same
    she should so some such than that the their theirs them themselves then there
    these they this those through to too under until up very was we were what
    when where which while who whom why will with would you your yours yourself
    figure table page chapter example however thus also may many one two three
    mean means mean's refer refers referred reference define defined defines
    definition denote denotes consist consists comprised comprise called known
    based used uses using use make makes made making take takes taken give gives
    given set sets let lets allow allows help helps need needs want wants consider
    considered include includes provide provides represent represents form forms
    found find work works seem seems appear appears become becomes remain remains
    come comes go goes put puts keep keeps show shows shown see seen look looks
    become becomes become every all another both either neither several various
    certain entire whole able enough most least less almost rather quite already
    yet still even well back together hence therefore instead otherwise although
    though unless whether per via upon toward towards onto throughout despite
    beyond among along around across behind beneath beside besides except inside
    outside since than then within without always often sometimes usually never
    produce produces producing convert converts converted convert list lists
    identify identifies identifying identified underline underlined underlining
    first second third next last latest earlier latter following above below
    underline underlines labelled labeled named call calls called
    must musts might shall ought needs needed needs cannot cant
    listed contain contains containing store stores stored move moves moving
    carry carries build builds built break breaks broken release releases
    released absorb absorbs absorbed transport transports pump pumps pumped
    supply supplies generate generates generated occur occurs occurring
    begin begins began require requires required allow allows allowed
    examples figures tables sections chapters slides summary introduction
    overview conclusion problem problems example example's
    """.split()
)

_WORDS = re.compile(r"[A-Za-z][A-Za-z'’-]*")
# Retrieval also needs digits: "2NF", "3NF" and "C6H6" are course terms, and a
# letters-only pattern chops them down to "NF".
_QWORDS = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")

# --- junk filters ---------------------------------------------------------

_BULLET_RE = re.compile(r"[•▪●■◆·‣⁃−‒–—]\s")
_CAPS_RATIO = 0.6           # share of capitalised words that marks a heading dump
_CAPS_MIN_WORDS = 4
_META_RE = re.compile(
    r"\b(illustrative|generated (for|by)|for teaching use|"
    r"this (chapter|slide|page|section|figure|table|unit)|"
    r"as shown in|see (the )?(figure|table)|"
    r"source\s*[:=]|chapter \d+|slide \d+|page \d+|"
    r"assignment|homework|individual assignment|group assignment|"
    r"due (date|on)|marks?\b|grade[sd]?\b|"
    r"do all the questions|answer (the )?following|"
    r"exam|quiz|worksheet)\b",
    re.IGNORECASE,
)
_PAGE_FURNITURE_RE = re.compile(
    r"^(page|chapter|unit|slide|figure|fig\.?|table)\s*\d+", re.IGNORECASE
)
_SECTION_NUMBER_RE = re.compile(r"^\d+(?:\.\d+){1,}\s+")   # "7.1.1 Robotic Sensing"
_CID_RE = re.compile(r"\bcid:\d+", re.IGNORECASE)       # broken PDF font mapping
_MATH_GLYPHS = "∗∑√≤≥≈±÷×"  # equations extract as noise, never as material
_CAPS_RUN_RE = re.compile(r"[A-Z][A-Za-z&'’-]*(?:\s+[A-Z][A-Za-z&'’-]*)+")
_CITATION_RE = re.compile(
    r"\b(press|university|edition|ed\.|pp\.|vol\.|isbn|doi|arxiv|"
    r"proceedings|journal)\b",
    re.IGNORECASE,
)
# the whole "(cid:12)" block, parentheses included
_CID_BLOCK_RE = re.compile(r"\(\s*cid:\s*\d+\s*\)", re.IGNORECASE)
_COMPOUND_RE = re.compile(r"^[A-Z][a-z]+[A-Z]")          # "ProblemSolvingAgents"
# A running head glued by the PDF extractor: "SearchStrategies", not a word.
_GLUED_RE = re.compile(r"[a-z][A-Z]")
_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
# "The goal is to get exactly one liter of water" states a purpose, it does not
# define the goal — the same vacuity as "Color change is one rectangle".
_PURPOSE_BODY_RE = re.compile(r"^(to|how|why|whether|that|for)\b", re.IGNORECASE)
# "This schema is in 1NF" names no concept — the subject is a placeholder.
_DEMONSTRATIVE_RE = re.compile(
    r"^(this|that|these|those|it|there|here|one|such|above|below|example)\b",
    re.IGNORECASE,
)

# Wingdings/Symbol bullets extract as private-use codepoints. Left in place
# they glue a whole bullet list into one unreadable "sentence".
_PUA_RE = re.compile(r"[\ue000-\uf8ff\uf0b7\uf076\uf0a7]")
# A sentence cut off by the page/column break ends on a dangling word.
_DANGLING_TAIL_RE = re.compile(
    r"(?::|\b(?:and|or|of|the|a|an|to|in|on|for|with|that|which|is|are|was|were|"
    r"if|then|as|by|from|but|such|than|into|its|it|this|these|those|be)\s*)\.?$",
    re.IGNORECASE,
)
# Placeholders used in "the ..... is where column definitions go".
_PLACEHOLDER_RE = re.compile(r"\.{2,}|\(\s*\)|_{3,}|-{4,}|\.{3,}\s*\.")

# --- structure ------------------------------------------------------------

# "X is defined as / is / refers to / consists of Y" at the start of a sentence
_DEFINITION_RE = re.compile(
    r"^(?P<term>[A-Za-z][\w'’-]*(?:\s+[A-Za-z][\w'’-]*){0,3}?)\s+"
    r"(?:is\s+defined\s+as|are\s+defined\s+as|is\s+known\s+as|refers\s+to|"
    r"is|are|was|were|means|denotes|consists\s+of|comprises)\s+"
    r"(?P<body>.+)$",
    re.IGNORECASE,
)

_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)*(?:\s?%|percent)?", re.IGNORECASE)
_NUMBER_LIKE = re.compile(r"\d+(?:[.,]\d+)*%?")
_UNIT_RE = re.compile(
    r"(%|percent|°c|°f|kg|km|cm|mm|ml|ms|mhz|ghz|nm|mg|gb|mb|kb|v|a|w|kw)\b",
    re.IGNORECASE,
)

# A definition has to actually say something: three content words (or a number)
# after the copula. This is what rejects "Color change is one rectangle at a time."
_MIN_DEFINITION_CONTENT = 3

# Term centrality: how recurring a term is, and how widely it is spread.
_MIN_TERM_FREQ = 2
_MIN_TERM_PAGES = 2
_DISTRACTOR_POOL = 60


class InsufficientTextError(ValueError):
    """Raised when the PDF has too little readable text to build a quiz."""


@dataclass
class Segment:
    """A chunk of study text plus where it came from.

    Used both for whole pages (extraction) and single sentences (generation).
    `page` is 1-based within its file; None when the origin is unknown.
    """
    text: str
    course: str | None = None
    filename: str | None = None
    page: int | None = None


@dataclass
class Question:
    text: str                       # the poll question (<= 300 chars)
    options: list[str]              # 2-10 answer options (<= 100 chars each)
    correct_index: int               # index of the right option
    kind: str                        # "mcq" or "tf"
    explanation: str | None = None   # shown after answering (<= 190 chars)
    course: str | None = None        # provenance for the material reference
    filename: str | None = None
    page: int | None = None
    subtype: str = ""                # "def2term" / "term2def" / "number" / "mutated"


@dataclass
class Note:
    """One study point plus where it came from (course · file · page)."""

    text: str
    course: str | None = None
    filename: str | None = None
    page: int | None = None
    term: str | None = None          # the key term the note is about
    detail: str | None = None         # a second sentence about the same term

    def render(self, title: str = "Daily Note") -> str:
        """A Telegram-ready HTML message: title, the note, then its source.

        Labels are spelled out (course / file / page) so students can find the
        exact place in their material without guessing what a line means.
        """
        lines = [f"📖 <b>{escape(title)}</b>", ""]
        if self.term and self.text.lower().startswith(self.term.lower()):
            # the sentence already says the term — bold it there instead of
            # printing "Entities — Entities are …" twice
            head, tail = self.text[: len(self.term)], self.text[len(self.term):]
            lines.append(f"<b>{escape(head)}</b>{escape(tail)}")
        elif self.term:
            lines.append(f"<b>{escape(self.term)}</b> — {escape(self.text)}")
        else:
            lines.append(escape(self.text))
        if self.detail:
            lines += ["", f"💡 {escape(self.detail)}"]
        meta: list[str] = []
        if self.course:
            meta.append(f"📚 Course: <b>{escape(self.course)}</b>")
        if self.filename:
            meta.append(f"📄 File: {escape(self.filename)}")
        if self.page is not None:
            meta.append(f"📃 Page: <b>{self.page}</b>")
        if meta:
            lines += ["", "━━━━━━━━━━━━", *meta]
        return "\n".join(lines)


# --- PDF extraction -------------------------------------------------------

def extract_pages_from_pdf(path: str | Path) -> list[str]:
    """Return the text of every page, in order (empty string for blank pages).

    Page indices in this list are 1-based references elsewhere — empty pages
    are kept so page numbers always match the physical PDF.
    """
    with pdfplumber.open(path) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def extract_text_from_pdf(path: str | Path) -> str:
    """Return the concatenated text of every page of the PDF."""
    return "\n".join(extract_pages_from_pdf(path))


# --- Sentence helpers -----------------------------------------------------

def _split_sentences(text: str) -> list[str]:
    """Split a block of text into sentences.

    A Wingdings bullet ends the item before it, so
    ``"Ternary (degree 3) \uf0a7 An association …"`` becomes two sentences
    instead of one unreadable dump.
    """
    text = _PUA_RE.sub(" \u2022 ", text)
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])|\u2022", text)
    out = []
    for part in parts:
        part = part.strip().strip("\u2022").strip()
        if part:
            out.append(part)
    return out


def _word_list(sentence: str) -> list[str]:
    return _WORDS.findall(sentence)


def _content_words(sentence: str) -> list[str]:
    """Words that carry meaning — the raw material for terms and checks."""
    out = []
    for w in _word_list(sentence):
        lw = w.lower()
        if len(lw) < 3 or lw in STOPWORDS or lw.isdigit():
            continue
        out.append(w)
    return out


def _is_usable(sentence: str) -> bool:
    words = _word_list(sentence)
    if not (MIN_WORDS <= len(words) <= MAX_WORDS):
        return False
    letters = sum(c.isalpha() for c in sentence)
    # tables/formulas junk has few letters relative to their length
    return letters >= 0.5 * len(sentence)


def _is_junk(sentence: str) -> bool:
    """True when a line is page furniture or layout debris, not material."""
    if _BULLET_RE.search(sentence):
        return True
    if _CID_RE.search(sentence):                      # "cid:15" = mangled glyph
        return True
    if sum(sentence.count(ch) for ch in _MATH_GLYPHS) >= 2:
        return True
    words = _word_list(sentence)
    if len(words) >= _CAPS_MIN_WORDS:
        caps = sum(1 for w in words if w[:1].isupper())
        if caps / len(words) >= _CAPS_RATIO:
            return True  # heading row, table of contents
    if _META_RE.search(sentence):
        return True
    if _PAGE_FURNITURE_RE.match(sentence):
        return True
    # bibliography lines look like questions ("Fox, Probabilistic Robotics,
    # MIT Press, 2003.") but teach nothing
    if sentence.count(",") >= 2 and _CITATION_RE.search(sentence):
        return True
    if _PLACEHOLDER_RE.search(sentence):
        return True   # "The ..... is where column definitions go."
    if _DANGLING_TAIL_RE.search(sentence):
        return True   # cut off by a column/page break: "Definition: X is Y If"
    return False


def _strip_heading_prefix(sentence: str) -> str:
    """Remove a running head that pdfplumber glues to the first real sentence.

    "ProblemSolvingAgents SearchAlgorithm SearchStrategies Exercises
    Missionary-and-Cannibal Problem: …" is a page header followed by content —
    only the content is material.
    """
    match = _CAPS_RUN_RE.match(sentence)
    if not match:
        return sentence
    run = match.group(0)
    if len(run.split()) < 3:
        return sentence
    words = run.split()
    compound = any(_COMPOUND_RE.match(w) for w in words)
    lowered = {w.lower() for w in words}
    repeats = sentence[match.end() :].split()[:1]
    repeated = bool(repeats) and repeats[0].lower() in lowered
    if not (compound or repeated):
        return sentence
    rest = sentence[match.end() :].strip()
    return rest if rest else sentence


def _is_fragment(text: str) -> bool:
    """A leftover piece of a sentence: starts mid-word or mid-punctuation."""
    return bool(text) and not text[0].isalpha()


def _drop_repeated_lead(sentence: str) -> str:
    """"Manipulation Manipulation is the ability of …" — drop the echoed heading."""
    words = sentence.split(" ", 2)
    if len(words) >= 2 and words[0].lower() == words[1].lower().rstrip(".,:"):
        return words[2] if len(words) > 2 else ""
    return sentence


def _mutates_cleanly(sentence: str, original: str, replacement: str) -> bool:
    """Reject swaps that produce broken English ("A obstacles is …")."""
    position = sentence.find(original)
    if position <= 0:
        return False
    before = sentence[:position].rstrip().split(" ")
    previous = before[-1].lower() if before else ""
    if previous in ("a", "an"):
        wants_vowel = replacement[:1].lower() in "aeiou"
        has_vowel = previous == "an"
        if wants_vowel != has_vowel:
            return False
    return True


def _has_content(term: str) -> bool:
    """True when the term contains at least one meaningful word."""
    words = [w.lower() for w in _word_list(term)]
    return bool(words) and any(w not in STOPWORDS and len(w) >= 3 for w in words)


def _split_segment(segment: Segment) -> list[Segment]:
    """Split one segment into tagged sentences.

    When the segment is a real PDF page, fragments cut off by the page break
    are dropped: a sentence that doesn't end with punctuation at the end of a
    page, and a continuation that starts lowercase at the top of a page.
    """
    raw = _split_sentences(segment.text)
    out: list[Segment] = []
    for i, sentence in enumerate(raw):
        if segment.page is not None:
            starts_lower = sentence[:1].islower()
            ends_open = not sentence.rstrip().endswith((".", "!", "?", "”", '"'))
            is_last = i == len(raw) - 1
            if starts_lower:
                continue
            if is_last and ends_open:
                continue
        out.append(
            Segment(
                text=sentence,
                course=segment.course,
                filename=segment.filename,
                page=segment.page,
            )
        )
    return out


def _collect_sentences(segments: list[Segment]) -> list[Segment]:
    """Usable, non-junk, deduplicated, provenance-tagged sentences."""
    collected: list[Segment] = []
    seen: set[str] = set()
    for segment in segments:
        for sent in _split_segment(segment):
            sent.text = _SECTION_NUMBER_RE.sub("", sent.text)
            sent.text = _strip_heading_prefix(sent.text)
            sent.text = _drop_repeated_lead(sent.text)
            if _is_fragment(sent.text) or sent.text[:1].islower():
                continue  # heading removal left only the tail of a sentence
            if not _is_usable(sent.text) or _is_junk(sent.text):
                continue
            key = re.sub(r"\W+", "", sent.text.lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            collected.append(sent)
    return collected


# --- Term centrality ------------------------------------------------------

@dataclass
class TermStats:
    """Which words are worth asking about, and how central they are.

    Value is deliberately *not* plain tf-idf: a rare local word scores low in
    tf-idf but a high value in study terms. A concept that recurs **and** is
    spread over several pages is what a course wants you to know.
    """
    freq: Counter = field(default_factory=Counter)
    pages: dict = field(default_factory=lambda: defaultdict(set))
    display: dict = field(default_factory=dict)
    value: dict = field(default_factory=dict)
    sentence_count: int = 0
    total_pages: int = 1

    @classmethod
    def build(cls, sentences: list[Segment]) -> "TermStats":
        stats = cls(sentence_count=len(sentences))
        for sent in sentences:
            # Spread is measured over pages, but plain text has no page
            # numbers — fall back to distinct sentences so a term used in two
            # sentences still counts as central.
            where = sent.page if sent.page is not None else sent.text
            words = _content_words(sent.text)
            keys = [w.lower() for w in words]
            candidates = list(zip(keys, words))
            for i, (key, surface) in enumerate(candidates):
                stats._add(key, surface, where)
                if i + 1 < len(candidates):  # two-word terms, e.g. "state space"
                    stats._add(f"{key} {candidates[i + 1][0]}", None, where)
        total_pages = max(1, len({s.page for s in sentences if s.page is not None}))
        stats.total_pages = total_pages
        for key, count in stats.freq.items():
            spread = len(stats.pages[key])
            # spread matters more than rarity: central concepts win
            stats.value[key] = count * (1.0 + math.log1p(spread))
        return stats

    def _add(self, key: str, surface: str | None, where: object) -> None:
        self.freq[key] += 1
        if where is not None:
            self.pages[key].add(where)
        if surface is not None:
            self.display.setdefault(key, surface)

    def _min_pages(self) -> int:
        """Terms used on several pages are central; on a short text, freq is enough."""
        return _MIN_TERM_PAGES if self.total_pages >= 4 else 1

    def is_key(self, term: str) -> bool:
        """True when a term is recurrent and central enough to be quizzed."""
        return self._is_key_text(term)

    def _is_key_text(self, term: str) -> bool:
        return any(self._is_key(k) for k in self._keys_of(term))

    def _is_key(self, key: str) -> bool:
        return (
            self.freq.get(key, 0) >= _MIN_TERM_FREQ
            and len(self.pages.get(key, ())) >= self._min_pages()
        )

    def _is_usable_term(self, key: str) -> bool:
        """A term a student could be asked to recognise.

        "functionally" is an adverb made from "function", which is a real term
        in this course; "assembly" is a noun that merely ends in -ly. Telling
        them apart needs the material itself, so the stem has to be a term too.
        """
        if len(key.split()) == 1 and key.endswith("ly") and len(key) > 5:
            stem = key[:-2]
            if stem in self.freq or (stem + "e") in self.freq:
                return False
        return self._is_key(key)

    def _keys_of(self, term: str) -> list[str]:
        """The unigrams and bigrams that make up a surface term."""
        words = [w.lower() for w in _content_words(term)]
        keys = list(words)
        for i in range(len(words) - 1):
            keys.append(f"{words[i]} {words[i + 1]}")
        return keys

    def candidates(self, limit: int = _DISTRACTOR_POOL) -> list[str]:
        """Central terms, most central first — the pool for blanks/distractors."""
        scored = [
            (self.value.get(k, 0.0), self.display.get(k, k), k)
            for k in self.freq
            if len(k.split()) <= 2
            and self._is_usable_term(k)
            and not _GLUED_RE.search(self.display.get(k, k))
        ]
        scored.sort(reverse=True)
        return [display for _, display, _ in scored[:limit]]

    def value_of_text(self, text: str) -> float:
        """Centrality of the most important term inside a piece of text."""
        return max(
            (self.value.get(k, 0.0) for k in self._keys_of(text)), default=0.0
        )

    def key_terms_in(self, text: str) -> list[str]:
        """Central terms that appear in this text, most central first."""
        found = [
            (self.value.get(k, 0.0), self.display.get(k, k), k)
            for k in self._keys_of(text)
            if self._is_usable_term(k)
            and len(k.split()) <= 2
            and not _GLUED_RE.search(self.display.get(k, k))
        ]
        found.sort(reverse=True)
        out: list[str] = []
        for _, display, key in found:
            if display.lower() not in {o.lower() for o in out}:
                out.append(display)
        return out


def _strip_column_bleed(sentence: str, stats: "TermStats") -> str:
    """Cut a sidebar/column fragment that pdfplumber glued into a sentence.

    "A robot is a programmable machine that can sense Everyday examples its
    environment" — "Everyday examples" is a margin note, not material. A
    mid-sentence capitalised word that is *not* one of the course's own terms
    is the giveaway; real proper nouns (Turing, Water Jug Problem) survive
    because they recur in the material.
    """
    words = list(_WORDS.finditer(sentence))
    for match in words:
        start = match.start()
        if start == 0 or not sentence[start - 1].isspace():
            continue  # sentence-initial word, or part of a hyphenated compound
        word = match.group()
        if not word[0].isupper() or len(word) < 3:
            continue
        if stats.is_key(word) or word.lower() in STOPWORDS:
            continue
        head = sentence[: match.start()].rstrip()
        # keep the head when it is still a complete, useful sentence
        if len(_content_words(head)) >= 4:
            return head
    return sentence


# --- Definitions ----------------------------------------------------------

@dataclass
class Definition:
    term: str
    body: str
    start: int
    end: int


def _find_definition(sentence: str) -> Definition | None:
    """'Mitosis is the division of a cell' -> Definition('Mitosis', ...).

    Returns None when the sentence is not a *useful* definition: the term has
    no content, or the part after the copula is too thin to be worth learning
    ("Color change is one rectangle at a time" is rejected here).
    """
    match = _DEFINITION_RE.match(sentence)
    if not match:
        return None
    term = match.group("term").strip()
    term_words = [w.lower() for w in term.split()]
    if len(term_words) > 1 and len(set(term_words)) != len(term_words):
        return None  # "Manipulation Manipulation is …" — a heading, not a term
    body = match.group("body").strip()
    if not _has_content(term) or len(body) < 12:
        return None
    if _PURPOSE_BODY_RE.match(body):
        return None
    if _DEMONSTRATIVE_RE.match(term):
        return None   # "This schema is in 1NF" names no concept
    if all(w.lower() in STOPWORDS for w in term.split()):
        return None
    informative = bool(_NUMBER_RE.search(body)) or (
        len(_content_words(body)) >= _MIN_DEFINITION_CONTENT
    )
    if not informative:
        return None
    return Definition(term=term, body=body, start=match.start("term"), end=match.end("term"))


# --- Distractors ----------------------------------------------------------

def _shape_matches(candidate: str, answer: str) -> bool:
    """A distractor has to look like the answer: similar size and length.

    Capitalisation is deliberately *not* compared here — the option list is
    normalised to the answer's case in `_options_from`, which keeps the pool
    open and makes every option read the same way.
    """
    if abs(len(candidate.split()) - len(answer.split())) > 1:
        return False
    ratio = len(candidate) / max(1, len(answer))
    if not 0.55 <= ratio <= 1.8:
        return False
    low_c, low_a = candidate.lower(), answer.lower()
    if low_c in low_a or low_a in low_c:
        return False
    if not candidate.replace(" ", "").replace("'", "").isalnum():
        return False
    return len(candidate) <= MAX_OPTION_LEN


def _stem(word: str) -> str:
    """Rough singular form, so 'Algorithm', 'Algorithms' and 'entity type' /
    'entity types' each count as one term rather than two options."""
    low = word.lower().strip()
    if low.endswith("ies") and len(low) > 4:
        return low[:-3] + "y"          # entities -> entity
    if low.endswith(("sses", "shes", "ches", "xes", "zes")):
        return low[:-2]                # boxes -> box
    if low.endswith("s") and not low.endswith("ss"):
        return low[:-1]                # types -> type, algorithms -> algorithm
    return low


def _distractors(
    answer: str,
    stats: TermStats,
    rng: random.Random,
    page: int | None = None,
    k: int = 3,
    source: str | None = None,
) -> list[str]:
    """Other central terms of the same shape — the topic, not random words.

    `source` excludes anything already in the question sentence, so a
    distractor can never repeat a word the student can read in the stem.
    """
    answer = answer.strip()
    if len(answer) > MAX_OPTION_LEN:
        return []
    in_source = (source or "").lower()
    pool = [
        c for c in stats.candidates()
        if _shape_matches(c, answer) and c.lower() not in in_source
    ]
    same_page = [c for c in pool if page is not None and page in stats.pages.get(c.lower(), ())]
    elsewhere = [c for c in pool if c not in same_page]
    rng.shuffle(same_page)
    rng.shuffle(elsewhere)

    out: list[str] = []
    seen = {answer.lower()}
    stems = {_stem(answer)}
    for cand in same_page + elsewhere:
        low = cand.lower()
        if low in seen:
            continue
        stem = _stem(cand)
        if stem in stems:
            continue  # "Algorithm" and "Algorithms" are not two distractors
        seen.add(low)
        stems.add(stem)
        out.append(cand)
        if len(out) == k:
            break
    return out


def _number_distractors(token: str, rng: random.Random) -> list[str]:
    suffix = "%" if token.endswith("%") else ""
    raw = token[: -1] if suffix else token
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return []
    is_int = float(value).is_integer()
    base = int(value)
    offsets = list(range(-5, 6))
    rng.shuffle(offsets)
    out: list[str] = []
    for off in offsets:
        if off == 0:
            continue
        n = base + off if is_int else round(value + off * 0.5, 1)
        if n <= 0:
            continue
        text = str(int(n)) if float(n).is_integer() else f"{n:g}"
        out.append(text + suffix)
        if len(out) == 3:
            break
    return out


# --- Text shaping ---------------------------------------------------------

def _cap_question(text: str) -> str:
    if len(text) <= MAX_QUESTION_LEN:
        return text
    idx = text.find(_BLANK)
    if idx == -1:
        idx = 0
    start = max(0, idx - MAX_QUESTION_LEN // 3)
    start = min(start, max(0, len(text) - MAX_QUESTION_LEN))
    chunk = text[start : start + MAX_QUESTION_LEN]
    if start + MAX_QUESTION_LEN < len(text):
        cut = max(chunk.rfind(". "), chunk.rfind("? "), chunk.rfind("! "))
        blank_off = idx - start
        if cut > max(60, blank_off + 6):
            chunk = chunk[: cut + 1]
    return chunk


def _material_ref(sent: Segment) -> str | None:
    """'📄 Biology · notes.pdf · p.3' — whatever is known about the origin."""
    parts: list[str] = []
    if sent.course:
        parts.append(sent.course)
    if sent.filename:
        parts.append(sent.filename)
    if sent.page is not None:
        parts.append(f"p.{sent.page}")
    if not parts:
        return None
    return "📄 " + " · ".join(parts)


def _compose_explanation(base: str | None, sent: Segment) -> str | None:
    """Combine the source sentence with the material reference, staying inside
    Telegram's explanation limit (the reference itself is never truncated)."""
    ref = _material_ref(sent)
    if base and ref:
        room = MAX_EXPLANATION_LEN - len(ref) - 2  # "\n\n"
        if len(base) > room:
            base = base[: max(0, room - 1)].rsplit(" ", 1)[0].rstrip() + "…"
        return f"{base}\n\n{ref}"
    return ref or base


# --- Question builders ----------------------------------------------------

def _options_from(rng: random.Random, correct: str, distractors: list[str]) -> tuple[list[str], int] | None:
    """Assemble the option list, giving every option the answer's capitalisation."""
    options = [correct]
    for d in distractors:
        if d.lower() != correct.lower() and d not in options:
            options.append(d)
    if len(options) < 2:
        return None
    if correct[:1].isupper():
        options = [o[:1].upper() + o[1:] for o in options]
    elif correct.isupper() or correct.islower():
        options = [o if o.isupper() else o.lower() for o in options]
    rng.shuffle(options)
    return options, options.index(correct)


def _build_def2term(sent: Segment, stats: TermStats, rng: random.Random) -> Question | None:
    """"Which term is described as …?" — the most reliable way to quiz a concept."""
    definition = _find_definition(sent.text)
    if definition is None:
        return None
    term = definition.term
    # the term must not appear in its own definition, or the answer leaks
    if term.lower() in definition.body.lower():
        return None
    # "The problem is …" asks better as "problem", not "The problem"
    display = _ARTICLE_RE.sub("", term).strip() or term
    if not stats.is_key(display):
        return None
    distractors = _distractors(display, stats, rng, sent.page, source=sent.text)
    if len(distractors) < 2:
        return None
    shape = _options_from(rng, display, distractors)
    if shape is None:
        return None
    options, correct_index = shape

    body = definition.body.strip()
    if len(body) > 170:
        body = body[:170].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    text = _cap_question(f'Which term is described as: "{body}"')
    if len(text) < len(_BLANK):
        return None

    return Question(
        text=text,
        options=options,
        correct_index=correct_index,
        kind="mcq",
        subtype="def2term",
        explanation=_compose_explanation(sent.text, sent),
        course=sent.course,
        filename=sent.filename,
        page=sent.page,
    )


def _build_term2def(sent: Segment, stats: TermStats, rng: random.Random) -> Question | None:
    """Blank the term: "______ is the division of a cell"."""
    definition = _find_definition(sent.text)
    if definition is None:
        return None
    term = definition.term
    if len(sent.text) > MAX_QUESTION_LEN or not stats.is_key(term):
        return None
    # the blank swallows the article too, so the options must be bare nouns —
    # otherwise the list reads "The mitochondria" next to "Cell"
    display = _ARTICLE_RE.sub("", term).strip() or term
    if not stats.is_key(display):
        return None
    distractors = _distractors(display, stats, rng, sent.page, source=sent.text)
    if len(distractors) < 2:
        return None
    shape = _options_from(rng, display, distractors)
    if shape is None:
        return None
    options, correct_index = shape

    text = sent.text[: definition.start] + _BLANK + sent.text[definition.end :]
    text = _cap_question(text)
    if not text.rstrip().endswith((".", "!", "?", "…")):
        text = text.rstrip() + "."

    return Question(
        text=text,
        options=options,
        correct_index=correct_index,
        kind="mcq",
        subtype="term2def",
        explanation=_compose_explanation(sent.text, sent),
        course=sent.course,
        filename=sent.filename,
        page=sent.page,
    )


def _pick_number(sent: Segment) -> re.Match | None:
    """Prefer a number that carries meaning — one with a unit or a percentage."""
    matches = list(_NUMBER_RE.finditer(sent.text))
    if not matches:
        return None
    for match in matches:
        tail = sent.text[match.end() : match.end() + 14]
        if _UNIT_RE.match(tail.strip() + " ") or match.group().endswith("%"):
            return match
    return matches[0]


def _build_number(sent: Segment, stats: TermStats, rng: random.Random) -> Question | None:
    """"There are 118 known elements" -> blank it, with numeric distractors."""
    match = _pick_number(sent)
    if match is None:
        return None
    token = match.group().strip()
    if not _NUMBER_LIKE.fullmatch(token.rstrip(".")):
        return None
    # a bare page/figure number is not a fact worth asking about
    if len(token) <= 2 and not token.endswith("%"):
        return None
    distractors = _number_distractors(token, rng)
    if len(distractors) < 2:
        return None
    shape = _options_from(rng, token, distractors)
    if shape is None:
        return None
    options, correct_index = shape

    text = _cap_question(sent.text[: match.start()] + _BLANK + sent.text[match.end() :])
    if len(text) < len(_BLANK):
        return None
    if not text.rstrip().endswith((".", "!", "?", "…")):
        text = text.rstrip() + "."

    return Question(
        text=text,
        options=options,
        correct_index=correct_index,
        kind="mcq",
        subtype="number",
        explanation=_compose_explanation(sent.text, sent),
        course=sent.course,
        filename=sent.filename,
        page=sent.page,
    )


def _other_number(token: str, rng: random.Random) -> str | None:
    suffix = "%" if token.endswith("%") else ""
    raw = token[: -1] if suffix else token
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return None
    is_int = float(value).is_integer()
    base = int(value)
    for _ in range(12):
        off = rng.randint(-5, 5) or 1
        n = base + off if is_int else round(value + off * 0.5, 1)
        if n <= 0:
            continue
        text = str(int(n)) if float(n).is_integer() else f"{n:g}"
        if text != raw:
            return text + suffix
    return None


def _swap_word(text: str, original: str, replacement: str) -> str | None:
    """Replace a whole word only.

    ``str.replace`` happily turns "searching" into "nodesing" — the mutated
    sentence has to stay a sentence a teacher could have written.
    """
    pattern = re.compile(rf"\b{re.escape(original)}\b")
    if not pattern.search(text):
        return None
    return pattern.sub(lambda _: replacement, text, count=1)


def _build_tf(sent: Segment, stats: TermStats, rng: random.Random) -> Question | None:
    """True/false, but only from a sentence that states something checkable.

    The mutation swaps a **central term** (or a number) for a different one, so
    the false statement is a plausible claim about the subject rather than a
    word-salad sentence. A true statement is only offered when the sentence is
    a definition or quotes a value — an arbitrary sentence as "True" would be
    a coin flip, not a question.
    """
    if len(sent.text) > MAX_QUESTION_LEN:
        return None
    definition = _find_definition(sent.text)
    number = _pick_number(sent)
    valued_number = bool(
        number
        and _UNIT_RE.search((sent.text[number.end(): number.end() + 14]) or " ")
    )
    if definition is None and not valued_number:
        return None  # nothing here a student could be right or wrong about

    text = sent.text
    mutated = False
    base: str | None = None

    terms = [t for t in stats.key_terms_in(sent.text) if t in sent.text]
    if terms and (definition is not None or rng.random() < 0.5):
        for original in terms[:3]:
            swaps = _distractors(original, stats, rng, sent.page, k=6, source=sent.text)
            if not swaps:
                continue
            replacement = swaps[0]
            if not _mutates_cleanly(sent.text, original, replacement):
                continue
            candidate = _swap_word(sent.text, original, replacement)
            if candidate and candidate != sent.text and len(candidate) <= MAX_QUESTION_LEN:
                text = candidate
                mutated = True
                base = f"Correct: {sent.text}"
                break

    if not mutated and number is not None:
        token = number.group().strip().rstrip(".")
        if len(token) > 2 or token.endswith("%"):
            replacement = _other_number(token, rng)
            if replacement:
                start = sent.text.find(token)
                if start != -1:
                    text = (
                        sent.text[:start]
                        + replacement
                        + sent.text[start + len(token) :]
                    )
                    mutated = True
                    base = f"Correct: {sent.text}"

    options = ["True", "False"]
    correct_index = 0 if not mutated else 1
    if rng.random() < 0.5:
        options.reverse()
        correct_index = 1 - correct_index

    # even a verbatim "True" gets the sentence back, so a student who was
    # unsure can check it against their notes
    if base is None:
        base = f"Correct: {sent.text}"

    return Question(
        text=text,
        options=options,
        correct_index=correct_index,
        kind="tf",
        subtype="mutated" if mutated else "verbatim",
        explanation=_compose_explanation(base, sent),
        course=sent.course,
        filename=sent.filename,
        page=sent.page,
    )


# --- Core generation ------------------------------------------------------

_BUILDERS = (_build_def2term, _build_term2def, _build_number, _build_tf)


def _quizworthy(sent: Segment, stats: TermStats) -> float:
    """How good a source this sentence is (0 = never build a question from it)."""
    definition = _find_definition(sent.text)
    has_number = _pick_number(sent) is not None
    if definition is None and not has_number:
        return 0.0

    score = 0.0
    if definition is not None and stats.is_key(definition.term):
        score += 6.0
    if has_number:
        score += 3.0
    score += min(6.0, stats.value_of_text(sent.text))
    if sent.page is None:
        score -= 1.0
    return score


def _generate(
    tagged: list[Segment],
    count: int,
    rng: random.Random,
) -> list[Question]:
    if len(tagged) < 2 or sum(len(s.text.split()) for s in tagged) < 25:
        raise InsufficientTextError(
            "Not enough readable text to build a quiz — the PDF may be a "
            "scanned image or just notes headers."
        )

    stats = TermStats.build(tagged)
    cut = [_strip_column_bleed(s.text, stats) for s in tagged]
    tagged = [
        Segment(text, s.course, s.filename, s.page)
        for s, text in zip(tagged, cut)
        if text and not _is_junk(text)
    ]
    ranked = sorted(
        ((s, _quizworthy(s, stats)) for s in tagged),
        key=lambda pair: pair[1],
        reverse=True,
    )
    ranked = [(s, score) for s, score in ranked if score > 0]
    if not ranked:
        raise InsufficientTextError(
            "No facts found in this material — it reads like notes headers "
            "rather than study content."
        )

    questions: list[Question] = []
    seen_stems: set[str] = set()
    used_sentences: set[str] = set()
    # True/false is the easiest question to build from any sentence, so
    # uncapped it swamps the quiz. Keep it to roughly a third.
    tf_cap = max(1, (count + 2) // 3)
    tf_count = 0

    def add(question: Question | None, sent: Segment) -> bool:
        nonlocal tf_count
        if question is None:
            return False
        if question.kind == "tf" and tf_count >= tf_cap:
            return False
        stem = re.sub(r"\W+", "", question.text.lower())[:70]
        if stem in seen_stems:
            return False
        seen_stems.add(stem)
        used_sentences.add(sent.text)
        if question.kind == "tf":
            tf_count += 1
        questions.append(question)
        return True

    # pass 1 — one question per source sentence, best kind of question first
    for sent, _ in ranked:
        if len(questions) >= count:
            break
        builders = list(_BUILDERS)
        rng.shuffle(builders)
        for builder in builders:
            if add(builder(sent, stats, rng), sent):
                break

    # pass 2 — thin material can give a second question, but prefer fresh
    # sentences so one page does not end up carrying the whole quiz
    if len(questions) < count:
        fresh = [(s, sc) for s, sc in ranked if s.text not in used_sentences]
        reused = [(s, sc) for s, sc in ranked if s.text in used_sentences]
        for sent, _ in fresh + reused:
            if len(questions) >= count:
                break
            builders = list(_BUILDERS)
            rng.shuffle(builders)
            for builder in builders:
                if add(builder(sent, stats, rng), sent):
                    break

    if not questions:
        raise InsufficientTextError(
            "Could not build any questions from this material."
        )
    return questions


# --- Public API -----------------------------------------------------------

def generate_questions(
    text: str,
    count: int = DEFAULT_QUESTION_COUNT,
    seed: int | None = None,
) -> list[Question]:
    """Build up to `count` quiz questions from raw study text.

    Raises InsufficientTextError when there is not enough readable text
    (e.g. an empty or scanned/image-only PDF).
    """
    if not text or not text.strip():
        raise InsufficientTextError(
            "No readable text found — the PDF may be a scanned image."
        )
    count = max(1, min(int(count), MAX_QUESTIONS))
    rng = random.Random(seed)
    tagged = _collect_sentences([Segment(text=text)])
    return _generate(tagged, count, rng)


def generate_questions_from_segments(
    segments: list[Segment],
    count: int = DEFAULT_QUESTION_COUNT,
    seed: int | None = None,
) -> list[Question]:
    """Like generate_questions, but each segment keeps its provenance so the
    explanation can point at course · file · page."""
    if not segments:
        raise InsufficientTextError("There is no material to build questions from.")
    count = max(1, min(int(count), MAX_QUESTIONS))
    rng = random.Random(seed)
    tagged = _collect_sentences(segments)
    return _generate(tagged, count, rng)


def generate_mixed_questions(
    sources: list[tuple[str, str] | tuple[str, list[Segment]]],
    count: int = DEFAULT_QUESTION_COUNT,
    seed: int | None = None,
) -> tuple[list[Question], list[tuple[str, str]]]:
    """Build a quiz that mixes questions from several courses.

    Each source is (label, payload) where payload is either plain text or a
    list of Segments (preferred — keeps file/page provenance).

    Questions are drawn round-robin so every course contributes proportionally,
    instead of one big course crowding out the others.

    Returns (questions, skipped) where skipped is a list of (label, reason)
    for courses that had no usable text.
    """
    if not sources:
        raise InsufficientTextError("There are no courses with material yet.")
    count = max(1, min(int(count), MAX_QUESTIONS))

    pools: list[list[Question]] = []
    skipped: list[tuple[str, str]] = []
    for i, source in enumerate(sources):
        label, payload = source[0], source[1]
        if isinstance(payload, str):
            segments = [Segment(text=payload, course=label)]
        else:
            segments = [
                Segment(text=s.text, course=s.course or label,
                        filename=s.filename, page=s.page)
                for s in payload
            ]
        sub_seed = seed + i if seed is not None else None
        try:
            pools.append(generate_questions_from_segments(segments, count, sub_seed))
        except InsufficientTextError as exc:
            skipped.append((label, str(exc)))

    if not pools:
        raise InsufficientTextError(
            "None of the courses had enough readable text to build a quiz."
        )

    merged: list[Question] = []
    cursor = [0] * len(pools)
    while len(merged) < count:
        progressed = False
        for p, qs in enumerate(pools):
            if cursor[p] < len(qs):
                merged.append(qs[cursor[p]])
                cursor[p] += 1
                progressed = True
                if len(merged) == count:
                    break
        if not progressed:
            break
    return merged, skipped


def generate_note(segments: list[Segment], seed: int | None = None) -> Note:
    """Pick one high-value sentence from the material as a study note.

    Good notes state a definition or a fact about a central term, so sentences
    are ranked by the centrality of their best term; picking at random inside
    the top group keeps two consecutive days from showing the same line.
    """
    if not segments:
        raise InsufficientTextError("There is no material to build a note from.")
    tagged = _collect_sentences(segments)
    if not tagged:
        raise InsufficientTextError(
            "Could not find a usable sentence for a note — the PDF may be a "
            "scanned image or just notes headers."
        )

    stats = TermStats.build(tagged)
    cleaned: list[Segment] = []
    for s in tagged:
        text = _strip_column_bleed(s.text, stats)
        # cutting a column bleed can leave a half sentence behind, so the
        # junk checks have to run again on the result
        if text and not _is_junk(text):
            cleaned.append(Segment(text, s.course, s.filename, s.page))
    tagged = cleaned
    if not tagged:
        raise InsufficientTextError(
            "Could not find a usable sentence for a note — the PDF may be a "
            "scanned image or just notes headers."
        )
    scored = [(_quizworthy(s, stats), s) for s in tagged]
    usable = sorted(
        ((score, s) for score, s in scored if score > 0),
        key=lambda pair: pair[0],
        reverse=True,
    )
    pool = usable or [(0.0, s) for _, s in scored]

    rng = random.Random(seed)
    _, chosen = rng.choice(pool[: min(len(pool), max(3, len(pool) // 3))])

    text = chosen.text.strip()
    if not text.endswith((".", "!", "?", "…")):
        text += "."
    definition = _find_definition(chosen.text)
    term = definition.term if definition is not None else None
    return Note(
        text=text,
        course=chosen.course,
        filename=chosen.filename,
        page=chosen.page,
        term=term,
        detail=_supporting_sentence(chosen, term, tagged, rng),
    )


def _supporting_sentence(
    chosen: Segment,
    term: str | None,
    tagged: list[Segment],
    rng: random.Random,
) -> str | None:
    """A second sentence about the same term, so a note is a study point.

    Both sentences have to come from the material; the supporting one is only
    used when it says something about the term without repeating the note.
    """
    if not term:
        return None
    low = term.lower()
    options = [
        s for s in tagged
        if s.text != chosen.text
        and low in s.text.lower()
        and len(_content_words(s.text)) >= _MIN_DEFINITION_CONTENT
    ]
    if not options:
        return None
    rng.shuffle(options)
    return options[0].text.strip()


# --- Question answering ----------------------------------------------------

@dataclass
class Answer:
    """A reply built only from sentences that are actually in the material."""

    query: str
    text: str                          # the answer sentence
    course: str | None = None
    filename: str | None = None
    page: int | None = None
    also: list[str] = field(default_factory=list)   # other places it is said
    is_definition: bool = False

    def render(self) -> str:
        """A Telegram-ready HTML message: the answer, then where it is written.

        Nothing here is generated prose — every line is a sentence copied out
        of a PDF, so the student can check it against their own material.
        """
        head = "📖 <b>From your material</b>" if self.is_definition else "📖 <b>Closest match</b>"
        lines = [head, "", escape(self.text)]
        refs: list[str] = []
        if self.filename or self.course:
            parts = [p for p in (self.course, self.filename) if p]
            ref = " · ".join(parts)
            if self.page is not None:
                ref += f" · p.{self.page}"
            refs.append(f"📄 {escape(ref)}")
        for other in self.also:
            refs.append(f"📎 {escape(other)}")
        if refs:
            lines += ["", "━━━━━━━━━━━━", *refs]
        return "\n".join(lines)


def _stem_token(word: str) -> str:
    """Fold a word to a crude stem so 'attributes' matches 'attribute'."""
    w = word.lower()
    for suffix in ("ing", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[: -len(suffix)]
    return w


def _tokenize(text: str) -> list[str]:
    return [
        _stem_token(w) for w in _QWORDS.findall(text)
        if w.lower() not in STOPWORDS
    ]


# The words people open a study question with — they say nothing about the
# material, so they must not influence the ranking.
_QUESTION_WORDS = frozenset(
    """
    what which who when where why how is are was were do does did can could
    should would tell explain describe define definition meaning mean means
    about give list name any some please help hi hello hey bot
    """.split()
)

# Words students add to any question that must not be required to be present
# in their notes, otherwise "the difference between X and Y" fails whenever the
# slides never use the word "difference".
_GENERIC_QUERY_WORDS = frozenset(
    """
    difference differences example examples type types kind kinds sort sorts
    meaning meanings purpose purposes simple simply short briefly easy easily
    better best harder exactly specifically definition definitions tell explain
    describe say says said used use uses useful work works working happen
    happens happens know known understand
    """.split()
)

_FILLER_RE = re.compile(
    r"^\s*(?:hi|hey|hello|please|pls|ok|okay)?[\s,!?.]*"
    r"(?:can you|could you|would you|i want to know|i need to know|tell me|"
    r"explain to me|explain|describe|define|give me|give|"
    # "what is database" / "what's database" / "what does database mean"
    r"what(?:'s|\u2019s| is| are| does| do)?|"
    r"how(?:'s|\u2019s| is| are| does| do)?|"
    r"where(?:'s|\u2019s| is| are)?|"
    r"who(?:'s|\u2019s| is| are)?|"
    r"why(?:'s|\u2019s| is| are)?)\b[^a-z0-9]*",
    re.IGNORECASE,
)
_TRAILING_RE = re.compile(
    r"\b(?:please|pls|thanks|thank you|for me|about that|in my notes|"
    r"from my notes|in the pdf|from the pdf)\b\.?\s*$",
    re.IGNORECASE,
)


def _focus_terms(query: str) -> list[str]:
    """The content words of a question, in the order they were asked."""
    # "whats" / "hows" typed without an apostrophe
    query = re.sub(r"\b(what|how|where|who|why)s\b", r"\1 is", query, flags=re.IGNORECASE)
    cleaned = _TRAILING_RE.sub("", _FILLER_RE.sub("", query.strip()))
    words = [
        w for w in _QWORDS.findall(cleaned)
        if w.lower() not in _QUESTION_WORDS and w.lower() not in STOPWORDS
    ]
    return words or _QWORDS.findall(query)


class Retriever:
    """BM25 over the cleaned sentences of some material.

    BM25 rather than plain word matching because a question asks for a *rare*
    word ("ternary") as often as a common one ("key"), and BM25 rewards the
    rare word appearing often in one sentence without letting long pages win
    on length alone.
    """

    K1 = 1.5
    B = 0.75

    def __init__(self, segments: list[Segment]) -> None:
        tagged = _collect_sentences(segments)
        stats = TermStats.build(tagged)
        clean: list[Segment] = []
        for s in tagged:
            text = _strip_column_bleed(s.text, stats)
            if text and not _is_junk(text):
                clean.append(Segment(text, s.course, s.filename, s.page))
        self.sentences = clean
        self.docs = [_tokenize(s.text) for s in self.sentences]
        self.lengths = [len(d) for d in self.docs]
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if self.docs else 0.0
        # "database" -> the sentence that defines it, so asking what a term
        # means does not depend on which sentence BM25 happened to rank first
        self.definitions: dict[str, list[Segment]] = {}
        for sent in self.sentences:
            definition = _find_definition(sent.text)
            if definition is None:
                continue
            term = _ARTICLE_RE.sub("", definition.term).strip().lower()
            if not term:
                continue
            self.definitions.setdefault(term, []).append(sent)
        self.freqs: list[Counter] = [Counter(d) for d in self.docs]
        df: Counter = Counter()
        for freq in self.freqs:
            df.update(freq.keys())
        n = len(self.docs)
        self.idf: dict[str, float] = {
            term: math.log(1.0 + (n - count + 0.5) / (count + 0.5))
            for term, count in df.items()
        }

    def __bool__(self) -> bool:
        return bool(self.sentences)

    def _score(self, index: int, query_terms: list[str]) -> float:
        total = 0.0
        length = self.lengths[index]
        for term in set(query_terms):
            tf = self.freqs[index].get(term, 0)
            if not tf:
                continue
            norm = tf * (self.K1 + 1) / (
                tf + self.K1 * (1 - self.B + self.B * length / (self.avg_len or 1.0))
            )
            total += self.idf.get(term, 0.0) * norm
        return total

    def search(self, query: str, limit: int = 5) -> list[tuple[Segment, float]]:
        """The sentences that best answer `query`, best first."""
        terms = [_stem_token(w) for w in _focus_terms(query)]
        if not terms or not self.sentences:
            return []
        scored = [(self._score(i, terms), s) for i, s in enumerate(self.sentences)]
        hits = [(s, score) for score, s in scored if score > 0]
        hits.sort(key=lambda pair: pair[1], reverse=True)
        return hits[:limit]

    def vocabulary(self) -> set[str]:
        """Every stem that appears anywhere in the material."""
        return set(self.idf)

    def define(self, word: str) -> Segment | None:
        """The sentence that defines exactly `word`, if the material has one.

        Exact before approximate: asking "what is a database" must return the
        definition of *database*, not of *database normalization* just because
        that sentence happens to repeat the word more often.
        """
        word = word.strip().lower()
        if not word:
            return None
        if word in self.definitions:
            return self.definitions[word][0]
        # a query with two or three words may name a multi-word term
        for length in (3, 2):
            parts = word.split()
            if len(parts) < length:
                continue
            for start in range(len(parts) - length + 1):
                phrase = " ".join(parts[start : start + length])
                if phrase in self.definitions:
                    return self.definitions[phrase][0]
        return None


def answer_question(
    query: str,
    segments: list[Segment],
    limit: int = 5,
) -> Answer | None:
    """Answer a study question from the material, or None if it isn't there.

    The answer is always a sentence copied from a PDF — never a generated
    summary — so it can be checked against the student's own notes. When the
    question asks "what is X" and the material defines X, that definition is
    preferred over whichever sentence merely happens to score highest.
    """
    index = Retriever(segments)
    if not index:
        return None

    focus = _focus_terms(query)
    if not focus:
        return None

    hits = index.search(query, limit=limit)
    if not hits:
        return None

    # If the best sentence does not actually contain one of the words the
    # student typed, the match was a stem accident ("quantum" ~ "comput") and
    # the honest answer is "I couldn't find that" rather than a wrong sentence.
    required = [
        w for w in focus
        if w.lower() not in _GENERIC_QUERY_WORDS and len(w) >= 4
    ]
    if required:
        best_words = {w.lower() for w in _QWORDS.findall(hits[0][0].text)}
        if not any(w.lower() in best_words for w in required):
            return None

    # 1. the material defines exactly what was asked -> use that definition
    for word in focus:
        exact = index.define(word)
        if exact is not None:
            return Answer(
                query=query,
                text=exact.text,
                course=exact.course,
                filename=exact.filename,
                page=exact.page,
                also=_other_places(hits, exact, index),
                is_definition=True,
            )

    # 2. otherwise a definition of something covering the question wins
    low_focus = [w.lower() for w in focus]
    for sent, _ in hits:
        definition = _find_definition(sent.text)
        if definition is None:
            continue
        term = _ARTICLE_RE.sub("", definition.term).strip().lower()
        if not term:
            continue
        if any(_covers(term, f) for f in low_focus):
            return Answer(
                query=query,
                text=sent.text,
                course=sent.course,
                filename=sent.filename,
                page=sent.page,
                also=_other_places(hits, sent, index),
                is_definition=True,
            )

    # 3. otherwise the closest matching sentence
    sent, _ = hits[0]
    return Answer(
        query=query,
        text=sent.text,
        course=sent.course,
        filename=sent.filename,
        page=sent.page,
        also=_other_places(hits, sent, index),
    )


def _covers(term: str, word: str) -> bool:
    """Whole-word containment, so 'search' does not match 'informed search'."""
    return term == word or term.startswith(f"{word} ") or word.startswith(f"{term} ")


def _other_places(
    hits: list[tuple[Segment, float]],
    chosen: Segment,
    index: Retriever,
) -> list[str]:
    """Where else in the material the same thing is said, for cross-reading."""
    terms = set(_tokenize(chosen.text))
    refs: list[str] = []
    seen_pages: set[tuple] = set()
    for sent, _ in hits[1:]:
        if sent.text == chosen.text:
            continue
        overlap = len(terms & set(_tokenize(sent.text)))
        if overlap < 3:
            continue
        key = (sent.course, sent.filename, sent.page)
        if key in seen_pages:
            continue
        seen_pages.add(key)
        parts = [p for p in (sent.course, sent.filename) if p]
        if not parts:
            continue
        ref = " · ".join(parts)
        if sent.page is not None:
            ref += f" · p.{sent.page}"
        refs.append(ref)
        if len(refs) == 2:
            break
    return refs


# --- Old exam papers -----------------------------------------------------
# A past paper already contains questions *and* their answers. Those are the
# best possible quiz material: real questions a student actually faced, with
# the examiner's own answer key. This reads them and turns each one into a
# poll verbatim rather than re-inventing something.

_ANSWER_WORD = r"(?:ans(?:wer)?s?|correct|solution|key)"
# "Ans: x", "Answer - x", "Ans. x" — the separator is optional, because many
# papers write a bare "Ans." and put the answer straight after it.
_ANSWER_LINE_RE = re.compile(
    rf"^\s*{_ANSWER_WORD}\b\.?\s*(?:[:\-–]\s*)?", re.IGNORECASE
)
# the same marker mid-line, where a real separator is required to avoid
# matching ordinary prose such as "the correct answer is …"
_ANSWER_INLINE_RE = re.compile(
    rf"\s{_ANSWER_WORD}\b\.?\s*[:\-–]\s*", re.IGNORECASE
)
# "1)", "Q2.", "(ii)" and — in many papers — a bare "4 Which …"
_ITEM_RE = re.compile(r"^\s*(?:q(?:uestion)?\s*)?[(\[]?\d{1,2}[).]\s+", re.IGNORECASE)
_ITEM_BARE_RE = re.compile(r"^\s*\d{1,2}\s+(?=[A-Za-z])")
# "a)", "(ii)", "A." — the start of one answer option
_OPTION_RE = re.compile(
    r"^\s*(?:[(\[]?([a-dA-D])[)\].]|[(\[]?(i{1,3}|iv|v)[)\].])\s*",
    re.IGNORECASE,
)
# papers often repeat the label on its own line: "A." then "a. the text"
_DUP_LABEL_RE = re.compile(r"^\s*(?:[a-dA-D]|\d{1,2})[.)]\s*", re.IGNORECASE)
# artefacts of the export, not question text
_NOISE_LINE_RE = re.compile(
    r"^\s*(?:question\s*\d+\s*answer|end of (?:question|paper)|"
    r"\d{1,3}\s*\|\s*page|page\s*\d+(?:\s*of\s*\d+)?|page\s*\d+\s*\|)",
    re.IGNORECASE,
)
_OPTION_LETTERS = ("a", "b", "c", "d")


@dataclass
class ExamPair:
    """One question from a past paper, with the answer it was given.

    `options` holds the paper's own options when it had them, which makes for
    a far more faithful poll than inventing distractors.
    """

    question: str
    answer: str
    course: str | None = None
    filename: str | None = None
    page: int | None = None
    options: list[str] = field(default_factory=list)


def _strip_answer_lead(text: str) -> tuple[str | None, str]:
    """"a. Removes bullet points" -> ("a", "Removes bullet points").

    Papers write the chosen option's label in front of its text; that label is
    how we know *which* option is correct. Some write the bare letter instead
    ("Ans: c"), which means the same thing.
    """
    match = _DUP_LABEL_RE.match(text)
    if match:
        label = match.group().strip().rstrip(".)").lower()
        if label in _OPTION_LETTERS:
            return label, text[match.end():].strip()
    stripped = text.strip()
    if len(stripped) == 1 and stripped.lower() in _OPTION_LETTERS:
        return stripped.lower(), ""
    return None, stripped


def _looks_like_code(block: str) -> bool:
    """Is this brace block code rather than maths?

    "nav ul { list-style-type: none; }" is spilled-over CSS, while
    "{aⁿbⁿcⁿ | n ≥ 1}" is part of the question itself and must survive.
    """
    return ";" in block or ":" in block


def _strip_code_blocks(text: str) -> str:
    """Drop CSS/code braces a page can carry over from an adjacent question."""
    previous = None
    while previous != text:
        previous = text
        text = re.sub(
            r"\{[^{}]*\}",
            lambda m: " " if _looks_like_code(m.group()) else m.group(),
            text,
        )
    return re.sub(r"\s+", " ", text).strip()


def _normalize_exam_text(text: str) -> str:
    """Give the paper a usable line structure before it is parsed.

    How a PDF's text is broken into lines varies wildly — some extractors keep
    the author's line breaks, some glue a whole page onto one line. Rather than
    depend on either, this puts a line break in front of every structural
    marker (a new numbered item, an a)/b) option, an "Ans:") so the parser
    sees the same layout either way.
    """
    # broken PDF font mapping leaves "(cid:12)" between words; it is noise
    text = _CID_BLOCK_RE.sub("\n", text)
    # A new question, option or answer marker starts a new line. These use a
    # lookahead rather than a backreference so the marker text is left alone.
    _SPACE = r"[^\S\n]+"
    text = re.sub(
        _SPACE + r"(?=Q\s*\d{1,2}\s*[).]|\d{1,2}[).]\s)", "\n", text
    )
    text = re.sub(
        _SPACE + r"(?=[(\[]?[a-dA-D][)\].]\s|[(\[]?(?:i{1,3}|iv|v)[)\].]\s)",
        "\n",
        text,
    )
    text = re.sub(
        _SPACE + rf"(?={_ANSWER_WORD}\b\.?\s*[:\-–]\s)",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _exam_items(text: str) -> list[tuple[list[str], str]]:
    """Split a page into ((question lines), answer line) items.

    Papers write the answer key either on the marker's own line ("Ans. b.
    Linear Bounded Automaton") or with the marker alone and the answer on the
    next line, so both are handled.
    """
    items: list[tuple[list[str], str]] = []
    current: list[str] = []
    lines = [
        line.strip()
        for line in _normalize_exam_text(text).splitlines()
        if line.strip() and not _NOISE_LINE_RE.match(line.strip())
    ]
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = _ANSWER_LINE_RE.match(line)
        if marker:
            answer = line[marker.end():].strip()
            index += 1
            if not answer and index < len(lines):
                # a bare "Ans." — the answer is the following line
                answer = lines[index].strip()
                index += 1
            if answer and current:
                items.append((list(current), answer))
            current = []
            continue
        if (_ITEM_RE.match(line) or _ITEM_BARE_RE.match(line)) and current:
            joined = " ".join(current)
            inline = _ANSWER_INLINE_RE.search(joined)
            if inline:  # a one-line "… Ans: X" item
                items.append(
                    ([joined[: inline.start()].strip()], joined[inline.end():].strip())
                )
            current = [line]
            index += 1
            continue
        current.append(line)
        index += 1
    return items


def _split_question_and_options(lines: list[str]) -> tuple[str, list[tuple[str, str]]]:
    """Separate the question stem from the a)/b)/c) options beneath it.

    Returns the stem plus the options as (label, text) pairs, so a bare
    "Ans. c" can be resolved to the right option by label rather than by
    position (an option whose text failed to extract would shift them).
    """
    stem_lines: list[str] = []
    options: list[tuple[str, str]] = []
    for line in lines:
        match = _OPTION_RE.match(line)
        if match:
            marker = (match.group(1) or match.group(2)).lower()
            body = _OPTION_RE.sub("", line).strip()
            # "A." on its own line, with the letter repeated as "a. the text"
            if not body or _DUP_LABEL_RE.fullmatch(body):
                body = ""
            options.append((marker, body))
        elif options and not _NOISE_LINE_RE.match(line):
            # a wrapped continuation of the previous option
            marker, body = options[-1]
            options[-1] = (marker, f"{body} {line}".strip())
        elif not options:
            stem_lines.append(line)
    # a selector immediately above a CSS rule is spill-over, not question text
    kept = [
        line for i, line in enumerate(stem_lines)
        if not (
            i + 1 < len(stem_lines)
            and stem_lines[i + 1].lstrip().startswith("{")
            and not line.endswith("?")
        )
    ]
    return " ".join(kept).strip(), options


def _clean_exam_question(text: str) -> str:
    text = _ITEM_RE.sub("", text)
    text = _ITEM_BARE_RE.sub("", text)
    text = _strip_code_blocks(text)
    text = re.sub(r"\s+", " ", text).strip(" .;,")
    if text.endswith(":"):
        # "…we use:" reads as a question in a paper, not as a lead-in
        text = text[:-1].strip() + "?"
    elif text and not text.endswith((".", "?", "!")):
        text += "?"
    return text


def _clean_exam_answer(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" .:-")
    if not text or _ANSWER_LINE_RE.match(text) or re.match(r"^\d{1,3}$", text):
        return ""
    return text


def exam_pairs(segments: list[Segment]) -> list[ExamPair]:
    """Every question/answer pair a past paper states outright.

    Handles both layouts: a written answer ("Ans. A candidate key is a
    minimal superkey…") and a multiple-choice letter ("A. a. 1NF … Ans. c.
    3NF"), where the letter is resolved to the option's own text.

    If a paper has no answer key this returns nothing, and the caller builds
    questions from the exam text the ordinary way — a generated question from a
    real exam beats a fabricated answer.
    """
    pairs: list[ExamPair] = []
    seen: set[str] = set()
    for seg in segments:
        for lines, raw_answer in _exam_items(seg.text):
            stem, marked = _split_question_and_options(lines)
            options = [text for _, text in marked if text]
            by_label = {label: text for label, text in marked if text}
            question = _clean_exam_question(stem)
            label, answer_text = _strip_answer_lead(raw_answer)
            answer = _clean_exam_answer(answer_text)
            # "Ans. c." / "Ans: c" -> the text of option c
            if label and label in by_label:
                answer = by_label[label]
            if not question or not answer:
                continue
            if len(question.split()) < 4 or len(question) > MAX_QUESTION_LEN:
                continue
            if len(answer) > 90 or len(answer) < 1:
                continue
            key = re.sub(r"\W+", "", question.lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            pairs.append(
                ExamPair(
                    question=question,
                    answer=answer,
                    course=seg.course,
                    filename=seg.filename,
                    page=seg.page,
                    options=options,
                )
            )
    return pairs


def questions_from_exams(
    exam_sources: list[tuple[str, list[Segment]]],
    count: int,
    course_segments: list[Segment] | None = None,
    rng: random.Random | None = None,
) -> list[Question]:
    """Build quiz questions from past papers, up to `count` of them.

    Real exam questions are used verbatim. When the paper listed options, those
    are the distractors — the most faithful choice there is. Otherwise they
    come from the student's own course notes, so the wrong options are terms
    they have actually been taught.
    """
    if not exam_sources:
        return []
    rng = rng or random.Random()
    pairs: list[ExamPair] = []
    for _, segments in exam_sources:
        pairs.extend(exam_pairs(segments))
    if not pairs:
        return []

    stats = TermStats.build(course_segments) if course_segments else None
    out: list[Question] = []
    for pair in pairs[: max(1, count)]:
        own = [
            o for o in dict.fromkeys(pair.options)
            if o.lower() != pair.answer.lower() and len(o) <= MAX_OPTION_LEN
        ]
        shape = None
        if len(own) >= 2:
            shape = _options_from(rng, pair.answer, own[:6])
        if shape is None:
            distractors = (
                _distractors(pair.answer, stats, rng, pair.page, source=pair.question)
                if stats else []
            )
            if len(distractors) < 2:
                # last resort: other answers from the same paper
                others = [
                    p.answer for p in pairs
                    if p.answer.lower() != pair.answer.lower()
                    and abs(len(p.answer) - len(pair.answer)) <= max(12, len(pair.answer))
                ]
                rng.shuffle(others)
                distractors = others[:3]
            if len(distractors) < 2:
                continue
            shape = _options_from(rng, pair.answer, distractors)
        if shape is None:
            continue
        options, correct_index = shape
        sent = Segment(
            text=pair.question,
            course=pair.course,
            filename=pair.filename,
            page=pair.page,
        )
        out.append(
            Question(
                text=_cap_question(pair.question),
                options=options,
                correct_index=correct_index,
                kind="mcq",
                subtype="exam",
                explanation=_compose_explanation(
                    f"From a past exam. Answer: {pair.answer}", sent
                ),
                course=pair.course,
                filename=pair.filename,
                page=pair.page,
            )
        )
    return out
