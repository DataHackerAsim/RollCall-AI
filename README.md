<div align="center">

# Pro Attend AI

### Real-time classroom attendance, powered by face recognition.

Point a camera at your classroom. The system detects every face, matches each one against a
per-student reference embedding, and writes a verifiable, timestamped attendance record —
no roll-call, no sign-in sheet, no trust issues.

<p>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="Django" src="https://img.shields.io/badge/django-5.1-092E20?logo=django&logoColor=white">
  <img alt="InsightFace" src="https://img.shields.io/badge/InsightFace-ArcFace%20512d-ff6f00">
  <img alt="IEEE ICIT 2026" src="https://img.shields.io/badge/IEEE%20ICIT%202026-Accepted-00629B?logo=ieee&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="PRs welcome" src="https://img.shields.io/badge/PRs-welcome-brightgreen">
</p>

<sub>RetinaFace detection · ArcFace embeddings · dual-path extraction · multi-frame fusion · live RTSP · Django 5 · MIT licensed</sub>

</div>

---

> **Research:** This system is the subject of an accepted paper at **IEEE ICIT 2026**:
> *"AI-Powered Facial Recognition Attendance System"* — Asim Ahmed, Shawaiz Zafar et al.
> Publication details will be linked here upon proceedings release.

---

## Why this exists

Manual roll-call is slow, forgeable, and loses hours of instructional time every week.
Commercial attendance systems are closed-source, institution-priced, and ship biometric data
to third-party servers.

**Pro Attend AI runs entirely on your own machine.** Biometrics never leave the server you
installed it on. The source is here; you can read every line.

---

## Screenshots

![Instructor dashboard — live detection view on the left, detection control in the middle,
and an auto-updating student roster on the right](docs/screenshots/dashboard.png)

*The instructor dashboard during a session. Live detection view (left) ingests from the
classroom camera; recognised students flip to **Present** in the roster (right);
unrecognised faces drop into the **Unidentified Faces** row at the bottom, ready to be
dragged onto a student card to retrain that student's reference embedding. Faces are
obscured and the roster shown here is demo data — real deployments show real names.*

![Processing modal — face-count and identified-student counters tick up in real time as
frames are analysed](docs/screenshots/detection-running.png)

*The processing modal that appears while detection is running. Faces-detected and
students-identified counters update live as frames are processed across the multi-frame
fusion window, giving immediate feedback to the operator instead of a frozen spinner.*

---

## Features

**Recognition pipeline**
- **RetinaFace** detection → **ArcFace 512-d** embeddings (InsightFace `buffalo_l`)
- **Dual-path extraction** — every face is embedded twice: once from a tight crop with
  brightness normalisation, once from a margin crop with CLAHE. The higher cosine
  similarity wins. This reliably recovers matches that single-pass pipelines miss on real
  classroom CCTV footage.
- **Multi-frame fusion** — recognition is the consensus across N consecutive frames
  (default 5), so blinks, motion blur, and brief head turns don't cost anyone their
  attendance mark.
- **Thread-safe embedding store** with atomic reloads.

**Deployment reality**
- **Live RTSP streams** with a browser-friendly MJPEG bridge.
- **Background model warm-up** — the first recognition call doesn't pay the 8–15 second
  cold-start cost; InsightFace loads on Django boot in a daemon thread.
- **Per-day classroom overrides** — swap rooms for a day without editing a schedule.
- **CPU or GPU** — flip one argument in `views.py` to use `CUDAExecutionProvider`.

**Operator workflow**
- **Face Manager UI** — capture frames → auto-cluster unknown faces into identities →
  drag-and-drop onto student cards → automatic retraining. No notebook, no CLI, no
  touching model weights.
- **CSV roster import** with auto-created batches and sections.
- **CSV export** — attendance matrix, per-session stats, optional detailed time log, all
  timezone-aware.

**Engineering**
- Configuration via environment variables (`.env.example` documents every knob).
- Production-hardening (HSTS, secure cookies, SSL redirect) auto-engages when
  `DEBUG=False`.
- Real test suite (~25 cases) covering model invariants, pure helpers, auth guards, and
  regressions.

---

## Architecture
