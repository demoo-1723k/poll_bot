# 📚 Course Quiz Bot (`@my_poller_bot`)

A Telegram bot for the study group **[@practicemakesperfect2](https://t.me/practicemakesperfect2)**:
save PDFs as **courses**, then generate a **mixed quiz** (fill-in-the-blank +
true/false Telegram quiz polls) built from *all* courses and post it to the group —
on demand, or automatically every day at a time you pick. It can also post a
daily **note** from a random course, and **answer questions about your notes**.

Generation is **rule-based** (no AI API, no cost): text is extracted with
`pdfplumber`, cleaned of layout debris, and turned into questions only from
sentences that actually state a definition or a fact. After someone answers,
Telegram shows an explanation with the original sentence **and its source —
course · file · page number** — so students can open the exact part of the
material and re-read it. The same rule holds for `/ask`: answers are sentences
copied out of your PDFs, never generated prose.

Everything the bot posts goes into one **forum topic** (`study room` by
default), so quizzes, notes and answers stay together and out of the rest of
the group chat.

## The flow

`/start` shows a greeting and a menu with inline buttons:

| Button | What happens |
|---|---|
| **Add Course** | Asks for the course name → you send as many PDFs as you like → press **Done** → course is saved (texts are extracted and cached) |
| **Delete Course** | Lists your courses → pick one → *"Are you sure?"* → **Yes, Delete** / **Cancel** |
| **Generate Quiz** | Asks **how many questions** (presets 5/10/15/25/50 — or type any number 1–50), then builds a quiz mixing every course (round-robin, so big courses don't drown out small ones) and posts it to the group |
| **Generate Note** | Picks a **random course** and posts one note from it **in the group** — the same message the daily note uses, so you can see it before turning the automation on |
| **⏰ Auto Quiz** | Shows the current status (**ON**/**OFF**), a description, and time buttons in 24-hour format → pick a time (or **⌨️ Custom time** and type it) → asks how many questions → the daily quiz is ready |
| **📝 Auto Note** | Same screen and time picker, but no quantity — at the chosen time a note from a random course is posted every day |
| **📚 Exams** | Save past exam papers *with their answers* — about 30% of every quiz then uses the real questions from them. Tap a saved paper to delete it |

Commands: `/start` · `/help` · `/cancel` · `/ask <question>` · `/topic`

## Everything goes in one topic

Quizzes, daily notes and every answer are posted into the **study room** topic
rather than the group's General chat.

Telegram gives a topic a numeric id that every message must carry, and there
is no API to look one up by name — so the bot learns it, in any of these ways:

1. **Do nothing.** When someone creates a topic named `study room`, Telegram
   sends the bot the new topic's id, and it remembers it.
2. **Send `/topic` from inside the topic** — the bot learns it from wherever
   the command came from.
3. **Pin it** — put `QUIZ_TOPIC_ID=<id>` in `.env`.

The name is configurable with `QUIZ_TOPIC_NAME`. If the id is not known yet,
the bot logs a hint at startup and falls back to General.

## How questions are chosen

The generator never invents a question. A sentence becomes a question only if
it states something a student could be right or wrong about:

* **Central terms, not random words.** A word is only used as a blank or as a
  distractor if it recurs across the material (frequency × page spread). A word
  that appears once inside a worked example is never asked about.
* **Real definitions only.** "A search tree is a representation in which nodes
  denote paths" qualifies. "Color change is one rectangle at a time" and
  "The goal is to get one liter of water" do not — a purpose clause is not a
  definition.
* **Four question kinds** — *which term is described as …*, *______ is …*,
  a *number* blank, and *true/false* where a central term or a number has been
  swapped for a plausible wrong one.
* **True/false is capped** at roughly a third of a quiz, so a quiz stays a
  quiz rather than a run of coin flips.
* **Layout debris is dropped** — Wingdings bullets, headings, tables of
  contents, figure captions, "Individual Assignment (5%)", running heads, and
  columns bleeding into each other.
* Every question keeps its **course · file · page** in the explanation.

## `/ask` — questions about your notes

```
/ask what is a weak entity
```

The bot searches every course with **BM25** (a ranking function that rewards a
rare word appearing often in one sentence, without letting long pages win on
length alone) and replies with the sentence that answers it:

```
📖 From your material

A weak entity set doesn't have any primary key which can identify each
entity in a set distinctly.

━━━━━━━━━━━━
📄 Database · DB CH3.pdf · p.9
📎 Database · DB CH3.pdf · p.6
```

* The answer is always a **sentence from your PDFs**, never a summary written
  by the bot.
* If the material doesn't contain the answer, the bot says so
  ("I couldn't find that in your notes") instead of returning a sentence that
  merely looks related. A question using a word that appears nowhere in the
  notes — "what is quantum entanglement?" — always gets that answer.
* 📎 lines point at other pages that mention the same thing, for cross-reading.

### Asking in the group

`/ask …` works anywhere. In a group the bot **only listens when it is spoken
to directly**, so it doesn't read along with everyone's chat:

* say **“baymax what is a weak entity”** — the wake word calls the bot
  (`jarvis`, `jarves` and `bay max` are recognised too, in any spelling);
* **reply to one of the bot's messages** and just type the question;
* send `/ask <question>` — a command always reaches the bot;
* or ask in a **private chat** with the bot, where nothing is posted publicly.

Anything else in the group is ignored. Because of this, privacy mode can stay
**enabled** — you don't need `/setprivacy → Disable` to use `/ask`. You only
need it if you send PDFs into the group itself; adding them in a private chat
works with privacy mode on.

## Exams — using past papers

A past paper already contains questions *and* the examiner's answers. Those are
the best quiz material there is, so the bot reads them instead of re-inventing
anything:

1. **/start → 📚 Exams → Upload exam PDF**
2. Give the paper a name (e.g. *Database Midterm 2024*) and send the PDF.
3. Press **Done**.

The PDF must be a **text PDF** — scanned image-only files are rejected with an
explanation, because their questions cannot be read.

Both layouts are understood:

* **Multiple choice** — `a) 1NF  b) 2NF  c) 3NF` with `Ans: c` becomes the
  question with the paper's *own* options, and the right one selected. The
  letter is resolved to the option's text.
* **Written answers** — `Ans: A candidate key is a minimal superkey…` becomes
  the question with that sentence as the correct option; the wrong options are
  drawn from terms in your own course notes, or from other answers in the same
  paper.

Only **about 30% of each quiz** (`EXAM_SHARE`) comes from exams, so they enrich
the quiz instead of replacing it — the rest is generated from your notes as
usual. A paper with **no** answer key contributes no exam questions at all:
the bot would rather skip it than invent an answer. Those papers are still used
as ordinary material.

Deleting a paper removes it and its folder from disk, and its questions leave
the quizzes at once.

## Auto Quiz & Auto Note

* **Time** — tap a preset or use **⌨️ Custom time** and type it yourself. Times
  must be in 24-hour format: `07:30`, `19:05` (`7:30` is fine too, it is saved
  as `07:30`); `8 pm` and `25:00` are rejected.
* **Firing** — the bot re-reads its saved schedules every 20 seconds, so a time
  changed in the menu takes effect at once. Each task runs **once per day**; if
  the bot was closed when the time came, it sends when it starts instead of
  skipping the day.
* **Turn off** — both automation screens carry **❌ Turn off** while they are
  running. The saved time and question count are kept, so switching it back on
  is a couple of taps.
* **Rotation** — the daily note never uses the same course twice in a row (with
  a single course there is no alternative, so it repeats).
* ⚠️ The bot is a normal local program: it can only post while `bot.py` is
  running, on a machine whose clock is set to your local time. Keep it running
  (or on a small always-on machine) if you rely on the daily posts.

A note looks like this:

```
📖 Daily Note

Photosynthesis is the process by which plants convert light energy
into chemical energy.

💡 The light dependent reactions take place in the thylakoid
membranes of chloroplasts.

━━━━━━━━━━━━
📚 Course: Biology
📄 File: ch4_photosynthesis.pdf
📃 Page: 12
```

The note is a central term with its definition, plus a second sentence about the
same term from elsewhere in the material.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Add the bot to your group and make it an **admin** (it needs to send polls).
3. Install and configure:

```bash
python -m venv .venv
source .venv/bin/activate      # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env           # then paste your token + QUIZ_CHAT_ID into .env
```

4. Run it:

```bash
python bot.py
```

No activation? Call the venv Python directly:
`.venv/Scripts/python.exe bot.py` (PowerShell/Windows) or `.venv/bin/python bot.py`.

First startup takes ~25 seconds on Windows (antivirus scans the Telegram
library) — give it a moment before the `Bot started` log appears.

### ⚠️ Important: privacy mode (optional)

The bot only receives group messages addressed to it unless privacy mode is
disabled. **You do not need to disable it for quizzes, notes or `/ask`** —
commands, inline buttons and replies to the bot all work with privacy mode on.

Disable it (Telegram: **@BotFather → /setprivacy → Disable → your bot**, then
restart the bot) only if you want to **send course PDFs from inside the
group**. Adding them in a private chat with the bot works either way.

## Storage

Courses live on disk, gitignored:

```
data/
  courses.json        # index: id, name, files
  exams.json          # index of old exam papers
  automation.json     # daily Auto Quiz / Auto Note schedules
  topics.json         # learned forum topic ids (name -> id)
  courses/<id>/       # the PDFs + cached .txt extractions
  exams/<id>/         # one folder per uploaded exam paper
```

Delete a course or an exam from the menu and both the index entry and its
folder go away.

## Tests

```bash
python -m unittest -v
```

Tests build tiny PDFs in memory and cover extraction, question validity
against Telegram's poll limits, the junk-filtering rules (bullets, truncated
sentences, purpose clauses, running heads), storage lifecycle, mixed-quiz
generation, note formatting, BM25 retrieval and `/ask` answers, old-exam
parsing and quiz mixing, forum-topic routing, the wake-word trigger, the
24-hour time input, the daily scheduling rules and the auto quiz / auto note
menu flows.

## Project layout

```
bot.py        # menu + inline-button state machine, sends polls/notes, /ask, runs the daily schedules
generator.py  # PDF text extraction + rule-based questions, notes, BM25 retrieval and /ask
storage.py    # course CRUD (JSON index + per-course folders) + automation settings
tests/        # unit tests (self-contained, no sample files needed)
```
