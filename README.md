# Arb Watchtower engine kit

The small program Arb Watchtower uses to drive [auto-rsa](https://github.com/NelsonDane/auto-rsa)
(by Nelson Dane, GPL-3.0). It runs auto-rsa as a separate process, turns its prompts and
output into structured events, and repairs a few brokerage logins at runtime without
changing auto-rsa's files.

- `runner/run.py`: one job, one process. Reads a job as JSON on stdin, writes events as JSON lines.
- `runner/sofi_patch.py`: SoFi login, verification-code page and cookie fixes.
- `runner/paper.py`: a built-in paper account for trying things out.

Licensed under the GNU General Public License v3.0 (see LICENSE). You may use, change and
share it under those terms. The Arb Watchtower dashboard that talks to this kit over
stdin/stdout is a separate program and is not part of this repository.
