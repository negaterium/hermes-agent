# Cron and Gateway Rules

Cron and gateway work can persist beyond the current chat. Bias toward explicit scope and conservative verification.

## Cron

- Cron jobs run in fresh sessions without current chat context.
- Prompts must be self-contained.
- Cron pre-run scripts must be Python files.
- In this environment cron pre-run scripts should finish under the platform timeout.
- Do not schedule recursive cron jobs.
- Use report-only automation for cleanup or drift detection unless the user explicitly approves mutation.

## Gateway

- Ask before restarting the Hermes container when the active session may be killed.
- Do not trust a single status source when debugging gateway health.
- Check live process state and logs before claiming the gateway is down.
- Keep platform-specific delivery behavior documented with the code that changes it.

## Safety

Autonomous execution needs narrower permissions than interactive work. If a job can delete, rewrite, restart, or sync user data, require explicit scope and a rollback path.
