# Loom video script (target 3:30, hard cap 4:00)

The script has five parts: a checklist, a pre-roll to start before recording, a timeline, the narration for each scene, and fallbacks.

Narration lines are written to be read aloud. Paraphrase them freely; do not read them word for word.
Lines marked **[YOU]** must be filled in by you with what is true for you. Do not ship them as written.

---

## 0. Before you record (5 minutes)

**Machine**
- Plug the laptop in and close heavy apps (browsers with many tabs, Docker, games).
- Set the display to not sleep for 30 minutes. A sleep mid-demo freezes the load. This happened once during benchmarking.
- Use a 1440×900 or 1920×1080 screen and a browser zoom of about 90%, so a whole model panel fits on screen.

**Tabs to open in this order** (switch between them with Ctrl+Tab):
1. Dashboard: http://127.0.0.1:5173
2. `README.md`, rendered (VS Code: Ctrl+Shift+V). Scroll to the component table.
3. `docs/BENCHMARK.md`, rendered. Scroll to section 3.
4. `reports/s2/rpm.svg`, opened in the browser (Scenario 2 limits vs throughput).
5. Mock receiver: http://127.0.0.1:9100/received
6. `docs/prd.md`, rendered (for the AI section).

**Terminals:** two PowerShell windows, both in `E:\Assignments\ConsumaAssignment\backend`.

---

## 1. Pre-roll: start this about 60–90 s before you press record

The rolling-60-second windows take a minute to fill. Starting early means the charts already sit right at the limit when the video begins.

**Terminal 1: start the stack with the demo config** (simulator, control plane, mock receiver that rejects 2 callbacks, dashboard):

```powershell
..\.venv\Scripts\python -m tools.up --dashboard --config config\demo.yaml
```

**Terminal 2: overdrive both models at 120% for 7 minutes.**
- Model A gets 600 req/s against a 500 req/s limit; model B gets 400 req/s against 333 req/s.
- `--deadline 15` makes the excess show up as visible rejections within seconds, instead of only after 60 s of queueing.

```powershell
..\.venv\Scripts\python -m tools.loadgen --rate 1000 --duration 420 --models model-a=0.6,model-b=0.4 --tokens 1000 --token-spread 0.1 --deadline 15
```

Check the dashboard. **Connected** should be green, both blue lines should be climbing, and the accounting invariant should read **holds**. When the blue lines are near the red dashed limit, start recording.

---

## 2. Timeline at a glance

| Time | Scene | Screen | Brief requirement |
|---|---|---|---|
| 0:00–0:20 | Intro | README component table | Context |
| 0:20–1:00 | Processing under load | Dashboard | "show the system processing requests under load" |
| 1:00–1:45 | Live limit change | Dashboard, model-a panel | "change a model's limits while the system is running" |
| 1:45–2:25 | Async batch and callback | Dashboard batches + `/received` | "show an asynchronous batch and its completion callback" |
| 2:25–3:15 | Results and decisions | BENCHMARK.md + rpm.svg | "explain the measured results and your main decisions" |
| 3:15–3:45 | AI usage and your contribution | prd.md / terminal history | "briefly show how you used AI tools and what you personally contributed" |

---

## 3. Scene by scene

### Scene 1: Intro (0:00–0:20). Screen: README component table

> "Hi, I'm [YOU: name]. This is my submission for the high-throughput inference assignment. The starting point was a synchronous loop doing about 600 requests a minute against a 50,000 RPM, 100 million TPM provider. I built a Python control plane with a strict per-model rate-limit ledger, a provider simulator, an open-loop load generator, an independent auditor, and a React dashboard. Everything you'll see is running live on my laptop right now."

### Scene 2: Under load (0:20–1:00). Screen: Dashboard

Point at each element as you mention it:

1. The **model-a panel**: the blue "requests / trailing 60 s" line runs flat just under the red dashed RPM limit, and the gauge reads about 99%.
2. **sent/s vs completed/s**: a steady ~500/s for A and ~330/s for B.
3. The **Request accounting** row: submitted, succeeded, waiting, rejected, and the green **holds** pill.

> "Right now the load generator is offering 120% of capacity, about 1,000 requests a second across two models. Each model's limiter is a strict rolling 60-second window: 601 buckets of 100 milliseconds, for requests and for tokens. A request is only sent if the trailing window plus its cost stays under the limit. That's why this line sits flat at about 99% and never crosses: there's a 1% guard band. Sends are paced at a sixtieth of a second's share per bucket, so you get a flat line rather than a burst-then-stall sawtooth.
>
> The excess 20% doesn't pile up forever. At admission, each request gets a projected send slot. If that slot is past its deadline, it's rejected immediately with a retry-after. That's the rejected counter. And this invariant, submitted equals waiting plus in-flight plus succeeded, failed, expired and rejected, is checked continuously. Every request is in exactly one state."

### Scene 3: Live limit change (1:00–1:45). Screen: Dashboard, model-a panel

1. Click **"RPM → 5,000"**, then **"Apply limits live"**. Read out the message under the form: "N waiting requests expired".
2. Watch for about 15 s: the red limit drops to 5k. The blue window line stays above the new limit and *drains*, and sent/s for A drops to **0**.
3. Point at **model-b**: unchanged.
4. Type **30000** in RPM and click **Apply limits live** again. A's sent/s jumps straight back up.

> "Now I'm dropping model A from 30,000 to 5,000 RPM while traffic is flowing, with no restart. Two things happen. First, every waiting request is re-projected, and the ones that can no longer make their deadline expire immediately rather than rotting in the queue. Second, the 25,000 requests already sent in the last minute still count against the new limit. So A sends nothing until its window drains below 5,000, then paces at about 83 a second. A lowered limit can't un-send what was already sent, so the honest behavior is to pause. Model B doesn't notice at all: each model's ledger and scheduler are completely independent. … And restoring it takes effect on the next tick."

> Note: you don't need to wait the full ~50 s drain on camera. Restore once the pause is visible, or say "in the scenario run this settles at 4,932 per minute, which I'll show in a second."

### Scene 4: Async batch and callback (1:45–2:25). Screen: Dashboard batches → `/received` tab

1. In **Asynchronous batches**, set Size to **2000**, keep the callback URL `http://127.0.0.1:9100/callback`, and click **Submit batch**.
2. Point at the message: "acknowledged in N ms".
3. Watch the row go from `processing` to `completed_with_errors` and the callback pill from `pending` to `delivered (3)`. Click the row to expand the attempt table: **503, 503, 200**.
4. Switch to the `/received` tab and refresh. Show the delivered body and the 3 log entries, two with `accepted: false`.

> "This batch goes in with a callback URL. The acknowledgement comes back in milliseconds, before any processing, and the requests are scheduled in the background through the same limiters, so they share capacity with the live load. When the last request reaches a final state, the batch is sealed exactly once: an immutable summary, the results and an outbox row are written in one SQLite transaction. The callback body and the GET status endpoint are the same sealed object, so they can't disagree. There's even a SHA-256 of it in both. This mock receiver is set to reject the first two deliveries. You can see 503, 503, then 200, with exponential backoff and the batch id as the idempotency key. The outbox is durable, so a pending callback survives a restart."

### Scene 5: Results and decisions (2:25–3:15). Screen: BENCHMARK.md §3–4, then rpm.svg

Scroll slowly through the tables.

> "Here are the measured results, all generated by one-command scenario runners with PASS/FAIL checks against the brief's criteria. Scenario 1: 98% of the 50,000 RPM capacity used after warm-up, and the independent auditor saw a peak of 49,539 in any 60-second window, at 10-millisecond resolution, with zero provider 429s. All 330,000 requests were accounted for. Scenario 2 [switch to rpm.svg]: A held 4,932 a minute while constrained and returned to 29,000 after the restore, while B stayed at 100%. Scenario 3: a 282-millisecond ack for 10,000 requests, and the callback delivered on the third attempt, matching the API exactly.
>
> On throughput: 329,000 completed per second sustained, against a 300k requirement, and 1.1 million for the stretch, with every request id checked to be finalized exactly once. The important caveat: those two numbers are in-process. Python over HTTP does about 1,100 requests a second per process, which is plenty for the 50k-RPM scenarios but not 300k. So the benchmark runs the same ledger rules and the same simulator model on NumPy frames across worker processes, with per-worker capacity leases. Every number in the report is labelled with its transport.
>
> Three decisions I'd highlight. A strict ring instead of a token bucket, because a bucket's burst can break a rolling window. Admission-time deadlines as the overload policy, so queues stay bounded. And an auditor that never imports the limiter, so compliance isn't self-graded. Scaling to 10 billion a minute is about 167 million a second. It's projected in the report: the same unit repeated with hierarchical capacity leases. The bottleneck at that scale is provider I/O, not the scheduling logic."

### Scene 6: AI tools and your contribution (3:15–3:45). Screen: `docs/prd.md` header, then the terminal or Claude Code session

> "I built this with Claude Code. I used it in three stages. First, an ideation pass: five parallel agents explored approaches, a separate verifier checked their claims, and it caught for example that refunding over-reserved tokens into the current bucket would allow overshoot. Then a brainstorm that produced this PRD. Then implementation and test runs. Running everything for real surfaced bugs the AI had introduced: a float32 precision bug that zeroed the benchmark, a SQLite autocommit that stalled callbacks by four seconds, and an auditor indexing bug. Each was fixed and re-verified."

> **[YOU]** Say in one or two sentences what *you* decided, checked or changed. Only say what is true. Examples of the kind of thing to say:
> - "I chose Python and React and set the scope: no limit inference from 429s, no webhook signing."
> - "I reviewed the rate-limit semantics against the brief, especially how a limit drop should be audited."
> - "I re-ran the scenarios myself and checked the reports."
> - Anything you changed, rejected or questioned in the AI's output.

> "Thanks for watching. Everything is reproducible from the README."

---

## 4. Fallbacks during recording

| Problem | Fix |
|---|---|
| Dashboard shows **offline** | The control plane isn't up. Restart Terminal 1 and wait 5 s. |
| Charts are empty right after start | Normal: the first point appears after 1 s and the window takes 60 s to fill. That's why the pre-roll exists. |
| Batch stays `processing` for a long time | The load backlog is large. Use a smaller batch (500), or stop the load (Ctrl+C in Terminal 2) a few seconds before submitting. |
| Callback shows `delivered (1)` instead of `(3)` | The receiver was already used for that batch id, or was started with `--reject-first 0`. Restart `tools.up`. |
| Over time | Cut Scene 3's wait, restoring right after the pause appears, and shorten Scene 5's Scenario 2 sentence. |
| Ports already in use (8000/9000/9100/5173) | Something from a previous run is still alive. Close the old terminal, or end the `python`/`node` processes in Task Manager. |

## 5. After recording

- Trim dead air at the start and end in Loom.
- Add the Loom link to the top of `README.md`.
- Stop everything with Ctrl+C in both terminals.
