---
name: hello
description: Run the end-to-end demo ("hello") that builds default fluid, geo and well models, waits for human approval, then builds and runs a demo OPM deck. Use when the user says hello, asks to test the install, or asks for a demo run.
---

# Hello

Run `opmagent hello` and read its stage lines aloud briefly.

1. **First run** builds `HELLO_FLUID` (NeqSim), `HELLO_GEO` and `HELLO_WELLS` as demo models and submits
   them for review. It stops at "waiting for your approval". Tell the user the three models are ready and
   how to approve: say "approve hello" in their own client (reviewer tools), or run
   `opmagent review approve-hello` in a terminal. **You cannot and must not approve them.**
2. **After approval**, run `opmagent hello` again. It builds the HELLO deck, parses it with OPM and runs it
   for 5 years (flow binary if installed, else the opm.simulators Python bindings).
3. Report pass/fail per stage. A failure points to the part of the install to fix: Java/NeqSim for the
   fluid model, OPM (`pip install opm opm-simulators` or the OPM packages) for the deck and run.
4. "Clean up hello" → confirm first, then `opmagent hello --clean` (removes demo drafts; approved demo
   models stay, flagged as demo).
