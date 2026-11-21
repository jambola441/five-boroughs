# Who decides what (round graphs)

The lead decides only what changes the game's direction or risk. Everything else is decided by the owner of
that stream, or by a standing policy that the graph applies in code and reports.

## The lead
- Budgets of exploration rounds, and any gate made LOOSER (a threshold raised, a class excluded, a definition
  narrowed).
- Ship / no-ship, and anything that touches live or uploads.
- Debt that is not covered by a policy below, and every debt on a round that would ship.
- Approving a round at the lead gate (`approval.json`).
- Disputes between streams.

## A stream coordinator (page coordinator for page rounds)
- Designs the stream's rounds with its engineer (the page editor) and hands the pipeline engineer ONE finished
  round file: steps, switches, gates, frames.
- Makes gates STRICTER or clearer, picks frame sites, accepts the engineer's measurement fixes (they are recorded,
  never applied to a running round).
- Escalates to the lead only what the lead section lists, as one question with its evidence.
- Doesn't approve rounds and doesn't write code.

## The owner of a check (terrain study, drive test, macmini, page editor)
- Writes reasons for its own check's rows (reasons files). Every reason is listed in the gate output; the lead
  sees the list at the lead gate.
- Diagnoses its own failures.

## Standing policies (applied by the graph, reported, no one asked)
1. **Baselines**: a terrain round's gate (b) baseline is the previous round's own measured deck-gap totals,
   when that round's QA output exists. Nobody has to correct a baseline by hand.
2. **Small growth at an existing site**: new tents (tent_diff) of at most 2 triangles and 1 m² in total, all in
   tiles that already had tents in the base, go on the round's debt list automatically, with owner "page: tents
   class" and pay-back "with the tents class fix". More than that, or any tent in a tile with none before, fails.
3. **Retries**: a step that ran out of Irrigator's time while its job kept running is retried automatically;
   a check whose owner changed its reasons file is re-run automatically.
4. **Hand-offs**: frames (`READY`) and approvals (`approval.json`) are picked up automatically when they appear.

## Frozen per round
A round's code (code_pin) and its round file (gates, budgets, switches) are frozen when it starts. A change
goes to the next round. Two exceptions, both recorded in the run's round.json under "changes": the lead's own
decisions on that round's verdict (a debt entry, a baseline correction), and policy entries the graph adds.
The lead may always stop a round.
