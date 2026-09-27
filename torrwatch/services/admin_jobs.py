"""Administrative client operations executed by the ordinary leased worker."""

import json

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from torrwatch.clients.types import ClientErrorCode, TorrentClientError
from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.models import Job, TorrentClient
from torrwatch.domain.enums import JobStatus, JobType
from torrwatch.services.delivery import TorrentClientRepository, adapter_for
from torrwatch.services.jobs import JobExecutionFailure, now_utc


def enqueue_client_test(database: Database, client_id: int) -> bool:
    with database.session() as session:
        client = session.get(TorrentClient, client_id)
        if client is None or not client.enabled:
            raise ValueError("Клиент не найден или выключен.")
        result = session.execute(
            insert(Job)
            .values(
                job_type=JobType.CLIENT_TEST,
                payload_json=json.dumps({"client_id": client_id}),
                status=JobStatus.PENDING,
                active_key=f"client-test:{client_id}",
                next_attempt_at=now_utc(),
            )
            .on_conflict_do_nothing(index_elements=[Job.active_key])
        )
        return bool(getattr(result, "rowcount", 0))


class AdminJobService:
    def __init__(self, database: Database, secrets: SecretBox) -> None:
        self.database = database
        self.clients = TorrentClientRepository(database, secrets)

    def owned(self, job: Job, owner: str) -> bool:
        with self.database.session() as session:
            return (
                session.scalar(
                    select(Job.id).where(
                        Job.id == job.id,
                        Job.status == JobStatus.RUNNING,
                        Job.worker_id == owner,
                        Job.lease_expires_at > now_utc(),
                    )
                )
                is not None
            )

    async def handle(self, job: Job, owner: str) -> None:
        if not self.owned(job, owner):
            raise JobExecutionFailure("Владение задачей утрачено.", retryable=True)
        payload = json.loads(job.payload_json)
        try:
            config = self.clients.config(payload["client_id"])
            if job.job_type == JobType.CLIENT_REMOVE and payload.get("endpoint") != config.base_url:
                raise JobExecutionFailure(
                    "Адрес клиента изменён после подтверждения удаления. Операция остановлена.",
                    retryable=False,
                )
            adapter = adapter_for(config)
            if job.job_type == JobType.CLIENT_TEST:
                result = await adapter.test_connection()
                if not result.connected:
                    raise TorrentClientError(ClientErrorCode.UNAVAILABLE, "Connection failed")
            elif job.job_type == JobType.CLIENT_REMOVE:
                identity = payload["infohash"]
                if await adapter.inspect(identity) is not None:
                    if not self.owned(job, owner):
                        raise JobExecutionFailure("Владение задачей утрачено.", retryable=True)
                    await adapter.remove(identity, delete_data=False)
                if await adapter.inspect(identity) is not None:
                    raise TorrentClientError(ClientErrorCode.UNAVAILABLE, "Removal not confirmed")
            else:
                raise JobExecutionFailure("Неизвестная операция.", retryable=False)
        except TorrentClientError as error:
            messages = {
                ClientErrorCode.AUTH_FAILED: "Клиент отклонил вход. Проверьте логин и пароль.",
                ClientErrorCode.INVALID_CONFIGURATION: "Проверьте настройки и доступность клиента.",
                ClientErrorCode.UNAVAILABLE: "Клиент недоступен. Проверьте адрес и сеть.",
                ClientErrorCode.PROTOCOL_ERROR: "Клиент вернул неожиданный ответ.",
            }
            raise JobExecutionFailure(
                messages[error.code],
                retryable=error.code
                in {ClientErrorCode.UNAVAILABLE, ClientErrorCode.PROTOCOL_ERROR}
                and job.attempts < 5,
            ) from None
