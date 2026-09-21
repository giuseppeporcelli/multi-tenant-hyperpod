# Multi-Tenant Architectures with Amazon SageMaker HyperPod EKS — Appendices

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
