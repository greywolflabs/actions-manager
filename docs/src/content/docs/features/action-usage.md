---
title: Action Usage
description: See which actions your managed workflows use and at which version, and update them to the latest release from the page, a workflow at a time or all at once.
---

See which actions your managed workflows use and at which version, which references are not pinned
to a commit SHA, and update them to the latest release without leaving the page.

---

## Why it matters

A `uses:` reference such as `actions/checkout@v4` points at a tag, and a tag can be moved after you
have reviewed it. A reference to a full 40-character commit SHA cannot be. GitHub can now
[block actions and enforce SHA pinning for a whole organization](https://github.blog/changelog/2025-08-15-github-actions-policy-now-supports-blocking-and-sha-pinning-actions/):
once that is on, every workflow that uses a tag or a branch fails.

Action Usage answers three questions about your managed workflows:

- Which actions do they use, and at which versions?
- Which repositories are still on an old version, such as `actions/checkout@v3`?
- Which workflows would fail under an organization SHA-pinning policy?

## The Action Usage page

Open it from the **Action Usage** tab in the app header, after **Managed Actions**.

![The Action Usage page: three counts (3 actions in use, 4 workflows not pinned to a SHA with a Show only these link, 5 workflows read), a filter box with an All and a Not pinned switch and Collapse all and Expand all links, then three full-width rows. acme-corp/shared-workflows is open with a note that it has no release to update to and a table of its two workflows. actions/checkout is open with "Latest release v7.1.0" above a table whose columns are Workflow, Project, Repositories and Version, with an Update to v7.1.0 button on two rows and Under review on the third. actions/setup-node is closed.](../assets/screenshots/action-usage/inventory.png)

The three counts at the top are the actions in use, the workflows that use a tag or a branch (and
would fail under an organization SHA-pinning policy), and the workflows read. Choose the second
count to show only the actions that have such a workflow; it then reads **Show all**, and choosing it
again, or **All** beside the filter, brings every action back. While the list is filtered, a line above
it says how many actions are showing, with a **Show all** link that also clears the search. A tag or a
branch can be moved after you review it, and a full commit SHA cannot; the small **i** under the counts
says so.

Each action is a row. **Choose anywhere on a row to open it**, and again to close it. **Expand all**
and **Collapse all** above the list do every action at once. A row shows:

- **The action**, `owner/repo`, and a chip for each version in use, such as `v3 × 2`: the ref, and how
  many workflows use it. A tag or a branch is amber, a full commit SHA is green with a check. A SHA is
  shortened to its first seven characters; hover a chip to see all of it.
- **How many workflows and repositories** the action reaches, and a badge: **All pinned**, or
  **N not pinned**.
- **Update all**, which updates every workflow that uses the action.

Use **Filter actions** to find one by name, or **Not pinned** to hide the actions that are fully
pinned.

### An open action

Open a row to see a table of the workflows that use it. Its columns are **Workflow** (the file name
on GitHub, and what it calls inside the action when that is a path, such as a reusable workflow),
**Project**, **Repositories** (hover for their names) and **Version**, with the update button at the
end. The workflows that need an update come first, then they are ordered by project and file.

A workflow that is a repository's own copy is marked **own copy**, and one in a Reusable Workflow
Project is marked **reusable workflow**.

## Updating a workflow

Opening an action looks up its **latest release** on GitHub, with your own token, once for each
action you open: the newest published release, which leaves out drafts and prereleases, or for an
action that publishes no releases, its highest stable version tag. It is shown once above the table,
as **Latest release v7.1.0 → 3d3c42e**. Each workflow that isn't on it then has an
**Update to v7.1.0** button, and one that is shows **Up to date**.

![The actions/checkout row open, with the Update to v7.1.0 button on its first workflow pressed and its menu showing two choices: Update and review, which opens it in the editor with nothing saved until you commit, and Update and commit locally, which saves it in the project now with no pull request](../assets/screenshots/action-usage/update-workflow.png)

If an action has no release or version tag, a note above the table says **No release to update to**
once, and the buttons read **Not available**. To move its workflows to a tag, branch or commit of your
choosing, use **Update all** and then **Change**.

Choose the button for two ways to do it:

- **Update and review** opens the project with that workflow selected and the new version already
  in the editor, marked **Unsaved**. Nothing is saved until you choose **Commit Locally**, so you can
  look the change over, edit it further, or leave it. It isn't kept if you refresh the page.
- **Update and commit locally** saves the workflow in the project straight away, the same as
  **Commit Locally** in the editor. The project is left with changes to deliver, so **Create Pull
  Requests** is ready when you are. The page reloads and the workflow moves to the new version.

Neither opens a pull request. Updating changes the workflow in ActionsManager; delivering it is the
project's own **Create Pull Requests**, as for any other change.

A workflow has no button when:

- you can't edit its project;
- it is a repository's own copy of the workflow (marked **own copy**), which keeps its copy;
- the workflow has an open pull request (the button reads **Under review**). Close or merge that
  first, because saving it would put the workflow out of step with its pull request.

A reusable workflow in a Reusable Workflow Project, marked **reusable workflow**, can be updated the
same way. Reusable workflows linked into a project belong to the project that owns them and are not
listed.

## Updating all of them

**Update all** on a row updates every workflow that uses the action, in every project you can edit,
and commits each one locally. No pull requests are opened.

![The Update all dialog for actions/checkout: "Latest release v7.1.0 → 3d3c42e" with a Change button, "2 projects · 2 workflows selected", a Node services row with a checkbox, a note that acme-corp/ledger-service has its own copy and keeps it, and two workflow files showing the Now and After line, one noted as under review and not updated, a Web apps row with a checkbox, and an Update 2 workflows button](../assets/screenshots/action-usage/update-all.png)

1. The dialog opens already showing the change to the latest release. The tag is looked up once and
   written as its commit SHA. Nothing changes yet.
2. Check what it will do. Each project has a checkbox, so you can leave one out, and a count of the
   workflows it will update. **Show changes** lists the workflow files and the line in each, as it is
   now and as it would be. A workflow with an open pull request is shown and marked as not updated.
3. Choose **Update workflows**. ActionsManager takes the checked projects one after another and saves
   their workflows. The result of each project shows beside it, so a failure in one doesn't stop the
   others. The page reloads when it is done.

To update to something other than the latest release, such as an older tag, a branch or a commit
SHA, choose **Change**, type it in and choose **Preview**. **Use latest release** goes back to the
default. If an action has no release or version tag to default to, the dialog says so and shows the
field for you. The head of a branch is never chosen for you: that is code nobody has reviewed.

### What gets written

- **The SHA, with the version beside it.** `uses: actions/checkout@v4` becomes
  `uses: actions/checkout@<sha> # v5.0.0`. This is the form Dependabot and Renovate read, so they can
  still bump it later. A comment already on the line is replaced, not added to. If you enter a bare
  SHA there is no version to write, and a version comment left from the old ref is removed.
- **Every use of the action in the workflow.** Steps, and jobs that call a reusable workflow in that
  repository. A path into the action, such as `github/codeql-action/init`, is kept.
- **Nothing else.** Comments, a `uses` inside a `run:` script, other actions and the layout of the
  file are not touched.
- **No change when it is already there.** A workflow already on that SHA with that comment is left
  out, and a project with no workflow to change is not listed.

### Repositories with their own copy

A repository's own copy of a workflow (a per-repository override, created when you resolve drift in
one repository) replaces the project's copy for that repository. Updating changes the project's
copy, so those repositories keep theirs: the dialog names them, and they stay listed under their
old version.

### Who can do it

You can see the page, and preview an update, for the projects you can see. Updating needs to be an
editor of the project, and the server checks your role again for every update.

## What it reads

Action Usage reads the workflows ActionsManager stores, so it shows what ActionsManager will
deliver, not what is on GitHub right now. The inventory makes no calls to GitHub; only looking up
the latest version, when you open an action or an update dialog, does.

- **Steps and reusable workflow calls.** It reads `uses:` on every step and on every job that calls
  a reusable workflow. A `uses` in a comment or a `run:` script is not a reference.
- **Repositories.** A workflow reaches every repository in its project. A repository that runs its
  own copy of a workflow (a per-repository override) is listed under the versions that copy uses,
  and marked **own copy**.
- **Tag or branch.** GitHub is not asked what a ref is, so it is read from the name: a version
  number such as `v4`, `1.2.3` or `v5.0.0-rc1` is called a tag, and anything else that is not a full
  SHA, such as `main`, is called a branch. Either way it is **not pinned**. A short SHA is not
  pinned either: GitHub's policy accepts only the full 40 characters.
- **Local and Docker references.** `./local/action` and `docker://image` references are counted in
  the summary and never flagged.
- **Workflows being removed.** A workflow that a campaign is deleting is left out.

Action Usage lists the projects you can see, by the same rules as everywhere else in the app. See
[Permissions](permissions.html).

## What it doesn't do

It doesn't open pull requests, check for versions newer than the latest release, or read workflows
in a repository that ActionsManager doesn't manage.
