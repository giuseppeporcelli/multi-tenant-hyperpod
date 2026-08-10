#!/usr/bin/env bash
# =============================================================================
# configure_sshd_multi_tenant.sh
#
# Writes /etc/ssh/sshd_config.d/50-mg-multi-tenant.conf on the local HyperPod
# node with an AllowGroups directive appropriate for the node's role.
#
# Node roles (auto-detected from /opt/ml/config/resource_config.json when
# available, falling back to hostname pattern match):
#
#   controller     → AllowGroups: Admin
#   login          → AllowGroups: Admin, mg-domain-teamA, mg-domain-teamB
#   compute-team-a → AllowGroups: Admin, mg-domain-teamA
#   compute-team-b → AllowGroups: Admin, mg-domain-teamB
#
# Public keys are read from users' FSx-backed ~/.ssh/authorized_keys — since
# FSx is mounted on every node, a single key added to a user's home directory
# grants access to every node they are permitted on (subject to sshd AllowGroups).
#
# Two exposure modes are supported (transport is orthogonal to the AllowGroups
# enforcement here):
#   1. SSH via SSM tunnel (recommended). No inbound port on the node. Users
#      connect with:
#          ssh -o "ProxyCommand=aws ssm start-session \
#              --target <instance-id> --document-name AWS-StartSSHSession \
#              --parameters portNumber=%p" USER@<instance-id>
#   2. Direct SSH on port 22 (advanced). Requires a security-group ingress
#      rule to the cluster's SG on TCP 22 from your VPC / bastion CIDR.
#
# Run as root. Recommended: invoke from a HyperPod lifecycle script so
# new/replaced compute nodes receive the policy automatically.
# =============================================================================
set -euo pipefail

# -----------------------------------------------------------------------------
# 1. Detect node role
# -----------------------------------------------------------------------------
NODE_ROLE=""

if [[ -f /opt/ml/config/resource_config.json ]]; then
  NODE_ROLE=$(python3 -c "
import json, sys
try:
    d = json.load(open('/opt/ml/config/resource_config.json'))
    print(d.get('current_group_name', ''))
except Exception:
    pass
" 2>/dev/null || true)
fi

# Fallback: hostname pattern
if [[ -z "${NODE_ROLE}" ]]; then
  H=$(hostname 2>/dev/null || echo "")
  case "${H}" in
    *controller*|*head*) NODE_ROLE="controller" ;;
    *login*)             NODE_ROLE="login" ;;
    *team-a*)            NODE_ROLE="compute-team-a" ;;
    *team-b*)            NODE_ROLE="compute-team-b" ;;
  esac
fi

# Allow explicit override
if [[ -n "${MG_NODE_ROLE:-}" ]]; then
  NODE_ROLE="${MG_NODE_ROLE}"
fi

# -----------------------------------------------------------------------------
# 2. Map role → AllowGroups
# -----------------------------------------------------------------------------
case "${NODE_ROLE}" in
  controller|Controller*|head|Head*)
    ALLOW_GROUPS="Admin"
    ;;
  login|LoginGroup*|login-*)
    ALLOW_GROUPS="Admin mg-domain-teamA mg-domain-teamB"
    ;;
  compute-team-a|*team-a*)
    ALLOW_GROUPS="Admin mg-domain-teamA"
    ;;
  compute-team-b|*team-b*)
    ALLOW_GROUPS="Admin mg-domain-teamB"
    ;;
  *)
    echo "Unknown or missing node role '${NODE_ROLE}' — refusing to configure sshd" >&2
    echo "Set MG_NODE_ROLE=controller|login|compute-team-a|compute-team-b to override." >&2
    exit 1
    ;;
esac

# -----------------------------------------------------------------------------
# 3. Write the drop-in
# -----------------------------------------------------------------------------
DROPIN=/etc/ssh/sshd_config.d/50-mg-multi-tenant.conf

cat > "${DROPIN}" <<EOF
# ${DROPIN}
# Multi-tenant HyperPod Slurm: group-based SSH access.
# Managed by scripts/configure_sshd_multi_tenant.sh
# Node role: ${NODE_ROLE}

AllowGroups ${ALLOW_GROUPS}

# Public keys are read from users' FSx-backed home directories
# (default AuthorizedKeysFile ~/.ssh/authorized_keys).
PubkeyAuthentication yes
PasswordAuthentication no
EOF

chmod 0644 "${DROPIN}"

# -----------------------------------------------------------------------------
# 4. Validate config and restart sshd
# -----------------------------------------------------------------------------
if command -v sshd >/dev/null 2>&1; then
  sshd -t
else
  /usr/sbin/sshd -t
fi

if systemctl list-unit-files 2>/dev/null | grep -q "^ssh\(d\)\?\.service"; then
  systemctl restart ssh 2>/dev/null || systemctl restart sshd
else
  service ssh restart 2>/dev/null || service sshd restart
fi

echo "Configured sshd on role '${NODE_ROLE}' with AllowGroups=${ALLOW_GROUPS}"
