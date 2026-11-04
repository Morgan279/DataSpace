# Feasibility decision: INCONCLUSIVE

**No conclusion can yet be drawn about complementary execution mechanisms on
DataSpace.** No public-reference task has been selected, run, or scored. The
current policy denies the official Hugging Face host with HTTP 403, and no
released tasks, gold files, or frozen configs are present in this checkout.
The configuration draft requests `huggingface.co`, but the running instance
still reports no custom allowed hosts. A fresh request after the handoff again
returned HTTP 403. Applying and publishing the network change to the running
environment is necessary before the dataset can be inspected. The official
GitHub repository has the same HEAD as this checkout; its releases page did
not expose an alternative dataset archive.

The upstream checkout is `Morgan279/DataSpace` at
`6491caa4c70cc06cacb6103ba73cefb00746abfe`. Its README describes 410 tasks,
60 public-reference answers and configs, and a separately distributed ZIP.
It describes both code and dataset as MIT licensed; the dataset's hosted card
could not be independently inspected while the host is blocked. The official
evaluator was kept unchanged: its existing 18 tests passed.

The credential-free setup has Pandas 2.3.3 and DuckDB 1.4.2. The isolated
runner, provenance checks, unchanged evaluator invocation, and range reader
passed twelve research tests. A generated synthetic CSV fixture was run three
times per strategy: all six predictions passed its synthetic gold under the
official evaluator. [Machine-readable results](results/synthetic.json) include
timing, CPU, memory, validation overhead and hashes. These toy measurements do
not estimate DataSpace accuracy, strategy dominance, complementarity, or
headroom; neither an LLM nor an adaptive agent was evaluated.

The archive tooling can attempt to index and extract only selected members
using HTTP byte ranges, and checks the extracted members. Its range behavior
was tested locally, not against the blocked official host. The need for a full
download to obtain task data cannot be determined until the host responds.
Verifying the publisher's SHA-256 for the full ZIP necessarily requires the
full ZIP; a partial download cannot establish that verification. See the
[reproduction and next-step commands](README.md).

## What remains to establish P1

The smallest informative next test is to make the official data reachable,
inspect the 60 reference-bearing tasks, and freeze a 10–20-task cohort before
scoring. Select across input formats, operations, and data sizes, including
plausibly difficult or non-SQL-friendly cases; retain failures and record the
selection and exclusion reasons. The current archive index can screen file
types and estimate selected bytes, but its host compatibility is unverified.
No official task-specific implementations or selection manifest exist yet.

Construct paired task solutions from questions and inputs without using the
test references. Freeze the implementations and a simple, input-feature-based
router before scoring held-out tasks. Compare fixed Pandas and DuckDB, the
predefined router, and a retrospective per-task oracle on the same tasks and
versions. Add fixed Python or hybrid execution only where they represent a
usefully distinct mechanism. Report unknown or unsupported cases as such,
include correctness and all measured costs, and keep incorrect fast answers
out of performance wins. The oracle is an upper bound, not an implementable
router or evidence that a transformation can be learned.

The existing harness measures execution, import/setup, process CPU and peak
RSS, and evaluator overhead. It does not measure the human or model time to
interpret tasks, author solutions, choose a mechanism, or perform retries;
those end-to-end costs must be recorded separately in a real study. Three
repetitions can expose noise but cannot establish statistical significance on
their own. Where development answers inform changes, keep those tasks separate
from held-out test tasks and freeze the rules before final evaluation.

## Implication for P2

Do not start a learned-transformation experiment until P1 demonstrates
credible complementary benefit beyond the strongest fixed strategy after
overhead. There are currently no data on which to choose a transformation,
train a router, or test generalization. The strongest alternative explanation
for any future apparent gain is task-specific implementation or selection
bias, including hand-written solutions, cold-start costs, and cache or
measurement noise, rather than a reusable computational transformation. A
small cohort would still be exploratory even if an oracle gap emerged.
