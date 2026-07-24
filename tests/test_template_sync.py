"""Template synchronization job, targeting, retry, and bootstrap tests."""

from importlib import import_module
from typing import Self
from uuid import UUID

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from coder_manager import tasks, worker_database
from coder_manager.config import Settings
from coder_manager.crypto import InstancePasswordCipher
from coder_manager.domains.coder import CoderTemplate, CoderTemplateVersion
from coder_manager.domains.template_source import TemplateArchive
from coder_manager.models import (
    Instance,
    InstanceStatus,
    JobExecution,
    JobStatus,
    Template,
    TemplateDeployment,
    TemplateDeploymentStatus,
    TemplateSyncStatus,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_CREATE_STEP_04,
    INSTANCE_CREATE_STEP_04_TASK,
)
from coder_manager.tasks.template._sync import (
    TemplateSourceSnapshot,
    sync_template_target,
)
from tests.conftest import TEST_CRYPTO_KEY
from tests.test_workspaces import create_instance, create_template, set_instance_status


def configure_worker(
    monkeypatch: pytest.MonkeyPatch,
    sync_session_maker: sessionmaker[Session],
) -> None:
    """Route worker persistence to the isolated test database."""

    monkeypatch.setattr(
        worker_database,
        "get_worker_session_maker",
        lambda: sync_session_maker,
    )


async def queued_template_job(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    template_id: object,
) -> UUID:
    """Queue a synchronization and return its private durable job identifier."""

    response = await client.post(f"/api/v1/templates/{template_id}/sync")
    assert response.status_code == 202
    async with session_maker() as session:
        template = await session.get(Template, UUID(str(template_id)))
        assert template is not None
        assert template.job_id is not None
        return template.job_id


async def test_template_sync_targets_scope_and_retries_partial_failure(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Target only matching applications and retry an errored durable job."""

    configure_worker(monkeypatch, sync_session_maker)
    first = await create_instance(client, "FIRST")
    second = await create_instance(client, "SECOND")
    await set_instance_status(session_maker, first["id"])
    await set_instance_status(session_maker, second["id"])
    template = await create_template(
        client,
        name="Application Python",
        scope="application",
        application="FIRST",
    )
    job_id = await queued_template_job(client, session_maker, template["id"])
    sync_module = import_module("coder_manager.tasks.template.sync.step_01_sync_template")
    archive = TemplateArchive(commit="a" * 40, content=b"ustar")
    monkeypatch.setattr(sync_module, "fetch_template_archive", lambda _snapshot: archive)
    targeted: list[UUID] = []

    def fail_target(
        _snapshot: TemplateSourceSnapshot,
        _archive: TemplateArchive,
        instance_id: UUID,
        _session_factory: sessionmaker[Session],
        *,
        heartbeat: object,
    ) -> bool:
        """Fail the matching target once after recording its identity."""

        del heartbeat
        targeted.append(instance_id)
        message = "remote unavailable"
        raise RuntimeError(message)

    monkeypatch.setattr(sync_module, "sync_template_target", fail_target)
    with pytest.raises(RuntimeError, match="1 target"):
        tasks.step_01_sync_template.run(str(job_id))

    assert targeted == [UUID(str(first["id"]))]
    async with session_maker() as session:
        stored_template = await session.get(Template, UUID(str(template["id"])))
        job = await session.get(JobExecution, job_id)
        assert stored_template is not None
        assert job is not None
        assert stored_template.sync_status is TemplateSyncStatus.ERROR
        assert job.status is JobStatus.ERROR

    monkeypatch.setattr(
        sync_module,
        "sync_template_target",
        lambda *_args, **_kwargs: True,
    )
    assert tasks.step_01_sync_template.run(str(job_id)) == {"status": "success"}
    async with session_maker() as session:
        stored_template = await session.get(Template, UUID(str(template["id"])))
        assert stored_template is not None
        assert stored_template.sync_status is TemplateSyncStatus.SUCCESS


async def test_applied_commit_skips_all_remote_calls(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use only current deployment state to avoid republishing an unchanged HEAD."""

    instance = await create_instance(client, "UNCHANGED")
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id)
    template = await create_template(client)
    template_id = UUID(str(template["id"]))
    commit = "b" * 40
    async with session_maker() as session:
        stored_instance = await session.get(Instance, instance_id)
        assert stored_instance is not None
        stored_instance.password_enc = InstancePasswordCipher(SecretStr(TEST_CRYPTO_KEY)).encrypt(
            SecretStr("password"), instance_id
        )
        session.add(
            TemplateDeployment(
                template_id=template_id,
                instance_id=instance_id,
                target_commit=commit,
                applied_commit=commit,
                status=TemplateDeploymentStatus.SUCCESS,
            )
        )
        await session.commit()

    sync_helpers = import_module("coder_manager.tasks.template._sync")
    monkeypatch.setattr(
        sync_helpers,
        "CoderClient",
        lambda *_args, **_kwargs: pytest.fail("Coder must not be contacted"),
    )
    snapshot = TemplateSourceSnapshot(
        id=template_id,
        name="Python",
        coder_name="python",
        git_url="git@git.example.com:templates/python.git",
        source_path=".",
        branch="main",
    )
    assert (
        sync_template_target(
            snapshot,
            TemplateArchive(commit=commit, content=b"ustar"),
            instance_id,
            sync_session_maker,
        )
        is False
    )


async def test_target_sync_creates_first_remote_template(  # noqa: C901
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist remote identifiers around a first upload and imported version."""

    instance = await create_instance(client, "FIRST REMOTE")
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id)
    template = await create_template(client)
    template_id = UUID(str(template["id"]))
    async with session_maker() as session:
        stored_instance = await session.get(Instance, instance_id)
        assert stored_instance is not None
        stored_instance.password_enc = InstancePasswordCipher(SecretStr(TEST_CRYPTO_KEY)).encrypt(
            SecretStr("password"), instance_id
        )
        await session.commit()

    organization_id = UUID("10000000-0000-0000-0000-000000000001")
    expected_version_id = UUID("20000000-0000-0000-0000-000000000002")
    remote_template_id = UUID("30000000-0000-0000-0000-000000000003")
    calls: list[str] = []

    class FakeCoderClient:
        """Record the first-publication Coder operations."""

        def __init__(self, instance_url: str) -> None:
            """Validate the selected instance endpoint."""

            assert instance_url == instance["instance_url"]

        def __enter__(self) -> Self:
            """Enter the fake client context."""

            return self

        def __exit__(self, *_args: object) -> None:
            """Leave the fake client context."""

        def authenticate_prepared_admin(self, password: SecretStr) -> None:
            """Validate the decrypted administrator secret."""

            assert password.get_secret_value() == "password"
            calls.append("authenticate")

        def default_organization_id(self) -> UUID:
            """Return the selected default organization."""

            return organization_id

        def template_by_name(self, selected_organization: UUID, name: str) -> None:
            """Report that no remote template exists yet."""

            assert (selected_organization, name) == (organization_id, "python")

        def upload_template_archive(self, content: bytes) -> UUID:
            """Record and identify the uploaded source archive."""

            assert content == b"ustar"
            calls.append("upload")
            return UUID("40000000-0000-0000-0000-000000000004")

        def create_template_version(
            self,
            selected_organization: UUID,
            *,
            file_id: UUID,
            version_name: str,
            template_id: UUID | None,
        ) -> CoderTemplateVersion:
            """Create the pending native Coder template version."""

            assert selected_organization == organization_id
            assert file_id == UUID("40000000-0000-0000-0000-000000000004")
            assert version_name == f"git-{'d' * 40}"
            assert template_id is None
            calls.append("create-version")
            return CoderTemplateVersion(
                expected_version_id,
                "pending",
                archived=False,
            )

        def wait_template_version(
            self,
            selected_version: UUID,
            *,
            timeout_seconds: float,
            poll_interval_seconds: float,
            heartbeat: object,
        ) -> CoderTemplateVersion:
            """Complete the import while exercising the heartbeat."""

            assert selected_version == expected_version_id
            assert timeout_seconds > poll_interval_seconds
            assert callable(heartbeat)
            heartbeat()
            calls.append("wait")
            return CoderTemplateVersion(
                expected_version_id,
                "succeeded",
                archived=False,
            )

        def create_template(
            self,
            selected_organization: UUID,
            *,
            name: str,
            display_name: str,
            version_id: UUID,
        ) -> CoderTemplate:
            """Create the first remote template from the imported version."""

            assert (selected_organization, name, display_name, version_id) == (
                organization_id,
                "python",
                "Python",
                expected_version_id,
            )
            calls.append("create-template")
            return CoderTemplate(remote_template_id)

    sync_helpers = import_module("coder_manager.tasks.template._sync")
    monkeypatch.setattr(sync_helpers, "CoderClient", FakeCoderClient)
    monkeypatch.setattr(
        sync_helpers,
        "get_settings",
        lambda: Settings(crypto_key=TEST_CRYPTO_KEY),
    )
    heartbeat_calls: list[bool] = []
    changed = sync_template_target(
        TemplateSourceSnapshot(
            id=template_id,
            name="Python",
            coder_name="python",
            git_url="git@git.example.com:templates/python.git",
            source_path=".",
            branch="main",
        ),
        TemplateArchive(commit="d" * 40, content=b"ustar"),
        instance_id,
        sync_session_maker,
        heartbeat=lambda: heartbeat_calls.append(True),
    )

    assert changed is True
    assert heartbeat_calls == [True]
    assert calls == ["authenticate", "upload", "create-version", "wait", "create-template"]
    async with session_maker() as session:
        deployment = await session.scalar(
            select(TemplateDeployment).where(
                TemplateDeployment.template_id == template_id,
                TemplateDeployment.instance_id == instance_id,
            )
        )
        assert deployment is not None
        assert deployment.coder_organization_id == organization_id
        assert deployment.coder_template_id == remote_template_id
        assert deployment.coder_template_version_id == expected_version_id
        assert deployment.target_commit == "d" * 40
        assert deployment.applied_commit == "d" * 40
        assert deployment.status is TemplateDeploymentStatus.SUCCESS


async def test_new_instance_syncs_compatible_templates_before_success(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the bootstrap synchronization step before marking the instance ready."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "BOOTSTRAP")
    instance_id = UUID(str(instance["id"]))
    job_id = UUID(str(instance["job_id"]))
    template = await create_template(
        client,
        name="Bootstrap Python",
        scope="application",
        application="BOOTSTRAP",
    )
    with sync_session_maker() as session:
        stored_instance = session.get(Instance, instance_id)
        job = session.get(JobExecution, job_id)
        assert stored_instance is not None
        assert job is not None
        stored_instance.step = INSTANCE_CREATE_STEP_04
        stored_instance.status = InstanceStatus.PENDING
        job.task_name = INSTANCE_CREATE_STEP_04_TASK
        job.step = INSTANCE_CREATE_STEP_04
        session.commit()

    create_module = import_module("coder_manager.tasks.instance.create.step_04_sync_templates")
    archive = TemplateArchive(commit="c" * 40, content=b"ustar")
    monkeypatch.setattr(create_module, "fetch_template_archive", lambda _snapshot: archive)
    targeted: list[tuple[UUID, UUID]] = []

    def capture_target(
        snapshot: TemplateSourceSnapshot,
        _archive: TemplateArchive,
        target_instance_id: UUID,
        _session_factory: sessionmaker[Session],
        *,
        heartbeat: object,
    ) -> bool:
        """Record the template and instance selected by bootstrap."""

        del heartbeat
        targeted.append((snapshot.id, target_instance_id))
        return True

    monkeypatch.setattr(create_module, "sync_template_target", capture_target)
    assert tasks.step_04_sync_templates.run(str(job_id)) == {"status": "success"}
    assert targeted == [(UUID(str(template["id"])), instance_id)]
    async with session_maker() as session:
        stored_instance = await session.get(Instance, instance_id)
        assert stored_instance is not None
        assert stored_instance.status is InstanceStatus.SUCCESS
