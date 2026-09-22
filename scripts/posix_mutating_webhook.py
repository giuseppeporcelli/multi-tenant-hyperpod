"""
POSIX security context mutating admission webhook (core snippet).

This is a minimal, illustrative snippet — not a complete, production-ready
webhook. It shows the core logic for enforcing POSIX identities on Pods in a
multi-tenant HyperPod on EKS environment:

  1. Read the requesting user's identity from the AdmissionReview request.
     The username (user@domain.com) is carried in the Kubernetes auth
     attribute `Extra["sessionName"]`, which AWS Identity Center propagates
     for the current user.
  2. Look up that user's POSIX identity (UID, GID, supplemental groups) from a
     mapping table — here an Amazon DynamoDB table.
  3. Build a JSONPatch that sets the Pod's securityContext (runAsUser,
     runAsGroup, fsGroup, supplementalGroups) accordingly.

Wire this handler behind an HTTPS server (Flask/FastAPI + TLS) and register it
via a MutatingWebhookConfiguration that intercepts CREATE on pods.
"""

import base64
import copy
import json
from typing import Any

import boto3

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
POSIX_TABLE_NAME = "hyperpod-posix-identities"

# The auth "Extra" key that carries the human user identity. HyperPod/SageMaker
# populates this with the caller's username in the form user@domain.com.
SESSION_NAME_KEY = "sessionName"

_dynamodb = boto3.resource("dynamodb")
_posix_table = _dynamodb.Table(POSIX_TABLE_NAME)


# ---------------------------------------------------------------------------
# 1. Extract the username from the admission request
# ---------------------------------------------------------------------------
def extract_username(admission_request: dict[str, Any]) -> str | None:
    """
    Pull the username (user@domain.com) from the request's userInfo.extra.

    The AdmissionReview request carries the authenticated caller under
    `request.userInfo`. The per-user session identity is surfaced in
    `userInfo.extra["sessionName"]` as a list of string values.
    """
    user_info = admission_request.get("userInfo", {})
    extra = user_info.get("extra", {})

    session_values = extra.get(SESSION_NAME_KEY)
    if session_values:
        # `extra` values are always lists of strings in the admission API.
        return session_values[0]

    return None


# ---------------------------------------------------------------------------
# 2. Look up the POSIX identity from the mapping table (DynamoDB)
# ---------------------------------------------------------------------------
def lookup_posix_identity(username: str) -> dict[str, Any] | None:
    """
    Resolve a username to its POSIX identity.

    Expected DynamoDB item shape (partition key = "username"):
        {
            "username": "user1@example.com",
            "uid": 10001,
            "gid": 20001,
            "supplementalGroups": [30001, 30002]
        }
    """
    response = _posix_table.get_item(Key={"username": username})
    item = response.get("Item")
    if not item:
        return None

    return {
        "uid": int(item["uid"]),
        "gid": int(item["gid"]),
        "supplementalGroups": [int(g) for g in item.get("supplementalGroups", [])],
    }


# ---------------------------------------------------------------------------
# 3. Build the JSONPatch that mutates the Pod security context
# ---------------------------------------------------------------------------
def build_security_context_patch(
    pod: dict[str, Any], posix: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Produce a JSONPatch (RFC 6902) that sets the pod-level securityContext and
    overrides any per-container runAsUser/runAsGroup so the enforced identity
    can't be bypassed at the container level.
    """
    patches: list[dict[str, Any]] = []

    pod_security_context = {
        "runAsUser": posix["uid"],
        "runAsGroup": posix["gid"],
        "fsGroup": posix["gid"],
        "runAsNonRoot": True,
    }
    if posix["supplementalGroups"]:
        pod_security_context["supplementalGroups"] = posix["supplementalGroups"]

    # Replace or add the pod-level securityContext.
    patches.append(
        {
            "op": "replace" if pod["spec"].get("securityContext") else "add",
            "path": "/spec/securityContext",
            "value": pod_security_context,
        }
    )

    # Override per-container securityContext user/group to prevent escalation.
    for idx, container in enumerate(pod["spec"].get("containers", [])):
        container_sc = copy.deepcopy(container.get("securityContext", {}))
        container_sc["runAsUser"] = posix["uid"]
        container_sc["runAsGroup"] = posix["gid"]
        container_sc["allowPrivilegeEscalation"] = False
        patches.append(
            {
                "op": "replace" if container.get("securityContext") else "add",
                "path": f"/spec/containers/{idx}/securityContext",
                "value": container_sc,
            }
        )

    return patches


# ---------------------------------------------------------------------------
# Admission handler — ties the three steps together
# ---------------------------------------------------------------------------
def handle_admission(admission_review: dict[str, Any]) -> dict[str, Any]:
    """
    Entry point for the webhook. Receives an AdmissionReview, returns an
    AdmissionReview response with an optional JSONPatch.
    """
    request = admission_review["request"]
    uid = request["uid"]  # opaque request UID that must be echoed back
    pod = request["object"]

    def allow(patches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        response: dict[str, Any] = {"uid": uid, "allowed": True}
        if patches:
            response["patchType"] = "JSONPatch"
            response["patch"] = base64.b64encode(
                json.dumps(patches).encode("utf-8")
            ).decode("utf-8")
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": response,
        }

    def deny(message: str) -> dict[str, Any]:
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {
                "uid": uid,
                "allowed": False,
                "status": {"code": 403, "message": message},
            },
        }

    username = extract_username(request)
    if not username:
        # No session identity present — fail closed for tenant workloads.
        return deny(f"missing '{SESSION_NAME_KEY}' in userInfo.extra")

    posix = lookup_posix_identity(username)
    if posix is None:
        return deny(f"no POSIX identity mapping found for user '{username}'")

    patches = build_security_context_patch(pod, posix)
    return allow(patches)
