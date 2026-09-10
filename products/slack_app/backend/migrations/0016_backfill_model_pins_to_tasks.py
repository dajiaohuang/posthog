from django.db import migrations


def backfill_model_pins(apps, schema_editor):
    """Move personal Slack model pins into the central per-(user, project) tasks config.

    The App Home now writes model preferences only to that config (the Slack-local pin
    store is being retired), so pins that predate the switch are copied over and cleared
    here. A pin lands in every connected project where its owner is a member, matching
    how it used to apply regardless of routing. Conflicts resolve most-recent-wins on
    ``updated_at``, so a central preference set, or deliberately cleared, after the pin
    keeps its value. The Slack identity resolves the same two ways the inbound event
    path maps it: explicit account link first, then profile-cache email match. A pin we
    cannot attribute to a PostHog user is left in place; it cannot influence a run
    (mentions from unresolved users are dropped) and goes away with the column.

    Model ids and efforts are copied as stored, without catalogue validation: run
    resolution is lenient about stale ids, and a migration must not depend on a live
    model catalogue.
    """
    SlackSettings = apps.get_model("slack_app", "SlackSettings")
    SlackUserProfileCache = apps.get_model("slack_app", "SlackUserProfileCache")
    Integration = apps.get_model("posthog", "Integration")
    OrganizationMembership = apps.get_model("posthog", "OrganizationMembership")
    Team = apps.get_model("posthog", "Team")
    User = apps.get_model("posthog", "User")
    UserIntegration = apps.get_model("posthog", "UserIntegration")
    UserTasksConfig = apps.get_model("tasks", "UserTasksConfig")

    def resolve_user(workspace_id, slack_user_id):
        link = (
            UserIntegration.objects.filter(
                kind="slack",
                integration_id=slack_user_id,
                config__slack_team_id=workspace_id,
                user__is_active=True,
            )
            .order_by("-created_at")
            .select_related("user")
            .first()
        )
        if link is not None:
            return link.user
        email = (
            SlackUserProfileCache.objects.filter(
                integration__kind="slack",
                integration__integration_id=workspace_id,
                slack_user_id=slack_user_id,
            )
            .exclude(email=None)
            .exclude(email="")
            .values_list("email", flat=True)
            .first()
        )
        if not email:
            return None
        return User.objects.filter(email__iexact=email, is_active=True).first()

    def member_canonical_team_ids(workspace_id, user):
        teams = list(
            Integration.objects.filter(kind="slack", integration_id=workspace_id).values_list(
                "team_id", "team__organization_id"
            )
        )
        member_org_ids = set(
            OrganizationMembership.objects.filter(
                user=user, organization_id__in={org_id for _, org_id in teams}
            ).values_list("organization_id", flat=True)
        )
        member_team_ids = {team_id for team_id, org_id in teams if org_id in member_org_ids}
        # UserTasksConfig rows are keyed on the project root team.
        pairs = Team.objects.filter(id__in=member_team_ids).values_list("id", "parent_team_id")
        return sorted({parent_id or team_id for team_id, parent_id in pairs})

    rows = SlackSettings.objects.filter(slack_user_id__isnull=False, ai_preferences__isnull=False)
    for row in rows.iterator():
        prefs = row.ai_preferences or {}
        # Both halves of the pair or the row was never a configured pin.
        payload = {key: prefs[key] for key in ("runtime_adapter", "model", "reasoning_effort") if prefs.get(key)}
        if "runtime_adapter" not in payload or "model" not in payload:
            continue
        user = resolve_user(row.slack_workspace_id, row.slack_user_id)
        if user is None:
            continue
        for team_id in member_canonical_team_ids(row.slack_workspace_id, user):
            config = UserTasksConfig._base_manager.filter(team_id=team_id, user_id=user.id).first()
            if config is not None and config.updated_at >= row.updated_at:
                continue
            if config is None:
                UserTasksConfig._base_manager.create(team_id=team_id, user_id=user.id, ai_run_preferences=payload)
            else:
                config.ai_run_preferences = payload
                config.save(update_fields=["ai_run_preferences", "updated_at"])
        # Accounted for on every reachable project; clear the pin so display and
        # runs stop preferring a store that no longer takes writes.
        row.ai_preferences = None
        row.save(update_fields=["ai_preferences", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("slack_app", "0015_backfill_slack_thread_conversation_type"),
        # UserTasksConfig and the posthog identity models the mapping reads.
        ("tasks", "0115_teamtasksconfig_usertasksconfig"),
        ("posthog", "0001_squash_2026_09_07_initial"),
    ]

    operations = [
        migrations.RunPython(backfill_model_pins, migrations.RunPython.noop, elidable=True),
    ]
