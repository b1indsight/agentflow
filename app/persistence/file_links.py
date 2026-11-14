"""Persistent opaque file links; worker paths and contents are never stored here."""

from uuid import uuid4

from pydantic import BaseModel

from app.persistence.repository import Repository


class FileLink(BaseModel):
    link_id: str
    worker_id: str
    file_id: str


class FileLinkStore(Repository):
    """Map public link IDs to authenticated worker identities and private handles."""

    def register(self, worker_id: str, file_id: str) -> FileLink:
        with self._db.connection() as connection:
            row = connection.execute(
                "INSERT INTO worker_file_links(link_id,worker_id,file_id) VALUES (%s,%s,%s) "
                "ON CONFLICT(worker_id,file_id) DO UPDATE SET file_id=EXCLUDED.file_id "
                "RETURNING link_id,worker_id,file_id",
                (uuid4().hex, worker_id, file_id),
            ).fetchone()
        return FileLink.model_validate(row)

    def get(self, link_id: str) -> FileLink | None:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT link_id,worker_id,file_id FROM worker_file_links WHERE link_id=%s",
                (link_id,),
            ).fetchone()
        return FileLink.model_validate(row) if row else None
