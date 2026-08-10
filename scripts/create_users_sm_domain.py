#!/usr/bin/env python3
"""
Create Linux users and groups from a SageMaker AI domain configuration.

Given a SageMaker domain ID, this script:
1. Creates a Linux group using the domain name as group name and the domain-level
   CustomPosixUserConfig GID as the group GID.
   If a group with the target GID already exists under a different name, that
   existing group is reused and no new group is created (handles IAM-auth domains
   where the GID is pre-allocated to a Slurm team group such as "TeamA").
2. For each user profile in the domain, creates a Linux user where:
   - Username is the local part of SingleSignOnUserValue (before @), OR
     the UserProfileName directly when SingleSignOnUserValue is absent
     (IAM-auth domains do not support SSO user values).
   - UID is the CustomPosixUserConfig UID from the user profile
   - Primary group is the domain group (or the pre-existing group at that GID)
"""

import argparse
import grp
import logging
import os
import pwd
import subprocess
import sys
import time

import boto3

# ---------- Configuration ----------
HOME_BASE_DIR = "/fsx"
DEFAULT_GID = 2001
DEFAULT_UID_START = 20001
# -----------------------------------

logger = logging.getLogger(__name__)


def get_domain_info(sagemaker_client, domain_id: str) -> dict:
    """Describe the domain by ID and return its details."""
    return sagemaker_client.describe_domain(DomainId=domain_id)


def get_user_profiles(sagemaker_client, domain_id: str) -> list:
    """List and describe all user profiles in the domain."""
    paginator = sagemaker_client.get_paginator("list_user_profiles")
    profiles = []

    for page in paginator.paginate(DomainIdEquals=domain_id):
        for profile_summary in page["UserProfiles"]:
            profile = sagemaker_client.describe_user_profile(
                DomainId=domain_id,
                UserProfileName=profile_summary["UserProfileName"],
            )
            profiles.append(profile)
            time.sleep(1)  # Respect 1 RPS throttle on DescribeUserProfile

    return profiles


def group_exists(group_name: str) -> bool:
    """Check if a Linux group already exists."""
    try:
        grp.getgrnam(group_name)
        return True
    except KeyError:
        return False


def user_exists(username: str) -> bool:
    """Check if a Linux user already exists."""
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def run_command(cmd: list[str], dry_run: bool = False) -> None:
    """Run a system command, or log it in dry-run mode."""
    if dry_run:
        logger.info("[dry-run] %s", " ".join(cmd))
        return

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Command failed: %s — %s", " ".join(cmd), result.stderr.strip())
    else:
        logger.info("OK: %s", " ".join(cmd))


def gid_exists(gid: int) -> str | None:
    """Return the name of the group that owns this GID, or None if no group has it."""
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return None


def create_group(group_name: str, gid: int, dry_run: bool = False) -> str:
    """Create a Linux group and return the effective group name to use.

    If a group with the target GID already exists (possibly under a different
    name such as 'TeamA'), reuse it rather than failing.  The caller should
    use the returned name when creating users so they land in the correct group.
    """
    existing_by_gid = gid_exists(gid)
    if existing_by_gid is not None:
        if existing_by_gid == group_name:
            logger.info("Group '%s' (gid=%d) already exists, skipping.", group_name, gid)
        else:
            logger.info(
                "GID %d is already owned by group '%s' (wanted '%s') — reusing '%s'.",
                gid, existing_by_gid, group_name, existing_by_gid,
            )
        return existing_by_gid

    if group_exists(group_name):
        # Name exists but different GID — unusual; log and proceed
        existing_gid = grp.getgrnam(group_name).gr_gid
        logger.warning(
            "Group '%s' already exists with GID %d (wanted %d), using it as-is.",
            group_name, existing_gid, gid,
        )
        return group_name

    logger.info("Creating group '%s' with GID %d...", group_name, gid)
    run_command(["groupadd", "--gid", str(gid), group_name], dry_run=dry_run)
    return group_name


def ensure_home_directory(username: str, uid: int, gid: int, home_dir: str, dry_run: bool = False) -> None:
    """Ensure the home directory exists with correct ownership."""
    if not os.path.isdir(home_dir):
        logger.info("Creating home directory '%s'...", home_dir)
        run_command(["mkdir", "-p", home_dir], dry_run=dry_run)
    run_command(["chown", f"{uid}:{gid}", home_dir], dry_run=dry_run)


def create_user(username: str, uid: int, group_name: str, gid: int, dry_run: bool = False) -> None:
    """Create a Linux user with the given UID and primary group, or fix home if user exists.

    After creating (or verifying) the user this function always:
    - Adds the user as a supplementary member of their team group via ``usermod -aG``.
      Slurm's AllowGroups directive resolves group membership by reading the supplementary
      member list in /etc/group, NOT the primary GID in /etc/passwd.  A user created with
      ``useradd --gid TeamA`` has TeamA as their primary group but is absent from the
      supplementary list, causing AllowGroups to fail open (any user can submit to any
      partition).  The ``usermod -aG`` call fixes this; it is idempotent when already applied.
    - Sets the home directory permissions to mode 750 (owner rwx, group r-x, others ---).
      This allows team members to read each other's home directories within the same group,
      which is useful for shared code and scripts.  If complete isolation between team members
      is required, change the chmod call below to use mode 700.
    """
    home_dir = os.path.join(HOME_BASE_DIR, username)

    if user_exists(username):
        # Ensure home directory is set correctly even for pre-existing users
        existing = pwd.getpwnam(username)
        if existing.pw_dir != home_dir:
            logger.info(
                "User '%s' exists but home is '%s', updating to '%s'...",
                username, existing.pw_dir, home_dir,
            )
            run_command(["usermod", "--home", home_dir, username], dry_run=dry_run)
            ensure_home_directory(username, uid, gid, home_dir, dry_run=dry_run)
        else:
            logger.info("User '%s' (uid=%d) already exists with correct home, skipping.", username, uid)
    else:
        logger.info(
            "Creating user '%s' with UID %d, group '%s', home '%s'...",
            username, uid, group_name, home_dir,
        )
        run_command(
            [
                "useradd",
                "--uid", str(uid),
                "--gid", group_name,
                "--home-dir", home_dir,
                "--create-home",
                "--shell", "/bin/bash",
                username,
            ],
            dry_run=dry_run,
        )

    # Always ensure supplementary group membership (idempotent).
    # Required for Slurm AllowGroups enforcement — see docstring above.
    logger.info(
        "Adding '%s' as supplementary member of group '%s' (required for Slurm AllowGroups)...",
        username, group_name,
    )
    run_command(["usermod", "-aG", group_name, username], dry_run=dry_run)

    # Always set home directory permissions to 750 (idempotent).
    # Change to 700 if complete isolation between team members is required.
    run_command(["chmod", "750", home_dir], dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(
        description="Create Linux users and group from a SageMaker domain."
    )
    parser.add_argument("domain_id", help="ID of the SageMaker domain.")
    parser.add_argument(
        "--region", default=None, help="AWS region (uses default if not specified)."
    )
    parser.add_argument(
        "--profile", default=None, help="AWS CLI profile name."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log commands without executing them.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug-level logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    session_kwargs = {}
    if args.region:
        session_kwargs["region_name"] = args.region
    if args.profile:
        session_kwargs["profile_name"] = args.profile

    session = boto3.Session(**session_kwargs)
    sagemaker_client = session.client("sagemaker")

    # 1. Get domain info
    domain_id = args.domain_id
    logger.info("Describing SageMaker domain '%s'...", domain_id)
    domain_info = get_domain_info(sagemaker_client, domain_id)
    domain_name = domain_info["DomainName"]

    # Extract domain-level GID from CustomPosixUserConfig (fall back to default)
    try:
        domain_gid = domain_info["DefaultUserSettings"]["CustomPosixUserConfig"]["Gid"]
    except KeyError:
        domain_gid = DEFAULT_GID
        logger.warning(
            "CustomPosixUserConfig.Gid not set on domain, using default GID %d.", domain_gid
        )

    # 2. Create the Linux group (group name = domain name); returns effective name
    effective_group = create_group(domain_name, domain_gid, dry_run=args.dry_run)

    # 3. Get user profiles and create Linux users
    logger.info("Listing user profiles for domain '%s' (%s)...", domain_name, domain_id)
    profiles = get_user_profiles(sagemaker_client, domain_id)

    if not profiles:
        logger.warning("No user profiles found in this domain.")
        return

    logger.info("Found %d user profile(s).", len(profiles))

    next_uid = DEFAULT_UID_START

    for profile in profiles:
        profile_name = profile["UserProfileName"]

        # Extract username: prefer SSO value (SSO-auth domains), fall back to
        # UserProfileName (IAM-auth domains do not set SingleSignOnUserValue).
        sso_value = profile.get("SingleSignOnUserValue")
        if sso_value:
            username = sso_value.split("@")[0]
        else:
            username = profile_name
            logger.info(
                "Profile '%s': no SingleSignOnUserValue (IAM-auth domain) — "
                "using UserProfileName '%s' as Linux username.",
                profile_name, username,
            )

        # Extract UID from user-level CustomPosixUserConfig (fall back to auto-incremented default)
        try:
            uid = profile["UserSettings"]["CustomPosixUserConfig"]["Uid"]
        except KeyError:
            uid = next_uid
            logger.warning(
                "CustomPosixUserConfig.Uid not set for '%s', using default UID %d.",
                profile_name, uid,
            )

        create_user(username, uid, effective_group, domain_gid, dry_run=args.dry_run)
        next_uid += 1

    logger.info("Done.")
    logger.info("")
    logger.info(
        "IMPORTANT: Run the following command on the Slurm controller node to refresh "
        "slurmctld's group membership cache so that AllowGroups enforcement takes effect:"
    )
    logger.info("    sudo scontrol reconfig")


if __name__ == "__main__":
    main()
