# Multi-Tenant Architectures with Amazon SageMaker HyperPod

Reference architectures and guidance for building multi-tenant environments on Amazon SageMaker HyperPod. This repository covers two orchestration options:

- **HyperPod with EKS** — Uses Kubernetes namespaces, EKS access entries, and HyperPod Task Governance for workload isolation and fair resource allocation across teams.
- **HyperPod with Slurm** — Uses Slurm partitions, Linux user/group permissions, and the `AllowGroups` directive to enforce team-level isolation on shared GPU clusters.

Both architectures leverage AWS IAM Identity Center for centralized authentication (federated with external identity providers such as Microsoft Entra ID), per-team SageMaker Domains for a tailored user experience, and shared high-performance storage (FSx for Lustre or OpenZFS) with per-team and per-user directory isolation.

## Repository Contents

```
multi-tenant-hyperpod-eks.md   # Full walkthrough for the EKS-based architecture
multi-tenant-hyperpod-slurm.md # Full walkthrough for the Slurm-based architecture
diagrams/                           # Draw.io source files for architecture diagrams
images/                             # Architecture diagram images
scripts/                            # Helper scripts for user/group provisioning
```

## Scripts

- **create_users_sm_domain.py** — Creates Linux users and groups on a HyperPod cluster based on a SageMaker Domain's user profile configuration (UIDs, GIDs, group mappings).
- **run_create_users.sh** — Wrapper to run the user creation script across multiple domains.

## License

This project is licensed under the MIT License.
