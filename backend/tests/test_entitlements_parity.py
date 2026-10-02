"""
Parity of every tier gate, self-hosted half (#2137).

For each gate and license tier this records what the self-hosted rules
decide: allow or deny, the status and the exact message, and how many times
the gate asked GitHub to count something. The table was recorded before the
cloud tier logic moved into backend/cloud/, and must keep passing unchanged.
The one deliberate edit since is the repo-limit message (#2193), which
offered paid plans the self-hosted beta does not sell. The cloud half,
tests/cloud/test_entitlements_parity.py, reuses the gate drivers below with
its own table.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import HTTPException

import github_env_vars
import github_secrets
import license
import projects
import tier_service
from models import Account, Project

TIERS = ("free", "professional", "enterprise")

# (existing standard projects, existing rwx projects, type of the new one)
PROJECT_SCENARIOS = [
    (2, 0, "standard"), (3, 0, "standard"), (4, 0, "standard"),
    (0, 1, "rwx"), (0, 2, "rwx"), (9, 0, "rwx"), (10, 0, "standard"),
]
REPO_COUNTS = [10, 11, 50, 51]
# (secrets already in the project, secrets being added)
SECRET_SCENARIOS = [(1, 1), (2, 1), (5, 1), (6, 1), (10, 1)]
# (variables already in the project, variables being added)
ENV_VAR_SCENARIOS = [(1, 1), (1, 2), (5, 1), (6, 1)]
# (environments already in the project, whether the new one already exists)
ENV_CREATE_SCENARIOS = [(1, False), (2, False), (2, True), (5, False), (6, False)]
# environments already in the project; the sync adds the environment to 2 repos
ENV_SYNC_SCENARIOS = [0, 1, 4, 5]


def make_account(db, account_type, name="parity-user", **fields):
    account = Account(
        github_user=name,
        github_email=f"{name}@example.com",
        account_type=account_type,
        github_account_type="User",
        last_login_at=datetime(2026, 1, 1),
        **fields,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _denial(exc):
    return (exc.status_code, exc.detail)


def observe_effective_tier(db, account):
    return tier_service.get_effective_tier(account)


def observe_run_history_days(db, account):
    return tier_service.get_run_history_days(account)


def observe_private_scope(db, account):
    return tier_service.check_private_visibility_scope(account)


def observe_repo_limit(db, account):
    return {count: tier_service.check_repo_limit(account, count) for count in REPO_COUNTS}


def observe_project_create(db, account):
    results = {}
    for n, (standard, rwx, project_type) in enumerate(PROJECT_SCENARIOS):
        owner = make_account(db, account.account_type, name=f"parity-owner-{n}")
        for kind, count in (("standard", standard), ("rwx", rwx)):
            for i in range(count):
                db.add(Project(
                    project_name=f"p{n}-{kind}-{i}",
                    project_code=f"P{n}{kind[0].upper()}{i}",
                    user_id=owner.user_id,
                    project_type=kind,
                ))
        db.commit()
        schema = projects.ProjectSchema(
            project_name=f"new-{n}", selected_repos=["octo/repo"], workflows=[], project_type=project_type
        )
        try:
            projects._validate_create_request(db, schema, owner.github_user)
            results[(standard, rwx, project_type)] = "allowed"
        except HTTPException as exc:
            results[(standard, rwx, project_type)] = _denial(exc)
    return results


def observe_private_project_create(db, account):
    schema = projects.ProjectSchema(
        project_name="private-new", selected_repos=["octo/repo"], workflows=[],
        repository_visibility_scope="private",
    )
    try:
        projects._validate_create_request(db, schema, account.github_user)
        return "allowed"
    except HTTPException as exc:
        return _denial(exc)


def observe_secrets(db, account):
    db.add(Project(project_name="parity", project_code="PAR", user_id=account.user_id))
    db.commit()
    results = {}
    for current, new in SECRET_SCENARIOS:
        secrets = [{"secret_key": f"K{i}", "secret_value": "v"} for i in range(new)]
        with patch("github_secrets.count_project_secrets", AsyncMock(return_value=current)):
            results[(current, new)] = asyncio.run(
                github_secrets._validate_account_limits(
                    account.github_user, "parity", secrets, ["octo/repo"], db
                )
            )
    return results


def observe_env_vars(db, account):
    results = {}
    for current, new in ENV_VAR_SCENARIOS:
        count = AsyncMock(return_value=current)
        new_vars = AsyncMock(return_value=[{"env_key": f"K{i}"} for i in range(new)])
        with patch("github_env_vars.count_project_env_vars", count), \
                patch("github_env_vars._get_truly_new_variables", new_vars):
            result = asyncio.run(github_env_vars._check_free_account_limits(
                account, account.github_user, "PAR", ["octo/repo"], [], {}, True, 1, db
            ))
        results[(current, new)] = (result, count.await_count)
    return results


def observe_environment_create(db, account):
    results = {}
    for current, exists in ENV_CREATE_SCENARIOS:
        count = AsyncMock(return_value=current)
        with patch("github_env_vars.count_project_environments", count):
            result = asyncio.run(github_env_vars._check_environment_limit(
                account.github_user, "octo/repo", exists, account
            ))
        results[(current, exists)] = (result, count.await_count)
    return results


def observe_environment_sync(db, account):
    results = {}
    for current in ENV_SYNC_SCENARIOS:
        count = AsyncMock(return_value=current)
        request = AsyncMock()
        request.json = AsyncMock(return_value={
            "user": account.github_user,
            "project_name": "parity",
            "repo_names": ["octo/a", "octo/b"],
            "environment_name": "staging",
        })
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = account
        with patch("github_env_vars.require_project_write_access"), \
                patch.dict("github_env_vars.user_tokens", {account.github_user: "token"}), \
                patch("github_env_vars.count_project_environments", count), \
                patch("github_env_vars._find_existing_environments", AsyncMock(return_value={})), \
                patch("github_env_vars.httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.get = AsyncMock(return_value=Mock(status_code=404))
            result = asyncio.run(github_env_vars.sync_environment(request, session))
        results[current] = (result, count.await_count)
    return results


GATES = {
    "effective_tier": observe_effective_tier,
    "run_history_days": observe_run_history_days,
    "private_scope": observe_private_scope,
    "repo_limit": observe_repo_limit,
    "project_create": observe_project_create,
    "private_project_create": observe_private_project_create,
    "secrets": observe_secrets,
    "env_vars": observe_env_vars,
    "environment_create": observe_environment_create,
    "environment_sync": observe_environment_sync,
}


@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("tier", TIERS)
def test_self_hosted_gate_parity(gate, tier, test_db, monkeypatch):
    monkeypatch.setattr(license, "get_installation_tier", lambda: tier)
    account = make_account(test_db, tier)
    assert GATES[gate](test_db, account) == SELF_HOSTED_EXPECTED[tier][gate]


SELF_HOSTED_EXPECTED = {'free': {'effective_tier': 'free',
          'run_history_days': 30,
          'private_scope': (True, None),
          'repo_limit': {10: (True, None),
                         11: (False,
                              'Self-hosted beta allows up to 10 repositories per project. Paid plans are not '
                              'available during the self-hosted beta.'),
                         50: (False,
                              'Self-hosted beta allows up to 10 repositories per project. Paid plans are not '
                              'available during the self-hosted beta.'),
                         51: (False,
                              'Self-hosted beta allows up to 10 repositories per project. Paid plans are not '
                              'available during the self-hosted beta.')},
          'project_create': {(2, 0, 'standard'): 'allowed',
                             (3, 0, 'standard'): 'allowed',
                             (4, 0, 'standard'): (403,
                                                  'Self-hosted beta allows up to 4 Caller Workflow '
                                                  'Project(s). You have reached the beta limit for Caller '
                                                  'Workflow Projects. Paid plans are not available during '
                                                  'the self-hosted beta.'),
                             (0, 1, 'rwx'): 'allowed',
                             (0, 2, 'rwx'): (403,
                                             'Self-hosted beta allows up to 2 Reusable Workflow Project(s). '
                                             'You have reached the beta limit for Reusable Workflow '
                                             'Projects. Paid plans are not available during the self-hosted '
                                             'beta.'),
                             (9, 0, 'rwx'): 'allowed',
                             (10, 0, 'standard'): (403,
                                                   'Self-hosted beta allows up to 4 Caller Workflow '
                                                   'Project(s). You have reached the beta limit for Caller '
                                                   'Workflow Projects. Paid plans are not available during '
                                                   'the self-hosted beta.')},
          'private_project_create': 'allowed',
          'secrets': {(1, 1): None,
                      (2, 1): None,
                      (5, 1): None,
                      (6, 1): {'error': 'Self-hosted beta allows up to 6 secrets per project. You currently '
                                        'have 6 and are trying to add 1 new secret(s). Paid plans are not '
                                        'available during the self-hosted beta.',
                               'status': 403,
                               'current_count': 6,
                               'new_count': 1,
                               'limit': 6},
                      (10, 1): {'error': 'Self-hosted beta allows up to 6 secrets per project. You currently '
                                         'have 10 and are trying to add 1 new secret(s). Paid plans are not '
                                         'available during the self-hosted beta.',
                                'status': 403,
                                'current_count': 10,
                                'new_count': 1,
                                'limit': 6}},
          'env_vars': {(1, 1): ({}, 1),
                       (1, 2): ({}, 1),
                       (5, 1): ({}, 1),
                       (6, 1): ({'error': 'Self-hosted beta allows up to 6 environment variables per '
                                          'project. You currently have 6 and are trying to add 1 new '
                                          'variable(s). Paid plans are not available during the self-hosted '
                                          'beta.',
                                 'status': 403,
                                 'current_count': 6,
                                 'limit': 6},
                                1)},
          'environment_create': {(1, False): (None, 1),
                                 (2, False): (None, 1),
                                 (2, True): (None, 1),
                                 (5, False): (None, 1),
                                 (6, False): ({'error': 'Self-hosted beta allows up to 6 GitHub environments '
                                                        'per project. You currently have 6 environment(s). '
                                                        'Paid plans are not available during the self-hosted '
                                                        'beta.',
                                               'status': 403,
                                               'current_count': 6,
                                               'limit': 6},
                                              1)},
          'environment_sync': {0: ({'error': "Environment 'staging' not found in any repository",
                                    'status': 404},
                                   1),
                               1: ({'error': "Environment 'staging' not found in any repository",
                                    'status': 404},
                                   1),
                               4: ({'error': "Environment 'staging' not found in any repository",
                                    'status': 404},
                                   1),
                               5: ({'error': 'Self-hosted beta allows up to 6 GitHub environments per '
                                             'project. Syncing would create 2 new environment(s), but you '
                                             'already have 5. Paid plans are not available during the '
                                             'self-hosted beta.',
                                    'status': 403,
                                    'current_count': 5,
                                    'limit': 6},
                                   1)}},
 'professional': {'effective_tier': 'professional',
                  'run_history_days': 30,
                  'private_scope': (True, None),
                  'repo_limit': {10: (True, None),
                                 11: (True, None),
                                 50: (True, None),
                                 51: (False,
                                      'Self-hosted beta allows up to 50 repositories per project. Paid plans '
                                      'are not available during the self-hosted beta.')},
                  'project_create': {(2, 0, 'standard'): 'allowed',
                                     (3, 0, 'standard'): 'allowed',
                                     (4, 0, 'standard'): (403,
                                                          'Self-hosted beta allows up to 4 Caller Workflow '
                                                          'Project(s). You have reached the beta limit for '
                                                          'Caller Workflow Projects. Paid plans are not '
                                                          'available during the self-hosted beta.'),
                                     (0, 1, 'rwx'): 'allowed',
                                     (0, 2, 'rwx'): (403,
                                                     'Self-hosted beta allows up to 2 Reusable Workflow '
                                                     'Project(s). You have reached the beta limit for '
                                                     'Reusable Workflow Projects. Paid plans are not '
                                                     'available during the self-hosted beta.'),
                                     (9, 0, 'rwx'): 'allowed',
                                     (10, 0, 'standard'): (403,
                                                           'Self-hosted beta allows up to 4 Caller Workflow '
                                                           'Project(s). You have reached the beta limit for '
                                                           'Caller Workflow Projects. Paid plans are not '
                                                           'available during the self-hosted beta.')},
                  'private_project_create': 'allowed',
                  'secrets': {(1, 1): None,
                              (2, 1): None,
                              (5, 1): None,
                              (6, 1): {'error': 'Self-hosted beta allows up to 6 secrets per project. You '
                                                'currently have 6 and are trying to add 1 new secret(s). '
                                                'Paid plans are not available during the self-hosted beta.',
                                       'status': 403,
                                       'current_count': 6,
                                       'new_count': 1,
                                       'limit': 6},
                              (10, 1): {'error': 'Self-hosted beta allows up to 6 secrets per project. You '
                                                 'currently have 10 and are trying to add 1 new secret(s). '
                                                 'Paid plans are not available during the self-hosted beta.',
                                        'status': 403,
                                        'current_count': 10,
                                        'new_count': 1,
                                        'limit': 6}},
                  'env_vars': {(1, 1): ({}, 1),
                               (1, 2): ({}, 1),
                               (5, 1): ({}, 1),
                               (6, 1): ({'error': 'Self-hosted beta allows up to 6 environment variables per '
                                                  'project. You currently have 6 and are trying to add 1 new '
                                                  'variable(s). Paid plans are not available during the '
                                                  'self-hosted beta.',
                                         'status': 403,
                                         'current_count': 6,
                                         'limit': 6},
                                        1)},
                  'environment_create': {(1, False): (None, 1),
                                         (2, False): (None, 1),
                                         (2, True): (None, 1),
                                         (5, False): (None, 1),
                                         (6, False): ({'error': 'Self-hosted beta allows up to 6 GitHub '
                                                                'environments per project. You currently '
                                                                'have 6 environment(s). Paid plans are not '
                                                                'available during the self-hosted beta.',
                                                       'status': 403,
                                                       'current_count': 6,
                                                       'limit': 6},
                                                      1)},
                  'environment_sync': {0: ({'error': "Environment 'staging' not found in any repository",
                                            'status': 404},
                                           1),
                                       1: ({'error': "Environment 'staging' not found in any repository",
                                            'status': 404},
                                           1),
                                       4: ({'error': "Environment 'staging' not found in any repository",
                                            'status': 404},
                                           1),
                                       5: ({'error': 'Self-hosted beta allows up to 6 GitHub environments '
                                                     'per project. Syncing would create 2 new '
                                                     'environment(s), but you already have 5. Paid plans are '
                                                     'not available during the self-hosted beta.',
                                            'status': 403,
                                            'current_count': 5,
                                            'limit': 6},
                                           1)}},
 'enterprise': {'effective_tier': 'enterprise',
                'run_history_days': 30,
                'private_scope': (True, None),
                'repo_limit': {10: (True, None), 11: (True, None), 50: (True, None), 51: (True, None)},
                'project_create': {(2, 0, 'standard'): 'allowed',
                                   (3, 0, 'standard'): 'allowed',
                                   (4, 0, 'standard'): (403,
                                                        'Self-hosted beta allows up to 4 Caller Workflow '
                                                        'Project(s). You have reached the beta limit for '
                                                        'Caller Workflow Projects. Paid plans are not '
                                                        'available during the self-hosted beta.'),
                                   (0, 1, 'rwx'): 'allowed',
                                   (0, 2, 'rwx'): (403,
                                                   'Self-hosted beta allows up to 2 Reusable Workflow '
                                                   'Project(s). You have reached the beta limit for Reusable '
                                                   'Workflow Projects. Paid plans are not available during '
                                                   'the self-hosted beta.'),
                                   (9, 0, 'rwx'): 'allowed',
                                   (10, 0, 'standard'): (403,
                                                         'Self-hosted beta allows up to 4 Caller Workflow '
                                                         'Project(s). You have reached the beta limit for '
                                                         'Caller Workflow Projects. Paid plans are not '
                                                         'available during the self-hosted beta.')},
                'private_project_create': 'allowed',
                'secrets': {(1, 1): None,
                            (2, 1): None,
                            (5, 1): None,
                            (6, 1): {'error': 'Self-hosted beta allows up to 6 secrets per project. You '
                                              'currently have 6 and are trying to add 1 new secret(s). Paid '
                                              'plans are not available during the self-hosted beta.',
                                     'status': 403,
                                     'current_count': 6,
                                     'new_count': 1,
                                     'limit': 6},
                            (10, 1): {'error': 'Self-hosted beta allows up to 6 secrets per project. You '
                                               'currently have 10 and are trying to add 1 new secret(s). '
                                               'Paid plans are not available during the self-hosted beta.',
                                      'status': 403,
                                      'current_count': 10,
                                      'new_count': 1,
                                      'limit': 6}},
                'env_vars': {(1, 1): ({}, 1),
                             (1, 2): ({}, 1),
                             (5, 1): ({}, 1),
                             (6, 1): ({'error': 'Self-hosted beta allows up to 6 environment variables per '
                                                'project. You currently have 6 and are trying to add 1 new '
                                                'variable(s). Paid plans are not available during the '
                                                'self-hosted beta.',
                                       'status': 403,
                                       'current_count': 6,
                                       'limit': 6},
                                      1)},
                'environment_create': {(1, False): (None, 1),
                                       (2, False): (None, 1),
                                       (2, True): (None, 1),
                                       (5, False): (None, 1),
                                       (6, False): ({'error': 'Self-hosted beta allows up to 6 GitHub '
                                                              'environments per project. You currently have '
                                                              '6 environment(s). Paid plans are not '
                                                              'available during the self-hosted beta.',
                                                     'status': 403,
                                                     'current_count': 6,
                                                     'limit': 6},
                                                    1)},
                'environment_sync': {0: ({'error': "Environment 'staging' not found in any repository",
                                          'status': 404},
                                         1),
                                     1: ({'error': "Environment 'staging' not found in any repository",
                                          'status': 404},
                                         1),
                                     4: ({'error': "Environment 'staging' not found in any repository",
                                          'status': 404},
                                         1),
                                     5: ({'error': 'Self-hosted beta allows up to 6 GitHub environments per '
                                                   'project. Syncing would create 2 new environment(s), but '
                                                   'you already have 5. Paid plans are not available during '
                                                   'the self-hosted beta.',
                                          'status': 403,
                                          'current_count': 5,
                                          'limit': 6},
                                         1)}}}
