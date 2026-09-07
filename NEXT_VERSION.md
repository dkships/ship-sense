# What comes after v3.6

v3.6 restored Honesty to the primary score on a 66-case bank with a measured, deterministic grader. Two things remain open, in priority order: a paraphrase-robust Honesty grader, and a v4 that adds tasks without breaking the roster. Three briefs also carry wording issues that wait for a fresh-answer version.

## 1. Honesty grader: the entailment candidate was tested and failed

**Problem.** Alias matching under-credits landmines: on the reviewer-labelled sample the matcher was stricter than the reviewer in 88 of 116 disagreements. Generative LLM judges are ruled out — reviewers from scored labs passed their own lab's answers 6–13 points more often than others', and two of them were unstable on identical inputs. Sentence-embedding similarity is ruled out because bi-encoders score a sentence and its negation as near-identical.

**What was tried.** A local, open-weights NLI cross-encoder (MiniCheck-DeBERTa-v3-Large; MiniCheck-RoBERTa-Large as a second reading; both 0.4B parameters, offline, belonging to no scored lab) deciding whether any model statement entails each landmine or false claim. Validation followed a gate written before the run: the 320 reviewer-labelled checks were split by case with a fixed seed into ten calibration cases, where the two thresholds were chosen, and ten frozen hold-out cases.

**Result (2026-09-07).** On the hold-out, agreement with the two-reviewer consensus: alias grader 0.897 (landmines 0.854); checker on terse key descriptions 0.713 (landmines 0.524); checker on the keys' full-sentence example statements 0.868 (landmines 0.817). A union of the two graders gains three landmine calls out of 82 and lowers κ. The checker fails the gate, and the alias grader stays. The full result is in [METHODOLOGY.md](METHODOLOGY.md#grader-validity).

**What would reopen it.** Two things, not one: human labels rather than LLM-reviewer labels on a sample balanced between hits and misses (90% of the reviewed checks are passes, which is why κ is low for every grader and why agreement relative to the alias grader is now the yardstick), and hypotheses written as several concrete statements per check rather than one rubric sentence. Until then, the honest description of Honesty is the one in METHODOLOGY: deterministic, conservative on recall, measured.

## 2. Wording review queue

Three briefs carry context errors that no check depends on and that penalise no correct answer, so they stay in the bank and are corrected only in a version that collects fresh answers (a changed prompt cannot reuse saved ones): an inverted ratio label in case C2; "roughly tripled" for a 1.78× list growth in case H3; and, in case R2, a "candidates had scored similarly" premise not found in either source document plus a support-load figure taken from a later year than the decision. The rulings are in [CORRECTIONS.md](CORRECTIONS.md).

## 3. v4: more tasks, same roster

**Roster.** Every model on the board, every lab. Anthropic, OpenAI and Google run through native batch; xAI, Meta, Moonshot, Qwen, DeepSeek and Z.ai have no usable batch route and run live at the same shipped defaults, gated by `notes/gate_run.py`, as every published board has. A batch-only roster would drop six of nine labs including the current #1 model, and is not a Ship Sense board.

**Output cap.** 8,192 tokens as the shared default, raised per model only where a sizing probe on the longest-answering items or a vendor-documented floor requires it, exactly as v3.x. A 4,096-token cap including reasoning is not usable: on the current bank Claude Fable 5.1 peaked at 3,953 tokens and Gemini 3.8 Flash hit the 8,192 cap once. Truncation inflates Conviction and the gate refuses it.

**New Honesty items.** The Ship Sense Honesty construct is spontaneous flagging: given a brief and an analyst's flawed claim, does the model raise the right limitations unprompted and avoid asserting the unsupported ones? New items keep that form — a brief, a flawed claim, landmines and false alarms grounded in a real decision the author made — and are graded by the grader in force. A second, structured turn may be appended to each Honesty item in v4 (closed-form "does the supplied evidence establish X: yes / no / cannot determine", with a paired contrast whose correct answer flips), graded by exact match and reported as a separate recognition-style Honesty measure alongside the free-text one. That two-stage design gets a grader-free Honesty number without giving up the spontaneous measure; it needs fresh answers from every model and is why it waits for v4.

### The six v4 workflow drafts

Six structured "workflow" tasks were drafted in September 2026 (revenue reconciliation, launch scorecard, agent adoption metrics, feedback triage, account restrictions, acquisition forecast), each with a synthetic contrast, 72 answer fields in all. A three-provider reference panel qualified 28 of 72 reviews; subject collection never started. Assessment for v4:

- **Not Ship Sense tasks as drafted.** They are clerical-accuracy quizzes — sum four numbers, count calendar days, compute a percentage to two decimals, answer leading yes/no questions about what a table establishes. They test carefulness with supplied data, which is real and worth something, but not a decision about what to build, hold or kill, and their "honesty" fields are recognition items with the answer named in the question.
- **Not the author's decisions.** Each is a task-authored questionnaire around facts drawn from the author's work ("not a verbatim original user request", by its own provenance note), so it does not meet the provenance bar that every scored item is a decision the author actually made and recorded.
- **Ambiguous by their own test.** Even the reference panel disagreed with the drafted key on 30 of 72 reviews and cited inputs outside the allowed lists on 28.
- **Good raw material.** The underlying situations are real Ship Sense cases waiting to be written in the Ship Sense form: the proxy-versus-billing MRR discrepancy (an analyst claiming the whole gap is "database error"), the launch scorecard measured on the wrong window and population, the adoption dashboard with no read receipts, the feedback board where duplicate deletion destroys votes and a bug is misfiled as a duplicate, and the sponsorship offers with an unknown click denominator. Each has an analyst's overclaim to audit and a real decision behind it. They should be re-authored as free-text Honesty and Restraint items with landmines and false alarms, grounded in the same source evidence, and they enter the bank on the same terms as every other item.

**Cost and gates.** Fresh answers for 31 models on a handful of new items cost on the order of a few dollars per lab at current list prices; the shared $90 submission ceiling and $100 absolute cap on grader-validation spend set in September remain. Every run is gated on complete coverage, zero unsalvaged truncations and non-zero cost before it merges, and publication needs the privacy scan and a browser check on desktop and mobile.

## Reproduce the checks

The synthetic example runs without provider credentials:

```sh
python -m src.task_score --task examples/workflow_task.json --answer examples/workflow_answer.json
```

The historical v3.5 Decision score and its reproduction script are preserved under `docs/history/v3.5/` (`python -m src.decision_scores --output /tmp/decision-scores.json` reproduces the archived file from the archived public inputs).
