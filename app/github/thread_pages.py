"""Durable outer and nested comment continuations for one-request thread pages."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.github.threads import THREAD_FIELDS

_THREAD_PAGE_QUERY = """
query($owner:String!,$name:String!,$pr:Int!,$after:String,$comments:String){
repository(owner:$owner,name:$name){pullRequest(number:$pr){
reviewThreads(first:1,after:$after){nodes{
""" + THREAD_FIELDS + "} pageInfo {hasNextPage endCursor}}}}}"


class ThreadCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    after: str | None = None
    thread_id: str | None = None
    comments: str | None = None
    following: str | None = None
