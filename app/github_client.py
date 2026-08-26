"""GitHub API 平面：githubkit 封装（App 认证）。ARCHITECTURE §4.5、附录 A。

TODO(verify) V1：githubkit 的确切 API（App→installation token、PR、GraphQL resolveReviewThread）。
  下面的调用按 githubkit 的常见形态写，方法名/返回结构落地时对照官方文档校准。
TODO(verify) V3：为 code agent 的 push 签发仓库级、短时、contents:write 的 scoped token。
"""

from __future__ import annotations

from loguru import logger

from app.config import get_settings

# githubkit 采用惰性导入：M0 阶段未装依赖也能 import 本模块（桩）。
try:
    from githubkit import (  # type: ignore
        AppInstallationAuthStrategy,
        GitHub,
    )
except Exception:  # noqa: BLE001
    GitHub = None  # type: ignore
    AppInstallationAuthStrategy = None  # type: ignore


_RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { isResolved } }
}
"""


class GitHubApp:
    """实现 ports.GitHubClient。"""

    def __init__(self) -> None:
        s = get_settings()
        self.app_id = s.github_app_id
        self.private_key = ""
        if s.github_private_key_path.exists():
            self.private_key = s.github_private_key_path.read_text()

    def _client(self, installation_id: int):
        if GitHub is None:
            raise RuntimeError("githubkit not installed")  # TODO(verify) V1
        return GitHub(
            AppInstallationAuthStrategy(self.app_id, self.private_key, installation_id)
        )

    async def installation_token(self, installation_id: int) -> str:
        # githubkit 会用 App JWT 自动换 installation token 并缓存；这里显式取一把用于 git push。
        gh = self._client(installation_id)
        try:
            auth = await gh.auth.async_get_token()  # TODO(verify) V1/V3 精确方法名与 scope
            return auth
        except Exception as e:
            logger.error("installation_token failed: {}", e)
            raise

    async def open_pr(
        self, installation_id: int, repo: str, head: str, base: str, title: str, body: str
    ) -> int:
        owner, name = repo.split("/", 1)
        gh = self._client(installation_id)
        resp = await gh.rest.pulls.async_create(
            owner, name, title=title, head=head, base=base, body=body
        )
        return resp.parsed_data.number  # TODO(verify) V1

    async def comment(self, installation_id: int, repo: str, number: int, body: str) -> None:
        owner, name = repo.split("/", 1)
        gh = self._client(installation_id)
        await gh.rest.issues.async_create_comment(owner, name, number, body=body)

    async def set_labels(
        self, installation_id: int, repo: str, number: int, labels: list[str]
    ) -> None:
        owner, name = repo.split("/", 1)
        gh = self._client(installation_id)
        await gh.rest.issues.async_set_labels(owner, name, number, labels=labels)

    async def request_reviewers(
        self, installation_id: int, repo: str, number: int, reviewers: list[str]
    ) -> None:
        owner, name = repo.split("/", 1)
        gh = self._client(installation_id)
        await gh.rest.pulls.async_request_reviewers(owner, name, number, reviewers=reviewers)

    async def list_review_threads(self, installation_id: int, repo: str, pr: int) -> list[dict]:
        # GraphQL：拿 reviewThreads，映射 comment_id → thread_id。TODO(verify) V1。
        owner, name = repo.split("/", 1)
        gh = self._client(installation_id)
        query = """
        query($owner:String!,$name:String!,$pr:Int!){
          repository(owner:$owner,name:$name){
            pullRequest(number:$pr){
              reviewThreads(first:100){ nodes{ id isResolved comments(first:20){ nodes{ databaseId }}}}
            }}}
        """
        data = await gh.async_graphql(query, {"owner": owner, "name": name, "pr": pr})
        return data["repository"]["pullRequest"]["reviewThreads"]["nodes"]

    async def resolve_review_thread(self, installation_id: int, thread_id: str) -> None:
        gh = self._client(installation_id)
        await gh.async_graphql(_RESOLVE_THREAD_MUTATION, {"threadId": thread_id})
