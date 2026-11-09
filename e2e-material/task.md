Our {{SOC_NAME}} Linux kernel lives in the SDK `{{SDK}}`.

Add a kernel log message containing exactly the string `{{MARKER}}` that is printed
on every boot (put it somewhere that always runs during boot). Build the kernel,
boot it on a free test board that can run `{{SDK}}`, and prove from the board's serial
console, with `dmesg | grep {{MARKER}}`, that the message is printed.

Use the task name `{{TASK}}` and holder name `e2e-agent`. Commit your change on the
task branch. When you are done (also if something fails), release everything you leased.

Finish your answer with a line `RESULT: <the exact dmesg line you saw>` (or
`RESULT: FAILED <reason>`).
