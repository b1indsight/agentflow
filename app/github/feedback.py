"""One complete, bounded page for durable feedback polling, without prompt truncation."""

from typing import Any

from app.domain.github_models import FeedbackPage, FeedbackSource, RemoteFeedback
from app.github.pagination import next_cursor

_REVIEWS = """
query($owner:String!,$name:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$name){pullRequest(number:$number){
    reviews(first:50,after:$cursor){
      nodes { fullDatabaseId body updatedAt state author {login __typename} }
      pageInfo {hasNextPage endCursor}
    }
  }}
}
"""


async def read_feedback_page(
    client: Any, repo: str, number: int, source: FeedbackSource, cursor: str | None,
) -> FeedbackPage:
    """Reviews require GraphQL's edit timestamp; REST submitted_at is not a content version."""
    owner, name = repo.split("/")
    if source != "reviews":
        page = int(cursor) if cursor is not None else 1
        if page < 1:
            raise ValueError("invalid comment page")
        rows = (await client.rest.issues.async_list_comments(
            owner, name, number, page=page, per_page=50,
        )).parsed_data
        return FeedbackPage(items=[RemoteFeedback(
            github_id=row.id, author=row.user.login, is_bot=row.user.type == "Bot",
            body=row.body or "", updated_at=row.updated_at,
        ) for row in rows if row.user is not None],
            next_cursor=str(page + 1) if len(rows) == 50 else None)
    data = await client.async_graphql(_REVIEWS, {
        "owner": owner, "name": name, "number": number, "cursor": cursor,
    })
    if data.get("errors"):
        raise ValueError("partial review response")
    connection = data["repository"]["pullRequest"]["reviews"]
    following = next_cursor(connection, cursor)
    return FeedbackPage(items=[RemoteFeedback(
        github_id=node["fullDatabaseId"], author=node["author"]["login"], body=node["body"],
        updated_at=node["updatedAt"], state=node["state"].lower(),
        is_bot=node["author"]["__typename"] == "Bot",
    ) for node in connection["nodes"] if node["author"] is not None], next_cursor=following)
