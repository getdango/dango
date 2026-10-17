# Security Policy

## Supported versions

Security fixes are released for the latest minor version of Dango (currently 1.0.x). Please upgrade
to the latest release (`pip install --upgrade getdango`) before reporting, to check the issue still applies.

## Reporting a vulnerability

Please do not open a public issue, pull request or discussion for a vulnerability.

Use GitHub private vulnerability reporting: on the
[getdango/dango](https://github.com/getdango/dango) repository, go to **Security** and choose
**Report a vulnerability**.

Please include:

- the affected Dango version and how it was installed (local, or a cloud server created with `dango deploy`)
- a description of the issue and its impact
- steps to reproduce, or a minimal proof of concept
- whether you want to be credited in the fix notes

## What to expect

- We aim to acknowledge a report within 7 days.
- We will confirm whether the report is accepted, keep you updated on progress, and coordinate a
  disclosure date with you once a fix is available.

## Scope

In scope: the Dango package (CLI, web application, API, cloud deployment tooling) and its default configuration.

Out of scope: vulnerabilities in third-party data sources, services or dependencies that are not caused
by how Dango uses them (report those to the upstream project), and attacks that require prior control of
the machine Dango runs on or of an admin account.

## Known design limits

Dango is a single-team tool. Users with the Editor role can run notebooks and scripts, and those run as the
operating-system user that runs the Dango server. Grant that role only to people you would trust with shell
access to that server. See the documentation at <https://docs.getdango.dev> for details.
