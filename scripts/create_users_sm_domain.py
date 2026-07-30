#!/usr/bin/env python3
"""
Create Linux users and groups from a SageMaker AI domain configuration.

Given a SageMaker domain ID, this script:
1. Creates a Linux group using the domain name as group name and the domain-level
   CustomPosixUserConfig GID as the group GID.
2. For each user profile in the domain, creates a Linux user where:
   - Username is the local part of SingleSignOnUserValue (before @)
   - UID is the CustomPosixUserConfig UID from the user profile
   - Primary group is the domain group
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


def create_group(group_name: str, gid: int, dry_run: bool = False) -> None:
    """Create a Linux group."""
    if group_exists(group_name):
        logger.info("Group '%s' (gid=%d) already exists, skipping.", group_name, gid)
        return

    logger.info("Creating group '%s' with GID %d...", group_name, gid)
    run_command(["groupadd", "--gid", str(gid), group_name], dry_run=dry_run)


def ensure_home_directory(username: str, uid: int, gid: int, home_dir: str, dry_run: bool = False) -> None:
    """Ensure the home directory exists with correct ownership."""
    if not os.path.isdir(home_dir):
        logger.info("Creating home directory '%s'...", home_dir)
        run_command(["mkdir", "-p", home_dir], dry_run=dry_run)
    run_command(["chown", f"{uid}:{gid}", home_dir], dry_run=dry_run)


def create_user(username: str, uid: int, group_name: str, gid: int, dry_run: bool = False) -> None:
    """Create a Linux user with the given UID and primary group, or fix home if user exists."""
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
        return

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

    # 2. Create the Linux group (group name = domain name)
    create_group(domain_name, domain_gid, dry_run=args.dry_run)

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

        # Extract SSO username (local part before @)
        try:
            sso_value = profile["SingleSignOnUserValue"]
        except KeyError:
            logger.warning("Skipping profile '%s': no SingleSignOnUserValue.", profile_name)
            next_uid += 1
            continue

        username = sso_value.split("@")[0]

        # Extract UID from user-level CustomPosixUserConfig (fall back to auto-incremented default)
        try:
            uid = profile["UserSettings"]["CustomPosixUserConfig"]["Uid"]
        except KeyError:
            uid = next_uid
            logger.warning(
                "CustomPosixUserConfig.Uid not set for '%s', using default UID %d.",
                profile_name, uid,
            )

        create_user(username, uid, domain_name, domain_gid, dry_run=args.dry_run)
        next_uid += 1

    logger.info("Done.")


if __name__ == "__main__":
    main()
