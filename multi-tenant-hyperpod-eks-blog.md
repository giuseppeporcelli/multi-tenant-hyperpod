# Multi-Tenant Architectures with Amazon SageMaker HyperPod EKS

## Introduction

Amazon SageMaker HyperPod is a purpose-built AI service that simplifies the management of large-scale compute clusters for generative AI (gen AI) workloads. It provides resilient, optimized clusters with Kubernetes (Amazon EKS) or Slurm orchestration, enabling organizations to run distributed training, interactive development, and model inference at scale — while automatically handling node health monitoring, fault recovery, and cluster lifecycle management.

As organizations scale their gen AI operations, a common challenge emerges: multiple teams within the same company need shared access to expensive GPU clusters while maintaining security boundaries, resource fairness, and operational independence. A data science team training large language models, a computer vision group running inference workloads, and a research team experimenting with new architectures — all may need access to the same HyperPod cluster. Without a well-designed multi-tenant architecture, organizations face uncontrolled resource consumption, weak isolation between teams, and administrative overhead that slows down innovation.

In this post, we present a reference architecture for building a multi-tenant environment on Amazon SageMaker HyperPod with EKS. This architecture leverages AWS Identity Center for centralized authentication, per-team SageMaker Domains for a tailored user experience, Kubernetes namespaces for workload isolation, and HyperPod Task Governance for fair resource allocation. By the end of this guide, you will have a clear blueprint for enabling multiple teams to securely and efficiently share a single HyperPod EKS cluster.

## Architecture Overview

The following diagram illustrates the high-level architecture of a multi-tenant HyperPod EKS deployment. In this example, two teams — Team A and Team B — share a single HyperPod EKS cluster, each operating within their own isolated namespace.

![Multi-tenant HyperPod EKS Architecture](images/multi-tenant-hp-eks.jpg)

The architecture is structured as a layered flow from left to right, connecting user identity through authorization controls and into isolated workload namespaces on the cluster.

**Users and authentication (left side).** On the far left, individual users from each team (User 1 from Team A, User 2 from Team B) interact with the system through two paths. Both paths authenticate through the AWS IAM Identity Center Portal, which federates with an external identity provider (such as Microsoft Entra ID) shown at the bottom-left. The first path is via CLI access, where users authenticate using `aws sso login` — which redirects them to the Identity Center portal for authentication — and then obtain temporary credentials from their team's permission set to submit tasks directly to the EKS cluster with `kubectl` (shown by the pink arrow flowing from the CLI terminal across the top of the diagram directly into the HyperPod EKS cluster). The second path is through the Identity Center portal directly, where users select the SageMaker Studio application to log in to their team-specific SageMaker Domain. Each team has a corresponding permission set (TeamA permission set, TeamB permission set) that carries the IAM policies needed for CLI workflows. Identity Center automatically provisions an IAM role for each permission set — shown in the diagram as TeamA-permissionset-role and TeamB-permissionset-role (labeled "CLI/Console role") — which serves as the IAM principal when users authenticate via `aws sso login`.

**SageMaker Domains (center-left).** From the Identity Center portal, users are routed to their team-specific SageMaker Domain. Each domain — SageMaker Domain Team A and SageMaker Domain Team B — provides a dedicated SageMaker Studio GUI and is configured with a team-specific execution role (TeamA-role and TeamB-role respectively). These domains serve as the primary workspace interface, enabling users to submit tasks from the GUI (shown by the pink arrows flowing toward EKS).

**EKS access control (center).** At the EKS boundary, access entries map IAM roles to Kubernetes permissions. The diagram shows Access Entries for TeamA-role and TeamB-role (the Studio execution roles), which authorize requests originating from the SageMaker Studio GUI. Additionally, access entries must be configured for the IDC-provisioned CLI/Console roles (TeamA-permissionset-role and TeamB-permissionset-role) to authorize requests arriving via `kubectl`. All access entries are associated with managed or custom RBAC policies (represented by the key icon) and scoped to the team's designated namespace, ensuring that regardless of whether access originates from Studio or the CLI, users can only interact with resources in their own namespace.

**HyperPod EKS cluster (right side).** The cluster itself is depicted with two cross-cutting platform layers at the top: HyperPod Observability (for monitoring and dashboards) and HyperPod Task Governance (for compute quota management and scheduling priorities). Below these layers, the cluster is partitioned into Namespace A (Team A) and Namespace B (Team B). Within each namespace, teams can run their own HyperPod Spaces (interactive development environments), HyperPod PyTorch jobs (distributed training workloads), and HyperPod Inference endpoints (model serving).

**Storage (bottom).** Beneath the cluster, the architecture shows two storage tiers. The first is a POSIX-compliant file system (FSx for Lustre or OpenZFS) organized into per-team shared directories (`/fsx/TeamA`, `/fsx/TeamB`) and per-user home directories (`/home/User1`, `/home/User2`). The second is per-team or shared Amazon S3 buckets for object storage, governed by the team's IAM execution role.

This architecture ensures that the entire flow — from authentication through authorization to workload execution — maintains team isolation while sharing expensive GPU infrastructure efficiently.

## Authentication and Access Control

The foundation of any multi-tenant system is robust authentication: verifying who users are before they interact with any resource. In this architecture, AWS IAM Identity Center serves as the centralized authentication layer, federating with an external identity provider to manage user identities and group memberships.

### Why AWS IAM Identity Center

AWS IAM Identity Center (successor to AWS Single Sign-On) provides a single place to manage workforce identities across AWS accounts and applications. For a multi-tenant HyperPod deployment, it offers several critical capabilities:

- **Centralized identity management** — Rather than maintaining separate user databases per AWS service, Identity Center provides a single source of truth for all user identities and their group memberships.
- **Federation with existing identity providers** — Most enterprises already manage their workforce identities in systems like Microsoft Entra ID (formerly Azure AD), Okta, or Ping Identity. Identity Center integrates with these providers, allowing organizations to reuse their existing identity infrastructure without duplicating user accounts.
- **Native integration with SageMaker** — SageMaker Domains support Identity Center authentication out of the box, enabling seamless single sign-on from the corporate identity provider directly into SageMaker Studio.
- **AWS account access** — Identity Center can also grant users access to the underlying AWS account with specific permission sets, enabling CLI workflows alongside the Studio GUI experience.
- **Required for Amazon Managed Grafana** — Amazon Managed Grafana uses Identity Center as its authentication mechanism for workforce users, making it the natural choice when teams also need access to observability dashboards for monitoring their workloads.

### Configuring Identity Center with an External Identity Provider

In this reference architecture, we use Microsoft Entra ID as the external identity provider, though the same pattern applies to any SAML 2.0 or SCIM-compatible provider.

The configuration involves:

1. **Group structure in the identity provider** — In Entra ID, create groups that correspond to your organizational teams. In our example, we define three groups: `TeamA`, `TeamB`, and `Admin`. Each group contains the users belonging to that team (for example, `user1-teamA@domain.com` in the TeamA group).

2. **SCIM provisioning** — Enable SCIM (System for Cross-domain Identity Management) synchronization between Entra ID and AWS IAM Identity Center. SCIM provides automatic provisioning and de-provisioning of users and groups. When a new user is added to the TeamA group in Entra ID, they are automatically synchronized to Identity Center and gain the appropriate access without manual intervention.

3. **SAML-based authentication** — Configure SAML 2.0 federation so that when users authenticate, they do so against Entra ID. Identity Center acts as the service provider, trusting the assertions from your Entra ID tenant.

This configuration ensures that team membership — which drives all downstream authorization decisions — is managed in your existing corporate directory and automatically propagated to AWS.

## Authorization

With authentication established, the next layer is authorization: controlling what actions each team can perform across AWS services and the Kubernetes cluster. Authorization in this architecture operates at two levels — AWS IAM for service-level access, and Kubernetes RBAC for cluster-level access.

### Per-Team IAM Roles

Each team requires a dedicated IAM role that encapsulates the AWS-level permissions needed for their AI/ML workflows. These roles serve as the SageMaker Domain execution role and define what AWS services the team can access.

A typical team IAM role should include policies granting access to:

- **Amazon SageMaker** — For managing HyperPod resources, Spaces, training jobs, and endpoints through the SageMaker API.
- **Amazon S3** — For reading training datasets and writing model artifacts, checkpoints, and logs. Scope these permissions to team-specific bucket prefixes.
- **Amazon CloudWatch** — For viewing logs and metrics related to the team's workloads.
- **Amazon EKS** — Specifically, the `eks:InvokeKubernetesApi` permission, which is critical for enabling the SageMaker Studio GUI to make Kubernetes API calls on behalf of the user (for example, listing Spaces or submitting jobs).

The trust policy on each IAM role must include `sagemaker.amazonaws.com` as a trusted principal, allowing SageMaker to assume the role on behalf of users when they operate through Studio. CLI access via Identity Center uses a separate permission set with its own policies (see "AWS Account Access via Identity Center" below), allowing CLI permissions to be independently scoped.

### AWS Account Access via Identity Center

Beyond SageMaker Studio, teams often need direct AWS account access for CLI operations — running `kubectl` commands, scripting workflows, or accessing resources programmatically. Identity Center permission sets provide this capability.

For the **Admin** group, assign a permission set with administrative access as required by your company policies, granting the necessary account access for cluster management and administrative operations.

For **Team A** and **Team B**, create permission sets with inline or managed policies that grant the permissions needed for CLI workflows directly. A typical team permission set includes permissions for `eks:InvokeKubernetesApi` (to interact with the EKS cluster), scoped S3 access for the team's data, and CloudWatch read access for monitoring. These policies are defined independently from the Studio execution role, allowing administrators to tailor CLI permissions to the specific operations teams perform from the command line.

Users retrieve temporary credentials via the AWS CLI using `aws sso login`, which they can then use to configure `kubectl` for direct interaction with the EKS cluster.

### Configuring the AWS CLI

Team members configure the AWS CLI to authenticate via Identity Center by running `aws configure sso`. This creates profiles in `~/.aws/config` that reference the appropriate Identity Center session and permission set. Each team member uses their team-specific profile when interacting with the cluster from the command line, ensuring that authorization boundaries are maintained whether access originates from Studio or from a local terminal.

## SageMaker Domains

SageMaker Domains provide the workspace boundary for each team, offering a tailored user experience, pre-configured execution roles, and built-in integration with Identity Center authentication.

### Why SageMaker Domains

Using one SageMaker Domain per team is a well-established pattern for organizing multi-team environments. This approach offers several advantages:

- **Established multi-team pattern** — AWS has documented this approach extensively for separating lines of business or teams with multiple domains, making it a proven and supported configuration.
- **Native Identity Center authentication** — Each domain can be configured with Identity Center authentication, meaning users sign in once through their corporate identity provider and land directly in their team's Studio environment.
- **Built-in team configuration** — Domains already provide mechanisms to specify configurations for users and teams without requiring additional custom entities. For example, `CustomPosixUserConfig` can be used to assign specific UIDs and GIDs to users, which is essential for POSIX file system permissions on shared storage.
- **Navigation customization** — Domain settings allow administrators to hide navigation items that are not relevant to the team's workflows, presenting a focused interface tailored to HyperPod use cases.

### Setting Up Per-Team Domains

Create one SageMaker Domain per team with Identity Center authentication. In our example, we create `TeamA-domain` and `TeamB-domain`. Each domain is configured as follows:

1. **Default execution role** — Set the domain's default execution role to the team-specific IAM role created in the authorization step. This ensures that all actions performed through Studio inherit the appropriate permissions.

2. **Identity Center group assignment** — Add the corresponding Identity Center group (e.g., the `TeamA` group) to the domain. This enables the SageMaker Studio application for all members of that group, granting them access to the domain's Studio interface.

3. **Application assignment verification** — After configuring group access, review the application assignment in Identity Center to confirm that the correct groups are mapped to the correct domains.

4. **Navigation customization** — Configure the default navigation settings for each domain to present only the relevant capabilities. For example, you might hide items not related to HyperPod workflows, providing a streamlined "HyperPod-focused" user experience that reduces cognitive overhead for team members who only need to work with HyperPod resources.

## HyperPod EKS Cluster Configuration

The HyperPod EKS cluster is where workloads actually execute. Multi-tenancy at the cluster level is achieved through Kubernetes namespaces for isolation and EKS access entries for authorization.

### Namespace Isolation

Create a dedicated Kubernetes namespace for each team — for example, `hyperpod-ns-team-a` and `hyperpod-ns-team-b`. Namespaces provide a logical boundary within the cluster, ensuring that each team's workloads (Spaces, training jobs, inference endpoints) are isolated from one another.

Namespaces can be created manually with `kubectl create namespace` or provisioned automatically through HyperPod Task Governance, which manages namespaces as part of its quota and scheduling configuration.

### EKS Access Entries

EKS access entries connect IAM principals to Kubernetes RBAC permissions. For each team, create two access entries:

- **Studio access entry** — The IAM principal is the team's SageMaker Domain execution role. This entry is used when actions originate from the SageMaker Studio GUI.
- **CLI access entry** — The IAM principal is the SSO-provisioned role created by Identity Center for the team's permission set (following the pattern `AWSReservedSSO_<permission-set-name>_<unique-id>`). This entry is used when users interact with the cluster via `kubectl`.

Both entries are scoped to the team's namespace with managed or custom Kubernetes policies. For example, both Team A entries grant permissions only within `hyperpod-ns-team-a`. The two entries can carry different RBAC policies if desired — for instance, the CLI entry might restrict write access to certain resource types while the Studio entry allows full access.

This scoping ensures that whether access originates from Studio or from the CLI, users can only interact with resources in their own namespace. Attempting to list or modify resources in another team's namespace results in a Kubernetes `Forbidden` error.

For more advanced scenarios, you can use Kubernetes groups in the access entry to map users to custom ClusterRoles or Roles that provide fine-grained permissions beyond the standard managed policies.

### HyperPod Task Governance

When Task Governance is enabled on the cluster, it provides an additional layer of resource management:

- **Compute quotas** — Define how much GPU and CPU capacity each team can consume. This prevents a single team from monopolizing shared hardware during training runs.
- **Priorities** — Assign scheduling priorities to each team or workload type, allowing critical production inference workloads to preempt experimental training jobs when resources are constrained.
- **Fair scheduling** — Task Governance ensures that when multiple teams are competing for resources, allocation follows the configured policies rather than a first-come-first-served model.

Configure Task Governance with appropriate quotas and priorities per team namespace, balancing between guaranteed minimum allocations and burst capacity for bursty workloads.

## Storage

Shared storage is a critical component of multi-tenant AI/ML environments. Teams need high-performance file systems for training data, checkpoints, and model artifacts, while maintaining appropriate access boundaries between teams.

### POSIX-Compliant File Systems

For workloads that require a shared, high-performance POSIX file system — which is common for distributed training where multiple nodes read the same dataset or write checkpoints — consider the following options:

- **Amazon FSx for Lustre** — Provides high-throughput, low-latency parallel file system access, ideal for large-scale training workloads that need to read large datasets at high speed.
- **Amazon FSx for OpenZFS** — Offers a general-purpose file system with strong POSIX semantics, snapshots, and compression. Well-suited for workloads that need traditional file system features alongside high performance.
- **Amazon EFS** — Provides fully managed, elastic NFS storage. EFS also supports access points, which can simplify per-team directory isolation by mapping different mount points to different directories with enforced UIDs and GIDs.

The storage layout typically follows this structure:

- **Per-team shared directories** — Each team has a shared directory (e.g., `/fsx/TeamA`, `/fsx/TeamB`) for datasets, models, and artifacts that all team members need to access.
- **Per-user home directories** — Each user has a personal home directory (e.g., `/home/User1`, `/home/User2`) for individual work, experiments, and notebooks.

The POSIX permission model on these file systems relies on UIDs, GIDs, and supplemental groups to enforce access boundaries. SageMaker Domains support `CustomPosixUserConfig`, which allows administrators to assign specific UIDs and GIDs to users. These POSIX identities should then be propagated to the pod's security context when a user launches a HyperPod Space or submits a training job, ensuring that file system access respects the configured ownership and permissions.

### Amazon S3 Storage

For object storage, access to S3 buckets is governed by the team's IAM execution role. You can create per-team buckets or use a shared bucket with per-team prefixes, relying on IAM policies to enforce isolation. Pods within the cluster need appropriate service accounts configured with IAM Roles for Service Accounts (IRSA) or Pod Identity to authenticate to S3. For simplicity, you can associate the same execution role configured on the SageMaker Domain to a Kubernetes service account within the team's namespace, providing consistent S3 access from both Studio and cluster workloads.

## HyperPod Spaces

HyperPod Spaces provide interactive development environments (IDEs) running directly on cluster nodes. In a multi-tenant setting, Spaces must be properly scoped to each team's namespace and configured with appropriate resource templates.

### Space Templates

Create namespace-scoped Space templates for each team. These templates define the resource configurations (instance types, storage volumes, environment variables) available to team members when creating Spaces. By scoping templates to a namespace, you ensure that each team can only launch Spaces within their designated boundary.

When HyperPod Task Governance is enabled, templates should include the default labels required by the governance system (such as team identifier and priority labels). The cluster administrator pre-configures these labels so that team members do not need to specify them manually when launching Spaces.

### Persistent Volume Claims

Create the appropriate Persistent Volume Claims (PVCs) in each team's namespace, referencing the shared file system. These PVCs mount the team's shared directory and the user's home directory into the Space, providing access to training data, checkpoints, and personal workspaces.

### Owner-Only vs. Shared Spaces

Consider your organization's requirements around Space sharing:

- **Owner-only Spaces** — Each Space is accessible only by the user who created it. This is the default and most secure configuration, appropriate when teams work on sensitive or independent projects.
- **Shared Spaces** — Multiple team members can access the same Space, useful for pair programming, collaborative debugging, or shared development environments. When enabling shared Spaces, ensure that POSIX permissions and supplemental groups are configured to allow appropriate access to files created within the Space.

### Connectivity: SSM vs. SSH

HyperPod Spaces support two connectivity methods for remote IDE access:

- **AWS Systems Manager (SSM)** — Provides a managed, secure connection without requiring inbound firewall rules or SSH key management. SSM sessions are authenticated via IAM, making them a natural fit for this architecture where access is already controlled through IAM roles.
- **SSH** — Provides traditional SSH access for tools and workflows that require it. Requires additional key management and network configuration.

SSM is generally recommended for multi-tenant environments due to its tighter integration with IAM-based access control.

### Consistency Between Studio GUI and CLI

Users should be able to create and manage Spaces either from the SageMaker Studio GUI or from the command line with `kubectl`, and the results should be consistent. Although Studio and CLI use different IAM principals (the Studio execution role and the SSO-provisioned role respectively), both have EKS access entries scoped to the same team namespace. This ensures that Spaces created from either interface appear in the same namespace. Note that the RBAC policies on each access entry may differ intentionally — for example, CLI access might be scoped to a subset of operations while Studio retains broader permissions within the namespace.

## HyperPod Training Operator

The HyperPod Training Operator enables teams to submit distributed training jobs as Kubernetes custom resources (for example, PyTorchJob). In the multi-tenant architecture, training jobs are namespace-scoped, meaning they automatically inherit the team's isolation boundaries.

Teams can submit training jobs from the CLI using `kubectl apply` with the appropriate job manifest. The job runs in the team's namespace, uses the team's compute quotas (if Task Governance is enabled), and has access to the team's storage volumes.

When Task Governance is enabled, training jobs are subject to the team's allocated quotas and priority settings. If a team has consumed its guaranteed allocation, jobs may be queued until resources become available or until lower-priority workloads are preempted.

## HyperPod Inference Operator

The HyperPod Inference Operator allows teams to deploy models as inference endpoints directly on the cluster. Similar to training jobs, inference endpoints are namespace-scoped and subject to the team's RBAC policies and Task Governance quotas.

Teams can deploy models from the CLI by creating inference endpoint custom resources in their namespace. The endpoints are isolated per namespace, meaning Team A cannot access or interfere with Team B's inference endpoints.

For production inference workloads that require high availability, consider assigning higher scheduling priority to inference endpoints than to training jobs, ensuring that model serving is not interrupted by batch training workloads.

## HyperPod Observability

Visibility into cluster health, workload performance, and resource utilization is essential for all teams. HyperPod Observability provides built-in monitoring and dashboarding capabilities through Amazon Managed Grafana.

### Configuring Team Access to Grafana

Teams need access to observability dashboards to monitor their workloads, troubleshoot performance issues, and understand resource consumption. However, in a multi-tenant environment, this access should typically be read-only:

1. **Configure Identity Center authentication for Amazon Managed Grafana** — In the Amazon Managed Grafana console, navigate to Authentication and enable AWS IAM Identity Center. This allows users to sign in to Grafana with the same corporate credentials they use for SageMaker Studio.

2. **Assign team groups as Viewers** — Map the Identity Center groups (TeamA, TeamB) to the Grafana Viewer role. This grants team members read-only access to dashboards and metrics without the ability to modify dashboards or data sources.

3. **Admin access** — Assign the Admin group to the Grafana Admin or Editor role, allowing them to create and modify dashboards, configure alerting, and manage data sources.

4. **Team-specific dashboards** — Consider creating dedicated dashboards that filter data by namespace, allowing each team to see only their own workload metrics. Grafana Teams can potentially be leveraged to restrict dashboard visibility to specific groups, providing an additional layer of data isolation.

## Getting Started

To simplify the deployment of this multi-tenant architecture, we recommend an infrastructure-as-code approach that automates the provisioning of all per-team resources. A CloudFormation template or CDK stack can be designed to accept a team name as input and automatically create:

- The team IAM role with appropriate policies and trust relationships
- The Identity Center permission set with CLI-specific policies for the team
- The SageMaker Domain with Identity Center authentication and group assignment
- The Kubernetes namespace within the HyperPod EKS cluster
- The EKS access entries with namespace-scoped policies (one for the Studio execution role, one for the SSO-provisioned CLI role)
- Task Governance configuration (quotas and priorities)
- HyperPod Space templates within the team namespace
- Persistent Volume Claims for shared and home directories
- Storage directories on the shared file system
- A default S3 bucket or prefix for the team

This approach reduces the administrative overhead of onboarding new teams from a multi-step manual process to a single deployment action, reducing errors and ensuring consistency across teams.

## Conclusion

This post presented a reference architecture for building multi-tenant environments on Amazon SageMaker HyperPod with EKS. By combining AWS IAM Identity Center for authentication, per-team IAM roles for AWS-level authorization, SageMaker Domains for a tailored workspace experience, Kubernetes namespaces for workload isolation, and HyperPod Task Governance for fair resource allocation, organizations can enable multiple teams to securely and efficiently share a single HyperPod EKS cluster.

This is a flexible, composable approach that combines multiple building blocks into a cohesive solution. The architecture accommodates a variety of use cases and organizational structures. For example, organizations can extend this pattern to connect teams to HyperPod Slurm clusters alongside EKS, providing a unified multi-tenant experience across different orchestration backends — a pattern already proven in production environments.

While this approach requires assembling and configuring multiple components, the result is a high degree of control and customization that can be tailored to each organization's specific security, compliance, and operational requirements; the foundational patterns — identity federation, namespace isolation, RBAC, and quota-based governance — will remain applicable.

---

# Appendix A: CLI Access with Identity Center Permission Sets

This appendix details how a team member can use the AWS CLI and `kubectl` with credentials provided directly by their Identity Center permission set.

## Setting Up the Permission Set

In Identity Center, create a custom permission set for each team with inline or managed policies that grant the permissions needed for CLI workflows. The permission set acts as the team's CLI identity — no additional role assumption is required by the user.

Example inline policy for **TeamA-permission-set**:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "EKSAccess",
            "Effect": "Allow",
            "Action": [
                "eks:InvokeKubernetesApi",
                "eks:DescribeCluster"
            ],
            "Resource": [
                "arn:aws:eks:us-west-2:123456789012:cluster/hyperpod-cluster"
            ]
        },
        {
            "Sid": "S3TeamObjectAccess",
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject"
            ],
            "Resource": [
                "arn:aws:s3:::ml-data-bucket/team-a/*"
            ]
        },
        {
            "Sid": "S3TeamListAccess",
            "Effect": "Allow",
            "Action": [
                "s3:ListBucket"
            ],
            "Resource": [
                "arn:aws:s3:::ml-data-bucket"
            ],
            "Condition": {
                "StringLike": {
                    "s3:prefix": "team-a/*"
                }
            }
        },
        {
            "Sid": "CloudWatchRead",
            "Effect": "Allow",
            "Action": [
                "logs:GetLogEvents",
                "logs:FilterLogEvents",
                "logs:DescribeLogStreams"
            ],
            "Resource": [
                "arn:aws:logs:us-west-2:123456789012:log-group:/aws/eks/hyperpod-cluster/*:*"
            ]
        },
        {
            "Sid": "CloudWatchDescribeLogGroups",
            "Effect": "Allow",
            "Action": [
                "logs:DescribeLogGroups"
            ],
            "Resource": [
                "arn:aws:logs:us-west-2:123456789012:log-group:*"
            ]
        },
        {
            "Sid": "CloudWatchMetrics",
            "Effect": "Allow",
            "Action": [
                "cloudwatch:GetMetricData",
                "cloudwatch:ListMetrics"
            ],
            "Resource": "*"
        },
        {
            "Sid": "SSMSessionAccess",
            "Effect": "Allow",
            "Action": [
                "ssm:StartSession"
            ],
            "Resource": "*"
        }
    ]
}
```

## Configuring the EKS Access Entry

Identity Center creates an IAM role for the permission set following the naming pattern:

```
arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/<region>/AWSReservedSSO_TeamA-permission-set_<unique-id>
```

Create an EKS access entry for this role, scoped to the team's namespace. This grants the SSO-provisioned role Kubernetes-level permissions when users interact with the cluster via `kubectl`.

## AWS CLI Configuration

Configure the AWS CLI with `aws configure sso` and select the appropriate permission set. An example `~/.aws/config`:

```ini
[sso-session my-sso]
sso_start_url = https://d-xxxxxxxxxx.awsapps.com/start
sso_region = us-west-2
sso_registration_scopes = sso:account:access

[default]
sso_session = my-sso
sso_account_id = 123456789012
sso_role_name = AdministratorAccess
region = us-west-2

[profile team-a]
sso_session = my-sso
sso_account_id = 123456789012
sso_role_name = TeamA-permission-set
region = us-west-2

[profile team-b]
sso_session = my-sso
sso_account_id = 123456789012
sso_role_name = TeamB-permission-set
region = us-west-2
```

## Login and kubectl Workflow

1. **Authenticate via SSO:**

```bash
aws sso logout --profile team-a
aws sso login --profile team-a
```

2. **Verify identity:**

```bash
aws sts get-caller-identity --profile team-a
```

3. **Configure kubectl for the HyperPod EKS cluster:**

```bash
aws eks update-kubeconfig \
  --name hyperpod-cluster \
  --region us-west-2 \
  --profile team-a
```

4. **Use kubectl with namespace-scoped access:**

```bash
# This will fail — access is scoped to the team namespace
kubectl get Workspaces
# Error: Forbidden in namespace "default"

# This succeeds — correct namespace
kubectl get Workspaces -n hyperpod-ns-team-a
```

---

# Appendix B: Known Gaps and Future Improvements

This appendix documents the current limitations and gaps identified in the multi-tenant architecture that are expected to be addressed in future HyperPod releases.

## Gap 1: HyperPod-Specific Navigation in SageMaker Domains

**Current state:** SageMaker Domains support hiding navigation items to tailor the user experience, which can be used to create a more HyperPod-focused interface. However, a fully purpose-built HyperPod navigation experience is not yet available as a native domain configuration option.

**Impact:** Administrators must manually configure navigation settings to approximate a HyperPod-centric experience. Users may still see some navigation items that are not relevant to their HyperPod workflows.

**Workaround:** Use the existing domain navigation settings to hide irrelevant items. This provides a reasonable approximation of a HyperPod-focused experience but requires manual configuration and may not cover all navigation elements.

## Gap 2: POSIX Security Context Injection for Pods

**Current state:** In order to enforce the POSIX permission model on shared file systems (FSx for Lustre, FSx for OpenZFS, EFS), pods must run with the correct UID, GID, and supplemental groups in their security context. Currently, there is no automatic mechanism to read the `CustomPosixUserConfig` from the SageMaker Domain and inject these values into the pod's security context at runtime.

**Impact:** Without proper security context configuration, pods may run as root or with a default UID that does not match the file system permissions, potentially allowing unauthorized access to other teams' files or preventing access to the team's own directories.

**Workaround:** Cluster administrators can use Kubernetes admission webhooks (such as a mutating webhook) to inject the correct security context based on the namespace or service account. Alternatively, Space templates and job manifests can be pre-configured with the correct `runAsUser`, `runAsGroup`, and `fsGroup` settings for each team. This requires manual coordination between the SageMaker Domain POSIX configuration and the Kubernetes resource definitions.

## Gap 3: Team Shared Directory Path Configuration

**Current state:** There is no dedicated attribute in the SageMaker Domain or HyperPod configuration to specify the name and path of the team's shared directory on the file system.

**Impact:** Administrators must establish a convention for team directory naming and manually ensure that Persistent Volume Claims, mount paths in templates, and file system directory structures are all consistent.

**Workaround:** Adopt a naming convention based on the team's GID or namespace name (e.g., `/fsx/<namespace-name>/` or `/fsx/gid-<GID>/`). Document this convention and encode it in the infrastructure-as-code templates that provision per-team resources. This ensures consistency but relies on convention rather than configuration.

## Gap 4: Training Job Submission from Studio GUI

**Current state:** Submitting HyperPod training jobs (using the HyperPod Training Operator) directly from the SageMaker Studio GUI is not yet available. Users must submit training jobs via the CLI using `kubectl apply`.

**Impact:** Team members who prefer the GUI workflow must switch to the CLI for training job submission, which adds friction and requires familiarity with Kubernetes manifests and `kubectl`.

**Workaround:** Provide pre-built job templates as YAML files that team members can customize and submit via the CLI. Consider creating wrapper scripts or Jupyter notebooks that generate and submit job manifests programmatically, providing a more guided experience until native GUI support is available.

## Gap 5: Inference Endpoint Deployment from Studio GUI

**Current state:** Deploying models as inference endpoints using the HyperPod Inference Operator directly from the SageMaker Studio GUI is not yet available. Users must deploy endpoints via the CLI.

**Impact:** Similar to Gap 4, this requires team members to use CLI-based workflows for model deployment, which may be less accessible for data scientists accustomed to GUI-driven deployment.

**Workaround:** Provide deployment templates and scripts that simplify the CLI-based workflow. Jupyter notebooks with pre-configured deployment cells can serve as an intermediate solution.

## Gap 6: Grafana Team-Based Dashboard Isolation

**Current state:** Using Grafana Teams to restrict dashboard visibility to specific Identity Center groups has not been fully validated. While Amazon Managed Grafana supports Teams and folder-level permissions, the integration with Identity Center groups for automatic team assignment requires further testing.

**Impact:** Without team-based dashboard isolation, all Viewer-level users may see all dashboards, including those showing metrics from other teams' namespaces. While the data in the dashboards is filtered by namespace, the dashboards themselves may be visible to all teams.

**Workaround:** Create namespace-filtered dashboards and rely on Grafana folder permissions to organize team-specific dashboards. Alternatively, use Grafana's row-level security or variable-based filtering to ensure that each user only sees data from their own namespace, even if the dashboards are shared.
