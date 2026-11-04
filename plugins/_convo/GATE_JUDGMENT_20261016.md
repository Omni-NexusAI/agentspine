# Speech-gate judgment: categories, wording, clarification, late connection (2026-10-16)

Status: implemented and measured **offline** on synthetic speech with one weak decision model (d1-omni-600M). Nothing here is
live-microphone, listening or physical acceptance, and nothing was deployed. Every number is a share of readings of the model,
not of real conversations.

## Method

- New labelled corpus, `.runtime/convo-live-test/sentences_j.json` (+ `corpus_j/`, two Windows SAPI voices, 16 kHz; built by
  `make_judgment_corpus.py`): 228 sentences in 11 categories (task requests, requests that start with the assistant's name, off-topic
  requests, "what do you think?" after a passing remark, vague fragments, background media, a second person talking to a third,
  self-talk, unfinished thoughts, acknowledgements, a late question aimed at someone else). Split `cal` / `test` by index; **`test`
  was not read until the wording and floors were chosen** (it was then read for the final comparison and for the late-connection
  line). Floors were fitted on the earlier 225-sentence corpus (three voices, with the endpoint's trailing quiet) and only then checked.
- Scripts (all read-only against the model): `j_measure.py`, `j_report.py`, `j_compare.py`, `j_matched.py`, `j_pair.py`,
  `j_cost.py`, `j_costpair.py`, `j_final.py`. Intervals are sentence-level bootstraps (the two voices of one sentence are one sample).
  Cost = 2 x unanswered requests + answered non-requests (the convention the first floors used).
- About 40 wordings and state variants were measured. Comparing them at one fixed floor is misleading (a wording that moves every
  probability looks better or worse); they were compared at a matched operating point and by floor-free AUROC.

## What was found

1. **Each criterion description is read only up to about 23 tokens.** A different tail changed the answer by 0.0 (to 1e-16); a trimmed
   one changed it a lot (the cut falls mid-clause). The shipped side cue's "or is a recording or broadcast" and the addressed cue's
   "it does not name or speak to another person" never reached the model. Cues must put the decisive words first.
2. **Requests that begin with the assistant's name were the worst-read class, not off-topic requests.** The side-conversation cue
   said the speech "names ... someone"; "Jarvis, what is the capital of Norway?" was read as talk to someone else (54-58% answered at
   the old floor, against 100% for plain requests; mean P(side) .63). Off-topic requests ("random question, do octopuses really have
   three hearts?") were answered 92-100%; the few misses carried a meta-preface ("completely off topic", "nothing to do with work").
   Removing "names or" from that cue (nothing else changed) is the one wording change that held up: named requests 54% -> 96%
   (test), overall unanswered requests down on all three sets.
3. **Nothing else resolvable.** A media class, a media-specific cue, extra yes/no questions ("is this a recording?"), a combined
   score with the explicit-request probability, instruction rewrites and three context lines either did nothing outside noise or
   helped one corpus and hurt the other. Background media and a second person talking are still answered a quarter to two thirds
   of the time; "unfinished" about a third to a half.

## Shipped change and before/after

Side cue `Speech to another person: it speaks to someone, talks about their things, is part of a phone call`; floors refitted
(`respond_min` .15 -> .30, `ambient_respond_min` .75 -> .90, `interrupt_min` .40 -> .60). Share of clips **answered** (requests:
higher is better; others: lower is better), 95% interval in brackets. Old = old wording at .15, new = new wording at .30.

Held-out `test` split (114 sentences x 2 voices, with trailing quiet):

| category | old | new |
|---|---|---|
| task requests | 100 | 100 |
| requests naming the assistant | 54 [33-79] | 96 [88-100] |
| off-topic requests | 100 | 100 |
| "what do you think?" after a remark | 100 | 83 [50-100] |
| background media | 33 | 29 |
| second person talking to a third | 29 | 25 |
| self-talk | 38 | 25 |
| unfinished thoughts | 58 | 38 |
| acknowledgements | 12 | 0 |
| **all requests unanswered** | 13.1 [5-24] | 3.6 [0-10] |
| **all non-requests answered** | 34.1 [24-44] | 25.0 [15-34] |

`cal` split (tuning only): requests unanswered 16.7 -> 10.7; non-requests answered 34.1 -> 37.1 (named 58 -> 79, media 67 -> 67,
second person 33 -> 42). Earlier 225-sentence corpus (where the floors were fitted, so optimistic): requests unanswered 19.5 -> 14.6,
non-requests answered 29.0 -> 24.6. Paired test-split difference: cost -0.28 [-0.48, -0.08]; on `cal` -0.09 [-0.25, +0.04].
Not tested: probes for `interrupt_min` / `ambient_respond_min` on the new corpus (fitted on the older one only: 4.3% and 4.4%
of non-requests respectively).

**Saved settings keep their old values.** A config saved with the previous defaults (.15 / .75 / .40) under the new wording
answers 48% of non-requests; move saved `policy_gate` values to the new defaults when deploying.

## Clarification

The previous rule asked whenever "unclear" led "addressed" and the mass reached the floor. Over 4,896 readings of the 204-clip
set under 24 wordings it asked 31 times: 11 about acknowledgements, 10 side remarks, 8 unfinished thoughts, 2 requests. Now it asks only when
P(unclear) >= `clarify_min` (.30) **and** acknowledgement + other-people + unfinished mass is below `clarify_elsewhere_max` (.50),
once per ambiguity episode, then `clarification_cooldown` (60 s, kept: stricter than the 45 s first proposed). New rule: 2 of
4,896 (both requests), 0 of 1,131 readings of the shipped wording. The model's P(unclear) was >= .30 in 4 of 30,795 readings of all
wordings tried and never on vague speech, so with this model the question is dormant. "Addressed with a low explicit-request
probability" was rejected as a trigger: that probability is unstable across corpora (it is lowest for plain questions like
"do octopuses really have three hearts?").

## Late connection

A line about the previous classified turn ("The previous speech was ignored as a passing remark.") was measured against the bare
follow-up: 83% answered with and without it (cal), 92% -> 83% (test), while talk to someone else rose 0% -> 33% (cal). Under the old
wording it improved overall discrimination (AUROC +.04 to +.07 on three sets) only by masking the name defect above; with the shipped
wording it gains nothing resolvable. The state sentence is unchanged; connecting "what do you think?" to a recent remark has to be
done by the main model with the recent ignored speech attached to the next admitted turn.

## What still fails, and the recommendation

The decision model cannot reliably tell a television announcement, a second speaker or a trailing thought from a request (a third to
two thirds answered), reads cue wording with large, unstable effects (a one-clause trim moved mean probabilities by 0.10), and
almost never says "unclear". Wording and floors are tuned about as far as 114 sentences allow. Re-measure with the same scripts
(`j_measure.py` / `j_final.py` / `tests/calibrate_gate.py`) on a stronger audio decision model or real consented recordings before
spending more effort on cues; a larger or better-instructed model is the lever that can still move these numbers. None was loaded
or downloaded here.
