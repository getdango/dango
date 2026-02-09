# ADR-005: DigitalOcean Only for v1

## Status
Accepted

## Context
Dango needs a cloud deployment target for `dango deploy`. Users should be able to provision a server, install Dango, and have a running instance with a single command. Supporting multiple cloud providers increases development scope, testing surface, and documentation burden.

## Decision
Support only DigitalOcean for cloud deployment in Dango v1. Default to the `s-2vcpu-4gb` droplet size ($24/mo). Pin Ubuntu 22.04 LTS as the base image. Install Docker via `get.docker.com` during provisioning.

## Rationale
VAL-002 validated DigitalOcean provisioning end-to-end:

- **Fast provisioning:** Total time from API call to running Docker is ~114 seconds. Droplet creation takes 34s, SSH is available after a 30s wait, and Docker installs in 42s via the official `get.docker.com` script. Under 2 minutes total — no need for pre-built snapshots.
- **Simple API:** REST API with bearer token authentication. Straightforward CRUD for droplets, SSH keys, and firewalls. No complex IAM, VPC, or region configuration required.
- **Right-sized for small teams:** The `s-2vcpu-4gb` tier ($24/mo) provides 2 vCPUs, 4GB RAM, and 80GB disk — sufficient for DuckDB, Metabase, dbt, and the Dango server. A budget option (`s-1vcpu-2gb`, $12/mo) is available with a performance warning.
- **Predictable pricing:** Fixed monthly pricing with no surprise charges for bandwidth, API calls, or storage IOPS. Small teams can budget accurately.

Perfecting one provider's experience (provisioning, monitoring, teardown) produces a better product than partially supporting three providers.

## Alternatives Considered
- **AWS (EC2):** More powerful and flexible, but significantly more complex. IAM roles, VPC configuration, security groups, and the EC2 API are designed for infrastructure teams, not small data teams. Pricing is harder to predict (per-second billing, data transfer charges, EBS costs). The learning curve conflicts with Dango's "simple for small teams" goal.
- **Multi-cloud (DO + AWS + GCP):** Maximum reach but multiplies development and testing effort. Each provider has different APIs, CLI tools, networking models, and pricing structures. Provider-specific bugs would be harder to reproduce and fix. Better to excel on one platform than be mediocre on three.
- **Self-hosted only (no cloud integration):** Lower development effort but removes a key value proposition. Many small teams want deployment to be a single command, not a manual server setup guide.

## Consequences
- Users on AWS, GCP, or Azure must self-host (manual Docker setup) or wait for provider support in a future version. The Docker-based deployment model means self-hosting is straightforward — the missing piece is only the automated provisioning.
- Vendor lock-in to DigitalOcean's API for the provisioning layer. The application itself runs in Docker and is provider-agnostic — only the `dango deploy` command is DO-specific.
- DigitalOcean has fewer regions than AWS/GCP. Users needing specific geographic regions (e.g., mainland China, South America) may not find a nearby datacenter.
- The `get.docker.com` install script adds ~42s to provisioning. This is acceptable given the total time is under 2 minutes. Pinning Ubuntu 22.04 LTS avoids compatibility issues with newer OS releases.
