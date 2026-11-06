"""Canonical immutable-snapshot selection and display text shared by personal progress."""

LATEST_ACTIVITY = (
    "WITH latest AS (SELECT DISTINCT ON (key) * FROM console_activity"
    " WHERE flow_id=%s AND version<=%s AND source!='flow'"
    " ORDER BY key,deleted DESC,updated_at DESC,version DESC) "
)


def display_body(body: str, source: str) -> str:
    """Strip only the existing GitHub transport receipt, without loading full text."""
    return (
        f"CASE WHEN {source}='worker' THEN {body} ELSE "
        f"regexp_replace({body}, E'\\n\\n<!-- flowtool:effect:[0-9a-f]{{64}} -->$', '') END"
    )
